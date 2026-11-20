"""T-0042 L4: desire gate for self-actions + evidence injection + isolation.

What this file pins (one class per concern):

* ``ServiceTests``    -- the state key, the gate order, the counters, the lift.
* ``GateTests``       -- ``execute_actions`` refuses before the branch runs, one
  refusal = one ``decision_log(kind='self_action')`` row, per-turn budget.
* ``IsolationTests``  -- the point of the whole card: ``ai_chat`` can be pinned
  at its ceiling without moving ``self_action`` (and vice versa), because the
  arithmetic is shared but the state keys / config sections are not.
* ``InjectionTests``  -- evidence reaches the *next* prompt exactly once, is
  wrapped as untrusted, expires after 24h, and never touches ``stable_prefix``.
* ``TurnTests``       -- a refused action does not cost the text reply; a vague
  "look this up" that does *not* name her memory still goes through the gate
  (the owner's explicit exemption is pinned separately, in
  ``tests/test_self_action_exempt.py``).

Offline only: temp database, fake memory / qzone / photo / bilibili / LLM.
No network, no ComfyUI, no TTS.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fatfish.ai_chat.service import AiChatService
from fatfish.config import load_config
from fatfish.context import SELF_ACTION_PROMPT_HEADER, ensure_default_persona
from fatfish.db import Database
from fatfish.desire.settings import (
    SELF_ACTION_DEFAULTS,
    read_desire_settings,
)
from fatfish.desire.state import state_key
from fatfish.llm import LLMResult, LLMUsage
from fatfish.runtime import SELF_ACTION_TOOLS, Runtime, TurnAction
from fatfish.security import TrustLevel
from fatfish.self_action.service import SelfActionService
from fatfish.voice import VoiceDecision

TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
NIGHT = datetime(2026, 10, 24, 2, 0, tzinfo=TZ8).timestamp()
SESSION = "qq:private:1001"
PEER = "2002"
AI_CHAT_KEY = state_key("ai_chat", PEER)
SELF_KEY = state_key("self_action", "global")
FOUR = ("search_memory", "read_qzone", "make_photo", "search_videos")
ARGS = {
    "search_memory": {"query": "海豚"},
    "read_qzone": {"refresh": True},
    "make_photo": {"prompt": "海边的鲸鱼"},
    "search_videos": {"topic": "海洋"},
}


# --------------------------------------------------------------------- fakes
class FakeMemory:
    def __init__(self, hits=None):
        self.hits = list(hits or [])
        self.calls: list[dict] = []

    def search(self, query, limit=8, *, session_id=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "session_id": session_id})
        return list(self.hits)


class FakeQzone:
    enabled = False

    def __init__(self):
        self.fetch_calls: list[dict] = []
        self.recent_calls = 0

    @property
    def breaker_open(self) -> bool:
        return False

    def fetch_and_ingest(self, client=None, *, manual=False, now=None):
        self.fetch_calls.append({"manual": manual, "now": now})
        return {"ok": True, "new": 0}

    def recent_items(self, limit=10, *, include_friend=False, include_hidden=False):
        self.recent_calls += 1
        return []


class FakePhoto:
    enabled = True

    def __init__(self, *, ok: bool = True, error: str | None = None):
        self.ok = ok
        self.error = error
        self.calls: list[dict] = []

    def generate(self, text, *, context=None, session_id=None, now=None):
        self.calls.append({"text": text, "session_id": session_id, "now": now})
        if not self.ok:
            return {
                "ok": False,
                "path": None,
                "caption": "",
                "error": self.error or "comfyui_timeout",
            }
        return {
            "ok": True,
            "path": "data/photos/test.png",
            "caption": "一张测试图",
            "error": None,
        }


class FakeBilibili:
    def __init__(self):
        self.calls: list[dict] = []

    def browse_once(self, limit=5, keyword=None, client=None):
        self.calls.append({"limit": limit, "keyword": keyword})
        return [SimpleNamespace(title="深海鲸歌", up="海洋研究所", url="https://b23.tv/x")]


class FakeLLM:
    available = True

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def chat(self, messages, session_id=None):
        self.calls.append({"messages": messages, "session_id": session_id})
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=4),
            model="fake-model",
        )


class FakeVoice:
    def decide(self, text, **kwargs):
        return VoiceDecision("text_only", 0.0, ["test"])

    def record(self, *args, **kwargs):
        pass


class DictConfig:
    """Minimal ``config.get("section.key")`` provider for service unit tests."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, dotted: str, default=None):
        cur = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


