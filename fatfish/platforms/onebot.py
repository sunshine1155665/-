"""Minimal OneBot v11 forward-WebSocket client.

NapCat (or another OneBot v11 implementation) exposes a forward WebSocket
server; this client connects *to* it. ``websocket-client`` is an optional
dependency: importing this module never fails, but connecting without it
raises a clear error.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

try:  # optional dependency; the rest of the app must import without it
    import websocket  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - depends on environment
    websocket = None  # type: ignore[assignment]

INSTALL_HINT = (
    "websocket-client is required for QQ/OneBot support. "
    "Install it with: pip install websocket-client"
)


def websocket_available() -> bool:
    """True when the optional ``websocket-client`` dependency is importable."""
    return websocket is not None


def _require_websocket():
    if websocket is None:
        raise RuntimeError(INSTALL_HINT)
    return websocket


def _as_id(value: Any) -> int | str:
    """OneBot accepts numeric ids; keep strings that are not pure digits."""
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        try:
            return int(text)
        except ValueError:  # pragma: no cover - defensive
            return text
    return text


def _segments(message: Any) -> list[dict[str, Any]]:
    """Normalize a message argument to a OneBot segment list."""
    if not isinstance(message, (list, tuple)):
        raise TypeError("message must be a list of OneBot v11 segments")
    return [segment for segment in message if isinstance(segment, dict)]


def _require_action_ok(action: str, response: dict[str, Any]) -> None:
    """Raise ``RuntimeError`` unless a response reports success."""
    status = str(response.get("status") or "").strip().lower()
    retcode = response.get("retcode")
    ok = status in {"ok", "async"} or retcode in (0, "0")
    if not ok:
        detail = str(response.get("wording") or response.get("message") or "")[:120]
        raise RuntimeError(
            f"{action} failed: status={status!r} retcode={retcode!r} {detail}"
        )


def _response_messages(response: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``data.messages`` dicts of a history/forward response; [] if absent."""
    data = response.get("data")
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return []
    return [row for row in messages if isinstance(row, dict)]


