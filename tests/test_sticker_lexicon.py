"""查询侧同义词扩展（fatfish/sticker/lexicon.py）的边界。

这张表是给 hash 后端补语义用的，但它的三条纪律比词表本身重要：
只扩查询侧、单跳不连锁、不重复追加。任何一条破了，调表就会变成
重算库或把查询淹掉。
"""

from __future__ import annotations

import unittest

from fatfish.sticker.lexicon import QUERY_ALIASES, expand_query


class ExpandQueryTests(unittest.TestCase):
    def test_no_match_returns_original(self) -> None:
        self.assertEqual(expand_query("今天天气不错"), "今天天气不错")

    def test_hit_appends_aliases(self) -> None:
        # 实测病例：用户说「惊到」，库里写「惊讶、没想到」。
        expanded = expand_query("真的惊到我了")
        self.assertIn("惊讶", expanded)
        self.assertIn("震惊", expanded)
        self.assertTrue(expanded.startswith("真的惊到我了"), "原文必须保留在最前")

    def test_single_hop_only(self) -> None:
        # 一跳定型：展开只扫原文，别名不当二次触发词。
        # 「气鼓鼓」只能经「生气」二跳到达，所以不许出现；
        # 「晚安」只能经「睡觉」二跳到达，同理。
        expanded = expand_query("气死我了")
        self.assertIn("生气", expanded)
        self.assertNotIn("气鼓鼓", expanded)
        expanded = expand_query("好困")
        self.assertIn("睡觉", expanded)
        self.assertNotIn("晚安", expanded)

    def test_existing_words_not_duplicated(self) -> None:
        # 查询里已有的词不再作为别名追加（中文无空格，直接数子串）。
        expanded = expand_query("我好难过")
        self.assertEqual(expanded.count("难过"), 1)
        self.assertIn("伤心", expanded)

    def test_multi_trigger_merge_is_deterministic(self) -> None:
        # 委屈->(难过,伤心) 与 难过->(伤心) 的交集只出现一次。
        expanded = expand_query("又委屈又难过")
        self.assertEqual(expanded.split().count("伤心"), 1)
        again = expand_query("又委屈又难过")
        self.assertEqual(expanded, again)

    def test_blank_is_blank(self) -> None:
        self.assertEqual(expand_query(""), "")
        self.assertEqual(expand_query("   "), "")

    def test_table_hygiene(self) -> None:
        # 表是手工维护的：空触发词/空别名组录进去等于埋雷。
        for trigger, aliases in QUERY_ALIASES.items():
            self.assertTrue(trigger.strip(), "触发词不能是空白")
            self.assertTrue(aliases, f"触发词 {trigger} 的别名组不能为空")
            for alias in aliases:
                self.assertTrue(alias.strip(), f"触发词 {trigger} 有空白别名")


if __name__ == "__main__":
    unittest.main()