# ------------------------------------------------------------------- helpers
def write_config(
    tmp: Path,
    *,
    name: str = "gate.toml",
    db_name: str = "gate.db",
    enabled: bool = True,
    initial: float = 0.35,
    step: float = 0.10,
    floor: float = 0.20,
    ceiling: float = 0.90,
    decay: int = 30,
    daily_cap: int = 12,
    per_turn: int = 2,
    quiet: str = '["01:00", "08:00"]',
    bilibili: bool = True,
    ai_chat: bool = False,
) -> Path:
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / db_name).as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        "[qzone]",
        "enabled = false",
        "[activity]",
        "enabled = false",
        "[qq]",
        "enabled = false",
        "catchup_enabled = false",
        "[self_action]",
        f"enabled = {'true' if enabled else 'false'}",
        f"initial_threshold = {initial}",
        f"threshold_step = {step}",
        f"threshold_floor = {floor}",
        f"threshold_ceiling = {ceiling}",
        f"decay_events_per_hour = {decay}",
        "hard_cap_per_hour = 0",
        f"daily_cap = {daily_cap}",
        f"max_events_per_turn = {per_turn}",
        "probe_interval_minutes = 30",
        f"quiet_hours = {quiet}",
        "[bilibili]",
        f"enabled = {'true' if bilibili else 'false'}",
        "[ai_chat]",
        f"enabled = {'true' if ai_chat else 'false'}",
        f'peer_uin = "{PEER}"',
        'peer_name = "大肥鱼"',
        "initial_threshold = 0.30",
        "threshold_step = 0.12",
        "threshold_floor = 0.15",
        "threshold_ceiling = 0.95",
        "decay_messages_per_hour = 50",
        "hard_cap_per_hour = 0",
        "probe_interval_minutes = 10",
        'quiet_hours = ["01:00", "08:00"]',
    ]
    path = tmp / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_env(tmp: Path, **kwargs) -> tuple[object, Database, Runtime]:
    config = load_config("low", write_config(tmp, **kwargs))
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


def settings_for(**overrides):
    base = {
        "enabled": True,
        "initial_threshold": 0.35,
        "threshold_step": 0.10,
        "threshold_floor": 0.20,
        "threshold_ceiling": 0.90,
        "decay_events_per_hour": 30,
        "hard_cap_per_hour": 0,
        "probe_interval_minutes": 30,
        "daily_cap": 12,
        "max_events_per_turn": 2,
        "quiet_hours": ["01:00", "08:00"],
    }
    base.update(overrides)
    config = DictConfig({"self_action": dict(base)})
    return read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS)


def add_evidence(
    db: Database,
    *,
    created_at: float = NOON - 60,
    action: str = "search_memory",
    source: str = "memory",
    title: str = "海豚",
    content: str = "- 主人哥哥喜欢海豚（preference）",
    session_id: str = SESSION,
) -> int:
    cursor = db.execute(
        "INSERT INTO self_action_evidence"
        "(session_id, action, source, title, content, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, action, source, title, content, created_at),
    )
    return int(cursor.lastrowid)


def evidence_rows(db: Database) -> list[dict]:
    return [dict(row) for row in db.query("SELECT * FROM self_action_evidence ORDER BY id")]


