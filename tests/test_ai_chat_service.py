"""T-0047 (L3): AiChatService 状态持久化 / 审计 / outbox 投递。

全部离线：临时库 + fake client，不联网、不读真实 ``data/fatfish.db``。

钉住的行为（对卡 T-0047 逐条对应）：

* 只有 ``qq:private:<peer_uin>`` 且 ``[ai_chat]`` active 才算该会话，其余一律
  False；账号未绑定时 enqueue 根本不入队。
* 闸门：``desire < threshold`` 拒、相等允许、``passive=True`` 直接放行。
* ``record_sent(out)`` 抬阈值、``in`` 不抬；同 ``(peer_uin, direction, turn_id)``
  幂等且不二次抬阈值（跨实例也幂等）。
* ``enqueue`` 同 key 只有一行；阈值不在入队时改（投递成功后由 record_sent 抬）。
* ``sent_last_hour`` 只做审计/兜底硬帽：100 条历史仍然只按阈值判。
* 状态 JSON 损坏时回落 ``initial_threshold`` 并告警，不抛。
* ``qq.handlers()`` 含 ``ai_chat_message``；payload 缺 ``peer_uin`` 直接报错且
  一次 client 调用都不发生；``on_sent`` 回调抛异常不影响发送结果。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.ai_chat import engine
from fatfish.ai_chat.service import (
    AUDIT_KIND,
    OUTBOX_KIND,
    REASON_PASSIVE,
    AiChatService,
)
from fatfish.ai_chat.settings import AiChatSettings
from fatfish.db import Database
from fatfish.platforms.qq import QQAdapter

T0 = 1_700_000_000.0
PEER = "2002"
SESSION = f"qq:private:{PEER}"
STATE_KEY = f"ai_chat:state:{PEER}"


def make_settings(**overrides) -> AiChatSettings:
    base = {"enabled": True, "peer_uin": PEER}
    base.update(overrides)
    return AiChatSettings(**base)


class FakeClient:
    """Records every call; nothing here touches a socket."""

    def __init__(self) -> None:
        self.connected = True
        self.calls: list[tuple[str, ...]] = []
        self.sent: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str, list[dict]]] = []

    def send_private_text(self, user_id, text):
        self.calls.append(("send_private_text", str(user_id), str(text)))
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def send_group_text(self, group_id, text):
        self.calls.append(("send_group_text", str(group_id), str(text)))
        self.sent.append(("group", str(group_id), str(text)))
        return "echo-group"

    def send_private_message(self, user_id, message):
        self.calls.append(("send_private_message", str(user_id)))
        self.messages.append(("private", str(user_id), list(message)))
        return "echo-private-media"

    def send_group_message(self, group_id, message):
        self.calls.append(("send_group_message", str(group_id)))
        self.messages.append(("group", str(group_id), list(message)))
        return "echo-group-media"


class ServiceBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "fatfish.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.settings = make_settings()
        self.service = AiChatService(self.db, self.settings, now_fn=lambda: T0)

    def outbox_count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM outbox")
        return int(row["n"]) if row else 0

    def message_count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM ai_chat_messages")
        return int(row["n"]) if row else 0

    def insert_message(self, *, direction="out", created_at=T0, content="x", uin=PEER):
        self.db.execute(
            "INSERT INTO ai_chat_messages(peer_uin, direction, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (uin, direction, content, created_at),
        )


# --------------------------------------------------------------- session gating
class SessionRecognitionTests(ServiceBase):
    def test_only_the_bound_private_session_is_true(self):
        self.assertTrue(self.service.is_ai_chat_session(SESSION))
        self.assertTrue(self.service.is_ai_chat_session(f"  {SESSION}  "))

    def test_everything_else_is_false(self):
        for session_id in (
            "qq:group:2002",
            "qq:private:1001",
            "qq:private:20020",
            "qq:private:",
            "web:default",
            "",
            None,
            2002,
            ["qq:private:2002"],
        ):
            with self.subTest(session_id=session_id):
                self.assertFalse(self.service.is_ai_chat_session(session_id))

    def test_disabled_feature_is_false_even_for_the_bound_uin(self):
        for settings in (
            make_settings(enabled=False),
            make_settings(peer_uin=""),
            make_settings(peer_uin="   "),
        ):
            with self.subTest(reason=settings.disabled_reason):
                service = AiChatService(self.db, settings, now_fn=lambda: T0)
                self.assertFalse(service.is_ai_chat_session(SESSION))
                # A whitespace-only uin normalizes to "unbound": no session id,
                # no state key that could collide, and enqueue stays closed.
                expected = f"qq:private:{settings.peer_uin.strip()}"
                self.assertEqual(
                    service.session_id, "" if not settings.peer_uin.strip() else expected
                )
                self.assertFalse(service.enqueue("x", desire=1.0, threshold=0.0)["ok"])

    def test_session_id_and_state_key(self):
        self.assertEqual(self.service.session_id, SESSION)
        self.assertEqual(self.service.state_key, STATE_KEY)


# ----------------------------------------------------------------------- state
class StatePersistenceTests(ServiceBase):
    def test_missing_state_is_initial_threshold_without_decay(self):
        state = self.service.load_state()
        self.assertAlmostEqual(state.threshold, self.settings.initial_threshold)
        self.assertIsNone(state.updated_at)
        self.assertIsNone(state.last_desire)
        self.assertAlmostEqual(self.service.current_threshold(T0), 0.30)

    def test_save_and_load_round_trip(self):
        self.service.save_state(engine.AiChatState(threshold=0.57, updated_at=T0, last_desire=0.8))
        raw = json.loads(self.db.get_meta(STATE_KEY))
        self.assertEqual(set(raw), {"threshold", "updated_at", "last_desire"})
        state = self.service.load_state()
        self.assertAlmostEqual(state.threshold, 0.57)
        self.assertAlmostEqual(state.updated_at, T0)
        self.assertAlmostEqual(state.last_desire, 0.8)

    def test_corrupt_json_falls_back_and_warns(self):
        self.db.set_meta(STATE_KEY, "{not json at all")
        with self.assertLogs("fatfish.ai_chat.service", level="WARNING"):
            state = self.service.load_state()
        self.assertAlmostEqual(state.threshold, 0.30)
        self.assertIsNone(state.updated_at)
        # Still usable: the gate keeps working off the fallback.
        self.assertAlmostEqual(self.service.current_threshold(T0), 0.30)

    def test_non_object_json_falls_back(self):
        for raw in ("[]", '"text"', "42"):
            with self.subTest(raw=raw):
                self.db.set_meta(STATE_KEY, raw)
                with self.assertLogs("fatfish.ai_chat.service", level="WARNING"):
                    state = self.service.load_state()
                self.assertAlmostEqual(state.threshold, 0.30)
                self.assertIsNone(state.updated_at)

    def test_missing_threshold_falls_back(self):
        self.db.set_meta(STATE_KEY, json.dumps({"updated_at": T0, "last_desire": 0.9}))
        with self.assertLogs("fatfish.ai_chat.service", level="WARNING"):
            state = self.service.load_state()
        self.assertAlmostEqual(state.threshold, 0.30)
        self.assertIsNone(state.updated_at)

    def test_bad_updated_at_does_not_raise(self):
        self.db.set_meta(STATE_KEY, json.dumps({"threshold": 0.5, "updated_at": "昨天"}))
        state = self.service.load_state()
        self.assertAlmostEqual(state.threshold, 0.5)
        self.assertIsNone(state.updated_at)


# ------------------------------------------------------------------------ gate
class GateTests(ServiceBase):
    def test_below_threshold_is_refused(self):
        allowed, reason, meta = self.service.can_send(0.29, now=T0)
        self.assertFalse(allowed)
        self.assertEqual(reason, engine.REASON_BELOW_THRESHOLD)
        self.assertAlmostEqual(meta["threshold"], 0.30)
        self.assertAlmostEqual(meta["desire"], 0.29)
        self.assertEqual(meta["sent_last_hour"], 0)
        self.assertAlmostEqual(meta["decay_per_minute"], 0.1)

    def test_equality_is_allowed(self):
        allowed, reason, _ = self.service.can_send(0.30, now=T0)
        self.assertTrue(allowed)
        self.assertEqual(reason, engine.REASON_OK)

    def test_passive_bypasses_the_threshold(self):
        allowed, reason, meta = self.service.can_send(0.0, now=T0, passive=True)
        self.assertTrue(allowed)
        self.assertEqual(reason, REASON_PASSIVE)
        self.assertAlmostEqual(meta["threshold"], 0.30)
        self.assertAlmostEqual(meta["desire"], 0.0)

    def test_garbage_desire_counts_as_zero(self):
        for raw in (None, "", "abc", float("nan"), True):
            with self.subTest(raw=raw):
                allowed, reason, meta = self.service.can_send(raw, now=T0)
                self.assertFalse(allowed)
                self.assertEqual(reason, engine.REASON_BELOW_THRESHOLD)
                self.assertEqual(meta["desire"], 0.0)

    def test_threshold_decays_between_reads(self):
        self.service.save_state(engine.AiChatState(threshold=0.60, updated_at=T0))
        allowed, reason, meta = self.service.can_send(0.40, now=T0 + 120)
        # 0.60 - 0.1*2 = 0.40 exactly -> equality allows.
        self.assertTrue(allowed, reason)
        self.assertAlmostEqual(meta["threshold"], 0.40)


# ---------------------------------------------------------------------- ledger
class RecordSentTests(ServiceBase):
    def test_out_lifts_threshold_and_writes_row(self):
        message_id = self.service.record_sent(
            "你好呀", desire=0.8, threshold=0.30, direction="out", turn_id=11, now=T0
        )
        row = self.db.query_one("SELECT * FROM ai_chat_messages WHERE id=?", (message_id,))
        self.assertEqual(row["peer_uin"], PEER)
        self.assertEqual(row["direction"], "out")
        self.assertEqual(row["content"], "你好呀")
        self.assertAlmostEqual(row["turn_id"], 11)
        self.assertAlmostEqual(row["created_at"], T0)
        self.assertAlmostEqual(self.service.load_state().threshold, 0.42)
        self.assertAlmostEqual(self.service.current_threshold(T0), 0.42)

    def test_in_only_writes_and_never_lifts(self):
        message_id = self.service.record_sent(
            "对方说的话", desire=None, threshold=None, direction="in", turn_id=12, now=T0
        )
        row = self.db.query_one("SELECT * FROM ai_chat_messages WHERE id=?", (message_id,))
        self.assertEqual(row["direction"], "in")
        self.assertAlmostEqual(self.service.load_state().threshold, 0.30)
        self.assertAlmostEqual(self.service.current_threshold(T0), 0.30)

    def test_duplicate_turn_is_idempotent_and_lifts_once(self):
        first = self.service.record_sent(
            "同一条", desire=0.9, threshold=0.30, direction="out", turn_id=21, now=T0
        )
        second = self.service.record_sent(
            "同一条", desire=0.9, threshold=0.30, direction="out", turn_id=21, now=T0
        )
        self.assertEqual(first, second)
        self.assertEqual(self.message_count(), 1)
        self.assertAlmostEqual(self.service.load_state().threshold, 0.42)

    def test_duplicate_turn_is_idempotent_across_instances(self):
        self.service.record_sent(
            "跨实例", desire=0.9, threshold=0.30, direction="out", turn_id=22, now=T0
        )
        other = AiChatService(self.db, self.settings, now_fn=lambda: T0)
        replay = other.record_sent(
            "跨实例", desire=0.9, threshold=0.30, direction="out", turn_id=22, now=T0
        )
        self.assertIsNotNone(replay)
        self.assertEqual(self.message_count(), 1)
        self.assertAlmostEqual(other.load_state().threshold, 0.42)

    def test_without_turn_id_each_call_appends(self):
        self.service.record_sent("一", desire=0.9, threshold=0.3, direction="out", now=T0)
        self.service.record_sent("二", desire=0.9, threshold=0.3, direction="out", now=T0)
        self.assertEqual(self.message_count(), 2)

    def test_unbound_or_unknown_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.record_sent("x", desire=0.5, threshold=0.3, direction="sideways")
        unbound = AiChatService(self.db, make_settings(peer_uin=""), now_fn=lambda: T0)
        with self.assertRaises(ValueError):
            unbound.record_sent("x", desire=0.5, threshold=0.3)
        self.assertEqual(self.message_count(), 0)


# -------------------------------------------------------------- sent_last_hour
class SentLastHourTests(ServiceBase):
    def test_counts_only_outgoing_rows_in_the_window(self):
        self.insert_message(direction="out", created_at=T0 - 10)
        self.insert_message(direction="out", created_at=T0 - 100)
        self.insert_message(direction="in", created_at=T0 - 10)
        self.insert_message(direction="out", created_at=T0 - 4000)  # too old
        self.insert_message(direction="out", created_at=T0 - 10, uin="1001")  # other peer
        self.assertEqual(self.service.sent_last_hour(T0), 2)

    def test_unbound_peer_counts_zero(self):
        self.insert_message(direction="out")
        unbound = AiChatService(self.db, make_settings(peer_uin=""), now_fn=lambda: T0)
        self.assertEqual(unbound.sent_last_hour(T0), 0)

    def test_hundred_messages_do_not_block_the_gate(self):
        # 50/h is a decay baseline, not a limit: with hard_cap=0 (default) the
        # historical count must not refuse anything.
        for _ in range(100):
            self.insert_message(direction="out", created_at=T0 - 5)
        self.assertEqual(self.service.sent_last_hour(T0), 100)
        allowed, reason, meta = self.service.can_send(0.9, now=T0)
        self.assertTrue(allowed, reason)
        self.assertEqual(meta["sent_last_hour"], 100)
        refused, reason, _ = self.service.can_send(0.1, now=T0)
        self.assertFalse(refused)
        self.assertEqual(reason, engine.REASON_BELOW_THRESHOLD)

    def test_hard_cap_only_bites_when_explicitly_enabled(self):
        for _ in range(50):
            self.insert_message(direction="out", created_at=T0 - 5)
        capped = AiChatService(
            self.db, make_settings(hard_cap_per_hour=50), now_fn=lambda: T0
        )
        allowed, reason, meta = capped.can_send(1.0, now=T0)
        self.assertFalse(allowed)
        self.assertEqual(reason, engine.REASON_HARD_CAP)
        self.assertEqual(meta["sent_last_hour"], 50)


# ----------------------------------------------------------------------- audit
class AuditTests(ServiceBase):
    def audit_rows(self):
        return self.db.query("SELECT * FROM decision_log WHERE kind=? ORDER BY id", (AUDIT_KIND,))

    def test_writes_metadata_only(self):
        self.service.audit(
            engine.REASON_BELOW_THRESHOLD,
            desire=0.12,
            threshold=0.44,
            outcome="queued? no",
            session_id=SESSION,
            now=T0,
        )
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], engine.REASON_BELOW_THRESHOLD)
        self.assertEqual(row["outcome"], "queued? no")
        self.assertEqual(row["session_id"], SESSION)
        self.assertAlmostEqual(row["at"], T0)
        payload = json.loads(row["output_json"])
        self.assertAlmostEqual(payload["desire"], 0.12)
        self.assertAlmostEqual(payload["threshold"], 0.44)
        self.assertEqual(payload["reason"], engine.REASON_BELOW_THRESHOLD)
        self.assertNotIn("content", payload)

    def test_free_text_is_clipped(self):
        self.service.audit("r" * 500, outcome="o" * 500, now=T0)
        row = self.audit_rows()[0]
        self.assertEqual(len(row["action"]), 200)
        self.assertEqual(len(row["outcome"]), 200)
        payload = json.loads(row["output_json"])
        self.assertLessEqual(len(payload["detail"]), 200)
        self.assertLessEqual(len(payload["reason"]), 200)

    def test_audit_failure_never_raises(self):
        def boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        self.db.execute = boom  # type: ignore[method-assign]
        with self.assertLogs("fatfish.ai_chat.service", level="WARNING"):
            self.service.audit(engine.REASON_OK, outcome="ok", now=T0)


# --------------------------------------------------------------------- enqueue
class EnqueueTests(ServiceBase):
    def enqueue(self, text="在吗", **overrides):
        kwargs = {"desire": 0.77, "threshold": 0.30, "turn_id": 31, "now": T0}
        kwargs.update(overrides)
        return self.service.enqueue(text, **kwargs)

    def test_payload_and_key_shape(self):
        result = self.enqueue()
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        row = self.db.query_one("SELECT * FROM outbox WHERE id=?", (result["id"],))
        self.assertEqual(row["kind"], OUTBOX_KIND)
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["peer_uin"], PEER)
        self.assertEqual(payload["text"], "在吗")
        self.assertEqual(payload["media"], [])
        self.assertEqual(payload["session_id"], SESSION)
        self.assertAlmostEqual(payload["desire"], 0.77)
        self.assertAlmostEqual(payload["threshold"], 0.30)
        self.assertEqual(payload["turn_id"], 31)
        self.assertIn(PEER, row["idempotency_key"])
        self.assertIn(SESSION, row["idempotency_key"])
        self.assertIn("turn:31", row["idempotency_key"])

    def test_threshold_is_not_touched_at_enqueue_time(self):
        self.enqueue()
        self.assertIsNone(self.db.get_meta(STATE_KEY))
        self.assertAlmostEqual(self.service.load_state().threshold, 0.30)

    def test_same_turn_enqueues_one_row(self):
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])
        self.assertEqual(self.outbox_count(), 1)

    def test_same_text_without_turn_enqueues_one_row(self):
        first = self.service.enqueue("同一句", desire=0.5, threshold=0.3, now=T0)
        second = self.service.enqueue("同一句", desire=0.5, threshold=0.3, now=T0)
        self.assertEqual(first["id"], second["id"])
        self.assertIn(":msg:", second["idempotency_key"])
        self.assertEqual(self.outbox_count(), 1)
        third = self.service.enqueue("另一句", desire=0.5, threshold=0.3, now=T0)
        self.assertNotEqual(first["id"], third["id"])
        self.assertEqual(self.outbox_count(), 2)

    def test_disabled_feature_never_enqueues(self):
        for settings in (
            make_settings(enabled=False),
            make_settings(peer_uin=""),
            make_settings(peer_uin="   "),
        ):
            with self.subTest(reason=settings.disabled_reason):
                service = AiChatService(self.db, settings, now_fn=lambda: T0)
                result = service.enqueue("不该出现", desire=1.0, threshold=0.0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "ai_chat_disabled")
                self.assertEqual(self.outbox_count(), 0)

    def test_wrong_session_is_refused(self):
        result = self.service.enqueue(
            "发错人", desire=1.0, threshold=0.0, session_id="qq:private:1001"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "ai_chat_disabled")
        self.assertEqual(self.outbox_count(), 0)

    def test_empty_text_without_media_is_refused(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                result = self.service.enqueue(text, desire=1.0, threshold=0.0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "empty_message")
                self.assertEqual(self.outbox_count(), 0)

    def test_media_only_payload_is_allowed(self):
        result = self.service.enqueue(
            "",
            media=[{"type": "image", "path": "a.png"}],
            desire=0.9,
            threshold=0.3,
            now=T0,
        )
        self.assertTrue(result["ok"])
        row = self.db.query_one("SELECT payload_json FROM outbox WHERE id=?", (result["id"],))
        stored = json.loads(row["payload_json"])
        self.assertEqual(stored["media"], [{"type": "image", "path": "a.png"}])


# ------------------------------------------------------------------ qq handler
class QQHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = Database(self.dir / "fatfish.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.settings = make_settings()
        self.service = AiChatService(self.db, self.settings, now_fn=lambda: T0)
        self.fake = FakeClient()

    def adapter(self, on_sent=None) -> QQAdapter:
        adapter = QQAdapter(
            SimpleNamespace(db=self.db),
            {"enabled": True, "catchup_enabled": False},
            on_sent=on_sent,
        )
        adapter.client = self.fake
        return adapter

    def item(self, payload: dict, item_id: int = 1) -> dict:
        return {"id": item_id, "kind": OUTBOX_KIND, "payload": payload}

    def test_handlers_register_the_kind(self):
        adapter = self.adapter()
        handlers = adapter.handlers()
        self.assertIn(OUTBOX_KIND, handlers)
        self.assertEqual(handlers[OUTBOX_KIND].__func__, adapter.send_outbox_item.__func__)

    def test_missing_peer_uin_never_touches_the_client(self):
        adapter = self.adapter()
        for payload in ({}, {"peer_uin": ""}, {"peer_uin": "   ", "text": "hi"}):
            with self.subTest(payload=payload):
                result = adapter.send_outbox_item(self.item(payload))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "missing_peer_uin")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(self.fake.messages, [])

    def test_sends_private_text_and_calls_on_sent(self):
        seen: list[tuple[dict, dict]] = []
        adapter = self.adapter(on_sent=lambda payload, result: seen.append((payload, result)))
        payload = {
            "peer_uin": PEER,
            "text": "主动说一句",
            "media": [],
            "session_id": SESSION,
            "desire": 0.8,
            "threshold": 0.3,
            "turn_id": 41,
        }
        result = adapter.send_outbox_item(self.item(payload))
        self.assertTrue(result["ok"])
        self.assertEqual(result["echo"], "echo-private")
        self.assertEqual(self.fake.sent, [("private", PEER, "主动说一句")])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0]["turn_id"], 41)
        self.assertEqual(seen[0][0]["peer_uin"], PEER)
        self.assertTrue(seen[0][1]["ok"])

    def test_callback_exception_does_not_fail_the_send(self):
        def boom(payload, result):
            raise RuntimeError("db down")

        adapter = self.adapter(on_sent=boom)
        payload = {"peer_uin": PEER, "text": "还能发出去吗", "media": []}
        with self.assertLogs("fatfish.platforms.qq", level="WARNING"):
            result = adapter.send_outbox_item(self.item(payload))
        self.assertTrue(result["ok"])
        self.assertEqual(result["echo"], "echo-private")
        self.assertEqual(self.fake.sent, [("private", PEER, "还能发出去吗")])

    def test_media_uses_segments_and_still_notifies(self):
        path = self.dir / "pic.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n fake bytes")
        seen: list[dict] = []
        adapter = self.adapter(on_sent=lambda payload, result: seen.append(result))
        payload = {
            "peer_uin": PEER,
            "text": "看图",
            "media": [{"type": "image", "path": str(path)}],
        }
        result = adapter.send_outbox_item(self.item(payload))
        self.assertTrue(result["ok"])
        self.assertEqual(result["media_sent"], 1)
        self.assertEqual(len(self.fake.messages), 1)
        target_type, target_id, segments = self.fake.messages[0]
        self.assertEqual((target_type, target_id), ("private", PEER))
        self.assertEqual(segments[0], {"type": "text", "data": {"text": "看图"}})
        self.assertEqual(segments[1]["type"], "image")
        self.assertEqual(len(seen), 1)

    def test_delivery_callback_records_and_lifts(self):
        """End-to-end wiring contract: enqueue -> send -> record_sent -> +0.12."""

        def on_sent(payload, result):
            self.service.record_sent(
                payload["text"],
                desire=payload["desire"],
                threshold=payload["threshold"],
                direction="out",
                turn_id=payload.get("turn_id"),
                now=T0,
            )

        queued = self.service.enqueue(
            "真发出去才算数", desire=0.9, threshold=0.30, turn_id=51, now=T0
        )
        self.assertTrue(queued["ok"])
        row = self.db.query_one("SELECT payload_json FROM outbox WHERE id=?", (queued["id"],))
        adapter = self.adapter(on_sent=on_sent)
        result = adapter.send_outbox_item(
            self.item(json.loads(row["payload_json"]), item_id=queued["id"])
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(self.service.current_threshold(T0), 0.42)
        # A retried delivery of the same turn records nothing new.
        adapter.send_outbox_item(
            self.item(json.loads(row["payload_json"]), item_id=queued["id"])
        )
        self.assertAlmostEqual(self.service.current_threshold(T0), 0.42)
        count = self.db.query_one("SELECT COUNT(*) AS n FROM ai_chat_messages")
        self.assertEqual(int(count["n"]), 1)

    def test_group_target_is_impossible(self):
        adapter = self.adapter()
        result = adapter.send_outbox_item(
            self.item({"peer_uin": PEER, "text": "hi", "session_id": "qq:group:2002"})
        )
        # The handler always sends private; a groupish session_id in the payload
        # cannot turn this into a group message.
        self.assertTrue(result["ok"])
        self.assertEqual(result["target_type"], "private")
        self.assertEqual(self.fake.sent, [("private", PEER, "hi")])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
