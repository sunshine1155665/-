"""T-0029: the continue-policy engine (撒娇阶梯) -- pure engine + wiring.

Offline only: LLM / TTS / voice policy are faked, no network, every database
lives in a temp dir. Boss-approved spec (2026-10-25):

* three signals decide whether she comes back on her own: an open loop
  (``open_loops`` is the source of truth, 7-day window), her own stated
  desire (>=0.5), and the other side's stated will (chat_stop / busy /
  topic_change -- keyword fast path + LLM tag, last write wins);
* the ladder is +1/+10/+30 minutes, rung count 1-3 scaled by desire;
* its own lane: no ProactiveGate 90-minute pool, no ``wakeup.max_per_day``,
  per-session daily cap instead (default 6 delivered rungs);
* cancel-on-reply kills the whole ladder; a rung due in quiet hours defers
  to the morning with its rung count intact; her silence collapses the
  ladder without muting the loop; an LLM outage pushes a rung once;
* an active stop flag silences every outbound channel of that session:
  rungs, rituals/share wakeups, share actions, outbox delivery.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.continue_policy import (
    ContinuePolicy,
    classify_stop_keywords,
    clamp_desire,
)
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.owner import OwnerRegistry
from fatfish.runtime import Runtime, parse_turn_plan
from fatfish.tts import TTSResult
from fatfish.voice import VoiceDecision

TZ8 = timezone(timedelta(hours=8))
# 2026-10-24 14:00 / 23:31 / 2026-10-25 08:00 local (UTC+8): deterministic
# against both real ZoneInfo and the Windows UTC+8 fallback in clock.
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
QUIET = datetime(2026, 10, 24, 23, 31, tzinfo=TZ8).timestamp()
MORNING = datetime(2026, 10, 25, 8, 0, tzinfo=TZ8).timestamp()

OWNER_UIN = "10001"
OWNER_SESSION = f"qq:private:{OWNER_UIN}"
FRIEND_UIN = "20002"
FRIEND_SESSION = f"qq:private:{FRIEND_UIN}"
SILENCE = "【沉默】"


def plan_with(*, desire: Any = None, thread_open: bool = True, wants: str | None = None) -> str:
    payload: dict[str, Any] = {"messages": ["嗯，本鲸鱼娘在听"]}
    if desire is not None or thread_open:
        payload["continue"] = {"thread_open": thread_open, "desire": desire}
    if wants:
        payload["user_wants"] = wants
    return json.dumps(payload, ensure_ascii=False)


class FakeLLM:
    available = True

    def __init__(self, texts):
        self.texts = [str(t) for t in texts]
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, session_id=None):
        self.calls.append({"messages": messages, "session_id": session_id})
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return LLMResult(
            text=text, usage=LLMUsage(prompt_tokens=10, completion_tokens=4),
            model="fake-model",
        )


class DeadLLM(FakeLLM):
    available = False


class FakeVoicePolicy:
    def decide(self, text, **kwargs):
        return VoiceDecision("text_only", 0.0, ["fake"])

    def record(self, decision, **kwargs):
        pass


class FakeTTS:
    def synthesize(self, text, *, emotion=None):
        return TTSResult(True, audio_path="fake.wav")

    def synthesize_segments(self, text, *, emotion=None):
        return [(text[:10], TTSResult(True, audio_path="seg.wav"))]


def make_env(
    tmp: Path,
    *,
    bind_owner: bool = True,
    bind_friend: bool = False,
    extra: dict | None = None,
):
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "continue.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        "[qzone]",
        "enabled = false",
        "[bilibili]",
        "enabled = false",
        "[sticker]",
        "enabled = false",
        "[voice]",
        "enabled = false",
        "[tts]",
        "enabled = false",
    ]
    sections: dict[str, list[str]] = {}

    def _toml_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return f'"{value}"'
        return str(value)

    for key, value in (extra or {}).items():
        if "." in key:
            section, name = key.split(".", 1)
            sections.setdefault(section, []).append(f"{name} = {_toml_value(value)}")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    for section, rows in sections.items():
        lines.append(f"[{section}]")
        lines.extend(rows)
    path = tmp / "continue.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    runtime = Runtime(db, config)
    runtime.voice = FakeVoicePolicy()
    runtime.tts = FakeTTS()
    if bind_owner:
        OwnerRegistry(db, config).bind(OWNER_SESSION, "主人哥哥")
    if bind_friend:
        OwnerRegistry(db, config).bind(FRIEND_SESSION, "姐姐", role="friend")
    return config, db, runtime


class ContinuePolicyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_env(self, **kwargs):
        config, db, runtime = make_env(self.tmp, **kwargs)
        self.addCleanup(db.close)
        return config, db, runtime

    def pending_rungs(self, db: Database) -> list:
        return db.query(
            "SELECT * FROM wakeups WHERE intent='self_continue' AND status='pending'"
        )

    def decisions(self, db: Database, kind: str = "continue_policy") -> list[dict]:
        rows = db.query(
            "SELECT * FROM decision_log WHERE kind=? ORDER BY id", (kind,)
        )
        out = []
        for row in rows:
            try:
                payload = json.loads(row["output_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            out.append({"action": row["action"], "outcome": row["outcome"], **payload})
        return out

    def actions(self, db: Database) -> list[tuple[str, str]]:
        return [(d["action"], d["outcome"]) for d in self.decisions(db)]


# --------------------------------------------------------------- pure engine
class EngineUnitTests(ContinuePolicyTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.config, self.db, _ = self.make_env()
        self.policy = ContinuePolicy(self.db, self.config)

    def test_rung_count_scales_with_desire(self) -> None:
        # shipped thresholds: coax_desire_low=0.35 / coax_desire_high=0.60
        self.assertEqual(self.policy.rung_count(None), 1)
        self.assertEqual(self.policy.rung_count(0.3), 1)
        self.assertEqual(self.policy.rung_count(0.35), 2)
        self.assertEqual(self.policy.rung_count(0.59), 2)
        self.assertEqual(self.policy.rung_count(0.6), 3)
        self.assertEqual(self.policy.rung_count(0.99), 3)

    def test_arm_requires_thread_or_desire(self) -> None:
        decision = self.policy.should_arm(
            OWNER_SESSION, now=NOON, desire=None, thread_open=False
        )
        self.assertEqual(decision.action, "none")
        self.assertEqual(decision.reason, "no_thread_no_desire")

    def test_arm_via_desire_and_via_loop(self) -> None:
        via_desire = self.policy.should_arm(
            OWNER_SESSION, now=NOON, desire=0.7, thread_open=False
        )
        self.assertEqual(via_desire.action, "arm")
        self.assertEqual(via_desire.reason, "desire")
        # 0.7 >= coax_desire_high (0.60) -> the full three-rung ladder
        self.assertEqual(via_desire.rungs, 3)

        self.db.execute(
            "INSERT INTO open_loops(id, text, status, session_id, subject, created_at) "
            "VALUES('loop-1', '约好晚上聊新番', 'open', ?, '主人哥哥', ?)",
            (OWNER_SESSION, NOON - 60),
        )
        via_loop = self.policy.should_arm(
            OWNER_SESSION, now=NOON, desire=None, thread_open=False
        )
        self.assertEqual(via_loop.action, "arm")
        self.assertEqual(via_loop.reason, "open_loop")
        self.assertEqual(via_loop.loop_id, "loop-1")
        # loop wins even when she reports no desire of her own
        self.assertEqual(via_loop.rungs, 1)

    def test_loop_older_than_window_is_not_a_thread(self) -> None:
        self.db.execute(
            "INSERT INTO open_loops(id, text, status, session_id, subject, created_at) "
            "VALUES('loop-old', '老话题', 'open', ?, '主人哥哥', ?)",
            (OWNER_SESSION, NOON - 8 * 86400),
        )
        decision = self.policy.should_arm(
            OWNER_SESSION, now=NOON, desire=None, thread_open=False
        )
        self.assertEqual(decision.action, "none")

    def test_chat_stop_blocks_until_cleared(self) -> None:
        self.policy.set_stop_flag(OWNER_SESSION, "chat_stop", now=NOON)
        # chat_stop has no TTL: still active an hour later
        self.assertEqual(
            self.policy.stop_flag(OWNER_SESSION, now=NOON + 3600), "chat_stop"
        )
        blocked = self.policy.should_arm(
            OWNER_SESSION, now=NOON + 3600, desire=0.9, thread_open=True
        )
        self.assertEqual(blocked.action, "none")
        self.assertIn("stop_flag", blocked.reason)
        # the other side speaking lifts it
        self.policy.clear_stop_flag(OWNER_SESSION)
        self.assertIsNone(self.policy.stop_flag(OWNER_SESSION, now=NOON + 3601))

    def test_busy_flag_expires_on_its_own(self) -> None:
        self.policy.set_stop_flag(OWNER_SESSION, "busy", now=NOON)
        self.assertEqual(self.policy.stop_flag(OWNER_SESSION, now=NOON + 60), "busy")
        # default busy_minutes=120
        self.assertIsNone(self.policy.stop_flag(OWNER_SESSION, now=NOON + 121 * 60))

    def test_topic_change_mutes_the_latest_loop_for_the_day(self) -> None:
        self.db.execute(
            "INSERT INTO open_loops(id, text, status, session_id, subject, created_at) "
            "VALUES('loop-2', '聊到一半的话题', 'open', ?, '主人哥哥', ?)",
            (OWNER_SESSION, NOON - 60),
        )
        self.policy.set_stop_flag(OWNER_SESSION, "topic_change", now=NOON)
        self.assertIsNone(self.policy.stop_flag(OWNER_SESSION, now=NOON))
        # the muted loop no longer reads as an open thread...
        decision = self.policy.should_arm(
            OWNER_SESSION, now=NOON, desire=None, thread_open=False
        )
        self.assertEqual(decision.action, "none")
        # ...but her own desire still arms (the ladder hook just can't be that loop)
        again = self.policy.should_arm(
            OWNER_SESSION, now=NOON, desire=0.8, thread_open=False
        )
        self.assertEqual(again.action, "arm")
        # ...and the mute dies with the day
        tomorrow = self.policy.should_arm(
            OWNER_SESSION, now=NOON + 86400, desire=None, thread_open=False
        )
        self.assertEqual(tomorrow.action, "arm")

    def test_daily_cap_blocks_new_ladders_when_positive(self) -> None:
        # Explicit config on purpose: the shipped profiles now ship 0 (unlimited).
        config, db, _ = self.make_env(extra={"chat.coax_max_rungs_per_day": 6})
        policy = ContinuePolicy(db, config)
        self.assertEqual(policy.max_rungs_per_day, 6)
        for _ in range(6):
            policy.count_rung(OWNER_SESSION, now=NOON)
        decision = policy.should_arm(
            OWNER_SESSION, now=NOON, desire=0.9, thread_open=True
        )
        self.assertEqual(decision.action, "none")
        self.assertEqual(decision.reason, "daily_cap")
        # the cap is per day
        fresh = policy.should_arm(
            OWNER_SESSION, now=NOON + 86400, desire=0.9, thread_open=True
        )
        self.assertEqual(fresh.action, "arm")

    def test_zero_daily_cap_means_unlimited(self) -> None:
        config, db, _ = self.make_env(extra={"chat.coax_max_rungs_per_day": 0})
        policy = ContinuePolicy(db, config)
        self.assertEqual(policy.max_rungs_per_day, 0)
        for _ in range(50):
            policy.count_rung(OWNER_SESSION, now=NOON)
        # 0 is "no quota", not "quota of zero": a full day of rungs never
        # turns into daily_cap.
        for _ in range(10):
            decision = policy.should_arm(
                OWNER_SESSION, now=NOON, desire=0.9, thread_open=True
            )
            self.assertEqual(decision.action, "arm")
            self.assertEqual(decision.reason, "desire")

    def test_missing_daily_cap_keeps_the_documented_default(self) -> None:
        # The <0 key is gone: the historical default (6) must still apply.
        config, db, _ = self.make_env()
        self.assertIn("chat", config._data)
        config._data["chat"].pop("coax_max_rungs_per_day", None)
        policy = ContinuePolicy(db, config)
        self.assertEqual(policy.max_rungs_per_day, 6)

    def test_next_rung_delay_follows_intervals(self) -> None:
        self.assertEqual(self.policy.next_rung_delay(1), 10 * 60)
        self.assertEqual(self.policy.next_rung_delay(2), 30 * 60)
        self.assertIsNone(self.policy.next_rung_delay(3))
        self.assertIsNone(self.policy.next_rung_delay(0))

    def test_disabled_engine_never_arms(self) -> None:
        config, db, _ = self.make_env(extra={"chat.continue_policy_enabled": False})
        policy = ContinuePolicy(db, config)
        decision = policy.should_arm(
            OWNER_SESSION, now=NOON, desire=0.9, thread_open=True
        )
        self.assertEqual(decision.action, "none")
        self.assertEqual(decision.reason, "disabled")

    def test_classify_stop_keywords(self) -> None:
        self.assertEqual(classify_stop_keywords("别聊了，我想静一静"), "chat_stop")
        self.assertEqual(classify_stop_keywords("不要说话"), "chat_stop")
        self.assertEqual(classify_stop_keywords("我在忙，等会再说"), "busy")
        self.assertEqual(classify_stop_keywords("换个话题吧"), "topic_change")
        self.assertIsNone(classify_stop_keywords("今天吃了吗"))
        self.assertIsNone(classify_stop_keywords(""))

    def test_clamp_desire(self) -> None:
        self.assertEqual(clamp_desire(0.5), 0.5)
        self.assertEqual(clamp_desire("0.7"), 0.7)
        self.assertEqual(clamp_desire(1.7), 1.0)
        self.assertEqual(clamp_desire(-3), 0.0)
        self.assertIsNone(clamp_desire(None))
        self.assertIsNone(clamp_desire("很多"))
        self.assertIsNone(clamp_desire(float("nan")))
        self.assertIsNone(clamp_desire(True))


class ParseTurnPlanTagTests(unittest.TestCase):
    def test_tags_extracted_from_json(self) -> None:
        raw = json.dumps(
            {
                "messages": ["好呀"],
                "continue": {"thread_open": True, "desire": 0.6},
                "user_wants": "busy",
            },
            ensure_ascii=False,
        )
        plan = parse_turn_plan(raw)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.continue_intent, {"thread_open": True, "desire": 0.6})
        self.assertEqual(plan.user_wants, "busy")

    def test_garbage_tags_read_as_missing(self) -> None:
        raw = json.dumps(
            {
                "messages": ["好呀"],
                "continue": {"thread_open": "yes", "desire": "特别想"},
                "user_wants": "sleep",
            },
            ensure_ascii=False,
        )
        plan = parse_turn_plan(raw)
        assert plan is not None
        # "yes" -> truthy bool; desire garbage -> None; user_wants validated
        self.assertEqual(plan.continue_intent, {"thread_open": True, "desire": None})
        self.assertIsNone(plan.user_wants)

    def test_plain_text_fallback_has_no_tags(self) -> None:
        plan = parse_turn_plan("就直接说话")
        assert plan is not None
        self.assertIsNone(plan.continue_intent)
        self.assertIsNone(plan.user_wants)
        self.assertFalse(plan.parsed)


# ----------------------------------------------------------- runtime wiring
class LadderWiringTests(ContinuePolicyTestBase):
    def test_no_signal_no_ladder_replaces_always_hang(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([json.dumps({"messages": ["嗯"]}, ensure_ascii=False)])

        runtime.handle_user_message(OWNER_SESSION, "在吗", now=NOON)

        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(
            ("ladder_skipped", "no_thread_no_desire"), self.actions(db)
        )

    def test_desire_arms_first_rung_with_payload(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([plan_with(desire=0.7)])

        runtime.handle_user_message(OWNER_SESSION, "在吗", now=NOON)

        pending = self.pending_rungs(db)
        self.assertEqual(len(pending), 1)
        payload = json.loads(pending[0]["payload_json"])
        self.assertEqual(payload["origin"], "coax")
        self.assertEqual(payload["rung"], 1)
        # desire 0.7 >= coax_desire_high (0.60) -> three rungs
        self.assertEqual(payload["max_rungs"], 3)
        self.assertIsNone(payload["loop_id"])
        # +1 minute == the scheduler's 60s floor
        self.assertEqual(pending[0]["fire_at"], NOON + 60)
        self.assertIn(("ladder_armed", "ok"), self.actions(db))

    def test_open_loop_arms_with_loop_id(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([json.dumps({"messages": ["好呀"], "actions": [
            {"tool": "open_loop", "args": {"text": "晚上聊新番"}}
        ]}, ensure_ascii=False)])

        runtime.handle_user_message(OWNER_SESSION, "晚上一起聊新番好不好", now=NOON)

        pending = self.pending_rungs(db)
        self.assertEqual(len(pending), 1)
        payload = json.loads(pending[0]["payload_json"])
        self.assertIsNotNone(payload["loop_id"])
        self.assertIn(("ladder_armed", "ok"), self.actions(db))

    def test_cancel_on_reply_kills_pending_rung(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([plan_with(desire=0.7), plan_with(desire=0.7)])

        runtime.handle_user_message(OWNER_SESSION, "在吗", now=NOON)
        first = self.pending_rungs(db)
        self.assertEqual(len(first), 1)

        runtime.handle_user_message(OWNER_SESSION, "来了来了", now=NOON + 30)

        alive = self.pending_rungs(db)
        self.assertEqual(len(alive), 1)
        self.assertNotEqual(alive[0]["id"], first[0]["id"])
        self.assertEqual(
            db.query_one("SELECT status FROM wakeups WHERE id=?", (first[0]["id"],))[
                "status"
            ],
            "cancelled",
        )
        self.assertIn(("cancelled_by_reply", "user_replied"), self.actions(db))

    def test_chat_stop_keyword_silences_the_session(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([plan_with(desire=0.7)])

        runtime.handle_user_message(OWNER_SESSION, "别聊了，让我静一静", now=NOON)

        # the flag lands before the end-of-turn arm decision: no rung at all
        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(("stop_flag_set", "chat_stop"), self.actions(db))
        # and it holds across turns until he speaks again
        self.assertEqual(
            runtime.continue_policy.stop_flag(OWNER_SESSION, now=NOON + 3600),
            "chat_stop",
        )

    def test_flag_lifts_when_he_speaks_again(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([plan_with(desire=0.7), plan_with(desire=0.7)])

        runtime.handle_user_message(OWNER_SESSION, "别聊了", now=NOON)
        runtime.handle_user_message(OWNER_SESSION, "我回来了，刚才在开会", now=NOON + 600)

        self.assertIsNone(
            runtime.continue_policy.stop_flag(OWNER_SESSION, now=NOON + 601)
        )
        self.assertEqual(len(self.pending_rungs(db)), 1)

    def test_busy_from_llm_tag_blocks_ladder_and_shares(self) -> None:
        config, db, runtime = self.make_env()
        share_plan = json.dumps(
            {
                "messages": ["好，那你忙"],
                "actions": [{"tool": "share_with_owner", "args": {"text": "主人哥哥你看这个"}}],
                "user_wants": "busy",
            },
            ensure_ascii=False,
        )
        runtime.llm = FakeLLM([share_plan])

        # neutral inbound text: the flag can only come from the LLM tag here
        result = runtime.handle_user_message(OWNER_SESSION, "先这样吧", now=NOON)

        # the share action is refused: quiet means every outbound channel
        share_result = next(r for r in result["actions"] if r["tool"] == "share_with_owner")
        self.assertFalse(share_result["ok"])
        self.assertEqual(share_result["error"], "stop_flag")
        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(("stop_flag_set", "busy"), self.actions(db))
        self.assertIn(("suppressed", "stop_flag"), self.actions(db))

    def test_share_to_friend_refused_while_friend_flagged(self) -> None:
        config, db, runtime = self.make_env(bind_friend=True)
        runtime.continue_policy.set_stop_flag(FRIEND_SESSION, "chat_stop", now=NOON)
        share_plan = json.dumps(
            {"messages": ["好呀"], "actions": [
                {"tool": "share_to_friend", "args": {"text": "姐姐你看这个"}}
            ]},
            ensure_ascii=False,
        )
        runtime.llm = FakeLLM([share_plan])

        result = runtime.handle_user_message(OWNER_SESSION, "去跟姐姐分享一下", now=NOON)

        friend_share = next(r for r in result["actions"] if r["tool"] == "share_to_friend")
        self.assertFalse(friend_share["ok"])
        self.assertEqual(friend_share["error"], "stop_flag")


class LadderFireTests(ContinuePolicyTestBase):
    def _arm(self, runtime, *, desire=0.4, thread_open=True, texts=None):
        runtime.llm = FakeLLM(texts or [plan_with(desire=desire, thread_open=thread_open)])
        runtime.handle_user_message(OWNER_SESSION, "在吗", now=NOON)
        return self.pending_rungs(runtime.db)[0]

    def _deliver(self, runtime, wakeup_row, *, now, reply_plan):
        runtime.llm = FakeLLM([reply_plan])
        return runtime.process_due_wakeups(now=now)

    def test_rung_delivery_uses_coax_prompt_and_own_lane(self) -> None:
        config, db, runtime = self.make_env()
        # desire 0.3 < coax_desire_low (0.35): exactly one rung
        row = self._arm(runtime, desire=0.3)
        rung_at = row["fire_at"]

        results = self._deliver(
            runtime, row, now=rung_at,
            reply_plan=json.dumps({"messages": ["那我们晚上接着说好不好"]}, ensure_ascii=False),
        )

        self.assertTrue(results[0]["acted"])
        self.assertEqual(results[0]["outcome"], "delivered")
        # the coax prompt rode the turn, on the hook's topic
        prompt = runtime.llm.calls[-1]["messages"][-1]["content"]
        self.assertIn("撒娇提醒", prompt)
        # delivery landed in the ladder's own ledger lane, not ProactiveGate's
        self.assertIn(("rung_delivered", "queued"), self.actions(db))
        self.assertEqual(self.decisions(db, "proactive_message"), [])
        # outbox still uses the shared delivery kind
        item = db.query_one("SELECT * FROM outbox ORDER BY id DESC LIMIT 1")
        self.assertEqual(item["kind"], "proactive_message")
        # one delivered rung, max_rungs=1: no next rung, no pending
        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(("rung_fired", "delivered"), self.actions(db))
        self.assertEqual(runtime.continue_policy.rungs_today(OWNER_SESSION, now=rung_at), 1)

    def test_full_ladder_1_10_30(self) -> None:
        config, db, runtime = self.make_env()
        row = self._arm(runtime, desire=0.9)  # three rungs
        t1 = row["fire_at"]
        say = json.dumps({"messages": ["在等你回我嘛"]}, ensure_ascii=False)

        runtime.llm = FakeLLM([say])
        runtime.process_due_wakeups(now=t1)
        second = self.pending_rungs(db)
        self.assertEqual(len(second), 1)
        payload = json.loads(second[0]["payload_json"])
        self.assertEqual(payload["rung"], 2)
        self.assertEqual(second[0]["fire_at"], t1 + 10 * 60)

        runtime.llm = FakeLLM([say])
        runtime.process_due_wakeups(now=t1 + 10 * 60)
        third = self.pending_rungs(db)
        self.assertEqual(json.loads(third[0]["payload_json"])["rung"], 3)
        self.assertEqual(third[0]["fire_at"], t1 + 10 * 60 + 30 * 60)

        runtime.llm = FakeLLM([say])
        runtime.process_due_wakeups(now=t1 + 40 * 60)
        # max_rungs=3 reached: the ladder is exhausted, nothing pending
        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(("ladder_exhausted", "ok"), self.actions(db))
        self.assertEqual(
            runtime.continue_policy.rungs_today(OWNER_SESSION, now=t1 + 40 * 60), 3
        )

    def test_exhausted_ladder_mutes_the_loop_for_the_day(self) -> None:
        config, db, runtime = self.make_env()
        loop_plan = json.dumps({"messages": ["好呀"], "actions": [
            {"tool": "open_loop", "args": {"text": "晚上聊新番"}}
        ]}, ensure_ascii=False)
        runtime.llm = FakeLLM([loop_plan])
        runtime.handle_user_message(OWNER_SESSION, "晚上一起聊新番", now=NOON)
        row = self.pending_rungs(db)[0]

        say = json.dumps({"messages": ["在等你回我嘛"]}, ensure_ascii=False)
        runtime.llm = FakeLLM([say, say])
        runtime.process_due_wakeups(now=row["fire_at"])
        # loop-armed ladder has max_rungs=1 (no desire reported): exhausted now

        # the loop is muted for today: a fresh turn with the loop still open
        # but no fresh signal does not re-arm
        runtime.llm = FakeLLM([json.dumps({"messages": ["嗯"]}, ensure_ascii=False)])
        runtime.handle_user_message(OWNER_SESSION, "在吗", now=row["fire_at"] + 60)
        self.assertEqual(self.pending_rungs(db), [])

    def test_silence_collapses_ladder_without_mute(self) -> None:
        config, db, runtime = self.make_env()
        row = self._arm(runtime, desire=0.9)

        results = self._deliver(runtime, row, now=row["fire_at"], reply_plan=SILENCE)

        self.assertEqual(results[0]["outcome"], "silence")
        self.assertIsNone(db.query_one("SELECT * FROM outbox"))
        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(("rung_fired", "silence"), self.actions(db))
        # silence did NOT consume a rung of the daily cap
        self.assertEqual(
            runtime.continue_policy.rungs_today(OWNER_SESSION, now=row["fire_at"]), 0
        )

    def test_llm_outage_pushes_rung_once(self) -> None:
        config, db, runtime = self.make_env()
        row = self._arm(runtime, desire=0.9)
        due = row["fire_at"]

        runtime.llm = DeadLLM(["x"])
        results = runtime.process_due_wakeups(now=due)
        self.assertEqual(results[0]["outcome"], "llm_retry")
        retry = self.pending_rungs(db)
        self.assertEqual(len(retry), 1)
        self.assertEqual(json.loads(retry[0]["payload_json"])["llm_retryed"], True)
        # retry delay default 5 minutes
        self.assertEqual(retry[0]["fire_at"], due + 5 * 60)
        # no outbox, no rung consumed
        self.assertIsNone(db.query_one("SELECT * FROM outbox"))
        self.assertEqual(runtime.continue_policy.rungs_today(OWNER_SESSION, now=due), 0)

        # second outage with llm_retryed set: give up, silence
        runtime.llm = DeadLLM(["x"])
        results = runtime.process_due_wakeups(now=due + 5 * 60)
        self.assertEqual(results[0]["outcome"], "silence")
        self.assertEqual(self.pending_rungs(db), [])

    def test_reply_between_rungs_cancels_ladder(self) -> None:
        config, db, runtime = self.make_env()
        row = self._arm(runtime, desire=0.9)
        say = json.dumps({"messages": ["在等你回我嘛"]}, ensure_ascii=False)

        runtime.llm = FakeLLM([say])
        runtime.process_due_wakeups(now=row["fire_at"])
        self.assertEqual(len(self.pending_rungs(db)), 1)

        runtime.llm = FakeLLM([plan_with(desire=0.3, thread_open=False)])
        runtime.handle_user_message(OWNER_SESSION, "哈哈来了来了", now=row["fire_at"] + 60)
        self.assertEqual(self.pending_rungs(db), [])

    def test_rung_bypasses_proactive_gate_pool(self) -> None:
        config, db, runtime = self.make_env()
        row = self._arm(runtime, desire=0.4)
        # a proactive message "sent" right now would block any gated wakeup
        # for 90 minutes -- the rung must not care
        runtime.memory.log_decision(
            "proactive_message", {"x": 1}, action="sent", outcome="queued", at=NOON
        )

        results = self._deliver(
            runtime, row, now=row["fire_at"],
            reply_plan=json.dumps({"messages": ["就问一句嘛"]}, ensure_ascii=False),
        )
        self.assertTrue(results[0]["acted"])
        self.assertEqual(results[0]["outcome"], "delivered")

        # ...while an ordinary proactive wakeup still respects the pool
        from fatfish.wakeup import WakeupScheduler

        scheduler = WakeupScheduler(db, config)
        gated = scheduler.schedule(
            row["fire_at"] + 120, intent="check_user", session_id=OWNER_SESSION,
            now=row["fire_at"], reason="看看主人",
        )
        blocked = runtime.process_due_wakeups(now=gated.fire_at)
        self.assertFalse(blocked[0]["acted"])
        self.assertEqual(blocked[0]["reason"], "too_soon")

    def test_rung_due_in_quiet_hours_defers_to_morning(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM([plan_with(desire=0.9)])
        runtime.handle_user_message(OWNER_SESSION, "在吗", now=NOON)
        # grab the armed rung and shove it into the quiet window (as an
        # urgent schedule would): the deferral must keep payload and rung
        row = self.pending_rungs(db)[0]
        db.execute(
            "UPDATE wakeups SET fire_at=?, expires_at=? WHERE id=?",
            (QUIET + 60, QUIET + 60 + 6 * 3600, row["id"]),
        )

        results = runtime.process_due_wakeups(now=QUIET + 60)

        self.assertEqual(results[0]["reason"], "quiet_deferred")
        deferred = self.pending_rungs(db)
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["fire_at"], MORNING)
        payload = json.loads(deferred[0]["payload_json"])
        self.assertEqual(payload["rung"], 1)
        self.assertEqual(payload["max_rungs"], 3)
        self.assertIn(("quiet_deferred", "ok"), self.actions(db))

    def test_flag_set_while_rung_pending_cancels_it(self) -> None:
        config, db, runtime = self.make_env()
        row = self._arm(runtime, desire=0.9)

        runtime.continue_policy.set_stop_flag(OWNER_SESSION, "busy", now=row["fire_at"] - 1)
        results = runtime.process_due_wakeups(now=row["fire_at"])

        self.assertFalse(results[0]["acted"])
        self.assertEqual(results[0]["reason"], "stop_flag")
        self.assertEqual(self.pending_rungs(db), [])
        self.assertIn(("rung_cancelled", "stop_flag"), self.actions(db))

    def test_flag_suppresses_other_proactive_wakeups(self) -> None:
        config, db, runtime = self.make_env()
        from fatfish.wakeup import WakeupScheduler

        scheduler = WakeupScheduler(db, config)
        ritual = scheduler.schedule(
            NOON + 60, intent="check_user", session_id=OWNER_SESSION,
            now=NOON, reason="晚安问候",
        )
        runtime.continue_policy.set_stop_flag(OWNER_SESSION, "chat_stop", now=NOON + 30)

        results = runtime.process_due_wakeups(now=NOON + 60)

        self.assertFalse(results[0]["acted"])
        self.assertEqual(results[0]["reason"], "stop_flag")
        self.assertIn(("suppressed", "stop_flag"), self.actions(db))

    def test_flag_defers_queued_proactive_outbox_items(self) -> None:
        config, db, runtime = self.make_env()
        item_id, created = runtime.outbox.enqueue(
            "proactive_message",
            {"text": "之前想说的话", "session_id": OWNER_SESSION, "reason": "self_continue"},
            idempotency_key="proactive:flagged",
        )
        self.assertTrue(created)
        # enqueue stamps real wall-clock times; pull the row into test time
        db.execute(
            "UPDATE outbox SET created_at=?, available_at=? WHERE id=?",
            (NOON - 60, NOON - 60, item_id),
        )
        runtime.continue_policy.set_stop_flag(OWNER_SESSION, "busy", now=NOON)

        results = runtime.process_outbox(now=NOON)

        self.assertEqual(results[0]["status"], "deferred")
        row = db.query_one("SELECT * FROM outbox WHERE id=?", (results[0]["id"],))
        self.assertEqual(row["status"], "pending")
        self.assertIn(("suppressed", "stop_flag"), self.actions(db))

    def test_web_session_rung_still_dropped_silently(self) -> None:
        config, db, runtime = self.make_env()
        from fatfish.wakeup import WakeupScheduler

        scheduler = WakeupScheduler(db, config)
        scheduler.schedule(
            NOON + 60, intent="self_continue", session_id="web-default", now=NOON
        )
        runtime.llm = FakeLLM(["x"])

        results = runtime.process_due_wakeups(now=NOON + 60)

        self.assertFalse(results[0]["acted"])
        self.assertEqual(results[0]["reason"], "not_deliverable")
        self.assertEqual(len(runtime.llm.calls), 0)
        self.assertIn(("rung_skipped", "not_deliverable"), self.actions(db))


if __name__ == "__main__":
    unittest.main()
