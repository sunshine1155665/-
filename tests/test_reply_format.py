"""Reply formatting: no transcript artifacts, no blob replies.

Regression cover for a bug where the model prefixed every reply with
``[10-22 21:16]`` and merged three short bubbles into one. Both came from the
same mistake: the transcript stamped the assistant's *own* past messages with a
timestamp, so the model read that as its speaking style instead of metadata.
"""

import pathlib
import re
import tempfile
import time
import unittest

from fatfish.context import ContextAssembler
from fatfish.memory.service import MemoryService
from fatfish.runtime import normalize_reply_segments

STAMP = re.compile(r"^\s*\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\]")


class StripArtifactTests(unittest.TestCase):
    def test_strips_day_month_timestamp_prefix(self) -> None:
        out = normalize_reply_segments(["[10-22 21:16] 问女孩子年龄……"])
        self.assertEqual(out, ["问女孩子年龄……"])

    def test_strips_full_date_timestamp_prefix(self) -> None:
        out = normalize_reply_segments(["[2026-10-22 21:16] 嗯"])
        self.assertEqual(out, ["嗯"])

    def test_strips_role_label(self) -> None:
        self.assertEqual(normalize_reply_segments(["用户: 在吗"]), ["在吗"])
        self.assertEqual(normalize_reply_segments(["assistant: 在的"]), ["在的"])

    def test_keeps_legitimate_brackets(self) -> None:
        self.assertEqual(
            normalize_reply_segments(["[捂脸] 不好意思啦"]), ["[捂脸] 不好意思啦"]
        )

    def test_segment_of_only_metadata_is_dropped(self) -> None:
        self.assertEqual(normalize_reply_segments(["[10-22 21:16]"]), [])
        self.assertEqual(normalize_reply_segments(["   "]), [])


class BubbleSplitTests(unittest.TestCase):
    def test_blob_of_short_lines_becomes_separate_bubbles(self) -> None:
        blob = (
            "[10-22 21:16] 问女孩子年龄……姐姐你也太直接了吧\n"
            "[10-22 21:16] 按人类那套算岁数不太对得上\n"
            "[10-22 21:17] 你就当……比你小一点？"
        )
        out = normalize_reply_segments([blob])
        self.assertEqual(len(out), 3)
        for text in out:
            self.assertIsNone(STAMP.match(text), text)

    def test_single_line_is_untouched(self) -> None:
        self.assertEqual(
            normalize_reply_segments(["嗯……好哦。姐姐想聊什么呀？"]),
            ["嗯……好哦。姐姐想聊什么呀？"],
        )

    def test_paragraph_with_long_line_is_kept_whole(self) -> None:
        paragraph = "啊" * 120 + "\n嗯"
        out = normalize_reply_segments([paragraph])
        self.assertEqual(len(out), 1)
        self.assertIn("\n", out[0])

    def test_respects_max_segments(self) -> None:
        blob = "\n".join(f"第{i}句" for i in range(10))
        out = normalize_reply_segments([blob], max_segments=4)
        self.assertEqual(len(out), 4)

    def test_collapses_duplicate_neighbour(self) -> None:
        out = normalize_reply_segments(["在的", "在的", "嗯"])
        self.assertEqual(out, ["在的", "嗯"])


class TranscriptStampTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_owner import make_env

        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = pathlib.Path(self._tmp.name)
        self.config, self.db, _ = make_env(self._tmp_path)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_only_user_turns_carry_timestamps(self) -> None:
        self.memory.add_turn("qq:private:1", "user", "鲸鲸今年多大了")
        self.memory.add_turn("qq:private:1", "assistant", "问女孩子年龄……")
        msgs = self.assembler.history_messages("qq:private:1")

        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertIsNotNone(STAMP.match(msgs[0]["content"]))
        self.assertIsNone(
            STAMP.match(msgs[1]["content"]),
            "assistant 历史带时间戳会让模型模仿，必须保持干净",
        )

    def test_dirty_row_is_scrubbed_when_transcript_is_rebuilt(self) -> None:
        self.memory.add_turn(
            "qq:private:3",
            "assistant",
            "[10-22 21:16] 问女孩子年龄……\n[10-22 21:17] 你就当比你小一点",
        )
        msgs = self.assembler.history_messages("qq:private:3")

        self.assertEqual(len(msgs), 1)
        self.assertNotIn("[10-22", msgs[0]["content"])
        self.assertIn("问女孩子年龄", msgs[0]["content"])

    def test_stamped_history_still_stable_between_turns(self) -> None:
        self.memory.add_turn("qq:private:2", "user", "在吗")
        self.memory.add_turn("qq:private:2", "assistant", "在的")
        first, second = self.assembler.history_messages("qq:private:2")
        time.sleep(0.01)
        again = self.assembler.history_messages("qq:private:2")
        self.assertEqual([first, second], again)


if __name__ == "__main__":
    unittest.main()
