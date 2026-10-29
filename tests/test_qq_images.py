"""QQ image messages: sliding merge window + vision direct-read (offline).

Covers the 2026-10 behaviour change in the QQ adapter:

* Pure-image messages are no longer silently dropped (the old
  ``if not text: return None`` in ``parse_event``).
* ``qq.merge_window_seconds`` merges a same-sender burst into ONE turn after
  the window goes quiet (sliding debounce, injectable timer).
* ``qq.vision_enabled`` downloads image URLs into OpenAI-compatible inline
  data URLs for the current LLM user message; bytes are never persisted, and
  download failures degrade honestly to the 「[图片]」 placeholder with an
  error log.

Everything runs offline: temporary directories, injected downloaders and
timers, no real sockets, no real ``data/fatfish.db``.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMClient, LLMResult, LLMUsage, vision_content
from fatfish.platforms.qq import (
    QQAdapter,
    _compose_turn_text,
    image_data_url,
)
from fatfish.runtime import Runtime
from fatfish.voice import VoiceDecision

OWNER_SESSION = "qq:private:1001"


def make_env(
    tmp: Path,
    *,
    qq_extra: str = "",
) -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'images.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[voice]
enabled = false

[tts]
enabled = false

[sticker]
enabled = false

[qq]
enabled = true
self_id = 999
{qq_extra}
"""
    path = tmp / "images.toml"
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
    image_urls: list[str] | None = None,
) -> dict:
    segments: list[dict] = [
        {"type": "image", "data": {"url": url}} for url in (image_urls or [])
    ]
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": self_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": segments,
    }


def group_event(
    group_id: int = 5555,
    user_id: int = 1001,
    text: str = "在吗",
    *,
    message_id: int = 22,
    mention: bool = False,
    self_id: int = 999,
    image_urls: list[str] | None = None,
) -> dict:
    segments: list[dict] = [
        {"type": "image", "data": {"url": url}} for url in (image_urls or [])
    ]
    if mention:
        segments.append({"type": "at", "data": {"qq": self_id}})
    if text:
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


def history_row(text: str, ts: float, *, message_id: int, user_id: int = 1001) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message_id": message_id,
        "time": ts,
        "message": [{"type": "text", "data": {"text": text}}],
    }


class FakeTimer:
    """Deterministic debounce timer: start() is a no-op, fire() runs the callback."""

    def __init__(self, delay: float, fn) -> None:
        self.delay = delay
        self.fn = fn
        self.cancelled = False
        self.started = False

    def cancel(self) -> None:
        self.cancelled = True

    def start(self) -> None:
        self.started = True

    def fire(self) -> None:
        if not self.cancelled:
            self.fn()


class FakeOneBotClient:
    def __init__(self, *args, **kwargs) -> None:
        self.connected = True
        self.sent: list[tuple[str, str, str]] = []
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

    def recv(self, timeout=None):
        return None


class HistoryClient:
    """Minimal stand-in for the NapCat get_friend_msg_history extension."""

    connected = True

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def get_friend_msg_history(self, uin, count=20, on_event=None):
        return list(self.rows)


def fake_downloader(payload=b"jpeg-bytes", content_type: str = "image/jpeg"):
    """Build an injectable downloader; a raising payload records the call first."""
    calls: list[str] = []

    def download(url, **kwargs):
        calls.append(str(url))
        if isinstance(payload, Exception):
            raise payload
        return payload, content_type

    download.calls = calls
    return download


class TurnRecorder:
    """Replace ``runtime.handle_user_message`` and record every call."""

    def __init__(self, runtime: Runtime, reply: str = "收到啦") -> None:
        self.calls: list[dict] = []
        self._runtime = runtime
        self._reply = reply

    def install(self) -> None:
        def fake(session_id, text, is_group=False, **kwargs):
            self.calls.append({"session_id": session_id, "text": text, **kwargs})
            return {"messages": [self._reply], "reply": self._reply}

        self._runtime.handle_user_message = fake


