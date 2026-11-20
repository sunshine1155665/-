from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.db import Database
from fatfish.memory.entities import (
    FRIEND_ENTITY_ID,
    OWNER_ENTITY_ID,
    SELF_ENTITY_ID,
    archive_session_entities,
    backfill_entities,
    entities_for_query,
    ensure_known_entities,
    extract_from_turns,
    record_mention,
    session_scope,
    upsert_entity,
)


def make_db(tmp: Path) -> Database:
    db = Database(tmp / "entities.db")
    db.initialize()
    return db


class EntityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-entities-")
        self.tmp = Path(self._tmp.name)
        self.db = make_db(self.tmp)
        self.now = time.time()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ fixtures
    def _add_turn(
        self,
        content: str,
        *,
        role: str = "user",
        session_id: str = "s1",
        event_id: str | None = None,
        episode_id: str | None = None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at, "
            "event_id, episode_id) VALUES (?,?,?,?,?,?)",
            (session_id, role, content, self.now, event_id, episode_id),
        )
        return int(cursor.lastrowid)

    def _add_event(self, event_id: str, turn_ids: list[int], *, session_id: str = "s1") -> None:
        self.db.execute(
            "INSERT INTO conversation_events(id, session_id, start_at, end_at, status, "
            "title, summary, turn_ids_json, memory_ids_json, topics_json, importance, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, session_id, self.now, self.now, "archived", "被骂那晚",
             "主人被骂后很累。", json.dumps(turn_ids), "[]", "[]", 0.7,
             self.now, self.now),
        )

    def _add_memory_item(
        self,
        item_id: str,
        *,
        source_type: str,
        source_id: str,
        session_id: str = "s1",
        event_id: str | None = None,
        episode_id: str | None = None,
        entities_json: str = "[]",
    ) -> None:
        self.db.execute(
            "INSERT INTO memory_items(id, type, scope, session_id, event_id, episode_id, "
            "gist, summary, observed_at, importance, confidence, status, trust_level, "
            "origin_scope, source_type, source_id, entities_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, "episodic", "global", session_id, event_id, episode_id,
             "gist-" + item_id, "summary", self.now, 0.4, 0.7, "active", "internal",
             "self", source_type, source_id, entities_json, self.now, self.now),
        )

    def _session(self, session_id: str = "s1", *, event_ids: list[str] | None = None) -> SimpleNamespace:
        groups = event_ids or ["ev1"]
        return SimpleNamespace(
            session_id=session_id,
            date_text="2026-10-24",
            episode_id=f"ep:2026-10-24:{session_id}",
            event_groups=[
                SimpleNamespace(
                    event_ids=[group_id],
                    primary_event_id=group_id,
                    source_id=group_id,
                )
                for group_id in groups
            ],
        )

    def _mention_count(self, **filters: str) -> int:
        where = " AND ".join(f"{key}=?" for key in filters)
        row = self.db.query_one(
            f"SELECT count(*) AS n FROM entity_mentions WHERE {where}",
            tuple(filters.values()),
        )
        return int(row["n"])

    # ------------------------------------------------------------------ extract
    def test_extract_from_turns_covers_known_surfaces(self) -> None:
        t1 = self._add_turn("主人哥哥昨天说什么了", role="user")
        t2 = self._add_turn("姐姐也在吗", role="user")
        t3 = self._add_turn("我在呢", role="assistant")
        t4 = self._add_turn("我回家了", role="user")
        t5 = self._add_turn("鲸鲸一直都在", role="assistant")
        turns = [
            {"id": t1, "role": "user", "content": "主人哥哥昨天说什么了", "created_at": self.now},
            {"id": t2, "role": "user", "content": "姐姐也在吗", "created_at": self.now},
            {"id": t3, "role": "assistant", "content": "我在呢", "created_at": self.now},
            {"id": t4, "role": "user", "content": "我回家了", "created_at": self.now},
            {"id": t5, "role": "assistant", "content": "鲸鲸一直都在", "created_at": self.now},
        ]
        names = extract_from_turns(self.db, turns, "event", "ev1", now=self.now)
        self.assertEqual(names, ["主人哥哥", "姐姐", "鲸鲸"])

        rows = self.db.query(
            "SELECT entity_id, surface, turn_id, char_start, char_end "
            "FROM entity_mentions ORDER BY id"
        )
        got = {(r["entity_id"], r["surface"], int(r["turn_id"])) for r in rows}
        self.assertEqual(got, {
            (OWNER_ENTITY_ID, "主人哥哥", t1),
            (FRIEND_ENTITY_ID, "姐姐", t2),
            (SELF_ENTITY_ID, "我", t3),
            (OWNER_ENTITY_ID, "我", t4),
            (SELF_ENTITY_ID, "鲸鲸", t5),
        })
        by_surface = {(r["entity_id"], r["surface"]): r for r in rows}
        self.assertEqual((by_surface[(OWNER_ENTITY_ID, "主人哥哥")]["char_start"],
                          by_surface[(OWNER_ENTITY_ID, "主人哥哥")]["char_end"]), (0, 4))
        self.assertEqual((by_surface[(FRIEND_ENTITY_ID, "姐姐")]["char_start"],
                          by_surface[(FRIEND_ENTITY_ID, "姐姐")]["char_end"]), (0, 2))
        self.assertEqual((by_surface[(SELF_ENTITY_ID, "鲸鲸")]["char_start"],
                          by_surface[(SELF_ENTITY_ID, "鲸鲸")]["char_end"]), (0, 2))
        self.assertEqual((by_surface[(SELF_ENTITY_ID, "我")]["char_start"],
                          by_surface[(SELF_ENTITY_ID, "我")]["char_end"]), (0, 1))
        # "主人哥哥" must not produce a second, overlapping "主人" mention.
        self.assertEqual(self._mention_count(**{"entity_id": OWNER_ENTITY_ID, "surface": "主人"}), 0)
        self.assertEqual(self._mention_count(**{"entity_id": OWNER_ENTITY_ID, "surface": "主人哥哥"}), 1)

    # ------------------------------------------------------------------ scope
    def test_session_scope_classification(self) -> None:
        self.assertEqual(session_scope("qq:group:123"), "group")
        self.assertEqual(session_scope("QQ:GROUP:9"), "group")
        self.assertEqual(session_scope("irc:#group-lab"), "group")
        self.assertEqual(session_scope("qq:private:456"), "direct")
        self.assertEqual(session_scope("web:devcheck"), "direct")
        self.assertEqual(session_scope("s1"), "direct")
        self.assertEqual(session_scope(""), "direct")

    def test_quote_guard_skips_first_person_inside_quotes(self) -> None:
        t1 = self._add_turn("主人说“我累了”，我说没事呢", role="assistant")
        t2 = self._add_turn("「主人哥哥最好了」", role="assistant")
        t3 = self._add_turn("她写道「我很好」")
        turns = [
            {"id": t1, "role": "assistant", "content": "主人说“我累了”，我说没事呢", "created_at": self.now},
            {"id": t2, "role": "assistant", "content": "「主人哥哥最好了」", "created_at": self.now},
            {"id": t3, "role": "user", "content": "她写道「我很好」", "created_at": self.now},
        ]
        names = extract_from_turns(self.db, turns, "event", "evq", now=self.now)
        self.assertEqual(names, ["主人哥哥", "鲸鲸"])

        rows = self.db.query(
            "SELECT entity_id, surface, turn_id, char_start FROM entity_mentions ORDER BY id"
        )
        got = {(r["entity_id"], r["surface"], int(r["turn_id"])) for r in rows}
        # The quoted 我 (char 4) is skipped for every role - quoted words are
        # someone else's voice; the assistant's own 我 outside the quote
        # (char 9) stays self; quoted 主人哥哥 still attributes to owner.
        self.assertEqual(got, {
            (OWNER_ENTITY_ID, "主人", t1),
            (SELF_ENTITY_ID, "我", t1),
            (OWNER_ENTITY_ID, "主人哥哥", t2),
        })
        kept = next(r for r in rows if r["surface"] == "我")
        self.assertEqual(kept["char_start"], 9)

    def test_prefix_guard_blocks_diminutive_hits(self) -> None:
        t1 = self._add_turn("小姐姐今天好吗")  # 姐姐 after 小: guarded
        t2 = self._add_turn("大哥哥说得对")  # 哥哥 after 大: guarded
        t3 = self._add_turn("姐姐在吗")  # bare 姐姐 at char 0: hits
        t4 = self._add_turn("老主人别生气")  # 主人 after 老: guarded
        # 主人哥哥 as a whole is exempt from the prefix guard: the
        # longest-surface rule treats it as one appellation, and dropping the
        # strongest owner signal after a diminutive char would cost more than
        # a rare false mention.
        t5 = self._add_turn("小主人哥哥辛苦了")
        turns = [
            {"id": tid, "role": "user", "content": content, "created_at": self.now}
            for tid, content in (
                (t1, "小姐姐今天好吗"),
                (t2, "大哥哥说得对"),
                (t3, "姐姐在吗"),
                (t4, "老主人别生气"),
                (t5, "小主人哥哥辛苦了"),
            )
        ]
        names = extract_from_turns(self.db, turns, "event", "evb", now=self.now)
        self.assertEqual(names, ["主人哥哥", "姐姐"])

        rows = self.db.query("SELECT surface, entity_id, turn_id FROM entity_mentions")
        got = {(r["surface"], int(r["turn_id"])) for r in rows}
        self.assertEqual(got, {("姐姐", t3), ("主人哥哥", t5)})
        by_turn = {int(r["turn_id"]): r["entity_id"] for r in rows}
        self.assertEqual(by_turn[t3], FRIEND_ENTITY_ID)
        self.assertEqual(by_turn[t5], OWNER_ENTITY_ID)

    # ------------------------------------------------------------------ mentions
    def test_record_mention_is_idempotent(self) -> None:
        ensure_known_entities(self.db, now=self.now)
        turn_id = self._add_turn("主人哥哥在吗")
        first = record_mention(
            self.db, OWNER_ENTITY_ID, "event", "ev1", "主人哥哥",
            turn_id=turn_id, char_start=0, char_end=4, now=self.now,
        )
        second = record_mention(
            self.db, OWNER_ENTITY_ID, "event", "ev1", "主人哥哥",
            turn_id=turn_id, char_start=0, char_end=4, now=self.now,
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self._mention_count(source_type="event", source_id="ev1"), 1)

        # turn_id=None needs an explicit existence check (NULLs skip the index).
        self.assertTrue(record_mention(self.db, OWNER_ENTITY_ID, "event", "ev2", "主人", now=self.now))
        self.assertFalse(record_mention(self.db, OWNER_ENTITY_ID, "event", "ev2", "主人", now=self.now))
        self.assertEqual(self._mention_count(source_type="event", source_id="ev2"), 1)

    # ------------------------------------------------------------------ upsert
    def test_upsert_entity_merges_aliases(self) -> None:
        self.assertTrue(
            upsert_entity(self.db, "person:alice", "person", "小爱", ["爱酱", "爱酱"], now=1000.0)
        )
        self.assertFalse(
            upsert_entity(self.db, "person:alice", "person", "小爱", ["爱酱", "Alice"], now=2000.0)
        )
        row = self.db.query_one(
            "SELECT canonical_name, aliases_json, first_seen_at, last_seen_at "
            "FROM entities WHERE id='person:alice'"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["canonical_name"], "小爱")
        self.assertEqual(set(json.loads(row["aliases_json"])), {"爱酱", "Alice"})
        self.assertEqual(row["first_seen_at"], 1000.0)
        self.assertEqual(row["last_seen_at"], 2000.0)

    def test_upsert_entity_keeps_old_name_as_alias_on_rename(self) -> None:
        upsert_entity(self.db, "person:alice", "person", "小爱", ["Alice"], now=1000.0)
        self.assertFalse(upsert_entity(self.db, "person:alice", "person", "爱酱", None, now=2000.0))
        row = self.db.query_one(
            "SELECT canonical_name, aliases_json FROM entities WHERE id='person:alice'"
        )
        self.assertEqual(row["canonical_name"], "爱酱")
        self.assertEqual(set(json.loads(row["aliases_json"])), {"小爱", "Alice"})
        hits = entities_for_query(self.db, "小爱今天好吗")
        self.assertEqual([hit["id"] for hit in hits], ["person:alice"])

    # ------------------------------------------------------------------ query
    def test_entities_for_query_matches_substrings(self) -> None:
        ensure_known_entities(self.db, now=self.now)
        upsert_entity(self.db, "person:alice", "person", "小爱", ["爱酱"], now=self.now)

        hits = entities_for_query(self.db, "主人哥哥昨天说什么了")
        self.assertEqual([hit["id"] for hit in hits], [OWNER_ENTITY_ID])
        self.assertEqual(hits[0]["kind"], "person")
        self.assertEqual(hits[0]["canonical_name"], "主人哥哥")

        hits = entities_for_query(self.db, "鲸鲸今年多大了")
        self.assertEqual([hit["id"] for hit in hits], [SELF_ENTITY_ID])

        hits = entities_for_query(self.db, "爱酱今天好吗")
        self.assertEqual([hit["id"] for hit in hits], ["person:alice"])

        self.assertEqual(entities_for_query(self.db, "今天天气不错"), [])
        self.assertEqual(entities_for_query(self.db, ""), [])

    # ------------------------------------------------------------------ archive
    def test_archive_session_entities_is_idempotent(self) -> None:
        t1 = self._add_turn("主人哥哥今天好累。")
        t2 = self._add_turn("鲸鲸给主人哥哥捶捶背。", role="assistant")
        self._add_event("ev1", [t1, t2])
        self._add_memory_item(
            "mem-ev1", source_type="event_archive", source_id="ev1",
            event_id="ev1", episode_id="ep:2026-10-24:s1",
        )
        session = self._session("s1")

        first = archive_session_entities(self.db, session, now=self.now)
        self.assertEqual(first["skipped"], "")
        self.assertEqual(first["mentions"], 6)  # 3 per pass x (event + episode)
        self.assertEqual(first["entities"], 3)  # owner / friend / self
        self.assertEqual(first["items_updated"], 1)

        self.assertGreater(
            self._mention_count(source_type="event", source_id="ev1"), 0
        )
        self.assertGreater(
            self._mention_count(
                source_type="episode", source_id="ep:2026-10-24:s1"
            ),
            0,
        )
        item = self.db.query_one(
            "SELECT entities_json FROM memory_items WHERE id='mem-ev1'"
        )
        self.assertEqual(
            set(json.loads(item["entities_json"])), {"主人哥哥", "鲸鲸"}
        )

        second = archive_session_entities(self.db, session, now=self.now)
        self.assertEqual(second["mentions"], 0)
        self.assertEqual(second["entities"], 0)
        self.assertEqual(second["items_updated"], 0)

    def test_archive_skips_test_session(self) -> None:
        session = self._session("qq:private:verify-x")
        result = archive_session_entities(self.db, session, now=self.now)
        self.assertEqual(
            result,
            {"skipped": "test_session", "mentions": 0, "entities": 0, "items_updated": 0},
        )
        row = self.db.query_one("SELECT count(*) AS n FROM entity_mentions")
        self.assertEqual(row["n"], 0)
        row = self.db.query_one("SELECT count(*) AS n FROM entities")
        self.assertEqual(row["n"], 0)

    def test_group_scope_lowers_confidence_and_skips_merge(self) -> None:
        group_id = "qq:group:123"
        texts = [
            ("user", "哥哥好"),
            ("user", "姐姐也在吗"),
            ("user", "我来了"),
            ("assistant", "我在呢"),
            ("assistant", "鲸鲸一直都在"),
        ]
        g_turns = [self._add_turn(text, role=role, session_id=group_id) for role, text in texts]
        self._add_event("evg", g_turns, session_id=group_id)
        self._add_memory_item(
            "mem-evg", source_type="event_archive", source_id="evg",
            session_id=group_id, event_id="evg",
            episode_id=f"ep:2026-10-24:{group_id}",
        )

        result = archive_session_entities(
            self.db, self._session(group_id, event_ids=["evg"]), now=self.now
        )
        self.assertEqual(result["skipped"], "")
        self.assertEqual(result["entities"], 3)  # owner/friend/self still created
        self.assertEqual(result["mentions"], 10)  # 5 per pass x (event + episode)
        self.assertEqual(result["items_updated"], 0)  # no attribution claimed in group

        for source_type in ("event", "episode"):
            rows = self.db.query(
                "SELECT entity_id, surface, confidence FROM entity_mentions "
                f"WHERE source_type='{source_type}'"
            )
            conf = {(r["entity_id"], r["surface"]): r["confidence"] for r in rows}
            self.assertEqual(len(rows), 5)
            # owner/friend hits in a group are weak evidence (members call
            # each other by the owner's nicknames) -> 0.4; the bot's own
            # surfaces stay 0.8.
            self.assertEqual(conf[(OWNER_ENTITY_ID, "哥哥")], 0.4)
            self.assertEqual(conf[(FRIEND_ENTITY_ID, "姐姐")], 0.4)
            self.assertEqual(conf[(OWNER_ENTITY_ID, "我")], 0.4)  # user 我 in group
            self.assertEqual(conf[(SELF_ENTITY_ID, "我")], 0.8)  # assistant 我 = bot
            self.assertEqual(conf[(SELF_ENTITY_ID, "鲸鲸")], 0.8)

        item = self.db.query_one("SELECT entities_json FROM memory_items WHERE id='mem-evg'")
        self.assertEqual(json.loads(item["entities_json"]), [])

        second = archive_session_entities(
            self.db, self._session(group_id, event_ids=["evg"]), now=self.now
        )
        self.assertEqual(second["mentions"], 0)
        self.assertEqual(second["items_updated"], 0)

        # Same input in a direct session keeps confidence 0.8 and merges.
        d_turns = [self._add_turn(text, role=role, session_id="s1") for role, text in texts]
        self._add_event("evd", d_turns)
        self._add_memory_item(
            "mem-evd", source_type="event_archive", source_id="evd",
            event_id="evd", episode_id="ep:2026-10-24:s1",
        )
        direct = archive_session_entities(
            self.db, self._session("s1", event_ids=["evd"]), now=self.now
        )
        self.assertEqual(direct["mentions"], 10)
        self.assertEqual(direct["entities"], 0)  # already created by the group pass
        self.assertEqual(direct["items_updated"], 1)
        rows = self.db.query(
            "SELECT entity_id, surface, confidence FROM entity_mentions "
            "WHERE source_type='event' AND source_id='evd'"
        )
        self.assertEqual(
            {(r["entity_id"], r["surface"]): r["confidence"] for r in rows},
            {
                (OWNER_ENTITY_ID, "哥哥"): 0.8,
                (FRIEND_ENTITY_ID, "姐姐"): 0.8,
                (OWNER_ENTITY_ID, "我"): 0.8,
                (SELF_ENTITY_ID, "我"): 0.8,
                (SELF_ENTITY_ID, "鲸鲸"): 0.8,
            },
        )
        item = self.db.query_one("SELECT entities_json FROM memory_items WHERE id='mem-evd'")
        self.assertEqual(set(json.loads(item["entities_json"])), {"主人哥哥", "姐姐", "鲸鲸"})

    # ------------------------------------------------------------------ backfill
    def test_backfill_entities_is_idempotent(self) -> None:
        t1 = self._add_turn(
            "主人哥哥和姐姐去看电影。", event_id="ev9", episode_id="ep:2026-10-23:s1"
        )
        t2 = self._add_turn(
            "鲸鲸也想看。", role="assistant", event_id="ev9", episode_id="ep:2026-10-23:s1"
        )
        self._add_event("ev9", [t1, t2])
        self._add_memory_item(
            "mem-ev9", source_type="event_archive", source_id="ev9",
            event_id="ev9", episode_id="ep:2026-10-23:s1",
        )
        self._add_memory_item(
            "mem-test", source_type="episode", source_id="ep:2026-10-23:s2",
            session_id="web:devcheck", episode_id="ep:2026-10-23:s2",
        )

        first = backfill_entities(self.db, now=self.now)
        self.assertEqual(first["items"], 1)  # the devcheck item is skipped, not scanned
        self.assertEqual(first["skipped_test"], 1)
        self.assertEqual(first["mentions"], 6)  # 3 surfaces x (memory + event)
        self.assertEqual(first["items_updated"], 1)

        self.assertGreater(
            self._mention_count(source_type="memory", source_id="mem-ev9"), 0
        )
        self.assertGreater(
            self._mention_count(source_type="event", source_id="ev9"), 0
        )
        item = self.db.query_one(
            "SELECT entities_json FROM memory_items WHERE id='mem-ev9'"
        )
        self.assertEqual(
            set(json.loads(item["entities_json"])), {"主人哥哥", "姐姐", "鲸鲸"}
        )

        second = backfill_entities(self.db, now=self.now)
        self.assertEqual(second["mentions"], 0)
        self.assertEqual(second["items_updated"], 0)


if __name__ == "__main__":
    unittest.main()
