"""Role isolation for the last-resort fallback reply.

2026-10-31: a teacher session (杨老师) hit the empty-LLM-plan fallback and was
called 主人哥哥.  The fallback must address the actual speaker.
"""

from __future__ import annotations

import unittest

from fatfish.runtime import _fallback_reply, parse_turn_plan


class FallbackReplyTests(unittest.TestCase):
    def test_owner_generic_keeps_owner_address(self) -> None:
        self.assertIn("主人哥哥", _fallback_reply("哼", role="owner"))

    def test_teacher_generic_uses_display_name(self) -> None:
        text = _fallback_reply("哼", role="teacher", name="杨老师")
        self.assertIn("杨老师", text)
        self.assertNotIn("主人哥哥", text)

    def test_teacher_goodnight_uses_display_name(self) -> None:
        text = _fallback_reply("晚安", role="teacher", name="杨老师")
        self.assertIn("杨老师早点睡", text)
        self.assertNotIn("主人哥哥", text)

    def test_friend_uses_display_name(self) -> None:
        text = _fallback_reply("哼", role="friend", name="姐姐")
        self.assertIn("姐姐", text)
        self.assertNotIn("主人哥哥", text)

    def test_teacher_intro_never_calls_owner(self) -> None:
        text = _fallback_reply("你是谁呀", role="teacher", name="杨老师")
        self.assertNotIn("主人哥哥", text)

    def test_unknown_speaker_is_neutral(self) -> None:
        text = _fallback_reply("哼")
        self.assertIn("你慢慢说", text)
        self.assertNotIn("主人哥哥", text)

    def test_empty_plan_is_not_raw_json(self) -> None:
        plan = parse_turn_plan('{"messages": [], "actions": []}')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.messages, [])
        self.assertEqual(plan.reply, "")
        plain = parse_turn_plan("就是普通文本")
        self.assertEqual(plain.reply, "就是普通文本")


if __name__ == "__main__":
    unittest.main()
