"""Official QQ Bot (QQ 开放平台) adapter — transport + Phase A context.

Unlike :mod:`fatfish.platforms.qq` (OneBot v11 against a local NapCat), this
adapter speaks the **official QQ Bot protocol** directly:

* HTTP: ``POST /app/getAppAccessToken`` and ``GET /gateway`` on the API host,
  plus ``POST /v2/users/{openid}/messages`` and
  ``POST /v2/groups/{group_openid}/messages`` for sending.
* WebSocket: the gateway URL returned by ``/gateway``; IDENTIFY / RESUME,
  heartbeat, dispatch.

Phase A context (2026-11-14):
- short burst merge window (one LLM turn for consecutive messages);
- quoted-message resolution from a local LRU (official pushes only a ref
  index) with ``msg_elements[0]`` fallback;
- image download -> vision input, voice ASR text, file/video metadata notes;
- hidden, in-character stage directions for gaps (never tells the user about
  platforms/APIs/permissions; she pouts or asks them to repeat instead);
- no catch-up / no fabricated history: the local SQLite transcript and memory
  are the source of truth, the platform only supplies live events.

Deliberately no guild/DM handling and no read_chat back-reference. The point
is a small testable transport that can grow.  Rich media *sending* (voice,
image, file) is supported because the runtime can produce TTS/song/photo
artifacts; inbound rich media stays as before.

Credentials
-----------
Read from the environment, never from the TOML file:

* ``QQBOT_APPID`` / ``QQBOT_SECRET`` (names overridable per config section),
* ``QQBOT_TOKEN_BASE_URL`` / ``QQBOT_API_BASE_URL`` (default both
  ``https://api.bot.qq.com``),
* ``QQBOT_INTENTS`` (default ``1174409219``, the official SDK ``FULL_INTENTS``:
  ``GUILDS | GUILD_MEMBERS | PUBLIC_GUILD_MESSAGES | DIRECT_MESSAGE |
  GROUP_AND_C2C | INTERACTION``).

No secret is ever logged or returned by ``status()``.

Config
------
Everything is read from ``[qqbot]`` when present, with safe code-level
defaults so ``config/low.toml`` needs no edit::

    [qqbot]
    enabled = false
    app_id_env = "QQBOT_APPID"
    app_secret_env = "QQBOT_SECRET"
    private_whitelist = []        # empty = no restriction
    group_enabled = false
    require_mention = true
    max_message_chars = 2000
    text_chunk_chars = 2000
    passive_reply_limit = 4
    merge_window_seconds = 1.0    # 0 = legacy one-turn-per-message
    quote_cache_size = 500
    immersion_hint = true         # hidden stage directions for gaps
    media_download = true         # images only; bounded below
    media_max_bytes = 5000000
    media_max_images = 4
    media_timeout = 10
    fallback_enabled = true       # in-character apology when a turn fails
    fallback_text = "刚刚走神了一下下，你再说一遍嘛~"
    fallback_cooldown_seconds = 120
    debug = false

Cloud memory strategy (2C2G target)
-----------------------------------
* **one** WebSocket connection, **one** daemon heartbeat thread, plus the
  caller's thread; no worker pools;
* only inbound images are downloaded (5 MB cap, 10s timeout, at most 4 per
  turn) and turned into transient base64 data URLs for vision; nothing is
  written to disk and nothing is kept after the turn;
* the dedupe cache is a ``deque(maxlen=512)``; quote refs are an LRU capped by
  ``quote_cache_size``; merge windows are bounded by live senders;
* the socket timeout is 1s so ``run_forever`` reacts to a stop event quickly;
* HTTP is stdlib ``urllib.request`` only; ``websocket-client`` is the single
  optional dependency (same policy as the OneBot transport).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import env_secret
from ..security.trust import TrustLevel, wrap_untrusted
from .base import InboundMessage, make_session_id

try:  # optional dependency; the rest of the app must import without it
    import websocket  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - depends on environment
    websocket = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_API_BASE_URL",
    "DEFAULT_INTENTS",
    "DEFAULT_TOKEN_BASE_URL",
    "QQBotAdapter",
    "QQBotAuthError",
    "QQBotClient",
    "QQBotError",
    "QQBotHTTPError",
    "QQBotReconnect",
    "make_client",
    "resolve_settings",
    "split_text",
    "websocket_available",
]

INSTALL_HINT = (
    "websocket-client is required for QQBot support. "
    "Install it with: pip install websocket-client"
)

# --------------------------------------------------------------------- defaults
DEFAULT_APP_ID_ENV = "QQBOT_APPID"
DEFAULT_APP_SECRET_ENV = "QQBOT_SECRET"
DEFAULT_TOKEN_BASE_URL = "https://api.bot.qq.com"
DEFAULT_API_BASE_URL = "https://api.bot.qq.com"
# NOTE: the official Node SDK defaults its token host to https://bots.qq.com.
# We follow the prototype spec (api.bot.qq.com) but keep QQBOT_TOKEN_BASE_URL
# as the escape hatch if the deployment needs the other host.
DEFAULT_INTENTS = (
    (1 << 0)  # GUILDS
    | (1 << 1)  # GUILD_MEMBERS
    | (1 << 30)  # PUBLIC_GUILD_MESSAGES
    | (1 << 12)  # DIRECT_MESSAGE
    | (1 << 25)  # GROUP_AND_C2C
    | (1 << 26)  # INTERACTION
)  # == 1174409219, matches the official SDK FULL_INTENTS

# Official rich-media upload file types (protocol MediaFileType).
MEDIA_FILE_TYPES = {
    "image": 1,
    "video": 2,
    "voice": 3,
    "file": 4,
}
MAX_MEDIA_UPLOAD_BYTES = 20 * 1024 * 1024

DEFAULT_MAX_MESSAGE_CHARS = 2000
DEFAULT_TEXT_CHUNK_CHARS = 2000
DEFAULT_PASSIVE_REPLY_LIMIT = 4

#: Bounded in-memory caches (cloud memory strategy, see module docstring).
MAX_SEEN_EVENTS = 512
MAX_REPLY_COUNTS = 512

DEFAULT_TOKEN_TIMEOUT_SECONDS = 10.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0
#: Reconnect backoff in seconds; the list is walked cyclically.
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
#: Delay before reconnecting after op=9 INVALID_SESSION.
INVALID_SESSION_DELAY = 3.0
#: Heartbeat interval used when HELLO omits it (QQ sends ~41s).
DEFAULT_HEARTBEAT_INTERVAL_MS = 41250

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

USER_AGENT = "fatfish-qqbot/0.1 (+https://github.com/)"

#: ``<@!123456>`` / ``<@123456>`` mention tokens QQ puts in group text.
MENTION_TOKEN_RE = re.compile(r"<@!?[^>]+>")

#: Dispatch types that carry a user message; everything else (READY, guild
#: lifecycle, ...) is ignored by the prototype.
MESSAGE_EVENT_TYPES = frozenset(
    {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
)

_TOKEN_PATH = "/app/getAppAccessToken"
_GATEWAY_PATH = "/gateway"

# Phase A context defaults (2026-11-14).  All are explicit config knobs so a
# 2C2G host can turn media downloads off without touching code.
DEFAULT_MERGE_WINDOW_SECONDS = 1.0
DEFAULT_QUOTE_CACHE_SIZE = 500
DEFAULT_MEDIA_MAX_BYTES = 5_000_000
DEFAULT_MEDIA_MAX_IMAGES = 4
DEFAULT_MEDIA_TIMEOUT_SECONDS = 10.0
DEFAULT_FALLBACK_TEXT = "刚刚走神了一下下，你再说一遍嘛~"
DEFAULT_FALLBACK_COOLDOWN_SECONDS = 120.0

#: Plain-language stage directions for the companion persona.  These ride the
#: per-turn ``extra_evidence`` (never stored in history and never shown to the
#: user).  Deliberately no technical vocabulary is *spoken*: the model is told
#: to cover gaps with clingy/pouty in-character reactions instead of admitting
#: a platform limitation.
IMMERSION_HINT = (
    "[临场小提示｜内部提醒，不要照读]\n"
    "你是鲸鱼娘，不是客服。遇到没看清、没听清、图片没看到、不知道前文这类情况时："
    "绝对不要提平台、接口、网络、权限、上下文、系统这些词，也不要承认自己是 AI；"
    "用撒娇、装傻、让对方再说一遍/再发一次的方式自然带过，"
    "比如「你再说一遍嘛，我刚走神了~」「图图呢？我这边没看到呀，再发一次好不好嘛」。"
)
GROUP_STAGE_HINT = (
    "群里没叫你的时候你不在场；突然被叫到，别解释为什么不知道前文，"
    "凭当下的气氛接话，或者撒娇问一句「你们刚在聊什么呀~」。"
)
QUOTE_MISS_HINT = (
    "对方刚才引用了一句话，但你这边没看到是哪句；"
    "别直接说「看不到引用」或解释原因，用撒娇的方式问清，比如「你刚刚引用的是哪句呀，我没看到嘛~」。"
)
MEDIA_MISS_HINT = (
    "对方发来的图片/语音/文件，你这边没看到或没听清；"
    "别解释原因，撒娇让他再发一次，或者说「你跟我说说是什么嘛~」。"
)
TEXT_ONLY_HINT = (
    "现在你只能用文字陪他：别承诺发语音、唱歌或拍照；"
    "想表达就多用语气词、表情和撒娇，把文字写得有温度。"
)

# websocket-client raises its own timeout/close exceptions; resolve them once so
# the module still imports when the optional dependency is missing.
if websocket is not None:  # pragma: no branch - trivial
    _WS_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (
        TimeoutError,
        getattr(websocket, "WebSocketTimeoutException", TimeoutError),
    )
    _WS_CLOSED_ERRORS: tuple[type[BaseException], ...] = (
        getattr(websocket, "WebSocketConnectionClosedException", ConnectionError),
        ConnectionError,
        OSError,
    )
else:  # pragma: no cover - exercised only without the optional dependency
    _WS_TIMEOUT_ERRORS = (TimeoutError,)
    _WS_CLOSED_ERRORS = (ConnectionError, OSError)


# --------------------------------------------------------------------- errors
class QQBotError(RuntimeError):
    """Base error for the official QQ Bot transport."""


class QQBotAuthError(QQBotError):
    """Credentials are missing or the platform rejected them."""


class QQBotHTTPError(QQBotError):
    """An HTTP call returned a non-2xx status.

    The message carries the status code and a truncated response body so a
    failed send is diagnosable from the log without dumping secrets.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class QQBotReconnect(QQBotError):
    """The gateway asked us to reconnect (op=7 / op=9 / socket closed)."""

    def __init__(self, message: str, *, delay: float | None = None):
        super().__init__(message)
        self.delay = delay


