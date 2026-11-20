"""QQ adapter for OneBot v11 (NapCat forward WebSocket).

The adapter parses OneBot message events, applies private/group security
gates, hands the text to :class:`fatfish.runtime.Runtime`, and sends each reply
segment through the outbox as ``qq_send`` items. Text is sent as text; an
optional TTS voice and sticker (as an image) are sent as OneBot media segments
via the same outbox.

Reply policy
------------
* Private chats: always handled (after the optional private whitelist).
* Groups: ``group_reply_mode`` controls the trigger:
  - ``mention`` (default): the bot must be @-mentioned
  - ``name``: the text must contain one of ``bot_names``
  - ``all``: every group message is eligible
  - ``off``: no group replies at all
* Groups are additionally limited by ``group_cooldown_seconds`` and
  ``group_max_per_hour``.
* Voice replies in groups are off by default (``qq.send_voice_in_group``);
  sticker and photo images are private-only.

Online catch-up (T-0028)
------------------------
After every successful (re)connect the adapter arms a catch-up scan; once the
connection has been stable for ``qq.catchup_delay_seconds`` it scans the
owner's and the friend's sessions (in that order) for unanswered messages and
runs one catch-up turn per contact through the normal reply path. See
:mod:`fatfish.platforms.catchup`.

Merge window + image vision (2026-10)
-------------------------------------
``qq.merge_window_seconds`` (>0) turns on a per-``(session_id, user_id)``
sliding debounce: every new message cancels the previous timer and restarts
the delay; when a window finally goes quiet, all buffered messages are merged
into ONE :class:`InboundMessage` and run through the normal reply path as a
single turn (one context assembly, one LLM call, one reply). This is what
makes "发图后补文字" and message bursts read as one exchange. With ``0``
(default) the adapter behaves exactly as before, one turn per message.

``qq.vision_enabled`` (default false) turns on image direct-read: image
segments are downloaded (10s timeout, 5MB cap each) and passed to the LLM as
OpenAI-compatible inline data URLs on the current user message. The bytes are
never persisted — the stored turn and every text view carry only the
「[图片]」placeholder, and a failed/oversized download degrades honestly to
placeholder-only with an error log.

Threading: the WebSocket recv thread only appends to the buffer and re-arms
the debounce timer; a fired timer merely marks the window due, and the flush
itself runs serially on the adapter's owning thread (the ``run_forever`` loop
drains due windows every iteration, ``handle_event`` drains opportunistically).
The timer thread never touches the runtime — the SQLite connection is
thread-confined. A process restart drops buffered messages (at most one
window's worth, ≤ merge_window_seconds) by design.

Every skipped group message is recorded in ``decision_log`` as
``qq_group_skip`` so the behaviour is auditable.

Read-only history reads (T-0071)
--------------------------------
``fetch_group_history`` / ``fetch_forward_messages`` /
``fetch_friend_history`` / ``fetch_group_members`` are fault-tolerant,
read-only wrappers over the NapCat extension actions (group history page,
merged-forward content, friend history page, member list). They return
plain row dicts, never raise, and never send a message — a failed call or
an unreachable client reads as an empty result. Row formatting for prompts
lives in :mod:`fatfish.platforms.history`; ``extract_forward_ref`` pulls
the ``resid`` out of a merged-forward json segment so a later
``get_forward_msg`` call can fetch the full content.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..clock import human_date
from ..config import env_secret
from .base import InboundMessage, make_session_id
from .catchup import CATCHUP_RETRY_KIND, CatchUpService, CatchupTarget, HistoryMessage
from .onebot import OneBotClient, websocket_available

logger = logging.getLogger(__name__)

CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^\]]*)?)\]")
AT_SEGMENT_TYPE = "at"
REPLY_SEGMENT_TYPE = "reply"
IMAGE_SEGMENT_TYPE = "image"

#: Outbox kind for 大肥鱼间聊天 (T-0041/L3).  The recipient is *only* ever
#: ``payload["peer_uin"]``; the service that enqueued it already checked binding.
AI_CHAT_MESSAGE_KIND = "ai_chat_message"

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
VOICE_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".silk", ".amr"})
DEFAULT_MEDIA_MAX_BYTES = 10 * 1024 * 1024

# Vision download limits (code-level constants; no TOML edits required).
VISION_DOWNLOAD_TIMEOUT_SECONDS = 10.0
VISION_MAX_BYTES = 5 * 1024 * 1024
# One catch-up turn is a recap, not a gallery: cap the inline images so a
# reconnect cannot ship a whole album to the vision model.
CATCHUP_MAX_IMAGES = 4

DEFAULT_BOT_NAMES = ["大肥鱼", "鲸鱼娘"]


class _MergeBuffer:
    """One sliding window per ``(session_id, user_id)``.

    Only the adapter's owning thread (WS recv loop / test driver) mutates
    ``messages`` and ``timer``; the debounce timer thread only marks the key
    due, never touches this object.
    """

    __slots__ = ("messages", "timer")

    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []
        self.timer: Any = None


def _default_timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    """Debounce timer factory. Daemon so a pending window never delays exit."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    return timer


