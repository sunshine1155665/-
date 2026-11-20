"""「主人哥哥不差钱」人设块 (T-0043).

The block lives in ``ContextAssembler.stable_prefix()`` rather than in
``DEFAULT_PERSONA``, because the live persona is read from ``persona.core_json``.
The regression that matters: someone "fixing" this later by editing only the
default dict, which does nothing on an already-initialised database.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.context import MONEY_BLOCK, ContextAssembler
from fatfish.memory import MemoryService
from tests.test_owner import OWNER_SESSION, make_env

GROUP_SESSION = "qq:group:77777"
FRIEND_SESSION = "qq:private:20002"

# Positive directives that would tell her to economise. They are deliberately
# *positive* forms only: the block itself quotes "省钱" / "太贵了" inside a
# negation (「别把『省钱』挂在嘴边」), so a blanket ``assertNotIn("省钱")``
# would fail on correct copy. Anything mentioning these roots must sit in a
# negated clause -- see ``test_no_reverse_money_guidance``.
BANNED_POSITIVE = (
    "能省则省",
    "省着点花",
    "要省钱",
    "少花点",
    "别乱花钱",
    "别乱花",
    "太贵了就别买",
    "太贵就别买",
    "别买了",
)
# Encouraging overspending / borrowing is out of scope too.
BANNED_ESCALATION = ("随便花", "借钱", "转账", "贷款", "刷爆", "奢侈")
MONEY_ROOTS = ("省", "太贵", "抠")
NEGATIONS = ("别", "不要", "不用", "不必", "不许", "不能")


class PersonaMoneyBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_default_db_prefix_carries_the_block(self) -> None:
        stable = self.assembler.stable_prefix()

        self.assertIn("不差钱", stable)
        self.assertIn(MONEY_BLOCK, stable)
        self.assertEqual(stable.count(MONEY_BLOCK), 1, "block injected twice")
        # Sits with the persona text, ahead of the style examples.
        self.assertLess(stable.index("【关系】"), stable.index("【不计较钱】"))
        self.assertLess(stable.index("【不计较钱】"), stable.index("【语气示例】"))

    def test_block_survives_a_user_customised_persona(self) -> None:
        """Guards the "only edit DEFAULT_PERSONA" mistake."""
        custom = {
            "identity": "我是主人哥哥手改过的人设。",
            "personality": "冷静。",
            "speech": "短句。",
            "relationship": "同事。",
            "boundaries": ["不乱说。"],
            "examples": ["嗯。"],
        }
        self.db.execute(
            "UPDATE persona SET core_json=? WHERE id='default'",
            (json.dumps(custom, ensure_ascii=False),),
        )

        stable = self.assembler.stable_prefix()

        self.assertIn("手改过的人设", stable)  # the edit really took effect
        self.assertNotIn("一条会出卖心情的尾巴", stable)  # default identity is gone
        self.assertIn("不差钱", stable)
        # ...and it also reaches the built prompt, not just the raw prefix.
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        self.assertIn("不差钱", bundle.model_messages()[0]["content"])

    def test_no_reverse_money_guidance(self) -> None:
        stable = self.assembler.stable_prefix()

        for phrase in BANNED_POSITIVE + BANNED_ESCALATION:
            self.assertNotIn(phrase, stable, f"reverse guidance leaked: {phrase}")

        # Structural check on the block itself: every clause that mentions
        # economising must be a *negated* one (「别把省钱挂在嘴边」).
        clauses = [c for c in MONEY_BLOCK.replace("\n", "；").split("；") if c.strip()]
        self.assertTrue(clauses)
        for clause in clauses:
            if any(root in clause for root in MONEY_ROOTS):
                self.assertTrue(
                    any(word in clause for word in NEGATIONS),
                    f"un-negated money directive: {clause}",
                )

    def test_prefix_is_byte_identical_on_repeat_calls(self) -> None:
        first = self.assembler.stable_prefix()
        second = self.assembler.stable_prefix()
        self.assertEqual(first, second)

        built = self.assembler.build(OWNER_SESSION, "在吗", now=time.time(), role="owner")
        self.assertIn("不差钱", built.model_messages()[0]["content"])

    def test_block_is_global_not_owner_only(self) -> None:
        owner = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        group = self.assembler.build(GROUP_SESSION, "在吗", role=None)
        friend = self.assembler.build(FRIEND_SESSION, "在吗", role="friend")

        for bundle in (owner, group, friend):
            self.assertIn("不差钱", bundle.model_messages()[0]["content"])
        # Same persona block for every audience on the same prompt variant;
        # since 2026-11-15 the QQ group uses the minimal variant (different
        # tool section + group rules), so its prefix is deliberately different.
        self.assertEqual(
            {
                owner.model_messages()[0]["content"],
                friend.model_messages()[0]["content"],
            },
            {self.assembler.stable_prefix()},
        )
        self.assertNotEqual(
            group.model_messages()[0]["content"],
            self.assembler.stable_prefix(),
        )


if __name__ == "__main__":
    unittest.main()