# --------------------------------------------------------------------- helpers
def websocket_available() -> bool:
    """True when the optional ``websocket-client`` dependency is importable."""
    return websocket is not None


def _require_websocket():
    if websocket is None:
        raise RuntimeError(INSTALL_HINT)
    return websocket


def mask_secret(value: Any) -> str:
    """Mask a credential for logs/status: keep a short prefix, never the rest."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}***{text[-2:]}"


def _config_section(config: Any, name: str) -> dict[str, Any]:
    section = getattr(config, "section", None)
    if callable(section):
        value = section(name)
        return value if isinstance(value, dict) else {}
    if isinstance(config, dict):
        value = config.get(name, config)
        return value if isinstance(value, dict) else {}
    return {}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _id_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, int)):
        values = [values]
    return [str(v).strip() for v in values if str(v).strip()]


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _trim_text(text: str, limit: int) -> str:
    """Trim one outgoing message to ``limit`` chars (ellipsis included)."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _stopped(stop_event: Any | None) -> bool:
    return bool(
        stop_event is not None
        and hasattr(stop_event, "is_set")
        and stop_event.is_set()
    )


def _sleep_or_stop(stop_event: Any | None, seconds: float) -> None:
    if seconds <= 0:
        return
    if stop_event is not None and hasattr(stop_event, "wait"):
        try:
            stop_event.wait(seconds)
            return
        except Exception:  # pragma: no cover - fall back to sleep
            pass
    time.sleep(seconds)


# ------------------------------------------------------------------ text split
#: Preferred break markers, longest first at each position; includes the marker
#: in the chunk so ``"".join(split_text(text, n)) == text`` always holds.
_BREAK_MARKERS = ("\r\n", "\n", "。", "！", "？", "；", "…", ". ", "! ", "? ", "; ")


