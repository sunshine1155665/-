"""2026-11-15: daily quotas with ``0 = unlimited`` (wakeup / continue / voice).

The boss mandate ("钱的方面不用省，日上限归零") turned several daily counters
into ``0`` in every shipped profile.  Before this change ``0`` meant different
things in different places -- "cap of zero" in wakeup/voice, silently coerced
to the default 6 in the continue ladder -- so each lane needs both halves
pinned:

* ``0`` really means "no daily cap";
* a positive value keeps the exact pre-change rule (the cap still bites).

Offline only: temp dirs, an explicit TOML overlay merged over ``low.toml``,
no network, no real ``data/``.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fatfish.config import Config, load_config
from fatfish.continue_policy import DEFAULT_MAX_RUNGS_PER_DAY, ContinuePolicy
from fatfish.db import Database
from fatfish.voice import VoicePolicy
from fatfish.wakeup import (
    SELF_CONTINUE_INTENT,
    ProactiveGate,
    WakeupScheduler,
)

TZ8 = timezone(timedelta(hours=8))
# 2026-10-24 14:00 local (UTC+8): outside quiet hours, deterministic against
# both the real ZoneInfo and the Windows UTC+8 fallback in fatfish.clock.
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
SESSION = "qq:private:10001"


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def make_env(
    tmp: Path,
    *,
    name: str,
    extras: dict[str, object] | None = None,
) -> tuple[Config, Database]:
    """Temp config (``low`` + explicit dotted overrides) and an empty database."""
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / f"{name}.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
    ]
    sections: dict[str, list[str]] = {}
    for key, value in (extras or {}).items():
        section, field = key.split(".", 1)
        sections.setdefault(section, []).append(f"{field} = {_toml_value(value)}")
    for section, rows in sections.items():
        lines.append(f"[{section}]")
        lines.extend(rows)
    path = tmp / f"{name}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def _bare_config(**sections: dict) -> Config:
    """A bare ``Config`` carrying only the sections given (no disk, no profile)."""
    return Config({name: dict(body) for name, body in sections.items()}, profile="test")


def _insert_sent(count: int, *, at: float, db: Database) -> None:
    for _ in range(count):
        db.execute(
            "INSERT INTO decision_log(at, kind, action, outcome) VALUES (?,?,?,?)",
            (at, "proactive_message", "sent", "queued"),
        )


def _insert_voice_usage(count: int, *, at: float, db: Database) -> None:
    for _ in range(count):
        db.execute(
            "INSERT INTO voice_usage(at, session_id, chars, mode) VALUES (?,?,?,?)",
            (at, "s", 4, "voice_only"),
        )


class EnvTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_env(self, *, name: str, extras: dict | None = None):
        config, db = make_env(self.tmp, name=name, extras=extras)
        self.addCleanup(db.close)
        return config, db


class WakeupDailyCapTests(EnvTestBase):
    def test_zero_general_cap_is_unlimited(self) -> None:
        config, db = self.make_env(
            name="wakeup_zero", extras={"wakeup.max_per_day": 0}
        )
        scheduler = WakeupScheduler(db, config)
        for offset in range(40):
            scheduler.schedule(
                NOON + 60 + offset,
                intent="check_user",
                reason="r",
                session_id=SESSION,
                now=NOON,
            )
        self.assertEqual(len(scheduler.list(limit=100)), 40)

        gate = ProactiveGate(db, config)
        # 100 proactive messages already "sent" today: with the old "0 = cap of
        # zero" semantics the gate refused everything; 0 is now no cap at all.
        _insert_sent(100, at=NOON - 3700, db=db)
        wakeup = scheduler.list(limit=1)[0]
        self.assertEqual(gate.should_act(wakeup, now=NOON + 120), (True, "ok"))

    def test_positive_general_cap_still_bites(self) -> None:
        config, db = self.make_env(
            name="wakeup_cap1", extras={"wakeup.max_per_day": 1}
        )
        scheduler = WakeupScheduler(db, config)
        scheduler.schedule(
            NOON + 60, intent="check_user", reason="r", session_id=SESSION, now=NOON
        )
        with self.assertRaises(ValueError):
            scheduler.schedule(
                NOON + 120,
                intent="check_user",
                reason="r",
                session_id=SESSION,
                now=NOON,
            )

        gate = ProactiveGate(db, config)
        _insert_sent(1, at=NOON - 3700, db=db)
        wakeup = scheduler.list(limit=1)[0]
        self.assertEqual(
            gate.should_act(wakeup, now=NOON + 120), (False, "daily_limit")
        )

    def test_zero_self_continue_cap_is_unlimited(self) -> None:
        config, db = self.make_env(
            name="self_continue_zero",
            extras={
                "wakeup.max_per_day": 5,
                "wakeup.self_continue_max_per_day": 0,
            },
        )
        scheduler = WakeupScheduler(db, config)
        for offset in range(80):
            scheduler.schedule(
                NOON + 60 + offset,
                intent=SELF_CONTINUE_INTENT,
                reason="r",
                session_id=SESSION,
                now=NOON,
            )
        self.assertEqual(len(scheduler.list(limit=200)), 80)

    def test_positive_self_continue_cap_still_bites(self) -> None:
        config, db = self.make_env(
            name="self_continue_cap2",
            extras={"wakeup.self_continue_max_per_day": 2},
        )
        scheduler = WakeupScheduler(db, config)
        for offset in range(2):
            scheduler.schedule(
                NOON + 60 + offset,
                intent=SELF_CONTINUE_INTENT,
                reason="r",
                session_id=SESSION,
                now=NOON,
            )
        with self.assertRaises(ValueError):
            scheduler.schedule(
                NOON + 300,
                intent=SELF_CONTINUE_INTENT,
                reason="r",
                session_id=SESSION,
                now=NOON,
            )


class ContinueDailyCapTests(EnvTestBase):
    def _policy(self, *, name: str, cap: object) -> ContinuePolicy:
        config = _bare_config(
            chat={"coax_max_rungs_per_day": cap},
            app={"timezone": "Asia/Shanghai"},
        )
        db = Database(self.tmp / f"{name}.db")
        self.addCleanup(db.close)
        db.initialize()
        return ContinuePolicy(db, config)

    def test_zero_cap_is_unlimited(self) -> None:
        policy = self._policy(name="cap_zero", cap=0)
        self.assertEqual(policy.max_rungs_per_day, 0)
        for _ in range(50):
            policy.count_rung(SESSION, now=NOON)
        for _ in range(10):
            decision = policy.should_arm(
                SESSION, now=NOON, desire=0.9, thread_open=True
            )
            self.assertEqual(decision.action, "arm")
            self.assertEqual(decision.reason, "desire")

    def test_positive_cap_still_bites(self) -> None:
        policy = self._policy(name="cap_two", cap=2)
        self.assertEqual(policy.max_rungs_per_day, 2)
        for _ in range(2):
            policy.count_rung(SESSION, now=NOON)
        decision = policy.should_arm(
            SESSION, now=NOON, desire=0.9, thread_open=True
        )
        self.assertEqual(decision.action, "none")
        self.assertEqual(decision.reason, "daily_cap")

    def test_missing_cap_keeps_the_historical_default(self) -> None:
        config = _bare_config(chat={}, app={"timezone": "Asia/Shanghai"})
        db = Database(self.tmp / "continue_missing.db")
        self.addCleanup(db.close)
        db.initialize()
        policy = ContinuePolicy(db, config)
        self.assertEqual(policy.max_rungs_per_day, DEFAULT_MAX_RUNGS_PER_DAY)


class VoiceDailyCapTests(EnvTestBase):
    def _policy(self, *, name: str, **voice: object) -> VoicePolicy:
        config = _bare_config(
            voice={"enabled": True, **voice}, app={"timezone": "Asia/Shanghai"}
        )
        db = Database(self.tmp / f"{name}.db")
        self.addCleanup(db.close)
        db.initialize()
        return VoicePolicy(db, config)

    def _decide(self, policy: VoicePolicy):
        # A direct user request bypasses the scoring model, so the decision
        # reflects only the budget gate.
        return policy.decide("晚安，做个好梦", session_id="s", user_requested="voice", now=NOON)

    def test_zero_daily_cap_is_unlimited(self) -> None:
        policy = self._policy(name="voice_day_zero", max_per_hour=99, max_per_day=0)
        # 200 uses in the last 24h (but outside the rolling hour, so this test
        # pins the *daily* cap only): with "0 = cap of zero" every one of them
        # would have blocked speech.
        _insert_voice_usage(200, at=NOON - 7200, db=policy.db)
        decision = self._decide(policy)
        self.assertEqual(decision.mode, "voice_only")
        self.assertIn("user_requested_voice", decision.reasons)

    def test_positive_daily_cap_still_bites(self) -> None:
        policy = self._policy(name="voice_day_two", max_per_hour=99, max_per_day=2)
        _insert_voice_usage(1, at=NOON - 60, db=policy.db)
        self.assertEqual(self._decide(policy).mode, "voice_only")
        _insert_voice_usage(1, at=NOON - 60, db=policy.db)
        decision = self._decide(policy)
        self.assertEqual(decision.mode, "text_only")
        self.assertIn("voice_budget_exhausted", decision.reasons)

    def test_hourly_cap_keeps_its_old_semantics(self) -> None:
        # max_per_hour > 0 still caps the rolling hour...
        policy = self._policy(name="voice_hour_one", max_per_hour=1, max_per_day=0)
        _insert_voice_usage(1, at=NOON - 60, db=policy.db)
        decision = self._decide(policy)
        self.assertEqual(decision.mode, "text_only")
        self.assertIn("voice_budget_exhausted", decision.reasons)
        # ...but a row older than an hour does not count against it.
        fresh = self._policy(name="voice_hour_old", max_per_hour=1, max_per_day=0)
        _insert_voice_usage(1, at=NOON - 7200, db=fresh.db)
        self.assertEqual(self._decide(fresh).mode, "voice_only")

    def test_zero_hourly_cap_still_means_voice_disabled(self) -> None:
        # Pre-2026-11-15 meaning, deliberately unchanged: 0/hour disables voice
        # ("count < 0" is never true).  Only the *daily* cap got the 0=unlimited
        # convention.
        policy = self._policy(name="voice_hour_zero", max_per_hour=0, max_per_day=0)
        decision = self._decide(policy)
        self.assertEqual(decision.mode, "text_only")
        self.assertIn("voice_budget_exhausted", decision.reasons)


if __name__ == "__main__":
    unittest.main()
