"""Tests for the sentence-aware TTS segmentation policy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.config import Config
from fatfish.tts import TTSClient, TTSResult
from fatfish.tts.segmentation import plan_segments


class _FakeDB:
    def execute(self, *args: object, **kwargs: object) -> None:
        return None


class RecordingTTS(TTSClient):
    def __init__(self, config: Config) -> None:
        super().__init__(config, _FakeDB())  # type: ignore[arg-type]
        self.calls: list[str] = []

    def synthesize(self, text: str, *, emotion: str | None = None) -> TTSResult:
        self.calls.append(text)
        return TTSResult(True, audio_path=f"fake/{len(self.calls)}.wav", provider="fake")


class PlanSegmentsTests(unittest.TestCase):
    def test_single_short_sentence_is_one_segment(self) -> None:
        plan = plan_segments("晚安。", max_chars=20)
        self.assertEqual([item.text for item in plan], ["晚安。"])
        self.assertEqual(plan[0].mode, "single_short")

    def test_single_long_sentence_is_not_split(self) -> None:
        text = "这是一句超过二十个字而且中间没有句末标点的很长很长的句子我们要保持它完整。"
        plan = plan_segments(text, max_chars=20)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].text, text)
        self.assertEqual(plan[0].mode, "single_long")
        self.assertGreater(plan[0].chars, 20)

    def test_short_sentences_are_packed(self) -> None:
        plan = plan_segments("晚安。做个好梦。明天见。", max_chars=20)
        self.assertEqual([item.text for item in plan], ["晚安。做个好梦。明天见。"])
        self.assertEqual(plan[0].mode, "packed")

    def test_packing_respects_the_budget(self) -> None:
        plan = plan_segments(
            "嗯。今天真的很开心。我们明天一起去海边看日出吧。", max_chars=20
        )
        self.assertEqual([item.text for item in plan], ["嗯。今天真的很开心。", "我们明天一起去海边看日出吧。"])
        self.assertTrue(all(item.chars <= 20 for item in plan))

    def test_long_sentence_stands_alone(self) -> None:
        long = "这是一句超过二十个字而且中间没有句末标点的很长很长的句子"
        plan = plan_segments(f"晚安。{long}。明天见。", max_chars=20)
        self.assertEqual([item.text for item in plan], ["晚安。", f"{long}。", "明天见。"])
        self.assertEqual(plan[1].mode, "long_sentence")

    def test_text_is_preserved_exactly(self) -> None:
        text = "晚安。做个好梦。今天也要开心呀。"
        plan = plan_segments(text, max_chars=8)
        self.assertEqual("".join(item.text for item in plan), text)

    def test_empty_and_punctuation_only(self) -> None:
        self.assertEqual(plan_segments(""), [])
        self.assertEqual(plan_segments("   "), [])
        self.assertEqual(plan_segments("……"), [])

    def test_budget_is_configurable(self) -> None:
        text = "晚安。做个好梦。明天见。"
        # 3 + 5 = 8 fits exactly; the last 4-char sentence starts a new segment.
        self.assertEqual(len(plan_segments(text, max_chars=8)), 2)
        self.assertEqual(len(plan_segments(text, max_chars=40)), 1)


class ClientPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _config(self, **tts: object) -> Config:
        data = {
            "app": {"data_dir": str(self.tmp / "data")},
            "tts": {
                "enabled": True,
                "mode": "command",
                "max_chars_per_sentence": 20,
                "min_chars_per_sentence": 4,
                **tts,
            },
        }
        return Config(data, profile="test")

    def test_sentence_policy_is_used(self) -> None:
        client = RecordingTTS(self._config(segment_policy="sentence"))
        pairs = list(client.iter_synthesize("晚安。做个好梦。明天见。"))
        self.assertEqual([segment for segment, _ in pairs], ["晚安。做个好梦。明天见。"])
        self.assertEqual(client.calls, ["晚安。做个好梦。明天见。"])

    def test_sentence_policy_keeps_long_sentence_whole(self) -> None:
        long = "这是一句超过二十个字而且中间没有句末标点的很长很长的句子"
        client = RecordingTTS(self._config(segment_policy="sentence"))
        pairs = list(client.iter_synthesize(f"{long}。"))
        self.assertEqual([segment for segment, _ in pairs], [f"{long}。"])

    def test_legacy_policy_still_hard_splits(self) -> None:
        client = RecordingTTS(self._config(segment_policy="chars"))
        text = "这是一段没有标点符号的很长很长的句子需要被切开并且保持每段不超过二十个字符"
        pairs = list(client.iter_synthesize(text))
        self.assertGreater(len(pairs), 1)
        self.assertTrue(all(len(segment) <= 20 for segment, _ in pairs))


if __name__ == "__main__":
    unittest.main()
