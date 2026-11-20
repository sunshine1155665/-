"""T-0048 (L4): 提示词段 + 主动轮 + 撒娇梯避让，全部离线。

Fake LLM / 临时库 / fake outbox handler，不联网、不读真实 ``data/fatfish.db``。

钉住的行为（对卡 T-0048 逐条对应）：

* ``peer_uin`` 为空（或 ``enabled=false``）时 tick 不跑 LLM、不入队 —— 红线。
* ``[大肥鱼间聊天]`` 段只在绑定的那条会话出现，且带当前阈值；普通私聊、群聊、
  别的私聊都没有这段，stable prefix 也不受影响（缓存安全）。
* 顶层 ``ai_chat.desire`` 缺失按 0；低于阈值只 audit ``below_threshold``；
  达标恰好入队 1 条，阈值在投递成功后由 ``record_sent`` 抬。
* 水位先写：LLM 抛异常时水位已存在、``tick()`` 不抛。
* ``probe_interval_minutes`` 内不重复跑 LLM；安静时段跳过且不写水位。
* 该会话内撒娇梯不 arm，已 arm 的 rung 也被丢弃。
* 普通私聊行为不变；``process_outbox`` 无 handler 时把 ``ai_chat_message``
  defer（不是 failed）；``QQAdapter`` 注入的 ``on_sent`` 发送成功后落库抬阈值。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fatfish.ai_chat import engine
from fatfish.ai_chat.service import OUTBOX_KIND, AiChatService
from fatfish.cli import _ai_chat_on_sent
from fatfish.config import load_config
from fatfish.context import AI_CHAT_PROMPT_HEADER, ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.owner import OwnerRegistry
from fatfish.platforms.qq import QQAdapter
from fatfish.runtime import Runtime, parse_turn_plan
from fatfish.voice import VoiceDecision
from fatfish.wakeup import SELF_CONTINUE_INTENT

TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
NIGHT = datetime(2026, 10, 24, 2, 0, tzinfo=TZ8).timestamp()

OWNER_UIN = "10001"
PEER_UIN = "2002"
PEER_SESSION = f"qq:private:{PEER_UIN}"
OWNER_SESSION = f"qq:private:{OWNER_UIN}"
STATE_KEY = f"ai_chat:state:{PEER_UIN}"
WATERMARK = f"ai_chat:last_turn:{PEER_UIN}"

INITIAL = 0.30
STEP = 0.12


def plan(*, messages=("在的",), desire=None, extra=None) -> str:
    data = {"messages": list(messages), "actions": []}
    if desire is not None:
        data["ai_chat"] = {"desire": desire}
    if extra:
        data.update(extra)
    return json.dumps(data, ensure_ascii=False)


class FakeLLM:
    available = True

    def __init__(self, text: str = plan(), error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages, session_id=None):
        self.calls.append({"messages": messages, "session_id": session_id})
        if self.error is not None:
            raise self.error
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=4),
            model="fake-model",
        )

    @property
    def joined(self) -> str:
        return "\n".join(
            str(message.get("content") or "")
            for call in self.calls
            for message in call["messages"]
        )


class FakeVoicePolicy:
    def decide(self, text, **kwargs):
        return VoiceDecision("text_only", 0.0, ["test"])

    def record(self, decision, **kwargs):
        pass


class FakeTTS:
    def synthesize(self, text, *, emotion=None):  # pragma: no cover - never used
        raise AssertionError("tts must not run in these tests")


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.sent: list[tuple[str, str, str]] = []

    def send_private_text(self, user_id, text):
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def send_group_text(self, group_id, text):  # pragma: no cover
        raise AssertionError("no group send expected")

    def send_private_message(self, user_id, message):  # pragma: no cover
        raise AssertionError("text-only payload expected")


def write_config(
    tmp: Path,
    *,
    enabled: str = "true",
    peer_uin: str = PEER_UIN,
    quiet_hours: str = '["01:00", "08:00"]',
    extra: dict | None = None,
) -> Path:
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "ai_chat_runtime.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        "[qzone]",
        "enabled = false",
        'cookie_path = "qzone_cookies.json"',
        f'owner_uin = "{OWNER_UIN}"',
        "[activity]",
        "enabled = false",
        "[bilibili]",
        "enabled = false",
        "[qq]",
        "enabled = false",
        "catchup_enabled = false",
        "[ai_chat]",
        f"enabled = {enabled}",
        f'peer_uin = "{peer_uin}"',
        'peer_name = "大肥鱼"',
        f"initial_threshold = {INITIAL}",
        f"threshold_step = {STEP}",
        "threshold_floor = 0.15",
        "threshold_ceiling = 0.95",
        "decay_messages_per_hour = 50",
        "hard_cap_per_hour = 0",
        "probe_interval_minutes = 10",
        f"quiet_hours = {quiet_hours}",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    path = tmp / "ai_chat_runtime.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class AiChatRuntimeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = SimpleNamespace(now=NOON)

    def make_env(self, llm_text: str = plan(), **config_kwargs):
        path = write_config(self.tmp, **config_kwargs)
        config = load_config("low", path)
        config.ensure_dirs()
        db = Database(config.db_path)
        db.initialize()
        self.addCleanup(db.close)
        ensure_default_persona(db)
        runtime = Runtime(db, config)
        # Deterministic clock for the AI-to-AI service (record_sent / audit).
        service = AiChatService(
            db,
            runtime.ai_chat_settings,
            now_fn=lambda: self.clock.now,
            outbox=runtime.outbox,
        )
        runtime.ai_chat = service
        runtime.context.ai_chat = service
        runtime.llm = FakeLLM(llm_text)
        runtime.voice = FakeVoicePolicy()
        runtime.tts = FakeTTS()
        OwnerRegistry(db, config).bind(OWNER_SESSION, "主人哥哥")
        return config, db, runtime, runtime.llm

    # ------------------------------------------------------------- assertions
    def outbox_rows(self, db: Database, kind: str = OUTBOX_KIND) -> list[dict]:
        return [
            dict(row)
            for row in db.query(
                "SELECT id, status, payload_json FROM outbox WHERE kind=? ORDER BY id",
                (kind,),
            )
        ]

    def audit_rows(self, db: Database, action: str | None = None) -> list[dict]:
        if action is None:
            return [
                dict(row)
                for row in db.query(
                    "SELECT action, outcome FROM decision_log WHERE kind='ai_chat' "
                    "ORDER BY id"
                )
            ]
        return [
            dict(row)
            for row in db.query(
                "SELECT action, outcome FROM decision_log "
                "WHERE kind='ai_chat' AND action=? ORDER BY id",
                (action,),
            )
        ]

    def state(self, service: AiChatService) -> engine.AiChatState:
        return service.load_state()


class PeerUnboundTests(AiChatRuntimeBase):
    """Red line: no peer, no outward path at all."""

    def test_empty_peer_never_calls_llm_or_queues(self):
        config, db, runtime, llm = self.make_env(peer_uin="")
        self.assertFalse(runtime.ai_chat.settings.active)
        self.assertIsNone(runtime.maybe_ai_chat_turn(now=NOON))
        runtime.maybe_nightly = lambda **kwargs: None
        self.assertIsNone(runtime.tick(now=NOON)["ai_chat"])
        self.assertEqual(llm.calls, [])
        self.assertEqual(self.outbox_rows(db), [])
        self.assertIsNone(db.get_meta(WATERMARK))

    def test_disabled_flag_behaves_the_same(self):
        config, db, runtime, llm = self.make_env(enabled="false")
        self.assertIsNone(runtime.maybe_ai_chat_turn(now=NOON))
        self.assertEqual(llm.calls, [])
        self.assertEqual(self.outbox_rows(db), [])

    def test_unbound_session_is_not_the_ai_chat_session(self):
        config, db, runtime, llm = self.make_env(peer_uin="")
        self.assertFalse(runtime.ai_chat.is_ai_chat_session(PEER_SESSION))
        result = runtime.handle_user_message(
            PEER_SESSION, "你好", now=NOON, allow_voice=False
        )
        self.assertFalse(result["ai_chat"]["session"])
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS n FROM ai_chat_messages")["n"], 0
        )


class PromptBlockTests(AiChatRuntimeBase):
    def test_normal_sessions_do_not_get_the_block(self):
        config, db, runtime, llm = self.make_env()
        for session_id in (OWNER_SESSION, "qq:group:5555", "qq:private:9999"):
            with self.subTest(session=session_id):
                bundle = runtime.context.build(session_id, "在吗", now=NOON)
                self.assertNotIn(AI_CHAT_PROMPT_HEADER, bundle.text())
                stable = next(
                    s for s in bundle.sections if s.name == "stable_prefix"
                )
                self.assertNotIn(AI_CHAT_PROMPT_HEADER, stable.text)

    def test_bound_session_gets_the_block_with_live_threshold(self):
        config, db, runtime, llm = self.make_env()
        bundle = runtime.context.build(PEER_SESSION, "在吗", now=NOON)
        text = bundle.text()
        self.assertIn(AI_CHAT_PROMPT_HEADER, text)
        self.assertIn(f"当前渴望阈值 {INITIAL:.2f}", text)
        # 阈值每轮变，必须留在 dynamic 段，不打穿 stable prefix 缓存。
        stable = next(s for s in bundle.sections if s.name == "stable_prefix")
        self.assertNotIn(AI_CHAT_PROMPT_HEADER, stable.text)
        self.assertNotIn("ai_chat", stable.text)

    def test_threshold_number_follows_the_state(self):
        config, db, runtime, llm = self.make_env()
        runtime.ai_chat.save_state(
            engine.AiChatState(threshold=0.83, updated_at=self.clock.now)
        )
        bundle = runtime.context.build(PEER_SESSION, "在吗", now=self.clock.now)
        self.assertIn("当前渴望阈值 0.83", bundle.text())

    def test_block_reaches_the_wire(self):
        config, db, runtime, llm = self.make_env()
        runtime.handle_user_message(PEER_SESSION, "在吗", now=NOON, allow_voice=False)
        self.assertIn(AI_CHAT_PROMPT_HEADER, llm.joined)


class ProactiveTurnTests(AiChatRuntimeBase):
    def test_below_threshold_queues_nothing_and_audits(self):
        config, db, runtime, llm = self.make_env(plan(desire=0.1))
        outcome = runtime.maybe_ai_chat_turn(now=NOON)
        self.assertEqual(outcome["decision"], "skipped")
        self.assertEqual(outcome["reason"], engine.REASON_BELOW_THRESHOLD)
        self.assertEqual(self.outbox_rows(db), [])
        self.assertEqual(len(self.audit_rows(db, "below_threshold")), 1)
        self.assertAlmostEqual(runtime.ai_chat.current_threshold(NOON), INITIAL)

    def test_missing_desire_counts_as_zero(self):
        config, db, runtime, llm = self.make_env(plan())  # no ai_chat block
        outcome = runtime.maybe_ai_chat_turn(now=NOON)
        self.assertEqual(outcome["desire"], 0.0)
        self.assertEqual(outcome["decision"], "skipped")
        self.assertEqual(self.outbox_rows(db), [])

    def test_above_threshold_queues_exactly_one(self):
        config, db, runtime, llm = self.make_env(plan(desire=0.9))
        outcome = runtime.maybe_ai_chat_turn(now=NOON)
        self.assertEqual(outcome["decision"], "sent")
        rows = self.outbox_rows(db)
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["peer_uin"], PEER_UIN)
        self.assertEqual(payload["session_id"], PEER_SESSION)
        self.assertAlmostEqual(payload["desire"], 0.9)
        self.assertEqual(len(self.audit_rows(db, "proactive_sent")), 1)
        # 入队本身不改阈值：等投递成功。
        self.assertAlmostEqual(runtime.ai_chat.current_threshold(NOON), INITIAL)
        # 主动轮不该顺手记一条 "passive"：那条会在投递时由 on_sent 记。
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS n FROM ai_chat_messages")["n"], 0
        )

    def test_delivery_lifts_the_threshold(self):
        config, db, runtime, llm = self.make_env(plan(desire=0.9))
        runtime.maybe_ai_chat_turn(now=NOON)
        adapter = QQAdapter(runtime, config, on_sent=_ai_chat_on_sent(runtime))
        fake = FakeClient()
        adapter.client = fake
        results = runtime.process_outbox(handlers=adapter.handlers(), now=NOON)
        self.assertEqual([r["status"] for r in results], ["done"])
        self.assertEqual(fake.sent, [("private", PEER_UIN, "在的")])
        self.assertAlmostEqual(
            runtime.ai_chat.current_threshold(self.clock.now), INITIAL + STEP
        )
        rows = db.query("SELECT * FROM ai_chat_messages")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "out")

    def test_silence_queues_nothing_and_stays_out_of_history(self):
        config, db, runtime, llm = self.make_env(
            plan(messages=["【沉默】"], desire=0.9)
        )
        outcome = runtime.maybe_ai_chat_turn(now=NOON)
        self.assertEqual(outcome["decision"], "silence")
        self.assertEqual(self.outbox_rows(db), [])
        self.assertEqual(len(self.audit_rows(db, "silence")), 1)
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM conversation_turns "
            "WHERE session_id=? AND ephemeral=0",
            (PEER_SESSION,),
        )
        self.assertEqual(int(row["n"]), 0)

    def test_watermark_is_written_before_the_turn(self):
        config, db, runtime, llm = self.make_env()
        llm.error = RuntimeError("gateway down")
        outcome = runtime.maybe_ai_chat_turn(now=NOON)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["decision"], "error")
        self.assertEqual(float(db.get_meta(WATERMARK)), NOON)
        self.assertEqual(len(self.audit_rows(db, "turn_error")), 1)
        self.assertEqual(self.outbox_rows(db), [])

    def test_broken_turn_does_not_break_tick(self):
        config, db, runtime, llm = self.make_env()
        llm.error = RuntimeError("gateway down")
        runtime.maybe_nightly = lambda **kwargs: None
        result = runtime.tick(now=NOON)
        self.assertFalse(result["ai_chat"]["ok"])
        self.assertEqual(float(db.get_meta(WATERMARK)), NOON)

    def test_probe_interval_blocks_a_second_turn(self):
        config, db, runtime, llm = self.make_env(plan(desire=0.1))
        self.assertIsNotNone(runtime.maybe_ai_chat_turn(now=NOON))
        self.assertEqual(len(llm.calls), 1)
        self.assertIsNone(runtime.maybe_ai_chat_turn(now=NOON + 120))
        self.assertEqual(len(llm.calls), 1)
        self.assertIsNone(runtime.maybe_ai_chat_turn(now=NOON + 599))
        self.assertEqual(len(llm.calls), 1)
        # 过了 probe 间隔就允许再探一次（这是节奏，不是封嘴）。
        self.assertIsNotNone(runtime.maybe_ai_chat_turn(now=NOON + 601))
        self.assertEqual(len(llm.calls), 2)

    def test_quiet_hours_skip_without_watermark(self):
        config, db, runtime, llm = self.make_env()
        self.assertIsNone(runtime.maybe_ai_chat_turn(now=NIGHT))
        self.assertEqual(llm.calls, [])
        self.assertIsNone(db.get_meta(WATERMARK))
        self.assertEqual(self.outbox_rows(db), [])

    def test_stop_flag_skips_the_probe(self):
        config, db, runtime, llm = self.make_env()
        runtime.continue_policy.set_stop_flag(PEER_SESSION, "chat_stop", now=NOON)
        self.assertIsNone(runtime.maybe_ai_chat_turn(now=NOON))
        self.assertEqual(llm.calls, [])

    def test_activity_turn_waits_when_the_probe_ran(self):
        config, db, runtime, llm = self.make_env(plan(desire=0.9))
        activity_calls: list[dict] = []

        def fake_activity(**kwargs):
            activity_calls.append(kwargs)
            return {"ok": True, "decision": "act"}

        runtime.maybe_activity_turn = fake_activity
        runtime.maybe_nightly = lambda **kwargs: None
        first = runtime.tick(now=NOON)
        self.assertEqual(first["ai_chat"]["decision"], "sent")
        self.assertEqual(activity_calls, [])
        self.assertIsNone(first["activity"])
        # 下一 tick 不探（间隔没到），撒娇/活动轮才轮到。
        second = runtime.tick(now=NOON + 60)
        self.assertIsNone(second["ai_chat"])
        self.assertEqual(len(activity_calls), 1)

    def test_tick_returns_none_when_nothing_is_due(self):
        config, db, runtime, llm = self.make_env()
        runtime.maybe_ai_chat_turn(now=NOON)
        runtime.maybe_nightly = lambda **kwargs: None
        result = runtime.tick(now=NOON + 60)
        self.assertIsNone(result["ai_chat"])


class PassiveReplyTests(AiChatRuntimeBase):
    def test_passive_reply_skips_the_gate_but_lifts(self):
        config, db, runtime, llm = self.make_env(plan(desire=0.05))
        # 阈值顶到 ceiling 也不拦被动回复。
        runtime.ai_chat.save_state(
            engine.AiChatState(threshold=0.9, updated_at=self.clock.now)
        )
        result = runtime.handle_user_message(
            PEER_SESSION, "你在吗", now=NOON, allow_voice=False
        )
        self.assertEqual(result["reply"], "在的")
        self.assertTrue(result["ai_chat"]["session"])
        rows = db.query("SELECT * FROM ai_chat_messages WHERE direction='out'")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(runtime.ai_chat.current_threshold(self.clock.now), 0.95)
        self.assertEqual(len(self.audit_rows(db, "passive_reply")), 1)

    def test_normal_private_chat_is_unchanged(self):
        config, db, runtime, llm = self.make_env(
            plan(extra={"continue": {"thread_open": True, "desire": 0.9}})
        )
        result = runtime.handle_user_message(
            OWNER_SESSION, "在吗", now=NOON, allow_voice=False
        )
        self.assertFalse(result["ai_chat"]["session"])
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS n FROM ai_chat_messages")["n"], 0
        )
        self.assertIsNone(db.get_meta(STATE_KEY))
        # 普通私聊的撒娇梯照旧 arm。
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM wakeups WHERE session_id=? AND intent=?",
            (OWNER_SESSION, SELF_CONTINUE_INTENT),
        )
        self.assertEqual(int(row["n"]), 1)


class LadderSuppressionTests(AiChatRuntimeBase):
    def test_ladder_does_not_arm_in_the_ai_chat_session(self):
        config, db, runtime, llm = self.make_env(
            plan(extra={"continue": {"thread_open": True, "desire": 0.9}})
        )
        runtime.handle_user_message(PEER_SESSION, "在吗", now=NOON, allow_voice=False)
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM wakeups WHERE session_id=? AND intent=?",
            (PEER_SESSION, SELF_CONTINUE_INTENT),
        )
        self.assertEqual(int(row["n"]), 0)

    def test_prearmed_rung_is_dropped(self):
        config, db, runtime, llm = self.make_env()
        runtime.wakeup.schedule(
            NOON - 1,
            intent=SELF_CONTINUE_INTENT,
            reason="撒娇梯第1棒",
            session_id=PEER_SESSION,
            payload={"origin": "coax", "rung": 1, "max_rungs": 2},
            now=NOON - 60,
            chain_depth=0,
        )
        results = runtime.process_due_wakeups(now=NOON)
        matching = [r for r in results if r.get("reason") == "ai_chat_session"]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0]["acted"])
        self.assertEqual(llm.calls, [])


class OutboxTests(AiChatRuntimeBase):
    def test_ai_chat_message_is_deferred_without_a_handler(self):
        config, db, runtime, llm = self.make_env()
        queued = runtime.ai_chat.enqueue(
            "主动说一句", desire=0.9, threshold=INITIAL, turn_id=7, now=NOON
        )
        self.assertTrue(queued["ok"])
        results = runtime.process_outbox(now=NOON)
        self.assertEqual([r["status"] for r in results], ["deferred"])
        row = db.query_one("SELECT status FROM outbox WHERE id=?", (queued["id"],))
        self.assertEqual(row["status"], "pending")

    def test_stop_flag_defers_a_queued_row(self):
        config, db, runtime, llm = self.make_env()
        queued = runtime.ai_chat.enqueue(
            "主动说一句", desire=0.9, threshold=INITIAL, turn_id=8, now=NOON
        )
        runtime.continue_policy.set_stop_flag(PEER_SESSION, "chat_stop", now=NOON)
        results = runtime.process_outbox(now=NOON)
        self.assertEqual([r["status"] for r in results], ["deferred"])
        self.assertEqual(results[0]["result"]["reason"], "stop_flag")
        row = db.query_one("SELECT status FROM outbox WHERE id=?", (queued["id"],))
        self.assertEqual(row["status"], "pending")

    def test_on_sent_records_once_per_turn(self):
        config, db, runtime, llm = self.make_env()
        payload_text = "主动说一句"
        runtime.ai_chat.enqueue(
            payload_text, desire=0.9, threshold=INITIAL, turn_id=11, now=NOON
        )
        adapter = QQAdapter(runtime, config, on_sent=_ai_chat_on_sent(runtime))
        adapter.client = FakeClient()
        runtime.process_outbox(handlers=adapter.handlers(), now=NOON)
        self.assertAlmostEqual(
            runtime.ai_chat.current_threshold(self.clock.now), INITIAL + STEP
        )
        # 同一个 turn 重投一次：幂等，阈值不二次抬。
        runtime.ai_chat.record_sent(
            payload_text,
            desire=0.9,
            threshold=INITIAL + STEP,
            direction="out",
            turn_id=11,
            now=self.clock.now,
        )
        self.assertAlmostEqual(
            runtime.ai_chat.current_threshold(self.clock.now), INITIAL + STEP
        )
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS n FROM ai_chat_messages")["n"], 1
        )

    def test_on_sent_is_none_when_the_feature_is_off(self):
        config, db, runtime, llm = self.make_env(peer_uin="")
        self.assertIsNone(_ai_chat_on_sent(runtime))


class ParseDesireTests(unittest.TestCase):
    def test_absent_field_is_none(self):
        self.assertIsNone(parse_turn_plan(plan()).ai_chat_desire)

    def test_desire_is_normalized(self):
        self.assertAlmostEqual(
            parse_turn_plan(plan(desire=1.7)).ai_chat_desire, 1.0
        )
        self.assertAlmostEqual(
            parse_turn_plan(plan(desire="0.4")).ai_chat_desire, 0.4
        )
        self.assertAlmostEqual(
            parse_turn_plan(plan(desire="abc")).ai_chat_desire, 0.0
        )

    def test_garbage_block_does_not_break_the_turn(self):
        raw = json.dumps({"messages": ["嗯"], "ai_chat": "high"})
        parsed = parse_turn_plan(raw)
        self.assertEqual(parsed.messages, ["嗯"])
        self.assertIsNone(parsed.ai_chat_desire)


if __name__ == "__main__":
    unittest.main()
