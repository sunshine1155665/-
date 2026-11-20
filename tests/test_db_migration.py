"""T-0030: migration invariants for Database.migrate().

migrate() is deliberately re-run-every-time idempotent rather than
version-gated: a damaged database (a table dropped by an accident) heals on
the next initialize(). These tests pin the two properties that must survive
any refactor of the migration list:

* a fresh initialize() leaves nothing for a second migrate() to apply;
* a pre-v8 database (no photo_jobs / qzone tables) upgrades in place to the
  exact schema.sql shape, DDL single-sourced from schema.sql.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fatfish.db import SCHEMA_VERSION, Database, _statement_from_schema


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "migrate.db"
        self.addCleanup(self._tmp.cleanup)

    def _fresh(self) -> Database:
        db = Database(self.path)
        self.addCleanup(db.close)
        db.initialize()
        return db

    def test_fresh_initialize_is_idempotent(self) -> None:
        db = self._fresh()
        result = db.migrate()
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(db.get_meta("schema_version"), SCHEMA_VERSION)

    def test_pre_v8_database_upgrades_in_place(self) -> None:
        db = self._fresh()
        # simulate a v7 database: feature tables gone, marker rolled back
        for table in ("photo_jobs", "qzone_feed_items", "qzone_posts"):
            db.execute(f"DROP TABLE {table}")
        db.set_meta("schema_version", "7")

        result = db.migrate()

        self.assertEqual(
            sorted(item for item in result["applied"] if "." not in item),
            ["photo_jobs", "qzone_feed_items", "qzone_posts"],
        )
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        for table in ("photo_jobs", "qzone_feed_items", "qzone_posts"):
            row = db.query_one(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            self.assertIsNotNone(row, table)
        # the v9 columns ride along on the recreated table
        self.assertIn("desire", db.columns("qzone_posts"))
        self.assertIn("decided_by", db.columns("qzone_posts"))
        # and a second pass settles back to "nothing to do"
        self.assertEqual(db.migrate()["applied"], [])

    def test_recreated_tables_match_schema_sql(self) -> None:
        db = self._fresh()
        db.execute("DROP TABLE qzone_posts")
        db.migrate()
        # same column set a fresh database gets -- the DDL is single-sourced
        fresh = Database(self.path.with_name("fresh.db"))
        self.addCleanup(fresh.close)
        fresh.initialize()
        self.assertEqual(
            db.columns("qzone_posts"), fresh.columns("qzone_posts")
        )

    def test_photo_jobs_reply_to_backfilled_on_v16(self) -> None:
        # v17 keeps the inbound msg_id on the async job row.  A database that
        # was created before the column must gain it on the next migrate().
        db = self._fresh()
        db.execute("DROP TABLE photo_jobs")
        db.execute(
            "CREATE TABLE photo_jobs ("
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '', "
            "created_at REAL NOT NULL, finished_at REAL, "
            "status TEXT NOT NULL DEFAULT 'running', "
            "provider TEXT NOT NULL DEFAULT 'local', task_id TEXT, "
            "request_text TEXT NOT NULL DEFAULT '', updated_at REAL, "
            "positive TEXT NOT NULL, negative TEXT NOT NULL DEFAULT '', "
            "caption TEXT NOT NULL DEFAULT '', prompt_source TEXT NOT NULL DEFAULT '', "
            "image_path TEXT, error TEXT, "
            "comfyui_started INTEGER NOT NULL DEFAULT 0)"
        )
        db.set_meta("schema_version", "16")
        result = db.migrate()
        self.assertIn("photo_jobs.reply_to", result["applied"])
        self.assertIn("reply_to", db.columns("photo_jobs"))

    def test_statement_extraction_executes(self) -> None:
        # every name migrate() relies on must resolve to runnable SQL
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        for name in (
            "photo_jobs",
            "sticker_jobs",
            "qzone_feed_items",
            "qzone_posts",
            "ai_chat_messages",
            "idx_photo_jobs_created",
            "idx_sticker_jobs_created",
            "idx_qzone_feed_seen",
            "idx_qzone_feed_ingested",
            "idx_qzone_posts_status",
            "idx_qzone_posts_created",
            "idx_ai_chat_peer_created",
        ):
            sql = _statement_from_schema(name)
            self.assertTrue(sql.rstrip().endswith(";"), name)
            conn.execute(sql)
            if name.startswith("idx_"):
                continue
            # re-executing is a no-op (IF NOT EXISTS)
            conn.execute(sql)

    def test_statement_extraction_refuses_unknown_names(self) -> None:
        with self.assertRaises(RuntimeError):
            _statement_from_schema("no_such_table")


if __name__ == "__main__":
    unittest.main()