class _AdapterCase(unittest.TestCase):
    """Shared setUp: temp env, recorder, adapter with deterministic timers."""

    qq_extra = ""
    fake_timers = False
    use_recorder = True

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db, self.runtime = make_env(
            Path(self.tmp.name), qq_extra=self.qq_extra
        )
        self.addCleanup(self.db.close)
        self.recorder = TurnRecorder(self.runtime)
        if self.use_recorder:
            self.recorder.install()
        self.adapter = QQAdapter(self.runtime, self.config)
        self.adapter.client = FakeOneBotClient()
        self.timers: list[FakeTimer] = []
        if self.fake_timers and self.adapter.merge_window_seconds > 0:
            self.use_fake_timers()

    def use_fake_timers(self) -> None:
        def factory(delay, fn):
            timer = FakeTimer(delay, fn)
            self.timers.append(timer)
            return timer

        self.adapter._timer_factory = factory

    def buffer_key(self, session_id: str = OWNER_SESSION, user_id: str = "1001"):
        return (session_id, user_id)

    def latest_timer(self, session_id: str = OWNER_SESSION) -> FakeTimer:
        buffer = self.adapter._buffers.get(self.buffer_key(session_id))
        assert buffer is not None and buffer.timer is not None
        return buffer.timer

    def flush_window(self, session_id: str = OWNER_SESSION) -> list[dict]:
        """Fire the debounce timer and drain due windows on the owning thread."""
        self.latest_timer(session_id).fire()
        return self.adapter.drain_due_flushes()


class ComposeTextTests(unittest.TestCase):
    def test_no_images_keeps_text(self):
        self.assertEqual(_compose_turn_text(0, "你好"), "你好")
        self.assertEqual(_compose_turn_text(0, "  "), "")

    def test_placeholder_counts_images(self):
        self.assertEqual(_compose_turn_text(1, ""), "[图片]")
        self.assertEqual(_compose_turn_text(3, ""), "[图片]×3")
        self.assertEqual(_compose_turn_text(2, "看这个"), "[图片]×2\n看这个")

    def test_data_url_defaults_to_jpeg(self):
        self.assertEqual(
            image_data_url(b"ab", ""), f"data:image/jpeg;base64,{base64.b64encode(b'ab').decode()}"
        )
        self.assertEqual(
            image_data_url(b"ab", "image/png"),
            f"data:image/png;base64,{base64.b64encode(b'ab').decode()}",
        )


