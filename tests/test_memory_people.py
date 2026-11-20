"""Person cards: create / alias / merge / binding sync / inbound sender (T-0057)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.config import Config
from fatfish.db import Database
from fatfish.memory.entities import (
    FRIEND_ENTITY_ID,
    OWNER_ENTITY_ID,
    SELF_ENTITY_ID,
    create_person,
    duplicate_candidates,
    ensure_known_entities,
    entity_card,
    entity_family_ids,
    entity_for_uin,
    list_people,
    merge_entities,
    note_inbound_person,
    qzone_author_identity,
    record_mention,
    resolve_entity_id,
    sync_binding_entities,
    update_person,
)
from fatfish.owner import OwnerRegistry


def make_db(tmp: Path) -> Database:
    db = Database(tmp / "people.db")
    db.initialize()
    return db


def make_config(tmp: Path) -> Config:
    data = tmp / "data"
    return Config(
        {
            "app": {"data_dir": str(data), "timezone": "Asia/Shanghai"},
            "storage": {"db_path": str(data / "people.db")},
            "memory": {"embedding_backend": "hash", "embedding_dim": 64},
            "relationship": {
                "owned": False,
                "owner_display_name": "主人哥哥",
                "friend_display_name": "姐姐",
            },
        },
        profile="low",
    )


class PersonCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-people-")
        self.tmp = Path(self._tmp.name)
        self.db = make_db(self.tmp)
        self.now = time.time()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _card(self, entity_id: str) -> dict:
        card = entity_card(self.db, entity_id)
        self.assertIsNotNone(card)
        return card

    def _mention_rows(self, entity_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?",
            (entity_id,),
        )
        return int(row["n"])

    # ------------------------------------------------------------------ create
    def test_create_person_with_uin_is_keyed_by_uin(self) -> None:
        result = create_person(
            self.db,
            name="小明",
            uin="123456789",
            aliases=["阿明"],
            relation="同学",
            remark="实验室的",
        )
        self.assertTrue(result["created"])
        card = result["entity"]
        self.assertEqual("person:qq:123456789", card["id"])
        self.assertEqual("小明", card["name"])
        self.assertEqual("123456789", card["uin"])
        self.assertEqual("同学", card["relation"])
        self.assertEqual("实验室的", card["remark"])
        self.assertIn("阿明", card["aliases"])
        # The uin must never leak into the matchable name fields.
        self.assertNotIn("123456789", card["name"])
        self.assertFalse(any("123456789" in alias for alias in card["aliases"]))

    def test_create_person_without_uin_uses_nick_key(self) -> None:
        card = create_person(self.db, name="小红")["entity"]
        self.assertEqual("person:nick:小红", card["id"])
        self.assertEqual("", card["uin"])

    def test_create_person_is_idempotent(self) -> None:
        first = create_person(self.db, name="小明", uin="123456789")
        second = create_person(self.db, name="小明", uin="123456789")
        self.assertFalse(second["created"])
        self.assertEqual(first["entity"]["id"], second["entity"]["id"])
        rows = self.db.query("SELECT id FROM entities WHERE kind='person'")
        self.assertEqual(1, len(rows))

    def test_create_person_rekeys_exact_name_card_once_uin_is_known(self) -> None:
        create_person(self.db, name="小红")
        record_mention(
            self.db, "person:nick:小红", "session", "qq:group:1", "小红", now=self.now
        )
        result = create_person(self.db, name="小红", uin="987654321")
        self.assertEqual(["person:nick:小红"], result["merged"])
        self.assertEqual("person:qq:987654321", result["entity"]["id"])
        self.assertEqual(
            "person:qq:987654321",
            resolve_entity_id(self.db, "person:nick:小红"),
        )
        # The old card is a tombstone, not a deletion: its mention is still there.
        self.assertEqual(1, self._mention_rows("person:nick:小红"))
        self.assertEqual(1, result["entity"]["mentions"])

    def test_create_person_never_merges_a_different_uin(self) -> None:
        create_person(self.db, name="小明", uin="111")
        result = create_person(self.db, name="小明", uin="222")
        self.assertEqual([], result["merged"])
        self.assertEqual("person:qq:222", result["entity"]["id"])
        self.assertEqual(1, len(result["merge_candidates"]))
        self.assertEqual("person:qq:111", result["merge_candidates"][0]["id"])

    def test_create_person_requires_a_name_or_uin(self) -> None:
        with self.assertRaises(ValueError):
            create_person(self.db, name="", uin="")

    def test_create_person_uin_only_gets_a_placeholder_name(self) -> None:
        card = create_person(self.db, uin="123456789")["entity"]
        self.assertEqual("QQ用户6789", card["name"])
        self.assertEqual("123456789", card["uin"])

    # ------------------------------------------------------------- uin lookup
    def test_entity_for_uin_prefers_the_owner_binding(self) -> None:
        config = make_config(self.tmp)
        owner = OwnerRegistry(self.db, config)
        owner.bind("qq:private:10001", display_name="阿主")
        self.assertEqual(OWNER_ENTITY_ID, entity_for_uin(self.db, "10001"))
        # Creating with that uin must not mint a second person:qq card.
        result = create_person(self.db, name="阿主", uin="10001")
        self.assertEqual(OWNER_ENTITY_ID, result["entity"]["id"])
        self.assertIsNone(
            self.db.query_one("SELECT id FROM entities WHERE id='person:qq:10001'")
        )

    def test_entity_for_uin_prefers_the_friend_binding(self) -> None:
        config = make_config(self.tmp)
        owner = OwnerRegistry(self.db, config)
        owner.bind_friend("qq:private:20002", display_name="小美")
        self.assertEqual(FRIEND_ENTITY_ID, entity_for_uin(self.db, "20002"))

    def test_entity_for_uin_finds_an_attr_keyed_card(self) -> None:
        create_person(self.db, name="小红")
        self.db.execute(
            "UPDATE entities SET attrs_json=? WHERE id='person:nick:小红'",
            (json.dumps({"uin": "555666777"}),),
        )
        self.assertEqual("person:nick:小红", entity_for_uin(self.db, "555666777"))

    def test_entity_for_uin_ignores_non_digits(self) -> None:
        self.assertIsNone(entity_for_uin(self.db, "not-a-uin"))
        self.assertIsNone(entity_for_uin(self.db, ""))

    # -------------------------------------------------------------- updating
    def test_rename_keeps_the_old_name_as_an_alias(self) -> None:
        create_person(self.db, name="小明", uin="123")
        card = update_person(self.db, "person:qq:123", name="明明")["entity"]
        self.assertEqual("明明", card["name"])
        self.assertIn("小明", card["aliases"])

    def test_alias_add_and_remove(self) -> None:
        create_person(self.db, name="小明", uin="123", aliases=["阿明", "小明哥"])
        card = update_person(
            self.db,
            "person:qq:123",
            aliases_add=["明明"],
            aliases_remove=["阿明"],
        )["entity"]
        self.assertIn("明明", card["aliases"])
        self.assertNotIn("阿明", card["aliases"])
        self.assertIn("小明哥", card["aliases"])

    def test_canonical_name_cannot_be_removed_as_an_alias(self) -> None:
        create_person(self.db, name="小明", uin="123")
        card = update_person(
            self.db, "person:qq:123", aliases_remove=["小明"]
        )["entity"]
        self.assertNotIn("小明", card["aliases"])

    def test_relation_and_remark_can_be_cleared(self) -> None:
        create_person(self.db, name="小明", uin="123", relation="同学", remark="实验室")
        card = update_person(self.db, "person:qq:123", relation="", remark="")["entity"]
        self.assertEqual("", card["relation"])
        self.assertEqual("", card["remark"])

    def test_update_unknown_entity_raises(self) -> None:
        with self.assertRaises(ValueError):
            update_person(self.db, "person:qq:404", name="x")

    def test_update_empty_name_raises(self) -> None:
        create_person(self.db, name="小明", uin="123")
        with self.assertRaises(ValueError):
            update_person(self.db, "person:qq:123", name="  ")

    # ------------------------------------------------------------------ merge
    def test_merge_unions_aliases_and_folds_mentions(self) -> None:
        create_person(self.db, name="小明", uin="111", aliases=["阿明"])
        create_person(self.db, name="明明", uin="222", aliases=["小明"])
        record_mention(self.db, "person:qq:111", "session", "s1", "小明")
        record_mention(self.db, "person:qq:222", "session", "s2", "明明")
        before_rows = int(
            self.db.query_one("SELECT COUNT(*) AS n FROM entity_mentions")["n"]
        )
        result = merge_entities(self.db, "person:qq:222", "person:qq:111")
        self.assertTrue(result["merged"])
        card = result["entity"]
        self.assertEqual("person:qq:111", card["id"])
        self.assertIn("明明", card["aliases"])
        self.assertIn("阿明", card["aliases"])
        self.assertEqual(2, card["mentions"])
        # Mentions are evidence: not moved, not deleted.
        after_rows = int(
            self.db.query_one("SELECT COUNT(*) AS n FROM entity_mentions")["n"]
        )
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(1, self._mention_rows("person:qq:222"))
        self.assertEqual(
            "person:qq:111", resolve_entity_id(self.db, "person:qq:222")
        )

    def test_merge_adopts_the_real_name_over_a_placeholder(self) -> None:
        create_person(self.db, uin="111")
        create_person(self.db, name="小明", uin="222")
        card = merge_entities(self.db, "person:qq:222", "person:qq:111")["entity"]
        self.assertEqual("小明", card["name"])
        self.assertIn("QQ用户111", card["aliases"])

    def test_merge_same_entity_is_a_noop(self) -> None:
        create_person(self.db, name="小明", uin="111")
        result = merge_entities(self.db, "person:qq:111", "person:qq:111")
        self.assertFalse(result["merged"])
        self.assertEqual("same_entity", result["reason"])

    def test_merge_refuses_unknown_cards(self) -> None:
        create_person(self.db, name="小明", uin="111")
        with self.assertRaises(ValueError):
            merge_entities(self.db, "person:qq:404", "person:qq:111")
        with self.assertRaises(ValueError):
            merge_entities(self.db, "person:qq:111", "person:qq:404")

    def test_merge_chain_resolves_to_the_last_card(self) -> None:
        for uin in ("111", "222", "333"):
            create_person(self.db, name=f"p{uin}", uin=uin)
        merge_entities(self.db, "person:qq:111", "person:qq:222")
        merge_entities(self.db, "person:qq:222", "person:qq:333")
        self.assertEqual(
            "person:qq:333", resolve_entity_id(self.db, "person:qq:111")
        )
        self.assertEqual(
            ["person:qq:333"], [c["id"] for c in list_people(self.db)]
        )
        self.assertEqual(
            {"person:qq:333", "person:qq:111", "person:qq:222"},
            set(entity_family_ids(self.db, "person:qq:333")),
        )

    def test_merge_refuses_a_pre_existing_cycle(self) -> None:
        create_person(self.db, name="a", uin="111")
        create_person(self.db, name="b", uin="222")
        # Corrupt the store on purpose: b -> a, then try a -> b.
        self.db.execute(
            "UPDATE entities SET merged_into='person:qq:111' WHERE id='person:qq:222'"
        )
        # resolve(222) is now 111, so this looks like a self-merge.
        result = merge_entities(self.db, "person:qq:111", "person:qq:222")
        self.assertFalse(result["merged"])

    # ------------------------------------------------------------------ lists
    def test_list_people_newest_activity_first(self) -> None:
        create_person(self.db, name="旧人", uin="111", now=100.0)
        create_person(self.db, name="新人", uin="222", now=200.0)
        self.assertEqual(["新人", "旧人"], [c["name"] for c in list_people(self.db)])

    def test_list_people_query_by_name_alias_and_uin(self) -> None:
        create_person(self.db, name="小明", uin="123456789", aliases=["阿明"])
        self.assertEqual(1, len(list_people(self.db, query="阿")))
        self.assertEqual(1, len(list_people(self.db, query="123456789")))
        self.assertEqual(0, len(list_people(self.db, query="不存在")))

    def test_list_people_hides_merged_cards(self) -> None:
        create_person(self.db, name="小明", uin="111")
        create_person(self.db, name="明明", uin="222")
        merge_entities(self.db, "person:qq:222", "person:qq:111")
        people = list_people(self.db)
        self.assertEqual(1, len(people))
        self.assertEqual(["person:qq:222"], people[0]["merged_from"])

    def test_list_people_respects_the_limit(self) -> None:
        for index in range(5):
            create_person(self.db, name=f"人{index}", uin=str(100 + index))
        self.assertEqual(2, len(list_people(self.db, limit=2)))

    # ----------------------------------------------------------- duplicates
    def test_duplicate_candidates_report_shared_names(self) -> None:
        create_person(self.db, name="小明", uin="111")
        create_person(self.db, name="小明", uin="222")
        pairs = duplicate_candidates(self.db)
        self.assertEqual(1, len(pairs))
        self.assertEqual("小明", pairs[0]["shared"])
        ids = {pairs[0]["a"]["id"], pairs[0]["b"]["id"]}
        self.assertEqual({"person:qq:111", "person:qq:222"}, ids)

    def test_duplicate_candidates_match_alias_to_name(self) -> None:
        create_person(self.db, name="小明", uin="111", aliases=["明明"])
        create_person(self.db, name="明明", uin="222")
        self.assertEqual(1, len(duplicate_candidates(self.db)))

    def test_duplicate_candidates_ignore_merged_cards(self) -> None:
        create_person(self.db, name="小明", uin="111")
        create_person(self.db, name="小明", uin="222")
        merge_entities(self.db, "person:qq:222", "person:qq:111")
        self.assertEqual([], duplicate_candidates(self.db))

    # -------------------------------------------------------------- bindings
    def test_sync_binding_entities_sets_name_and_uin(self) -> None:
        config = make_config(self.tmp)
        owner = OwnerRegistry(self.db, config)
        owner.bind("qq:private:10001", display_name="阿主")
        owner.bind_friend("qq:private:20002", display_name="小美")
        result = sync_binding_entities(self.db)
        self.assertEqual("10001", result["owner"]["uin"])
        self.assertEqual("20002", result["friend"]["uin"])
        self.assertEqual("阿主", entity_card(self.db, OWNER_ENTITY_ID)["name"])
        self.assertEqual("小美", entity_card(self.db, FRIEND_ENTITY_ID)["name"])
        self.assertEqual("10001", entity_card(self.db, OWNER_ENTITY_ID)["uin"])
        self.assertEqual("20002", entity_card(self.db, FRIEND_ENTITY_ID)["uin"])

    def test_sync_binding_entities_is_idempotent(self) -> None:
        config = make_config(self.tmp)
        OwnerRegistry(self.db, config).bind("qq:private:10001", display_name="阿主")
        sync_binding_entities(self.db)
        sync_binding_entities(self.db)
        rows = self.db.query(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id=?",
            (OWNER_ENTITY_ID,),
        )
        self.assertEqual(0, int(rows[0]["n"]))

    def test_ensure_known_entities_preserves_a_manual_name(self) -> None:
        ensure_known_entities(self.db)
        update_person(self.db, SELF_ENTITY_ID, name="小鲸鱼")
        ensure_known_entities(self.db)
        self.assertEqual("小鲸鱼", entity_card(self.db, SELF_ENTITY_ID)["name"])

    def test_qzone_friend_author_uses_the_bound_friend_name(self) -> None:
        config = make_config(self.tmp)
        owner = OwnerRegistry(self.db, config)
        owner.bind_friend("qq:private:20002", display_name="小美")
        entity_id, kind, canonical = qzone_author_identity(
            self.db, uin="20002", name="页面上随便写的", scope="friend"
        )
        self.assertEqual(FRIEND_ENTITY_ID, entity_id)
        self.assertEqual("person", kind)
        self.assertEqual("小美", canonical)

    # -------------------------------------------------------- inbound sender
    def test_note_inbound_person_creates_a_card_with_a_session_mention(self) -> None:
        result = note_inbound_person(
            self.db,
            session_id="qq:private:123456789",
            uin="123456789",
            name="小明",
            now=self.now,
        )
        self.assertEqual("person:qq:123456789", result["entity_id"])
        self.assertTrue(result["created"])
        self.assertTrue(result["mention"])
        card = self._card("person:qq:123456789")
        self.assertEqual("小明", card["name"])
        self.assertEqual(1, card["mentions"])

    def test_note_inbound_person_is_one_mention_per_session(self) -> None:
        note_inbound_person(
            self.db, session_id="qq:private:123", uin="123", name="小明"
        )
        note_inbound_person(
            self.db, session_id="qq:private:123", uin="123", name="小明"
        )
        self.assertEqual(1, self._mention_rows("person:qq:123"))

    def test_note_inbound_person_adds_a_new_alias(self) -> None:
        note_inbound_person(
            self.db, session_id="qq:private:123", uin="123", name="小明"
        )
        note_inbound_person(
            self.db, session_id="qq:private:123", uin="123", name="阿明"
        )
        card = self._card("person:qq:123")
        self.assertEqual("小明", card["name"])
        self.assertIn("阿明", card["aliases"])

    def test_note_inbound_person_skips_without_name(self) -> None:
        result = note_inbound_person(
            self.db, session_id="qq:private:123", uin="123", name=""
        )
        self.assertEqual("no_name", result["skipped"])
        self.assertIsNone(
            self.db.query_one("SELECT id FROM entities WHERE id='person:qq:123'")
        )

    def test_note_inbound_person_skips_without_uin(self) -> None:
        result = note_inbound_person(
            self.db, session_id="qq:group:1", uin="", name="小明"
        )
        self.assertEqual("no_uin", result["skipped"])

    def test_note_inbound_person_after_the_uin_only_card_exists(self) -> None:
        create_person(self.db, uin="123")
        result = note_inbound_person(
            self.db, session_id="qq:private:123", uin="123", name="小明"
        )
        self.assertFalse(result["created"])
        card = self._card("person:qq:123")
        self.assertEqual("QQ用户123", card["name"])
        self.assertIn("小明", card["aliases"])

    def test_note_inbound_person_uses_the_owner_card_when_bound(self) -> None:
        config = make_config(self.tmp)
        OwnerRegistry(self.db, config).bind("qq:private:10001", display_name="阿主")
        result = note_inbound_person(
            self.db, session_id="qq:private:10001", uin="10001", name="阿主的昵称"
        )
        self.assertEqual(OWNER_ENTITY_ID, result["entity_id"])
        owner = self._card(OWNER_ENTITY_ID)
        self.assertEqual("阿主", owner["name"])
        self.assertIn("阿主的昵称", owner["aliases"])
        self.assertEqual("10001", owner["uin"])
        self.assertEqual("owner", owner["attrs"].get("role"))

    def test_note_inbound_person_never_puts_the_uin_in_matchable_names(self) -> None:
        note_inbound_person(
            self.db, session_id="qq:private:123456789", uin="123456789", name="小明"
        )
        card = self._card("person:qq:123456789")
        self.assertNotIn("123456789", card["name"])
        self.assertFalse(any("123456789" in alias for alias in card["aliases"]))


if __name__ == "__main__":
    unittest.main()
