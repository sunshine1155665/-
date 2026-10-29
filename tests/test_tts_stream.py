"""Tests for per-segment TTS iteration. No audio, network or real DB needed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.config import Config
from fatfish.tts import TTSClient, TTSResult
from fatfish.voice.segments import split_short_sentences

TEXT = "今天的海面很安静，风也很轻，我想回家啦。晚安。"
EXPECTED = ["今天的海面很安静，", "风也很轻，", "我想回家啦。", "晚安。"]


class _FakeDB:
    """TTS iterator tests never reach _metric, so a no-op DB is enough."""

    def execute(self, *args: object, **kwargs: object) -> None:
        return None


class FakeTTS(TTSClient):
    """TTSClient with synthesize replaced by an in-memory recorder."""

    def __init__(
        self,
        config: Config,
        *,
        fail_markers: tuple[str, ...] = (),
        raise_markers: tuple[str, ...] = (),
    ) -> None:
        super().__init__(config, _FakeDB())  # type: ignore[arg-type]
        self.calls: list[tuple[str, str | None]] = []
        self.fail_markers = fail_markers
        self.raise_markers = raise_markers

    def synthesize(self, text: str, *, emotion: str | None = None) -> TTSResult:
        self.calls.append((text, emotion))
        if any(marker in text for marker in self.raise_markers):
            raise RuntimeError("synthetic failure")
        if any(marker in text for marker in self.fail_markers):
            return TTSResult(False, error="synthetic_error", provider="fake")
        return TTSResult(
            True,
            audio_path=f"fake/audio-{len(self.calls)}.wav",
            provider="fake",
            cached=False,
        )


class TTSIteratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, **tts: object) -> Config:
        data = {
            "app": {"data_dir": str(self.tmp / "data")},
            "tts": {
                "enabled": True,
                "mode": "command",
                "max_chars_per_sentence": 10,
                "min_chars_per_sentence": 4,
                **tts,
            },
        }
        return Config(data, profile="test")

    def test_iter_uses_configured_splitter(self) -> None:
        client = FakeTTS(self._config())
        self.assertEqual(
            split_short_sentences(
                TEXT,
                max_chars=10,
                min_chars=4,
            ),
            EXPECTED,
        )
        results = list(client.iter_synthesize(TEXT))
        self.assertEqual([segment for segment, _ in results], EXPECTED)
        self.assertEqual([call[0] for call in client.calls], EXPECTED)
        self.assertTrue(all(result.ok for _, result in results))

    def test_iterator_is_lazy_and_yields_immediately(self) -> None:
        client = FakeTTS(self._config())
        iterator = client.iter_synthesize(TEXT)
        self.assertEqual(client.calls, [])

        segment, result = next(iterator)
        self.assertEqual(segment, EXPECTED[0])
        self.assertTrue(result.ok)
        self.assertEqual(len(client.calls), 1)

        segment, result = next(iterator)
        self.assertEqual(segment, EXPECTED[1])
        self.assertTrue(result.ok)
        self.assertEqual(len(client.calls), 2)

    def test_failures_continue_to_next_segment(self) -> None:
        client = FakeTTS(self._config(), fail_markers=("风也很轻",))
        results = list(client.iter_synthesize(TEXT))
        self.assertEqual([segment for segment, _ in results], EXPECTED)
        self.assertEqual(len(client.calls), len(EXPECTED))
        self.assertTrue(results[0][1].ok)
        self.assertFalse(results[1][1].ok)
        self.assertEqual(results[1][1].error, "synthetic_error")
        self.assertTrue(results[2][1].ok)
        self.assertTrue(results[3][1].ok)

    def test_exceptions_continue_to_next_segment(self) -> None:
        client = FakeTTS(self._config(), raise_markers=("风也很轻",))
        results = list(client.iter_synthesize(TEXT))
        self.assertEqual(len(results), len(EXPECTED))
        self.assertFalse(results[1][1].ok)
        self.assertIn("tts_exception:RuntimeError", results[1][1].error or "")
        self.assertTrue(results[2][1].ok)
        self.assertTrue(results[3][1].ok)
        self.assertEqual(len(client.calls), len(EXPECTED))

    def test_emotion_is_forwarded(self) -> None:
        client = FakeTTS(self._config())
        list(client.iter_synthesize(TEXT, emotion="gentle"))
        self.assertTrue(client.calls)
        self.assertTrue(all(emotion == "gentle" for _, emotion in client.calls))

    def test_synthesize_segments_returns_same_list(self) -> None:
        client = FakeTTS(self._config())
        pairs = client.synthesize_segments(TEXT, emotion="sleepy")
        self.assertIsInstance(pairs, list)
        self.assertEqual([segment for segment, _ in pairs], EXPECTED)
        self.assertTrue(all(result.ok for _, result in pairs))
        self.assertTrue(all(emotion == "sleepy" for _, emotion in client.calls))

    def test_empty_text_yields_nothing(self) -> None:
        client = FakeTTS(self._config())
        self.assertEqual(list(client.iter_synthesize("")), [])
        self.assertEqual(list(client.iter_synthesize("……")), [])
        self.assertEqual(client.synthesize_segments("   "), [])
        self.assertEqual(client.calls, [])

    def test_default_limits_are_twenty_and_four(self) -> None:
        config = self._config()
        data = config.as_dict()
        data["tts"].pop("max_chars_per_sentence")
        data["tts"].pop("min_chars_per_sentence")
        client = FakeTTS(Config(data, profile="test"))
        text = "这是一段没有标点符号的很长很长的句子需要被切开并且保持每段不超过二十个字符"
        results = list(client.iter_synthesize(text))
        self.assertGreater(len(results), 1)
        self.assertTrue(all(len(segment) <= 20 for segment, _ in results))
        self.assertEqual("".join(segment for segment, _ in results), text)

    def test_existing_synthesize_is_unchanged(self) -> None:
        # The plain method is still the low-level single-shot API; the
        # iterator only composes it. A disabled client reports per segment.
        data = {
            "app": {"data_dir": str(self.tmp / "data")},
            "tts": {"enabled": False},
        }
        client = TTSClient(Config(data, profile="test"), _FakeDB())  # type: ignore[arg-type]
        empty = client.synthesize("")
        self.assertFalse(empty.ok)
        self.assertEqual(empty.error, "empty_text")
        disabled = client.synthesize("你好")
        self.assertFalse(disabled.ok)
        self.assertEqual(disabled.error, "disabled")


if __name__ == "__main__":
    unittest.main()
