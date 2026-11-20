"""Kev / Jev-compatible decision client.

Kev serves POST /v1/systemone. This client is optional: callers fall back to
rules when the server is unavailable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class KevAnswer:
    kind: str
    value: Any
    confidence: float
    probabilities: dict[str, float]


class KevClient:
    def __init__(self, base_url: str, model: str = "", timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)

    def system_one(
        self,
        state: Any,
        questions: dict[str, dict[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, KevAnswer]:
        payload = {
            "model": model or self.model,
            "state": state,
            "questions": questions,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_answers(data)

    def noul(self, state: Any, instructions: str, *, criteria: dict[str, str] | None = None) -> KevAnswer:
        question: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = criteria
        return self.system_one(state, {"q": question})["q"]

    def choice(
        self,
        state: Any,
        instructions: str,
        options: dict[str, str],
    ) -> KevAnswer:
        question = {"type": "choice", "instructions": instructions, "criteria": options}
        return self.system_one(state, {"q": question})["q"]

    def score(
        self,
        state: Any,
        instructions: str,
        levels: list[str],
    ) -> KevAnswer:
        question = {"type": "score", "instructions": instructions, "criteria": levels}
        return self.system_one(state, {"q": question})["q"]


def _parse_answers(data: dict[str, Any]) -> dict[str, KevAnswer]:
    raw = data.get("answers") or data.get("results") or data
    answers: dict[str, KevAnswer] = {}
    if not isinstance(raw, dict):
        return answers
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        kind = str(value.get("type", ""))
        if kind == "choice":
            answers[key] = KevAnswer(
                kind=kind,
                value=value.get("choice"),
                confidence=float(value.get("confidence", 0.0) or 0.0),
                probabilities=_float_map(value.get("probabilities")),
            )
        elif kind == "noul":
            p = float(value.get("noul", 0.0) or 0.0)
            answers[key] = KevAnswer(
                kind=kind,
                value=p,
                confidence=max(p, 1.0 - p),
                probabilities={"true": p, "false": 1.0 - p},
            )
        elif kind == "score":
            answers[key] = KevAnswer(
                kind=kind,
                value=float(value.get("score", 0.0) or 0.0),
                confidence=float(value.get("confidence", 0.0) or 0.0),
                probabilities=_float_map(value.get("probabilities")),
            )
    return answers


def _float_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, raw in value.items():
        try:
            result[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return result