def decision_rows(db: Database) -> list[dict]:
    return [
        dict(row)
        for row in db.query(
            "SELECT action, outcome FROM decision_log WHERE kind='self_action' ORDER BY id"
        )
    ]


class Env(unittest.TestCase):
    """Temp dir + runtime with all four action dependencies faked."""

    env_kwargs: dict = {}
    fake_memory: bool = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_env(self.tmp, **self.env_kwargs)
        self.addCleanup(self.db.close)
        self.real_memory = self.runtime.memory
        self.memory = FakeMemory()
        self.qzone = FakeQzone()
        self.photo = FakePhoto()
        self.bilibili = FakeBilibili()
        if self.fake_memory:
            self.runtime.memory = self.memory
        self.runtime.qzone = self.qzone
        self.runtime.photo = self.photo
        self.runtime.bilibili = self.bilibili

    def extra_env(self, name: str, **kwargs):
        """A second isolated environment (its own db file) in the same tmp."""
        kwargs.setdefault("name", f"{name}.toml")
        kwargs.setdefault("db_name", f"{name}.db")
        return make_env(self.tmp, **kwargs)

    def run_action(self, tool: str, args: dict, *, now: float = NOON, trust=TrustLevel.PRIVATE):
        results = self.runtime.execute_actions(
            [TurnAction(tool, dict(args))],
            session_id=SESSION,
            reply="",
            now=now,
            trust=trust,
        )
        self.assertEqual(len(results), 1, results)
        return results[0]

    def service(self) -> SelfActionService:
        service = self.runtime._self_action_service()
        self.assertIsNotNone(service)
        return service

    def fake_calls(self) -> int:
        return (
            len(self.memory.calls)
            + len(self.qzone.fetch_calls)
            + self.qzone.recent_calls
            + len(self.photo.calls)
            + len(self.bilibili.calls)
        )


