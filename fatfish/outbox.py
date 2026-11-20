"""SQLite-backed outbox with idempotency keys."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database


@dataclass(slots=True)
class OutboxItem:
    id: int
    idempotency_key: str
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: float
    created_at: float
    lease_until: float | None = None
    max_attempts: int = 5


class Outbox:
    def __init__(self, db: Database, *, lease_seconds: float = 60.0, max_attempts: int = 5):
        self.db = db
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        available_at: float | None = None,
        max_attempts: int | None = None,
    ) -> tuple[int, bool]:
        """Return (id, created). Duplicate keys return the existing row.

        ``max_attempts`` overrides the instance default for this one item
        (e.g. the qzone publish path allows exactly 3 retries); ``None`` keeps
        the previous behaviour unchanged.
        """
        key = idempotency_key or f"{kind}:{uuid.uuid4().hex}"
        now = time.time()
        available = now if available_at is None else float(available_at)
        attempts_limit = self.max_attempts if max_attempts is None else max(1, int(max_attempts))
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM outbox WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                return int(row["id"]), False
            cur = conn.execute(
                "INSERT INTO outbox("
                "idempotency_key, kind, payload_json, status, created_at, available_at, max_attempts"
                ") VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (
                    key,
                    kind,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    available,
                    attempts_limit,
                ),
            )
            return int(cur.lastrowid), True

    def claim(self, limit: int = 10, *, now: float | None = None) -> list[OutboxItem]:
        now = time.time() if now is None else now
        claimed: list[OutboxItem] = []
        lease_until = now + self.lease_seconds
        with self.db.transaction() as conn:
            # Recover rows whose worker died while holding a lease.
            conn.execute(
                "UPDATE outbox SET status='pending', lease_until=NULL "
                "WHERE status='processing' AND (lease_until IS NULL OR lease_until < ?)",
                (now,),
            )
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status='pending' AND available_at<=? "
                "AND attempts < max_attempts ORDER BY id LIMIT ?",
                (now, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                updated = conn.execute(
                    "UPDATE outbox SET status='processing', attempts=attempts+1, lease_until=? "
                    "WHERE id=? AND status='pending'",
                    (lease_until, row["id"]),
                )
                if updated.rowcount:
                    claimed.append(
                        OutboxItem(
                            id=int(row["id"]),
                            idempotency_key=row["idempotency_key"],
                            kind=row["kind"],
                            payload=json.loads(row["payload_json"] or "{}"),
                            status="processing",
                            attempts=int(row["attempts"]) + 1,
                            available_at=float(row["available_at"]),
                            created_at=float(row["created_at"]),
                            lease_until=lease_until,
                            max_attempts=int(row["max_attempts"] or self.max_attempts),
                        )
                    )
        return claimed

    def mark_done(self, item_id: int) -> None:
        self.db.execute(
            "UPDATE outbox SET status='done', processed_at=?, lease_until=NULL, last_error=NULL WHERE id=?",
            (time.time(), item_id),
        )

    def mark_failed(self, item_id: int, error: str, *, retry_after: float = 30.0) -> None:
        now = time.time()
        row = self.db.query_one(
            "SELECT attempts, max_attempts FROM outbox WHERE id=?", (item_id,)
        )
        attempts = int(row["attempts"] or 0) if row else 0
        max_attempts = int(row["max_attempts"] or self.max_attempts) if row else self.max_attempts
        if attempts >= max_attempts:
            self.db.execute(
                "UPDATE outbox SET status='dead', processed_at=?, lease_until=NULL, last_error=? WHERE id=?",
                (now, str(error)[:2000], item_id),
            )
            return
        self.db.execute(
            "UPDATE outbox SET status='pending', available_at=?, lease_until=NULL, last_error=? WHERE id=?",
            (now + max(1.0, retry_after), str(error)[:2000], item_id),
        )

    def defer(self, item_id: int, seconds: float = 30.0) -> None:
        """Release a claimed item without counting the claim as an attempt.

        Used when no handler is available yet (for example a QQ message while
        the web UI is the only thing ticking): the item stays pending and is
        retried later instead of being marked failed or dead.
        """
        now = time.time()
        self.db.execute(
            "UPDATE outbox SET status='pending', available_at=?, lease_until=NULL, "
            "attempts=CASE WHEN status='processing' THEN MAX(0, attempts-1) ELSE attempts END "
            "WHERE id=? AND status IN ('processing', 'pending')",
            (now + max(1.0, float(seconds)), item_id),
        )

    def retry(self, item_id: int) -> None:
        self.db.execute(
            "UPDATE outbox SET status='pending', available_at=?, lease_until=NULL WHERE id=?",
            (time.time(), item_id),
        )

    def recover_expired(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cur = self.db.execute(
            "UPDATE outbox SET status='pending', lease_until=NULL "
            "WHERE status='processing' AND (lease_until IS NULL OR lease_until < ?)",
            (now,),
        )
        return int(cur.rowcount or 0)

    def stats(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}
