"""Pin the IndexTTS-2.5 tone map.

The map is deliberately data, not prose: a wrong channel order or a swapped
reference is inaudible in code review but very audible in production.  These
tests are cheap and keep the accepted six-tone result from drifting.
"""

from __future__ import annotations

import unittest

from fatfish.tts.indextts import (
    DEFAULT_PITCH_SEMITONES,
    EMOTION_ORDER,
    INTERVAL_SILENCE_MS,
    TONE_REFS,
    TONE_VECTORS,
    interval_silence_for,
    post_filter_for,
    reference_name_for,
    vector_for,
)
from fatfish.tts.instruct import TONES


class IndexTTSToneMapTests(unittest.TestCase):
    def test_every_tone_has_a_reference(self) -> None:
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertIn(tone, TONE_REFS)
                self.assertTrue(TONE_REFS[tone].endswith(".wav"))

    def test_neutral_means_no_vector(self) -> None:
        # 与 CosyVoice 的 instruct 一样：neutral 不是"一条中性向量"，而是
        # "别给向量"，让参考音自己的韵律出来。
        self.assertIsNone(vector_for("neutral"))
        self.assertIsNone(vector_for("???"))

    def test_vectors_are_eight_dimensional_and_bounded(self) -> None:
        for tone in TONES:
            with self.subTest(tone=tone):
                vector = vector_for(tone)
                if vector is None:
                    continue
                self.assertEqual(len(vector), len(EMOTION_ORDER))
                self.assertTrue(all(0.0 <= value <= 1.0 for value in vector))
                self.assertGreater(sum(vector), 0.0)

    def test_every_weight_uses_a_known_emotion(self) -> None:
        for tone, weights in TONE_VECTORS.items():
            with self.subTest(tone=tone):
                self.assertTrue(set(weights) <= set(EMOTION_ORDER))

    def test_emotion_order_is_the_model_order(self) -> None:
        # IndexTTS-2 的 8 维顺序；写错一位就是"开心"变"生气"。
        self.assertEqual(
            EMOTION_ORDER,
            (
                "happy",
                "angry",
                "sad",
                "afraid",
                "disgusted",
                "melancholic",
                "surprised",
                "calm",
            ),
        )

    def test_every_tone_uses_the_v6_reference(self) -> None:
        # 2.5 的 v6 声线只有一个人身份锚点；参考音不再按语气切换。
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertEqual(reference_name_for(tone), "cand_home_up1.wav")

    def test_annoyed_is_aggrieved_not_angry(self) -> None:
        # 用户明确要求“愤怒”听起来像委屈，不是凶。angry 归零，sad/happy/calm
        # 才是小妹妹吃醋的底色；以后不要为了“更生气”把 angry 加回来。
        weights = TONE_VECTORS["annoyed"]
        self.assertEqual(weights.get("angry", 0.0), 0.0)
        self.assertGreaterEqual(weights.get("sad", 0.0), 0.30)
        self.assertGreaterEqual(weights.get("happy", 0.0), 0.20)

    def test_sad_is_not_allowed_to_swallow_the_words(self) -> None:
        # 2.5 的 sad 偏置是 1.0，0.38 是八场景实测里既能听出难过、
        # 又不吞字的上限；以后不要为了“更难过”把权重加回去。
        self.assertLessEqual(TONE_VECTORS["sad"]["sad"], 0.40)
        self.assertGreater(TONE_VECTORS["sad"]["happy"], 0.0)

    def test_pitch_overlay_is_config_driven_and_off_by_default(self) -> None:
        # 参考音 cand_home_up1.wav 离线制作时已经 +2 半音；管线再叠 +2 就是
        # +4，音高过头（2026-10-25 用户反馈）。叠加改成 tts.pitch_semitones，
        # 默认 0 = 不过滤；负值同样视为不叠加（机制只做升调）。
        self.assertEqual(DEFAULT_PITCH_SEMITONES, 0)
        self.assertIsNone(post_filter_for(0))
        self.assertIsNone(post_filter_for(-1))
        self.assertIsNone(post_filter_for(-2))

    def test_pitch_two_reproduces_the_legacy_filter(self) -> None:
        # 旧 +2 机制原样保留，供换回原调参考音时使用：asetrate 提频
        # 1.122（= 2 ** (2/12)）+ atempo 时长还原。
        self.assertEqual(
            post_filter_for(2),
            "asetrate=22050*1.122,aresample=22050,atempo=1/1.122",
        )

    def test_post_filter_tracks_the_sample_rate(self) -> None:
        # 旧实现用字符串替换 "22050"；新实现按采样率生成，任何速率都不残留旧值。
        spec = post_filter_for(2, 24000)
        assert spec is not None
        self.assertIn("asetrate=24000*1.122", spec)
        self.assertIn("aresample=24000", spec)
        self.assertIn("atempo", spec)
        self.assertNotIn("22050", spec)

    def test_interval_silence_is_short_and_global(self) -> None:
        # 200ms 的默认停顿被 mimo 反复判成"硬切/断成两截"；120ms 是
        # 同时改善 neutral/happy/sad/annoyed/lively 的那一档。
        self.assertEqual(INTERVAL_SILENCE_MS, 120)
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertEqual(interval_silence_for(tone), 120)


if __name__ == "__main__":
    unittest.main()