# ------------------------------------------------------------ service (unit)
class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp, name="svc.toml", db_name="svc.db")
        self.addCleanup(self.db.close)

    def service(self, **overrides) -> SelfActionService:
        return SelfActionService(
            self.db, settings_for(**overrides), now_fn=lambda: NOON
        )

    def test_state_key_is_domain_scoped_not_bare(self) -> None:
        service = self.service()
        self.assertEqual(service.state_key, "self_action:state:global")
        self.assertEqual(service.state_key, SELF_KEY)
        self.assertNotEqual(service.state_key, "self_action:state")
        self.assertNotEqual(service.state_key, AI_CHAT_KEY)

    def test_fresh_state_uses_initial_threshold(self) -> None:
        service = self.service()
        state = service.load_state()
        self.assertAlmostEqual(state.threshold, 0.35)
        self.assertIsNone(state.updated_at)
        self.assertAlmostEqual(service.current_threshold(NOON), 0.35)

    def test_corrupt_state_falls_back_with_a_warning(self) -> None:
        self.db.set_meta(SELF_KEY, "{not json")
        service = self.service()
        with self.assertLogs("fatfish.self_action.service", level="WARNING"):
            state = service.load_state()
        self.assertAlmostEqual(state.threshold, 0.35)
        self.assertIsNone(state.updated_at)

    def test_disabled_wins_over_everything(self) -> None:
        service = self.service(enabled=False)
        allowed, reason, meta = service.can_run(1.0, now=NOON)
        self.assertFalse(allowed)
        self.assertEqual(reason, "self_action_disabled")
        self.assertIn("threshold", meta)

    def test_quiet_hours_across_midnight(self) -> None:
        service = self.service(quiet_hours=["23:00", "08:00"], initial_threshold=0.0,
                               threshold_floor=0.0)
        _, reason, _ = service.can_run(0.9, now=NIGHT)  # 02:00, inside the window
        self.assertEqual(reason, "quiet_hours")
        allowed, reason, _ = service.can_run(0.9, now=NOON)
        self.assertTrue(allowed, reason)

    def test_empty_quiet_hours_never_blocks(self) -> None:
        service = self.service(quiet_hours=[], initial_threshold=0.0, threshold_floor=0.0)
        allowed, reason, _ = service.can_run(0.9, now=NIGHT)
        self.assertTrue(allowed, reason)

    def test_daily_cap_blocks_when_reached(self) -> None:
        service = self.service(daily_cap=2)
        add_evidence(self.db, created_at=NOON - 30)
        add_evidence(self.db, created_at=NOON - 20)
        allowed, reason, meta = service.can_run(1.0, now=NOON)
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily_cap")
        self.assertEqual(meta["daily_count"], 2)

    def test_daily_cap_zero_means_no_cap(self) -> None:
        service = self.service(daily_cap=0, initial_threshold=0.0, threshold_floor=0.0)
        for _ in range(20):
            add_evidence(self.db, created_at=NOON - 30)
        allowed, reason, meta = service.can_run(1.0, now=NOON)
        self.assertTrue(allowed, reason)
        self.assertEqual(meta["daily_count"], 20)

    def test_below_threshold_refused_and_equal_allowed(self) -> None:
        service = self.service()
        allowed, reason, meta = service.can_run(0.34, now=NOON)
        self.assertFalse(allowed)
        self.assertEqual(reason, "below_threshold")
        self.assertAlmostEqual(meta["desire"], 0.34)
        self.assertAlmostEqual(meta["threshold"], 0.35)
        allowed, reason, _ = service.can_run(0.35, now=NOON)
        self.assertTrue(allowed, reason)

    def test_missing_desire_counts_as_zero(self) -> None:
        service = self.service()
        allowed, reason, _ = service.can_run(None, now=NOON)
        self.assertFalse(allowed)
        self.assertEqual(reason, "below_threshold")

    def test_meta_carries_the_numbers_the_gate_used(self) -> None:
        service = self.service(initial_threshold=0.0, threshold_floor=0.0)
        _, _, meta = service.can_run(0.5, now=NOON)
        self.assertEqual(
            set(meta),
            {
                "threshold",
                "desire",
                "sent_last_hour",
                "daily_count",
                "decay_per_minute",
                "explicit",
            },
        )
        self.assertFalse(meta["explicit"])
        self.assertAlmostEqual(meta["decay_per_minute"], 0.05)

    def test_record_run_lifts_the_threshold(self) -> None:
        service = self.service()
        service.record_run("search_memory", desire=0.8, threshold=0.35, now=NOON)
        self.assertAlmostEqual(service.current_threshold(NOON), 0.45, places=6)

    def test_record_run_stops_at_the_ceiling(self) -> None:
        service = self.service()
        for _ in range(10):
            service.record_run("search_memory", desire=1.0, threshold=0.35, now=NOON)
        self.assertAlmostEqual(service.current_threshold(NOON), 0.90, places=6)

    def test_decay_applies_between_actions(self) -> None:
        service = self.service()
        service.record_run("search_memory", desire=0.8, threshold=0.35, now=NOON)
        later = NOON + 120  # 2 minutes * 0.05/min
        self.assertAlmostEqual(service.current_threshold(later), 0.35, places=6)

    def test_sent_last_hour_ignores_older_rows(self) -> None:
        service = self.service()
        add_evidence(self.db, created_at=NOON - 60)
        add_evidence(self.db, created_at=NOON - 7200)
        self.assertEqual(service.sent_last_hour(NOON), 1)

    def test_daily_count_ignores_yesterday_and_other_actions(self) -> None:
        service = self.service()
        add_evidence(self.db, created_at=NOON - 60)
        add_evidence(self.db, created_at=NOON - 86400)
        add_evidence(self.db, action="post_qzone", source="qzone", created_at=NOON - 30)
        self.assertEqual(service.daily_count(NOON), 1)


