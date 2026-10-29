"""Local recall gate: decide which memories/evidence actually fit the turn.

Retrieval itself is local and always allowed. Injection is gated so a one-shot
API call gets enough context without dumping the whole database into the prompt.
Kev is optional; rules are the fallback.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..db import Database
from .service import MemoryHit, RagHit

try:
    from ..decision.kev import KevClient
except Exception:  # pragma: no cover - optional
    KevClient = None  # type: ignore


@dataclass(slots=True)
class RecallGateResult:
    memory_hits: list[MemoryHit]
    rag_hits: list[RagHit]
    scores: dict[str, float]


class RecallGate:
    def __init__(self, db: Database, config: Config, kev: Any | None = None):
        self.db = db
        self.config = config
        self.memory_threshold = float(config.get("memory.recall_threshold", 0.35))
        self.rag_threshold = float(config.get("memory.rag_threshold", 0.28))
        self.kev_threshold = float(config.get("memory.recall_kev_threshold", 0.50))
        self.use_kev = str(config.get("decision.backend", "rules")).lower() == "kev"
        self.kev = kev
        self._kev_disabled_until = 0.0
        if self.kev is None and self.use_kev and KevClient is not None:
            self.kev = KevClient(
                str(config.get("decision.kev_base_url", "http://127.0.0.1:8009")),
                str(config.get("decision.kev_model", "")),
                float(config.get("decision.kev_timeout_seconds", 3.0)),
            )

    def select(
        self,
        query: str,
        memory_hits: list[MemoryHit],
        rag_hits: list[RagHit],
        *,
        context_text: str = "",
        memory_limit: int = 4,
        rag_limit: int = 3,
    ) -> RecallGateResult:
        mem_fit = self._kev_scores("memory", query, context_text, memory_hits) if self.kev else {}
        rag_fit = self._kev_scores("rag", query, context_text, rag_hits) if self.kev else {}

        selected_memory: list[MemoryHit] = []
        scores: dict[str, float] = {}
        for hit in memory_hits:
            base = hit.score
            fit = mem_fit.get(hit.item.id)
            score = base if fit is None else 0.6 * base + 0.4 * fit
            scores[f"memory:{hit.item.id}"] = round(score, 4)
            threshold = self.kev_threshold if fit is not None else self.memory_threshold
            if score >= threshold:
                selected_memory.append(hit)
        selected_memory.sort(
            key=lambda h: scores.get(f"memory:{h.item.id}", h.score), reverse=True
        )

        selected_rag: list[RagHit] = []
        for hit in rag_hits:
            base = hit.score
            fit = rag_fit.get(hit.id)
            score = base if fit is None else 0.6 * base + 0.4 * fit
            scores[f"rag:{hit.id}"] = round(score, 4)
            threshold = self.kev_threshold if fit is not None else self.rag_threshold
            if score >= threshold:
                selected_rag.append(hit)
        selected_rag.sort(key=lambda h: scores.get(f"rag:{h.id}", h.score), reverse=True)

        return RecallGateResult(
            memory_hits=selected_memory[: max(1, memory_limit)],
            rag_hits=selected_rag[: max(1, rag_limit)],
            scores=scores,
        )

    def _kev_scores(
        self,
        kind: str,
        query: str,
        context_text: str,
        hits: list[Any],
    ) -> dict[str, float]:
        if not hits or self.kev is None or time.time() < self._kev_disabled_until:
            return {}
        state = {
            "current_message": query,
            "recent_context": (context_text or "")[:1600],
            "candidates": [],
        }
        questions: dict[str, dict[str, Any]] = {}
        for index, hit in enumerate(hits):
            if kind == "memory":
                cid = hit.item.id
                text = hit.item.gist
                when = hit.item.event_date or ""
                state["candidates"].append({"id": cid, "text": text, "date": when})
            else:
                cid = hit.id
                text = hit.text[:400]
                state["candidates"].append({"id": cid, "text": text})
            questions[f"q{index}"] = {
                "type": "noul",
                "instructions": (
                    "这条候选内容是否能帮助理解或回应当前消息？"
                    "只判断相关性，不要判断它是否真实。"
                ),
            }
        try:
            answers = self.kev.system_one(state, questions)
        except Exception:
            # Avoid paying the timeout on every turn when Kev is down.
            self._kev_disabled_until = time.time() + 60.0
            return {}
        result: dict[str, float] = {}
        for index, hit in enumerate(hits):
            answer = answers.get(f"q{index}")
            if answer is None:
                continue
            cid = hit.item.id if kind == "memory" else hit.id
            value = answer.value if isinstance(answer.value, (int, float)) else 0.0
            result[str(cid)] = max(0.0, min(1.0, float(value)))
        return result


def scores_debug(result: RecallGateResult) -> str:
    return json.dumps(result.scores, ensure_ascii=False, sort_keys=True)
