"""Tests for the short-sentence splitter used by the TTS iterator."""

from __future__ import annotations

import unittest

from fatfish.voice.segments import split_short_sentences

DEMO_LINES = {
    "01_home": (
        "哼，主人哥哥回来啦。……本鲸鱼娘<strong>才</strong>没有一直等你呢。"
        "尾巴？<strong>尾——</strong>巴自己摇的！",
        [
            "哼，主人哥哥回来啦。",
            "……本鲸鱼娘<strong>才</strong>没有一直等你呢。",
            "尾巴？",
            "<strong>尾——</strong>巴自己摇的！",
        ],
    ),
    "02_miss": (
        "主人哥哥，我今天<strong>超——</strong>级想你！……才、才不是故意说出来的！",
        [
            "主人哥哥，我今天<strong>超——</strong>级想你！",
            "……才、才不是故意说出来的！",
        ],
    ),
    "03_care": (
        "<strong>先躺下</strong>，<strong>别</strong>刷手机了。……我陪你一会儿。",
        [
            "<strong>先躺下</strong>，<strong>别</strong>刷手机了。",
            "……我陪你一会儿。",
        ],
    ),
    "04_jealous": (
        "你是不是<strong>又</strong>在看别的鲸鱼娘？……哼，我才不在乎。",
        [
            "你是不是<strong>又</strong>在看别的鲸鱼娘？",
            "……哼，我才不在乎。",
        ],
    ),
    "05_praise": (
        "才、才不是因为你夸我才开心的。……<strong>就一点点</strong>啦。",
        [
            "才、才不是因为你夸我才开心的。",
            "……<strong>就一点点</strong>啦。",
        ],
    ),
    "06_serious": (
        "我在。[breath]你慢慢说。",
        ["我在。", "[breath]你慢慢说。"],
    ),
    "07_night": (
        "晚安。……不许熬夜哦。",
        ["晚安。", "……不许熬夜哦。"],
    ),
    "08_share": (
        "主人哥哥，我看到一个<strong>超</strong>好玩的！……你猜是什么？",
        [
            "主人哥哥，我看到一个<strong>超</strong>好玩的！",
            "……你猜是什么？",
        ],
    ),
}