class MergeWindowTests(_AdapterCase):
    qq_extra = "merge_window_seconds = 5\nvision_enabled = true"
    fake_timers = True

    def setUp(self) -> None:
        super().setUp()
        self.downloader = fake_downloader(b"jpeg-bytes", "image/jpeg")
        self.adapter.image_downloader = self.downloader

    def test_image_plus_text_merges_into_one_turn(self):
        first = self.adapter.handle_event(
            private_event(text="", image_urls=["https://img.example/a.jpg"], message_id=1)
        )
        self.assertEqual(first["buffered"], True)
        self.assertEqual(first["window_messages"], 1)
        self.assertEqual(self.recorder.calls, [])

        second = self.adapter.handle_event(
            private_event(text="看看这张", message_id=2)
        )
        self.assertEqual(second["window_messages"], 2)
        self.assertEqual(self.recorder.calls, [])

        results = self.flush_window()
        self.assertEqual(len(self.recorder.calls), 1)
        call = self.recorder.calls[0]
        self.assertEqual(call["session_id"], OWNER_SESSION)
        self.assertEqual(call["text"], "[图片]\n看看这张")
        self.assertEqual(
            call["images"],
            [image_data_url(b"jpeg-bytes", "image/jpeg")],
        )
        self.assertEqual(self.downloader.calls, ["https://img.example/a.jpg"])
        # One turn, one reply, batch event id recorded for dedupe.
        self.assertEqual(
            self.adapter.client.sent, [("private", "1001", "收到啦")]
        )
        self.assertIn("1+2", self.adapter._seen_events)

    def test_new_message_cancels_and_restarts_the_timer(self):
        self.adapter.handle_event(private_event(text="一", message_id=1))
        first_timer = self.latest_timer()
        self.adapter.handle_event(private_event(text="二", message_id=2))
        second_timer = self.latest_timer()
        self.assertIsNot(first_timer, second_timer)
        self.assertTrue(first_timer.cancelled)
        self.assertFalse(second_timer.cancelled)

        # The cancelled timer firing must do nothing at all.
        first_timer.fire()
        self.assertEqual(self.adapter.drain_due_flushes(), [])
        self.assertEqual(self.recorder.calls, [])
        self.assertIn("1", self.adapter._seen_events)

        second_timer.fire()
        self.assertEqual(len(self.adapter.drain_due_flushes()), 1)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "一\n\n二")
        self.assertNotIn("images", self.recorder.calls[0])

    def test_third_message_resets_sliding_window(self):
        self.adapter.handle_event(private_event(text="一", message_id=1))
        self.adapter.handle_event(private_event(text="二", message_id=2))
        second_timer = self.latest_timer()
        self.adapter.handle_event(private_event(text="三", message_id=3))
        third_timer = self.latest_timer()
        self.assertTrue(second_timer.cancelled)

        # All three were sent within one sliding window -> one merged turn.
        third_timer.fire()
        self.assertEqual(len(self.adapter.drain_due_flushes()), 1)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "一\n\n二\n\n三")
        self.assertEqual(self.adapter._buffers.get(self.buffer_key()), None)

    def test_duplicate_event_inside_window_counts_once(self):
        event = private_event(text="一", message_id=1)
        self.adapter.handle_event(event)
        again = self.adapter.handle_event(event)
        self.assertTrue(again["duplicate"])
        results = self.flush_window()
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "一")

    def test_single_text_message_reaches_turn_unchanged(self):
        result = self.adapter.handle_event(private_event(text="你好呀", message_id=1))
        self.assertTrue(result["buffered"])
        results = self.flush_window()
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "你好呀")

    def test_close_cancels_pending_window_timers(self):
        self.adapter.handle_event(private_event(text="一", message_id=1))
        timer = self.latest_timer()
        self.adapter.close()
        self.assertTrue(timer.cancelled)
        self.assertEqual(self.adapter._buffers, {})


class RealWindowTimingTests(_AdapterCase):
    """Real threading.Timer at 0.3s: the debounce survives actual threads."""

    qq_extra = "merge_window_seconds = 0.3"

    def test_burst_within_window_flashes_once_after_quiet(self):
        self.adapter.handle_event(private_event(text="一", message_id=1))
        time.sleep(0.12)
        self.adapter.handle_event(private_event(text="二", message_id=2))
        self.assertEqual(self.recorder.calls, [])
        # The timer thread only marks the window due; the owning thread
        # (here: the test) drains it.
        time.sleep(0.55)
        results = self.adapter.drain_due_flushes()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "一\n\n二")
        self.assertEqual(self.adapter.drain_due_flushes(), [])


class WindowOffTests(_AdapterCase):
    """``merge_window_seconds = 0`` keeps the historical per-message path."""

    def test_text_message_processed_immediately(self):
        result = self.adapter.handle_event(private_event(text="你好呀", message_id=1))
        self.assertNotIn("buffered", result)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "你好呀")

    def test_pure_image_message_processed_immediately_with_placeholder(self):
        result = self.adapter.handle_event(
            private_event(text="", image_urls=["https://img.example/a.jpg"], message_id=2)
        )
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["text"], "[图片]")
        self.assertNotIn("images", self.recorder.calls[0])
        self.assertEqual(result["event_id"], "2")

    def test_multi_image_message_placeholder_counts(self):
        self.adapter.handle_event(
            private_event(
                text="",
                image_urls=["https://img.example/a.jpg", "https://img.example/b.jpg"],
                message_id=3,
            )
        )
        self.assertEqual(self.recorder.calls[0]["text"], "[图片]×2")

    def test_processed_row_is_not_refetched_by_catchup(self):
        self.adapter.handle_event(private_event(text="已回复", message_id=64))
        self.assertEqual(len(self.recorder.calls), 1)
        self.adapter.client = HistoryClient([history_row("已回复", 1100.0, message_id=64)])
        self.assertEqual(self.adapter._fetch_history_texts("1001", 20), [])


