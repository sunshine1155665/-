from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.db import Database
from fatfish.memory.tags import (
    classify_tag,
    ensure_tag,
    link_tag,
    sync_tags,
    backfill_tags,
)


def make_db(tmp: Path) -> Database:
    db = Database(tmp / "tags.db")
    db.initialize()
    return db


class TagsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-tags-")
        self.tmp = Path(self._tmp.name)
        self.db = make_db(self.tmp)
        self.now = time.time()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def _usage(self, tag_id: int) -> tuple[int, str]:
        row = self.db.query_one(
            "SELECT usage_count, status FROM tags WHERE id = ?", (tag_id,)
        )
        assert row is not None
        return int(row["usage_count"]), str(row["status"])

    def _add_memory(self, item_id: str, tags_json: str) -> None:
        self.db.execute(
            "INSERT INTO memory_items(id, gist, observed_at, tags_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, f"gist {item_id}", self.now, tags_json, self.now, self.now),
        )

    # -- classify_tag ------------------------------------------------------

    def test_classify_time_formats(self) -> None:
        self.assertEqual(classify_tag("2026-10-22"), ("time", "2026-10-22"))
        self.assertEqual(classify_tag("2026-10"), ("time", "2026-10"))
        self.assertEqual(classify_tag("2026"), ("time", "2026"))

    def test_classify_emotion(self) -> None:
        self.assertEqual(classify_tag("开心"), ("emotion", "开心"))

    def test_classify_relation_case_insensitive(self) -> None:
        self.assertEqual(classify_tag("主人"), ("relation", "主人"))
        self.assertEqual(classify_tag("OWNER"), ("relation", "OWNER"))

    def test_classify_topic_fallback_and_truncation(self) -> None:
        self.assertEqual(classify_tag("archive"), ("topic", "archive"))
        self.assertEqual(classify_tag("鲸鲸"), ("topic", "鲸鲸"))
        long_name = "很" * 60
        self.assertEqual(classify_tag(long_name), ("topic", "很" * 48))

    def test_classify_blank_returns_none(self) -> None:
        self.assertIsNone(classify_tag(""))
        self.assertIsNone(classify_tag("   "))

    # -- ensure_tag / link_tag ---------------------------------------------

    def test_ensure_tag_is_idempotent(self) -> None:
        first = ensure_tag(self.db, "topic", "archive", now=self.now)
        second = ensure_tag(self.db, "topic", "archive", now=self.now)
        self.assertEqual(first, second)
        rows = self.db.query("SELECT id FROM tags")
        self.assertEqual(len(rows), 1)

    def test_link_tag_is_idempotent(self) -> None:
        tag_id = ensure_tag(self.db, "topic", "archive", now=self.now)
        self.assertTrue(link_tag(self.db, tag_id, "memory", "m1", now=self.now))
        self.assertFalse(link_tag(self.db, tag_id, "memory", "m1", now=self.now))
        usage, status = self._usage(tag_id)
        self.assertEqual(usage, 1)
        self.assertEqual(status, "candidate")
        links = self.db.query("SELECT * FROM tag_links WHERE tag_id = ?", (tag_id,))
        self.assertEqual(len(links), 1)

    def test_candidate_promotes_after_two_targets(self) -> None:
        recurring = ensure_tag(self.db, "emotion", "开心", now=self.now)
        self.assertTrue(link_tag(self.db, recurring, "memory", "m1", now=self.now))
        usage, status = self._usage(recurring)
        self.assertEqual((usage, status), (1, "candidate"))
        self.assertTrue(link_tag(self.db, recurring, "memory", "m2", now=self.now))
        usage, status = self._usage(recurring)
        self.assertEqual((usage, status), (2, "active"))

        single = ensure_tag(self.db, "topic", "archive", now=self.now)
        self.assertTrue(link_tag(self.db, single, "memory", "m1", now=self.now))
        usage, status = self._usage(single)
        self.assertEqual((usage, status), (1, "candidate"))

    # -- sync_tags ----------------------------------------------------------

    def test_sync_tags_creates_namespaced_rows(self) -> None:
        result = sync_tags(
            self.db, ["archive", "2026-10-22", "开心"], "memory", "m1", now=self.now
        )
        self.assertEqual(result, {"created": 3, "linked": 3})

        namespaces = {
            str(row["name"]): str(row["namespace"])
            for row in self.db.query("SELECT namespace, name FROM tags")
        }
        self.assertEqual(
            namespaces,
            {"archive": "topic", "2026-10-22": "time", "开心": "emotion"},
        )
        links = self.db.query(
            "SELECT target_type, target_id FROM tag_links"
        )
        self.assertEqual(len(links), 3)
        for link in links:
            self.assertEqual(str(link["target_type"]), "memory")
            self.assertEqual(str(link["target_id"]), "m1")

    def test_sync_tags_repeat_is_noop(self) -> None:
        tags = ["archive", "2026-10-22", "开心"]
        sync_tags(self.db, tags, "memory", "m1", now=self.now)
        result = sync_tags(self.db, tags, "memory", "m1", now=self.now)
        self.assertEqual(result, {"created": 0, "linked": 0})

    def test_sync_tags_skips_blank_entries(self) -> None:
        result = sync_tags(self.db, ["", "   ", 42], "memory", "m1", now=self.now)
        self.assertEqual(result, {"created": 0, "linked": 0})
        self.assertEqual(self.db.query("SELECT id FROM tags"), [])

    # -- backfill_tags -------------------------------------------------------

    def test_backfill_tags_links_memory_items(self) -> None:
        self._add_memory("m1", json.dumps(["2026-10-22", "开心"], ensure_ascii=False))
        self._add_memory("m2", "{not valid json")

        first = backfill_tags(self.db, now=self.now)
        self.assertEqual(first, {"items": 2, "tags": 2, "linked": 2})

        tags = self.db.query("SELECT id, namespace, name, status FROM tags ORDER BY id")
        self.assertEqual(
            [(str(r["namespace"]), str(r["name"])) for r in tags],
            [("time", "2026-10-22"), ("emotion", "开心")],
        )
        links = self.db.query(
            "SELECT tag_id, target_type, target_id FROM tag_links ORDER BY tag_id"
        )
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(str(link["target_type"]), "memory")
            self.assertEqual(str(link["target_id"]), "m1")
        self.assertEqual(
            {int(link["tag_id"]) for link in links},
            {int(row["id"]) for row in tags},
        )

        # Rerun: same links already exist, nothing new is counted.
        second = backfill_tags(self.db, now=self.now)
        self.assertEqual(second, {"items": 2, "tags": 2, "linked": 0})

    def test_backfill_tags_skips_deleted_items(self) -> None:
        self._add_memory("m1", json.dumps(["archive"]))
        self.db.execute(
            "UPDATE memory_items SET status = 'deleted' WHERE id = 'm1'"
        )
        result = backfill_tags(self.db, now=self.now)
        self.assertEqual(result, {"items": 0, "tags": 0, "linked": 0})
        self.assertEqual(self.db.query("SELECT tag_id FROM tag_links"), [])


if __name__ == "__main__":
    unittest.main()
