"""Pure formatting for QQ history reads (T-0071).

``fetch_*`` on the QQ adapter return raw OneBot message-event rows; this
module turns them into the plain ``{"sender_id", "sender_name", "text",
"time"}`` dicts a prompt builder (T-0072 ``read_chat``) can consume. It is
deliberately dependency-free (stdlib only, no fatfish imports) so both the
platform layer and later consumers can use it without import cycles.

Rules (per task card):

* Skip the bot's own messages (``sender``/``user_id`` equals ``self_id``).
* Skip rows without extractable text.
* Only text segments count — image/record/video/file/json/xml/face and any
  other segment type are ignored, never turned into placeholders here.
* Each kept row's text is truncated to ``max_chars`` (codebase-wide
  ``…``-ellipsis style) and at most ``max_rows`` rows are returned, in the
  order the caller provided (no sorting — NapCat's page order is preserved).
"""

from __future__ import annotations

from typing import Any

TEXT_SEGMENT_TYPE = "text"

DEFAULT_MAX_CHARS = 200
DEFAULT_MAX_ROWS = 50


def extract_history_rows(
    rows: Any,
    self_id: Any,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Format raw history rows into plain prompt-ready dicts.

    ``rows`` is any iterable of OneBot message-event dicts (as returned by
    ``get_group_msg_history`` / ``get_friend_msg_history``); junk items are
    ignored. ``self_id`` is the bot's own uin (rows sent by it are skipped;
    ``None``/``0`` keeps everything). Never raises on malformed rows.
    """
    limit = max(1, int(max_chars))
    cap = int(max_rows)
    if cap <= 0:
        return []
    self_key = str(self_id).strip() if self_id is not None else ""
    formatted: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sender_id = _sender_id(row)
        if self_key and sender_id == self_key:
            continue
        text = _row_text(row).strip()
        if not text:
            continue
        formatted.append(
            {
                "sender_id": sender_id,
                "sender_name": _sender_name(row),
                "text": _truncate(text, limit),
                "time": _row_time(row),
            }
        )
        if len(formatted) >= cap:
            break
    return formatted


def _sender_id(row: dict[str, Any]) -> str:
    """``sender.user_id`` when present, else the top-level ``user_id``."""
    sender = row.get("sender")
    if isinstance(sender, dict):
        value = _clean_id(sender.get("user_id"))
        if value:
            return value
    return _clean_id(row.get("user_id"))


def _sender_name(row: dict[str, Any]) -> str:
    """Group card first, then nickname — same precedence as the live path."""
    sender = row.get("sender")
    if isinstance(sender, dict):
        for key in ("card", "nickname"):
            value = " ".join(str(sender.get(key) or "").split())
            if value:
                return value[:64]
    return " ".join(str(row.get("nickname") or "").split())[:64]


def _row_text(row: dict[str, Any]) -> str:
    """Concatenated text of the row's text segments; everything else ignored."""
    segments = row.get("message")
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            parts.append(segment)
            continue
        if not isinstance(segment, dict):
            continue
        if str(segment.get("type") or "") != TEXT_SEGMENT_TYPE:
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            data = {}
        parts.append(str(data.get("text") or ""))
    return "".join(parts)


def _row_time(row: dict[str, Any]) -> float:
    try:
        return float(row.get("time") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _clean_id(value: Any) -> str:
    """Stringify an id, dropping a float-ish ``.0`` tail (NapCat quirk)."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _truncate(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"
