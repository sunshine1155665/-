"""Per-turn trace for observability and later evaluation."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .db import Database


class TraceRecorder:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        session_id: str,
        *,
        scope: str,
        trust: str,
        user_text: str,
        stable_prefix_hash: str = "",
        dynamic_hash: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_hit_tokens: int = 0,
        latency_ms: float = 0.0,
        model: str = "",
        actions: list[str] | None = None,
        result: dict[str, Any] | None = None,
        security_risk: float = 0.0,
        blocked: bool = False,
    ) -> str:
        trace_id = uuid.uuid4().hex
        text = str(user_text or "")
        self.db.execute(
            "INSERT INTO turn_traces("
            "id, session_id, scope, trust, at, user_hash, user_len, stable_prefix_hash, dynamic_hash, "
            "prompt_tokens, completion_tokens, cache_hit_tokens, latency_ms, model, "
            "actions_json, result_json, security_risk, blocked"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trace_id,
                session_id,
                scope,
                trust,
                time.time(),
                hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
                len(text),
                stable_prefix_hash,
                dynamic_hash,
                int(prompt_tokens),
                int(completion_tokens),
                int(cache_hit_tokens),
                float(latency_ms),
                model,
                json.dumps(actions or [], ensure_ascii=False),
                json.dumps(result or {}, ensure_ascii=False, default=str)[:12000],
                float(security_risk),
                1 if blocked else 0,
            ),
        )
        return trace_id

    def last(self, limit: int = 1) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM turn_traces ORDER BY at DESC LIMIT ?", (max(1, int(limit)),)
        )
        return [dict(row) for row in rows]

    def get(self, trace_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM turn_traces WHERE id=?", (trace_id,))
        return dict(row) if row else None

    def summary(self, limit: int = 100) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT latency_ms, prompt_tokens, completion_tokens, cache_hit_tokens, blocked "
            "FROM turn_traces ORDER BY at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        if not rows:
            return {"turns": 0}
        n = len(rows)
        return {
            "turns": n,
            "blocked": sum(int(row["blocked"] or 0) for row in rows),
            "avg_latency_ms": round(sum(float(row["latency_ms"] or 0) for row in rows) / n, 1),
            "prompt_tokens": sum(int(row["prompt_tokens"] or 0) for row in rows),
            "completion_tokens": sum(int(row["completion_tokens"] or 0) for row in rows),
            "cache_hit_tokens": sum(int(row["cache_hit_tokens"] or 0) for row in rows),
        }
