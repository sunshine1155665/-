"""Runtime tests for sentence-level TTS streaming.

These tests use a fake LLM, voice policy and TTS client: no network, no audio
and no real speech synthesis. They verify the runtime contract only.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.runtime import Runtime
from fatfish.tts import TTSResult
from fatfish.voice import VoiceDecision

REPLY = "晚安，做个好梦。今天也要开心呀。"
SEGMENTS = ["晚安，做个好梦。", "今天也要开心呀。"]


def make_env(tmp: Path, *, short_sentence_mode: bool = True) -> tuple[object, Database]:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'runtime_voice.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[voice]
enabled = true
min_score = 0.62
text_with_voice_min = 0.45
max_per_hour = 10
max_per_day = 50

[tts]
enabled = true
mode = "command"
short_sentence_mode = {str(short_sentence_mode).lower()}
max_chars_per_sentence = 20
min_chars_per_sentence = 4
stream_sentences = true

[sticker]
enabled = false

[wakeup]
max_per_day = 10
max_chain_depth = 3
"""
    path = tmp / "runtime_voice.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


class FakeLLM:
    available = True

    def __init__(self, text: str = REPLY):
        self.text = text
        self.calls: list[tuple[object, str | None]] = []

    def chat(self, messages, session_id=None):
        self.calls.append((messages, session_id))
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=4),
            model="fake-model",
        )


class FakeVoicePolicy:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def decide(self, text, **kwargs):
        return VoiceDecision("voice_only", 1.0, ["test"])

    def record(self, decision, **kwargs):
        self.records.append((decision.mode, kwargs))

    def count(self, session_id=None):
        return sum(1 for _, kwargs in self.records if kwargs.get("session_id") == session_id)


class FakeTTS:
    """Records synthesis order so streaming can be asserted precisely."""

    def __init__(
        self,
        pairs: list[tuple[str, TTSResult]] | None = None,
        *,
        single: TTSResult | None = None,
        iter_error: Exception | None = None,
        segments_error: Exception | None = None,
        single_error: Exception | None = None,
        events: list | None = None,
    ) -> None:
        self.pairs = list(pairs or [])
        self.single = single or TTSResult(True, audio_path="full.wav")
        self.iter_error = iter_error
        self.segments_error = segments_error
        self.single_error = single_error
        self.events = events if events is not None else []
        self.iter_calls: list[tuple[str, str | None]] = []
        self.segments_calls: list[tuple[str, str | None]] = []
        self.synthesize_calls: list[tuple[str, str | None]] = []

    def iter_synthesize(self, text: str, *, emotion: str | None = None):
        self.iter_calls.append((text, emotion))
        if self.iter_error is not None:
            raise self.iter_error
        for segment, result in self.pairs:
            self.events.append(("synthesize", segment))
            yield segment, result

    def synthesize_segments(self, text: str, *, emotion: str | None = None):
        self.segments_calls.append((text, emotion))
        if self.segments_error is not None:
            raise self.segments_error
        self.events.append(("synthesize_segments", None))
        return list(self.pairs)

    def synthesize(self, text: str, *, emotion: str | None = None) -> TTSResult:
        self.synthesize_calls.append((text, emotion))
        if self.single_error is not None:
            raise self.single_error
        return self.single


class RuntimeVoiceStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _runtime(
        self,
        *,
        short_sentence_mode: bool = True,
        llm_text: str = REPLY,
        tts: FakeTTS | None = None,
    ) -> tuple[Runtime, FakeTTS]:
        config, db = make_env(self.tmp, short_sentence_mode=short_sentence_mode)
        self.addCleanup(db.close)
        runtime = Runtime(db, config)
        fake_tts = tts or FakeTTS()
        runtime.llm = FakeLLM(llm_text)
        runtime.voice = FakeVoicePolicy()
        runtime.tts = fake_tts
        return runtime, fake_tts

    def test_stream_callback_runs_immediately_per_segment(self) -> None:
        events: list = []
        pairs = [
            (SEGMENTS[0], TTSResult(True, audio_path="seg-one.wav", format="wav")),
            (SEGMENTS[1], TTSResult(True, audio_path="seg-two.wav", format="wav")),
        ]
        tts = FakeTTS(pairs, events=events)
        runtime, tts = self._runtime(tts=tts)

        streamed: list[tuple[str, str | None]] = []

        def voice_stream(segment: str, result: TTSResult) -> None:
            streamed.append((segment, result.audio_path))
            events.append(("voice", segment))

        result = runtime.handle_user_message(
            "s", "晚安", voice_stream=voice_stream
        )

        self.assertEqual(result["reply"], REPLY)
        self.assertEqual(
            events,
            [
                ("synthesize", SEGMENTS[0]),
                ("voice", SEGMENTS[0]),
                ("synthesize", SEGMENTS[1]),
                ("voice", SEGMENTS[1]),
            ],
        )
        self.assertEqual(streamed, list(zip(SEGMENTS, ["seg-one.wav", "seg-two.wav"])))
        # 自发语音现在会带语气：REPLY 里有「晚安/好梦」，场合词直接判 soft。
        # 这是 voice/tone.py 的兜底，模型没机会指定时的默认路径。
        self.assertEqual(tts.iter_calls, [(REPLY, "soft")])
        self.assertEqual(tts.synthesize_calls, [])
        self.assertEqual(tts.segments_calls, [])

        payload = result["tts"]
        self.assertIsInstance(payload, dict)
        assert payload is not None
        self.assertTrue(payload["streamed"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["audio_path"], "seg-one.wav")
        self.assertEqual(payload["format"], "wav")
        self.assertEqual(payload["segments_total"], 2)
        self.assertEqual(payload["segments_ok"], 2)
        self.assertEqual(
            [item["text"] for item in payload["segments"]], SEGMENTS
        )
        self.assertEqual(
            set(payload["segments"][0]),
            {"text", "ok", "audio_path", "format", "cached", "error"},
        )

        trace = runtime.trace.last(1)[0]
        self.assertTrue(json.loads(trace["result_json"])["tts_ok"])

    def test_no_callback_uses_synthesize_segments(self) -> None:
        pairs = [
            (SEGMENTS[0], TTSResult(False, error="first_failed")),
            (SEGMENTS[1], TTSResult(True, audio_path="second.wav", format="mp3")),
        ]
        runtime, tts = self._runtime(tts=FakeTTS(pairs))

        result = runtime.handle_user_message("s", "晚安")

        payload = result["tts"]
        assert payload is not None
        self.assertFalse(payload["streamed"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["audio_path"], "second.wav")
        self.assertEqual(payload["format"], "mp3")
        self.assertEqual(payload["segments_total"], 2)
        self.assertEqual(payload["segments_ok"], 1)
        self.assertEqual(payload["segments"][0]["error"], "first_failed")
        self.assertIsNone(payload["error"])
        # 自发语音现在会带语气：REPLY 里有「晚安/好梦」，场合词直接判 soft。
        # 这是 voice/tone.py 的兜底，模型没机会指定时的默认路径。
        self.assertEqual(tts.segments_calls, [(REPLY, "soft")])
        self.assertEqual(tts.iter_calls, [])
        self.assertEqual(tts.synthesize_calls, [])

    def test_all_failed_segments_leave_text_intact(self) -> None:
        pairs = [
            (SEGMENTS[0], TTSResult(False, error="one_failed")),
            (SEGMENTS[1], TTSResult(False, error="two_failed")),
        ]
        runtime, _ = self._runtime(tts=FakeTTS(pairs))
        called: list[str] = []

        result = runtime.handle_user_message(
            "s",
            "晚安",
            voice_stream=lambda segment, item: called.append(segment),
        )

        self.assertEqual(called, SEGMENTS)
        self.assertEqual(result["reply"], REPLY)
        payload = result["tts"]
        assert payload is not None
        self.assertTrue(payload["streamed"])
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["audio_path"])
        self.assertEqual(payload["segments_ok"], 0)
        self.assertEqual(payload["segments_total"], 2)
        self.assertEqual(payload["error"], "one_failed")
        self.assertEqual(runtime.voice.records, [])

    def test_iterator_exception_does_not_block_text(self) -> None:
        runtime, _ = self._runtime(tts=FakeTTS(iter_error=RuntimeError("iterator exploded")))

        result = runtime.handle_user_message(
            "s", "晚安", voice_stream=lambda segment, item: None
        )

        self.assertEqual(result["reply"], REPLY)
        payload = result["tts"]
        assert payload is not None
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["segments_total"], 0)
        self.assertIn("iterator exploded", payload["error"] or "")
        metric = runtime.db.query_one(
            "SELECT key FROM metrics WHERE key='tts_error' ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(metric)

    def test_short_sentence_mode_off_keeps_single_synthesis(self) -> None:
        single = TTSResult(True, audio_path="whole.wav", format="wav", cached=True)
        runtime, tts = self._runtime(short_sentence_mode=False, tts=FakeTTS(single=single))
        called: list[str] = []

        result = runtime.handle_user_message(
            "s",
            "晚安",
            voice_stream=lambda segment, item: called.append(segment),
        )

        self.assertEqual(called, [])
        # 自发语音现在会带语气：REPLY 里有「晚安/好梦」，场合词直接判 soft。
        # 这是 voice/tone.py 的兜底，模型没机会指定时的默认路径。
        self.assertEqual(tts.synthesize_calls, [(REPLY, "soft")])
        self.assertEqual(tts.iter_calls, [])
        self.assertEqual(tts.segments_calls, [])
        payload = result["tts"]
        assert payload is not None
        self.assertEqual(
            payload,
            {
                "ok": True,
                "audio_path": "whole.wav",
                "format": "wav",
                "cached": True,
                "error": None,
                "covered_messages": 1,
            },
        )

    def test_single_synthesis_exception_does_not_block_text(self) -> None:
        runtime, _ = self._runtime(
            short_sentence_mode=False,
            tts=FakeTTS(single_error=RuntimeError("tts exploded")),
        )

        result = runtime.handle_user_message("s", "晚安")

        self.assertEqual(result["reply"], REPLY)
        payload = result["tts"]
        assert payload is not None
        self.assertFalse(payload["ok"])
        self.assertIn("tts exploded", payload["error"] or "")

    def test_explicit_send_voice_keeps_full_synthesis(self) -> None:
        plan = json.dumps(
            {
                "messages": ["你好呀。"],
                "actions": [{"tool": "send_voice", "args": {"text": "整段语音"}}],
            },
            ensure_ascii=False,
        )
        explicit = TTSResult(True, audio_path="explicit.wav", format="wav")
        runtime, tts = self._runtime(llm_text=plan, tts=FakeTTS(single=explicit))
        called: list[str] = []

        result = runtime.handle_user_message(
            "s",
            "说句话",
            voice_stream=lambda segment, item: called.append(segment),
        )

        self.assertEqual(called, [])
        # 不填 emotion 不再等于"没有语气"：runtime 会用 voice/tone.py 从这句话里
        # 读一个（"整段语音" 没有情绪词也没有场合词 → neutral，等价于不发 instruct）。
        self.assertEqual(tts.synthesize_calls, [("整段语音", "neutral")])
        self.assertEqual(tts.iter_calls, [])
        self.assertEqual(tts.segments_calls, [])
        payload = result["tts"]
        assert payload is not None
        self.assertEqual(payload["audio_path"], "explicit.wav")
        self.assertNotIn("streamed", payload)
        self.assertNotIn("segments", payload)

    def test_callback_exception_does_not_stop_later_segments(self) -> None:
        pairs = [
            (SEGMENTS[0], TTSResult(True, audio_path="one.wav")),
            (SEGMENTS[1], TTSResult(True, audio_path="two.wav")),
        ]
        runtime, _ = self._runtime(tts=FakeTTS(pairs))
        seen: list[str] = []

        def voice_stream(segment: str, result: TTSResult) -> None:
            seen.append(segment)
            if len(seen) == 1:
                raise RuntimeError("sink broke")

        result = runtime.handle_user_message("s", "晚安", voice_stream=voice_stream)

        self.assertEqual(seen, SEGMENTS)
        payload = result["tts"]
        assert payload is not None
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["segments_total"], 2)
        metric = runtime.db.query_one(
            "SELECT key FROM metrics WHERE key='voice_stream_error' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(metric)


if __name__ == "__main__":
    unittest.main()
