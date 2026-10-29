"""Tests for the QQ/OneBot adapter and the Bilibili client/collector.

No network access and no real QQ connection: clients and sockets are faked.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fatfish.bilibili import BilibiliClient, BilibiliCollector, BilibiliError
from fatfish.bilibili.client import (
    MAX_COMMENT_CHARS,
    _clean_cookie_mapping,
    bvid_to_aid,
    parse_cookie_string,
    parse_login_cookies,
)
from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.outbox import Outbox
from fatfish.platforms import InboundMessage, OutboundMessage, make_session_id
from fatfish.platforms import onebot as onebot_module
from fatfish.platforms.onebot import OneBotClient
from fatfish.platforms.qq import QQAdapter
from fatfish.runtime import Runtime
from fatfish.tts import TTSResult

BILI_ENV_KEYS = (
    "BILIBILI_COOKIE",
    "BILIBILI_SESSDATA",
    "BILIBILI_BILI_JCT",
    "BILIBILI_DEDEUSERID",
)


def make_env(tmp: Path, *, qq_extra: str = "") -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'platforms.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[qq]
enabled = true
self_id = 999
{qq_extra}

[bilibili]
enabled = true
cookie_path = "bilibili_cookies.json"
request_interval_seconds = 1.0
"""
    path = tmp / "platforms.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


def private_event(
    user_id: int = 1001,
    text: str = "你好呀",
    *,
    message_id: int = 11,
    self_id: int = 999,
) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": self_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def group_event(
    group_id: int = 5555,
    user_id: int = 1001,
    text: str = "今天吃什么",
    *,
    message_id: int = 22,
    mention: bool = True,
    self_id: int = 999,
    reply: int | None = None,
) -> dict:
    segments: list[dict] = []
    if reply is not None:
        segments.append({"type": "reply", "data": {"id": reply}})
    if mention:
        segments.append({"type": "at", "data": {"qq": self_id}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": self_id,
        "user_id": user_id,
        "group_id": group_id,
        "message_id": message_id,
        "message": segments,
    }


class FakeOneBotClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.connected = True
        self.sent: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str, list[dict]]] = []
        self.closed = False

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False
        self.closed = True

    def send_private_text(self, user_id, text):
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def send_group_text(self, group_id, text):
        self.sent.append(("group", str(group_id), str(text)))
        return "echo-group"

    def send_private_message(self, user_id, message):
        self.messages.append(("private", str(user_id), list(message)))
        return "echo-private-media"

    def send_group_message(self, group_id, message):
        self.messages.append(("group", str(group_id), list(message)))
        return "echo-group-media"

    def recv(self, timeout=None):
        return None


class QQParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, self.runtime = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.adapter = QQAdapter(self.runtime, self.config)

    def test_private_event_and_session_id(self):
        msg = self.adapter.parse_event(private_event())
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.platform, "qq")
        self.assertEqual(msg.session_id, "qq:private:1001")
        self.assertEqual(msg.user_id, "1001")
        self.assertIsNone(msg.group_id)
        self.assertFalse(msg.is_group)
        self.assertEqual(msg.text, "你好呀")
        self.assertEqual(msg.event_id, "11")

    def test_group_mention_sets_session_and_reply(self):
        msg = self.adapter.parse_event(group_event(reply=77))
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.session_id, "qq:group:5555")
        self.assertEqual(msg.group_id, "5555")
        self.assertTrue(msg.is_group)
        self.assertTrue(msg.mentioned)
        self.assertEqual(msg.reply_to, "77")

    def test_group_without_mention_is_skipped_and_logged(self):
        self.assertIsNone(self.adapter.parse_event(group_event(mention=False)))
        row = self.db.query_one(
            "SELECT outcome FROM decision_log WHERE kind='qq_group_skip' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "mention_required")

    def test_group_name_trigger(self):
        self.adapter.group_reply_mode = "name"
        self.assertIsNotNone(self.adapter.parse_event(group_event(mention=False, text="鲸鱼娘在吗")))
        self.assertIsNone(self.adapter.parse_event(group_event(mention=False, text="在吗")))

    def test_group_all_and_off_modes(self):
        self.adapter.group_reply_mode = "all"
        self.assertIsNotNone(self.adapter.parse_event(group_event(mention=False)))
        self.adapter.group_reply_mode = "off"
        self.assertIsNone(self.adapter.parse_event(group_event(mention=True)))

    def test_self_messages_are_ignored(self):
        self.assertIsNone(self.adapter.parse_event(private_event(user_id=999)))
        self.assertIsNone(
            self.adapter.parse_event(
                group_event(user_id=999, mention=False, message_id=23)
            )
        )

    def test_private_and_group_whitelists(self):
        self.adapter.private_whitelist = {"2002"}
        self.assertIsNone(self.adapter.parse_event(private_event(user_id=1001)))
        self.assertIsNotNone(self.adapter.parse_event(private_event(user_id=2002)))

        self.adapter.group_whitelist = {"7777"}
        self.assertIsNone(self.adapter.parse_event(group_event(group_id=5555)))
        self.assertIsNotNone(
            self.adapter.parse_event(group_event(group_id=7777, message_id=24))
        )

    def test_empty_text_and_non_message_events(self):
        self.assertIsNone(self.adapter.parse_event(private_event(text="   ")))
        heartbeat = {"post_type": "meta_event", "meta_event_type": "heartbeat"}
        self.assertIsNone(self.adapter.parse_event(heartbeat))
        self.assertIsNone(self.adapter.parse_event(None))

    def test_cq_string_message(self):
        event = private_event()
        event["message"] = "[CQ:reply,id=88][CQ:at,qq=999] 在的"
        msg = self.adapter.parse_event(event)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.text, "在的")
        self.assertEqual(msg.reply_to, "88")
        self.assertTrue(msg.mentioned)

    def test_group_cooldown(self):
        first = self.adapter.parse_event(group_event(message_id=1))
        second = self.adapter.parse_event(group_event(message_id=2))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        row = self.db.query_one(
            "SELECT outcome FROM decision_log WHERE kind='qq_group_skip' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(row["outcome"], "cooldown")

    def test_group_hourly_rate_limit(self):
        self.adapter.group_cooldown_seconds = 0
        self.adapter.group_max_per_hour = 2
        self.assertIsNotNone(self.adapter.parse_event(group_event(message_id=1)))
        self.assertIsNotNone(self.adapter.parse_event(group_event(message_id=2)))
        self.assertIsNone(self.adapter.parse_event(group_event(message_id=3)))
        row = self.db.query_one(
            "SELECT outcome FROM decision_log WHERE kind='qq_group_skip' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(row["outcome"], "group_rate_limit")

    def test_status_does_not_leak_token(self):
        with mock.patch.dict(os.environ, {"QQ_ACCESS_TOKEN": "super-secret-token"}):
            status = self.adapter.status()
        self.assertTrue(status["access_token_set"])
        self.assertNotIn("super-secret-token", json.dumps(status, ensure_ascii=False))


class QQOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, self.runtime = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.runtime.handle_user_message = (
            lambda session_id, text, is_group=False, **kwargs: {
                "messages": ["第一句", "第二句"],
                "reply": "第一句\n第二句",
            }
        )
        self.adapter = QQAdapter(self.runtime, self.config)
        self.fake = FakeOneBotClient()
        self.adapter.client = self.fake

    def test_private_handle_event_enqueues_and_sends(self):
        self.adapter.handle_event(private_event())
        self.assertEqual(
            self.fake.sent,
            [("private", "1001", "第一句"), ("private", "1001", "第二句")],
        )
        rows = self.db.query("SELECT kind, status, payload_json FROM outbox ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["kind"] == "qq_send" for row in rows))
        self.assertTrue(all(row["status"] == "done" for row in rows))
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["target_type"], "private")
        self.assertEqual(payload["target_id"], "1001")

    def test_group_handle_event_payload_and_reply(self):
        self.adapter.handle_event(group_event(reply=909))
        self.assertEqual(
            self.fake.sent,
            [("group", "5555", "第一句"), ("group", "5555", "第二句")],
        )
        rows = self.db.query("SELECT payload_json FROM outbox ORDER BY id")
        first = json.loads(rows[0]["payload_json"])
        second = json.loads(rows[1]["payload_json"])
        self.assertEqual(first["target_type"], "group")
        self.assertEqual(first["target_id"], "5555")
        self.assertEqual(first["reply_to"], "909")
        self.assertIsNone(second["reply_to"])
        keys = {row["idempotency_key"] for row in self.db.query("SELECT idempotency_key FROM outbox")}
        self.assertEqual(keys, {"qq:22:0", "qq:22:1"})

    def test_duplicate_event_is_sent_once(self):
        event = private_event(message_id=555)
        self.adapter.handle_event(event)
        self.adapter.handle_event(event)
        self.assertEqual(len(self.fake.sent), 2)
        count = self.db.query_one("SELECT COUNT(*) AS n FROM outbox")
        self.assertEqual(int(count["n"]), 2)

    def test_duplicate_event_across_adapters_uses_outbox_key(self):
        event = private_event(message_id=556)
        self.adapter.handle_event(event)
        other = QQAdapter(self.runtime, self.config)
        other.client = self.fake
        other.handle_event(event)
        self.assertEqual(len(self.fake.sent), 2)
        count = self.db.query_one("SELECT COUNT(*) AS n FROM outbox")
        self.assertEqual(int(count["n"]), 2)

    def test_qq_send_is_deferred_without_handler(self):
        item_id, _ = self.runtime.outbox.enqueue(
            "qq_send",
            {"target_type": "private", "target_id": "1001", "text": "later"},
            idempotency_key="qq:defer:0",
        )
        results = self.runtime.process_outbox()
        self.assertEqual(results[0]["status"], "deferred")
        row = self.db.query_one("SELECT status, available_at, attempts FROM outbox WHERE id=?", (item_id,))
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["available_at"], time.time() - 1)
        self.assertEqual(int(row["attempts"]), 0)

    def test_bilibili_kind_is_deferred_without_handler(self):
        self.runtime.outbox.enqueue(
            "bilibili_ingest",
            {"bvid": "BV1"},
            idempotency_key="bili:defer",
        )
        results = self.runtime.process_outbox()
        self.assertEqual(results[0]["status"], "deferred")

    def test_qq_proactive_message_is_sent_by_handler(self):
        self.runtime.outbox.enqueue(
            "proactive_message",
            {"text": "想你了", "session_id": "qq:private:1001", "reason": "wakeup"},
            idempotency_key="proactive:test",
        )
        results = self.runtime.process_outbox(handlers=self.adapter.handlers())
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(self.fake.sent, [("private", "1001", "想你了")])

    def test_non_qq_proactive_message_is_skipped(self):
        self.runtime.outbox.enqueue(
            "proactive_message",
            {"text": "网页消息", "session_id": "web:default"},
            idempotency_key="proactive:web",
        )
        results = self.runtime.process_outbox(handlers=self.adapter.handlers())
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(self.fake.sent, [])

    def test_proactive_qq_message_deferred_without_handler(self):
        item_id, _ = self.runtime.outbox.enqueue(
            "proactive_message",
            {"text": "晚点发", "session_id": "qq:group:5555"},
            idempotency_key="proactive:qq:defer",
        )
        results = self.runtime.process_outbox()
        self.assertEqual(results[0]["status"], "deferred")
        row = self.db.query_one("SELECT status FROM outbox WHERE id=?", (item_id,))
        self.assertEqual(row["status"], "pending")

    def test_message_is_trimmed_to_config_limit(self):
        self.adapter.max_message_chars = 10
        self.adapter.handle_event(private_event())
        for _, _, text in self.fake.sent:
            self.assertLessEqual(len(text), 10)


class QQMediaTests(unittest.TestCase):
    """QQ voice/image delivery through outbox payloads and OneBot segments."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config, self.db, self.runtime = make_env(self.root)
        self.addCleanup(self.db.close)
        self.adapter = QQAdapter(self.runtime, self.config)
        self.fake = FakeOneBotClient()
        self.adapter.client = self.fake
        self.image = self.root / "face.PNG"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
        self.voice = self.root / "line.wav"
        self.voice.write_bytes(b"RIFFfake-wav-bytes")

    def _result(self, **overrides):
        result = {"messages": ["你好"], "reply": "你好", "tts": None, "sticker": None}
        result.update(overrides)
        self.runtime.handle_user_message = lambda *args, **kwargs: result
        return result

    def _enqueue(self, payload, key):
        self.runtime.outbox.enqueue("qq_send", payload, idempotency_key=key)
        return self.runtime.process_outbox(handlers=self.adapter.handlers())

    @staticmethod
    def _b64(segment):
        return base64.b64decode(segment["data"]["file"].split("base64://", 1)[1])

    def test_image_segment_base64_after_text(self):
        results = self._enqueue(
            {
                "target_type": "private",
                "target_id": "1001",
                "text": "看图",
                "media": [{"type": "image", "path": str(self.image), "name": "face.png"}],
            },
            "qq:media:image",
        )
        self.assertEqual(results[0]["status"], "done")
        target_type, target_id, segments = self.fake.messages[0]
        self.assertEqual((target_type, target_id), ("private", "1001"))
        self.assertEqual(segments[0], {"type": "text", "data": {"text": "看图"}})
        self.assertEqual(segments[1]["type"], "image")
        self.assertEqual(segments[1]["data"]["sub_type"], 0)
        self.assertEqual(self._b64(segments[1]), self.image.read_bytes())

    def test_voice_segment_base64_without_text(self):
        results = self._enqueue(
            {
                "target_type": "private",
                "target_id": "1001",
                "media": [{"type": "voice", "path": str(self.voice)}],
            },
            "qq:media:voice",
        )
        self.assertEqual(results[0]["status"], "done")
        _, _, segments = self.fake.messages[0]
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["type"], "record")
        self.assertEqual(self._b64(segments[0]), self.voice.read_bytes())

    def test_group_media_uses_group_message(self):
        self._enqueue(
            {
                "target_type": "group",
                "target_id": "5555",
                "text": "听一下",
                "media": [{"type": "voice", "path": str(self.voice)}],
            },
            "qq:media:group",
        )
        target_type, target_id, segments = self.fake.messages[0]
        self.assertEqual((target_type, target_id), ("group", "5555"))
        self.assertEqual([s["type"] for s in segments], ["text", "record"])
        self.assertEqual(self.fake.sent, [])

    def test_invalid_media_skips_but_text_is_sent(self):
        missing = self.root / "nope.png"
        results = self._enqueue(
            {
                "target_type": "private",
                "target_id": "1001",
                "text": "还在吗",
                "media": [{"type": "image", "path": str(missing)}],
            },
            "qq:media:missing",
        )
        self.assertEqual(results[0]["status"], "done")
        inner = results[0]["result"]
        self.assertEqual(inner["media_sent"], 0)
        self.assertEqual(inner["media_errors"][0]["error"], "missing_file")
        _, _, segments = self.fake.messages[0]
        self.assertEqual(segments, [{"type": "text", "data": {"text": "还在吗"}}])

    def test_media_only_invalid_file_fails(self):
        results = self._enqueue(
            {
                "target_type": "private",
                "target_id": "1001",
                "media": [{"type": "voice", "path": str(self.root / "gone.wav")}],
            },
            "qq:media:only-missing",
        )
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("no_sendable_media", results[0]["error"])
        self.assertEqual(self.fake.messages, [])

    def test_oversized_and_unsupported_files(self):
        self.adapter.media_max_bytes = 4
        note = self.root / "note.txt"
        note.write_bytes(b"hello")
        results = self._enqueue(
            {
                "target_type": "private",
                "target_id": "1001",
                "text": "hi",
                "media": [
                    {"type": "image", "path": str(self.image)},
                    {"type": "image", "path": str(note)},
                    {"type": "video", "path": str(self.image)},
                ],
            },
            "qq:media:invalid",
        )
        self.assertEqual(results[0]["status"], "done")
        errors = {item["error"] for item in results[0]["result"]["media_errors"]}
        self.assertEqual(
            errors, {"too_large", "unsupported_suffix", "unsupported_media_type"}
        )
        self.assertEqual(self.fake.messages[0][2][0]["type"], "text")

    def test_tts_voice_private_queued(self):
        self._result(tts={"ok": True, "audio_path": str(self.voice)})
        result = self.adapter.handle_event(private_event(message_id=801))
        self.assertEqual(result["media_queued"], 1)
        self.assertEqual(result["media_errors"], [])
        # The voice already carries the words, so no duplicate text lines.
        self.assertEqual(self.fake.sent, [])
        _, _, segments = self.fake.messages[0]
        self.assertEqual(segments[0]["type"], "record")
        self.assertEqual(self._b64(segments[0]), self.voice.read_bytes())
        keys = {
            row["idempotency_key"]
            for row in self.db.query("SELECT idempotency_key FROM outbox")
        }
        self.assertIn("qq:801:voice", keys)

    def test_voice_can_keep_text_when_opted_in(self):
        self.adapter.send_text_with_voice = True
        self._result(tts={"ok": True, "audio_path": str(self.voice)})
        self.adapter.handle_event(private_event(message_id=811))
        self.assertEqual(self.fake.sent, [("private", "1001", "你好")])

    def test_voice_keeps_text_appended_after_synthesis(self):
        self._result(
            messages=["你好", "照片生成好啦"],
            tts={
                "ok": True,
                "audio_path": str(self.voice),
                "covered_messages": 1,
            },
        )
        self.adapter.handle_event(private_event(message_id=812))
        self.assertEqual(self.fake.sent, [("private", "1001", "照片生成好啦")])

    def test_tts_voice_respects_switches_and_failures(self):
        self._result(tts={"ok": True, "audio_path": str(self.voice)})
        self.adapter.send_voice = False
        result = self.adapter.handle_event(private_event(message_id=802))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])

        self.adapter.send_voice = True
        self._result(tts={"ok": False, "audio_path": str(self.voice)})
        result = self.adapter.handle_event(private_event(message_id=803))
        self.assertEqual(result["media_queued"], 0)

        self._result(tts={"ok": True, "audio_path": None})
        result = self.adapter.handle_event(private_event(message_id=804))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(result["media_errors"][0]["error"], "voice_path_missing")

    def test_group_voice_is_private_only_by_default(self):
        self.adapter.group_cooldown_seconds = 0
        self._result(tts={"ok": True, "audio_path": str(self.voice)})
        result = self.adapter.handle_event(group_event(message_id=805))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])
        self.assertEqual(self.fake.sent, [("group", "5555", "你好")])

        self.adapter.send_voice_in_group = True
        result = self.adapter.handle_event(group_event(message_id=806))
        self.assertEqual(result["media_queued"], 1)
        target_type, target_id, segments = self.fake.messages[0]
        self.assertEqual((target_type, target_id), ("group", "5555"))
        self.assertEqual(segments[0]["type"], "record")

    def test_sticker_lookup_queued_as_image(self):
        self.runtime.sticker.add_sticker(
            str(self.image), caption="开心", intents=["happy"], sticker_id="stk-happy"
        )
        self._result(sticker={"sent": True, "sticker_id": "stk-happy"})
        result = self.adapter.handle_event(private_event(message_id=807))
        self.assertEqual(result["media_queued"], 1)
        self.assertEqual(result["media_errors"], [])
        _, _, segments = self.fake.messages[0]
        self.assertEqual(segments[0]["type"], "image")
        self.assertEqual(self._b64(segments[0]), self.image.read_bytes())

    def test_sticker_direct_path_and_missing_lookup(self):
        self._result(
            sticker={"sent": True, "sticker_id": "unknown", "file_path": str(self.image)}
        )
        result = self.adapter.handle_event(private_event(message_id=808))
        self.assertEqual(result["media_queued"], 1)
        self.assertEqual(self.fake.messages[0][2][0]["type"], "image")

        self._result(sticker={"sent": True, "sticker_id": "not-in-db"})
        result = self.adapter.handle_event(private_event(message_id=809))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(result["media_errors"][0]["error"], "sticker_not_found")
        self.assertEqual(self.fake.sent, [
            ("private", "1001", "你好"),
            ("private", "1001", "你好"),
        ])

    def test_sticker_disabled_or_not_sent(self):
        self.runtime.sticker.add_sticker(
            str(self.image), caption="开心", intents=["happy"], sticker_id="stk-off"
        )
        self._result(sticker={"sent": True, "sticker_id": "stk-off"})
        self.adapter.send_images = False
        result = self.adapter.handle_event(private_event(message_id=810))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])

        self.adapter.send_images = True
        self._result(sticker={"sent": False, "sticker_id": "stk-off"})
        result = self.adapter.handle_event(private_event(message_id=811))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])

    def test_media_idempotency_across_adapters(self):
        self._result(tts={"ok": True, "audio_path": str(self.voice)})
        event = private_event(message_id=812)
        self.adapter.handle_event(event)
        other = QQAdapter(self.runtime, self.config)
        other.client = self.fake
        result = other.handle_event(event)
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(len(self.fake.messages), 1)
        self.assertEqual(len(self.fake.sent), 0)

    def test_handle_event_reports_media_send_error(self):
        self._result(tts={"ok": True, "audio_path": str(self.root / "gone.wav")})
        result = self.adapter.handle_event(private_event(message_id=813))
        self.assertEqual(result["media_queued"], 1)
        self.assertEqual(self.fake.sent, [("private", "1001", "你好")])
        errors = {item.get("error") for item in result["media_errors"]}
        self.assertIn("no_sendable_media", errors)


class QQVoiceStreamTests(unittest.TestCase):
    """Sentence-level voice streaming through the QQ outbox."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config, self.db, self.runtime = make_env(self.root)
        self.addCleanup(self.db.close)
        self.adapter = QQAdapter(self.runtime, self.config)
        self.fake = FakeOneBotClient()
        self.adapter.client = self.fake
        self.voices = []
        for name in ("one", "two", "three"):
            path = self.root / f"{name}.wav"
            path.write_bytes(f"RIFF-{name}".encode())
            self.voices.append(path)
        self.image = self.root / "face.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nstream-image")
        self.callback_log: list[tuple[str, int, int]] = []

    def _install_stream_runtime(self, pairs: list[tuple[str, TTSResult]]) -> None:
        """A fake runtime that drives the adapter callback in iterator order."""

        def handle(
            session_id,
            text,
            *,
            is_group=False,
            voice_stream=None,
            now=None,
            **kwargs,
        ):
            self.assertIsNotNone(voice_stream)
            for segment, tts_result in pairs:
                voice_stream(segment, tts_result)
                self.callback_log.append(
                    (segment, len(self.fake.messages), len(self.fake.sent))
                )
            ok_results = [result for _, result in pairs if result.ok]
            first = ok_results[0] if ok_results else None
            return {
                "messages": ["你好"],
                "reply": "你好",
                "tts": {
                    "ok": first is not None,
                    "audio_path": first.audio_path if first else None,
                    "format": "wav",
                    "cached": False,
                    "error": None,
                    "streamed": True,
                    "segments": [
                        {
                            "text": segment,
                            "ok": result.ok,
                            "audio_path": result.audio_path,
                            "format": result.format,
                            "cached": result.cached,
                            "error": result.error,
                        }
                        for segment, result in pairs
                    ],
                    "segments_ok": len(ok_results),
                    "segments_total": len(pairs),
                },
                "sticker": None,
            }

        self.runtime.handle_user_message = handle

    def _keys(self) -> set[str]:
        return {
            row["idempotency_key"]
            for row in self.db.query("SELECT idempotency_key FROM outbox")
        }

    def test_private_streams_each_successful_segment_immediately(self):
        pairs = [
            ("第一句。", TTSResult(True, audio_path=str(self.voices[0]))),
            ("第二句。", TTSResult(False, error="synthetic_error")),
            ("第三句。", TTSResult(True, audio_path=str(self.voices[1]))),
        ]
        self._install_stream_runtime(pairs)

        result = self.adapter.handle_event(private_event(message_id=901))

        # The callback ran for every segment, and each successful sentence was
        # already sent when the callback returned (before the next segment).
        self.assertEqual(
            self.callback_log,
            [("第一句。", 1, 0), ("第二句。", 1, 0), ("第三句。", 2, 0)],
        )
        self.assertEqual(result["media_queued"], 2)
        self.assertEqual(result["media_errors"], [])
        # One segment failed, so the text reply is still sent as a fallback.
        self.assertEqual(self.fake.sent, [("private", "1001", "你好")])
        self.assertEqual(len(self.fake.messages), 2)
        self.assertTrue(all(item[2][0]["type"] == "record" for item in self.fake.messages))
        self.assertEqual(
            self._keys(),
            {"qq:901:0", "qq:901:voice:0", "qq:901:voice:1"},
        )
        # Failed segments produce no media item, and the full reply is never
        # synthesized/sent a second time.
        self.assertNotIn("qq:901:voice", self._keys())

    def test_group_voice_stream_respects_policy(self):
        self.adapter.group_cooldown_seconds = 0
        pairs = [
            ("第一句。", TTSResult(True, audio_path=str(self.voices[0]))),
            ("第二句。", TTSResult(True, audio_path=str(self.voices[1]))),
        ]
        self._install_stream_runtime(pairs)

        result = self.adapter.handle_event(group_event(message_id=902))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])
        self.assertEqual(self.fake.sent, [("group", "5555", "你好")])
        self.assertNotIn("qq:902:voice:0", self._keys())

        self.adapter.send_voice_in_group = True
        result = self.adapter.handle_event(group_event(message_id=903))
        self.assertEqual(result["media_queued"], 2)
        self.assertEqual(len(self.fake.messages), 2)
        self.assertEqual([item[0] for item in self.fake.messages], ["group", "group"])
        self.assertEqual(
            self._keys(),
            {
                "qq:902:0",
                "qq:903:voice:0",
                "qq:903:voice:1",
            },
        )

    def test_send_voice_switch_disables_streaming(self):
        self._install_stream_runtime(
            [("第一句。", TTSResult(True, audio_path=str(self.voices[0])))]
        )
        self.adapter.send_voice = False
        result = self.adapter.handle_event(private_event(message_id=904))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])
        self.assertEqual(self.fake.sent, [("private", "1001", "你好")])

    def test_streamed_voice_skips_full_tts_but_keeps_sticker(self):
        pairs = [("第一句。", TTSResult(True, audio_path=str(self.voices[0])))]
        self._install_stream_runtime(pairs)
        self.runtime.sticker.add_sticker(
            str(self.image), caption="开心", intents=["happy"], sticker_id="stk-stream"
        )
        original = self.runtime.handle_user_message

        def handle(*args, **kwargs):
            result = original(*args, **kwargs)
            result["sticker"] = {"sent": True, "sticker_id": "stk-stream"}
            return result

        self.runtime.handle_user_message = handle

        result = self.adapter.handle_event(private_event(message_id=905))

        self.assertEqual(result["media_queued"], 2)
        self.assertEqual(result["media_errors"], [])
        kinds = [item[2][0]["type"] for item in self.fake.messages]
        self.assertEqual(kinds, ["record", "image"])
        self.assertEqual(
            self._keys(),
            {
                "qq:905:voice:0",
                "qq:905:image",
            },
        )

    def test_streamed_voice_without_path_records_error(self):
        self._install_stream_runtime(
            [("第一句。", TTSResult(True, audio_path=None))]
        )
        result = self.adapter.handle_event(private_event(message_id=906))
        self.assertEqual(result["media_queued"], 0)
        self.assertEqual(self.fake.messages, [])
        self.assertEqual(
            [item["error"] for item in result["media_errors"]],
            ["voice_path_missing"],
        )

    def test_streamed_send_failure_is_reported(self):
        missing = self.root / "gone.wav"
        self._install_stream_runtime(
            [("第一句。", TTSResult(True, audio_path=str(missing)))]
        )
        result = self.adapter.handle_event(private_event(message_id=907))
        self.assertEqual(result["media_queued"], 1)
        self.assertEqual(self.fake.messages, [])
        errors = {item.get("error") for item in result["media_errors"]}
        self.assertIn("no_sendable_media", errors)


