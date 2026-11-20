"""Explicit agent state machine.

The point is to make wakeups, user messages, browsing and generation
mutually understandable instead of racing in separate threads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database

IDLE = "idle"
CHATTING = "chatting"
BROWSING = "browsing"
GENERATING = "generating"
SLEEPING = "sleeping"
BUSY = "busy"

ALL_STATES = {IDLE, CHATTING, BROWSING, GENERATING, SLEEPING, BUSY}

_ALLOWED: dict[str, set[str]] = {
    IDLE: {CHATTING, BROWSING, GENERATING, SLEEPING, BUSY},
    CHATTING: {IDLE, BUSY, SLEEPING, GENERATING},
    BROWSING: {IDLE, CHATTING, BUSY, SLEEPING},
    GENERATING: {IDLE, CHATTING, BUSY},
    SLEEPING: {IDLE, CHATTING, BUSY},
    BUSY: {IDLE, CHATTING, SLEEPING},
}


@dataclass(slots=True)
class StateSnapshot:
    state: str
    since: float
    reason: str


class StateMachine:
    def __init__(self, db: Database):
        self.db = db

    def current(self) -> StateSnapshot:
        row = self.db.query_one("SELECT state, since, reason FROM agent_state WHERE id=1")
        if not row:
            now = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO agent_state(id, state, since, reason) VALUES (1, ?, ?, ?)",
                (IDLE, now, "init"),
            )
            return StateSnapshot(IDLE, now, "init")
        return StateSnapshot(row["state"], float(row["since"]), row["reason"] or "")

    def can_transition(self, target: str) -> bool:
        if target not in ALL_STATES:
            return False
        current = self.current().state
        return target == current or target in _ALLOWED.get(current, set())

    def transition(self, target: str, reason: str = "", *, force: bool = False) -> StateSnapshot:
        if target not in ALL_STATES:
            raise ValueError(f"unknown state: {target}")
        current = self.current()
        if not force and target != current.state and target not in _ALLOWED.get(current.state, set()):
            raise ValueError(f"illegal transition {current.state} -> {target} ({reason})")
        now = time.time()
        self.db.execute(
            "UPDATE agent_state SET state=?, since=?, reason=? WHERE id=1",
            (target, now, reason[:500]),
        )
        return StateSnapshot(target, now, reason)

    def can_accept_user_message(self) -> bool:
        return self.current().state != SLEEPING

    def can_run_wakeup(self) -> bool:
        return self.current().state in {IDLE, SLEEPING}

    def is_busy(self) -> bool:
        return self.current().state in {BUSY, GENERATING, CHATTING}

    def snapshot(self) -> dict[str, object]:
        cur = self.current()
        return {"state": cur.state, "since": cur.since, "reason": cur.reason}