def download_image(
    url: str,
    *,
    timeout: float = VISION_DOWNLOAD_TIMEOUT_SECONDS,
    max_bytes: int = VISION_MAX_BYTES,
) -> tuple[bytes, str]:
    """Fetch one remote image for vision input; returns ``(raw, content_type)``.

    Raises on network failure, empty body, non-image content type, or payload
    over ``max_bytes`` — the caller degrades to the 「[图片]」 placeholder and
    logs. Injectable via ``QQAdapter(..., image_downloader=...)`` so tests
    never touch the network.
    """
    request = urllib.request.Request(
        str(url), headers={"user-agent": f"fatfish/{__version__}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        content_type = (
            str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        )
        data = resp.read(max_bytes + 1)
    if not data:
        raise ValueError("empty image body")
    if len(data) > max_bytes:
        raise ValueError(f"image exceeds {max_bytes} bytes (got {len(data)})")
    if not content_type.startswith("image/"):
        raise ValueError(f"unexpected content type: {content_type or 'missing'}")
    return data, content_type


def image_data_url(data: bytes, content_type: str) -> str:
    """Inline data URL in the OpenAI vision format (jpeg when unknown)."""
    kind = content_type if str(content_type).startswith("image/") else "image/jpeg"
    return f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"


def _compose_turn_text(image_count: int, text: str) -> str:
    """Turn text for storage and the prompt text view.

    Image bytes never land here: an image-bearing turn reads
    ``[图片]``/``[图片]×n`` plus the words, which is also exactly what the LLM
    sees as text next to the inline image parts (or alone when vision is off
    or a download failed).
    """
    text = str(text or "").strip()
    if image_count <= 0:
        return text
    placeholder = "[图片]" if image_count == 1 else f"[图片]×{image_count}"
    return f"{placeholder}\n{text}" if text else placeholder


class QQAdapter:
    """Bridge between OneBot v11 events and the fatfish runtime."""

    def __init__(
        self,
        runtime: Any,
        config: Any,
        *,
        on_sent: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
        image_downloader: Callable[..., tuple[bytes, str]] | None = None,
    ):
        self.runtime = runtime
        self.config = config
        # T-0047: called as ``on_sent(payload, result)`` after an
        # ``ai_chat_message`` actually went out, so the service can record the
        # message and lift the desire threshold.  Optional and failure-proof:
        # bookkeeping must never turn a delivered message into a failed item.
        self.on_sent = on_sent

        section = _config_section(config, "qq")
        self.enabled = bool(section.get("enabled", False))
        self.ws_url = str(section.get("ws_url", "ws://127.0.0.1:3001"))
        self.access_token_env = str(section.get("access_token_env", "QQ_ACCESS_TOKEN"))
        self.self_id = _to_int(section.get("self_id", 0), 0)
        self.private_whitelist = _id_set(section.get("private_whitelist", []))
        self.group_whitelist = _id_set(section.get("group_whitelist", []))
        self.group_reply_mode = str(section.get("group_reply_mode", "mention")).strip().lower()
        if self.group_reply_mode not in {"mention", "name", "all", "off"}:
            self.group_reply_mode = "mention"
        self.group_cooldown_seconds = max(
            0.0, _to_float(section.get("group_cooldown_seconds", 90), 90.0)
        )
        self.group_max_per_hour = max(0, _to_int(section.get("group_max_per_hour", 6), 6))
        raw_names = section.get("bot_names")
        if raw_names is None:
            raw_names = DEFAULT_BOT_NAMES
        elif isinstance(raw_names, str):
            raw_names = [raw_names]
        self.bot_names = [str(n).strip() for n in raw_names if str(n).strip()]
        if not self.bot_names:
            self.bot_names = list(DEFAULT_BOT_NAMES)
        self.max_message_chars = max(1, _to_int(section.get("max_message_chars", 2000), 2000))

        # Media defaults are code-level so no TOML edit is required.
        self.send_voice = _to_bool(section.get("send_voice", True), True)
        self.send_images = _to_bool(section.get("send_images", True), True)
        self.send_voice_in_group = _to_bool(section.get("send_voice_in_group", False), False)
        # A voice reply already carries the words.  Sending the same lines as
        # text again looks like a duplicate, so it is opt-in.
        self.send_text_with_voice = _to_bool(
            section.get("send_text_with_voice", False), False
        )
        self.media_max_bytes = max(
            1, _to_int(section.get("media_max_bytes", DEFAULT_MEDIA_MAX_BYTES), DEFAULT_MEDIA_MAX_BYTES)
        )

        # Sliding merge window: 0 (the code default) keeps the historical
        # one-turn-per-message behaviour — existing deployments and tests are
        # untouched until the profile opts in (production sets 5).
        self.merge_window_seconds = max(
            0.0, _to_float(section.get("merge_window_seconds", 0), 0.0)
        )
        # Image direct-read is off by default (the local LLM may have no
        # vision); the cloud profile turns it on. False = images degrade to
        # the 「[图片]」 placeholder without any download.
        self.vision_enabled = _to_bool(section.get("vision_enabled", False), False)
        self.vision_max_bytes = VISION_MAX_BYTES
        self.image_downloader: Callable[..., tuple[bytes, str]] = (
            image_downloader if image_downloader is not None else download_image
        )

        # T-0028: online catch-up. After every (re)connect the adapter scans
        # the owner's and the friend's sessions for unanswered messages and
        # replies once, in order. The service owns judgment/watermarks/audit;
        # the adapter injects the platform mechanics below.
        self.catchup_enabled = _to_bool(section.get("catchup_enabled", True), True)
        self.catchup_delay_seconds = max(
            0.0, _to_float(section.get("catchup_delay_seconds", 10), 10.0)
        )
        self.catchup: CatchUpService | None = (
            CatchUpService(self.runtime, config) if self.catchup_enabled else None
        )
        self._catchup_pending_since: float | None = None

        self.client_factory: Callable[..., OneBotClient] = OneBotClient
        self.client: Any | None = None
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.last_connect_error: str | None = None

        # Per-group rate limiting is in-memory; restarting the adapter resets it.
        self._group_history: dict[str, deque[float]] = {}
        self._group_last_reply: dict[str, float] = {}
        self._seen_events: deque[str] = deque(maxlen=512)

        # Merge-window state. Buffers are keyed by (session_id, user_id); the
        # debounce timers only mark keys due and the flush runs on the thread
        # that owns the runtime (see module docstring for the threading model).
        self._buffers: dict[tuple[str, str], _MergeBuffer] = {}
        self._due_buffers: queue.SimpleQueue = queue.SimpleQueue()
        # Injectable for tests; defaults to a real daemon threading.Timer.
        self._timer_factory: Callable[[float, Callable[[], None]], Any] = _default_timer

    # ------------------------------------------------------------------ config
    @property
    def access_token(self) -> str | None:
        return env_secret(self.access_token_env)

    # ------------------------------------------------------------------- parse
    def parse_event(self, event: dict[str, Any] | None) -> InboundMessage | None:
        if not isinstance(event, dict):
            return None
        if str(event.get("post_type") or "") != "message":
            return None
        message_type = str(event.get("message_type") or "").strip().lower()
        if message_type not in {"private", "group"}:
            return None

        self_id = _to_int(event.get("self_id"), 0) or self.self_id
        user_id = _clean_id(event.get("user_id"))
        group_id = _clean_id(event.get("group_id")) if message_type == "group" else None
        if not user_id or (message_type == "group" and not group_id):
            return None

        # Never answer ourselves; also covers echoed bot messages.
        if str(user_id) == str(self_id) and self_id:
            return None
        if bool(event.get("self_id")) and str(event.get("user_id")) == str(event.get("self_id")):
            return None

        if message_type == "private":
            if self.private_whitelist and str(user_id) not in self.private_whitelist:
                return None
            target_id = user_id
            is_group = False
        else:
            if self.group_whitelist and str(group_id) not in self.group_whitelist:
                return None
            target_id = str(group_id)
            is_group = True

        text, mentioned, reply_to, images = _extract_message(event, self_id)
        text = text.strip()
        # A pure-image message (no text segments) used to be dropped silently
        # here; it now survives as a message whose turn text will be the
        # 「[图片]」 placeholder.
        if not text and not images:
            return None

        session_id = make_session_id("qq", "group" if is_group else "private", target_id)
        event_id = _event_id(event, session_id)

        if is_group and self.merge_window_seconds <= 0:
            # Window off: per-message gate, exactly as before. Window on: the
            # gate moves to flush time so one @-mention inside the window
            # admits the whole merged turn (an unmentionable image segment
            # buffered first must not be dropped before its merge partner
            # arrives), and _record_group_reply counts one reply per flush.
            reason = self._group_gate_reason(
                group_id=str(group_id),
                text=text,
                mentioned=mentioned,
                now=time.time(),
            )
            if reason is not None:
                self._log_group_skip(
                    session_id=session_id,
                    group_id=str(group_id),
                    user_id=user_id,
                    reason=reason,
                )
                return None
            self._record_group_reply(str(group_id), now=time.time())

        return InboundMessage(
            platform="qq",
            event_id=event_id,
            session_id=session_id,
            user_id=user_id,
            group_id=str(group_id) if group_id is not None else None,
            text=text,
            is_group=is_group,
            mentioned=mentioned,
            reply_to=reply_to,
            sender_name=_sender_name(event),
            images=list(images),
            raw=event,
        )

    def _group_gate_reason(
        self,
        *,
        group_id: str,
        text: str,
        mentioned: bool,
        now: float,
    ) -> str | None:
        mode = self.group_reply_mode
        if mode == "off":
            return "group_replies_off"
        if mode == "mention" and not mentioned:
            return "mention_required"
        if mode == "name" and not any(name in text for name in self.bot_names):
            return "name_required"

        history = self._group_history.setdefault(group_id, deque())
        cutoff = now - 3600.0
        while history and history[0] < cutoff:
            history.popleft()
        last = self._group_last_reply.get(group_id)
        if last is not None and self.group_cooldown_seconds > 0:
            if now - last < self.group_cooldown_seconds:
                return "cooldown"
        if self.group_max_per_hour > 0 and len(history) >= self.group_max_per_hour:
            return "group_rate_limit"
        return None

    def _record_group_reply(self, group_id: str, *, now: float) -> None:
        history = self._group_history.setdefault(group_id, deque())
        history.append(now)
        cutoff = now - 3600.0
        while history and history[0] < cutoff:
            history.popleft()
        self._group_last_reply[group_id] = now

    def _log_group_skip(
        self,
        *,
        session_id: str,
        group_id: str,
        user_id: str,
        reason: str,
    ) -> None:
        memory = getattr(self.runtime, "memory", None)
        log = getattr(memory, "log_decision", None)
        if not callable(log):
            return
        try:
            log(
                "qq_group_skip",
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "reason": reason,
                    "mode": self.group_reply_mode,
                },
                action="skip",
                outcome=reason,
                model="qq",
                session_id=session_id,
            )
        except Exception:  # pragma: no cover - logging must never break parsing
            pass

    # ------------------------------------------------------------------ handle
    def handle_event(self, event: dict[str, Any] | None) -> dict[str, Any] | None:
        # T-0072: cache a merged-forward ref before anything else. Even a
        # textless forward card (parse_event drops it) must leave its ref in
        # the session cache so a later ``read_chat`` scope=forward can read
        # what was sent; caching never runs a turn and never fails an event.
        self._cache_forward_ref(event)
        message = self.parse_event(event)
        if message is None:
            return None
        if self.merge_window_seconds > 0:
            # Mature windows flush first: if a debounce timer fired while the
            # previous turn was still running, the new message must open a
            # fresh window instead of being swallowed by the expired one.
            self.drain_due_flushes()
            return self._buffer_message(message)
        if message.event_id in self._seen_events:
            return {
                "event_id": message.event_id,
                "session_id": message.session_id,
                "duplicate": True,
                "media_queued": 0,
                "media_errors": [],
            }
        text, data_urls = self._prepare_turn_input(message)
        if not text:
            return None
        result = self._turn_and_deliver(
            session_id=message.session_id,
            target_type="group" if message.is_group else "private",
            target_id=str(message.group_id if message.is_group else message.user_id),
            event_id=message.event_id,
            is_group=message.is_group,
            text=text,
            reply_to=message.reply_to,
            sender_id=message.user_id,
            sender_name=message.sender_name,
            images=data_urls,
        )
        self._seen_events.append(message.event_id)
        return result

    # ------------------------------------------------- forward cache (T-0072)
    def _cache_forward_ref(self, event: dict[str, Any] | None) -> None:
        """Hand a merged-forward ref to the runtime, per session.

        The runtime keeps exactly one ref per session (``_remember_forward_ref``).
        Read-only and failure-proof: no reply is produced, no message is sent,
        and a runtime without the helper (older embedders, test doubles) is
        simply skipped. Whitelists are not applied here -- this only records
        that a forward exists; turns still go through the normal gates.
        """
        try:
            if not isinstance(event, dict):
                return
            ref = extract_forward_ref(event)
            if not ref:
                return
            message_type = str(event.get("message_type") or "").strip().lower()
            if message_type == "group":
                group_id = _clean_id(event.get("group_id"))
                if not group_id:
                    return
                session_id = make_session_id("qq", "group", group_id)
            elif message_type == "private":
                user_id = _clean_id(event.get("user_id"))
                if not user_id:
                    return
                session_id = make_session_id("qq", "private", user_id)
            else:
                return
            remember = getattr(self.runtime, "_remember_forward_ref", None)
            if callable(remember):
                remember(session_id, ref)
        except Exception:  # caching must never break event handling
            pass

    # ------------------------------------------------- merge window (sliding)
    def _buffer_message(self, message: InboundMessage) -> dict[str, Any]:
        """Append to the sender's sliding window and restart its timer.

        The event id is marked seen immediately, so a replay of the same event
        (NTQQ re-delivery, catch-up redispatch) can never be counted twice,
        whether the window has flushed yet or not.
        """
        if message.event_id in self._seen_events:
            return {
                "event_id": message.event_id,
                "session_id": message.session_id,
                "duplicate": True,
                "buffered": True,
                "media_queued": 0,
                "media_errors": [],
            }
        key = (message.session_id, str(message.user_id))
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = _MergeBuffer()
            self._buffers[key] = buffer
        buffer.messages.append(message)
        self._seen_events.append(message.event_id)
        self._rearm_timer(key, buffer)
        return {
            "event_id": message.event_id,
            "session_id": message.session_id,
            "buffered": True,
            "window_messages": len(buffer.messages),
            "media_queued": 0,
            "media_errors": [],
        }

    def _rearm_timer(self, key: tuple[str, str], buffer: _MergeBuffer) -> None:
        # Sliding window: every arriving message cancels the previous timer
        # and restarts the full delay; the window closes only after
        # ``merge_window_seconds`` of silence from this sender.
        if buffer.timer is not None:
            buffer.timer.cancel()
        timer = self._timer_factory(
            self.merge_window_seconds, lambda: self._mark_due(key)
        )
        buffer.timer = timer
        timer.start()

    def _mark_due(self, key: tuple[str, str]) -> None:
        # Runs on the timer thread. It must only enqueue the key: the SQLite
        # connection is thread-confined, so the flush itself happens on the
        # adapter's owning thread (run_forever loop / handle_event / an
        # explicit drain_due_flushes call).
        self._due_buffers.put(key)

    def drain_due_flushes(self) -> list[dict[str, Any]]:
        """Flush every window whose quiet timer has fired.

        Serial by construction — call this from the thread that owns the
        runtime (``run_forever`` does it once per recv iteration; standalone
        embedders and tests call it directly).
        """
        results: list[dict[str, Any]] = []
        while True:
            try:
                key = self._due_buffers.get_nowait()
            except queue.Empty:
                break
            result = self._flush_key(key)
            if result is not None:
                results.append(result)
        return results

    def _flush_key(self, key: tuple[str, str]) -> dict[str, Any] | None:
        """Merge one window into a single message and run its turn."""
        buffer = self._buffers.pop(key, None)
        if buffer is None or not buffer.messages:
            return None  # stale marker or already flushed
        if buffer.timer is not None:
            buffer.timer.cancel()
            buffer.timer = None
        merged = self._merge_messages(buffer.messages)
        if merged.is_group:
            # Deferred group gate over the whole window: any segment's
            # mention admits the merge; the reply is rate-counted once.
            reason = self._group_gate_reason(
                group_id=str(merged.group_id),
                text=merged.text,
                mentioned=merged.mentioned,
                now=time.time(),
            )
            if reason is not None:
                self._log_group_skip(
                    session_id=merged.session_id,
                    group_id=str(merged.group_id or ""),
                    user_id=merged.user_id,
                    reason=reason,
                )
                return {
                    "event_id": merged.event_id,
                    "session_id": merged.session_id,
                    "skipped": reason,
                    "media_queued": 0,
                    "media_errors": [],
                }
            self._record_group_reply(str(merged.group_id), now=time.time())
        if merged.event_id in self._seen_events:
            return {
                "event_id": merged.event_id,
                "session_id": merged.session_id,
                "duplicate": True,
                "media_queued": 0,
                "media_errors": [],
            }
        self._seen_events.append(merged.event_id)
        text, data_urls = self._prepare_turn_input(merged)
        if not text:
            return None
        return self._turn_and_deliver(
            session_id=merged.session_id,
            target_type="group" if merged.is_group else "private",
            target_id=str(merged.group_id if merged.is_group else merged.user_id),
            event_id=merged.event_id,
            is_group=merged.is_group,
            text=text,
            reply_to=merged.reply_to,
            sender_id=merged.user_id,
            sender_name=merged.sender_name,
            images=data_urls,
        )

    def _merge_messages(self, messages: list[InboundMessage]) -> InboundMessage:
        """Collapse one window into a single inbound message.

        Text segments join with a blank line (they were separate messages);
        images accumulate in arrival order; the mention flag ORs across the
        window; reply_to is the last one present in the window, and sender
        name and raw event come from the window's last message.
        """
        first, last = messages[0], messages[-1]
        texts = [m.text for m in messages if str(m.text or "").strip()]
        images: list[str] = []
        for message in messages:
            images.extend(message.images)
        reply_to = None
        for message in messages:
            if message.reply_to:
                reply_to = message.reply_to
        return InboundMessage(
            platform=first.platform,
            # Synthetic batch id keeps dedupe usable: it names the first event
            # and the window size, while every member id was already marked
            # seen at buffer time.
            event_id=f"{first.event_id}+{len(messages)}",
            session_id=first.session_id,
            user_id=first.user_id,
            group_id=first.group_id,
            text="\n\n".join(texts),
            is_group=first.is_group,
            mentioned=any(m.mentioned for m in messages),
            reply_to=reply_to,
            sender_name=last.sender_name,
            images=images,
            raw=last.raw,
        )

    def _prepare_turn_input(
        self, message: InboundMessage
    ) -> tuple[str, list[str]]:
        """Compose the stored turn text and (vision only) the inline images.

        Returns ``(text, data_urls)``. ``text`` carries the 「[图片]」
        placeholder and is what gets stored and logged; ``data_urls`` only
        materialise when vision is enabled and every download succeeds — any
        failure degrades that image to the placeholder with an error log
        instead of pretending it was seen.
        """
        data_urls: list[str] = []
        if message.images and self.vision_enabled:
            for url in message.images:
                try:
                    data, content_type = self.image_downloader(url)
                except Exception as exc:
                    logger.error(
                        "qq image download failed (%s): %s: %s",
                        url,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                if len(data) > self.vision_max_bytes:
                    logger.error(
                        "qq image skipped for vision: %s (%d > %d bytes)",
                        url,
                        len(data),
                        self.vision_max_bytes,
                    )
                    continue
                data_urls.append(image_data_url(data, content_type))
        return _compose_turn_text(len(message.images), message.text), data_urls

    def _turn_and_deliver(
        self,
        *,
        session_id: str,
        target_type: str,
        target_id: str,
        event_id: str,
        is_group: bool,
        text: str,
        reply_to: str | None = None,
        extra_evidence: list[str] | None = None,
        ephemeral_input: bool = False,
        suppress_self_continue: bool = False,
        sender_id: str | None = None,
        sender_name: str = "",
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one runtime turn and queue its reply/media for the target.

        The single delivery path for both live events (``handle_event``) and
        the T-0028 catch-up turn: text segments, streamed TTS, sticker and
        photo all flow through the outbox exactly as before.
        """
        stream_state: dict[str, Any] = {
            "index": 0,
            "queued": 0,
            "errors": [],
            "item_ids": [],
        }

        def voice_stream(segment: str, tts_result: Any) -> None:
            """Queue and send one successful sentence before the next one."""
            if not bool(getattr(tts_result, "ok", False)):
                return
            if not self.send_voice or (is_group and not self.send_voice_in_group):
                return
            audio_path = str(getattr(tts_result, "audio_path", None) or "").strip()
            if not audio_path:
                stream_state["errors"].append(
                    {"type": "voice", "error": "voice_path_missing"}
                )
                return
            index = int(stream_state["index"])
            stream_state["index"] = index + 1
            try:
                item_id, created = self._enqueue_media(
                    target_type,
                    target_id,
                    event_id,
                    "voice",
                    audio_path,
                    index=index,
                )
                stream_state["item_ids"].append(item_id)
                if created:
                    stream_state["queued"] += 1
                # Deliver now so this sentence is heard while the next one is
                # still being synthesized by the runtime.
                streamed_results = self.runtime.process_outbox(handlers=self.handlers())
            except Exception as exc:  # media must never block text or TTS
                stream_state["errors"].append(
                    {"type": "voice", "error": f"{type(exc).__name__}: {exc}"}
                )
                return
            for streamed_result in streamed_results:
                if not isinstance(streamed_result, dict):
                    continue
                if streamed_result.get("id") != item_id:
                    continue
                if streamed_result.get("status") in {"failed", "error"}:
                    stream_state["errors"].append(
                        {
                            "type": "voice",
                            "error": str(streamed_result.get("error") or "send_failed"),
                        }
                    )

        stream_kwargs: dict[str, Any] = {}
        tts_section = _config_section(self.config, "tts")
        if _to_bool(tts_section.get("stream_sentences", True), True):
            stream_kwargs["voice_stream"] = voice_stream
        # Internal-turn switches ride along only when set, so the live-event
        # path calls the runtime with exactly the signature it always had.
        turn_kwargs: dict[str, Any] = {}
        if extra_evidence:
            turn_kwargs["extra_evidence"] = list(extra_evidence)
        if ephemeral_input:
            turn_kwargs["ephemeral_input"] = True
        if suppress_self_continue:
            turn_kwargs["suppress_self_continue"] = True
        # v13 sender attribution: present on live events, absent on the
        # catch-up turn (which is system-authored and ephemeral anyway).
        if sender_id:
            turn_kwargs["sender_id"] = str(sender_id)
        if sender_name:
            turn_kwargs["sender_name"] = str(sender_name)
        # Vision images ride along only when present, so every existing
        # runtime caller/fake keeps its exact signature.
        if images:
            turn_kwargs["images"] = [str(url) for url in images]
        # Async photo jobs keep the inbound id for passive replies.  Catch-up
        # turns use a synthetic id, so they must not become reply targets.
        if not ephemeral_input and event_id:
            turn_kwargs["reply_to"] = str(event_id)
        result = self.runtime.handle_user_message(
            session_id,
            text,
            is_group=is_group,
            **turn_kwargs,
            **stream_kwargs,
        )
        messages = result.get("messages") or []
        if isinstance(messages, str):
            messages = [messages]
        messages = [str(m) for m in messages if str(m).strip()]
        if (
            messages
            and not self.send_text_with_voice
            and self._voice_delivered(result, is_group, stream_state)
        ):
            tts = result.get("tts") if isinstance(result, dict) else None
            covered = tts.get("covered_messages") if isinstance(tts, dict) else None
            if covered is None:
                covered = len(messages)
            try:
                covered = max(0, min(int(covered), len(messages)))
            except (TypeError, ValueError):
                covered = len(messages)
            # Keep messages appended after synthesis (e.g. the photo note).
            messages = messages[covered:]
        queued = 0
        for index, text in enumerate(messages):
            text = _trim_text(text, self.max_message_chars)
            if not text:
                continue
            payload = {
                "target_type": target_type,
                "target_id": str(target_id or ""),
                "text": text,
                "reply_to": reply_to if index == 0 else None,
            }
            _, created = self.runtime.outbox.enqueue(
                "qq_send",
                payload,
                idempotency_key=f"qq:{event_id}:{index}",
            )
            if created:
                queued += 1
        media_queued, media_errors, media_item_ids = self._queue_media_result(
            result, target_type=target_type, target_id=target_id, event_id=event_id, is_group=is_group
        )
        media_queued += int(stream_state["queued"])
        media_errors = [*stream_state["errors"], *media_errors]
        media_item_ids |= {int(item_id) for item_id in stream_state["item_ids"]}
        outbox_results = self.runtime.process_outbox(handlers=self.handlers())
        media_errors = list(media_errors)
        for outbox_result in outbox_results:
            if not isinstance(outbox_result, dict):
                continue
            inner = outbox_result.get("result")
            if isinstance(inner, dict):
                for error in inner.get("media_errors") or []:
                    if isinstance(error, dict):
                        media_errors.append(error)
            if (
                outbox_result.get("id") in media_item_ids
                and outbox_result.get("status") == "failed"
            ):
                media_errors.append(
                    {
                        "type": "media",
                        "error": str(outbox_result.get("error") or "send_failed"),
                    }
                )
        return {
            "event_id": event_id,
            "session_id": session_id,
            "is_group": is_group,
            "queued": queued,
            "media_queued": media_queued,
            "media_errors": media_errors,
            "outbox": outbox_results,
        }

    def _voice_delivered(
        self,
        result: Any,
        is_group: bool,
        stream_state: dict[str, Any],
    ) -> bool:
        """True when this turn's whole reply was already sent as voice."""
        if not self.send_voice or (is_group and not self.send_voice_in_group):
            return False
        tts = result.get("tts") if isinstance(result, dict) else None
        if not isinstance(tts, dict) or not bool(tts.get("ok")):
            return False
        if bool(tts.get("streamed")):
            total = _to_int(tts.get("segments_total"), 0)
            ok = _to_int(tts.get("segments_ok"), 0)
            return (
                total > 0
                and ok == total
                and _to_int(stream_state.get("queued"), 0) == total
                and not stream_state.get("errors")
            )
        audio_path = str(tts.get("audio_path") or "").strip()
        return bool(audio_path) and Path(audio_path).is_file()

    def _queue_media_result(
        self,
        result: Any,
        *,
        target_type: str,
        target_id: str,
        event_id: str,
        is_group: bool,
    ) -> tuple[int, list[dict[str, Any]], set[int]]:
        """Queue the runtime's TTS voice and sticker image for this turn.

        Returns ``(queued, errors, outbox_item_ids)``. Media is optional: a
        missing/broken path records an error instead of touching the text reply.
        """
        if not isinstance(result, dict):
            return 0, [], set()

        queued = 0
        errors: list[dict[str, Any]] = []
        item_ids: set[int] = set()

        tts = result.get("tts")
        if (
            isinstance(tts, dict)
            and not bool(tts.get("streamed"))
            and bool(tts.get("ok"))
        ):
            audio_path = str(tts.get("audio_path") or "").strip()
            if not audio_path:
                errors.append({"type": "voice", "error": "voice_path_missing"})
            elif self.send_voice and (not is_group or self.send_voice_in_group):
                item_id, created = self._enqueue_media(
                    target_type,
                    target_id,
                    event_id,
                    "voice",
                    audio_path,
                )
                item_ids.add(item_id)
                if created:
                    queued += 1

        sticker = result.get("sticker")
        if (
            isinstance(sticker, dict)
            and bool(sticker.get("sent"))
            and self.send_images
            and not is_group
        ):
            sticker_id = str(sticker.get("sticker_id") or "").strip()
            file_path = str(sticker.get("file_path") or "").strip()
            if not file_path and sticker_id:
                file_path = self._sticker_path(sticker_id)
                if not file_path:
                    errors.append(
                        {"type": "image", "name": sticker_id, "error": "sticker_not_found"}
                    )
            elif not file_path:
                errors.append({"type": "image", "error": "sticker_id_missing"})
            if file_path:
                item_id, created = self._enqueue_media(
                    target_type,
                    target_id,
                    event_id,
                    "image",
                    file_path,
                )
                item_ids.add(item_id)
                if created:
                    queued += 1

        photo = result.get("photo")
        if (
            isinstance(photo, dict)
            and bool(photo.get("ok"))
            and self.send_images
            and not is_group
        ):
            file_path = str(photo.get("path") or "").strip()
            if not file_path:
                errors.append({"type": "image", "error": "photo_path_missing"})
            else:
                # index=1 keeps this item distinct from a sticker image with
                # the same event id; both can be sent in one turn.
                item_id, created = self._enqueue_media(
                    target_type,
                    target_id,
                    event_id,
                    "image",
                    file_path,
                    index=1,
                )
                item_ids.add(item_id)
                if created:
                    queued += 1

        sing = result.get("sing")
        if (
            isinstance(sing, dict)
            and bool(sing.get("ok"))
            and self.send_voice
            and not is_group
        ):
            file_path = str(sing.get("path") or "").strip()
            if not file_path:
                errors.append({"type": "voice", "error": "sing_path_missing"})
            else:
                # index=2 keeps the song distinct from the turn's spoken reply
                # (voice, no index) and from a photo (image, index=1).
                item_id, created = self._enqueue_media(
                    target_type,
                    target_id,
                    event_id,
                    "voice",
                    file_path,
                    index=2,
                )
                item_ids.add(item_id)
                if created:
                    queued += 1

        return queued, errors, item_ids

    def _sticker_path(self, sticker_id: str) -> str:
        service = getattr(self.runtime, "sticker", None)
        getter = getattr(service, "get", None)
        if not callable(getter):
            return ""
        try:
            sticker = getter(sticker_id)
        except Exception:  # pragma: no cover - lookup must never break a reply
            return ""
        return str(getattr(sticker, "file_path", "") or "").strip() if sticker else ""

    def _enqueue_media(
        self,
        target_type: str,
        target_id: str,
        event_id: str,
        media_type: str,
        path: str,
        *,
        index: int | None = None,
    ) -> tuple[int, bool]:
        payload = {
            "target_type": target_type,
            "target_id": target_id,
            "media": [{"type": media_type, "path": path, "name": _basename(path)}],
        }
        key = f"qq:{event_id}:{media_type}"
        if index is not None:
            key = f"{key}:{index}"
        return self.runtime.outbox.enqueue(
            "qq_send",
            payload,
            idempotency_key=key,
        )

    # --------------------------------------------------------- catch-up (T-0028)
    def _login_ok(self) -> bool:
        """True only with a live connection: no undeliverable catch-up replies."""
        client = self.client
        return bool(client is not None and getattr(client, "connected", False))

    def _maybe_run_connect_catchup(self, *, now: float | None = None) -> dict[str, Any] | None:
        """Fire the armed post-connect scan once the connection feels stable.

        The delay (``qq.catchup_delay_seconds``) is deliberate: NTQQ
        re-delivers most offline messages as normal events right after login,
        and those are answered by the ordinary path. Scanning only after the
        delay (plus the stream-order judgment and the history dedupe) keeps
        the catch-up from double-answering them.
        """
        now = time.time() if now is None else float(now)
        armed = self._catchup_pending_since
        if self.catchup is None or armed is None:
            return None
        if now - armed < self.catchup_delay_seconds:
            return None
        self._catchup_pending_since = None
        try:
            return self.catchup.scan_all(
                now=now,
                trigger="connect",
                connected=self._login_ok(),
                fetch_history=self._fetch_history_messages,
                run_turn=self._run_catchup_turn,
                enqueue_retry=self._enqueue_catchup_retry,
            )
        except Exception as exc:  # must never tear down the recv loop
            print(
                f"[qq] catchup scan failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
            return None

    def _fetch_history_texts(self, uin: str, count: int) -> list[tuple[str, float]]:
        """Legacy text-only view of :meth:`_fetch_history_messages`."""
        return [(row.text, row.at) for row in self._fetch_history_messages(uin, count)]

    def _fetch_history_messages(self, uin: str, count: int) -> list[HistoryMessage]:
        """``get_friend_msg_history`` rows as history messages for the service.

        Rows are re-parsed by :meth:`parse_event`, so whitelist, self-message
        and empty-text filters apply to history exactly as to live events.
        Contact rows older than the latest bot-authored row are dropped: that
        row is the bot's last reply, which is the only usable cutoff for a
        session with no local assistant turn (person-card contacts).
        Raises on any failure; the service degrades to the local stream.
        """

        def redispatch(event: dict[str, Any]) -> None:
            # A live message pushed while the history response is polled must
            # flow through the ordinary event path, not be drained by the
            # fetch (ADR-0008). Same guard shape as the recv loop.
            try:
                self.handle_event(event)
            except Exception as exc:
                print(
                    f"[qq] failed to handle event during history fetch: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

        client = self.ensure_client()
        getter = getattr(client, "get_friend_msg_history", None)
        if not callable(getter):
            raise RuntimeError("get_friend_msg_history not supported by this client")
        rows = getter(uin, count=count, on_event=redispatch)
        messages: list[HistoryMessage] = []
        # The local turn stream is the primary "answered" cutoff.  A session
        # that was never recorded locally (a person-card contact such as a
        # teacher) has no assistant turn, so fall back to the history itself:
        # the last row authored by the bot is a safe cutoff -- contact rows
        # older than it were already answered.
        last_self_ts = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            event = dict(row)
            event.setdefault("post_type", "message")
            event.setdefault("message_type", "private")
            if not event.get("self_id") and self.self_id:
                event["self_id"] = self.self_id
            try:
                ts = float(event.get("time") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            self_id = _to_int(event.get("self_id"), 0) or self.self_id
            raw_sender = str(event.get("user_id") or "").strip()
            if raw_sender and self_id and raw_sender == str(self_id):
                last_self_ts = max(last_self_ts, ts)
                continue
            message = self.parse_event(event)
            if message is None or str(message.user_id) != str(uin):
                continue
            # A row whose event was already buffered/handled by the live path
            # must not ride the catch-up evidence too: the merge window will
            # (or did) answer it, and judging it "missed" here would double-
            # count it (once by the window flush, once by the catch-up turn).
            if str(message.event_id) in self._seen_events:
                continue
            messages.append(
                HistoryMessage(
                    text=_compose_turn_text(len(message.images), message.text),
                    at=ts,
                    images=[str(url) for url in message.images],
                )
            )
        if last_self_ts > 0:
            messages = [row for row in messages if row.at > last_self_ts]
        return messages

    def _catchup_images(self, missed: list[HistoryMessage]) -> list[str]:
        """Inline data URLs for images in the missed rows (vision only).

        The same downloader and size cap as the live path; every failure
        degrades that image to the ``[图片]`` placeholder instead of pretending
        it was seen.
        """
        if not self.vision_enabled:
            return []
        images: list[str] = []
        for row in missed:
            for url in list(getattr(row, "images", None) or []):
                if len(images) >= CATCHUP_MAX_IMAGES:
                    return images
                try:
                    data, content_type = self.image_downloader(url)
                except Exception as exc:
                    logger.error(
                        "catchup image download failed (%s): %s: %s",
                        url,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                if len(data) > self.vision_max_bytes:
                    logger.error(
                        "catchup image skipped for vision: %s (%d > %d bytes)",
                        url,
                        len(data),
                        self.vision_max_bytes,
                    )
                    continue
                images.append(image_data_url(data, content_type))
        return images

    def _run_catchup_turn(
        self,
        target: CatchupTarget,
        missed: list[HistoryMessage],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """One catch-up turn: system prompt + wrapped missed-message evidence."""
        now = time.time() if now is None else float(now)
        if not self._login_ok():
            return {"ok": False, "outcome": "skipped_offline"}
        llm = getattr(self.runtime, "llm", None)
        if llm is None or not bool(getattr(llm, "available", False)):
            # self_continue precedent (T-0027): without the LLM she stays
            # silent instead of shipping a fallback line for messages she
            # has not actually read.
            return {"ok": False, "outcome": "llm_unavailable"}
        service = self.catchup
        if service is None:  # pragma: no cover - guarded by callers
            return {"ok": False, "outcome": "error", "error": "catchup_disabled"}
        evidence = service.build_evidence(target, missed)
        images = self._catchup_images(missed)
        result = self._turn_and_deliver(
            session_id=target.session_id,
            target_type="private",
            target_id=target.uin,
            event_id=f"catchup-{target.uin}-{int(now)}",
            is_group=False,
            text=service.prompt,
            extra_evidence=evidence,
            # Missed images ride the same inline-vision channel as live
            # messages; without vision they stay `[图片]` in the evidence.
            images=images or None,
            # The instruction is system-authored (nobody typed it), so it must
            # not read as a real user utterance later; the missed messages ride
            # as wrapped evidence. suppress_self_continue: the reply answers
            # existing messages -- it is not the opening of a new exchange.
            ephemeral_input=True,
            suppress_self_continue=True,
        )
        return {
            "ok": True,
            "outcome": "replied",
            "queued": int(result.get("queued") or 0),
            "missed": len(missed),
        }

    def _enqueue_catchup_retry(self, target: CatchupTarget, fire_at: float) -> bool:
        """Park a re-scan at the next quiet end (08:00) as an outbox item."""
        day = human_date(float(fire_at), self.catchup.tz_name)  # type: ignore[union-attr]
        _, created = self.runtime.outbox.enqueue(
            CATCHUP_RETRY_KIND,
            {"session_id": target.session_id, "uin": target.uin, "label": target.label},
            idempotency_key=f"catchup-retry:{target.session_id}:{day}",
            available_at=float(fire_at),
        )
        return bool(created)

    def _handle_catchup_item(self, item: Any) -> dict[str, Any]:
        """Outbox handler for the quiet-hours deferred catch-up re-scan."""
        if isinstance(item, dict):
            item_id = item.get("id")
            payload = item.get("payload") or {}
        else:
            item_id = getattr(item, "id", None)
            payload = getattr(item, "payload", None) or {}
        if self.catchup is None:
            return {"ok": False, "id": item_id, "error": "catchup_disabled"}
        session_id = str(payload.get("session_id") or "").strip()
        uin = str(payload.get("uin") or "").strip()
        if not session_id or not uin or session_id != f"qq:private:{uin}":
            return {"ok": False, "id": item_id, "error": "invalid_catchup_payload"}
        if not self._login_ok():
            # No login state, no undeliverable reply: fail the item (the
            # outbox retries it, and the next successful connect rescans).
            return {"ok": False, "id": item_id, "error": "not_connected"}
        target = CatchupTarget(
            label=str(payload.get("label") or "friend") or "friend",
            uin=uin,
            session_id=session_id,
        )
        result = self.catchup.scan_target(
            target,
            now=time.time(),
            # A scheduled retry is intentional: bypass the connect-storm
            # interval, keep the judgment itself idempotent.
            force=True,
            fetch_history=self._fetch_history_messages,
            run_turn=self._run_catchup_turn,
            enqueue_retry=self._enqueue_catchup_retry,
            connected=True,
        )
        outcome = str(result.get("outcome") or "error")
        ok = outcome in {"replied", "nothing_new", "skipped_quiet"}
        if ok:
            return {"ok": True, "id": item_id, "kind": CATCHUP_RETRY_KIND, "outcome": outcome}
        return {"ok": False, "id": item_id, "kind": CATCHUP_RETRY_KIND, "error": outcome}

    # -------------------------------------------------- history reads (T-0071)
    def fetch_group_history(
        self,
        group_id: str | int,
        count: int = 20,
        message_seq: int = 0,
        *,
        reverse_order: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Read-only: one page of a group's history via the NapCat extension.

        Raw OneBot message-event rows (format with
        :func:`fatfish.platforms.history.extract_history_rows`). Fault
        tolerant: an unreachable client, a missing extension or a failed
        call all read as ``[]``. Never sends a message. ``on_event`` (when
        given) re-dispatches live events pushed while the response is
        polled — pass ``None`` only where dropping those events is
        acceptable.
        """
        return self._read_rows(
            getter_name="get_group_msg_history",
            label="fetch_group_history",
            invoke=lambda get: get(
                group_id,
                count=count,
                message_seq=message_seq,
                reverse_order=reverse_order,
                on_event=on_event,
            ),
        )

    def fetch_forward_messages(
        self,
        message_id: str | int,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Read-only: a merged-forward message's content rows.

        ``message_id`` is the wrapper message id or the ``resid`` extracted
        by :func:`extract_forward_ref`. Empty on any failure; never sends.
        """
        return self._read_rows(
            getter_name="get_forward_msg",
            label="fetch_forward_messages",
            invoke=lambda get: get(message_id, on_event=on_event),
        )

    def fetch_friend_history(
        self,
        user_id: str | int,
        count: int = 20,
        *,
        message_seq: int = 0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Read-only: one page of a friend's private-chat history.

        Straight passthrough to the client's ``get_friend_msg_history``
        (the same extension the T-0028 catch-up uses), wrapped in the same
        fault tolerance as the other fetchers. Never sends a message.
        """
        return self._read_rows(
            getter_name="get_friend_msg_history",
            label="fetch_friend_history",
            invoke=lambda get: get(
                user_id,
                count=count,
                message_seq=message_seq,
                on_event=on_event,
            ),
        )

    def fetch_group_members(self, group_id: str | int) -> list[dict[str, Any]]:
        """Read-only: the group's members as ``[{"user_id","nickname","card"}]``.

        Normalization happens on the client (:meth:`OneBotClient.get_group_member_list`);
        this wrapper adds the fault tolerance. Empty on any failure.
        """
        return self._read_rows(
            getter_name="get_group_member_list",
            label="fetch_group_members",
            invoke=lambda get: get(group_id),
        )

    def _read_rows(
        self,
        *,
        getter_name: str,
        label: str,
        invoke: Callable[[Any], Any],
    ) -> list[dict[str, Any]]:
        """One fault-tolerant read-only client call.

        Connects lazily via :meth:`ensure_client`, calls the named getter
        through ``invoke`` and returns its dict rows. Any failure — no
        connection, client without the extension, call raising, non-list
        result — logs a warning and returns ``[]``. The ``invoke`` callables
        used by the ``fetch_*`` wrappers only ever invoke read actions, so
        this path cannot send a message.
        """
        try:
            client = self.ensure_client()
            getter = getattr(client, getter_name, None)
            if not callable(getter):
                logger.warning(
                    "qq %s: %s not supported by this client", label, getter_name
                )
                return []
            rows = invoke(getter)
        except Exception as exc:
            logger.warning("qq %s failed: %s: %s", label, type(exc).__name__, exc)
            return []
        if not isinstance(rows, list):
            logger.warning("qq %s: unexpected non-list result", label)
            return []
        return [row for row in rows if isinstance(row, dict)]

    # -------------------------------------------------------------------- send
    def handlers(self) -> dict[str, Callable[[Any], dict[str, Any]]]:
        return {
            "qq_send": self.send_outbox_item,
            # Async artifacts (photo jobs, cloud TTS) are queued by the runtime
            # with the same kind names the official QQBot adapter uses.  NapCat
            # has no OneBot event for those kinds, so normalize them here to the
            # existing ``qq_send`` + media path instead of dropping the row.
            "qq_send_image": self.send_outbox_item,
            "qq_send_voice": self.send_outbox_item,
            "proactive_message": self.send_outbox_item,
            # T-0012 v3: proactive private-chat share to the bound friend
            # slot holder; the payload's friend_uin was resolved (and the
            # content screened) by the runtime at enqueue time.
            "friend_share": self.send_outbox_item,
            # T-0028: a catch-up scan deferred past quiet hours; due the next
            # morning, processed by whichever adapter is ticking then.
            "qq_catchup": self._handle_catchup_item,
            # T-0047: 大肥鱼间聊天 — one AI messaging the other bound AI.
            AI_CHAT_MESSAGE_KIND: self.send_outbox_item,
            # ADR-0014: roaming messages to a bound room, plus the owner-only
            # return notice.  Recipients resolve from the payload's session id;
            # a missing/invalid one is a handler error, never a guessed target.
            "roam_message": self.send_outbox_item,
            "roam_notice": self.send_outbox_item,
        }

    def send_outbox_item(self, item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "")
            item_id = item.get("id")
            payload = item.get("payload") or {}
        else:
            kind = str(getattr(item, "kind", "") or "")
            item_id = getattr(item, "id", None)
            payload = getattr(item, "payload", None) or {}
        if kind == "qq_send":
            target_type = str(payload.get("target_type") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip()
            text = str(payload.get("text") or "")
            media = payload.get("media")
        elif kind in ("qq_send_image", "qq_send_voice"):
            # Runtime-generated async artifacts (e.g. the ModelScope photo
            # poller) carry ``path`` + ``content`` instead of a media list.
            # Map them onto the OneBot segment path so NapCat delivers them.
            target_type = str(payload.get("target_type") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip()
            if not target_id:
                session_id = str(payload.get("session_id") or "")
                parts = session_id.split(":", 2)
                if (
                    len(parts) == 3
                    and parts[0] == "qq"
                    and parts[1] in {"private", "group"}
                    and parts[2]
                ):
                    target_type, target_id = parts[1], parts[2]
            media_kind = "image" if kind == "qq_send_image" else "voice"
            path = str(payload.get("path") or "").strip()
            media = (
                [{"type": media_kind, "path": path, "name": _basename(path)}]
                if path
                else []
            )
            text = str(payload.get("content") or payload.get("caption") or "")
        elif kind in ("proactive_message", "roam_message", "roam_notice"):
            session_id = str(payload.get("session_id") or "")
            if not session_id.startswith("qq:"):
                # Not ours: let the normal runtime/web path own it.
                return {"ok": True, "id": item_id, "kind": kind, "skipped": "not_qq_session"}
            parts = session_id.split(":", 2)
            if len(parts) != 3 or parts[1] not in {"private", "group"} or not parts[2]:
                return {"ok": False, "id": item_id, "kind": kind, "error": "invalid_qq_session_id"}
            if kind == "roam_message" and parts[1] == "group":
                # ADR-0014: groups are outside roaming in P0.
                return {"ok": False, "id": item_id, "kind": kind, "error": "roam_group_not_allowed"}
            target_type, target_id = parts[1], parts[2]
            text = str(payload.get("text") or "")
            media = payload.get("media")
        elif kind == "friend_share":
            # Always a private message to the uin that was bound when the
            # share was queued. No uin in the payload is a handler error,
            # never a guess at a recipient.
            target_type = "private"
            target_id = str(payload.get("friend_uin") or "").strip()
            text = str(payload.get("text") or "")
            media = payload.get("media")
            if not target_id:
                return {"ok": False, "id": item_id, "kind": kind, "error": "missing_friend_uin"}
        elif kind == AI_CHAT_MESSAGE_KIND:
            # 大肥鱼间聊天: always a private message to the bound peer. The
            # recipient comes from the payload and nowhere else -- a missing or
            # empty uin is a handler error, never a guess (no client call at
            # all, so an unbound peer can never be reached by accident).
            target_type = "private"
            target_id = str(payload.get("peer_uin") or "").strip()
            text = str(payload.get("text") or "")
            media = payload.get("media")
            if not target_id:
                return {"ok": False, "id": item_id, "kind": kind, "error": "missing_peer_uin"}
        else:
            return {"ok": False, "id": item_id, "kind": kind, "error": f"unsupported_kind:{kind}"}
        if not isinstance(media, list):
            media = []

        if target_type not in {"private", "group"}:
            return {"ok": False, "id": item_id, "kind": kind, "error": "invalid_target_type"}
        if not target_id:
            return {"ok": False, "id": item_id, "kind": kind, "error": "invalid_target_id"}
        text = _trim_text(text, self.max_message_chars) if text.strip() else ""
        if not text and not media:
            return {"ok": False, "id": item_id, "kind": kind, "error": "empty_message"}

        try:
            client = self.ensure_client()
            if not media:
                # Text-only payloads keep using the simple text API.
                if target_type == "group":
                    echo = client.send_group_text(target_id, text)
                else:
                    echo = client.send_private_text(target_id, text)
                return self._sent(
                    kind,
                    item_id,
                    payload,
                    target_type=target_type,
                    target_id=target_id,
                    echo=echo,
                )

            segments, media_sent, media_errors = self._build_segments(text, media)
            if not segments:
                return {
                    "ok": False,
                    "id": item_id,
                    "kind": kind,
                    "error": "no_sendable_media",
                    "media_errors": media_errors,
                }
            if target_type == "group":
                echo = client.send_group_message(target_id, segments)
            else:
                echo = client.send_private_message(target_id, segments)
        except Exception as exc:
            return {
                "ok": False,
                "id": item_id,
                "kind": kind,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return self._sent(
            kind,
            item_id,
            payload,
            target_type=target_type,
            target_id=target_id,
            echo=echo,
            media_sent=media_sent,
            media_errors=media_errors,
        )

    def _sent(
        self,
        kind: str,
        item_id: Any,
        payload: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
        echo: Any,
        media_sent: int | None = None,
        media_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the success result and run the post-send hook for ai_chat.

        The hook (``on_sent``) is where the service records the message and
        lifts the desire threshold.  It runs only after the platform accepted
        the send, and any exception it raises is logged and swallowed: the
        message is already delivered, so reporting failure would make the
        outbox resend a duplicate.
        """
        result: dict[str, Any] = {
            "ok": True,
            "id": item_id,
            "kind": kind,
            "target_type": target_type,
            "target_id": target_id,
            "echo": echo,
        }
        if media_sent is not None:
            result["media_sent"] = media_sent
            result["media_errors"] = list(media_errors or [])
        if kind == AI_CHAT_MESSAGE_KIND and self.on_sent is not None:
            try:
                self.on_sent(dict(payload) if isinstance(payload, dict) else {}, dict(result))
            except Exception as exc:  # bookkeeping must not undo a delivery
                logger.warning("ai_chat on_sent 回调失败（消息已发出）：%s", exc)
        return result

    def _build_segments(
        self,
        text: str,
        media: list[Any],
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        segments: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        sent = 0
        for raw in media:
            if not isinstance(raw, dict):
                errors.append({"type": "media", "error": "invalid_media_item"})
                continue
            media_type = str(raw.get("type") or "").strip().lower()
            path = str(raw.get("path") or "").strip()
            name = str(raw.get("name") or "").strip() or _basename(path)
            segment, error = self._media_segment(media_type, path)
            if segment is None:
                errors.append({"type": media_type or "media", "name": name, "error": error})
                continue
            segments.append(segment)
            sent += 1
        return segments, sent, errors

    def _media_segment(
        self,
        media_type: str,
        path: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Validate a local media file and build a base64 OneBot segment."""
        if media_type == "image":
            suffixes = IMAGE_SUFFIXES
            segment_type = "image"
        elif media_type == "voice":
            suffixes = VOICE_SUFFIXES
            segment_type = "record"
        else:
            return None, "unsupported_media_type"
        if not path:
            return None, "empty_path"

        file_path = Path(path)
        if file_path.suffix.lower() not in suffixes:
            return None, "unsupported_suffix"
        try:
            if not file_path.is_file():
                return None, "missing_file"
            size = file_path.stat().st_size
        except OSError:
            return None, "unreadable_file"
        if size <= 0:
            return None, "empty_file"
        if size > self.media_max_bytes:
            return None, "too_large"
        try:
            data = file_path.read_bytes()
        except OSError:
            return None, "unreadable_file"
        if len(data) > self.media_max_bytes:
            return None, "too_large"

        encoded = base64.b64encode(data).decode("ascii")
        if segment_type == "image":
            return {
                "type": "image",
                "data": {"file": f"base64://{encoded}", "sub_type": 0},
            }, None
        return {"type": "record", "data": {"file": f"base64://{encoded}"}}, None

    # -------------------------------------------------------------- connection
    def connect(self) -> Any:
        if self.client is not None and getattr(self.client, "connected", True):
            return self.client
        client = self.client_factory(
            self.ws_url,
            access_token=self.access_token,
            timeout=5,
        )
        client.connect()
        self.client = client
        self.connected_at = time.time()
        self.last_connect_error = None
        return client

    def ensure_client(self) -> Any:
        if self.client is not None and getattr(self.client, "connected", True):
            return self.client
        return self.connect()

    def close(self) -> None:
        client = self.client
        self.client = None
        # Pending merge windows die with the process by design (≤ one window
        # of messages lost on restart); just stop their timers cleanly.
        for buffer in self._buffers.values():
            if buffer.timer is not None:
                buffer.timer.cancel()
                buffer.timer = None
        self._buffers.clear()
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def run_forever(self, stop_event: Any | None = None) -> None:
        # ADR-0014 §7.4: one reconciliation per process, before the event loop
        # starts.  A trip that was active when the process died becomes
        # ``interrupted``; nothing is replayed or sent.
        self.runtime.reconcile_roam()
        backoff = 1.0
        last_tick = 0.0
        while not _stopped(stop_event):
            try:
                client = self.connect()
                backoff = 1.0
                last_tick = time.time()
                label = self.self_id or "unknown"
                # T-0028: arm the online catch-up for this connection; it
                # fires from the tick below once the connection has been
                # stable for ``qq.catchup_delay_seconds``.
                self._catchup_pending_since = time.time()
                print(
                    f"[qq] connected: {self.ws_url} (self_id={label})",
                    flush=True,
                )
                while not _stopped(stop_event):
                    event = client.recv(timeout=1)
                    if event:
                        self.last_event_at = time.time()
                        if str(event.get("post_type") or "") == "message":
                            try:
                                self.handle_event(event)
                            except Exception as exc:
                                # One malformed message must not tear down the socket.
                                print(
                                    f"[qq] failed to handle event: "
                                    f"{type(exc).__name__}: {exc}",
                                    flush=True,
                                )
                    now = time.time()
                    # Merge windows whose debounce timer fired while we were
                    # blocked in recv/turn: flushed here on the owning thread
                    # (recv(timeout=1) keeps the extra latency under ~1s and
                    # the heartbeat read is never blocked by the timer thread).
                    self.drain_due_flushes()
                    if now - last_tick >= 2.0:
                        self._maybe_run_connect_catchup(now=now)
                        self.runtime.tick(handlers=self.handlers())
                        last_tick = now
            except KeyboardInterrupt:
                break
            except Exception as exc:
                self.last_connect_error = f"{type(exc).__name__}: {exc}"
                print(f"[qq] connection lost: {self.last_connect_error}", flush=True)
                self.close()
                if _stopped(stop_event):
                    break
                if stop_event is not None:
                    try:
                        stop_event.wait(backoff)
                    except Exception:
                        time.sleep(backoff)
                else:
                    time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
        self.close()

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        client = self.client
        connected = bool(client is not None and getattr(client, "connected", True))
        return {
            "enabled": self.enabled,
            "ws_url": self.ws_url,
            "connected": connected,
            "self_id": self.self_id,
            "access_token_set": bool(self.access_token),
            "access_token_env": self.access_token_env,
            "websocket_client": websocket_available(),
            "private_whitelist": sorted(self.private_whitelist),
            "group_whitelist": sorted(self.group_whitelist),
            "group_reply_mode": self.group_reply_mode,
            "group_cooldown_seconds": self.group_cooldown_seconds,
            "group_max_per_hour": self.group_max_per_hour,
            "bot_names": list(self.bot_names),
            "max_message_chars": self.max_message_chars,
            "send_voice": self.send_voice,
            "send_images": self.send_images,
            "send_voice_in_group": self.send_voice_in_group,
            "media_max_bytes": self.media_max_bytes,
            "merge_window_seconds": self.merge_window_seconds,
            "vision_enabled": self.vision_enabled,
            "buffered_windows": len(self._buffers),
            "catchup_enabled": self.catchup_enabled,
            "catchup_delay_seconds": self.catchup_delay_seconds,
            "last_event_at": self.last_event_at,
            "connected_at": self.connected_at,
            "last_connect_error": self.last_connect_error,
        }


# --------------------------------------------------------------------- helpers
def extract_forward_ref(event: dict[str, Any] | None) -> str | None:
    """``resid`` of the event's merged-forward json segment, when it has one.

    NapCat wraps a merged-forward ("聊天记录") as a ``json`` segment whose
    payload announces ``app == "com.tencent.multimsg"`` and carries the
    fetch key in ``meta.detail.resid`` (older clients put ``resid`` at the
    top level). That value can be passed to
    :meth:`OneBotClient.get_forward_msg` to read the wrapped messages.
    Returns ``None`` for anything else — other json apps, unparseable
    payloads, or events without json segments — so a caller can simply
    treat "no ref" as "nothing to expand".
    """
    if not isinstance(event, dict):
        return None
    segments = event.get("message")
    if not isinstance(segments, list):
        return None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if str(segment.get("type") or "") != "json":
            continue
        resid = _multimsg_resid(segment.get("data"))
        if resid:
            return resid
    return None


def _multimsg_resid(data: Any) -> str | None:
    """Pull the resid out of one json segment's ``data`` block, if it is one."""
    candidates: list[Any] = [data]
    if isinstance(data, dict) and "data" in data:
        # OneBot v11 nests the ark payload string under a second "data" key.
        candidates.insert(0, data.get("data"))
    for candidate in candidates:
        payload = candidate
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("app") or "") != "com.tencent.multimsg":
            continue
        meta = payload.get("meta")
        detail = meta.get("detail") if isinstance(meta, dict) else None
        for source in (detail, payload):
            if isinstance(source, dict):
                value = str(source.get("resid") or source.get("resId") or "").strip()
                if value:
                    return value
    return None


def _extract_message(
    event: dict[str, Any], self_id: int
) -> tuple[str, bool, str | None, list[str]]:
    """Return ``(text, mentioned, reply_to, image_urls)``.

    ``image_urls`` are the remote http(s) URLs of the message's image
    segments (NapCat puts them in ``data.url``); local/base64-only image
    segments are skipped — they degrade to the 「[图片]」 placeholder like any
    other undeliverable image.
    """
    segments = event.get("message")
    mentioned = False
    reply_to: str | None = None
    images: list[str] = []
    parts: list[str] = []

    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, str):
                parts.append(segment)
                continue
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "")
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == AT_SEGMENT_TYPE:
                qq = str(data.get("qq") or "")
                if self_id and qq == str(self_id):
                    mentioned = True
                elif not self_id and qq not in {"", "all"}:
                    # Without a configured self_id we cannot prove a mention.
                    mentioned = False
            elif seg_type == REPLY_SEGMENT_TYPE:
                reply_to = str(data.get("id") or "") or reply_to
            elif seg_type == IMAGE_SEGMENT_TYPE:
                url = _image_url(data)
                if url:
                    images.append(url)
            elif seg_type in {"face", "record", "video", "mface", "json", "xml"}:
                continue
        text = "".join(parts)
    elif isinstance(segments, str) and segments:
        text, at_ids, cq_reply, images = _parse_cq_string(segments)
        mentioned = bool(self_id and str(self_id) in at_ids)
        reply_to = cq_reply
    else:
        raw_message = str(event.get("raw_message") or "")
        text, at_ids, cq_reply, images = _parse_cq_string(raw_message)
        mentioned = bool(self_id and str(self_id) in at_ids)
        reply_to = cq_reply

    if not text.strip():
        raw_message = str(event.get("raw_message") or "")
        if raw_message:
            fallback, at_ids, cq_reply, fallback_images = _parse_cq_string(raw_message)
            if fallback.strip() or fallback_images:
                text = fallback
                mentioned = mentioned or bool(self_id and str(self_id) in at_ids)
                reply_to = reply_to or cq_reply
                for url in fallback_images:
                    if url not in images:
                        images.append(url)
    return text, mentioned, reply_to, images


def _image_url(data: dict[str, Any]) -> str:
    """Remote URL of an image segment, or ``""`` for local/base64-only ones."""
    for key in ("url", "file"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _parse_cq_string(raw: str) -> tuple[str, set[str], str | None, list[str]]:
    """Split a CQ-code string into plain text, @-targets, reply id and image URLs."""
    text_parts: list[str] = []
    at_ids: set[str] = set()
    reply_to: str | None = None
    images: list[str] = []
    position = 0
    for match in CQ_RE.finditer(raw):
        text_parts.append(raw[position : match.start()])
        position = match.end()
        cq_type = match.group(1)
        params = _parse_cq_params(match.group(2))
        if cq_type == AT_SEGMENT_TYPE:
            qq = str(params.get("qq") or "")
            if qq:
                at_ids.add(qq)
        elif cq_type == REPLY_SEGMENT_TYPE:
            reply_to = str(params.get("id") or "") or reply_to
        elif cq_type == IMAGE_SEGMENT_TYPE:
            url = str(params.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                images.append(url)
    text_parts.append(raw[position:])
    text = _unescape_cq("".join(text_parts))
    return text, at_ids, reply_to, images


def _parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw.lstrip(",").split(","):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        params[key.strip()] = _unescape_cq(value)
    return params


def _unescape_cq(text: str) -> str:
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _event_id(event: dict[str, Any], session_id: str) -> str:
    message_id = event.get("message_id") or event.get("event_id")
    if message_id not in (None, ""):
        return str(message_id)
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{session_id}:{raw}".encode("utf-8")).hexdigest()[:16]


def _trim_text(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _sender_name(event: dict[str, Any]) -> str:
    """Display name from a OneBot ``sender`` block (group card first).

    ``card`` is the per-group nickname and is the name the group knows the
    person by; ``nickname`` is the account-wide one.  Whitespace is collapsed
    and the result capped, because it ends up in a person card and in prompt
    labels.  A missing sender block yields ``""`` -- the account id is still
    recorded on the turn, but no card is invented without a name.
    """
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return ""
    for key in ("card", "nickname"):
        value = " ".join(str(sender.get(key) or "").split())
        if value:
            return value[:64]
    return ""


def _id_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, (str, int)):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


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


def _basename(path: str) -> str:
    try:
        return Path(str(path or "")).name
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return ""


def _stopped(stop_event: Any | None) -> bool:
    return bool(stop_event is not None and hasattr(stop_event, "is_set") and stop_event.is_set())
