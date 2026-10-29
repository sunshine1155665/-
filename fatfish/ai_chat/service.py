"""大肥鱼间聊天 (T-0047): state persistence, audit, and outbox hand-off.

This is the L3 layer: the only place that writes desire-threshold state and the
only place that puts an ``ai_chat_message`` into the outbox.  It deliberately
owns **no arithmetic** -- decay / lift / gate all come from
:mod:`fatfish.ai_chat.engine`, so the numbers cannot drift between layers.

Rules this module enforces (and nothing above it may re-implement):

* **One session only.**  :meth:`AiChatService.is_ai_chat_session` is the single
  entry point: exactly ``qq:private:<peer_uin>`` while ``[ai_chat]`` is active.
  With ``enabled=false`` or an empty ``peer_uin`` it is ``False`` for everything,
  so no path can send to a guessed recipient.
* **One writer of state.**  :meth:`record_sent` is the only method that lifts
  the threshold, and it lifts only for ``direction="out"``.  Inbound messages
  are recorded but never counted as "we spoke".
* **Idempotent by construction.**  ``ai_chat_messages`` has no UNIQUE
  constraint, so repeats are filtered in code inside one ``db.transaction()``
  (``SELECT`` then ``INSERT``).  Re-delivering the same outbox item or
  re-recording the same turn must not lift the threshold twice.
* **50 messages per hour is a decay baseline, not a limit.**  ``sent_last_hour``
  exists for audit and for the optional ``hard_cap_per_hour`` stop-the-bleeding
  switch.  There is no ``>= 50 -> refuse`` branch anywhere in this file.

Audit rows go to ``decision_log(kind='ai_chat')`` with metadata only: no message
body, no cookies, no secrets, every free-text field clipped to 200 chars.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any

from ..outbox import Outbox
from . import engine
from .settings import AiChatSettings

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_KIND",
    "OUTBOX_KIND",
    "REASON_PASSIVE",
    "STATE_KEY_PREFIX",
    "AiChatService",
]

#: Outbox kind handled by ``fatfish/platforms/qq.py::send_outbox_item``.
OUTBOX_KIND = "ai_chat_message"
#: ``decision_log.kind`` for every gate/audit row this feature writes.
AUDIT_KIND = "ai_chat"
#: Gate reason used when a passive reply skipped the threshold check.
REASON_PASSIVE = "passive"
#: ``schema_meta`` key prefix; the full key is ``ai_chat:state:<peer_uin>``.
STATE_KEY_PREFIX = "ai_chat:state:"

#: Free-text cap for audit fields -- the ledger keeps metadata, not transcripts.
AUDIT_TEXT_LIMIT = 200
HOUR_SECONDS = 3600.0


def _clip(value: Any, limit: int = AUDIT_TEXT_LIMIT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit]


def _number(value: Any) -> float | None:
    """Finite float or ``None``; ``bool`` is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class AiChatService:
    """State + audit + delivery for the one bound peer (see module docstring)."""

    def __init__(
        self,
        db: Any,
        settings: AiChatSettings,
        *,
        now_fn: Any = time.time,
        outbox: Outbox | None = None,
    ):
        self.db = db
        self.settings = settings
        self._now_fn = now_fn or time.time
        self.outbox = outbox if outbox is not None else Outbox(db)

    # ------------------------------------------------------------------ basics
    def _now(self, now: Any = None) -> float:
        """The caller's clock when given (even if odd), else ``now_fn()``."""
        return float(self._now_fn()) if now is None else now

    @property
    def peer_uin(self) -> str:
        return str(self.settings.peer_uin or "").strip()

    @property
    def session_id(self) -> str:
        """The one session this feature may ever speak in (``""`` if unbound)."""
        uin = self.peer_uin
        return f"qq:private:{uin}" if uin else ""

    @property
    def state_key(self) -> str:
        return f"{STATE_KEY_PREFIX}{self.peer_uin}"

    def is_ai_chat_session(self, session_id: Any) -> bool:
        """Is ``session_id`` the bound peer's private chat while active?

        The single judge for the whole project: groups, other private chats,
        web sessions, ``None`` and anything not exactly
        ``qq:private:<peer_uin>`` are all ``False``.  No keyword matching, no
        recipient guessing.
        """
        if not self.settings.active:
            return False
        target = self.session_id
        if not target or not isinstance(session_id, str):
            return False
        return session_id.strip() == target

    # ------------------------------------------------------------------- state
    def load_state(self) -> engine.AiChatState:
        """Read the persisted state; never raises.

        A missing key means "never spoke" (``initial_threshold``,
        ``updated_at=None``).  A corrupt or incomplete JSON value falls back to
        the same shape with a warning instead of an exception: one bad meta row
        must not take the turn down.
        """
        try:
            raw = self.db.get_meta(self.state_key, None)
        except Exception as exc:  # pragma: no cover - db errors are not expected
            logger.warning("ai_chat 状态读取失败（%s），按初始阈值处理", exc)
            return engine.read_state(None, self.settings)
        if raw is None or raw == "":
            return engine.read_state(None, self.settings)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("ai_chat 状态 JSON 损坏，回落到 initial_threshold")
            return engine.read_state(None, self.settings)
        if not isinstance(data, dict):
            logger.warning("ai_chat 状态不是 JSON 对象，回落到 initial_threshold")
            return engine.read_state(None, self.settings)
        if _number(data.get("threshold")) is None:
            logger.warning("ai_chat 状态缺 threshold，回落到 initial_threshold")
            return engine.read_state(None, self.settings)
        return engine.read_state(data, self.settings)

    def save_state(self, state: Any) -> None:
        """Persist ``{"threshold", "updated_at", "last_desire"}`` as JSON."""
        normalized = engine.read_state(state, self.settings)
        self.db.set_meta(
            self.state_key,
            json.dumps(normalized.to_dict(), ensure_ascii=False, sort_keys=True),
        )

    def current_threshold(self, now: Any = None) -> float:
        """Threshold right now (decayed, clamped) -- engine math, our IO."""
        return engine.current_threshold(self.settings, self.load_state(), self._now(now))

    # -------------------------------------------------------------------- gate
    def sent_last_hour(self, now: Any = None) -> int:
        """Outgoing messages in the last rolling hour.

        Audit + optional hard cap only.  ``decay_messages_per_hour=50`` is a
        decay baseline, so this number never gates the sending on its own.
        """
        uin = self.peer_uin
        if not uin:
            return 0
        moment = _number(self._now(now))
        if moment is None:
            return 0
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM ai_chat_messages "
            "WHERE peer_uin=? AND direction='out' AND created_at>?",
            (uin, moment - HOUR_SECONDS),
        )
        return int(row["n"]) if row else 0

    def can_send(
        self, desire: Any, *, now: Any = None, passive: bool = False
    ) -> tuple[bool, str, dict[str, Any]]:
        """``(allowed, reason, meta)`` for one prospective message.

        ``passive=True`` (answering something the peer just said) bypasses the
        threshold entirely and reports ``"passive"`` -- but the reply still gets
        recorded and still lifts the threshold via :meth:`record_sent`.

        The proactive path counts the last hour and then defers to
        :func:`engine.can_proactive_send`, which never refuses on a message
        count unless ``hard_cap_per_hour`` was explicitly turned on.
        """
        moment = self._now(now)
        state = self.load_state()
        threshold = engine.current_threshold(self.settings, state, moment)
        sent = self.sent_last_hour(moment)
        meta: dict[str, Any] = {
            "threshold": threshold,
            "desire": engine.normalize_desire(desire),
            "sent_last_hour": sent,
            "decay_per_minute": self.settings.threshold_decay_per_minute,
        }
        if passive:
            return True, REASON_PASSIVE, meta
        allowed, reason = engine.can_proactive_send(
            self.settings, state, desire, moment, sent
        )
        return allowed, reason, meta

    # ------------------------------------------------------------------ ledger
    def record_sent(
        self,
        content: Any,
        *,
        desire: Any,
        threshold: Any,
        direction: str = "out",
        turn_id: Any = None,
        now: Any = None,
    ) -> int:
        """Record one message; the only method that lifts the threshold.

        ``direction="out"`` appends an ``ai_chat_messages`` row and applies
        :func:`engine.on_sent` (decay, then ``+threshold_step``, clamped) before
        saving the state.  ``direction="in"`` only appends the row.

        Idempotency: the same ``(peer_uin, direction, turn_id)`` returns the
        existing row id and does **not** lift the threshold a second time.
        ``turn_id`` is optional -- without it there is nothing to deduplicate
        against, so every call appends.
        """
        if direction not in {"in", "out"}:
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        uin = self.peer_uin
        if not uin:
            # Unbound means disabled: never write, never lift.
            raise ValueError("ai_chat peer_uin 为空，拒绝记账")
        moment = _now_value(self._now(now))
        body = "" if content is None else str(content)
        key_turn = _int_or_none(turn_id)
        desire_value = _number(desire)
        threshold_value = _number(threshold)

        with self.db.transaction() as conn:
            if key_turn is not None:
                row = conn.execute(
                    "SELECT id FROM ai_chat_messages "
                    "WHERE peer_uin=? AND direction=? AND turn_id=? ORDER BY id LIMIT 1",
                    (uin, direction, key_turn),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
            cursor = conn.execute(
                "INSERT INTO ai_chat_messages("
                "peer_uin, direction, content, desire, threshold, turn_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uin, direction, body, desire_value, threshold_value, key_turn, moment),
            )
            message_id = int(cursor.lastrowid)
            if direction == "out":
                lifted = engine.on_sent(self.settings, self.load_state(), desire, moment)
                self.save_state(lifted)
        return message_id

    # ------------------------------------------------------------------- audit
    def audit(
        self,
        reason: Any,
        *,
        desire: Any = None,
        threshold: Any = None,
        outcome: Any = "",
        session_id: Any = None,
        now: Any = None,
    ) -> None:
        """Write one ``decision_log(kind='ai_chat')`` row.  Metadata only.

        Never raises: an audit failure must not break the turn it audits.
        Free text (``reason`` / ``outcome``) is clipped to 200 chars so a model
        answer cannot smuggle a transcript into the ledger.
        """
        moment = _now_value(self._now(now))
        action = _clip(reason)
        detail = _clip(outcome)
        payload = {
            "peer_uin": self.peer_uin,
            "reason": action,
            "desire": _number(desire),
            "threshold": _number(threshold),
            "sent_last_hour": self.sent_last_hour(moment),
            "detail": detail,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        try:
            self.db.execute(
                "INSERT INTO decision_log("
                "at, kind, input_hash, output_json, action, outcome, latency_ms, model, session_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    moment,
                    AUDIT_KIND,
                    digest,
                    blob[:8000],
                    action,
                    detail,
                    0.0,  # latency_ms is unused here but NOT NULL-friendly
                    "ai_chat",
                    None if session_id is None else str(session_id),
                ),
            )
        except Exception as exc:  # audit must never break the turn it audits
            logger.warning("ai_chat 审计写入失败：%s", exc)

    # ------------------------------------------------------------------ outbox
    def idempotency_key(
        self, text: Any, *, turn_id: Any = None, media: Any = None, session_id: Any = None
    ) -> str:
        """Stable outbox key: ``ai_chat:<uin>:<session>:turn:<id>`` or ``:msg:<hash>``.

        A turn id wins when present (one message per turn); otherwise the key is
        a hash of the body plus media, so re-enqueuing identical text collapses
        to one row while a genuinely new message still gets its own.
        """
        target = self.session_id if session_id is None else str(session_id)
        prefix = f"ai_chat:{self.peer_uin}:{target}"
        turn = _int_or_none(turn_id)
        if turn is not None:
            return f"{prefix}:turn:{turn}"
        material = json.dumps(
            {"text": "" if text is None else str(text), "media": media or []},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:msg:{digest}"

    def enqueue(
        self,
        text: Any,
        *,
        media: Any = None,
        desire: Any,
        threshold: Any,
        turn_id: Any = None,
        session_id: Any = None,
        now: Any = None,
    ) -> dict[str, Any]:
        """Queue one ``ai_chat_message`` for the bound peer.

        Refuses before touching the outbox when the feature is off or the
        session is not the bound one.  The threshold is **not** changed here:
        it lifts in :meth:`record_sent` after delivery succeeds, so a failed
        send costs nothing.
        """
        target = self.session_id if session_id is None else str(session_id)
        if not self.settings.active or not self.is_ai_chat_session(target):
            return {
                "ok": False,
                "error": "ai_chat_disabled",
                "reason": self.settings.disabled_reason,
                "session_id": target,
            }
        body = "" if text is None else str(text)
        items = (
            [item for item in media if isinstance(item, dict)]
            if isinstance(media, list)
            else []
        )
        if not body.strip() and not items:
            return {"ok": False, "error": "empty_message", "session_id": target}

        key = self.idempotency_key(body, turn_id=turn_id, media=items, session_id=target)
        payload = {
            "peer_uin": self.peer_uin,
            "text": body,
            "media": items,
            "session_id": target,
            "desire": _number(desire),
            "threshold": _number(threshold),
            # Carried so the delivery callback can dedupe record_sent().
            "turn_id": _int_or_none(turn_id),
        }
        item_id, created = self.outbox.enqueue(
            OUTBOX_KIND,
            payload,
            idempotency_key=key,
            available_at=_now_value(self._now(now)),
        )
        return {
            "ok": True,
            "id": item_id,
            "created": bool(created),
            "kind": OUTBOX_KIND,
            "idempotency_key": key,
            "session_id": target,
        }


def _now_value(value: Any) -> float:
    """Coerce a clock value for storage; garbage degrades to wall clock."""
    number = _number(value)
    return time.time() if number is None else number


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)
