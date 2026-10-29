"""Offline tests for the post_qzone action policy: trust matrix, allowlist,
prompt wording and the qzone cookie redaction patterns.

No network, no real cookies: every credential-looking string is synthetic.
"""

from __future__ import annotations

import unittest

from fatfish.context import TOOL_SCHEMA
from fatfish.runtime import ALLOWED_ACTIONS, TurnAction, parse_turn_plan
from fatfish.security import TrustLevel, contains_credentials, redact_outbound
from fatfish.security.policy import ACTION_SPECS, PolicyEngine


class PostQzonePolicyMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyEngine()

    def test_private_trust_with_content_is_allowed(self) -> None:
        decision = self.policy.validate(
            "post_qzone", {"content": "今天也是想主人哥哥的一天"}, trust=TrustLevel.PRIVATE
        )
        self.assertTrue(decision.allowed)
        self.assertEqual("ok", decision.reason)

    def test_spec_shape_is_write_and_private(self) -> None:
        spec = ACTION_SPECS["post_qzone"]
        self.assertEqual("content", spec.required_text)
        self.assertTrue(spec.write)
        self.assertFalse(spec.allow_group)
        self.assertEqual(TrustLevel.PRIVATE, spec.min_trust)
        self.assertEqual(1, spec.max_calls)

    def test_group_chat_is_rejected(self) -> None:
        decision = self.policy.validate(
            "post_qzone", {"content": "hi"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("group_not_allowed", decision.reason)

    def test_external_and_memory_trust_are_too_low(self) -> None:
        for trust in (TrustLevel.EXTERNAL, TrustLevel.MEMORY, TrustLevel.UNKNOWN):
            decision = self.policy.validate("post_qzone", {"content": "hi"}, trust=trust)
            self.assertFalse(decision.allowed, trust)
            self.assertTrue(decision.reason.startswith("trust_too_low"), trust)

    def test_second_call_in_same_turn_is_rejected(self) -> None:
        first = self.policy.validate(
            "post_qzone", {"content": "一"}, trust=TrustLevel.PRIVATE, count=0
        )
        self.assertTrue(first.allowed)
        second = self.policy.validate(
            "post_qzone", {"content": "二"}, trust=TrustLevel.PRIVATE, count=1
        )
        self.assertFalse(second.allowed)
        self.assertEqual("too_many_tool_calls", second.reason)

    def test_missing_content_is_rejected(self) -> None:
        for args in ({}, {"content": ""}, {"content": "   "}):
            decision = self.policy.validate("post_qzone", args, trust=TrustLevel.PRIVATE)
            self.assertFalse(decision.allowed)
            self.assertEqual("missing_content", decision.reason)

    def test_unknown_action_rejected(self) -> None:
        decision = self.policy.validate("post_qzone_x", {"content": "hi"}, trust=TrustLevel.PRIVATE)
        self.assertFalse(decision.allowed)
        self.assertEqual("unknown_action", decision.reason)


class PostQzoneAllowlistTests(unittest.TestCase):
    def test_action_is_allowlisted(self) -> None:
        self.assertIn("post_qzone", ALLOWED_ACTIONS)

    def test_share_with_owner_is_allowlisted(self) -> None:
        self.assertIn("share_with_owner", ALLOWED_ACTIONS)

    def test_parse_turn_plan_keeps_post_qzone(self) -> None:
        plan = parse_turn_plan(
            '{"messages":["好耶"],"actions":[{"tool":"post_qzone","args":{"content":"出海啦"}}]}'
        )
        assert plan is not None
        self.assertTrue(plan.parsed)
        self.assertEqual([TurnAction("post_qzone", {"content": "出海啦"})], plan.actions)

    def test_parse_turn_plan_keeps_share_with_owner_with_desire(self) -> None:
        plan = parse_turn_plan(
            '{"messages":["给你看个东西"],"actions":['
            '{"tool":"share_with_owner","args":{"text":"这条鲸鱼好胖","desire":0.62}}]}'
        )
        assert plan is not None
        self.assertTrue(plan.parsed)
        self.assertEqual(
            [TurnAction("share_with_owner", {"text": "这条鲸鱼好胖", "desire": 0.62})],
            plan.actions,
        )

    def test_parse_turn_plan_still_drops_unknown_actions(self) -> None:
        plan = parse_turn_plan(
            '{"messages":["好耶"],"actions":[{"tool":"delete_qzone","args":{}}]}'
        )
        assert plan is not None
        self.assertEqual([], plan.actions)


class ShareWithOwnerPolicyMatrixTests(unittest.TestCase):
    """share_with_owner: private-only, twice a turn, wakeup not direct send."""

    def setUp(self) -> None:
        self.policy = PolicyEngine()

    def test_private_trust_with_text_is_allowed(self) -> None:
        decision = self.policy.validate(
            "share_with_owner", {"text": "主人哥哥你看这个"}, trust=TrustLevel.PRIVATE
        )
        self.assertTrue(decision.allowed)
        self.assertEqual("ok", decision.reason)

    def test_spec_shape_is_write_private_and_twice_per_turn(self) -> None:
        spec = ACTION_SPECS["share_with_owner"]
        self.assertEqual("text", spec.required_text)
        self.assertTrue(spec.write)
        self.assertFalse(spec.allow_group)
        self.assertEqual(TrustLevel.PRIVATE, spec.min_trust)
        self.assertEqual(2, spec.max_calls)

    def test_group_chat_is_rejected(self) -> None:
        decision = self.policy.validate(
            "share_with_owner", {"text": "hi"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("group_not_allowed", decision.reason)

    def test_external_and_memory_trust_are_too_low(self) -> None:
        for trust in (TrustLevel.EXTERNAL, TrustLevel.MEMORY, TrustLevel.UNKNOWN):
            decision = self.policy.validate("share_with_owner", {"text": "hi"}, trust=trust)
            self.assertFalse(decision.allowed, trust)
            self.assertTrue(decision.reason.startswith("trust_too_low"), trust)

    def test_third_call_in_same_turn_is_rejected(self) -> None:
        for count in (0, 1):
            decision = self.policy.validate(
                "share_with_owner", {"text": "一"}, trust=TrustLevel.PRIVATE, count=count
            )
            self.assertTrue(decision.allowed, count)
        third = self.policy.validate(
            "share_with_owner", {"text": "三"}, trust=TrustLevel.PRIVATE, count=2
        )
        self.assertFalse(third.allowed)
        self.assertEqual("too_many_tool_calls", third.reason)

    def test_missing_text_is_rejected(self) -> None:
        for args in ({}, {"text": ""}, {"text": "   "}):
            decision = self.policy.validate("share_with_owner", args, trust=TrustLevel.PRIVATE)
            self.assertFalse(decision.allowed)
            self.assertEqual("missing_text", decision.reason)

    def test_desire_arg_is_optional_and_not_required_text(self) -> None:
        # desire is metadata only; its absence must never block the action.
        decision = self.policy.validate(
            "share_with_owner", {"text": "hi"}, trust=TrustLevel.PRIVATE
        )
        self.assertTrue(decision.allowed)


class ToolSchemaWordingTests(unittest.TestCase):
    def test_schema_documents_post_qzone_and_approval(self) -> None:
        self.assertIn("post_qzone", TOOL_SCHEMA)
        # Below the threshold the review path is still the owner's promise.
        self.assertIn("审批", TOOL_SCHEMA)
        self.assertIn("content", TOOL_SCHEMA)

    def test_schema_documents_desire_anchors(self) -> None:
        self.assertIn("desire", TOOL_SCHEMA)
        self.assertIn("0.8", TOOL_SCHEMA)
        self.assertIn("0.6-0.8", TOOL_SCHEMA)
        self.assertIn("0.3-0.6", TOOL_SCHEMA)
        self.assertIn("0.7", TOOL_SCHEMA)

    def test_schema_documents_share_with_owner_and_routing(self) -> None:
        self.assertIn("share_with_owner", TOOL_SCHEMA)
        self.assertIn('"text"', TOOL_SCHEMA)
        # The observation-routing rule: reactions live in chat first, the
        # feed only for strong desire moments.
        self.assertIn("观后感", TOOL_SCHEMA)
        self.assertIn("碎碎念", TOOL_SCHEMA)


class QzoneCookieRedactionTests(unittest.TestCase):
    """Cookie field shapes must never ride a draft out; ordinary numbers must
    never be mistaken for one."""

    def test_qzone_cookie_assignments_detected(self) -> None:
        cases = {
            "skey": "skey=@abcdefgh",
            "skey_colon": "skey: @abcdefgh",
            "p_skey": "p_skey=Zxy12345token",
            "p_uin": "p_uin=oP01zJm2",
            "luin": "luin=oP01zJm2",
            "qzonetoken": "qzonetoken=abcdef123456",
            "qzone_cookie": "qzone_cookie=uin=10000; skey=abc",
            "uppercase": "SKEY = @AbcDefGh",
        }
        for name, text in cases.items():
            self.assertTrue(contains_credentials(f"带出去就完了 {text}"), name)

    def test_secret_note_and_bilibili_patterns_still_detected(self) -> None:
        self.assertTrue(contains_credentials("SESSDATA=abcdef123456"))
        self.assertTrue(contains_credentials("refresh_token=abcdef123456"))

    def test_ordinary_text_not_flagged(self) -> None:
        safe = [
            "今天中午吃了3个包子，花了12块5。",
            "monkey=1 is not a credential",
            "whiskey=1 更不是",
            "我的账号等级是 62 级",
            "键盘 key=0 的意思是初始按键",
            "2026-10-24 14:00 出海",
        ]
        for text in safe:
            self.assertFalse(contains_credentials(text), text)

    def test_redact_outbound_masks_qzone_values(self) -> None:
        text, hits = redact_outbound("这是我的 skey=@abcdef123 别丢了")
        self.assertNotIn("@abcdef123", text)
        self.assertIn("skey=[已隐藏]", text)
        self.assertTrue(hits)

    def test_redact_outbound_keeps_ordinary_text(self) -> None:
        original = "今天吃了3个包子，花了12块5，owner=主人哥哥。"
        text, hits = redact_outbound(original, internal=False)
        self.assertEqual(original, text)
        self.assertEqual([], hits)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
