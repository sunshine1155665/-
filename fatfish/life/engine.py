"""Minimal life engine: mood, energy, current activity, day plans."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..clock import get_timezone, in_quiet_hours
from ..config import Config
from ..db import Database

_ACTIVITIES = [
    ("browse_bilibili", 30, 0.6),
    ("read", 30, 0.4),
    ("rest", 20, 0.2),
    ("write_notes", 30, 0.5),
    ("listen_music", 20, 0.3),
    ("chores", 20, 0.5),
]


@dataclass(slots=True)
class LifeSnapshot:
    at: float
    mood: str
    energy: float
    social: float
    phase: str
    current_activity: str
    activity_started_at: float
    planned_minutes: int
    note: str


class LifeEngine:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.tz_name = config.timezone_name

    def state(self) -> LifeSnapshot:
        row = self.db.query_one("SELECT * FROM life_state WHERE id=1")
        if not row:
            now = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO life_state(id, at, mood, energy, social, phase, current_activity, "
                "activity_started_at, planned_minutes, note) VALUES (1,?,?,?,?,?,?,?,?,?)",
                (now, "calm", 70, 0.5, "idle", "idle", now, 30, "init"),
            )
            return self.state()
        return LifeSnapshot(
            at=float(row["at"]),
            mood=row["mood"],
            energy=float(row["energy"]),
            social=float(row["social"]),
            phase=row["phase"],
            current_activity=row["current_activity"],
            activity_started_at=float(row["activity_started_at"] or row["at"]),
            planned_minutes=int(row["planned_minutes"]),
            note=row["note"] or "",
        )

    def tick(self, *, now: float | None = None) -> LifeSnapshot:
        now = time.time() if now is None else now
        state = self.state()
        hours = max(0.0, now - state.at) / 3600.0
        decay = float(self.config.get("life.energy_decay_per_hour", 5.0))
        energy = max(0.0, min(100.0, state.energy - decay * hours))
        phase = self._phase(now)
        activity = state.current_activity
        started = state.activity_started_at
        planned = state.planned_minutes
        note = state.note

        if phase == "sleeping" and activity != "sleeping":
            activity, started, planned, note = "sleeping", now, 480, "quiet hours"
            energy = min(100.0, energy + 10.0)
        elif activity in {"idle", "sleeping"} or now >= started + planned * 60:
            activity, planned, note = self._choose_activity(now, energy)
            started = now
            energy = max(0.0, energy - 0.5)

        mood = "tired" if energy < 25 else ("energetic" if energy > 75 else state.mood)
        self.db.execute(
            "UPDATE life_state SET at=?, mood=?, energy=?, phase=?, current_activity=?, "
            "activity_started_at=?, planned_minutes=?, note=? WHERE id=1",
            (now, mood, energy, phase, activity, started, planned, note),
        )
        return self.state()

    def interrupt_for_chat(self, *, now: float | None = None) -> LifeSnapshot:
        now = time.time() if now is None else now
        state = self.state()
        self.db.execute(
            "UPDATE life_state SET at=?, phase='chatting', current_activity='chatting', "
            "activity_started_at=?, planned_minutes=5, note='user message' WHERE id=1",
            (now, now),
        )
        self._metric("life_interrupt", 1.0, {"from": state.current_activity})
        return self.state()

    def resume_after_chat(self, *, now: float | None = None) -> LifeSnapshot:
        now = time.time() if now is None else now
        self.db.execute(
            "UPDATE life_state SET at=?, phase=?, current_activity='idle', activity_started_at=?, "
            "planned_minutes=15, note='after chat' WHERE id=1",
            (now, self._time_phase(now), now),
        )
        return self.state()

    def plan_day(self, *, day: str | None = None, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        tz = get_timezone(self.tz_name)
        local = datetime.fromtimestamp(now, tz)
        day = day or local.strftime("%Y-%m-%d")
        existing = self.db.query_one("SELECT COUNT(*) AS n FROM day_plans WHERE plan_date=?", (day,))
        if existing and int(existing["n"]) > 0:
            return [dict(row) for row in self.db.query("SELECT * FROM day_plans WHERE plan_date=? ORDER BY start_min", (day,))]
        plan = [
            (0, 480, "sleeping", "睡觉"),
            (480, 540, "morning_routine", "起床、咖啡、看消息"),
            (540, 660, "browse_bilibili", "刷 B 站、收集素材"),
            (660, 720, "rest", "午休"),
            (720, 960, "project", "做自己的事/学习"),
            (960, 1080, "browse_bilibili", "继续刷 B 站/找灵感"),
            (1080, 1230, "chat", "和用户聊天"),
            (1230, 1290, "diary", "写日记/整理记忆"),
            (1290, 1440, "sleeping", "睡觉"),
        ]
        for start_min, end_min, activity, intent in plan:
            self.db.execute(
                "INSERT INTO day_plans(plan_date, start_min, end_min, activity, intent, planned_by, state, created_at) "
                "VALUES (?,?,?,?,?, 'bot', 'planned', ?)",
                (day, start_min, end_min, activity, intent, now),
            )
        return [dict(row) for row in self.db.query("SELECT * FROM day_plans WHERE plan_date=? ORDER BY start_min", (day,))]

    def _phase(self, now: float) -> str:
        sleep_start = self.config.get("life.sleep_start", "23:30")
        sleep_end = self.config.get("life.sleep_end", "08:00")
        if in_quiet_hours(now, sleep_start, sleep_end, self.tz_name):
            return "sleeping"
        state = self.state()
        if state.current_activity == "chatting":
            return "chatting"
        return self._time_phase(now)

    def _time_phase(self, now: float) -> str:
        local = datetime.fromtimestamp(now, get_timezone(self.tz_name))
        if 5 <= local.hour < 11:
            return "morning"
        if 11 <= local.hour < 17:
            return "afternoon"
        if 17 <= local.hour < 23:
            return "evening"
        return "night"

    def _choose_activity(self, now: float, energy: float) -> tuple[str, int, str]:
        if energy < 20:
            return "rest", 30, "low energy"
        local = datetime.fromtimestamp(now, get_timezone(self.tz_name))
        if 6 <= local.hour < 10:
            return "browse_bilibili", 40, "morning feed"
        if 10 <= local.hour < 17:
            return "read" if energy > 55 else "rest", 30, "afternoon"
        if 17 <= local.hour < 22:
            return "browse_bilibili", 30, "evening feed"
        return "write_notes", 20, "late notes"

    def _metric(self, key: str, value: float, tags: dict[str, Any]) -> None:
        import json

        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (time.time(), key, value, json.dumps(tags, ensure_ascii=False)),
        )