class OneBotClient:
    """Small synchronous OneBot v11 client over a forward WebSocket."""

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:3001",
        access_token: str | None = None,
        timeout: float = 5.0,
    ):
        self.ws_url = str(ws_url)
        self.access_token = (access_token or "").strip() or None
        self.timeout = max(0.1, float(timeout))
        self._ws: Any | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def connected(self) -> bool:
        if self._ws is None:
            return False
        return bool(getattr(self._ws, "connected", True))

    def connect(self) -> "OneBotClient":
        ws_module = _require_websocket()
        headers: list[str] = []
        if self.access_token:
            headers.append(f"Authorization: Bearer {self.access_token}")
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "ping_interval": 20,
            "ping_timeout": 10,
        }
        if headers:
            kwargs["header"] = headers
        try:
            self._ws = ws_module.create_connection(self.ws_url, **kwargs)
        except Exception as exc:
            self._ws = None
            raise RuntimeError(
                f"cannot connect to OneBot at {self.ws_url}: {type(exc).__name__}: {exc}. "
                "Check that NapCat is running and the forward WebSocket server is enabled."
            ) from exc
        return self

    def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            ws.close()
        except Exception:  # pragma: no cover - socket already dead
            pass

    # ------------------------------------------------------------------ send
    def send_action(self, action: str, params: dict[str, Any] | None = None) -> str:
        """Send one OneBot action; returns the echo id used for correlation."""
        ws = self._ws
        if ws is None or not self.connected:
            raise ConnectionError("OneBot client is not connected")
        echo = f"fatfish-{uuid.uuid4().hex[:12]}"
        payload = {
            "action": str(action),
            "params": dict(params or {}),
            "echo": echo,
        }
        try:
            ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            self._ws = None
            raise ConnectionError(
                f"failed to send OneBot action {action}: {type(exc).__name__}: {exc}"
            ) from exc
        return echo

    def send_private_text(self, user_id: str | int, text: str) -> str:
        return self.send_action(
            "send_private_msg",
            {"user_id": _as_id(user_id), "message": str(text)},
        )

    def send_group_text(self, group_id: str | int, text: str) -> str:
        return self.send_action(
            "send_group_msg",
            {"group_id": _as_id(group_id), "message": str(text)},
        )

    def send_private_message(self, user_id: str | int, message: list[dict[str, Any]]) -> str:
        """Send a OneBot v11 message-segment list to a private chat."""
        return self.send_action(
            "send_private_msg",
            {"user_id": _as_id(user_id), "message": _segments(message)},
        )

    def send_group_message(self, group_id: str | int, message: list[dict[str, Any]]) -> str:
        """Send a OneBot v11 message-segment list to a group chat."""
        return self.send_action(
            "send_group_msg",
            {"group_id": _as_id(group_id), "message": _segments(message)},
        )

    # ------------------------------------------------------- request/response
    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Send one action and wait for its response (matched by echo).

        The forward WebSocket carries both API responses and pushed events on
        the same socket. While polling for the response, any pushed event
        (``post_type`` present) is handed to ``on_event`` instead of being
        silently drained and lost; anything else is ignored. Raises
        ``TimeoutError`` when no matching response arrives in time.
        """
        echo = self.send_action(action, params)
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"OneBot action {action} timed out after {timeout}s")
            event = self.recv(timeout=remaining)
            if event is None:
                continue
            if str(event.get("echo") or "") == echo:
                return event
            if on_event is not None and event.get("post_type"):
                on_event(event)

    def get_friend_msg_history(
        self,
        user_id: str | int,
        count: int = 20,
        *,
        message_seq: int = 0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """NapCat extension: the latest private-chat history with one friend.

        Verified against ``vendor/napcat/NapCat.Shell/napcat.mjs``
        (``GetFriendMsgHistory: "get_friend_msg_history"``, payload
        ``{user_id, message_seq?, count=20, reverse_order?}``, response
        ``{messages: []}``). ``message_seq=0`` reads the newest page. Raises
        on a failed response; callers treat that as "enhancement unavailable"
        and fall back to the local turn stream. ``on_event`` re-dispatches
        pushed events that arrive while the response is being polled (see
        :meth:`call_action`), so the fetch cannot drain live messages.
        """
        response = self.call_action(
            "get_friend_msg_history",
            {
                "user_id": _as_id(user_id),
                "message_seq": str(int(message_seq)),
                "count": max(1, int(count)),
            },
            on_event=on_event,
        )
        status = str(response.get("status") or "").strip().lower()
        retcode = response.get("retcode")
        ok = status in {"ok", "async"} or retcode in (0, "0")
        if not ok:
            detail = str(response.get("wording") or response.get("message") or "")[:120]
            raise RuntimeError(
                f"get_friend_msg_history failed: status={status!r} retcode={retcode!r} {detail}"
            )
        data = response.get("data")
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return []
        return [row for row in messages if isinstance(row, dict)]

    def get_group_msg_history(
        self,
        group_id: str | int,
        count: int = 20,
        message_seq: int = 0,
        *,
        reverse_order: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """NapCat extension: one page of a group's chat history.

        Verified against ``vendor/napcat/NapCat.Shell/napcat.mjs``
        (``GoCQHTTP_GetGroupMsgHistory: "get_group_msg_history"``, payload
        ``{group_id, message_seq?, count=20, reverse_order?}``, response
        ``{messages: []}``). ``message_seq=0`` reads the newest page; an
        unknown ``message_seq`` is a failed response (NapCat throws
        「消息…不存在」). Raises on a failed response; callers treat that as
        "enhancement unavailable" and degrade gracefully. ``on_event``
        re-dispatches pushed events that arrive while the response is being
        polled (see :meth:`call_action`), so the fetch cannot drain live
        messages.
        """
        response = self.call_action(
            "get_group_msg_history",
            {
                "group_id": _as_id(group_id),
                "message_seq": str(int(message_seq)),
                "count": max(1, int(count)),
                "reverse_order": bool(reverse_order),
            },
            on_event=on_event,
        )
        _require_action_ok("get_group_msg_history", response)
        return _response_messages(response)

    def get_forward_msg(
        self,
        message_id: str | int,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """NapCat/go-cqhttp extension: one merged-forward message's content.

        Verified against ``vendor/napcat/NapCat.Shell/napcat.mjs``
        (``GoCQHTTP_GetForwardMsg: "get_forward_msg"``, payload
        ``{message_id}``, response ``{messages: []}``; ``messages`` is
        optional and reads as empty when absent). ``message_id`` accepts
        either the wrapper message's numeric id or the ``resid`` of its
        ``com.tencent.multimsg`` json payload (see
        :func:`fatfish.platforms.qq.extract_forward_ref`). Raises on a
        failed response; ``on_event`` re-dispatches pushed events (see
        :meth:`call_action`).
        """
        response = self.call_action(
            "get_forward_msg",
            {"message_id": _as_id(message_id)},
            on_event=on_event,
        )
        _require_action_ok("get_forward_msg", response)
        return _response_messages(response)

    def get_group_member_list(self, group_id: str | int) -> list[dict[str, Any]]:
        """OneBot v11 group member list, normalized to the fields we need.

        Verified against ``vendor/napcat/NapCat.Shell/napcat.mjs``
        (``GetGroupMemberList: "get_group_member_list"``, payload
        ``{group_id}``, response: an array of ``{group_id, user_id,
        nickname, card, role}``). Returns ``[{"user_id", "nickname",
        "card"}]`` rows only; junk rows are dropped. Raises on a failed
        response.
        """
        response = self.call_action(
            "get_group_member_list",
            {"group_id": _as_id(group_id)},
        )
        _require_action_ok("get_group_member_list", response)
        data = response.get("data")
        rows = data if isinstance(data, list) else []
        members: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            members.append(
                {
                    "user_id": _as_id(row.get("user_id")),
                    "nickname": str(row.get("nickname") or ""),
                    "card": str(row.get("card") or ""),
                }
            )
        return members

    # ------------------------------------------------------------------ recv
    def recv(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Receive one JSON object; ``None`` on timeout or non-JSON payload."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("OneBot client is not connected")
        if timeout is not None:
            try:
                ws.settimeout(max(0.0, float(timeout)))
            except Exception:  # pragma: no cover - fake sockets in tests
                pass
        try:
            raw = ws.recv()
        except Exception as exc:
            if _is_timeout(exc):
                return None
            self._ws = None
            raise ConnectionError(
                f"OneBot connection closed: {type(exc).__name__}: {exc}"
            ) from exc
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return event if isinstance(event, dict) else None


def _is_timeout(exc: BaseException) -> bool:
    timeout_types: tuple[type[BaseException], ...] = (TimeoutError,)
    if websocket is not None:
        custom = getattr(websocket, "WebSocketTimeoutException", None)
        if isinstance(custom, type) and custom is not TimeoutError:
            timeout_types = timeout_types + (custom,)
    return isinstance(exc, timeout_types)
