"""T-0023 share-desire model: the ``share_with_owner`` action, the wakeup
wiring that delivers her own words, and the desire-threshold auto-publish
matrix end to end.

Offline only: the OneBot client is faked, every credential-looking string is
synthetic, and the databases live in temp dirs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.platforms.qq import QQAdapter
from fatfish.qzone.posts import normalize_desire
from fatfish.runtime import ALLOWED_ACTIONS, Runtime, TurnAction, parse_turn_plan
from fatfish.security import TrustLevel
from fatfish.wakeup import WakeupScheduler

# 2026-10-24 14:00 / 23:50 in UTC+8: deterministic against both the real
# ZoneInfo and the Windows UTC+8 fallback in clock.get_timezone.
TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
NIGHT = datetime(2026, 10, 24, 23, 50, tzinfo=TZ8).timestamp()
SHARE_TEXT = "主人哥哥！我刚看完的那部海豚纪录片，尾巴都看直了！"


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'share_desire.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[wakeup]
enabled = true
min_delay_seconds = 60
max_per_day = 12

[qzone]
enabled = true
read_only = false
require_approval = false
auto_post_desire_threshold = 0.7
max_posts_per_day = 0
"""
    path = tmp / "share_desire.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


class ShareDesireTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


# ------------------------------------------------------------- action policy
class ShareWithOwnerActionTests(ShareDesireTestBase):
    def test_action_is_allowlisted_and_parses(self) -> None:
        self.assertIn("share_with_owner", ALLOWED_ACTIONS)
        plan = parse_turn_plan(
            '{"messages":["在吗"],"actions":[{"tool":"share_with_owner",'
            '"args":{"text":"想给你看个东西","desire":0.6}}]}'
        )
        assert plan is not None
        self.assertTrue(plan.parsed)
        self.assertEqual(
            [TurnAction("share_with_owner", {"text": "想给你看个东西", "desire": 0.6})],
            plan.actions,
        )

    def test_schedules_wakeup_with_her_own_words(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("share_with_owner", {"text": SHARE_TEXT, "desire": 0.6})],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual("share_with_owner", results[0]["result"]["intent"])
        wakeup = self.runtime.wakeup.get(results[0]["result"]["id"])
        assert wakeup is not None
        self.assertEqual("share_with_owner", wakeup.intent)
        self.assertEqual("qq:private:1001", wakeup.session_id)
        self.assertEqual(SHARE_TEXT, wakeup.payload.get("text"))
        self.assertEqual(0.6, wakeup.payload.get("desire"))
        # ~90s out: past the scheduler's 60s clamp, still this conversation.
        fire_delta = wakeup.fire_at - NOON
        self.assertGreaterEqual(fire_delta, 60)
        self.assertLessEqual(fire_delta, 95)

    def test_desire_is_recorded_only_and_optional(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("share_with_owner", {"text": SHARE_TEXT, "desire": "garbage"})],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        self.assertTrue(results[0]["ok"])
        wakeup = self.runtime.wakeup.get(results[0]["result"]["id"])
        assert wakeup is not None
        self.assertIsNone(wakeup.payload.get("desire"))

    def test_missing_text_is_rejected(self) -> None:
        for args in ({}, {"text": ""}, {"text": "   "}):
            results = self.runtime.execute_actions(
                [TurnAction("share_with_owner", args)],
                session_id="qq:private:1001",
                reply="",
                now=NOON,
            )
            self.assertFalse(results[0]["ok"], args)
            self.assertEqual("missing_text", results[0]["error"], args)
        self.assertEqual([], self.runtime.wakeup.list())

    def test_group_trust_is_rejected(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("share_with_owner", {"text": SHARE_TEXT})],
            session_id="qq:group:5555",
            reply="",
            now=NOON,
            trust=TrustLevel.GROUP,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual("group_not_allowed", results[0]["error"])
        self.assertEqual([], self.runtime.wakeup.list())

    def test_third_call_in_one_turn_is_rejected(self) -> None:
        action = TurnAction("share_with_owner", {"text": SHARE_TEXT})
        results = self.runtime.execute_actions(
            [action, action, action],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertFalse(results[2]["ok"])
        self.assertEqual("too_many_tool_calls", results[2]["error"])
        self.assertEqual(2, len(self.runtime.wakeup.list()))

    def test_daily_limit_reports_clean_error(self) -> None:
        self.config._data["wakeup"]["max_per_day"] = 0
        results = self.runtime.execute_actions(
            [TurnAction("share_with_owner", {"text": SHARE_TEXT})],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        self.assertFalse(results[0]["ok"])
        self.assertIn("limit", results[0]["error"])
        self.assertEqual([], self.runtime.wakeup.list())


# ------------------------------------------------------- wakeup -> delivery
class _FakeOneBotClient:
    def __init__(self, *args, **kwargs):
        self.connected = True
        self.sent: list[tuple[str, str, str]] = []

    def connect(self):
        return self

    def close(self):
        self.connected = False

    def send_private_text(self, user_id, text):
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def send_group_text(self, group_id, text):
        self.sent.append(("group", str(group_id), str(text)))
        return "echo-group"


class WakeupDeliveryWiringTests(ShareDesireTestBase):
    """The load-bearing wiring: payload text must reach QQ verbatim, ritual
    wakeups keep their templates, quiet hours still defer everything."""

    def _deliver(self):
        """Build the adapter on the faked OneBot client and flush the outbox."""
        adapter = QQAdapter(self.runtime, self.config)
        adapter.client_factory = _FakeOneBotClient  # type: ignore[assignment]
        results = self.runtime.process_outbox(handlers=adapter.handlers())
        return adapter, results

    def test_share_wakeup_delivers_original_text(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("share_with_owner", {"text": SHARE_TEXT, "desire": 0.6})],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        wakeup_id = results[0]["result"]["id"]
        wakeup = self.runtime.wakeup.get(wakeup_id)
        assert wakeup is not None
        outbox = self.runtime.process_due_wakeups(now=wakeup.fire_at + 1)
        self.assertTrue(any(r.get("acted") for r in outbox), outbox)
        row = self.db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='proactive_message'"
        )
        assert row is not None
        payload = json.loads(row["payload_json"])
        # The whole point: her words, not a reason template.
        self.assertEqual(SHARE_TEXT, payload["text"])
        self.assertEqual("qq:private:1001", payload["session_id"])
        # And the QQ adapter actually sends exactly that text.
        adapter, delivery = self._deliver()
        self.assertEqual("done", delivery[0]["status"], delivery)
        client = adapter.client
        assert isinstance(client, _FakeOneBotClient)
        self.assertEqual([("private", "1001", SHARE_TEXT)], client.sent)

    def test_ritual_wakeup_keeps_template_text(self) -> None:
        self.runtime.wakeup.schedule(
            NOON + 90,
            intent="check_user",
            reason="早安：问主人哥哥今天怎么样",
            session_id="qq:private:1001",
            now=NOON,
        )
        self.runtime.process_due_wakeups(now=NOON + 120)
        row = self.db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='proactive_message'"
        )
        assert row is not None
        payload = json.loads(row["payload_json"])
        self.assertIn("早安", payload["text"])

    def test_night_share_delivers_after_quiet_hours(self) -> None:
        # Scheduled at 23:50 -> the scheduler pushes fire_at to 08:00 next
        # morning; once the window is over the share goes out untouched.
        results = self.runtime.execute_actions(
            [TurnAction("share_with_owner", {"text": SHARE_TEXT})],
            session_id="qq:private:1001",
            reply="",
            now=NIGHT,
        )
        self.assertTrue(results[0]["ok"], results)
        wakeup = self.runtime.wakeup.get(results[0]["result"]["id"])
        assert wakeup is not None
        from fatfish.clock import to_local

        local = to_local(wakeup.fire_at, self.config.timezone_name)
        self.assertEqual(8, local.hour)
        self.assertEqual(0, local.minute)
        # Still night, not due yet: nothing happens, the row stays pending.
        self.assertEqual(
            [], self.runtime.process_due_wakeups(now=wakeup.fire_at - 3600)
        )
        # 08:00: the window is over, she finally says it.
        outbox = self.runtime.process_due_wakeups(now=wakeup.fire_at)
        self.assertTrue(any(r.get("acted") for r in outbox), outbox)
        adapter, delivery = self._deliver()
        self.assertEqual("done", delivery[0]["status"], delivery)
        client = adapter.client
        assert isinstance(client, _FakeOneBotClient)
        self.assertEqual([("private", "1001", SHARE_TEXT)], client.sent)

    def test_gate_backstop_cancels_urgent_wakeup_inside_quiet_hours(self) -> None:
        # Backstop for payloads that bypass the scheduler's deferral
        # (urgent=True): the gate still refuses to send inside the window.
        self.runtime.wakeup.schedule(
            NIGHT + 90,
            intent="share_with_owner",
            reason="分享：深夜突发",
            session_id="qq:private:1001",
            payload={"text": SHARE_TEXT},
            urgent=True,
            now=NIGHT,
        )
        outbox = self.runtime.process_due_wakeups(now=NIGHT + 120)
        self.assertEqual(1, len(outbox), outbox)
        self.assertFalse(outbox[0]["acted"])
        self.assertEqual("quiet_hours", outbox[0]["reason"])
        self.assertIsNone(
            self.db.query_one("SELECT id FROM outbox WHERE kind='proactive_message'")
        )

    def test_wakeup_payload_survives_a_broken_text_field(self) -> None:
        # A wakeup whose payload text is whitespace falls back: ritual
        # template first, then the reason itself -- never empty, never silent.
        self.runtime.wakeup.schedule(
            NOON + 90,
            intent="share_with_owner",
            reason="分享：（丢字段）",
            session_id="qq:private:1001",
            payload={"text": "   "},
            now=NOON,
        )
        self.runtime.process_due_wakeups(now=NOON + 120)
        # T-0031: no template matches and the payload text is empty -- the
        # reason itself is the fallback. A fired wakeup never delivers
        # nothing (that is a broken promise), and never an empty message.
        row = self.db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='proactive_message'"
        )
        self.assertIsNotNone(row)
        text = str(json.loads(row["payload_json"]).get("text") or "")
        self.assertEqual(text, "分享：（丢字段）")