# --------------------------------------------------------------- runtime gate
class GateTests(Env):
    def test_all_four_refused_when_disabled(self) -> None:
        config, db, runtime = self.extra_env("disabled", enabled=False)
        self.addCleanup(db.close)
        runtime.memory, runtime.qzone = self.memory, self.qzone
        runtime.photo, runtime.bilibili = self.photo, self.bilibili
        for tool in FOUR:
            with self.subTest(tool=tool):
                result = runtime.execute_actions(
                    [TurnAction(tool, {**ARGS[tool], "desire": 1.0})],
                    session_id=SESSION,
                    reply="",
                    now=NOON,
                    trust=TrustLevel.PRIVATE,
                )[0]
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "self_action_disabled")
        self.assertEqual(self.fake_calls(), 0)
        self.assertEqual(len(decision_rows(db)), 4)
        self.assertEqual({row["outcome"] for row in decision_rows(db)}, {"self_action_disabled"})
        self.assertIsNone(db.get_meta(SELF_KEY, None))

    def test_below_threshold_refused_before_the_branch(self) -> None:
        result = self.run_action("search_memory", {"query": "海豚", "desire": 0.2})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "below_threshold")
        self.assertEqual(self.memory.calls, [])
        self.assertEqual(evidence_rows(self.db), [])
        rows = decision_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "search_memory")
        self.assertEqual(rows[0]["outcome"], "below_threshold")
        self.assertAlmostEqual(self.service().current_threshold(NOON), 0.35)

    def test_missing_desire_is_zero_at_runtime(self) -> None:
        result = self.run_action("search_memory", {"query": "海豚"})
        self.assertEqual(result["error"], "below_threshold")
        self.assertEqual(self.memory.calls, [])

    def test_at_threshold_runs_stores_evidence_and_lifts(self) -> None:
        result = self.run_action("search_memory", {"query": "海豚", "desire": 0.35})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.memory.calls), 1)
        rows = evidence_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "search_memory")
        self.assertIsNone(rows[0]["consumed_at"])
        self.assertAlmostEqual(self.service().current_threshold(NOON), 0.45, places=6)
        self.assertEqual(decision_rows(self.db), [])

    def test_quiet_hours_refused(self) -> None:
        config, db, runtime = self.extra_env(
            "quiet", quiet='["00:00", "23:59"]'
        )
        self.addCleanup(db.close)
        runtime.memory = self.memory
        result = runtime.execute_actions(
            [TurnAction("search_memory", {"query": "海豚", "desire": 1.0})],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
        )[0]
        self.assertEqual(result["error"], "quiet_hours")
        self.assertEqual(self.memory.calls, [])
        self.assertEqual(decision_rows(db)[0]["outcome"], "quiet_hours")

    def test_daily_cap_refused(self) -> None:
        config, db, runtime = self.extra_env("daily", daily_cap=2)
        self.addCleanup(db.close)
        add_evidence(db, created_at=NOON - 100)
        add_evidence(db, created_at=NOON - 50)
        result = runtime.execute_actions(
            [TurnAction("search_memory", {"query": "海豚", "desire": 1.0})],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
        )[0]
        self.assertEqual(result["error"], "daily_cap")
        self.assertEqual(decision_rows(db)[0]["outcome"], "daily_cap")
        self.assertEqual(len(evidence_rows(db)), 2)  # the pre-seeded rows only

    def test_per_turn_budget(self) -> None:
        actions = [
            TurnAction("search_memory", {"query": "海豚", "desire": 1.0}),
            TurnAction("read_qzone", {"refresh": True, "desire": 1.0}),
            TurnAction("search_videos", {"topic": "海洋", "desire": 1.0}),
        ]
        results = self.runtime.execute_actions(
            actions, session_id=SESSION, reply="", now=NOON, trust=TrustLevel.PRIVATE
        )
        self.assertEqual(
            [r.get("ok") for r in results], [True, True, False], results
        )
        self.assertEqual(results[2]["error"], "too_many_self_actions")
        self.assertEqual(self.bilibili.calls, [])
        self.assertEqual(len(evidence_rows(self.db)), 2)
        self.assertEqual(
            decision_rows(self.db)[-1]["outcome"], "too_many_self_actions"
        )

    def test_failed_action_does_not_lift_the_threshold(self) -> None:
        self.photo.ok = False
        result = self.run_action("make_photo", {"prompt": "鲸鱼", "desire": 1.0})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "comfyui_timeout")
        # The failure evidence is still written (L3's anti-hallucination rule),
        # but the threshold stays where it was: a failed attempt costs nothing.
        self.assertEqual(evidence_rows(self.db)[0]["source"], "photo_failure")
        self.assertAlmostEqual(self.service().current_threshold(NOON), 0.35)

    def test_policy_still_wins_over_the_gate(self) -> None:
        result = self.run_action(
            "search_memory", {"query": "海豚", "desire": 1.0}, trust=TrustLevel.GROUP
        )
        self.assertEqual(result["error"], "group_not_allowed")
        self.assertEqual(self.memory.calls, [])
        self.assertEqual(decision_rows(self.db), [])

    def test_non_self_actions_are_untouched(self) -> None:
        # With the gate configured off, a pre-existing action must still work
        # and must not write any self_action audit row.
        config, db, runtime = self.extra_env("legacy", enabled=False)
        self.addCleanup(db.close)
        result = runtime.execute_actions(
            [TurnAction("remember", {"text": "主人哥哥喜欢海豚", "type": "preference"})],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
        )[0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(decision_rows(db), [])
        self.assertEqual(evidence_rows(db), [])

    def test_bilibili_disabled_makes_no_request(self) -> None:
        config, db, runtime = self.extra_env("nobili", bilibili=False)
        self.addCleanup(db.close)
        runtime.bilibili = self.bilibili
        result = runtime.execute_actions(
            [TurnAction("search_videos", {"topic": "海洋", "desire": 1.0})],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
        )[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bilibili_disabled")
        self.assertEqual(self.bilibili.calls, [])
        self.assertEqual(evidence_rows(db), [])

    def test_self_action_tools_constant(self) -> None:
        # T-0072 added read_chat as the fifth self-action, T-0074 added
        # watch_video as the sixth; the original four keep their slots and
        # nothing outside the family may sneak in.
        self.assertIn("read_chat", set(SELF_ACTION_TOOLS))
        self.assertIn("watch_video", set(SELF_ACTION_TOOLS))
        self.assertTrue(set(FOUR).issubset(set(SELF_ACTION_TOOLS)))
        self.assertEqual(len(set(SELF_ACTION_TOOLS)), 6)


# ---------------------------------------------------------------- isolation
class IsolationTests(Env):
    env_kwargs = {"ai_chat": True}

    def ai_chat_service(self) -> AiChatService:
        return AiChatService(
            self.db,
            self.runtime.ai_chat_settings,
            now_fn=lambda: NOON,
            outbox=self.runtime.outbox,
        )

    def test_ai_chat_ceiling_does_not_move_self_action(self) -> None:
        chat = self.ai_chat_service()
        for i in range(10):
            chat.record_sent(
                f"同族消息{i}", desire=1.0, threshold=chat.current_threshold(NOON), now=NOON
            )
        self.assertAlmostEqual(chat.current_threshold(NOON), 0.95, places=6)
        self.assertIsNotNone(self.db.get_meta(AI_CHAT_KEY, None))

        service = self.service()
        # Never acted in this domain: still the untouched initial value.
        self.assertAlmostEqual(service.current_threshold(NOON), 0.35, places=6)
        self.assertIsNone(self.db.get_meta(SELF_KEY, None))

        # ...and it still judges by *its own* threshold, not the peer's.
        result = self.run_action("search_memory", {"query": "海豚", "desire": 0.40})
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(service.current_threshold(NOON), 0.45, places=6)
        self.assertAlmostEqual(chat.current_threshold(NOON), 0.95, places=6)

    def test_self_action_ceiling_does_not_move_ai_chat(self) -> None:
        chat = self.ai_chat_service()
        chat.record_sent("一条", desire=0.5, threshold=0.30, now=NOON)
        raw_before = self.db.get_meta(AI_CHAT_KEY, None)
        threshold_before = chat.current_threshold(NOON)
        self.assertIsNotNone(raw_before)

        service = self.service()
        for _ in range(10):
            service.record_run("search_memory", desire=1.0, threshold=0.35, now=NOON)
        self.assertAlmostEqual(service.current_threshold(NOON), 0.90, places=6)

        # The ai_chat key is byte-identical, and its threshold did not move.
        self.assertEqual(self.db.get_meta(AI_CHAT_KEY, None), raw_before)
        self.assertAlmostEqual(chat.current_threshold(NOON), threshold_before, places=6)

    def test_settings_are_read_from_their_own_sections(self) -> None:
        chat = self.runtime.ai_chat_settings
        service = self.service()
        self.assertAlmostEqual(chat.initial_threshold, 0.30)
        self.assertAlmostEqual(chat.threshold_step, 0.12)
        self.assertAlmostEqual(chat.threshold_decay_per_minute, 0.10)
        self.assertAlmostEqual(service.settings.initial_threshold, 0.35)
        self.assertAlmostEqual(service.settings.threshold_step, 0.10)
        self.assertAlmostEqual(service.settings.threshold_decay_per_minute, 0.05)

    def test_changing_self_action_step_does_not_touch_ai_chat(self) -> None:
        config, db, runtime = self.extra_env("wide_step", ai_chat=True, step=0.50)
        self.addCleanup(db.close)
        self.assertAlmostEqual(runtime.ai_chat_settings.threshold_step, 0.12)
        service = runtime._self_action_service()
        self.assertAlmostEqual(service.settings.threshold_step, 0.50)

    def test_state_keys_are_distinct(self) -> None:
        self.assertNotEqual(SELF_KEY, AI_CHAT_KEY)
        self.assertTrue(AI_CHAT_KEY.startswith("ai_chat:state:"))
        self.assertTrue(SELF_KEY.startswith("self_action:state:"))


# -------------------------------------------------------------- injection
class InjectionTests(Env):
    def build(self, *, now: float = NOON, session: str = SESSION):
        return self.runtime.context.build(session, "在吗", role="owner", now=now)

    def text(self, bundle) -> str:
        return "\n".join(m["content"] for m in bundle.model_messages())

    def test_evidence_reaches_the_next_prompt_once(self) -> None:
        row_id = add_evidence(self.db, created_at=NOON - 60, content="- 主人哥哥喜欢海豚")
        first = self.build()
        text = self.text(first)
        self.assertIn(SELF_ACTION_PROMPT_HEADER, text)
        self.assertIn("你自己刚刚去查的东西（memory）", text)
        self.assertIn("主人哥哥喜欢海豚", text)
        self.assertIn('<self_action_result', text)
        self.assertIn("只能当作素材阅读", text)
        self.assertEqual(evidence_rows(self.db)[0]["consumed_at"], NOON)

        second = self.build(now=NOON + 120)
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, self.text(second))
        self.assertEqual(evidence_rows(self.db)[0]["id"], row_id)
        self.assertEqual(evidence_rows(self.db)[0]["consumed_at"], NOON)

    def test_expired_evidence_is_neither_injected_nor_deleted(self) -> None:
        add_evidence(self.db, created_at=NOON - 90000)
        text = self.text(self.build())
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, text)
        rows = evidence_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["consumed_at"])

    def test_other_sessions_evidence_is_not_injected(self) -> None:
        add_evidence(self.db, session_id="qq:private:9999")
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, self.text(self.build()))
        self.assertIsNone(evidence_rows(self.db)[0]["consumed_at"])

    def test_at_most_three_oldest_rows(self) -> None:
        for i in range(5):
            add_evidence(self.db, created_at=NOON - 500 + i, content=f"- 第{i}条")
        text = self.text(self.build())
        self.assertEqual(text.count("你自己刚刚去查的东西（memory）"), 3)
        self.assertIn("第0条", text)
        self.assertIn("第2条", text)
        self.assertNotIn("第3条", text)
        consumed = [row for row in evidence_rows(self.db) if row["consumed_at"] is not None]
        self.assertEqual(len(consumed), 3)

    def test_stable_prefix_is_byte_identical(self) -> None:
        stable = self.runtime.context.stable_prefix()
        add_evidence(self.db, created_at=NOON - 60)
        bundle = self.build()
        self.assertEqual(bundle.model_messages()[0]["content"], stable)
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, stable)
        self.assertEqual(
            bundle.stable_prefix_hash, hashlib.sha256(stable.encode("utf-8")).hexdigest()
        )

    def test_injection_rides_runtime_context_not_the_tail(self) -> None:
        add_evidence(self.db, created_at=NOON - 60)
        bundle = self.build()
        by_name = {section.name: section.text for section in bundle.sections}
        self.assertIn(SELF_ACTION_PROMPT_HEADER, by_name["runtime_context"])
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, by_name["turn_tail"])
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, by_name["stable_prefix"])

    def test_no_evidence_means_no_block(self) -> None:
        self.assertNotIn(SELF_ACTION_PROMPT_HEADER, self.text(self.build()))


