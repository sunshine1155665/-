"""Nightly retention tests.

Only disposable telemetry/queue rows may be deleted; conversation turns and
memories are explicitly out of scope.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fatfish.nightly import prune_history
from tests.test_owner import make_env


def _count(db, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = db.query_one(sql)
    return int(row["n"]) if row else 0


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.now = time.time()
        self.old = self.now - 400 * 86400

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _seed(self) -> None:
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (self.old, "old", 1, "{}"),
        )
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (self.now, "fresh", 1, "{}"),
        )
        self.db.execute(
            "INSERT INTO decision_log(at, kind) VALUES (?,?)", (self.old, "old")
        )
        self.db.execute(
            "INSERT INTO decision_log(at, kind) VALUES (?,?)", (self.now, "fresh")
        )
        for key, status, processed in (
            ("old-done", "done", self.old),
            ("old-pending", "pending", None),
            ("fresh-done", "done", self.now),
        ):
            self.db.execute(
                "INSERT INTO outbox"
                "(idempotency_key, kind, status, created_at, available_at, processed_at) "
                "VALUES (?,?,?,?,?,?)",
                (key, "test", status, self.old, self.old, processed),
            )
        self.db.execute(
            "INSERT INTO photo_jobs(id, created_at, positive) VALUES (?,?,?)",
            ("old-photo", self.old, "old"),
        )
        self.db.execute(
            "INSERT INTO photo_jobs(id, created_at, positive) VALUES (?,?,?)",
            ("fresh-photo", self.now, "fresh"),
        )
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) "
            "VALUES (?,?,?,?)",
            ("s", "user", "keep me", self.old),
        )

    def test_prune_removes_only_disposable_old_rows(self) -> None:
        self._seed()
        result = prune_history(self.db, self.config, now=self.now)

        self.assertGreaterEqual(result["total"], 4)
        self.assertEqual(1, _count(self.db, "metrics", "key='fresh'"))
        self.assertEqual(0, _count(self.db, "metrics", "key='old'"))
        self.assertEqual(1, _count(self.db, "decision_log", "kind='fresh'"))
        self.assertEqual(0, _count(self.db, "decision_log", "kind='old'"))
        self.assertEqual(1, _count(self.db, "outbox", "idempotency_key='fresh-done'"))
        self.assertEqual(
            1,
            _count(self.db, "outbox", "idempotency_key='old-pending'"),
            "pending queue rows must never be pruned",
        )
        self.assertEqual(0, _count(self.db, "outbox", "idempotency_key='old-done'"))
        self.assertEqual(0, _count(self.db, "photo_jobs", "id='old-photo'"))
        self.assertEqual(1, _count(self.db, "photo_jobs", "id='fresh-photo'"))
        self.assertEqual(
            1,
            _count(self.db, "conversation_turns", "content='keep me'"),
            "conversation data is not retention's business",
        )

    def test_dry_run_counts_without_deleting(self) -> None:
        self._seed()
        result = prune_history(
            self.db, self.config, now=self.now, dry_run=True
        )
        self.assertTrue(result["dry_run"])
        self.assertGreater(result["total"], 0)
        self.assertEqual(1, _count(self.db, "metrics", "key='old'"))

    def test_zero_days_disables_a_single_window(self) -> None:
        self._seed()
        original_get = self.config.get

        def get(key, default=None):
            if key == "nightly.metrics_retention_days":
                return 0
            return original_get(key, default)

        self.config.get = get  # type: ignore[method-assign]
        prune_history(self.db, self.config, now=self.now)
        self.assertEqual(1, _count(self.db, "metrics", "key='old'"))


class HealthCacheTests(unittest.TestCase):
    def test_integrity_check_is_cached_unless_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, db, _ = make_env(Path(tmp))
            try:
                db.health_check(full=True)  # populate the cache deterministically
                calls: list[str] = []
                original = db.query_one

                def wrapped(sql, params=()):
                    if "integrity_check" in sql:
                        calls.append(sql)
                    return original(sql, params)

                with mock.patch.object(db, "query_one", side_effect=wrapped):
                    db.health_check()
                    db.health_check()
                    db.health_check(full=True)
                self.assertEqual(1, len(calls), "only ?full=1 may re-run integrity_check")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