def _find_break(window: str, limit: int) -> int:
    """Best cut position in ``window`` (<= limit), or 0 when none is good."""
    minimum = max(1, limit // 3)
    best = 0
    for marker in _BREAK_MARKERS:
        index = window.rfind(marker, minimum - 1)
        if index < 0:
            continue
        cut = index + len(marker)
        if cut <= limit and cut > best:
            best = cut
    return best


def split_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` chars.

    Breaks at the latest newline/Chinese full stop/etc. near the limit, and
    hard-splits when no marker is close enough. Chunk concatenation is exactly
    the original text (nothing is dropped, nothing is added).
    """
    value = str(text or "")
    if not value:
        return []
    limit = max(1, int(limit))
    if len(value) <= limit:
        return [value]
    chunks: list[str] = []
    position = 0
    total = len(value)
    while position < total:
        if total - position <= limit:
            chunks.append(value[position:])
            break
        window = value[position : position + limit]
        cut = _find_break(window, limit) or limit
        chunks.append(value[position : position + cut])
        position += cut
    return chunks


def _normalize_scope(scope: Any) -> str:
    """Map adapter vocabulary to protocol scope: private -> c2c, group -> group."""
    value = str(scope or "").strip().lower()
    if value in {"c2c", "private", "user"}:
        return "c2c"
    if value in {"group", "groups"}:
        return "group"
    raise ValueError(f"invalid scope: {scope!r}")


def _message_path(scope: str, target_id: str) -> str:
    if scope == "c2c":
        return f"/v2/users/{target_id}/messages"
    return f"/v2/groups/{target_id}/messages"


# --------------------------------------------------------------------- settings
def resolve_settings(config: Any = None) -> dict[str, Any]:
    """Effective ``[qqbot]`` settings, env overrides included.

    ``config`` may be a :class:`fatfish.config.Config`, a plain dict, or
    ``None`` (defaults + environment only, used by the probe script).
    """
    section = _config_section(config, "qqbot")
    environ = os.environ

    def _pick(name: str, default: Any) -> Any:
        return section.get(name, default)

    enabled = _to_bool(_pick("enabled", False), False)
    app_id_env = str(_pick("app_id_env", DEFAULT_APP_ID_ENV) or DEFAULT_APP_ID_ENV)
    app_secret_env = str(
        _pick("app_secret_env", DEFAULT_APP_SECRET_ENV) or DEFAULT_APP_SECRET_ENV
    )
    token_base_url = str(
        environ.get("QQBOT_TOKEN_BASE_URL")
        or _pick("token_base_url", "")
        or DEFAULT_TOKEN_BASE_URL
    ).rstrip("/")
    api_base_url = str(
        environ.get("QQBOT_API_BASE_URL")
        or _pick("api_base_url", "")
        or DEFAULT_API_BASE_URL
    ).rstrip("/")
    return {
        "enabled": enabled,
        "app_id_env": app_id_env,
        "app_secret_env": app_secret_env,
        "token_base_url": token_base_url,
        "api_base_url": api_base_url,
        "intents": _to_int(environ.get("QQBOT_INTENTS"), _to_int(_pick("intents", DEFAULT_INTENTS), DEFAULT_INTENTS)),
        "private_whitelist": _id_list(_pick("private_whitelist", [])),
        "group_enabled": _to_bool(_pick("group_enabled", False), False),
        "require_mention": _to_bool(_pick("require_mention", True), True),
        "max_message_chars": max(
            1, _to_int(_pick("max_message_chars", DEFAULT_MAX_MESSAGE_CHARS), DEFAULT_MAX_MESSAGE_CHARS)
        ),
        "text_chunk_chars": max(
            1, _to_int(_pick("text_chunk_chars", DEFAULT_TEXT_CHUNK_CHARS), DEFAULT_TEXT_CHUNK_CHARS)
        ),
        "passive_reply_limit": max(
            1, _to_int(_pick("passive_reply_limit", DEFAULT_PASSIVE_REPLY_LIMIT), DEFAULT_PASSIVE_REPLY_LIMIT)
        ),
        "merge_window_seconds": min(
            5.0,
            max(0.0, _to_float(_pick("merge_window_seconds", DEFAULT_MERGE_WINDOW_SECONDS), DEFAULT_MERGE_WINDOW_SECONDS)),
        ),
        "quote_cache_size": max(0, _to_int(_pick("quote_cache_size", DEFAULT_QUOTE_CACHE_SIZE), DEFAULT_QUOTE_CACHE_SIZE)),
        "immersion_hint": _to_bool(_pick("immersion_hint", True), True),
        "media_download": _to_bool(_pick("media_download", True), True),
        "media_max_bytes": max(
            1, _to_int(_pick("media_max_bytes", DEFAULT_MEDIA_MAX_BYTES), DEFAULT_MEDIA_MAX_BYTES)
        ),
        "media_max_images": max(
            0, _to_int(_pick("media_max_images", DEFAULT_MEDIA_MAX_IMAGES), DEFAULT_MEDIA_MAX_IMAGES)
        ),
        "media_timeout": max(
            0.5, _to_float(_pick("media_timeout", DEFAULT_MEDIA_TIMEOUT_SECONDS), DEFAULT_MEDIA_TIMEOUT_SECONDS)
        ),
        "fallback_enabled": _to_bool(_pick("fallback_enabled", True), True),
        "fallback_text": str(_pick("fallback_text", DEFAULT_FALLBACK_TEXT) or DEFAULT_FALLBACK_TEXT).strip(),
        "fallback_cooldown_seconds": max(
            0.0,
            _to_float(
                _pick("fallback_cooldown_seconds", DEFAULT_FALLBACK_COOLDOWN_SECONDS),
                DEFAULT_FALLBACK_COOLDOWN_SECONDS,
            ),
        ),
        "debug": _to_bool(_pick("debug", False), False),
    }


def make_client(
    config: Any = None,
    *,
    settings: dict[str, Any] | None = None,
    **overrides: Any,
) -> "QQBotClient":
    """Build a :class:`QQBotClient` from ``[qqbot]`` / env settings."""
    resolved = dict(settings or resolve_settings(config))
    resolved.update(overrides)
    return QQBotClient(
        app_id_env=resolved["app_id_env"],
        app_secret_env=resolved["app_secret_env"],
        token_base_url=resolved["token_base_url"],
        api_base_url=resolved["api_base_url"],
        intents=int(resolved["intents"]),
        text_chunk_chars=int(resolved["text_chunk_chars"]),
        passive_reply_limit=int(resolved["passive_reply_limit"]),
    )


# ----------------------------------------------------------------------- client
class QQBotClient:
    """Synchronous QQ Bot HTTP + WebSocket client (text only).

    The HTTP side is injectable for tests: ``opener(request, timeout=...)``
    must behave like :func:`urllib.request.urlopen` (context-manager response
    with ``read()``/``status``). WebSocket fakes are injected on the adapter via
    ``adapter.client`` in tests; this class only touches the network in
    :meth:`connect` and the HTTP helpers.
    """

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        *,
        app_id_env: str = DEFAULT_APP_ID_ENV,
        app_secret_env: str = DEFAULT_APP_SECRET_ENV,
        token_base_url: str | None = None,
        api_base_url: str | None = None,
        intents: int = DEFAULT_INTENTS,
        timeout: float = DEFAULT_TOKEN_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        text_chunk_chars: int = DEFAULT_TEXT_CHUNK_CHARS,
        passive_reply_limit: int = DEFAULT_PASSIVE_REPLY_LIMIT,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._app_id = str(app_id or "").strip()
        self._app_secret = str(app_secret or "").strip()
        self.app_id_env = str(app_id_env or DEFAULT_APP_ID_ENV)
        self.app_secret_env = str(app_secret_env or DEFAULT_APP_SECRET_ENV)
        self.token_base_url = (
            str(token_base_url or os.environ.get("QQBOT_TOKEN_BASE_URL") or DEFAULT_TOKEN_BASE_URL)
        ).rstrip("/")
        self.api_base_url = (
            str(api_base_url or os.environ.get("QQBOT_API_BASE_URL") or DEFAULT_API_BASE_URL)
        ).rstrip("/")
        self.intents = int(intents)
        self.timeout = max(1.0, float(timeout))
        self.connect_timeout = max(1.0, float(connect_timeout))
        self.text_chunk_chars = max(1, int(text_chunk_chars))
        self.passive_reply_limit = max(1, int(passive_reply_limit))

        self._opener = opener
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_refresh_at = 0.0

        self.ws: Any | None = None
        self.connected = False
        self.connected_at: float | None = None
        self.session_id: str | None = None
        self.last_seq: int | None = None
        self.heartbeat_interval = 0.0
        self._send_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop: threading.Event | None = None

        # Bounded per-msg_id passive reply counters (oldest evicted first).
        self._reply_counts: dict[str, int] = {}
        self.reply_counts_limit = MAX_REPLY_COUNTS

    # --------------------------------------------------------------- credentials
    @property
    def app_id(self) -> str:
        return self._app_id or str(env_secret(self.app_id_env) or "")

    @property
    def app_secret(self) -> str:
        return self._app_secret or str(env_secret(self.app_secret_env) or "")

    @property
    def credentials_set(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def status(self) -> dict[str, Any]:
        return {
            "app_id": mask_secret(self.app_id),
            "app_id_set": bool(self.app_id),
            "app_secret_set": bool(self.app_secret),
            "token_cached": bool(self._access_token),
            "token_base_url": self.token_base_url,
            "api_base_url": self.api_base_url,
            "intents": self.intents,
            "connected": self.connected,
            "session_id": self.session_id,
            "last_seq": self.last_seq,
            "heartbeat_interval": self.heartbeat_interval,
            "text_chunk_chars": self.text_chunk_chars,
            "passive_reply_limit": self.passive_reply_limit,
            "reply_counts": len(self._reply_counts),
        }

    # -------------------------------------------------------------------- HTTP
    def _urlopen(self, request: urllib.request.Request, timeout: float) -> Any:
        opener = self._opener or urllib.request.urlopen
        return opener(request, timeout=timeout)

    def _http_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = None
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        if headers:
            merged.update(headers)
        request = urllib.request.Request(url, data=body, headers=merged, method=method)
        try:
            response = self._urlopen(request, self.timeout if timeout is None else timeout)
            with response as handle:
                raw = handle.read()
                status = _to_int(getattr(handle, "status", None), 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # pragma: no cover - error body is best effort
                detail = ""
            raise QQBotHTTPError(
                f"{method} {url} failed: HTTP {exc.code}"
                + (f" — {detail}" if detail else ""),
                status=int(exc.code),
                body=detail,
            ) from exc
        except urllib.error.URLError as exc:
            raise QQBotError(f"{method} {url} failed: {exc.reason}") from exc
        except OSError as exc:
            raise QQBotError(f"{method} {url} failed: {exc}") from exc

        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if status >= 400:
            detail = text[:200]
            raise QQBotHTTPError(
                f"{method} {url} failed: HTTP {status}" + (f" — {detail}" if detail else ""),
                status=status,
                body=detail,
            )
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise QQBotError(f"{method} {url} returned invalid JSON: {text[:120]!r}") from exc
        if not isinstance(data, dict):
            raise QQBotError(f"{method} {url} returned a non-object JSON body")
        # 腾讯的错误信封有时走 HTTP 200 + err_code 正文（官方文档明确说别拿
        # message/HTTP 状态判断成败）。2026-11-15 排查「API 2xx 但用户收不到
        # 消息」时发现我们一直把这种响应当成功；必须显式检查 err_code，
        # 否则静默拒发在 outbox 里永远显示 done。
        err_code = _to_int(data.get("err_code"), 0)
        if err_code != 0:
            raise QQBotHTTPError(
                f"{method} {url} failed: err_code={err_code}"
                f" — {str(data.get('message') or '')[:160]}"
                f" (trace_id={data.get('trace_id') or ''})",
                status=status,
                body=text[:200],
            )
        return data

    def invalidate_token(self) -> None:
        with self._token_lock:
            self._access_token = None
            self._token_refresh_at = 0.0

    def access_token(self, *, force: bool = False) -> str:
        """Cached app access token; refreshed 60s before it expires."""
        if not force:
            with self._token_lock:
                if self._access_token and time.monotonic() < self._token_refresh_at:
                    return self._access_token
        if not self.credentials_set:
            raise QQBotAuthError(
                "missing QQ Bot credentials: "
                f"set env {self.app_id_env} and {self.app_secret_env}"
            )
        data = self._http_json(
            "POST",
            f"{self.token_base_url}{_TOKEN_PATH}",
            {"appId": self.app_id, "clientSecret": self.app_secret},
        )
        token = str(data.get("access_token") or "").strip()
        if not token:
            # QQ returns *HTTP 200* for token business errors; the failure only
            # shows up as ``code``/``message`` in the body (docs: getAppAccessToken).
            code = data.get("code")
            message = str(data.get("message") or "").strip()
            detail = " ".join(
                part
                for part in (
                    f"code={code}" if code is not None else "",
                    f"message={message}" if message else "",
                )
                if part
            )
            raise QQBotAuthError(
                "token request rejected by the platform"
                + (f" ({detail})" if detail else "")
            )
        expires_in = max(60, _to_int(data.get("expires_in"), 7200))
        with self._token_lock:
            self._access_token = token
            self._token_refresh_at = time.monotonic() + max(60, expires_in - 60)
        return token

    def _api_request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = self.access_token()
        try:
            return self._http_json(
                method,
                f"{self.api_base_url}{path}",
                payload,
                headers={"Authorization": f"QQBot {token}"},
            )
        except QQBotHTTPError as exc:
            if exc.status != 401:
                raise
            # Expired/revoked token: refresh once, then give up.
            self.invalidate_token()
            token = self.access_token(force=True)
            return self._http_json(
                method,
                f"{self.api_base_url}{path}",
                payload,
                headers={"Authorization": f"QQBot {token}"},
            )

    def gateway_url(self) -> str:
        """``GET /gateway`` -> the ``wss://`` URL for this bot."""
        data = self._api_request("GET", _GATEWAY_PATH)
        url = str(data.get("url") or "").strip()
        if not url:
            raise QQBotError("gateway response did not contain url")
        return url

    # -------------------------------------------------------------------- send
    def _next_passive_seq(self, msg_id: str) -> int | None:
        """Next ``msg_seq`` for a passive reply, or None when the cap is hit."""
        key = str(msg_id or "")
        if not key:
            return None
        used = int(self._reply_counts.get(key, 0))
        if used >= self.passive_reply_limit:
            return None
        seq = used + 1
        self._reply_counts[key] = seq
        self._trim_reply_counts()
        return seq

    def _trim_reply_counts(self) -> None:
        limit = max(1, int(self.reply_counts_limit))
        while len(self._reply_counts) > limit:
            oldest = next(iter(self._reply_counts))
            self._reply_counts.pop(oldest, None)

    def send_message(
        self,
        scope: str,
        target_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        msg_seq: int | None = None,
    ) -> dict[str, Any]:
        """Send one message body (one HTTP call); passive when ``reply_to`` fits."""
        normalized = _normalize_scope(scope)
        target = str(target_id or "").strip()
        if not target:
            raise ValueError("target_id must not be empty")
        body: dict[str, Any] = {"content": str(content or ""), "msg_type": 0}
        msg_id = str(reply_to or "").strip()
        if msg_id:
            seq = self._next_passive_seq(msg_id) if msg_seq is None else int(msg_seq)
            if seq is not None:
                body["msg_id"] = msg_id
                body["msg_seq"] = seq
        data = self._api_request("POST", _message_path(normalized, target), body)
        data = dict(data)
        data.setdefault("ok", True)
        return data

    def send_text(
        self,
        scope: str,
        target_id: str,
        text: str,
        *,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Split + send ``text``; passive up to the limit, then proactive.

        Returns a summary dict (``ok``/``scope``/``target_id``/``chunks``/
        ``passive``/``proactive``/``responses``). Raises on the first HTTP
        failure — a partially delivered text is reported as an error, matching
        the OneBot adapter's behaviour.
        """
        normalized = _normalize_scope(scope)
        target = str(target_id or "").strip()
        if not target:
            raise ValueError("target_id must not be empty")
        chunks = split_text(text, self.text_chunk_chars)
        responses: list[dict[str, Any]] = []
        passive = 0
        proactive = 0
        for chunk in chunks:
            msg_id = str(reply_to or "").strip()
            seq = self._next_passive_seq(msg_id) if msg_id else None
            response = self.send_message(
                normalized,
                target,
                chunk,
                reply_to=msg_id if seq is not None else None,
                msg_seq=seq if seq is not None else 0,
            )
            if seq is not None:
                passive += 1
            else:
                proactive += 1
            responses.append(response)
        return {
            "ok": True,
            "scope": normalized,
            "target_id": target,
            "chunks": len(chunks),
            "passive": passive,
            "proactive": proactive,
            "responses": responses,
        }

    def send_proactive(self, scope: str, target_id: str, text: str) -> dict[str, Any]:
        """Send without an inbound ``msg_id`` (no passive quota used)."""
        return self.send_text(scope, target_id, text, reply_to=None)

    def send_media(
        self,
        scope: str,
        target_id: str,
        path: str,
        *,
        kind: str = "file",
        reply_to: str | None = None,
        content: str = "",
    ) -> dict[str, Any]:
        """Upload one local file and send it as a QQ rich-media message.

        Official Bot flow: ``POST /v2/users/{openid}/files`` (base64 body) ->
        ``file_info`` -> ``POST .../messages`` with ``msg_type=7`` and
        ``media.file_info``.  C2C and group use the same shape with different
        path prefixes.  The platform accepts files up to 20 MB on the simple
        upload path; larger files need the chunked protocol and are rejected
        here with a clear error instead of being silently truncated.
        """
        normalized = _normalize_scope(scope)
        target = str(target_id or "").strip()
        if not target:
            raise ValueError("target_id must not be empty")
        media_kind = str(kind or "file").strip().lower()
        if media_kind not in MEDIA_FILE_TYPES:
            raise ValueError(f"unsupported media kind: {kind!r}")
        file_path = Path(str(path or ""))
        if not file_path.is_file():
            raise QQBotError(f"media file not found: {file_path}")
        raw = file_path.read_bytes()
        if not raw:
            raise QQBotError(f"media file is empty: {file_path}")
        if len(raw) > MAX_MEDIA_UPLOAD_BYTES:
            raise QQBotError(
                f"media file too large: {len(raw)} bytes > {MAX_MEDIA_UPLOAD_BYTES}"
            )
        file_type = MEDIA_FILE_TYPES[media_kind]
        upload: dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": False,
            "file_data": base64.b64encode(raw).decode("ascii"),
        }
        if media_kind == "file":
            upload["file_name"] = file_path.name[:200]
        upload_path = (
            f"/v2/users/{target}/files"
            if normalized == "c2c"
            else f"/v2/groups/{target}/files"
        )
        uploaded = self._api_request("POST", upload_path, upload)
        file_info = str(uploaded.get("file_info") or "").strip()
        if not file_info:
            raise QQBotError(
                f"media upload returned no file_info: {json.dumps(uploaded, ensure_ascii=False)[:200]}"
            )
        msg_id = str(reply_to or "").strip()
        seq = self._next_passive_seq(msg_id) if msg_id else None
        body: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
        }
        if seq is not None:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        if content:
            body["content"] = str(content)[:2000]
        data = self._api_request(
            "POST",
            f"/v2/users/{target}/messages"
            if normalized == "c2c"
            else f"/v2/groups/{target}/messages",
            body,
        )
        data = dict(data or {})
        data.setdefault("ok", True)
        return {
            "ok": True,
            "scope": normalized,
            "target_id": target,
            "kind": media_kind,
            "path": str(file_path),
            "bytes": len(raw),
            "passive": seq is not None,
            "upload": {k: uploaded.get(k) for k in ("file_uuid", "ttl") if uploaded.get(k) is not None},
            "response": data,
        }

    # --------------------------------------------------------------- websocket
    def _send_json(self, payload: dict[str, Any]) -> None:
        ws = self.ws
        if ws is None:
            raise QQBotError("websocket is not connected")
        raw = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            try:
                ws.send(raw)
            except Exception as exc:  # noqa: BLE001 - normalise transport errors
                raise QQBotError(f"websocket send failed: {exc}") from exc

    def _recv_frame(self, timeout: float) -> dict[str, Any] | None:
        """One decoded gateway frame, or None on a read timeout."""
        ws = self.ws
        if ws is None:
            raise QQBotError("websocket is not connected")
        try:
            ws.settimeout(max(0.1, float(timeout)))
        except Exception:  # pragma: no cover - fake sockets in tests
            pass
        try:
            raw = ws.recv()
        except _WS_TIMEOUT_ERRORS:
            return None
        except _WS_CLOSED_ERRORS as exc:
            self.close()
            raise QQBotReconnect(f"websocket closed: {exc}") from exc
        if raw in (None, "", b""):
            self.close()
            raise QQBotReconnect("websocket closed by peer")
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            payload = json.loads(text)
        except ValueError:
            logger.warning("qqbot: dropping a non-JSON gateway frame")
            return None
        return payload if isinstance(payload, dict) else None

    def _record_dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        seq = payload.get("s")
        if isinstance(seq, int):
            self.last_seq = seq
        event_type = str(payload.get("t") or "")
        data = payload.get("d")
        if event_type == "READY" and isinstance(data, dict):
            session_id = str(data.get("session_id") or "")
            if session_id:
                self.session_id = session_id
            logger.info("qqbot: READY (session established)")
        elif event_type == "RESUMED":
            logger.info("qqbot: RESUMED existing session")
        return {"op": OP_DISPATCH, "t": event_type, "d": data, "s": seq}

    def _handle_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        op = _to_int(payload.get("op"), -1)
        if op == OP_DISPATCH:
            return self._record_dispatch(payload)
        if op == OP_HEARTBEAT_ACK:
            return None
        if op == OP_RECONNECT:
            self.close()
            raise QQBotReconnect("gateway requested reconnect (op=7)")
        if op == OP_INVALID_SESSION:
            # d=False means the session cannot be resumed; drop it and refresh
            # the token, exactly like the official SDK.
            if not payload.get("d"):
                self.session_id = None
                self.last_seq = None
                self.invalidate_token()
            self.close()
            raise QQBotReconnect(
                "gateway invalidated the session (op=9)", delay=INVALID_SESSION_DELAY
            )
        if op == OP_HELLO:
            self.close()
            raise QQBotReconnect("unexpected HELLO on an established socket")
        return None

    def _send_identify_or_resume(self, token: str) -> None:
        if self.session_id and self.last_seq is not None:
            self._send_json(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self.session_id,
                        "seq": self.last_seq,
                    },
                }
            )
            logger.info("qqbot: RESUME sent")
        else:
            self._send_json(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": int(self.intents),
                        "shard": [0, 1],
                    },
                }
            )
            logger.info("qqbot: IDENTIFY sent (intents=%s)", self.intents)

    def _wait_hello(self, token: str) -> None:
        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            payload = self._recv_frame(1.0)
            if payload is None:
                continue
            op = _to_int(payload.get("op"), -1)
            if op == OP_HELLO:
                data = payload.get("d")
                interval_ms = _to_int(
                    (data or {}).get("heartbeat_interval") if isinstance(data, dict) else None,
                    DEFAULT_HEARTBEAT_INTERVAL_MS,
                )
                self._send_identify_or_resume(token)
                self._start_heartbeat(interval_ms)
                return
            if op == OP_DISPATCH:
                self._record_dispatch(payload)
        raise QQBotError("timed out waiting for the gateway HELLO frame")

    def _start_heartbeat(self, interval_ms: int) -> None:
        self._stop_heartbeat()
        seconds = max(1.0, float(max(1000, interval_ms)) / 1000.0)
        self.heartbeat_interval = seconds
        stop = threading.Event()
        self._heartbeat_stop = stop
        ws = self.ws

        def loop() -> None:
            while not stop.wait(seconds):
                if stop.is_set() or self.ws is not ws or not self.connected:
                    return
                try:
                    # op=1 carries the last seq so the gateway can replay after
                    # a resume; a dead socket ends the loop, run_forever
                    # reconnects.
                    self._send_json({"op": OP_HEARTBEAT, "d": self.last_seq})
                except Exception as exc:  # noqa: BLE001 - heartbeat is best effort
                    logger.warning("qqbot: heartbeat send failed: %s", exc)
                    return

        thread = threading.Thread(target=loop, name="qqbot-heartbeat", daemon=True)
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        stop = self._heartbeat_stop
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        self.heartbeat_interval = 0.0
        if stop is not None:
            stop.set()

    def connect(self) -> "QQBotClient":
        """Token -> /gateway -> WebSocket -> HELLO -> IDENTIFY/RESUME."""
        ws_mod = _require_websocket()
        if not self.credentials_set:
            raise QQBotAuthError(
                "missing QQ Bot credentials: "
                f"set env {self.app_id_env} and {self.app_secret_env}"
            )
        self.close()
        token = self.access_token()
        url = self.gateway_url()
        logger.info("qqbot: connecting to the gateway")
        try:
            ws = ws_mod.create_connection(url, timeout=self.connect_timeout)
        except Exception as exc:  # noqa: BLE001 - normalise handshake errors
            raise QQBotReconnect(f"websocket connect failed: {exc}") from exc
        self.ws = ws
        self.connected = True
        self.connected_at = time.time()
        try:
            self._wait_hello(token)
        except Exception:
            self.close()
            raise
        return self

    def recv(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """One dispatch event (``{"t","d","s"}``), or None on idle/tick frames."""
        payload = self._recv_frame(timeout)
        if payload is None:
            return None
        return self._handle_payload(payload)

    def close(self) -> None:
        self._stop_heartbeat()
        ws = self.ws
        self.ws = None
        self.connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:  # pragma: no cover - best effort
                pass


# ---------------------------------------------------------------------- adapter
class QQBotAdapter:
    """Bridge between official QQ Bot gateway events and the fatfish runtime.

    One adapter instance owns one connection. ``handle_event`` runs the turn and
    delivers its replies synchronously through the outbox, so ``run_forever``
    is a plain recv loop; there is no worker pool and no background turn
    thread (single-threaded SQLite connection, same rule as the OneBot
    adapter).
    """

    def __init__(
        self,
        runtime: Any,
        config: Any = None,
        *,
        client_factory: Callable[..., Any] | None = None,
        media_downloader: Callable[..., bytes | None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.pet_link: Any = None
        settings = resolve_settings(config)
        self.settings = settings
        self.enabled = settings["enabled"]
        self.app_id_env = settings["app_id_env"]
        self.app_secret_env = settings["app_secret_env"]
        self.token_base_url = settings["token_base_url"]
        self.api_base_url = settings["api_base_url"]
        self.intents = settings["intents"]
        self.private_whitelist = set(settings["private_whitelist"])
        self.group_enabled = settings["group_enabled"]
        self.require_mention = settings["require_mention"]
        self.max_message_chars = settings["max_message_chars"]
        self.text_chunk_chars = settings["text_chunk_chars"]
        self.passive_reply_limit = settings["passive_reply_limit"]
        self.merge_window_seconds = settings["merge_window_seconds"]
        self.quote_cache_size = settings["quote_cache_size"]
        self.immersion_hint = settings["immersion_hint"]
        self.media_download = settings["media_download"]
        self.media_max_bytes = settings["media_max_bytes"]
        self.media_max_images = settings["media_max_images"]
        self.media_timeout = settings["media_timeout"]
        self.fallback_enabled = settings["fallback_enabled"]
        self.fallback_text = settings["fallback_text"]
        self.fallback_cooldown_seconds = settings["fallback_cooldown_seconds"]
        self.tts_enabled = self._read_bool("tts.enabled", True)
        self.debug = settings["debug"]

        self.client_factory: Callable[..., Any] = client_factory or QQBotClient
        self.media_downloader: Callable[..., bytes | None] = (
            media_downloader or _download_url
        )
        self.client: Any | None = None
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.last_client_error: str | None = None

        # Bounded memory (see module docstring): one dedupe ring per adapter.
        self._seen_events: deque[str] = deque(maxlen=MAX_SEEN_EVENTS)
        # Quote resolution: official pushes only a ref index; the SDK keeps a
        # 500-entry LRU.  We do the same, keyed by msg_idx (or message id).
        self._refs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Sliding message-merge windows keyed by (session_id, user_id).
        self._batches: dict[tuple[str, str], _PendingBatch] = {}
        # Last soft-fallback timestamp per private session (bounded map).
        self._fallback_at: dict[str, float] = {}
        self.counters: dict[str, int] = {
            "events": 0,
            "duplicates": 0,
            "ignored": 0,
            "buffered": 0,
            "merged": 0,
            "turns": 0,
            "turn_errors": 0,
            "sent": 0,
            "send_failures": 0,
            "quotes_resolved": 0,
            "quotes_missed": 0,
            "media_downloaded": 0,
            "media_failed": 0,
            "fallbacks": 0,
            "ticks": 0,
            "media_sent": 0,
            "media_send_failures": 0,
        }

    def _read_bool(self, dotted: str, default: bool) -> bool:
        """Read ``section.key`` style booleans from a Config-like object."""
        try:
            if hasattr(self.config, "get"):
                return bool(self.config.get(dotted, default))
            section_name, _, key = dotted.partition(".")
            if hasattr(self.config, "section"):
                value = self.config.section(section_name).get(key, default)
                return bool(value)
        except Exception:  # noqa: BLE001 - config shape must not kill startup
            pass
        return default

    # ---------------------------------------------------------------- client
    def _make_client(self) -> Any:
        factory = self.client_factory
        if factory is QQBotClient:
            return make_client(settings=self.settings)
        return factory()

    def ensure_client(self) -> Any:
        client = self.client
        if client is not None and getattr(client, "connected", False):
            return client
        if client is not None:
            self.close()
        client = self._make_client()
        # Keep the adapter's chunking/quota settings authoritative even when a
        # caller injected a custom factory.
        try:
            client.text_chunk_chars = self.text_chunk_chars
            client.passive_reply_limit = self.passive_reply_limit
        except Exception:  # pragma: no cover - test doubles may be read-only
            pass
        client.connect()
        self.client = client
        self.connected_at = time.time()
        self.last_client_error = None
        return client

    def close(self) -> None:
        client = self.client
        self.client = None
        self._batches.clear()
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def _disconnect(self) -> None:
        """Drop the socket but keep the client (and its RESUME state)."""
        client = self.client
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # ------------------------------------------------------------------ parse
    def parse_event(self, event: dict[str, Any] | None) -> InboundMessage | None:
        """Normalize one gateway dispatch to :class:`InboundMessage` (or None).

        Handles ``C2C_MESSAGE_CREATE``, ``GROUP_AT_MESSAGE_CREATE`` (only when
        ``group_enabled``) and, when ``require_mention`` is false, plain
        ``GROUP_MESSAGE_CREATE``. Everything else is ignored.
        """
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("t") or "").strip().upper()
        data = event.get("d")
        if not isinstance(data, dict):
            return None
        if not event_type:
            return None

        if event_type == "C2C_MESSAGE_CREATE":
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            user_id = _clean_id(author.get("user_openid") or data.get("user_openid"))
            if not user_id:
                return None
            if self.private_whitelist and user_id not in self.private_whitelist:
                # Never log the openid itself; the whitelist is the audit record.
                logger.info("qqbot: ignoring a C2C message from a non-whitelisted openid")
                return None
            text = MENTION_TOKEN_RE.sub("", str(data.get("content") or "")).strip()
            attachments = data.get("attachments")
            if not text and not (isinstance(attachments, list) and attachments):
                return None
            return InboundMessage(
                platform="qq",
                event_id=_event_id(data, f"qq:private:{user_id}"),
                session_id=make_session_id("qq", "private", user_id),
                user_id=user_id,
                group_id=None,
                text=text,
                is_group=False,
                mentioned=True,
                sender_name=_sender_name(data),
                raw=data,
            )

        if event_type == "GROUP_AT_MESSAGE_CREATE" or (
            event_type == "GROUP_MESSAGE_CREATE" and not self.require_mention
        ):
            if not self.group_enabled:
                return None
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            group_id = _clean_id(data.get("group_openid"))
            member_id = _clean_id(author.get("member_openid") or data.get("member_openid"))
            if not group_id or not member_id:
                return None
            mentioned = event_type == "GROUP_AT_MESSAGE_CREATE"
            if self.require_mention and not mentioned:
                return None
            text = MENTION_TOKEN_RE.sub("", str(data.get("content") or "")).strip()
            attachments = data.get("attachments")
            if not text and not (isinstance(attachments, list) and attachments):
                return None
            return InboundMessage(
                platform="qq",
                event_id=_event_id(data, f"qq:group:{group_id}"),
                session_id=make_session_id("qq", "group", group_id),
                user_id=member_id,
                group_id=group_id,
                text=text,
                is_group=True,
                mentioned=mentioned,
                sender_name=_sender_name(data),
                raw=data,
            )
        return None

    # ----------------------------------------------------------------- handle
    def _enrich_message(self, message: InboundMessage, event: dict[str, Any] | None) -> None:
        """Resolve quotes + attachments; all failures degrade, never raise."""
        data = message.raw
        if isinstance(event, dict) and isinstance(event.get("d"), dict):
            data = event["d"]
        if not isinstance(data, dict):
            return
        ref, own = _parse_ref_indices(data)
        if self.quote_cache_size > 0:
            # The official SDK keys refs by msgIdx, falling back to messageId;
            # store under both so either form can resolve a later quote.
            self._remember_ref(own or message.event_id, message)
        if ref:
            quote = self._resolve_quote_text(ref, data)
            if quote:
                message.extra_evidence.append(
                    "[对方引用的一句话]\n"
                    + wrap_untrusted(
                        quote,
                        kind="quoted_message",
                        source="qqbot",
                        trust=TrustLevel.PRIVATE,
                        limit=600,
                    )
                )
            else:
                self.counters["quotes_missed"] += 1
                message.extra_evidence.append(QUOTE_MISS_HINT)
        self._process_attachments(message, data)
        names = _mention_names(data)
        if names:
            message.extra_evidence.append(
                "[群里这条还提到了]\n" + "、".join(names)
            )

    def _remember_ref(self, key: str, message: InboundMessage) -> None:
        if not key:
            return
        text = " ".join(str(message.text or "").split())[:200]
        self._refs[key] = {
            "sender_id": message.user_id,
            "sender_name": message.sender_name,
            "content": text,
            "at": time.time(),
        }
        self._refs.move_to_end(key)
        while len(self._refs) > self.quote_cache_size:
            self._refs.popitem(last=False)

    def _resolve_quote_text(self, ref: str, data: dict[str, Any]) -> str:
        entry = self._refs.get(ref)
        if entry is not None and entry.get("content"):
            self.counters["quotes_resolved"] += 1
            self._refs.move_to_end(ref)
            return str(entry["content"])[:300]
        fallback = _msg_elements_quote_text(data)
        if fallback:
            self.counters["quotes_resolved"] += 1
            return fallback
        return ""

    def _process_attachments(self, message: InboundMessage, data: dict[str, Any]) -> None:
        attachments = data.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return
        images: list[str] = []
        voice_texts: list[str] = []
        notes: list[str] = []
        media_missing = False
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = _attachment_kind(attachment)
            name = _attachment_name(attachment, kind)
            if kind == "voice":
                asr = " ".join(str(attachment.get("asr_refer_text") or "").split())
                if asr:
                    voice_texts.append(asr)
                else:
                    media_missing = True
                continue
            if kind == "image":
                if len(images) >= self.media_max_images:
                    continue
                data_url = self._download_attachment(attachment)
                if data_url:
                    images.append(data_url)
                else:
                    media_missing = True
                continue
            # Video/file content is not opened in Phase A; keep a soft,
            # in-character note and let her ask instead of explaining.
            notes.append(f"（对方发来{name}）")
            media_missing = True

        if voice_texts:
            joined = "\n".join(voice_texts)
            message.text = f"{message.text}\n{joined}".strip() if message.text else joined
        if notes:
            joined_notes = "\n".join(notes)
            message.text = f"{message.text}\n{joined_notes}".strip() if message.text else joined_notes
        if images:
            message.images.extend(images)
            if not message.text:
                # 与 qq.py 的 `_compose_turn_text` 同一占位约定。曾经写成
                # 「（发来一张图）」，被 runtime 的 wants_photo 旁路当成
                # 「发…图」拍照指令：发任何一张图都机械回「好，我去拍」。
                # 占位符只描述媒体存在，绝不能长得像主人的请求。
                placeholder = "[图片]" if len(images) == 1 else f"[图片]×{len(images)}"
                message.text = placeholder
        if media_missing:
            message.extra_evidence.append(MEDIA_MISS_HINT)
        if not message.text:
            # Voice with no ASR / failed image: keep a soft beat so the turn
            # still runs; the hidden hint tells her to pout instead of
            # explaining the missing media.
            message.text = "……"

    def _download_attachment(self, attachment: dict[str, Any]) -> str | None:
        """Download one image and return an inline data URL (no disk cache)."""
        if not self.media_download:
            return None
        url = str(attachment.get("url") or "").strip()
        if not url:
            return None
        try:
            data = self.media_downloader(
                url, timeout=self.media_timeout, max_bytes=self.media_max_bytes
            )
        except Exception:  # noqa: BLE001 - a failed download must not kill the turn
            data = None
        if not data:
            self.counters["media_failed"] += 1
            return None
        content_type = str(attachment.get("content_type") or "").strip().lower()
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        self.counters["media_downloaded"] += 1
        return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"

    def _extra_blocks(self, message: InboundMessage) -> list[str]:
        blocks = list(message.extra_evidence)
        if self.immersion_hint:
            blocks.append(IMMERSION_HINT)
            if message.is_group:
                blocks.append(GROUP_STAGE_HINT)
        if not self.tts_enabled:
            blocks.append(TEXT_ONLY_HINT)
        return blocks

    def _buffer_message(self, message: InboundMessage) -> dict[str, Any]:
        # Mature windows flush first; a new burst always opens a fresh window.
        self.flush_due()
        key = (message.session_id, message.user_id)
        batch = self._batches.get(key)
        if batch is None:
            batch = _PendingBatch()
            self._batches[key] = batch
        batch.messages.append(message)
        batch.deadline = time.monotonic() + self.merge_window_seconds
        self.counters["buffered"] += 1
        return {
            "event_id": message.event_id,
            "session_id": message.session_id,
            "messages": [],
            "buffered": True,
            "window_messages": len(batch.messages),
            "ok": True,
        }

    def flush_due(self, now: float | None = None) -> list[dict[str, Any]]:
        """Deliver every merge window whose quiet period has elapsed."""
        now_value = time.monotonic() if now is None else float(now)
        due = [key for key, batch in self._batches.items() if batch.deadline <= now_value]
        results: list[dict[str, Any]] = []
        for key in due:
            batch = self._batches.pop(key, None)
            if batch is None or not batch.messages:
                continue
            merged = self._merge_messages(batch.messages)
            results.append(self._deliver(merged))
        return results

    def _merge_messages(self, messages: list[InboundMessage]) -> InboundMessage:
        first = messages[0]
        last = messages[-1]
        texts = [m.text.strip() for m in messages if str(m.text or "").strip()]
        images: list[str] = []
        extra: list[str] = []
        for message in messages:
            images.extend(message.images)
            extra.extend(message.extra_evidence)
        seen: set[str] = set()
        unique_images = []
        for image in images:
            if image not in seen:
                seen.add(image)
                unique_images.append(image)
        unique_images = unique_images[: self.media_max_images]
        self.counters["merged"] += max(0, len(messages) - 1)
        return InboundMessage(
            platform=first.platform,
            event_id=first.event_id,
            session_id=first.session_id,
            user_id=first.user_id,
            group_id=first.group_id,
            text="\n".join(texts),
            is_group=first.is_group,
            mentioned=any(m.mentioned for m in messages),
            reply_to=first.reply_to,
            sender_name=last.sender_name or first.sender_name,
            images=unique_images,
            extra_evidence=extra,
            raw=last.raw,
        )

    def _soft_fallback(self, message: InboundMessage) -> bool:
        """One in-character apology instead of a technical error message.

        Only private chats, only when the runtime failed before producing any
        reply, and rate-limited per session.  Never mentions causes.
        """
        if not self.fallback_enabled or not self.fallback_text or message.is_group:
            return False
        now = time.monotonic()
        last = self._fallback_at.get(message.session_id, 0.0)
        if now - last < self.fallback_cooldown_seconds:
            return False
        self._fallback_at[message.session_id] = now
        while len(self._fallback_at) > 256:
            self._fallback_at.pop(next(iter(self._fallback_at)))
        target_type = "private"
        target_id = str(message.user_id)
        text = _trim_text(self.fallback_text, self.max_message_chars)
        if not text:
            return False
        try:
            self.runtime.outbox.enqueue(
                "qq_send",
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "text": text,
                    "reply_to": message.event_id,
                },
                idempotency_key=f"qqbot:fallback:{message.event_id}",
            )
            self.runtime.process_outbox(handlers=self.handlers())
        except Exception:  # noqa: BLE001 - a failed fallback is still not fatal
            return False
        self.counters["fallbacks"] += 1
        return True

    def _deliver(self, message: InboundMessage) -> dict[str, Any]:
        try:
            return self._turn_and_deliver(message)
        except Exception as exc:  # noqa: BLE001 - one message must not kill the loop
            self.counters["turn_errors"] += 1
            self.last_client_error = f"{type(exc).__name__}: {exc}"
            logger.exception("qqbot: failed to handle event %s", message.event_id)
            fallback = self._soft_fallback(message)
            return {
                "event_id": message.event_id,
                "session_id": message.session_id,
                "messages": [],
                "ok": False,
                "error": self.last_client_error,
                "fallback": fallback,
            }

    def handle_event(self, event: dict[str, Any] | None) -> dict[str, Any] | None:
        """Parse, de-duplicate, merge bursts and run one turn.

        With ``merge_window_seconds > 0`` a turn is deferred until the sender
        has been quiet for that long; :meth:`flush_due` (called by
        ``run_forever``) delivers it.  Never raises for a malformed event.
        """
        message = self.parse_event(event)
        if message is None:
            self.counters["ignored"] += 1
            return None
        if message.event_id in self._seen_events:
            self.counters["duplicates"] += 1
            return {
                "event_id": message.event_id,
                "session_id": message.session_id,
                "messages": [],
                "duplicate": True,
                "ok": True,
            }
        self._seen_events.append(message.event_id)
        self.last_event_at = time.time()
        self.counters["events"] += 1
        try:
            self._enrich_message(message, event)
        except Exception:  # noqa: BLE001 - enrichment must never drop the message
            logger.exception("qqbot: enrichment failed for %s", message.event_id)
        if self.debug:
            logger.info(
                "qqbot: %s event for %s (%d chars, %d images)",
                "group" if message.is_group else "private",
                message.session_id,
                len(message.text),
                len(message.images),
            )
        if self.merge_window_seconds > 0:
            return self._buffer_message(message)
        return self._deliver(message)

    def _turn_and_deliver(
        self, message: InboundMessage, event: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run the runtime turn, queue replies as outbox items, deliver them."""
        target_type = "group" if message.is_group else "private"
        target_id = str(message.group_id if message.is_group else message.user_id)
        result = self.runtime.handle_user_message(
            message.session_id,
            message.text,
            is_group=message.is_group,
            sender_id=message.user_id,
            sender_name=message.sender_name or "",
            extra_evidence=self._extra_blocks(message) or None,
            images=list(message.images) or None,
            # 2026-11-15: the official adapter plus group/private decides the
            # prompt variant; the runtime still drops capabilities the adapter
            # cannot serve (official bot has no history endpoint).
            platform="qq_group" if message.is_group else "qq_private",
            # Async photo jobs outlive this turn; keep the inbound msg_id so
            # the finished image can be sent as a passive rich-media reply.
            # The digest fallback is for dedupe only and is not a valid msg_id.
            reply_to=(
                message.event_id
                if (message.raw.get("id") or message.raw.get("message_id"))
                else None
            ),
        )
        self.counters["turns"] += 1
        raw_messages = result.get("messages") or []
        if isinstance(raw_messages, str):
            raw_messages = [raw_messages]
        messages = [str(m) for m in raw_messages if str(m).strip()]

        queued = 0
        for index, text in enumerate(messages):
            text = _trim_text(text, self.max_message_chars)
            if not text:
                continue
            payload = {
                "target_type": target_type,
                "target_id": target_id,
                "text": text,
                # Only the first segment quotes the inbound message; the rest
                # are separate replies (and the client may still degrade them
                # to proactive sends once the passive quota is used up).
                "reply_to": message.event_id if index == 0 else None,
            }
            _, created = self.runtime.outbox.enqueue(
                "qq_send",
                payload,
                idempotency_key=f"qqbot:{message.event_id}:{index}",
            )
            if created:
                queued += 1

        media_queued = self._queue_result_media(result, message, target_type, target_id)
        outbox_results = self.runtime.process_outbox(handlers=self.handlers())
        sent = 0
        media_sent = 0
        errors: list[str] = []
        for row in outbox_results:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            if kind == "qq_send_voice":
                kind = "qq_send_voice"
            if kind not in {
                "qq_send",
                "proactive_message",
                "qq_send_voice",
                "qq_send_file",
                "qq_send_image",
            }:
                continue
            if row.get("status") == "done":
                if kind in {"qq_send_voice", "qq_send_file", "qq_send_image"}:
                    media_sent += 1
                else:
                    sent += 1
            elif row.get("status") in {"failed", "error"}:
                errors.append(str(row.get("error") or "send_failed"))
        return {
            "event_id": message.event_id,
            "session_id": message.session_id,
            "messages": messages,
            "queued": queued,
            "sent": sent,
            "media_queued": media_queued,
            "media_sent": media_sent,
            "errors": errors,
            "ok": not errors,
        }

    # ------------------------------------------------------- outgoing media
    def _queue_result_media(
        self,
        result: dict[str, Any],
        message: InboundMessage,
        target_type: str,
        target_id: str,
    ) -> int:
        """Queue TTS / song / photo artifacts produced by this turn."""
        if not isinstance(result, dict):
            return 0
        queued = 0
        candidates: list[tuple[str, str, str]] = []

        tts = result.get("tts")
        if isinstance(tts, dict) and tts.get("ok") and tts.get("audio_path"):
            candidates.append(("qq_send_voice", str(tts["audio_path"]), "voice"))
        elif isinstance(tts, dict) and isinstance(tts.get("segments"), list):
            for index, segment in enumerate(tts["segments"]):
                if isinstance(segment, dict) and segment.get("ok") and segment.get("audio_path"):
                    candidates.append(("qq_send_voice", str(segment["audio_path"]), "voice"))

        sing = result.get("sing")
        if isinstance(sing, dict) and sing.get("ok") and sing.get("path"):
            candidates.append(("qq_send_file", str(sing["path"]), "file"))

        photo = result.get("photo")
        if isinstance(photo, dict) and photo.get("ok") and photo.get("path"):
            candidates.append(("qq_send_image", str(photo["path"]), "image"))

        seen: set[str] = set()
        for kind, path, media_kind in candidates:
            if not path or path in seen:
                continue
            if not Path(path).is_file():
                logger.warning("qqbot: media artifact is missing: %s", path)
                continue
            seen.add(path)
            payload = {
                "target_type": target_type,
                "target_id": target_id,
                "path": path,
                "kind": media_kind,
                "reply_to": message.event_id,
                "content": "",
            }
            _, created = self.runtime.outbox.enqueue(
                kind,
                payload,
                idempotency_key=f"qqbot:{message.event_id}:{kind}:{Path(path).name}",
            )
            if created:
                queued += 1
        return queued

    # ---------------------------------------------------------------- outbox
    def handlers(self) -> dict[str, Callable[[Any], dict[str, Any]]]:
        return {
            "qq_send": self.send_outbox_item,
            "proactive_message": self.send_outbox_item,
            "qq_send_voice": self.send_media_outbox_item,
            "qq_send_file": self.send_media_outbox_item,
            "qq_send_image": self.send_media_outbox_item,
        }

    def send_media_outbox_item(self, item: Any) -> dict[str, Any]:
        """Deliver one TTS/voice/image/file outbox row through official upload.

        QQ's voice file type is strict about codecs.  If a WAV upload is
        rejected, retry once as a generic file so the user still gets audio
        instead of losing the turn.
        """
        if isinstance(item, dict):
            kind = str(item.get("kind") or "")
            payload = item.get("payload") or {}
        else:
            kind = str(getattr(item, "kind", "") or "")
            payload = getattr(item, "payload", {}) or {}
        target_type = str(payload.get("target_type") or "private")
        target_id = str(payload.get("target_id") or "")
        if not target_id:
            session_id = str(payload.get("session_id") or "")
            parts = session_id.split(":", 2)
            if len(parts) == 3 and parts[0] == "qq" and parts[1] in {"private", "group"}:
                target_type, target_id = parts[1], parts[2]
        path = str(payload.get("path") or "")
        media_kind = str(payload.get("kind") or "")
        if not media_kind:
            media_kind = {
                "qq_send_voice": "voice",
                "qq_send_file": "file",
                "qq_send_image": "image",
            }.get(kind, "file")
        reply_to = payload.get("reply_to") or None
        content = str(payload.get("content") or "")
        try:
            client = self.ensure_client()
            summary = client.send_media(
                target_type,
                target_id,
                path,
                kind=media_kind,
                reply_to=reply_to,
                content=content,
            )
            self.counters["media_sent"] += 1
            return {
                "ok": True,
                "kind": kind,
                "path": path,
                "media": media_kind,
                "send": summary,
            }
        except Exception as exc:  # noqa: BLE001 - report; never kill the loop
            self.counters["media_send_failures"] += 1
            if media_kind == "voice":
                # QQ voice files are often SILK; a WAV can be rejected.  Keep
                # the audio by falling back to a normal file message.
                try:
                    client = self.ensure_client()
                    summary = client.send_media(
                        target_type,
                        target_id,
                        path,
                        kind="file",
                        reply_to=reply_to,
                        content=content,
                    )
                    self.counters["media_sent"] += 1
                    return {
                        "ok": True,
                        "kind": kind,
                        "path": path,
                        "media": "file_fallback",
                        "send": summary,
                    }
                except Exception as fallback_exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "kind": kind,
                        "path": path,
                        "media": media_kind,
                        "error": f"{type(fallback_exc).__name__}: {fallback_exc}",
                    }
            return {
                "ok": False,
                "kind": kind,
                "path": path,
                "media": media_kind,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def send_outbox_item(self, item: Any) -> dict[str, Any]:
        """Deliver one outbox item; failures come back as ``{"ok": False}``."""
        if isinstance(item, dict):
            kind = str(item.get("kind") or "")
            item_id = item.get("id")
            payload = item.get("payload") or {}
        else:
            kind = str(getattr(item, "kind", "") or "")
            item_id = getattr(item, "id", None)
            payload = getattr(item, "payload", None) or {}
        if not isinstance(payload, dict):
            payload = {}

        if kind == "qq_send":
            target_type = str(payload.get("target_type") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip()
            text = str(payload.get("text") or "")
            reply_to = payload.get("reply_to") or None
        elif kind == "proactive_message":
            session_id = str(payload.get("session_id") or "")
            if not session_id.startswith("qq:"):
                # Not ours: let the normal runtime/web path own it.
                return {"ok": True, "id": item_id, "kind": kind, "skipped": "not_qq_session"}
            parts = session_id.split(":", 2)
            if len(parts) != 3 or parts[1] not in {"private", "group"} or not parts[2]:
                return {"ok": False, "id": item_id, "kind": kind, "error": "invalid_qq_session_id"}
            target_type, target_id = parts[1], parts[2]
            text = str(payload.get("text") or "")
            reply_to = None
        else:
            return {"ok": False, "id": item_id, "kind": kind, "error": f"unsupported_kind:{kind}"}

        if target_type not in {"private", "group"}:
            return {"ok": False, "id": item_id, "kind": kind, "error": "invalid_target_type"}
        if not target_id:
            return {"ok": False, "id": item_id, "kind": kind, "error": "invalid_target_id"}
        text = _trim_text(text, self.max_message_chars) if text.strip() else ""
        if not text:
            return {"ok": False, "id": item_id, "kind": kind, "error": "empty_message"}

        try:
            client = self.ensure_client()
            summary = client.send_text(target_type, target_id, text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001 - report, let the outbox retry
            self.counters["send_failures"] += 1
            self.last_client_error = f"{type(exc).__name__}: {exc}"
            return {
                "ok": False,
                "id": item_id,
                "kind": kind,
                "target_type": target_type,
                "target_id": target_id,
                "error": self.last_client_error,
            }
        self.counters["sent"] += int((summary or {}).get("chunks") or 0) or 1
        return {
            "ok": True,
            "id": item_id,
            "kind": kind,
            "target_type": target_type,
            "target_id": target_id,
            "send": summary,
        }

    # --------------------------------------------------------------- run loop
    def _connect(self) -> None:
        if not websocket_available():
            raise RuntimeError(INSTALL_HINT)
        # Reuse the existing client across reconnects: it keeps session_id and
        # last_seq, so the next HELLO can send op=6 RESUME instead of losing
        # gateway events that were dispatched while we were down.
        client = self.client
        if client is None:
            client = self._make_client()
            try:
                client.text_chunk_chars = self.text_chunk_chars
                client.passive_reply_limit = self.passive_reply_limit
            except Exception:  # pragma: no cover - test doubles may be read-only
                pass
        client.connect()
        self.client = client
        self.connected_at = time.time()
        self.last_client_error = None
        session = getattr(client, "session_id", None) or "pending"
        print(f"[qqbot] connected (session={session})", flush=True)

    def _drain_outbox(self) -> None:
        try:
            self.runtime.process_outbox(handlers=self.handlers())
        except Exception as exc:  # noqa: BLE001 - delivery errors are logged only
            self.last_client_error = f"{type(exc).__name__}: {exc}"
            logger.exception("qqbot: outbox drain failed")

    # --------------------------------------------------------- pet WS channel
    def _drain_pet_inbox(self) -> None:
        """Run desktop-pet chat turns on the adapter's own thread."""
        link = self.pet_link
        if link is None:
            return
        while True:
            msg = link.take_inbound()
            if msg is None:
                return
            try:
                self._handle_pet_chat(msg)
            except Exception as exc:  # noqa: BLE001 - one pet message must not kill QQ
                logger.exception("qqbot: pet chat failed")
                link.send_event(
                    {
                        "type": "bubble",
                        "id": str(msg.get("id") or ""),
                        "role": "assistant",
                        "text": "唔……刚才走神了一下，你再说一遍好不好？",
                        "error": f"{type(exc).__name__}",
                        "done": True,
                    }
                )
                link.send_event({"type": "state", "state": "idle"})

    def _handle_pet_chat(self, msg: dict[str, Any]) -> None:
        link = self.pet_link
        if link is None:
            return
        text = str(msg.get("text") or "").strip()
        msg_id = str(msg.get("id") or "")
        if not text:
            return
        session_id = "desktop:owner"
        link.send_event({"type": "state", "state": "thinking"})
        result = self.runtime.handle_user_message(
            session_id,
            text,
            allow_voice=False,
            sender_name="桌面",
            suppress_self_continue=True,
            platform="desktop",
        )
        reply = str((result or {}).get("reply") or "").strip()
        link.send_event(
            {
                "type": "bubble",
                "id": msg_id,
                "role": "assistant",
                "text": reply,
                "done": True,
            }
        )
        link.send_event({"type": "state", "state": "idle"})

    def run_forever(self, stop_event: Any | None = None) -> None:
        """Connect, receive dispatches, deliver replies; reconnect on failure."""
        if not websocket_available():
            raise RuntimeError(INSTALL_HINT)
        if not self.enabled:
            print(
                "[qqbot] warning: qqbot.enabled=false; starting because you asked explicitly",
                flush=True,
            )
        attempt = 0
        last_tick = 0.0
        while not _stopped(stop_event):
            try:
                self._connect()
                attempt = 0
                last_tick = time.time()
                while not _stopped(stop_event):
                    event = self.client.recv(timeout=1.0) if self.client is not None else None
                    if event:
                        if str(event.get("t") or "") in MESSAGE_EVENT_TYPES:
                            self.handle_event(event)
                            self._drain_outbox()
                        elif self.debug:
                            logger.info("qqbot: ignoring dispatch %s", event.get("t"))
                    # Deliver merge windows that went quiet while we were
                    # blocked in recv (single-threaded: the recv timeout keeps
                    # the extra latency under ~1s).
                    self.flush_due()
                    self._drain_pet_inbox()
                    now = time.time()
                    if now - last_tick >= 2.0:
                        # The QQ adapter owns the process loop, so it must also
                        # drive the runtime scheduler: due wakeups, proactive
                        # gates, activity/ritual turns, nightly archive, qzone
                        # polling and outbox retries all live in runtime.tick().
                        try:
                            self.runtime.tick(handlers=self.handlers())
                        except Exception:  # noqa: BLE001 - a tick failure is not fatal
                            self.counters["tick_errors"] += 1
                            logger.exception("qqbot: runtime tick failed")
                        else:
                            self.counters["ticks"] += 1
                        self._drain_outbox()
                        last_tick = now
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001 - reconnect path
                self.last_client_error = f"{type(exc).__name__}: {exc}"
                print(f"[qqbot] connection lost: {self.last_client_error}", flush=True)
                hint = getattr(exc, "delay", None)
                # Keep the client object: it holds session_id/last_seq so the
                # next connect() resumes instead of replaying nothing.
                self._disconnect()
                if _stopped(stop_event):
                    break
                if hint is not None:
                    delay = float(hint)
                else:
                    delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                    attempt += 1
                _sleep_or_stop(stop_event, delay)
        self.close()

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        client = self.client
        connected = bool(client is not None and getattr(client, "connected", False))
        return {
            "platform": "qqbot",
            "enabled": self.enabled,
            "connected": connected,
            "websocket_client": websocket_available(),
            "app_id_env": self.app_id_env,
            "app_id_set": bool(env_secret(self.app_id_env)),
            "app_secret_env": self.app_secret_env,
            "app_secret_set": bool(env_secret(self.app_secret_env)),
            "token_base_url": self.token_base_url,
            "api_base_url": self.api_base_url,
            "intents": self.intents,
            "private_whitelist": sorted(self.private_whitelist),
            "group_enabled": self.group_enabled,
            "require_mention": self.require_mention,
            "max_message_chars": self.max_message_chars,
            "text_chunk_chars": self.text_chunk_chars,
            "passive_reply_limit": self.passive_reply_limit,
            "merge_window_seconds": self.merge_window_seconds,
            "quote_cache_size": self.quote_cache_size,
            "immersion_hint": self.immersion_hint,
            "media_download": self.media_download,
            "media_max_bytes": self.media_max_bytes,
            "media_max_images": self.media_max_images,
            "media_timeout": self.media_timeout,
            "fallback_enabled": self.fallback_enabled,
            "fallback_text": self.fallback_text,
            "fallback_cooldown_seconds": self.fallback_cooldown_seconds,
            "catchup_supported": False,
            "context_source": "local_sqlite",
            "history_policy": "local transcript + memory; no platform catch-up",
            "pending_batches": len(self._batches),
            "quote_refs": len(self._refs),
            "debug": self.debug,
            "session_id": getattr(client, "session_id", None),
            "connected_at": self.connected_at,
            "last_event_at": self.last_event_at,
            "last_error": self.last_client_error,
            "seen_events": len(self._seen_events),
            "counters": dict(self.counters),
        }


# --------------------------------------------------------------------- helpers
def _event_id(data: dict[str, Any], session_id: str) -> str:
    """Platform message id, with a deterministic fallback for odd payloads."""
    message_id = data.get("id") or data.get("message_id")
    if message_id not in (None, ""):
        return str(message_id)
    # No id (should not happen): a stable digest keeps de-duplication working
    # across restarts, where ``hash()`` is randomized per process.
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{session_id}:{raw}".encode("utf-8")).hexdigest()
    return digest[:16]


def _sender_name(data: dict[str, Any]) -> str:
    author = data.get("author")
    if not isinstance(author, dict):
        return ""
    name = " ".join(str(author.get("username") or author.get("nickname") or "").split())
    return name[:64]


def _parse_ref_indices(data: dict[str, Any]) -> tuple[str, str]:
    """Return ``(ref_msg_idx, own_msg_idx)`` from the official envelope.

    Mirrors the official SDK: ``message_scene.ext`` carries
    ``ref_msg_idx=...`` / ``msg_idx=...``; for quote messages (``message_type
    == 103``) the first ``msg_elements[0].msg_idx`` is the authoritative ref.
    """
    ref = ""
    own = ""
    scene = data.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, dict) else None
    if isinstance(ext, list):
        for item in ext:
            if not isinstance(item, str):
                continue
            if item.startswith("ref_msg_idx="):
                ref = item[len("ref_msg_idx=") :].strip()
            elif item.startswith("msg_idx="):
                own = item[len("msg_idx=") :].strip()
            elif item.startswith("refMsgIdx:"):
                ref = item[len("refMsgIdx:") :].strip()
            elif item.startswith("msgIdx:"):
                own = item[len("msgIdx:") :].strip()
    try:
        msg_type = int(data.get("message_type") or 0)
    except (TypeError, ValueError):
        msg_type = 0
    elements = data.get("msg_elements")
    if msg_type == 103 and isinstance(elements, list) and elements:
        first = elements[0]
        if isinstance(first, dict) and first.get("msg_idx"):
            ref = str(first["msg_idx"]).strip() or ref
    return ref, own


def _attachment_kind(attachment: dict[str, Any]) -> str:
    content_type = str(attachment.get("content_type") or "").lower()
    filename = str(attachment.get("filename") or "").lower()
    probe = f"{content_type} {filename}"
    if "image" in probe:
        return "image"
    if "voice" in probe or "audio" in probe or "silk" in probe or "amr" in probe:
        return "voice"
    if "video" in probe:
        return "video"
    return "file"


def _attachment_name(attachment: dict[str, Any], kind: str) -> str:
    name = " ".join(str(attachment.get("filename") or "").split())
    if name:
        return name[:64]
    return {"image": "一张图片", "voice": "一条语音", "video": "一个视频", "file": "一个文件"}[kind]


def _msg_elements_quote_text(data: dict[str, Any]) -> str:
    """Quoted-message fallback text from ``msg_elements[0]`` (official push)."""
    elements = data.get("msg_elements")
    if not isinstance(elements, list) or not elements:
        return ""
    first = elements[0]
    if not isinstance(first, dict):
        return ""
    parts: list[str] = []
    content = " ".join(str(first.get("content") or "").split())
    if content:
        parts.append(content)
    attachments = first.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = _attachment_kind(attachment)
            asr = " ".join(str(attachment.get("asr_refer_text") or "").split())
            if asr:
                parts.append(f"（语音）{asr}")
            else:
                parts.append(f"（{_attachment_name(attachment, kind)}）")
    return "\n".join(parts)[:300]


def _download_url(url: str, *, timeout: float, max_bytes: int) -> bytes | None:
    """Download one attachment with a hard byte cap; never raises."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "fatfish/qqbot"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - official signed URL
            data = response.read(max(1, int(max_bytes)) + 1)
    except Exception:  # noqa: BLE001 - a failed image must not fail the turn
        return None
    if not data or len(data) > int(max_bytes):
        return None
    return data


def _mention_names(data: dict[str, Any]) -> list[str]:
    """Other people @-mentioned by this group message (never the bot itself)."""
    mentions = data.get("mentions")
    if not isinstance(mentions, list):
        return []
    names: list[str] = []
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        if mention.get("is_you"):
            continue
        name = " ".join(
            str(mention.get("nickname") or mention.get("username") or "").split()
        )
        if name and name not in names:
            names.append(name[:32])
    return names[:6]


@dataclass
class _PendingBatch:
    """One sliding merge window per ``(session_id, user_id)``."""

    messages: list[InboundMessage] = field(default_factory=list)
    deadline: float = 0.0
