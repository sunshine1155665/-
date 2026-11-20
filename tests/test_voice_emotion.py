"""Lock the edges of the 0-5 feeling scale used by the voice tone fallback.

These sentences are not random: each one was a real miscalculation.  The first
version gave every mood word the same 0.6 and every task word -0.8, so:

    「我好难过，怎么办啊」 1.40 —— treated as business because of 「怎么」
    「写代码写到崩溃」     1.40 —— treated as business because of 「代码」

The fix split the vocabularies into tiers (moods strong/mild, tasks
request/help/topic).  The numbers themselves are a judgement call, but these
two kinds of sentence dropping below the tone floor is a clear error.
"""

from __future__ import annotations

import unittest

from fatfish.voice.emotion import breakdown, emotion_intensity


class EmotionScaleTests(unittest.TestCase):
    def test_a_feeling_wrapped_in_a_question_still_counts(self) -> None:
        # 「怎么办」是难受时的求助措辞，不是任务指令。
        self.assertGreaterEqual(emotion_intensity("我好难过，怎么办啊"), 2.0)

    def test_a_topic_word_does_not_flatten_a_feeling(self) -> None:
        self.assertGreaterEqual(emotion_intensity("写代码写到崩溃"), 2.0)

    def test_a_request_that_also_carries_feeling_survives(self) -> None:
        for text in ("想哭，帮我看看这个错在哪", "累死了，今天还要写代码"):
            self.assertGreaterEqual(emotion_intensity(text), 2.0, text)

    def test_plain_work_requests_stay_low(self) -> None:
        for text in ("帮我把这段话翻译成英文", "把这份材料整理成表格", "这个公式怎么推导"):
            self.assertLess(emotion_intensity(text), 1.5, text)

    def test_scene_words_have_a_floor(self) -> None:
        # 道晚安不需要"很激动"才配柔和的语气，场合本身就是理由。
        for text in ("晚安", "早安", "谢谢你"):
            self.assertGreaterEqual(emotion_intensity(text), 3.0, text)

    def test_bare_acknowledgement_is_almost_flat(self) -> None:
        for text in ("嗯", "好的", "哦"):
            self.assertLess(emotion_intensity(text), 1.5, text)

    def test_breakdown_explains_the_score(self) -> None:
        parts = breakdown("累死了……")
        self.assertIn("mood", parts)
        self.assertIn("intensifier", parts)
        self.assertAlmostEqual(parts["score"], emotion_intensity("累死了……"))

    def test_empty_text_is_zero_not_a_quote(self) -> None:
        self.assertEqual(emotion_intensity(""), 0.0)
        self.assertEqual(emotion_intensity("   "), 0.0)


if __name__ == "__main__":
    unittest.main()