class SplitterTests(unittest.TestCase):
    def test_demo_lines(self) -> None:
        for name, (text, expected) in DEMO_LINES.items():
            with self.subTest(line=name):
                self.assertEqual(split_short_sentences(text), expected)

    def test_demo_lines_keep_protected_markup(self) -> None:
        for name, (text, _expected) in DEMO_LINES.items():
            with self.subTest(line=name):
                chunks = split_short_sentences(text)
                self.assertEqual(
                    sum(chunk.count("<strong>") for chunk in chunks),
                    text.count("<strong>"),
                )
                if "[breath]" in text:
                    self.assertTrue(any("[breath]" in chunk for chunk in chunks))

    def test_long_comma_sentence(self) -> None:
        text = "今天的海面很安静，风也很轻，我想和你一起看很久很久的星星，可以吗？"
        chunks = split_short_sentences(text)
        self.assertEqual(
            chunks,
            [
                "今天的海面很安静，风也很轻，",
                "我想和你一起看很久很久的星星，可以吗？",
            ],
        )
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))

    def test_english_words_and_numbers_are_not_cut(self) -> None:
        text = "I have 123 apples and 456 oranges, can you count them?"
        chunks = split_short_sentences(text, max_chars=20)
        self.assertEqual(
            chunks,
            ["I have 123 apples", "and 456 oranges, can", "you count them?"],
        )
        self.assertEqual(" ".join(chunks), text)
        for token in ("123", "apples", "456", "oranges", "count", "them"):
            self.assertTrue(any(token in chunk for chunk in chunks), token)

    def test_empty_and_ellipsis_only(self) -> None:
        self.assertEqual(split_short_sentences(""), [])
        self.assertEqual(split_short_sentences("   \n\t "), [])
        self.assertEqual(split_short_sentences("……"), [])
        self.assertEqual(split_short_sentences("......"), [])
        self.assertEqual(split_short_sentences("。。！？"), [])

    def test_ellipsis_runs_collapse(self) -> None:
        self.assertEqual(
            split_short_sentences("嗯…………", min_chars=0), ["嗯……"]
        )
        self.assertEqual(
            split_short_sentences("等等......", min_chars=0), ["等等……"]
        )
        self.assertEqual(
            split_short_sentences("我……想你了。"), ["我……", "想你了。"]
        )

    def test_protected_tags_are_never_split(self) -> None:
        text = "前面的话很多很多<strong>重点词</strong>后面的话也很多。"
        chunks = split_short_sentences(text, max_chars=10)
        self.assertTrue(any("<strong>重点词</strong>" in chunk for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(chunk.count("<strong>"), chunk.count("</strong>"))

        em = split_short_sentences(
            "第一段<em>很重要</em>。[breath]第二段也说一点。", min_chars=2
        )
        self.assertIn("第一段<em>很重要</em>。", em)
        self.assertTrue(any("[breath]" in chunk for chunk in em))
        self.assertEqual(
            split_short_sentences("我在。[breath]"),
            ["我在。[breath]"],
        )

    def test_tag_content_is_not_used_as_a_split_point(self) -> None:
        text = "<strong>别、怕。</strong>我在这里等着你。"
        chunks = split_short_sentences(text, max_chars=8, min_chars=2)
        self.assertEqual(
            chunks,
            ["<strong>别、怕。</strong>我在这里", "等着你。"],
        )

    def test_short_complete_sentences_stay_separate(self) -> None:
        self.assertEqual(
            split_short_sentences("好。今天天气不错。"),
            ["好。", "今天天气不错。"],
        )
        self.assertEqual(
            split_short_sentences("今天天气不错。晚安。", min_chars=4),
            ["今天天气不错。", "晚安。"],
        )
        self.assertEqual(
            split_short_sentences("短。后面的句子稍微长一点。", min_chars=4),
            ["短。", "后面的句子稍微长一点。"],
        )

    def test_max_chars_hard_cap_without_punctuation(self) -> None:
        text = "这是一段没有标点符号的很长很长的句子需要被切开并且保持每段不超过二十个字符"
        chunks = split_short_sentences(text, max_chars=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks), chunks)
        self.assertEqual("".join(chunks), text)

        # Terminal punctuation stays attached without pushing a chunk over the
        # cap when a viable earlier split exists.
        for capped in ("啊" * 20 + "！", "啊" * 20 + "。", "啊" * 20 + "，"):
            pieces = split_short_sentences(capped, max_chars=20, min_chars=4)
            self.assertTrue(all(len(piece) <= 20 for piece in pieces), pieces)
            self.assertEqual("".join(pieces), capped)

    def test_closing_quotes_and_brackets_stay_with_previous(self) -> None:
        self.assertEqual(
            split_short_sentences("“晚安。”他轻声说。"),
            ["“晚安。”", "他轻声说。"],
        )
        self.assertEqual(
            split_short_sentences("（真的吗？）然后呢。", min_chars=2),
            ["（真的吗？）", "然后呢。"],
        )
        self.assertEqual(
            split_short_sentences("「好的」……我们走吧。", min_chars=2),
            ["「好的」……", "我们走吧。"],
        )

    def test_parenthetical_aside_is_not_cut_apart(self) -> None:
        # A hard cap used to cut this into "……尾巴慢慢晃" + "了一下）".
        text = "本鲸鱼娘还懒懒地趴着呢（尾巴慢慢晃了一下）"
        self.assertEqual(split_short_sentences(text, max_chars=20), [text])
        for chunk in split_short_sentences(
            "前面（尾巴慢慢晃了一下）后面还有一点", max_chars=20
        ):
            self.assertEqual(chunk.count("（"), chunk.count("）"), chunk)

    def test_newlines_split_and_do_not_create_blanks(self) -> None:
        chunks = split_short_sentences("第一句话。\n\n第二句话在这里。")
        self.assertEqual(chunks, ["第一句话。", "第二句话在这里。"])
        self.assertFalse(any(not chunk.strip() for chunk in chunks))

    def test_configurable_limits(self) -> None:
        text = "今天的海面很安静，风也很轻，我想回家啦。晚安。"
        self.assertEqual(
            split_short_sentences(text, max_chars=10, min_chars=4),
            ["今天的海面很安静，", "风也很轻，", "我想回家啦。", "晚安。"],
        )


if __name__ == "__main__":
    unittest.main()