class VisionTests(_AdapterCase):
    qq_extra = "vision_enabled = true"

    def test_pure_image_sends_data_url_to_llm(self):
        downloader = fake_downloader(b"abc", "image/jpeg")
        self.adapter.image_downloader = downloader
        self.adapter.handle_event(
            private_event(text="", image_urls=["https://img.example/a.jpg"], message_id=31)
        )
        call = self.recorder.calls[0]
        self.assertEqual(call["text"], "[图片]")
        self.assertEqual(
            call["images"], [f"data:image/jpeg;base64,{base64.b64encode(b'abc').decode()}"]
        )
        self.assertEqual(downloader.calls, ["https://img.example/a.jpg"])

    def test_text_and_image_both_reach_the_llm(self):
        self.adapter.image_downloader = fake_downloader(b"abc", "image/png")
        self.adapter.handle_event(
            private_event(text="看看", image_urls=["https://img.example/a.png"], message_id=32)
        )
        call = self.recorder.calls[0]
        self.assertEqual(call["text"], "[图片]\n看看")
        self.assertEqual(
            call["images"], [f"data:image/png;base64,{base64.b64encode(b'abc').decode()}"]
        )

    def test_vision_off_never_downloads(self):
        self.adapter.vision_enabled = False
        downloader = fake_downloader()
        self.adapter.image_downloader = downloader
        self.adapter.handle_event(
            private_event(text="", image_urls=["https://img.example/a.jpg"], message_id=33)
        )
        call = self.recorder.calls[0]
        self.assertEqual(call["text"], "[图片]")
        self.assertNotIn("images", call)
        self.assertEqual(downloader.calls, [])

    def test_download_failure_degrades_with_error_log(self):
        self.adapter.image_downloader = fake_downloader(
            ConnectionError("boom"), "image/jpeg"
        )
        with self.assertLogs("fatfish.platforms.qq", level="ERROR") as captured:
            self.adapter.handle_event(
                private_event(
                    text="", image_urls=["https://img.example/a.jpg"], message_id=34
                )
            )
        call = self.recorder.calls[0]
        self.assertEqual(call["text"], "[图片]")
        self.assertNotIn("images", call)
        self.assertTrue(any("download failed" in line for line in captured.output))

    def test_oversize_image_degrades_with_error_log(self):
        self.adapter.image_downloader = fake_downloader(b"123456", "image/jpeg")
        self.adapter.vision_max_bytes = 4
        with self.assertLogs("fatfish.platforms.qq", level="ERROR") as captured:
            self.adapter.handle_event(
                private_event(text="看看", image_urls=["https://img.example/big.jpg"], message_id=35)
            )
        call = self.recorder.calls[0]
        self.assertEqual(call["text"], "[图片]\n看看")
        self.assertNotIn("images", call)
        self.assertTrue(any("skipped for vision" in line for line in captured.output))

    def test_partial_failure_keeps_the_downloaded_images(self):
        def flaky(url, **kwargs):
            if url.endswith("bad.jpg"):
                raise TimeoutError("slow")
            return b"ok", "image/jpeg"

        self.adapter.image_downloader = flaky
        with self.assertLogs("fatfish.platforms.qq", level="ERROR"):
            self.adapter.handle_event(
                private_event(
                    text="",
                    image_urls=["https://img.example/good.jpg", "https://img.example/bad.jpg"],
                    message_id=36,
                )
            )
        call = self.recorder.calls[0]
        self.assertEqual(call["text"], "[图片]×2")
        self.assertEqual(
            call["images"], [f"data:image/jpeg;base64,{base64.b64encode(b'ok').decode()}"]
        )


