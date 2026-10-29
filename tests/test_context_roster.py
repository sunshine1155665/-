"""Context-side person-card wiring (T-0060): roster block, persona hint, sender labels.

Everything is offline and temporary: no network, no real ``data/fatfish.db``.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.context import ContextAssembler
from fatfish.memory import MemoryService
from fatfish.memory.entities import (
    FRIEND_ENTITY_ID,
    OWNER_ENTITY_ID,
    SELF_ENTITY_ID,
    create_person,
    ensure_known_entities,
    record_mention,
    roster_for_prompt,
)

OWNER_SESSION = "qq:private:10001"
GROUP_SESSION = "qq:group:777"

STRANGER_TEXT = (
    "【当前对话者】不是主人。不要叫主人/主人哥哥，不要透露主人的私事；"
    "保持礼貌和边界。"
)


class ContextRosterTestCase(unittest.TestCase):
    """Shared scaffold: fresh db + assembler per test."""

    def setUp(self) -> None:
        from fatfish.owner import OwnerRegistry
        from tests.test_owner import make_env

        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-roster-")
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        # Creates the owner_bindings table (binds nothing by itself).
        self.owner = OwnerRegistry(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)
        self.now = time.time()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _person(
        self,
        name: str,
        *,
        uin: str = "",
        relation: str | None = None,
        remark: str | None = None,
        aliases: list[str] | None = None,
        mentions: int = 0,
        now: float | None = None,
    ) -> str:
        result = create_person(
            self.db,
            name=name,
            uin=uin,
            aliases=aliases,
            relation=relation,
            remark=remark,
            now=self.now if now is None else float(now),
        )
        entity_id = str((result["entity"] or {})["id"])
        for index in range(int(mentions)):
            record_mention(
                self.db,
                entity_id,
                "session",
                f"session-{name}-{index}",
                name,
                now=self.now,
            )
        return entity_id

    def _roster_names(self, query: str = "") -> list[str]:
        return [entry["name"] for entry in roster_for_prompt(self.db, query)]


class RosterForPromptTests(ContextRosterTestCase):
    def test_query_hit_is_selected(self) -> None:
        self._person(
            "妈妈",
            relation="母亲",
            remark="主人哥哥的妈妈（家人）",
            aliases=["老妈", "我妈"],
            mentions=3,
        )
        entries = roster_for_prompt(self.db, "妈妈今天说什么了")
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("妈妈", entry["name"])
        self.assertEqual("母亲", entry["relation"])
        self.assertEqual("主人哥哥的妈妈（家人）", entry["remark"])
        self.assertEqual({"我妈", "老妈"}, set(entry["aliases"]))
        # Prompt-safe fields only: no id, no uin, nothing else.
        self.assertEqual(
            {"name", "relation", "remark", "aliases"},
            set(entry),
        )

    def test_fallback_is_mentions_first_and_deterministic(self) -> None:
        # Created scrambled on purpose; ranking must not follow creation order.
        self._person("丁一", uin="20004", mentions=1)
        self._person("丁二", uin="20005", mentions=1)
        self._person("戊一", uin="20006", mentions=5)
        self._person("戊二", uin="20007", mentions=5, now=self.now + 100)
        self._person("阿丙", uin="20003", mentions=9)

        names = self._roster_names()
        # mentions desc; ties break on last_seen desc (戊二 newer than 戊一),
        # then id asc (person:qq:20004 < person:qq:20005).
        self.assertEqual(["阿丙", "戊二", "戊一", "丁一", "丁二"], names)
        self.assertEqual(names, self._roster_names())

    def test_known_three_are_excluded(self) -> None:
        ensure_known_entities(self.db, now=self.now)
        for entity_id, surface in (
            (OWNER_ENTITY_ID, "主人哥哥"),
            (FRIEND_ENTITY_ID, "姐姐"),
            (SELF_ENTITY_ID, "鲸鲸"),
        ):
            record_mention(
                self.db, entity_id, "session", f"s-{entity_id}", surface, now=self.now
            )
        self._person("阿丙", uin="20003", mentions=2)

        by_query = self._roster_names("主人哥哥和姐姐还有鲸鲸在吗")
        self.assertEqual(["阿丙"], by_query)
        self.assertEqual(["阿丙"], self._roster_names())

    def test_zero_mention_card_stays_out_of_the_fallback(self) -> None:
        self._person("路人甲")
        self.assertEqual([], self._roster_names())
        # An explicit query hit still reaches the card: the user named them.
        self.assertEqual(["路人甲"], self._roster_names("路人甲是谁"))

    def test_bound_family_zero_mentions_enters_fallback(self) -> None:
        self._person("妈妈", uin="30001", relation="母亲", mentions=0)
        self.owner.bind("qq:private:30001", "妈妈", role="family")
        self.assertIn("妈妈", self._roster_names())

    def test_owner_auto_card_is_excluded(self) -> None:
        # An auto-created QQ card for the owner's uin must not show up as a
        # second person alongside the owner slot.
        self._person("仗剑逍遥阅古今", uin="10001", mentions=5)
        self.owner.bind(OWNER_SESSION, "主人哥哥")
        self.assertEqual([], self._roster_names())

    def test_roster_is_capped_at_limit(self) -> None:
        for index in range(1, 13):
            self._person(f"阿{index:02d}", uin=f"300{index:02d}", mentions=1)
        names = self._roster_names()
        self.assertEqual(10, len(names))
        # Deterministic cut: mentions tie -> id asc, so the first ten win.
        self.assertEqual([f"阿{index:02d}" for index in range(1, 11)], names)

    def test_remark_is_clipped_to_sixty_chars(self) -> None:
        self._person("阿长", uin="30099", remark="记" * 80, mentions=1)
        entries = roster_for_prompt(self.db, "")
        self.assertEqual(1, len(entries))
        self.assertEqual(60, len(entries[0]["remark"]))
        self.assertTrue(entries[0]["remark"].endswith("…"))

    def test_entries_carry_no_uin(self) -> None:
        self._person("阿甲", uin="123456789", mentions=2)
        self._person("阿乙", uin="987654321", mentions=1)
        blob = json.dumps(roster_for_prompt(self.db, ""), ensure_ascii=False)
        self.assertNotIn("123456789", blob)
        self.assertNotIn("987654321", blob)
        self.assertNotIn("uin", blob)


class RosterBlockTests(ContextRosterTestCase):
    def _dynamic(self, query_text: str, role: str | None, session: str = OWNER_SESSION) -> str:
        return self.assembler._dynamic_text(time.time(), session, role, query_text=query_text)

    def test_block_renders_with_card(self) -> None:
        self._person(
            "妈妈",
            relation="母亲",
            remark="主人哥哥的妈妈（家人）",
            aliases=["老妈", "我妈"],
            mentions=1,
        )
        dynamic = self._dynamic("妈妈叫我吃饭", "owner")
        self.assertIn("【认识的人】", dynamic)
        self.assertIn("- 妈妈（母亲；别称：", dynamic)
        self.assertIn("主人哥哥的妈妈（家人）", dynamic)

    def test_block_renders_without_optional_segments(self) -> None:
        self._person("阿丙", uin="20003", mentions=1)
        dynamic = self._dynamic("随便聊聊", "owner")
        self.assertIn("【认识的人】\n- 阿丙\n", dynamic + "\n")

    def test_block_absent_on_empty_db(self) -> None:
        self.assertNotIn("【认识的人】", self._dynamic("在吗", "owner"))
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        dynamic = next(s.text for s in bundle.sections if s.name == "runtime_context")
        self.assertNotIn("【认识的人】", dynamic)

    def test_block_absent_for_stranger(self) -> None:
        # A stranger gets no directory of the owner's circle, same as the
        # memory digests: the roster is gated by the same can_see_memory rule.
        self._person("妈妈", relation="母亲", remark="主人的妈妈", mentions=2)
        self.assertNotIn("【认识的人】", self._dynamic("妈妈在吗", None, "qq:private:39999"))

    def test_block_carries_no_uin(self) -> None:
        self._person("小蓝", uin="123456789", relation="同学", mentions=2)
        self._person("阿乙", uin="987654321", mentions=1)
        dynamic = self._dynamic("小蓝在吗", "owner")
        self.assertIn("【认识的人】", dynamic)
        self.assertNotIn("123456789", dynamic)
        self.assertNotIn("987654321", dynamic)
        self.assertNotIn("123****6789", dynamic)


class PersonaHintTests(ContextRosterTestCase):
    def test_card_session_gets_card_hint(self) -> None:
        self._person("小蓝", uin="30003", relation="同学")
        hint = self.assembler._persona_hint("qq:private:30003", None)
        self.assertIn("【当前对话者】小蓝（同学）", hint)
        self.assertIn("不是主人", hint)
        self.assertIn("不要叫主人/主人哥哥", hint)
        self.assertIn("不要透露主人的私事", hint)

    def test_card_without_relation_says_contact(self) -> None:
        self._person("小黑", uin="30004")
        hint = self.assembler._persona_hint("qq:private:30004", None)
        self.assertIn("【当前对话者】小黑（联系人）", hint)

    def test_unknown_session_keeps_stranger_text(self) -> None:
        self.assertEqual(
            STRANGER_TEXT, self.assembler._persona_hint("qq:private:39999", None)
        )
        # A non-QQ session has no uin to look up at all.
        self.assertEqual(STRANGER_TEXT, self.assembler._persona_hint("web:devcheck", None))

    def test_owner_binding_wins_over_card(self) -> None:
        self._person("小明", uin="10001")
        self.db.execute(
            "INSERT INTO owner_bindings(session_id, display_name, bound_at, role) "
            "VALUES (?,?,?,?)",
            (OWNER_SESSION, "主人哥哥", self.now, "owner"),
        )
        resolved = self.assembler._resolve_role(OWNER_SESSION, None, None)
        self.assertEqual("owner", resolved)
        hint = self.assembler._persona_hint(OWNER_SESSION, resolved)
        self.assertIn("已认主的主人哥哥", hint)
        self.assertNotIn("小明", hint)

    def test_bound_uin_with_unmatched_session_string_stays_stranger(self) -> None:
        # The uin matches the owner binding but the session string did not
        # resolve the binding: never render "主人哥哥 ... 不是主人" -- the
        # plain stranger text is the safe fallback.
        self._person("小明", uin="10001")
        self.db.execute(
            "INSERT INTO owner_bindings(session_id, display_name, bound_at, role) "
            "VALUES (?,?,?,?)",
            (OWNER_SESSION, "主人哥哥", self.now, "owner"),
        )
        self.assertEqual(
            STRANGER_TEXT, self.assembler._persona_hint("irc:private:10001", None)
        )

    def test_friend_and_group_texts_are_unchanged(self) -> None:
        self.db.execute(
            "INSERT INTO owner_bindings(session_id, display_name, bound_at, role) "
            "VALUES (?,?,?,?)",
            ("qq:private:20002", "姐姐", self.now, "friend"),
        )
        friend_hint = self.assembler._persona_hint("qq:private:20002", "friend")
        self.assertIn("是主人哥哥亲近的朋友，不是主人", friend_hint)
        group_hint = self.assembler._persona_hint(GROUP_SESSION, None)
        self.assertIn("QQ 群聊，主人在群里", group_hint)


class SenderRenderTests(ContextRosterTestCase):
    def _add(self, content: str, **kwargs) -> int:
        return self.memory.add_turn(GROUP_SESSION, "user", content, **kwargs)

    def test_card_canonical_wins_over_sender_name(self) -> None:
        self._person("妈妈", uin="40001")
        self._add("今天吃什么", sender_id="40001", sender_name="群名片甲")
        msgs = self.assembler.history_messages(GROUP_SESSION)
        self.assertEqual(1, len(msgs))
        self.assertRegex(msgs[0]["content"], r"^\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\] 妈妈：今天吃什么$")

    def test_sender_name_fallback_without_card(self) -> None:
        self._add("今天吃什么", sender_id="49999", sender_name="小明")
        msgs = self.assembler.history_messages(GROUP_SESSION)
        self.assertRegex(msgs[0]["content"], r"^\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\] 小明：今天吃什么$")

    def test_sender_name_only_without_id(self) -> None:
        self._add("我要睡了", sender_name="小红")
        msgs = self.assembler.history_messages(GROUP_SESSION)
        self.assertRegex(msgs[0]["content"], r"^\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\] 小红：我要睡了$")

    def test_no_sender_keeps_plain_prefix(self) -> None:
        self._add("在吗")
        msgs = self.assembler.history_messages(GROUP_SESSION)
        self.assertRegex(msgs[0]["content"], r"^\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\] 在吗$")
        self.assertNotIn("：", msgs[0]["content"].split("] ", 1)[1])

    def test_flattened_transcript_carries_the_label(self) -> None:
        self._person("妈妈", uin="40001")
        self._add("今天吃什么", sender_id="40001", sender_name="群名片甲")
        text = self.assembler._history_text(GROUP_SESSION)
        self.assertIn("妈妈：今天吃什么", text)

    def test_label_that_carries_the_uin_is_dropped(self) -> None:
        self._add("hello", sender_id="123456789", sender_name="123456789")
        self._add("hi", sender_id="987654321", sender_name="987****4321")
        self._add("在吗", sender_id="555666777", sender_name="用户555666777")
        msgs = self.assembler.history_messages(GROUP_SESSION)
        self.assertEqual(3, len(msgs))
        for message, tail in zip(msgs, ("hello", "hi", "在吗")):
            self.assertRegex(message["content"], rf"^\[\d{{1,2}}-\d{{1,2}} \d{{1,2}}:\d{{2}}\] {tail}$")
        blob = json.dumps(msgs, ensure_ascii=False)
        for uin in ("123456789", "987654321", "555666777", "123****6789", "987****4321"):
            self.assertNotIn(uin, blob)

    def test_assembled_prompt_has_no_uin(self) -> None:
        self._person("小蓝", uin="123456789", relation="同学", mentions=1)
        self._add("今天吃什么", sender_id="123456789", sender_name="小蓝")
        self.memory.add_turn(GROUP_SESSION, "assistant", "吃鱼呀")
        bundle = self.assembler.build(GROUP_SESSION, "那我呢", role=None)
        blob = bundle.text() + "\n" + "\n".join(
            m["content"] for m in bundle.model_messages()
        )
        # The speaker prefix is rendered from the card...
        self.assertIn("小蓝：今天吃什么", blob)
        # ...and the account id never is, in any shape.
        self.assertNotIn("123456789", blob)
        self.assertNotIn("123****6789", blob)
        self.assertNotIn("person:qq:123456789", blob)


if __name__ == "__main__":
    unittest.main()
