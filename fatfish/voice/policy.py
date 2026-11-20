"""Voice vs text decision policy."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..clock import day_phase
from ..config import Config
from ..db import Database

_CODE = re.compile(r"```|`[^`]+`|https?://|www\.|\[[^\]]+\]\([^)]+\)")
_NUMBERS = re.compile(r"\b\d{3,}\b")


@dataclass(slots=True)
class VoiceDecision:
    mode: str  # text_only | voice_only | text_plus_voice
    score: float
    reasons: list[str] = field(default_factory=list)
    hard_rule: bool = False


class VoicePolicy:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.tz_name = config.timezone_name

    def decide(
        self,
        text: str,
        *,
        session_id: str = "default",
        is_group: bool = False,
        user_requested: str | None = None,
        emotional_intensity: float | None = None,
        now: float | None = None,
    ) -> VoiceDecision:
        now = time.time() if now is None else now
        text = (text or "").strip()
        reasons: list[str] = []

        if not text:
            return VoiceDecision("text_only", 0.0, ["empty"], True)
        if not bool(self.config.get("voice.enabled", True)):
            return VoiceDecision("text_only", 0.0, ["voice_disabled"], True)
        if user_requested == "text":
            return VoiceDecision("text_only", 0.0, ["user_requested_text"], True)
        if user_requested == "voice":
            if self._budget_ok(now):
                return VoiceDecision("voice_only", 1.0, ["user_requested_voice"], True)
            return VoiceDecision("text_only", 0.0, ["voice_budget_exhausted"], True)
        # Test-only switch (default off).  Production deliberately keeps long
        # informational replies text-only; during a voice audition we need every
        # private turn to be spoken so the sample set is not biased towards
        # short emotional lines.
        if not is_group and bool(self.config.get("voice.force", False)):
            if self._budget_ok(now):
                return VoiceDecision("voice_only", 1.0, ["forced_by_config"], True)
            return VoiceDecision("text_only", 0.0, ["voice_budget_exhausted"], True)
        if is_group:
            return VoiceDecision("text_only", 0.0, ["group_chat"], True)
        if _CODE.search(text) or "\n" in text and len(text) > 120:
            return VoiceDecision("text_only", 0.0, ["code_or_long_text"], True)
        if _information_density(text) >= 0.5 and len(text) > 20:
            return VoiceDecision("text_only", 0.0, ["information_dense"], True)
        if not self._budget_ok(now):
            return VoiceDecision("text_only", 0.0, ["voice_budget_exhausted"], True)

        emotion = _clamp(emotional_intensity if emotional_intensity is not None else _emotion(text))
        brevity = _brevity(text)
        intimacy = 0.8 if not is_group else 0.1
        user_pref = self._user_preference(session_id)
        context_fit = self._context_fit(now)
        novelty = self._novelty(session_id, now)
        info_density = _information_density(text)
        latency = 0.0

        score = (
            0.30 * emotion
            + 0.20 * brevity
            + 0.15 * intimacy
            + 0.15 * user_pref
            + 0.10 * context_fit
            + 0.10 * novelty
            - 0.30 * info_density
            - 0.20 * latency
        )
        score = _clamp(score)
        reasons.append(f"emotion={emotion:.2f}")
        reasons.append(f"brevity={brevity:.2f}")
        reasons.append(f"info={info_density:.2f}")

        min_score = float(self.config.get("voice.min_score", 0.62))
        mid_score = float(self.config.get("voice.text_with_voice_min", 0.45))
        if score >= min_score:
            return VoiceDecision("voice_only", round(score, 4), reasons)
        if score >= mid_score:
            return VoiceDecision("text_plus_voice", round(score, 4), reasons)
        return VoiceDecision("text_only", round(score, 4), reasons)

    def record(self, decision: VoiceDecision, *, session_id: str = "default", chars: int = 0) -> None:
        if decision.mode == "text_only":
            return
        self.db.execute(
            "INSERT INTO voice_usage(at, session_id, chars, mode) VALUES (?,?,?,?)",
            (time.time(), session_id, int(chars), decision.mode),
        )
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (time.time(), "voice_sent", 1.0, f'{{"mode":"{decision.mode}"}}'),
        )

    def _budget_ok(self, now: float) -> bool:
        hour = self.db.query_one(
            "SELECT COUNT(*) AS n FROM voice_usage WHERE at>=?", (now - 3600,)
        )
        day = self.db.query_one(
            "SELECT COUNT(*) AS n FROM voice_usage WHERE at>=?", (now - 86400,)
        )
        max_hour = int(self.config.get("voice.max_per_hour", 4))
        max_day = int(self.config.get("voice.max_per_day", 16))
        # 2026-11-15 boss mandate: ``voice.max_per_day = 0`` means unlimited.
        # Only 0 changes; the hourly cap keeps its historical meaning exactly
        # (0 there still disables voice altogether -- "count < 0" is never true),
        # and every positive daily cap still enforces "count < cap".
        hour_ok = not hour or int(hour["n"]) < max_hour
        day_ok = max_day == 0 or not day or int(day["n"]) < max_day
        return bool(hour_ok and day_ok)

    def _user_preference(self, session_id: str) -> float:
        # Placeholder until explicit voice feedback is recorded. Keep a neutral
        # prior rather than pretending sticker reactions are voice preferences.
        return 0.55

    def _novelty(self, session_id: str, now: float) -> float:
        row = self.db.query_one(
            "SELECT at FROM voice_usage WHERE session_id=? ORDER BY at DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return 1.0
        minutes = max(0.0, now - float(row["at"])) / 60.0
        return _clamp(1.0 - math.exp(-minutes / 30.0))

    def _context_fit(self, now: float) -> float:
        phase = day_phase(now, self.tz_name)
        return {"morning": 0.55, "afternoon": 0.50, "evening": 0.75, "night": 0.65}.get(phase, 0.5)


def _emotion(text: str) -> float:
    score = 0.25
    score += 0.15 * min(3, text.count("!"))
    score += 0.10 * min(3, text.count("？") + text.count("?"))
    if any(word in text for word in ("哈哈", "呜呜", "讨厌", "喜欢你", "晚安", "想你", "气死", "好耶", "呜呜呜")):
        score += 0.25
    if any(ch in text for ch in "😭😡🥺😳😆😂🤣❤️💔"):
        score += 0.20
    return _clamp(score)


def _brevity(text: str) -> float:
    n = len(text)
    if n <= 40:
        return 1.0
    if n >= 150:
        return 0.0
    return _clamp(1.0 - (n - 40) / 110.0)


def _information_density(text: str) -> float:
    score = 0.0
    if _CODE.search(text):
        score += 0.6
    if _NUMBERS.search(text):
        score += 0.2
    if len(text) > 120:
        score += 0.3
    if "\n" in text:
        score += 0.2
    if any(word in text for word in (
        "步骤", "安装", "运行", "命令", "配置", "设置", "教程", "怎么", "如何",
        "方法", "流程", "链接", "地址", "价格", "时间", "地点", "名单", "代码", "报错",
    )):
        score += 0.5
    if "：" in text and len(text) > 20:
        score += 0.15
    return _clamp(score)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