class GroupMentionAggregationTests(_AdapterCase):
    """Window on: the group gate judges the merged turn, not each segment."""

    qq_extra = "merge_window_seconds = 5"
    fake_timers = True

    def test_unmentioned_image_then_mentioned_text_passes(self):
        first = self.adapter.handle_event(
            group_event(text="", image_urls=["https://img.example/a.jpg"], message_id=41)
        )
        self.assertEqual(first["buffered"], True)
        self.adapter.handle_event(
            group_event(text="在吗", mention=True, message_id=42)
        )
        results = self.flush_window("qq:group:5555")
        self.assertEqual(len(self.recorder.calls), 1)
        call = self.recorder.calls[0]
        self.assertEqual(call["session_id"], "qq:group:5555")
        self.assertEqual(call["text"], "[图片]\n在吗")
        self.assertEqual(self.adapter.client.sent, [("group", "5555", "收到啦")])

    def test_unmentioned_image_only_is_skipped_and_logged(self):
        result = self.adapter.handle_event(
            group_event(text="", image_urls=["https://img.example/a.jpg"], message_id=43)
        )
        self.assertEqual(result["buffered"], True)
        self.flush_window("qq:group:5555")
        self.assertEqual(self.recorder.calls, [])
        row = self.db.query_one(
            "SELECT outcome FROM decision_log WHERE kind='qq_group_skip' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "mention_required")

    def test_reply_rate_counts_once_per_flush(self):
        self.adapter.group_reply_mode = "all"
        self.adapter.group_cooldown_seconds = 0
        self.adapter.group_max_per_hour = 2
        # Window 1: two buffered messages -> one counted reply.
        self.adapter.handle_event(group_event(text="一", message_id=44))
        self.adapter.handle_event(group_event(text="二", message_id=45))
        self.flush_window("qq:group:5555")
        # Window 2: one more reply is still allowed.
        self.adapter.handle_event(group_event(text="三", message_id=46))
        self.flush_window("qq:group:5555")
        self.assertEqual(len(self.recorder.calls), 2)
        # Window 3: the hourly budget (2 flushes) is spent.
        self.adapter.handle_event(group_event(text="四", message_id=47))
        result = self.flush_window("qq:group:5555")[0]
        self.assertEqual(len(self.recorder.calls), 2)
        self.assertEqual(result["skipped"], "group_rate_limit")


class CatchupDedupeTests(_AdapterCase):
    """History rows already buffered/handled live must not ride catch-up."""

    qq_extra = "merge_window_seconds = 5"
    fake_timers = True

    def test_fetch_history_skips_buffered_event_ids(self):
        self.adapter.handle_event(private_event(text="缓冲的", message_id=61))
        self.adapter.client = HistoryClient(
            [
                history_row("缓冲的", 1000.0, message_id=61),
                history_row("离线消息", 1030.0, message_id=62),
            ]
        )
        texts = self.adapter._fetch_history_texts("1001", 20)
        self.assertEqual(texts, [("离线消息", 1030.0)])

    def test_fetch_history_after_flush_still_skips_seen_ids(self):
        self.adapter.handle_event(private_event(text="缓冲的", message_id=61))
        results = self.flush_window()
        self.assertEqual(len(self.recorder.calls), 1)
        self.adapter.client = HistoryClient([history_row("缓冲的", 1000.0, message_id=61)])
        self.assertEqual(self.adapter._fetch_history_texts("1001", 20), [])

    def test_pure_image_history_row_reads_as_placeholder(self):
        row = history_row("", 1050.0, message_id=63)
        row["message"] = [{"type": "image", "data": {"url": "https://img.example/a.jpg"}}]
        self.adapter.client = HistoryClient([row])
        texts = self.adapter._fetch_history_texts("1001", 20)
        self.assertEqual(texts, [("[图片]", 1050.0)])


class LLMWireFormatTests(unittest.TestCase):
    def test_vision_content_shape(self):
        self.assertEqual(
            vision_content("描述这张", ["data:image/png;base64,QUJD"]),
            [
                {"type": "text", "text": "描述这张"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
        )
        self.assertEqual(
            vision_content("", ["u1"]),
            [{"type": "image_url", "image_url": {"url": "u1"}}],
        )

    def test_build_payload_round_trips_multimodal_content(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config, db, _ = make_env(Path(tmp.name))
        self.addCleanup(db.close)
        client = LLMClient(config, db)
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": vision_content("看", ["data:image/jpeg;base64,AAA"])},
        ]
        _, payload = client.build_payload(messages)
        parsed = json.loads(json.dumps(payload, ensure_ascii=False))
        content = parsed["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "看"})
        self.assertEqual(
            content[1], {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}
        )

    def test_text_only_payload_keeps_exact_wire_shape(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config, db, _ = make_env(Path(tmp.name))
        self.addCleanup(db.close)
        client = LLMClient(config, db)
        _, payload = client.build_payload(
            [{"role": "user", "content": "hi"}],
            max_tokens=800,
            temperature=0.8,
        )
        self.assertEqual(
            payload,
            {
                "model": client.model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 800,
                "temperature": 0.8,
                "stream": False,
                "reasoning_effort": "low",
            },
        )


class PromptBundleImageTests(unittest.TestCase):
    def test_model_messages_turn_user_content_is_an_array(self):
        from fatfish.context import PromptBundle, PromptSection

        bundle = PromptBundle(
            session_id="qq:private:1001",
            sections=[PromptSection("turn_tail", "[用户消息]\n[图片]\n看看", role="user")],
            turn_images=["data:image/jpeg;base64,AAA"],
        )
        messages = bundle.model_messages()
        content = messages[-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("[用户消息]", content[0]["text"])
        self.assertEqual(
            content[1], {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}
        )

    def test_text_view_and_hashes_stay_base64_free(self):
        from fatfish.context import PromptBundle, PromptSection

        bundle = PromptBundle(
            session_id="qq:private:1001",
            sections=[
                PromptSection("runtime_context", "ctx"),
                PromptSection("turn_tail", "[用户消息]\n[图片]", role="user"),
            ],
            dynamic_hash="dyn",
            turn_images=["data:image/jpeg;base64," + "A" * 4096],
        )
        self.assertNotIn("AAAA", bundle.text())
        self.assertNotIn("data:image", bundle.text())
        for section in bundle.sections:
            self.assertNotIn("data:image", section.text)

    def test_without_images_content_stays_a_string(self):
        from fatfish.context import PromptBundle, PromptSection

        bundle = PromptBundle(
            session_id="s",
            sections=[PromptSection("turn_tail", "[用户消息]\n你好", role="user")],
        )
        messages = bundle.model_messages()
        self.assertIsInstance(messages[-1]["content"], str)
        self.assertEqual(messages[-1]["content"], "[用户消息]\n你好")


class EndToEndVisionTurnTests(_AdapterCase):
    """Real runtime: the stored turn keeps text only; the LLM sees the image."""

    qq_extra = "vision_enabled = true"
    use_recorder = False

    def setUp(self) -> None:
        super().setUp()
        self.runtime.llm.api_key = "fake"
        self.chat_calls: list[list] = []

        def fake_chat(messages, **kwargs):
            self.chat_calls.append(messages)
            return LLMResult(
                text='{"messages":["好呀"],"actions":[]}',
                usage=LLMUsage(prompt_tokens=5, completion_tokens=2),
                model="fake-model",
            )

        self.runtime.llm.chat = fake_chat
        self.runtime.voice.decide = (
            lambda *args, **kwargs: VoiceDecision("text_only", 0.1, ["test"])
        )
        self.adapter.image_downloader = fake_downloader(b"abc", "image/jpeg")

    def test_turn_is_stored_as_text_and_llm_receives_image_part(self):
        self.adapter.handle_event(
            private_event(text="看看", image_urls=["https://img.example/a.jpg"], message_id=71)
        )
        self.assertEqual(len(self.chat_calls), 1)
        last = self.chat_calls[0][-1]
        self.assertEqual(last["role"], "user")
        self.assertIsInstance(last["content"], list)
        self.assertIn("[图片]", last["content"][0]["text"])
        self.assertIn("看看", last["content"][0]["text"])
        self.assertEqual(
            last["content"][1],
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(b'abc').decode()}"}},
        )
        # Stored turn: placeholder + words, never base64.
        row = self.db.query_one(
            "SELECT content FROM conversation_turns "
            "WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (OWNER_SESSION,),
        )
        self.assertEqual(row["content"], "[图片]\n看看")
        self.assertNotIn("data:image", row["content"])

    def test_vision_off_stores_placeholder_and_sends_plain_text(self):
        self.adapter.vision_enabled = False
        self.adapter.handle_event(
            private_event(text="", image_urls=["https://img.example/a.jpg"], message_id=72)
        )
        self.assertEqual(len(self.chat_calls), 1)
        last = self.chat_calls[0][-1]
        self.assertIsInstance(last["content"], str)
        self.assertIn("[图片]", last["content"])
        row = self.db.query_one(
            "SELECT content FROM conversation_turns "
            "WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (OWNER_SESSION,),
        )
        self.assertEqual(row["content"], "[图片]")


class EndToEndMergeTurnTests(_AdapterCase):
    """Real runtime: a burst becomes exactly one LLM call and one turn pair."""

    qq_extra = "merge_window_seconds = 5"
    fake_timers = True
    use_recorder = False

    def setUp(self) -> None:
        super().setUp()
        self.runtime.llm.api_key = "fake"
        self.chat_calls: list[list] = []

        def fake_chat(messages, **kwargs):
            self.chat_calls.append(messages)
            return LLMResult(
                text='{"messages":["好呀"],"actions":[]}',
                usage=LLMUsage(prompt_tokens=5, completion_tokens=2),
                model="fake-model",
            )

        self.runtime.llm.chat = fake_chat
        self.runtime.voice.decide = (
            lambda *args, **kwargs: VoiceDecision("text_only", 0.1, ["test"])
        )

    def test_burst_is_one_llm_call_with_joined_text(self):
        # All three events land inside one sliding window (each resets the
        # timer), so the whole burst is a single turn.
        self.adapter.handle_event(private_event(text="一", message_id=81))
        self.adapter.handle_event(private_event(text="二", message_id=82))
        self.adapter.handle_event(private_event(text="", image_urls=["https://img.example/a.jpg"], message_id=83))
        self.adapter.vision_enabled = True
        self.adapter.image_downloader = fake_downloader(b"abc", "image/jpeg")
        results = self.flush_window()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.chat_calls), 1)
        self.assertEqual(self.adapter.client.sent, [("private", "1001", "好呀")])
        user_rows = self.db.query(
            "SELECT content FROM conversation_turns "
            "WHERE session_id=? AND role='user' AND ephemeral=0 ORDER BY id",
            (OWNER_SESSION,),
        )
        self.assertEqual(
            [row["content"] for row in user_rows], ["[图片]\n一\n\n二"]
        )
        assistant_rows = self.db.query(
            "SELECT content FROM conversation_turns "
            "WHERE session_id=? AND role='assistant' AND ephemeral=0 ORDER BY id",
            (OWNER_SESSION,),
        )
        self.assertEqual([row["content"] for row in assistant_rows], ["好呀"])
        # One LLM call, and the image rides the final user message.
        last = self.chat_calls[0][-1]
        self.assertIsInstance(last["content"], list)
        self.assertIn("[图片]", last["content"][0]["text"])
        self.assertIn("一\n\n二", last["content"][0]["text"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
