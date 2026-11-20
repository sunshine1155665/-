"""Daily relationship rituals and the three-day neglect check.

These are local, deterministic policies. They create wakeups, not API calls.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .clock import get_timezone, human_time
from .config import Config
from .db import Database
from .owner import OwnerRegistry
from .wakeup import WakeupScheduler


class RelationshipRituals:
    def __init__(
        self,
        db: Database,
        config: Config,
        wakeup: WakeupScheduler,
        *,
        owner: OwnerRegistry | None = None,
    ):
        self.db = db
        self.config = config
        self.wakeup = wakeup
        self.owner = owner
        self.tz_name = config.timezone_name
        self.enabled = bool(config.get("relationship.enabled", True))
        self.session_id = str(config.get("relationship.session_id", "default"))
        self.morning = str(config.get("relationship.morning_greeting", "08:30"))
        self.night = str(config.get("relationship.night_checkin", "22:30"))
        self.neglect_days = int(config.get("relationship.neglect_days", 3))

    def tick(self, *, now: float | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, get_timezone(self.tz_name))
        today = local.strftime("%Y-%m-%d")
        scheduled: list[dict[str, Any]] = []

        if self._at_or_after(local, self.morning) and self.db.get_meta("last_morning_ritual_date") != today:
            item = self._schedule("早安：问主人哥哥今天怎么样")
            self.db.set_meta("last_morning_ritual_date", today)
            if item:
                scheduled.append(item)

        if self._at_or_after(local, self.night) and self.db.get_meta("last_night_ritual_date") != today:
            item = self._schedule("晚安：问主人哥哥今天累不累")
            self.db.set_meta("last_night_ritual_date", today)
            if item:
                scheduled.append(item)

        if self.neglect_days > 0:
            last_user = self._last_user_at()
            if last_user is not None and now - last_user >= self.neglect_days * 86400:
                last_alert = self.db.get_meta("last_neglect_alert_date")
                if last_alert != today:
                    item = self._schedule(
                        f"主人哥哥已经 {self.neglect_days} 天没理我了，去找他"
                    )
                    self.db.set_meta("last_neglect_alert_date", today)
                    if item:
                        scheduled.append(item)
        return scheduled

    @property
    def target_session_id(self) -> str:
        """The owner's session when bound; the configured fallback otherwise."""
        if self.owner is not None:
            binding = self.owner.get()
            if binding and binding.get("session_id"):
                return str(binding["session_id"])
        return self.session_id

    def _schedule(self, reason: str) -> dict[str, Any] | None:
        try:
            wakeup = self.wakeup.schedule(
                "+1m",
                intent="check_user",
                reason=reason,
                session_id=self.target_session_id,
            )
            return {
                "id": wakeup.id,
                "reason": reason,
                "fire_at": wakeup.fire_at,
                "fire_at_text": human_time(wakeup.fire_at, self.tz_name),
            }
        except ValueError as exc:
            return {"error": str(exc), "reason": reason}

    def _last_user_at(self) -> float | None:
        row = self.db.query_one(
            "SELECT MAX(created_at) AS at FROM conversation_turns WHERE role='user'"
        )
        return float(row["at"]) if row and row["at"] is not None else None

    @staticmethod
    def _at_or_after(local: datetime, hhmm: str) -> bool:
        try:
            hour, minute = (int(part) for part in hhmm.split(":"))
        except ValueError:
            return False
        return (local.hour, local.minute) >= (hour, minute)


def proactive_text_for_reason(reason: str) -> str | None:
    if reason.startswith("早安"):
        return "主人哥哥早安！今天也要好好吃饭哦。本鲸鱼娘会想你的。"
    if reason.startswith("晚安"):
        return "主人哥哥，今天累不累？累了就早点睡，本鲸鱼娘陪你。"
    if "天没理" in reason:
        return "主人哥哥都这么多天没理本鲸鱼娘了……我来找你了。"
    return None