class OneBotClientTests(unittest.TestCase):
    def test_missing_websocket_dependency_raises_clear_error(self):
        with mock.patch.object(onebot_module, "websocket", None):
            client = OneBotClient("ws://127.0.0.1:3001")
            with self.assertRaises(RuntimeError) as ctx:
                client.connect()
        self.assertIn("pip install websocket-client", str(ctx.exception))

    def test_send_recv_and_auth_header(self):
        class FakeSocket:
            connected = True

            def __init__(self):
                self.sent: list[dict] = []
                self.timeout = None
                self.closed = False

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def settimeout(self, value):
                self.timeout = value

            def recv(self):
                return json.dumps({"post_type": "message", "message_id": 1})

            def close(self):
                self.connected = False
                self.closed = True

        class FakeWebSocketModule:
            WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})

            def __init__(self):
                self.socket = FakeSocket()
                self.url = None
                self.kwargs: dict = {}

            def create_connection(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return self.socket

        fake_module = FakeWebSocketModule()
        with mock.patch.object(onebot_module, "websocket", fake_module):
            client = OneBotClient("ws://127.0.0.1:3001", access_token="tok", timeout=2)
            client.connect()
            self.assertEqual(fake_module.url, "ws://127.0.0.1:3001")
            self.assertEqual(fake_module.kwargs["timeout"], 2)
            self.assertEqual(fake_module.kwargs["header"], ["Authorization: Bearer tok"])
            self.assertIn("ping_interval", fake_module.kwargs)
            self.assertIn("ping_timeout", fake_module.kwargs)

            client.send_private_text(1001, "hi")
            client.send_group_text(5555, "hello")
            event = client.recv(timeout=1)
            client.close()

        sent = fake_module.socket.sent
        self.assertEqual(sent[0]["action"], "send_private_msg")
        self.assertEqual(sent[0]["params"], {"user_id": 1001, "message": "hi"})
        self.assertEqual(sent[1]["action"], "send_group_msg")
        self.assertEqual(sent[1]["params"], {"group_id": 5555, "message": "hello"})
        self.assertIn("echo", sent[0])
        self.assertEqual(event["message_id"], 1)
        self.assertTrue(fake_module.socket.closed)

    def test_send_message_segment_lists(self):
        class FakeSocket:
            connected = True

            def __init__(self):
                self.sent: list[dict] = []

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def settimeout(self, value):
                pass

            def recv(self):
                return "{}"

            def close(self):
                self.connected = False

        class FakeWebSocketModule:
            WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})

            def __init__(self):
                self.socket = FakeSocket()

            def create_connection(self, url, **kwargs):
                return self.socket

        fake_module = FakeWebSocketModule()
        with mock.patch.object(onebot_module, "websocket", fake_module):
            client = OneBotClient()
            client.connect()
            client.send_private_message(
                1001, [{"type": "text", "data": {"text": "hi"}}]
            )
            client.send_group_message(
                5555,
                [{"type": "image", "data": {"file": "base64://QQ==", "sub_type": 0}}],
            )

        sent = fake_module.socket.sent
        self.assertEqual(sent[0]["action"], "send_private_msg")
        self.assertEqual(
            sent[0]["params"],
            {"user_id": 1001, "message": [{"type": "text", "data": {"text": "hi"}}]},
        )
        self.assertEqual(sent[1]["action"], "send_group_msg")
        self.assertEqual(
            sent[1]["params"],
            {
                "group_id": 5555,
                "message": [
                    {"type": "image", "data": {"file": "base64://QQ==", "sub_type": 0}}
                ],
            },
        )

    def test_send_without_connection_raises(self):
        with mock.patch.object(onebot_module, "websocket", object()):
            client = OneBotClient()
            with self.assertRaises(ConnectionError):
                client.send_action("get_login_info")

    def test_recv_timeout_returns_none(self):
        class TimeoutWS:
            connected = True

            def settimeout(self, value):
                pass

            def recv(self):
                raise RuntimeError("timeout")

        class FakeModule:
            WebSocketTimeoutException = RuntimeError

            def create_connection(self, url, **kwargs):
                return TimeoutWS()

        with mock.patch.object(onebot_module, "websocket", FakeModule()):
            client = OneBotClient()
            client.connect()
            self.assertIsNone(client.recv(timeout=1))


class BilibiliWbiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, _runtime = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.client = BilibiliClient(self.config)
        self.client._throttle = lambda: None
        self.client._wbi_cache = {
            "img_key": "7cd084941338484aae1ad9425b84077c",
            "sub_key": "4932caff0ff746eab6f01bf08b70ac45",
            "mixin_key": "ea1db124af3c7062474693fa704f4ff8",
        }
        self.client._wbi_cached_at = time.time()

    def test_wbi_keys_derivation(self):
        fresh = BilibiliClient(self.config)
        with mock.patch.object(
            fresh,
            "nav",
            return_value={
                "is_login": False,
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                },
            },
        ):
            keys = fresh.wbi_keys()
        self.assertEqual(keys["img_key"], "7cd084941338484aae1ad9425b84077c")
        self.assertEqual(keys["sub_key"], "4932caff0ff746eab6f01bf08b70ac45")
        self.assertEqual(keys["mixin_key"], "ea1db124af3c7062474693fa704f4ff8")

    def test_sign_known_vector(self):
        signed = self.client.sign(
            {"foo": 114, "bar": 514, "zab": 1919810}, wts=1702204169
        )
        self.assertEqual(signed["wts"], "1702204169")
        self.assertEqual(signed["w_rid"], "8f6f2b5b3d485fe1886cec6a0be8c5d4")

    def test_sign_is_deterministic_and_strips_forbidden_chars(self):
        first = self.client.sign({"keyword": "a!b(c)", "page": 1}, wts=1700000000)
        second = self.client.sign({"page": 1, "keyword": "a!b(c)"}, wts=1700000000)
        self.assertEqual(first, second)
        self.assertEqual(first["keyword"], "abc")

    def test_wbi_cache_is_reused(self):
        self.client._wbi_cache = None
        calls = {"n": 0}

        def fake_nav():
            calls["n"] += 1
            return {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                }
            }

        with mock.patch.object(self.client, "nav", side_effect=fake_nav):
            self.client.wbi_keys()
            self.client.wbi_keys()
        self.assertEqual(calls["n"], 1)


class BilibiliCookieTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, _runtime = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for key in BILI_ENV_KEYS:
            os.environ.pop(key, None)

    def test_login_with_cookie_saves_and_reloads(self):
        client = BilibiliClient(self.config)
        cookies = client.login_with_cookie(
            "SESSDATA=abc%2C123; bili_jct=csrf-token; DedeUserID=4242"
        )
        self.assertEqual(cookies["SESSDATA"], "abc%2C123")
        self.assertTrue(client.cookie_path.exists())
        raw = json.loads(client.cookie_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["bili_jct"], "csrf-token")
        if os.name != "nt":
            self.assertEqual(client.cookie_path.stat().st_mode & 0o777, 0o600)

        reloaded = BilibiliClient.from_config(self.config)
        self.assertEqual(reloaded.cookies["SESSDATA"], "abc%2C123")
        self.assertEqual(reloaded.cookies["DedeUserID"], "4242")

    def test_env_cookies_win_over_file(self):
        BilibiliClient(self.config).login_with_cookie("SESSDATA=from-file; bili_jct=x")
        os.environ["BILIBILI_SESSDATA"] = "from-env"
        os.environ["BILIBILI_BILI_JCT"] = "env-jct"
        client = BilibiliClient.from_config(self.config)
        self.assertEqual(client.cookies["SESSDATA"], "from-env")
        self.assertEqual(client.cookies["bili_jct"], "env-jct")

    def test_full_cookie_env_parses(self):
        os.environ["BILIBILI_COOKIE"] = "SESSDATA=full; bili_jct=full-jct; DedeUserID=7"
        client = BilibiliClient.from_config(self.config)
        self.assertEqual(client.cookies["SESSDATA"], "full")
        self.assertEqual(client.cookies["DedeUserID"], "7")

    def test_save_and_load_explicit_path(self):
        target = Path(self.tmp.name) / "nested" / "cookies.json"
        client = BilibiliClient(self.config, cookies={"SESSDATA": "x"})
        client.save_cookies(target)
        other = BilibiliClient(self.config)
        loaded = other.load_cookies(target)
        self.assertEqual(loaded, {"SESSDATA": "x"})

    def test_parse_cookie_string(self):
        self.assertEqual(
            parse_cookie_string(" SESSDATA=a ; ; bili_jct=b "),
            {"SESSDATA": "a", "bili_jct": "b"},
        )

    def test_clean_cookie_mapping_drops_routing_metadata_and_attributes(self):
        cleaned = _clean_cookie_mapping(
            {
                "SESSDATA": "sess-value",
                "bili_jct": "jct-value",
                "first_domain": ".bilibili.com",
                "gourl": "https://www.bilibili.com",
                "ticket": "routing-ticket",
                "path": "/",
                "domain": ".bilibili.com",
                "expires": "Wed, 21 Oct 2026 07:28:00 GMT",
                "empty": "",
                "blank": "   ",
                "newline": "a\nb",
                "carriage": "a\rb",
                "semicolon": "a;b",
                "bad\nkey": "value",
                "numeric": 42,
            }
        )
        self.assertEqual(
            cleaned, {"SESSDATA": "sess-value", "bili_jct": "jct-value"}
        )
        self.assertEqual(_clean_cookie_mapping(None), {})

    def test_parse_login_cookies_from_url_query(self):
        cookies = parse_login_cookies(
            "https://passport.bilibili.com/login?"
            "SESSDATA=sess%2Cdata&bili_jct=jct&DedeUserID=42"
            "&first_domain=.bilibili.com&gourl=https%3A%2F%2Fwww.bilibili.com"
            "&ticket=abc&empty="
        )
        self.assertEqual(cookies["SESSDATA"], "sess,data")
        self.assertEqual(cookies["bili_jct"], "jct")
        self.assertEqual(cookies["DedeUserID"], "42")
        self.assertNotIn("first_domain", cookies)
        self.assertNotIn("gourl", cookies)
        self.assertNotIn("ticket", cookies)
        self.assertNotIn("empty", cookies)

    def test_parse_login_cookies_from_set_cookie_and_priority(self):
        cookies = parse_login_cookies(
            "https://passport.bilibili.com/login?SESSDATA=from-url&bili_jct=from-url",
            set_cookie_headers=[
                "SESSDATA=from-cookie; Path=/; Domain=.bilibili.com; HttpOnly",
                "bili_jct=from-cookie; Path=/; Max-Age=3600",
                "DedeUserID=4242; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT",
                "ticket=routing; Path=/",
            ],
            jar_cookies={"SESSDATA": "from-jar", "buvid3": "jar-buvid"},
        )
        self.assertEqual(cookies["SESSDATA"], "from-cookie")
        self.assertEqual(cookies["bili_jct"], "from-cookie")
        self.assertEqual(cookies["DedeUserID"], "4242")
        self.assertEqual(cookies["buvid3"], "jar-buvid")
        for attribute in ("Path", "Domain", "Expires", "Max-Age", "ticket"):
            self.assertNotIn(attribute, cookies)

    def test_from_config_ignores_legacy_routing_only_cookie_file(self):
        client = BilibiliClient(self.config)
        client.cookie_path.write_text(
            json.dumps(
                {
                    "first_domain": ".bilibili.com",
                    "gourl": "https://www.bilibili.com",
                    "ticket": "legacy-ticket",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(client.load_cookies(), {})
        self.assertEqual(client.cookies, {})

        reloaded = BilibiliClient.from_config(self.config)
        self.assertFalse(reloaded.has_login_cookie)
        self.assertFalse(bool(reloaded.cookies))


class BilibiliApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, _runtime = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.client = BilibiliClient(self.config)
        self.client._throttle = lambda: None
        self.client._wbi_cache = {
            "img_key": "7cd084941338484aae1ad9425b84077c",
            "sub_key": "4932caff0ff746eab6f01bf08b70ac45",
            "mixin_key": "ea1db124af3c7062474693fa704f4ff8",
        }
        self.client._wbi_cached_at = time.time()

    def test_qr_poll_status_parsing(self):
        responses = [
            {"code": 86101, "message": "waiting"},
            {"code": 86090, "message": "scanned"},
            {"code": 86038, "message": "expired"},
            {
                "code": 0,
                "message": "ok",
                "url": "https://passport.bilibili.com/login?SESSDATA=sess%2Cdata&bili_jct=jct&DedeUserID=42&sid=abc",
            },
        ]
        with mock.patch.object(self.client, "request", side_effect=responses):
            self.assertEqual(self.client.qr_poll("k")["status"], "waiting")
            self.assertEqual(self.client.qr_poll("k")["status"], "scanned")
            self.assertEqual(self.client.qr_poll("k")["status"], "expired")
            result = self.client.qr_poll("k")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cookies"]["SESSDATA"], "sess,data")
        self.assertEqual(result["cookies"]["DedeUserID"], "42")
        self.assertEqual(self.client.cookies["SESSDATA"], "sess,data")
        self.assertTrue(self.client.cookie_path.exists())

    def test_qr_poll_unknown_code_is_error(self):
        with mock.patch.object(
            self.client, "request", return_value={"code": 12345, "message": "boom"}
        ):
            result = self.client.qr_poll("k")
        self.assertEqual(result["status"], "error")

    def test_qr_poll_success_saves_clean_cookie_file(self):
        self.client.last_set_cookie_headers = [
            "SESSDATA=cookie-sess; Path=/; Domain=.bilibili.com; HttpOnly",
            "bili_jct=cookie-jct; Path=/",
            "DedeUserID=4242; Path=/",
            "first_domain=.bilibili.com; Path=/",
            "ticket=routing-ticket; Path=/",
        ]
        response = {
            "code": 0,
            "message": "ok",
            "url": (
                "https://passport.bilibili.com/login?"
                "SESSDATA=url-sess&bili_jct=url-jct&DedeUserID=99"
                "&gourl=https%3A%2F%2Fwww.bilibili.com&ticket=url-ticket"
            ),
        }
        with mock.patch.object(self.client, "request", return_value=response):
            result = self.client.qr_poll("qrcode-key")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cookies"]["SESSDATA"], "cookie-sess")
        self.assertEqual(result["cookies"]["DedeUserID"], "4242")

        raw = json.loads(self.client.cookie_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["SESSDATA"], "cookie-sess")
        self.assertEqual(raw["bili_jct"], "cookie-jct")
        self.assertEqual(raw["DedeUserID"], "4242")
        for routing in ("first_domain", "gourl", "ticket", "path", "domain"):
            self.assertNotIn(routing, raw)
        self.assertEqual(self.client.cookies["SESSDATA"], "cookie-sess")

    def test_search_normalization(self):
        payload = {
            "result": [
                {
                    "type": "video",
                    "bvid": "BV1xx411c7mD",
                    "title": '<em class="keyword">鲸鱼</em> 观察日记',
                    "description": "一条很大的鱼",
                    "author": "up主",
                    "pubdate": 1700000000,
                    "play": 123456,
                    "tag": "动物,海洋",
                }
            ]
        }
        with mock.patch.object(self.client, "request", return_value=payload) as req:
            videos = self.client.search("鲸鱼", limit=1)
        self.assertEqual(len(videos), 1)
        video = videos[0]
        self.assertEqual(video["bvid"], "BV1xx411c7mD")
        self.assertEqual(video["title"], "鲸鱼 观察日记")
        self.assertEqual(video["up"], "up主")
        self.assertEqual(video["topics"], ["动物", "海洋"])
        self.assertEqual(video["published_at"], 1700000000.0)
        self.assertEqual(video["url"], "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertGreater(video["hot_score"], 0)
        self.assertIn("w_rid", req.call_args.args[1])

    def test_popular_normalization(self):
        payload = {
            "list": [
                {
                    "bvid": "BV2",
                    "title": "热门视频",
                    "desc": "描述",
                    "pubdate": 1700000001,
                    "owner": {"name": "大UP"},
                    "stat": {"view": 1000},
                    "tname": "知识",
                }
            ]
        }
        with mock.patch.object(self.client, "request", return_value=payload):
            videos = self.client.popular(limit=5)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["up"], "大UP")
        self.assertEqual(videos[0]["topics"], ["知识"])
        self.assertEqual(videos[0]["desc"], "描述")

    def test_browse_dedupes_by_bvid(self):
        duplicate = [
            {"bvid": "BV1", "title": "a"},
            {"bvid": "BV1", "title": "a again"},
            {"bvid": "BV2", "title": "b"},
        ]
        with mock.patch.object(self.client, "popular", return_value=duplicate):
            videos = self.client.browse(limit=5)
        self.assertEqual([v["bvid"] for v in videos], ["BV1", "BV2"])

    def test_nav_anonymous_is_not_an_error(self):
        error = BilibiliError(
            "not logged in",
            code=-101,
            data={"data": {"isLogin": False, "wbi_img": {"img_url": "x.png"}}},
        )
        with mock.patch.object(self.client, "request", side_effect=error):
            nav = self.client.nav()
        self.assertFalse(nav["is_login"])
        self.assertEqual(nav["wbi_img"], {"img_url": "x.png"})

    def test_request_raises_bilibili_error_on_api_code(self):
        body = io.BytesIO(json.dumps({"code": -400, "message": "bad request"}).encode())
        error = urllib.error.HTTPError(
            "https://api.bilibili.com/x/test", 400, "Bad Request", {}, body
        )
        with mock.patch.object(self.client._opener, "open", side_effect=error):
            with self.assertRaises(BilibiliError) as ctx:
                self.client.request("/x/test")
        self.assertEqual(ctx.exception.code, -400)

    def test_bvid_to_aid_known_vectors(self):
        # Public algorithm reference vectors.
        self.assertEqual(bvid_to_aid("BV17x411w7KC"), 170001)
        self.assertEqual(bvid_to_aid("BV1xx411c7mD"), 2)
        for bad in ("", "BV1", "AV17x411w7KC", "BV17x411w7K!", "BV171x411w7KC1"):
            with self.assertRaises(ValueError):
                bvid_to_aid(bad)

    def test_video_comments_parses_reply_fixture(self):
        payload = {
            "replies": [
                {"member": {"uname": "用户甲"}, "content": {"message": "第一条评论"}},
                {
                    "member": {"uname": "用户乙"},
                    "content": {"message": "<b>带标签</b>的评论 &amp; 内容"},
                },
                None,
                {"member": {"uname": "用户丙"}, "content": {"message": "   "}},
            ]
        }
        with mock.patch.object(self.client, "request", return_value=payload) as req:
            comments = self.client.video_comments("BV17x411w7KC")
        self.assertEqual(comments, ["用户甲: 第一条评论", "用户乙: 带标签的评论 & 内容"])
        params = req.call_args.args[1]
        # WBI signed like search; oid is the converted aid, page size bounded.
        self.assertIn("w_rid", params)
        self.assertIn("wts", params)
        self.assertEqual(params["oid"], "170001")
        self.assertEqual(params["type"], "1")
        self.assertEqual(params["mode"], "3")
        self.assertEqual(params["ps"], "10")  # config default comments_per_video
        self.assertEqual(req.call_args.args[0], "https://api.bilibili.com/x/v2/reply/wbi/main")

    def test_video_comments_truncates_long_content(self):
        payload = {
            "replies": [
                {"member": {"uname": "话痨"}, "content": {"message": "字" * 500}},
                {"member": {"uname": "短评"}, "content": {"message": "刚刚好"}},
            ]
        }
        with mock.patch.object(self.client, "request", return_value=payload):
            comments = self.client.video_comments("BV17x411w7KC", limit=2)
        self.assertTrue(comments[0].startswith("话痨: "))
        self.assertLessEqual(len(comments[0]) - len("话痨: "), MAX_COMMENT_CHARS)
        self.assertTrue(comments[0].endswith("…"))
        self.assertEqual(comments[1], "短评: 刚刚好")

    def test_video_comments_caps_at_limit(self):
        replies = [
            {"member": {"uname": f"u{i}"}, "content": {"message": f"c{i}"}}
            for i in range(15)
        ]
        with mock.patch.object(self.client, "request", return_value={"replies": replies}):
            comments = self.client.video_comments("BV17x411w7KC", limit=3)
        self.assertEqual(comments, ["u0: c0", "u1: c1", "u2: c2"])

    def test_video_comments_missing_replies_is_empty(self):
        for payload in ({}, {"replies": None}, {"replies": []}, None):
            with mock.patch.object(self.client, "request", return_value=payload):
                self.assertEqual(self.client.video_comments("BV17x411w7KC", limit=3), [])

    def test_video_comments_disabled_sends_no_request(self):
        self.config._data["bilibili"]["comments_per_video"] = 0
        client = BilibiliClient(self.config)
        with mock.patch.object(client, "request") as req:
            self.assertEqual(client.video_comments("BV17x411w7KC"), [])
            req.assert_not_called()
        # An explicit limit=0 gates the same way on a default client.
        with mock.patch.object(self.client, "request") as req2:
            self.assertEqual(self.client.video_comments("BV17x411w7KC", limit=0), [])
            req2.assert_not_called()

    def test_video_comments_invalid_bvid_raises_without_request(self):
        with mock.patch.object(self.client, "request") as req:
            with self.assertRaises(ValueError):
                self.client.video_comments("not-a-bvid")
            req.assert_not_called()


class BilibiliCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, _runtime = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.memory = MemoryService(self.db, self.config)
        self.collector = BilibiliCollector(self.memory, self.config)

    def _videos(self):
        return [
            {
                "bvid": "BV1aa",
                "title": "视频一",
                "up": "UP一",
                "desc": "讲了一件事",
                "url": "https://www.bilibili.com/video/BV1aa",
                "published_at": 1700000000.0,
                "topics": ["知识"],
                "hot_score": 0.6,
            },
            {
                "bvid": "BV2bb",
                "title": "视频二",
                "up": "UP二",
                "desc": "讲了另一件事",
                "url": "https://www.bilibili.com/video/BV2bb",
                "published_at": 1700000001.0,
                "topics": [],
                "hot_score": 0.4,
            },
        ]

    def test_browse_once_ingests_and_dedupes(self):
        client = mock.Mock()
        client.browse.return_value = self._videos()
        cards = self.collector.browse_once(limit=2, client=client)
        self.assertEqual([card.bvid for card in cards], ["BV1aa", "BV2bb"])
        self.assertTrue(self.collector.is_ingested("BV1aa"))
        self.assertEqual(
            int(self.db.query_one("SELECT COUNT(*) AS n FROM rag_chunks")["n"]), 2
        )

        client.browse.return_value = self._videos()
        second = self.collector.browse_once(limit=2, client=client)
        self.assertEqual(second, [])
        self.assertEqual(
            int(self.db.query_one("SELECT COUNT(*) AS n FROM rag_chunks")["n"]), 2
        )

    def test_browse_once_passes_keyword(self):
        client = mock.Mock()
        client.browse.return_value = []
        self.collector.browse_once(limit=3, keyword="鲸鱼", client=client)
        client.browse.assert_called_once_with(limit=3, keyword="鲸鱼")

    def test_ingest_video_still_works(self):
        card = self.collector.ingest_video(
            bvid="BV_OLD",
            title="旧入口",
            up="UP",
            subtitle="字幕内容",
            topics=["测试"],
        )
        self.assertEqual(card.bvid, "BV_OLD")
        self.assertTrue(card.rag_id)
        self.assertTrue(card.memory_id)

    def test_browse_once_ingests_comments_into_rag(self):
        client = mock.Mock()
        client.browse.return_value = self._videos()
        client.video_comments.return_value = ["用户甲: 好耶", "用户乙: 收藏了"]
        cards = self.collector.browse_once(limit=2, client=client)
        self.assertEqual([card.bvid for card in cards], ["BV1aa", "BV2bb"])
        client.video_comments.assert_any_call("BV1aa", limit=10)
        client.video_comments.assert_any_call("BV2bb", limit=10)
        row = self.db.query_one(
            "SELECT text, metadata_json FROM rag_chunks WHERE source_id='BV1aa'"
        )
        self.assertIn("用户甲: 好耶", row["text"])
        self.assertIn("用户乙: 收藏了", row["text"])
        meta = json.loads(row["metadata_json"])
        self.assertIn("用户甲: 好耶", meta["comments"])

    def test_browse_once_comment_failure_does_not_block_ingest(self):
        client = mock.Mock()
        client.browse.return_value = self._videos()
        client.video_comments.side_effect = BilibiliError("reply api down", code=-404)
        cards = self.collector.browse_once(limit=2, client=client)
        self.assertEqual([card.bvid for card in cards], ["BV1aa", "BV2bb"])
        self.assertTrue(self.collector.is_ingested("BV1aa"))
        row = self.db.query_one(
            "SELECT metadata_json FROM rag_chunks WHERE source_id='BV1aa'"
        )
        self.assertEqual(json.loads(row["metadata_json"])["comments"], "")

    def test_browse_once_comments_disabled_skips_call(self):
        self.config._data["bilibili"]["comments_per_video"] = 0
        collector = BilibiliCollector(self.memory, self.config)
        client = mock.Mock()
        client.browse.return_value = self._videos()
        cards = collector.browse_once(limit=2, client=client)
        self.assertEqual([card.bvid for card in cards], ["BV1aa", "BV2bb"])
        client.video_comments.assert_not_called()
        row = self.db.query_one(
            "SELECT metadata_json FROM rag_chunks WHERE source_id='BV1aa'"
        )
        self.assertEqual(json.loads(row["metadata_json"])["comments"], "")


class PlatformModelTests(unittest.TestCase):
    def test_session_ids(self):
        self.assertEqual(make_session_id("qq", "private", 1001), "qq:private:1001")
        self.assertEqual(make_session_id("qq", "group", "5555"), "qq:group:5555")
        with self.assertRaises(ValueError):
            make_session_id("qq", "channel", 1)

    def test_outbound_payload(self):
        payload = OutboundMessage("group", 5555, "hi", reply_to=7).to_payload()
        self.assertEqual(
            payload,
            {"target_type": "group", "target_id": "5555", "text": "hi", "reply_to": "7"},
        )
        with self.assertRaises(ValueError):
            OutboundMessage("channel", "1", "hi")

    def test_inbound_defaults(self):
        msg = InboundMessage(
            platform="qq",
            event_id="1",
            session_id="qq:private:1",
            user_id="1",
            group_id=None,
            text="hi",
            is_group=False,
        )
        self.assertFalse(msg.mentioned)
        self.assertIsNone(msg.reply_to)
        self.assertEqual(msg.raw, {})


if __name__ == "__main__":
    unittest.main()
