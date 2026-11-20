"""Stdlib-only QQ Zone client: feeds + shuoshuo list, plus the publish path.

Design notes
------------
* No third-party HTTP stack: ``urllib`` only, same as ``fatfish/bilibili``.
* The read surface is passive. ``publish_text`` is the only write and is
  called exclusively by the outbox handler *after* the owner approved the
  draft (``qzone/posts.py``); nothing here decides to post on its own.
* Cookies are secrets: never printed, never logged, persisted to a single
  JSON file under the data directory with restrictive permissions off Windows.
* ``g_tk`` is the standard QQ Zone 5381 hash over ``p_skey`` (user.qzone.qq.com
  domain), falling back to ``skey`` when ``p_skey`` is absent.
* Endpoints are candidates: responses are parsed tolerantly and every shape is
  covered by offline fixtures. Real reachability is checked by ``doctor`` /
  the ``fatfish qzone doctor`` command, never by the test suite.
* Feed payloads may contain third-party identifiers (commenter names, reply
  text, location): ``raw_json`` is treated as sensitive-at-rest and must not be
  rendered to the user or sent to the LLM (see schema.sql, qzone_feed_items).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import feeds_html

MOBILE_LIST_URL = "https://mobile.qzone.qq.com/list"
# Candidate friend-feed endpoint (ic2 backend behind the user.qzone proxy).
# Answers as ``_Callback(...)`` holding a JS object literal with per-feed HTML
# blocks; ``feeds_html`` owns the lenient parse. Live-probed 2026-10-25 (auth
# pass); the mobile list below stays as the fallback for endpoint drift.
FEEDS3_HTML_URL = (
    "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/"
    "cgi-bin/feeds/feeds3_html_more"
)
# Candidate self-shuoshuo endpoint (taotao backend behind the user.qzone proxy).
EMOTION_LIST_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/"
    "cgi-bin/emotion_cgi_msglist_v6"
)
# Candidate publish endpoints (T-0007). Real reachability is verified by
# ``fatfish qzone doctor`` / a live cookie, never by the test suite.
QZONETOKEN_URL = (
    "https://user.qzone.qq.com/proxy/domain/publish.qzone.qq.com/"
    "cgi-bin/app_publish_qzonetoken.fcgi"
)
EMOTION_PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/"
    "cgi-bin/emotion_cgi_publish_v6"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://user.qzone.qq.com/"
MIN_REQUEST_INTERVAL = 1.0

MAX_CONTENT_CHARS = 4000
MAX_FEED_ITEMS = 50
# Embedded comments: hard caps keep the UGC surface short and bounded. They
# are only ever read out of the SAME response (zero extra requests) and are
# truncated per line like the Bilibili side.
MAX_COMMENT_CHARS = 100
DEFAULT_COMMENTS_PER_ITEM = 5

# ``uin``/``skey`` authenticate; ``p_skey`` signs user.qzone.qq.com requests
# (g_tk). A cookie without p_skey still passes the mobile list endpoint but is
# flagged by doctor() as degraded.
REQUIRED_COOKIE_FIELDS = ("uin", "skey")
G_TK_COOKIE_FIELDS = ("p_skey", "skey")

# Cookie values are short header-safe tokens; reject anything that could carry
# header injection or drift into log output.
_UNSAFE_COOKIE_CHARS = frozenset("\r\n\x00;")

_JSONP_CALLBACK_RE = re.compile(r"^\s*[_$A-Za-z][_$A-Za-z0-9]*\s*\((.*)\)\s*;?\s*$", re.S)


class QzoneError(RuntimeError):
    """QQ Zone API/network failure with the remote code when available."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        login_required: bool = False,
        data: Any = None,
    ):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.login_required = login_required
        self.data = data


