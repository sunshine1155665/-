"""Forgetting-curve ranking: recency is a signal, never a deletion rule.

2026-11-18: archived rows stay searchable with a penalty; an exact hit gets a
recency floor so an old-but-right memory cannot be pushed out by a fresh-but-
irrelevant one.  Compaction also protects memories that are actually recalled.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fatfish.memory import MemoryService
from tests.test_owner import make_env


class MemoryTimeWeightingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        self.memory = MemoryService(self.db, self.config)

    def _add(self, text: str, *, days_ago: float = 0, **kwargs):
        item = self.memory.add_item(text, **kwargs)
        observed = time.time() - days_ago * 86400.0
        self.db.execute(
            "UPDATE memory_items SET observed_at=? WHERE id=?",
            (observed, item.id),
        )
        return item

    def test_archived_exact_match_is_rescued(self) -> None:
        old = self._add(
            "主人哥哥的生日是 3 月 14 日",
            days_ago=730,
            type="relationship",
            importance=0.9,
            source_type="test",
            source_id="old-birthday",
        )
        self.db.execute(
            "UPDATE memory_items SET status='archived', tier=4 WHERE id=?",
            (old.id,),
        )
        hits = self.memory.search("生日", limit=5)
        self.assertIn(old.id, [hit.item.id for hit in hits])

    def test_recent_non_exact_beats_archived_noise(self) -> None:
        archived = self._add(
            "今天天气不错",
            days_ago=400,
            importance=0.2,
            source_type="test",
            source_id="old-weather",
        )
        self.db.execute(
            "UPDATE memory_items SET status='archived', tier=4 WHERE id=?",
            (archived.id,),
        )
        active = self._add(
            "冰美式是主人哥哥常喝的",
            days_ago=1,
            importance=0.5,
            source_type="test",
            source_id="new-coffee",
        )
        hits = self.memory.search("冰美式", limit=5)
        ids = [hit.item.id for hit in hits]
        self.assertIn(active.id, ids)
        if archived.id in ids:
            self.assertLess(ids.index(active.id), ids.index(archived.id))

    def test_explicit_time_window_does_not_apply_recency_to_itself(self) -> None:
        old = self._add(
            "2024 年夏天看过海边烟花",
            days_ago=700,
            type="episodic",
            importance=0.5,
            source_type="test",
            source_id="old-fireworks",
        )
        hits = self.memory.search(
            "海边烟花", limit=5, time_start=time.time() - 800 * 86400
        )
        self.assertIn(old.id, [hit.item.id for hit in hits])

    def test_compaction_protects_repeatedly_accessed_memories(self) -> None:
        item = self._add(
            "主人哥哥喜欢喝冰美式",
            days_ago=900,
            type="preference",
            importance=0.5,
            source_type="test",
            source_id="often-accessed",
        )
        self.db.execute(
            "UPDATE memory_items SET access_count=?, last_accessed_at=? WHERE id=?",
            (5, time.time(), item.id),
        )
        stats = self.memory.compact(dry_run=True)
        self.assertGreaterEqual(stats["protected"], 1)
        row = self.db.query_one(
            "SELECT status FROM memory_items WHERE id=?", (item.id,)
        )
        self.assertEqual(row["status"], "active")


if __name__ == "__main__":
    unittest.main()
