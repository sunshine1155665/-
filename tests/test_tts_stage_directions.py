"""Stage directions live in the bubble, not in the audio.

The persona prompt asks the model for small parenthetical actions -- ``（小声）``,
``（尾巴晃了一下）``. They belong on screen; a synthesiser reads them out loud.
These tests pin the boundary: what is removed, what must survive, and the one
switch that turns the whole thing off.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.config import Config
from fatfish.textfmt import strip_stage_directions
from fatfish.tts import TTSClient, TTSResult
from fatfish.voice.segments import split_short_sentences


class _FakeDB:
    """Never reached: the recording client below skips _metric."""

    def execute(self, *args: object, **kwargs: object) -> None:
        return None


class _RecordingTTS(TTSClient):
    """TTSClient that records the text handed to the provider instead of POSTing."""

    def __init__(self, config: Config) -> None:
        super().__init__(config, _FakeDB())  # type: ignore[arg-type]
        self.seen: list[str] = []

    def _http_json(self, text: str, emotion: str | None, out_path: Path) -> TTSResult:
        self.seen.append(text)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")
        return TTSResult(True, audio_path=str(out_path), provider="http_json")


class StripStageDirectionsTests(unittest.TestCase):
    def test_action_aside_is_removed(self) -> None:
        self.assertEqual(
            strip_stage_directions("那……那我就信你了（小声）。说出去的话我可记着。"),
            "那……那我就信你了。说出去的话我可记着。",
        )

    def test_aside_in_the_middle_keeps_the_sentence_intact(self) -> None:
        self.assertEqual(
            strip_stage_directions("笨蛋，说这种话……（尾巴晃了一下）那就多陪你一会儿。"),
            "笨蛋，说这种话……那就多陪你一会儿。",
        )

    def test_leading_aside_leaves_no_orphan_punctuation(self) -> None:
        self.assertEqual(strip_stage_directions("（小声），嗯……"), "嗯……")

    def test_ascii_parentheses_are_handled_too(self) -> None:
        self.assertEqual(strip_stage_directions("好耶(sighs)真的"), "好耶真的")

    def test_several_asides_in_one_reply(self) -> None:
        self.assertEqual(
            strip_stage_directions("（歪头）你回来啦（小声）。"), "你回来啦。"
        )

    def test_reply_that_is_only_an_aside_becomes_empty(self) -> None:
        self.assertEqual(strip_stage_directions("（尾巴晃了一下）"), "")

    def test_unclosed_trailing_aside_is_dropped(self) -> None:
        self.assertEqual(strip_stage_directions("我先睡啦（小声"), "我先睡啦")

    def test_parenthetical_clause_is_content_and_survives(self) -> None:
        text = "她说（我真的不知道）然后就走了。"
        self.assertEqual(strip_stage_directions(text), text)

    def test_parenthetical_question_survives(self) -> None:
        text = "（真的吗？）然后呢。"
        self.assertEqual(strip_stage_directions(text), text)

    def test_overlong_aside_survives(self) -> None:
        text = "嗯（把整条尾巴从左边慢慢绕到右边再绕回左边最后搭在腿上）好看吧。"
        self.assertEqual(strip_stage_directions(text), text)

    def test_aside_with_numbers_survives(self) -> None:
        for text in ("版本（3.2）上线了。", "（2024年）的事了。"):
            self.assertEqual(strip_stage_directions(text), text)

    def test_aside_without_chinese_is_dropped(self) -> None:
        for text, want in ("好耶(sighs)真的", "好耶真的"), ("（beta）先试试", "先试试"):
            self.assertEqual(strip_stage_directions(text), want)

    def test_aside_naming_somebody_reads_as_speech(self) -> None:
        for text in ("她说（你回来啦）的时候。", "（你怎么才回来）这句话。"):
            self.assertEqual(strip_stage_directions(text), text)

    def test_budget_is_configurable(self) -> None:
        self.assertEqual(
            strip_stage_directions("好（尾巴晃了一下）耶", max_chars=4), "好（尾巴晃了一下）耶"
        )

    def test_plain_text_is_returned_unchanged(self) -> None:
        text = "今天的海面很安静，风也很轻。"
        self.assertIs(strip_stage_directions(text), text)


class TTSClientStageDirectionTests(unittest.TestCase):
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
                "mode": "http_json",
                "base_url": "http://127.0.0.1:1/tts",
                "format": "wav",
                "max_chars_per_sentence": 20,
                "min_chars_per_sentence": 4,
                **tts,
            },
        }
        return Config(data, profile="test")

    def test_synthesize_never_hands_the_aside_to_the_provider(self) -> None:
        client = _RecordingTTS(self._config())
        result = client.synthesize("那……那我就信你了（小声）。")
        self.assertTrue(result.ok)
        self.assertEqual(client.seen, ["那……那我就信你了。"])

    def test_two_replies_differing_only_by_an_aside_share_one_cache_entry(self) -> None:
        client = _RecordingTTS(self._config())
        client.synthesize("嗯（小声）")
        again = client.synthesize("嗯")
        self.assertTrue(again.cached)
        self.assertEqual(len(client.seen), 1)

    def test_reply_of_only_an_aside_is_reported_as_empty(self) -> None:
        client = _RecordingTTS(self._config())
        result = client.synthesize("（尾巴晃了一下）")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_text")
        self.assertEqual(client.seen, [])

    def test_segments_are_what_the_listener_hears(self) -> None:
        client = _RecordingTTS(self._config())
        segments = [text for text, _ in client.iter_synthesize("笨蛋，说这种话……（尾巴晃了一下）那就多陪你一会儿。")]
        self.assertTrue(segments)
        for segment in segments:
            self.assertNotIn("（", segment)
            self.assertNotIn("尾巴", segment)
        self.assertEqual("".join(segments), "笨蛋，说这种话……那就多陪你一会儿。")

    def test_switch_off_keeps_the_aside_in_the_audio(self) -> None:
        client = _RecordingTTS(self._config(strip_stage_directions=False))
        client.synthesize("嗯（小声）")
        self.assertEqual(client.seen, ["嗯（小声）"])

    def test_timestamp_never_reaches_the_provider(self) -> None:
        """``send_voice`` synthesises model text that skipped the reply cleanup."""
        client = _RecordingTTS(self._config())
        client.synthesize("[10-22 21:16] 本鲸鱼娘还趴这儿呢")
        self.assertEqual(client.seen, ["本鲸鱼娘还趴这儿呢"])

    def test_splitter_still_keeps_parenthetical_clauses_whole(self) -> None:
        """The splitter's protection must survive: it guards *kept* asides."""
        self.assertEqual(
            split_short_sentences("（真的吗？）然后呢。", min_chars=2),
            ["（真的吗？）", "然后呢。"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
