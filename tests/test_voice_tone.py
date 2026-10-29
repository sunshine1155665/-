"""语气兜底判定：模型没明说时，从回复文本读一个类别。

为什么需要它：自发语音（绝大多数）根本不经过工具调用，模型没有机会指定语气。
没有兜底的话，`instruct` 那条通道就只对"模型恰好调了 send_voice"的少数轮次
有效 —— 参数接好了，实际还是几乎从不生效。
"""

from __future__ import annotations

import unittest

from fatfish.tts.instruct import TONES
from fatfish.voice.tone import TONE_FLOOR, reply_tone, tone_from_text


class ToneFromTextTests(unittest.TestCase):
    def test_scene_words_win_regardless_of_intensity(self) -> None:
        # 「晚安」本身就 3.0（场景地板），而「陪你」只有 1.7 —— 但两句话该用
        # 什么语气是由场合定的，不是由激动程度定的。
        self.assertEqual(tone_from_text("晚安啦，做个好梦"), "soft")
        self.assertEqual(tone_from_text("那就多陪你一会儿"), "soft")
        self.assertEqual(tone_from_text("那就多陪你一会儿"), "soft")

    def test_flat_reply_gets_no_tone(self) -> None:
        # 交代事情、说事实不该换语气 —— neutral 就是 zero-shot，等于没 instruct。
        for text in ("我回来了。", "帮我把这段话翻译成英文", "把这份材料整理成表格"):
            with self.subTest(text=text):
                self.assertEqual(tone_from_text(text), "neutral")

    def test_complaint_word_is_not_anger(self) -> None:
        # 命中「烦」但强度只有 0.8：那是在嫌公式烦，不是在发脾气。拿生气语气
        # 念技术解释会立刻出戏，所以情绪类必须过门槛。
        self.assertEqual(tone_from_text("这个公式量纲不对，烦"), "neutral")

    def test_real_anger_is_detected(self) -> None:
        self.assertEqual(tone_from_text("哼，才不是因为你夸我才开心的"), "annoyed")

    def test_passive_scolding_is_not_her_anger(self) -> None:
        # 回归锚点：这是鲸鱼娘安慰一个刚被批评的用户，原句里有「批评」，
        # 曾经的实现把它判成 annoyed —— 于是用"非常生气"的语气去哄人。
        # 被动/受事（被、挨、遭）说的是"谁受了这件事"，不是"谁在生气"。
        reply = (
            "诶……被批评了？那一定很难受吧（小声）\n"
            "先别动了，就靠着歇一会儿。被批评不代表你不好，这点我先替你记着\n"
            "要是想说说发生了什么，我在这儿听着；不想说就安静待着也行"
        )
        self.assertEqual(tone_from_text(reply), "soft")

    def test_active_scolding_is_still_anger(self) -> None:
        # 同一个字，主动用法不能一起被放过。
        self.assertEqual(tone_from_text("哼，你再骂我一句试试！！"), "annoyed")

    def test_comfort_outranks_topic_sadness(self) -> None:
        # 「难受/累」是话题，「小声/别怕/抱抱」是开口方式。哄人的句子必然提到
        # 前者，所以 SOFT 必须排在 SAD 前面，否则安慰话会被念成悲伤。
        self.assertEqual(tone_from_text("别难过了，抱抱"), "soft")
        self.assertEqual(tone_from_text("是不是很累？别怕，我在这儿"), "soft")

    def test_plain_sadness_still_reads_as_sad(self) -> None:
        # 让步不能把真正的难过也吃掉。
        self.assertEqual(tone_from_text("我今天真的好难过，想哭"), "sad")

    def test_tiredness_reads_as_lazy_not_sad(self) -> None:
        # 疲倦归慵懒。判成 sad 会被念成有气无力的虚弱 —— 用户的原话是
        # "元气是底层逻辑，疲倦要变成慵懒"。所以疲惫词走 soft（速度不变）。
        self.assertEqual(tone_from_text("好累啊，今天真的好累"), "soft")
        self.assertEqual(tone_from_text("我困死了，一点都不想动"), "soft")
        self.assertEqual(tone_from_text("又熬夜了，好乏"), "soft")

    def test_real_sadness_still_reads_as_sad(self) -> None:
        self.assertEqual(tone_from_text("我有点难过，怎么办啊"), "sad")
        self.assertEqual(tone_from_text("越想越委屈，好想哭"), "sad")

    def test_strong_positive_becomes_lively(self) -> None:
        # 要真的"炸"才用最快那条指令。模型实测把「今天真的好开心！」（3.50）
        # 仍判 happy，所以 lively 的门槛抬到了 3.8。
        self.assertEqual(tone_from_text("好耶！！啊啊啊他居然回我了"), "lively")
        self.assertEqual(tone_from_text("笑死我了！！"), "lively")

    def test_mild_positive_stays_happy(self) -> None:
        # 有点开心 ≠ 兴奋。用「开心 + 尽可能快」念一句平淡的庆幸会立刻出戏。
        self.assertEqual(tone_from_text("今天真的好开心！"), "happy")
        self.assertEqual(tone_from_text("我考了满分！！"), "happy")

    def test_every_result_is_a_real_tone(self) -> None:
        for text in ("", "   ", "哼", "晚安", "好累", "开心！！", "随便说说"):
            with self.subTest(text=text):
                self.assertIn(tone_from_text(text), TONES)

    def test_floor_matches_the_sticker_gate(self) -> None:
        # 两处用同一个刻度，调一个的时候另一个不会偷偷变得不一致。
        self.assertEqual(TONE_FLOOR, 2.0)


class ReplyToneTests(unittest.TestCase):
    def test_the_model_always_wins(self) -> None:
        # 模型知道自己想表达什么，还能读上下文；文本规则读不到反讽。
        self.assertEqual(reply_tone("帮我把这段话翻译成英文", "lively"), "lively")

    def test_chinese_from_the_model_is_understood(self) -> None:
        self.assertEqual(reply_tone("随便", "撒娇"), "soft")
        self.assertEqual(reply_tone("随便", "元气"), "lively")

    def test_missing_or_junk_falls_back_to_the_text(self) -> None:
        self.assertEqual(reply_tone("晚安啦", None), "soft")
        self.assertEqual(reply_tone("晚安啦", ""), "soft")
        self.assertEqual(reply_tone("晚安啦", "   "), "soft")
        # 未知值不能报错，也不能让整条语音失败 —— 收到 neutral 就等于没指定。
        self.assertEqual(reply_tone("我回来了。", "extremely-happy"), "neutral")


if __name__ == "__main__":
    unittest.main()