# ------------------------------------------------- desire threshold (service)
class NormalizeDesireTests(unittest.TestCase):
    def test_valid_values_pass_through(self) -> None:
        for raw, expected in ((0, 0.0), (1, 1.0), ("0.85", 0.85), (0.333, 0.333)):
            self.assertEqual(expected, normalize_desire(raw), raw)

    def test_invalid_values_are_none(self) -> None:
        for raw in (None, "", "abc", True, False, 1.5, -0.01, float("nan"), float("inf"), [1], {"d": 1}):
            self.assertIsNone(normalize_desire(raw), repr(raw))


class SchedulerDirectShareTests(ShareDesireTestBase):
    """The scheduler layer itself: min-delay clamp and payload round-trip."""

    def test_schedule_clamps_only_below_min_delay(self) -> None:
        scheduler = WakeupScheduler(self.db, self.config)
        wakeup = scheduler.schedule(
            NOON + 30,  # below the 60s clamp
            intent="share_with_owner",
            payload={"text": SHARE_TEXT},
            session_id="qq:private:1001",
            now=NOON,
        )
        self.assertEqual(NOON + 60, wakeup.fire_at)
        self.assertEqual(SHARE_TEXT, wakeup.payload["text"])

    def test_share_intent_counts_toward_daily_budget(self) -> None:
        scheduler = WakeupScheduler(self.db, self.config)
        self.config._data["wakeup"]["max_per_day"] = 2
        scheduler.schedule(
            NOON + 90, intent="share_with_owner", payload={"text": "1"}, now=NOON
        )
        scheduler.schedule(
            NOON + 90, intent="share_with_owner", payload={"text": "2"}, now=NOON
        )
        with self.assertRaises(ValueError):
            scheduler.schedule(
                NOON + 90, intent="share_with_owner", payload={"text": "3"}, now=NOON
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
