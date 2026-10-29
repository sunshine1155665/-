"""T-0013: QQ Zone authors become entity-layer first-class citizens.

Covers the ingest hook (entity card + mention + tag links), the uin-keyed
alias merge across nickname changes, the scope mapping onto the known people
(self -> owner, bound friend slot -> friend), the idempotent backfill CLI
path, and the mention back-link query that makes "姐姐最近发了什么" an
entity-filtered recall. Offline only: temp databases, no network, no real
data directory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.memory.entities import (
    FRIEND_ENTITY_ID,
    OWNER_ENTITY_ID,
    entities_for_query,
    sync_qzone_author,
)
from fatfish.owner import OwnerRegistry
from fatfish.qzone.collector import QzoneCollector, backfill_qzone_entities

OWNER_UIN = "10001"
FRIEND_UIN = "20002"


def make_env(tmp: Path) -> tuple[Any, Database, MemoryService, QzoneCollector]:
    data = tmp / "data"
    path = tmp / "qzone_entities.toml"
    path.write_text(
        "\n".join(
            [
                f'[app]\ndata_dir = "{data.as_posix()}"',
                'timezone = "Asia/Shanghai"',
                f'[storage]\ndb_path = "{(data / "qzone_entities.db").as_posix()}"',
                "[memory]",
                'embedding_backend = "hash"',
                "embedding_dim = 64",
                "[qzone]",
                "enabled = false",
                f'owner_uin = "{OWNER_UIN}"',
                "[bilibili]",
                "enabled = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    memory = MemoryService(db, config)
    collector = QzoneCollector(memory, config)
    return config, db, memory, collector


def feed_item(external_id: str, uin: str, name: str, content: str = "今天天气不错") -> dict:
    return {
        "external_id": external_id,
        "author_uin": uin,
        "author_name": name,
        "content": content,
        "published_at": 1_790_000_000.0,
    }


class QzoneEntityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, self.memory, self.collector = make_env(self.tmp)
        self.addCleanup(self.db.close)

    def ingest(self, items: list[dict]) -> None:
        stored = self.collector.store_items(items)
        self.collector.ingest_into_memory(stored)

    def entity(self, entity_id: str):
        return self.db.query_one("SELECT * FROM entities WHERE id=?", (entity_id,))

    def mentions(self, source_id: str) -> list:
        return self.db.query(
            "SELECT * FROM entity_mentions WHERE source_type='qzone' AND source_id=?",
            (source_id,),
        )


class IngestHookTests(QzoneEntityTestBase):
    def test_ingest_creates_author_entity_and_mention(self) -> None:
        self.ingest([feed_item("ext-1", "30003", "同事小王")])

        row = self.entity("person:qq:30003")
        self.assertIsNotNone(row)
        self.assertEqual("同事小王", row["canonical_name"])
        self.assertEqual("person", row["kind"])
        mention = self.mentions("ext-1")
        self.assertEqual(1, len(mention))
        self.assertEqual("person:qq:30003", mention[0]["entity_id"])
        self.assertEqual("同事小王", mention[0]["surface"])

    def test_same_uin_nickname_change_merges_alias(self) -> None:
        self.ingest([feed_item("ext-1", "30003", "小王")])
        self.ingest([feed_item("ext-2", "30003", "王老师")])

        rows = self.db.query(
            "SELECT id, canonical_name, aliases_json FROM entities "
            "WHERE id LIKE 'person:qq:%'"
        )
        self.assertEqual(1, len(rows))  # one card, not two
        self.assertEqual("王老师", rows[0]["canonical_name"])
        self.assertIn("小王", json.loads(rows[0]["aliases_json"]))
        # both items are anchored to the same entity
        self.assertEqual(
            {"ext-1", "ext-2"},
            {m["source_id"] for m in self.db.query(
                "SELECT source_id FROM entity_mentions WHERE entity_id='person:qq:30003'"
            )},
        )

    def test_owner_post_maps_to_owner_entity(self) -> None:
        OwnerRegistry(self.db, self.config).bind(f"qq:private:{OWNER_UIN}", "主人哥哥")
        self.ingest([feed_item("ext-self", OWNER_UIN, "鲸鱼饲养员")])

        row = self.entity(OWNER_ENTITY_ID)
        self.assertIsNotNone(row)
        self.assertEqual("主人哥哥", row["canonical_name"])  # established name kept
        self.assertIn("鲸鱼饲养员", json.loads(row["aliases_json"]))
        self.assertEqual(OWNER_ENTITY_ID, self.mentions("ext-self")[0]["entity_id"])

    def test_bound_friend_post_maps_to_friend_entity(self) -> None:
        OwnerRegistry(self.db, self.config).bind(
            f"qq:private:{FRIEND_UIN}", "姐姐", role="friend"
        )
        self.ingest([feed_item("ext-friend", FRIEND_UIN, "姐姐的号")])

        self.assertEqual(FRIEND_ENTITY_ID, self.mentions("ext-friend")[0]["entity_id"])
        row = self.entity(FRIEND_ENTITY_ID)
        self.assertEqual("姐姐", row["canonical_name"])
        self.assertIn("姐姐的号", json.loads(row["aliases_json"]))

    def test_author_without_uin_gets_name_keyed_card(self) -> None:
        self.ingest([feed_item("ext-anon", "", "路过的风")])
        row = self.entity("person:nick:路过的风")
        self.assertIsNotNone(row)
        self.assertEqual("person:nick:路过的风", self.mentions("ext-anon")[0]["entity_id"])

    def test_tags_linked_to_the_memory_item(self) -> None:
        self.ingest([feed_item("ext-tags", "30003", "小王")])
        item = self.db.query_one(
            "SELECT id FROM memory_items WHERE source_type='qzone' AND source_id='ext-tags'"
        )
        self.assertIsNotNone(item)
        linked = self.db.query(
            "SELECT t.namespace, t.name FROM tag_links l JOIN tags t ON t.id=l.tag_id "
            "WHERE l.target_type='memory_item' AND l.target_id=?",
            (str(item["id"]),),
        )
        names = {row["name"] for row in linked}
        self.assertIn("QQ空间", names)
        self.assertIn("动态", names)
        self.assertIn("小王", names)


class BackfillTests(QzoneEntityTestBase):
    def _store_without_ingest(self, items: list[dict]) -> None:
        """Simulate a pre-T-0013 database: feed rows exist, entities do not."""
        self.collector.store_items(items)

    def test_backfill_creates_then_settles(self) -> None:
        self._store_without_ingest(
            [
                feed_item("ext-1", "30003", "小王"),
                feed_item("ext-2", FRIEND_UIN, "姐姐的号"),
                feed_item("ext-3", "30003", "王老师"),
            ]
        )

        first = backfill_qzone_entities(self.db)
        self.assertEqual(3, first["items"])
        self.assertEqual(2, first["entities_created"])  # uin 30003 + friend-slot 20002
        self.assertEqual(3, first["mentions"])

        second = backfill_qzone_entities(self.db)
        self.assertEqual(0, second["entities_created"])
        self.assertEqual(0, second["mentions"])
        # mentions are still exactly one per item
        self.assertEqual(1, len(self.mentions("ext-1")))

    def test_mention_backlink_and_recall_query(self) -> None:
        self._store_without_ingest([feed_item("ext-9", "30003", "小王")])
        backfill_qzone_entities(self.db)

        # back-link: mention row -> entity card
        mention = self.mentions("ext-9")[0]
        card = self.entity(mention["entity_id"])
        self.assertEqual("小王", card["canonical_name"])

        # entity prefilter resolves a natural query to the author card
        hits = entities_for_query(self.db, "小王最近发了什么")
        self.assertIn("person:qq:30003", [hit["id"] for hit in hits])

    def test_owner_and_friend_queries_resolve_to_known_people(self) -> None:
        OwnerRegistry(self.db, self.config).bind(f"qq:private:{OWNER_UIN}", "主人哥哥")
        OwnerRegistry(self.db, self.config).bind(
            f"qq:private:{FRIEND_UIN}", "姐姐", role="friend"
        )
        self._store_without_ingest(
            [
                feed_item("ext-owner", OWNER_UIN, "鲸鱼饲养员"),
                feed_item("ext-friend", FRIEND_UIN, "姐姐的号"),
            ]
        )
        backfill_qzone_entities(self.db)

        hits = entities_for_query(self.db, "姐姐最近发了什么")
        self.assertIn(FRIEND_ENTITY_ID, [hit["id"] for hit in hits])
        hits = entities_for_query(self.db, "主人哥哥最近发了什么")
        self.assertIn(OWNER_ENTITY_ID, [hit["id"] for hit in hits])


if __name__ == "__main__":
    unittest.main()