class QzoneClient:
    def __init__(self, config: Any, cookies: dict[str, str] | None = None):
        self.config = config
        self.data_dir = _data_dir(config)
        raw_path = str(_cfg(config, "qzone.cookie_path", "qzone_cookies.json"))
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.data_dir / path
        self.cookie_path = path.resolve()
        self.user_agent = str(_cfg(config, "qzone.user_agent", DEFAULT_USER_AGENT))
        self.request_interval = max(
            MIN_REQUEST_INTERVAL,
            float(_cfg(config, "qzone.request_interval_seconds", 1.0)),
        )
        self.timeout = max(1.0, float(_cfg(config, "qzone.timeout_seconds", 10.0)))
        self.max_feed_items = max(1, int(_cfg(config, "qzone.max_feed_items_per_poll", 20)))
        self.owner_uin = str(_cfg(config, "qzone.owner_uin", "") or "").strip()
        # Embedded-comment extraction limit; 0 disables extraction. Comments
        # ride along in the feed/shuoshuo response: nothing here ever sends a
        # per-item comment request.
        self.comments_per_item = max(
            0, int(_cfg(config, "qzone.comments_per_item", DEFAULT_COMMENTS_PER_ITEM) or 0)
        )

        self.cookies: dict[str, str] = _clean_cookie_mapping(cookies or {})
        self._last_request_at = 0.0
        self._consecutive_failures = 0
        self._next_allowed_at = 0.0
        self._opener = urllib.request.build_opener()

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_config(cls, config: Any, cookies: dict[str, str] | None = None) -> "QzoneClient":
        """Env cookies win; otherwise load the JSON cookie file.

        ``QZONE_UIN`` / ``QZONE_SKEY`` / ``QZONE_P_SKEY`` each override the
        single matching field, so a rotated ``skey`` can be injected without
        rewriting the file. ``QZONE_COOKIE`` (``k=v; k=v``) fills any fields
        not already set.
        """
        merged: dict[str, str] = {}
        loader = cls(config)
        merged.update(loader.load_cookies())
        env_uin = (os.environ.get("QZONE_UIN") or "").strip()
        env_skey = (os.environ.get("QZONE_SKEY") or "").strip()
        env_p_skey = (os.environ.get("QZONE_P_SKEY") or "").strip()
        if env_uin:
            merged["uin"] = env_uin
        if env_skey:
            merged["skey"] = env_skey
        if env_p_skey:
            merged["p_skey"] = env_p_skey
        raw_cookie = (os.environ.get("QZONE_COOKIE") or "").strip()
        if raw_cookie:
            for key, value in parse_cookie_string(raw_cookie).items():
                merged.setdefault(key, value)
        if cookies:
            merged.update(_clean_cookie_mapping(cookies))
        return cls(config, cookies=_clean_cookie_mapping(merged))

    def load_cookies(self, path: str | Path | None = None) -> dict[str, str]:
        """Load the cookie file; a missing or unreadable file yields ``{}``."""
        target = Path(path) if path else self.cookie_path
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded = _clean_cookie_mapping(raw)
        self.cookies.update(loaded)
        return loaded

    def save_cookies(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.cookie_path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = _clean_cookie_mapping(self.cookies)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            try:
                os.chmod(target, 0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass
        return target

    def login_with_cookie(self, cookie_string: str) -> dict[str, str]:
        """Persist a pasted ``k=v; k=v`` cookie string (no network involved)."""
        parsed = _clean_cookie_mapping(parse_cookie_string(cookie_string))
        if not parsed:
            raise QzoneError("empty or invalid cookie string")
        missing = [f for f in REQUIRED_COOKIE_FIELDS if not parsed.get(f) and not self.cookies.get(f)]
        if missing:
            raise QzoneError(
                "cookie string is missing required field(s): " + ", ".join(missing)
            )
        self.cookies.update(parsed)
        self.save_cookies()
        return dict(self.cookies)

    @property
    def uin(self) -> str:
        return str(self.cookies.get("uin") or "").strip()

    def cookie_status(self) -> dict[str, Any]:
        """Cookie health without ever exposing values."""
        present = {name: bool(self.cookies.get(name)) for name in G_TK_COOKIE_FIELDS}
        missing = [name for name in REQUIRED_COOKIE_FIELDS if not self.cookies.get(name)]
        return {
            "cookie_path": str(self.cookie_path),
            "fields_present": present,
            "missing": missing,
            "g_tk_ready": bool(self.cookies.get("skey") or self.cookies.get("p_skey")),
            # g_tk signs user.qzone.qq.com (shuoshuo) with p_skey; the mobile
            # list endpoint accepts skey. Without p_skey the read path is
            # degraded, not dead.
            "full_read": all(self.cookies.get(name) for name in ("uin", "skey", "p_skey")),
        }

    def g_tk(self) -> int:
        """ACSRF token: 5381 hash over p_skey, falling back to skey."""
        skey = self.cookies.get("p_skey") or self.cookies.get("skey") or ""
        if not skey:
            raise QzoneError("g_tk requires skey or p_skey", login_required=True)
        return g_tk_from_skey(skey)

    # ----------------------------------------------------------------- request
    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _open(self, request: urllib.request.Request) -> Any:
        """Send one throttled request with exponential backoff on failures.

        Shared by the GET (``request``) and POST (``post_form``) paths so the
        publish path inherits the same risk-control protection.
        """
        now = time.monotonic()
        if now < self._next_allowed_at:
            raise QzoneError(
                f"qzone backing off for another {self._next_allowed_at - now:.1f}s"
            )
        self._throttle()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            self._note_failure()
            raise QzoneError(f"qzone http {exc.code}: {exc.reason}", code=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._note_failure()
            raise QzoneError(f"qzone network error: {exc}") from exc
        self._note_success()
        return parse_response_body(body)

    def request(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON/JSONP endpoint; returns the parsed payload."""
        full = url if "?" in url or not params else f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            full, headers=self._base_headers(), method="GET"
        )
        return self._open(request)

    def post_form(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """POST urlencoded form data to a JSON/JSONP endpoint.

        Same cookie header, throttling and backoff as ``request``; adds the
        Origin header and mood-page Referer the publish CGI expects.
        """
        full = url if "?" in url or not params else f"{url}?{urllib.parse.urlencode(params)}"
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        headers = self._base_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = "https://user.qzone.qq.com"
        if self.uin:
            headers["Referer"] = f"https://user.qzone.qq.com/{self.uin}/mood/"
        request = urllib.request.Request(full, data=body, headers=headers, method="POST")
        return self._open(request)

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.request_interval - (now - self._last_request_at)
        if self._last_request_at and wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        backoff = min(60.0, 2.0 ** min(self._consecutive_failures, 6))
        self._next_allowed_at = time.monotonic() + backoff

    def _note_success(self) -> None:
        self._consecutive_failures = 0
        self._next_allowed_at = 0.0

    # -------------------------------------------------------------- read-only
    def fetch_feeds(self, count: int | None = None) -> list[dict[str, Any]]:
        """Friend/self feed list via feeds3_html_more, mobile list fallback.

        The ic2 endpoint is the one that survived the 2026-10-25 probe (the
        mobile list was rejected -4003), so it goes first; endpoint drift on
        either side degrades to the other instead of failing the poll.
        """
        if not self.uin:
            raise QzoneError("qzone cookie has no uin", login_required=True)
        count = min(int(count or self.max_feed_items), MAX_FEED_ITEMS)
        try:
            payload = self.request(
                FEEDS3_HTML_URL,
                {
                    "uin": self.uin,
                    "hostuin": self.uin,
                    "scope": "0",
                    "begintime": "0",
                    "count": str(count),
                    "g_tk": self.g_tk(),
                    "format": "json",
                },
            )
            return feeds_html.parse_feeds3_payload(payload)
        except (QzoneError, feeds_html.Feeds3ParseError):
            pass  # either side failed: fall back to the legacy mobile list
        # Legacy mobile JSON endpoint (candidate).
        payload = self.request(
            MOBILE_LIST_URL,
            {
                "g_tk": self.g_tk(),
                "enc_host": "user.qzone.qq.com",
                "format": "json",
                "res_type": "2",
                "start": "0",
                "count": str(count),
                "uin": self.uin,
                "hostuin": self.uin,
            },
        )
        return parse_feeds_payload(payload, comments_limit=self.comments_per_item)

    def fetch_emotions(
        self, count: int | None = None, *, uin: str | None = None
    ) -> list[dict[str, Any]]:
        """Shuoshuo list via emotion_cgi_msglist_v6.

        默认拉自己的；传 ``uin`` 可以拉**好友**的说说列表（2026-10-29 实测：
        好友 uin 同样返回 ``code:0``，字段与聚合 feed 一致）。这是聚合
        ``feeds3_html_more`` 被 ic2 判未登录（``-3000/-4001``）时的回退路径。
        """
        target = str(uin or self.uin or "").strip()
        if not self.uin or not target:
            raise QzoneError("qzone cookie has no uin", login_required=True)
        count = min(int(count or self.max_feed_items), MAX_FEED_ITEMS)
        payload = self.request(
            EMOTION_LIST_URL,
            {
                "uin": target,
                "ftype": "0",
                "sort": "0",
                "pos": "0",
                "num": str(count),
                "replynum": "0",
                "g_tk": self.g_tk(),
                "code_version": "1",
                "format": "json",
                "need_private_comment": "1",
            },
        )
        items = parse_emotions_payload(payload, comments_limit=self.comments_per_item)
        for item in items:
            # msglist 有时不带作者 uin；按请求的 uin 补齐，避免下游把好友的
            # 说说当成自己的（origin_scope 是隐私边界）。
            if not item.get("author_uin"):
                item["author_uin"] = target
            if not item.get("url"):
                item["url"] = f"https://user.qzone.qq.com/{item['author_uin']}"
        return items

    # ------------------------------------------------------------- publishing
    def fetch_qzonetoken(self) -> str:
        """Fetch the publish token from app_publish_qzonetoken.fcgi (candidate).

        The endpoint answers with a JSONP envelope (``_Callback({...})``);
        ``parse_response_body`` already unwraps it. A missing token is a hard
        error: the publish CGI treats an empty token as a signature failure,
        so proceeding without one would only produce a confusing remote error.
        """
        payload = self.request(
            QZONETOKEN_URL,
            {
                "g_tk": self.g_tk(),
                "uin": self.uin,
                "format": "json",
            },
        )
        token = _find_key(payload, "qzonetoken")
        if not token:
            raise QzoneError("qzone publish token missing from response")
        return token

    def publish_text(self, content: str) -> dict[str, Any]:
        """Publish one text shuoshuo via emotion_cgi_publish_v6 (candidate).

        Called only by the outbox handler for an owner-approved draft. Returns
        ``{"ok": True, "tid": ..., "url": ...}`` on a ``code=0`` answer; any
        error envelope, HTML wall or network fault raises :class:`QzoneError`
        (the handler turns that into a retry).
        """
        text = str(content or "").strip()
        if not text:
            raise QzoneError("publish_text requires non-empty content")
        if not self.uin:
            raise QzoneError("qzone cookie has no uin", login_required=True)
        token = self.fetch_qzonetoken()
        payload = self.post_form(
            EMOTION_PUBLISH_URL,
            {
                "g_tk": self.g_tk(),
                "uin": self.uin,
            },
            {
                "syn": "1",
                "who": "1",
                "con": text,
                "feedversion": "1",
                "flag": "1",
                "multimedia": "0",
                "pickey": "",
                "topicid": "",
                "qid": "",
                "rd": "1",
                "refresh_type": "1",
                "model": "1",
                "from": "1",
                "p_uin": self.uin,
                "appid": "311",
                "certification": "",
                "is_weibo": "0",
                "attach": "",
                "hex": "",
                "ugc_update": "1",
                "richval": "",
                "subid": "",
                "nowtime": str(int(time.time())),
                "qzonetoken": token,
            },
        )
        tid = str(_find_key(payload, "tid") or "")
        result: dict[str, Any] = {"ok": True, "tid": tid}
        if tid:
            result["url"] = f"https://user.qzone.qq.com/{self.uin}/mood/{tid}"
        return result

    def doctor(self, *, online: bool = False) -> dict[str, Any]:
        """Cookie completeness, plus an optional live probe.

        ``ok`` 回答的是**「这份 cookie 还能不能用来登录」**，不是「所有端点都
        健康」：

        * 登录判定走 ``fetch_emotions``（自己的说说，单端点、无降级，``-3000``
          的语义干净）；它成功就说明会话有效，哪怕 0 条说说；
        * 好友动态 ``fetch_feeds`` 单独报在 ``feeds`` 里，**不影响** ``ok``。
          2026-10-29 实测：同一份有效 cookie，``emotion_cgi`` 一直 200，而
          ``feeds3_html_more`` 会间歇性回 ``-3000``、mobile 兜底回 ``-4003``。
          把端点抖动算成"要重新登录"会误导人（也会让免扫码续期自我否定）。

        ``online=True`` 会发真实请求，只给 CLI ``fatfish qzone doctor`` 和
        续期探测用；测试永远不设。
        """
        report: dict[str, Any] = {
            "ok": False,
            "cookie": self.cookie_status(),
            "owner_uin_configured": bool(self.owner_uin),
            "online_checked": False,
        }
        report["ok"] = bool(report["cookie"]["full_read"])
        if not online:
            return report
        report["online_checked"] = True
        try:
            items = self.fetch_emotions(count=1)
        except QzoneError as exc:
            report["ok"] = False
            report["probe"] = {
                "ok": False,
                "error": str(exc),
                "code": exc.code,
                "login_required": bool(exc.login_required),
            }
            return report
        report["probe"] = {"ok": True, "items": len(items)}
        try:
            feeds = self.fetch_feeds(count=1)
            report["feeds"] = {"ok": True, "items": len(feeds)}
        except QzoneError as exc:
            report["feeds"] = {
                "ok": False,
                "error": str(exc),
                "code": exc.code,
                "login_required": bool(exc.login_required),
            }
        return report


# ------------------------------------------------------------------ parsing
def g_tk_from_skey(skey: str) -> int:
    """Standard QQ Zone 5381 hash (ACSRF token).

    Matches the widely circulated JS ``getACSRFToken``: start at 5381, for each
    character add ``hash*32 + code``, mask the final value to 31 bits. Inputs
    are short header tokens, so the exact-int Python result matches the JS
    semantics used in practice.
    """
    value = 5381
    for char in str(skey or ""):
        value += (value << 5) + ord(char)
    return value & 0x7FFFFFFF


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    """Parse ``k=v; k=v`` (also accepts newlines) into a cookie dict."""
    cookies: dict[str, str] = {}
    raw = str(cookie_string or "").replace("\n", ";")
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            cookies[key] = value
    return cookies


def _clean_cookie_mapping(cookies: dict | None) -> dict[str, str]:
    """Keep only non-empty, header-safe string pairs."""
    if not cookies:
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in cookies.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = raw_key.strip()
        value = raw_value.strip()
        if not key or not value:
            continue
        if not _cookie_text_is_safe(key) or not _cookie_text_is_safe(value):
            continue
        cleaned[key] = value
    return cleaned


def _cookie_text_is_safe(value: str) -> bool:
    for char in value:
        if char in _UNSAFE_COOKIE_CHARS or ord(char) < 0x20 or ord(char) == 0x7F:
            return False
    return True


def parse_response_body(body: bytes | str) -> Any:
    """Parse a JSON or JSONP body; raise on HTML login walls.

    Risk control answers with either a JSON error envelope or an HTML page;
    both must surface as :class:`QzoneError` with ``login_required`` instead of
    silently parsing to an empty list.
    """
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    stripped = text.strip()
    if not stripped:
        raise QzoneError("qzone returned an empty body")
    if stripped.startswith("<"):
        raise QzoneError(
            "qzone returned HTML (login wall or risk control)", login_required=True
        )
    payload: Any = None
    try:
        payload = json.loads(stripped)
    except ValueError:
        match = _JSONP_CALLBACK_RE.match(stripped)
        inner = match.group(1) if match else stripped
        try:
            payload = json.loads(inner)
        except ValueError:
            # feeds3_html_more answers with a JSONP-wrapped JS object literal
            # (bare keys, single-quoted strings); ``feeds_html`` lenient parse
            # is the last resort before declaring the body invalid.
            try:
                payload = feeds_html.loose_json_loads(inner)
            except ValueError:
                payload = None
    if payload is None:
        raise QzoneError("qzone returned invalid JSON")
    _raise_on_error_envelope(payload)
    return payload


def _raise_on_error_envelope(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    code = payload.get("code")
    if isinstance(code, int) and code != 0:
        subcode = payload.get("subcode")
        raise QzoneError(
            f"qzone api error {code}: {payload.get('message') or 'unknown error'}",
            code=code,
            subcode=int(subcode) if isinstance(subcode, int) else None,
            login_required=code in {-3000, -4001, 10001, 10002, 4},
            data=payload,
        )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any, limit: int = MAX_CONTENT_CHARS) -> str:
    text = str(value or "")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text.strip()


def _extract_media(item: dict[str, Any]) -> list[str]:
    """Collect picture URLs from any of the shapes the endpoints use."""
    media: list[str] = []
    candidates = item.get("pics") or item.get("pictures") or item.get("piclist")
    if isinstance(candidates, list):
        for pic in candidates:
            if isinstance(pic, dict):
                url = pic.get("url1") or pic.get("url2") or pic.get("url") or pic.get("pic")
            else:
                url = pic
            text = str(url or "").strip()
            if text:
                media.append(text)
    pic = item.get("pic")
    if isinstance(pic, dict):
        for value in pic.get("data") or []:
            if isinstance(value, dict):
                url = str(value.get("url1") or value.get("url2") or "").strip()
                if url:
                    media.append(url)
    return media[:9]


def parse_feeds_payload(
    payload: Any, *, comments_limit: int = DEFAULT_COMMENTS_PER_ITEM
) -> list[dict[str, Any]]:
    """Normalize a friend-feed list payload; tolerate shape drift.

    Accepted containers: ``data.list`` / ``data.feeds`` / ``data.vFeeds`` /
    a bare list. Missing fields degrade to empty strings, never raise.
    Embedded comments (``commentlist``/``comments``) are extracted from the
    same payload — never fetched separately — and capped at
    ``comments_limit`` (0 disables extraction).
    """
    items: list[Any] = _find_list(payload, ("list", "feeds", "vFeeds"))
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        feed = raw.get("feedinfo") if isinstance(raw.get("feedinfo"), dict) else raw
        external_id = _clean_text(
            feed.get("feedid")
            or feed.get("feedId")
            or feed.get("key")
            or feed.get("itemid")
            or feed.get("tid"),
            128,
        )
        if not external_id:
            continue
        author_uin = _clean_text(
            feed.get("uin") or feed.get("hostuin") or feed.get("opuin"), 32
        )
        author_name = _clean_text(
            feed.get("nickname") or feed.get("nick") or feed.get("name"), 64
        )
        content = _clean_text(
            feed.get("content")
            or feed.get("summary")
            or feed.get("title")
            or feed.get("html")
        )
        published_at = _as_int(
            feed.get("abstime") or feed.get("pubtime") or feed.get("time")
        )
        normalized.append(
            {
                "external_id": external_id,
                "author_uin": author_uin,
                "author_name": author_name,
                "content": content,
                "media": _extract_media(feed),
                "published_at": float(published_at) if published_at else None,
                "url": f"https://user.qzone.qq.com/{author_uin}" if author_uin else "",
                "comments": _extract_comments(feed, comments_limit)
                or _extract_comments(raw, comments_limit),
                "raw": raw,
            }
        )
        if len(normalized) >= MAX_FEED_ITEMS:
            break
    return normalized


def parse_emotions_payload(
    payload: Any, *, comments_limit: int = DEFAULT_COMMENTS_PER_ITEM
) -> list[dict[str, Any]]:
    """Normalize an emotion_cgi_msglist_v6 payload (own shuoshuo).

    ``commentlist`` embedded in each msglist entry is extracted the same way
    (zero extra requests, capped at ``comments_limit``).
    """
    items: list[Any] = _find_list(payload, ("msglist", "list", "feeds"))
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        external_id = _clean_text(raw.get("tid") or raw.get("feedid") or raw.get("key"), 128)
        if not external_id:
            continue
        created = _as_int(raw.get("created_time") or raw.get("abstime") or raw.get("time"))
        normalized.append(
            {
                "external_id": external_id,
                "author_uin": _clean_text(raw.get("uin"), 32),
                "author_name": _clean_text(raw.get("name") or raw.get("nickname"), 64),
                "content": _clean_text(raw.get("content") or raw.get("content2")),
                "media": _extract_media(raw),
                "published_at": float(created) if created else None,
                "url": "",
                "comments": _extract_comments(raw, comments_limit),
                "raw": raw,
            }
        )
        if len(normalized) >= MAX_FEED_ITEMS:
            break
    return normalized


_COMMENT_LIST_KEYS = ("commentlist", "comment_list", "comments")
_COMMENT_AUTHOR_KEYS = ("name", "nickname", "nick", "uin")
_COMMENT_TEXT_KEYS = ("content", "content2", "message")


def _extract_comments(item: dict[str, Any], limit: int) -> list[dict[str, str]]:
    """Pull embedded comments out of one feed/shuoshuo entry, tolerantly.

    Real payloads embed the comment list inline; shapes drift across
    revisions, so the container key and the per-comment fields are probed.
    Author falls back to the raw uin when no nickname is present; text is
    truncated to :data:`MAX_COMMENT_CHARS`. ``limit <= 0`` returns ``[]``.
    The output inherits the item's privacy scope downstream (comments are
    never routed into the prompt bypass; only the memory/recall channel).
    """
    if limit <= 0:
        return []
    for key in _COMMENT_LIST_KEYS:
        candidates = item.get(key)
        if not isinstance(candidates, list):
            continue
        comments: list[dict[str, str]] = []
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            author = ""
            for author_key in _COMMENT_AUTHOR_KEYS:
                author = str(raw.get(author_key) or "").strip()
                if author:
                    break
            text = ""
            for text_key in _COMMENT_TEXT_KEYS:
                text = str(raw.get(text_key) or "").strip()
                if text:
                    break
            if not text:
                continue
            comments.append(
                {
                    "author": _truncate_chars(author, 64),
                    "text": _truncate_chars(text, MAX_COMMENT_CHARS),
                }
            )
            if len(comments) >= limit:
                break
        return comments
    return []


def _truncate_chars(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking the cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _find_list(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    """Dig the first plausible item list out of an envelope, tolerating drift."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _find_key(payload: Any, key: str, *, _depth: int = 0) -> Any:
    """Depth-first search for ``key`` in a JSON envelope (publish answers).

    The publish endpoints nest their fields differently across revisions
    (top-level ``tid`` vs ``data.tid``, ``data.qzonetoken`` vs a flat map), so
    the parser digs instead of guessing one shape. Bounded depth keeps hostile
    payloads from recursing far.
    """
    if _depth > 6:
        return None
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value):
            return value
        for child in payload.values():
            found = _find_key(child, key, _depth=_depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _find_key(child, key, _depth=_depth + 1)
            if found is not None:
                return found
    return None


# ------------------------------------------------------------------ helpers
def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(dotted, default)
    if isinstance(config, dict):
        current: Any = config
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _data_dir(config: Any) -> Path:
    value = getattr(config, "data_dir", None)
    if value is not None:
        return Path(value)
    raw = _cfg(config, "app.data_dir", "data")
    return Path(str(raw))
