"""Wakeup scheduling and proactive-message gating."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..clock import in_quiet_hours, next_quiet_end, parse_when
from ..config import Config
from ..db import Database


# T-0027 v2: the auto-continuation intent. Every finished assistant turn in a
# deliverable QQ private session schedules one of these (+60s); the user
# replying first cancels it (cancel-on-reply). Counted and chain-capped
# separately from ordinary wakeups so a chatty day cannot starve rituals and
# owner shares of scheduling slots.
SELF_CONTINUE_INTENT = "self_continue"


@dataclass(slots=True)
class Wakeup:
    id: str
    session_id: str | None
    created_at: float
    fire_at: float
    expires_at: float | None
    status: str
    intent: str
    reason: str
    source_turn_id: str | None
    payload: dict[str, Any]
    attempts: int
    chain_depth: int


class WakeupScheduler:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.tz_name = config.timezone_name

    def schedule(
        self,
        when: str | float,
        *,
        intent: str = "think",
        reason: str = "",
        session_id: str | None = None,
        source_turn_id: str | None = None,
        payload: dict[str, Any] | None = None,
        urgent: bool = False,
        chain_depth: int = 0,
        now: float | None = None,
    ) -> Wakeup:
        now = time.time() if now is None else now
        if intent == SELF_CONTINUE_INTENT:
            # T-0027 v2 item 4: self_continue chains cap at a tighter depth
            # than ordinary wakeups (auto=0, one explicit re-hang allowed,
            # hard top at ``wakeup.self_continue_max_chain_depth``).
            max_self_depth = int(
                self.config.get("wakeup.self_continue_max_chain_depth", 2)
            )
            if int(chain_depth) > max_self_depth:
                raise ValueError("self_continue chain depth exceeded")
        elif chain_depth > int(self.config.get("wakeup.max_chain_depth", 3)):
            raise ValueError("wakeup chain depth exceeded")
        fire_at = parse_when(str(when), self.tz_name, now=now) if isinstance(when, str) else float(when)
        min_delay = float(self.config.get("wakeup.min_delay_seconds", 60))
        max_delay = float(self.config.get("wakeup.max_delay_days", 7)) * 86400
        if fire_at - now < min_delay:
            fire_at = now + min_delay
        if fire_at - now > max_delay:
            raise ValueError("wakeup too far in the future")

        quiet_start = self.config.get("memory.quiet_start", "23:30")
        quiet_end = self.config.get("memory.quiet_end", "08:00")
        if not urgent and in_quiet_hours(fire_at, quiet_start, quiet_end, self.tz_name):
            fire_at = next_quiet_end(fire_at, quiet_end, self.tz_name)

        if not urgent and not self._under_daily_limit(now, intent=intent):
            raise ValueError("wakeup daily limit reached")

        wakeup_id = uuid.uuid4().hex
        expires_at = fire_at + 6 * 3600
        self.db.execute(
            "INSERT INTO wakeups(id, session_id, created_at, fire_at, expires_at, status, intent, reason, "
            "source_turn_id, payload_json, attempts, chain_depth) "
            "VALUES (?,?,?,?,?, 'pending', ?,?,?,?, 0, ?)",
            (
                wakeup_id, session_id, now, fire_at, expires_at, intent, reason[:500],
                source_turn_id, json.dumps(payload or {}, ensure_ascii=False), int(chain_depth),
            ),
        )
        self._metric("wakeup_scheduled", 1.0, {"intent": intent})
        return self.get(wakeup_id)  # type: ignore[return-value]

    def get(self, wakeup_id: str) -> Wakeup | None:
        row = self.db.query_one("SELECT * FROM wakeups WHERE id=?", (wakeup_id,))
        return self._row_to_wakeup(row) if row else None

    def list(self, status: str | None = None, limit: int = 20) -> list[Wakeup]:
        if status:
            rows = self.db.query(
                "SELECT * FROM wakeups WHERE status=? ORDER BY fire_at LIMIT ?",
                (status, int(limit)),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM wakeups ORDER BY fire_at DESC LIMIT ?", (int(limit),)
            )
        return [self._row_to_wakeup(row) for row in rows]

    def due(self, *, now: float | None = None, limit: int = 10) -> list[Wakeup]:
        now = time.time() if now is None else now
        rows = self.db.query(
            "SELECT * FROM wakeups WHERE status='pending' AND fire_at<=? ORDER BY fire_at LIMIT ?",
            (now, int(limit)),
        )
        return [self._row_to_wakeup(row) for row in rows]

    def fire(self, wakeup_id: str, *, now: float | None = None) -> Wakeup | None:
        now = time.time() if now is None else now
        self.db.execute(
            "UPDATE wakeups SET status='fired', attempts=attempts+1 WHERE id=? AND status='pending'",
            (wakeup_id,),
        )
        self._metric("wakeup_fired", 1.0, {"id": wakeup_id[:8]})
        return self.get(wakeup_id)

    def complete(self, wakeup_id: str, status: str = "done") -> None:
        if status not in {"done", "cancelled", "expired", "failed"}:
            raise ValueError(f"invalid completion status: {status}")
        self.db.execute("UPDATE wakeups SET status=? WHERE id=?", (status, wakeup_id))

    def cancel_pending(self, session_id: str, *, intent: str | None = None) -> int:
        if intent:
            cur = self.db.execute(
                "UPDATE wakeups SET status='cancelled' WHERE session_id=? AND status='pending' AND intent=?",
                (session_id, intent),
            )
        else:
            cur = self.db.execute(
                "UPDATE wakeups SET status='cancelled' WHERE session_id=? AND status='pending'",
                (session_id,),
            )
        return cur.rowcount

    def stats(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM wakeups GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    def _under_daily_limit(self, now: float, *, intent: str | None = None) -> bool:
        # self_continue (T-0027 v2) has its own budget: one row per finished
        # turn would burn the general wakeup.max_per_day in a single morning
        # and starve rituals / owner shares of slots. Cancelled self_continue
        # rows already drop out of both counts (status!='cancelled').
        #
        # 2026-11-15 boss mandate: ``..._max_per_day = 0`` means unlimited.
        # Only 0 changes: a positive cap keeps the exact old "< cap" rule.
        if intent == SELF_CONTINUE_INTENT:
            max_per_day = int(self.config.get("wakeup.self_continue_max_per_day", 60))
            if max_per_day == 0:
                return True
            row = self.db.query_one(
                "SELECT COUNT(*) AS n FROM wakeups WHERE created_at>=? AND created_at<? "
                "AND status!='cancelled' AND intent=?",
                (now - 86400, now + 1, SELF_CONTINUE_INTENT),
            )
            return bool(row and int(row["n"]) < max_per_day)
        max_per_day = int(self.config.get("wakeup.max_per_day", 5))
        if max_per_day == 0:
            return True
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM wakeups WHERE created_at>=? AND created_at<? "
            "AND status!='cancelled' AND intent<>'self_continue'",
            (now - 86400, now + 1),
        )
        return bool(row and int(row["n"]) < max_per_day)

    def _metric(self, key: str, value: float, tags: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (time.time(), key, value, json.dumps(tags, ensure_ascii=False)),
        )

    @staticmethod
    def _row_to_wakeup(row: Any) -> Wakeup:
        return Wakeup(
            id=row["id"],
            session_id=row["session_id"],
            created_at=float(row["created_at"]),
            fire_at=float(row["fire_at"]),
            expires_at=row["expires_at"],
            status=row["status"],
            intent=row["intent"],
            reason=row["reason"],
            source_turn_id=row["source_turn_id"],
            payload=json.loads(row["payload_json"] or "{}"),
            attempts=int(row["attempts"]),
            chain_depth=int(row["chain_depth"]),
        )


class ProactiveGate:
    """Cheap first gate before spending generation tokens on a wakeup."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.tz_name = config.timezone_name

    def should_act(self, wakeup: Wakeup, *, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        if wakeup.fire_at > now + 60:
            return False, "not_due"
        if wakeup.expires_at and now > float(wakeup.expires_at):
            return False, "expired"
        quiet_start = self.config.get("memory.quiet_start", "23:30")
        quiet_end = self.config.get("memory.quiet_end", "08:00")
        if in_quiet_hours(now, quiet_start, quiet_end, self.tz_name):
            return False, "quiet_hours"
        last = self.db.query_one(
            "SELECT at FROM decision_log WHERE kind='proactive_message' AND action='sent' ORDER BY at DESC LIMIT 1"
        )
        min_interval = float(self.config.get("wakeup.proactive_min_interval_minutes", 90)) * 60
        if last and now - float(last["at"]) < min_interval:
            return False, "too_soon"
        today_count = self.db.query_one(
            "SELECT COUNT(*) AS n FROM decision_log WHERE kind='proactive_message' "
            "AND action='sent' AND at>=?",
            (now - 86400,),
        )
        max_per_day = int(self.config.get("wakeup.max_per_day", 5))
        # 0 = unlimited (2026-11-15); a positive cap keeps the exact old rule.
        if max_per_day > 0 and today_count and int(today_count["n"]) >= max_per_day:
            return False, "daily_limit"
        return True, "ok"