# ------------------------------------------------------------------- wording
class TurnTests(Env):
    fake_memory = False
    def plan(self, *, desire: float, tool: str = "search_memory") -> str:
        return json.dumps(
            {
                "messages": ["嗯，我在"],
                "actions": [{"tool": tool, "args": {**ARGS[tool], "desire": desire}}],
            },
            ensure_ascii=False,
        )

    def turn(self, *, desire: float, tool: str = "search_memory"):
        self.runtime.llm = FakeLLM(self.plan(desire=desire, tool=tool))
        self.runtime.voice = FakeVoice()
        return self.runtime.handle_user_message(SESSION, "帮我查查海豚的事", now=NOON)

    def test_refused_action_does_not_touch_the_text_reply(self) -> None:
        out = self.turn(desire=0.1)
        self.assertEqual(out["reply"], "嗯，我在")
        self.assertEqual(out["messages"], ["嗯，我在"])
        self.assertEqual(out["actions"][0]["tool"], "search_memory")
        self.assertFalse(out["actions"][0]["ok"])
        self.assertEqual(out["actions"][0]["error"], "below_threshold")
        self.assertEqual(evidence_rows(self.db), [])

    def test_vague_lookup_still_goes_through_the_gate(self) -> None:
        # 「帮我查查海豚的事」没指向"记忆/以前"，不算主人指定了这个动作，按非豁免走。
        # 真·豁免（「你搜一下你以前的记录」）的语义与接线在
        # tests/test_self_action_exempt.py 里单独钉死（2026-10-26 boss 拍板改为允许豁免）。
        out = self.turn(desire=0.1)
        self.assertFalse(out["actions"][0]["ok"])
        self.assertEqual(out["actions"][0]["error"], "below_threshold")
        self.assertEqual(evidence_rows(self.db), [])

    def test_willing_turn_runs_and_lifts(self) -> None:
        out = self.turn(desire=0.9)
        self.assertTrue(out["actions"][0]["ok"], out["actions"])
        self.assertEqual(out["reply"], "嗯，我在")
        self.assertEqual(len(evidence_rows(self.db)), 1)
        self.assertAlmostEqual(self.service().current_threshold(NOON), 0.45, places=6)


if __name__ == "__main__":
    unittest.main()
