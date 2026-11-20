"""Offline tests for scripts/memory_bundle.py (T-0065).

The bundle tool moves *memory* between two fatfish databases (local v13 -> the
cloud copy) without dragging machine-local state along.  These tests pin the
properties the migration depends on:

* export -> import round-trips every whitelisted table and nothing else;
* a second import is a no-op (PRIMARY KEY + ``INSERT OR IGNORE``);
* a bundle whose embedding width does not match the target is refused
  *before* anything is written (a half-imported database is worse than none);
* ``--dry-run`` reports the same numbers without touching the target;
* the FTS mirrors are rebuilt after import, so a Chinese query hits.

Every database here lives in a temp directory: the real ``data/fatfish.db`` is
never opened.
"""

from __future__ import annotations

from contextlib import closing

import json
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.db import Database  # noqa: E402
from fatfish.lexicon.service import LexiconService  # noqa: E402
from scripts.memory_bundle import (  # noqa: E402
    EXPORT_TABLES,
    BundleError,
    export_bundle,
    import_bundle,
    verify_bundle,
)

DIMS = 256


def _embedding(value: float = 0.5, dims: int = DIMS) -> bytes:
    return struct.pack(f"{dims}f", *([value] * dims))


class BundleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.source = self.dir / "source.db"
        self.target = self.dir / "target.db"
        self.bundle = self.dir / "bundle.db"

    # -- fixtures ---------------------------------------------------------
    def _init(self, path: Path) -> None:
        db = Database(path)
        db.initialize()
        db.close()

    def _seed(self, path: Path, *, dims: int = DIMS, prefix: str = "") -> Database:
        db = Database(path)
        db.initialize()
        # lexicon_entries is created lazily by the service, not by initialize()
        LexiconService(db, None)
        now = 1_700_000_000.0
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO conversation_events(id, session_id, start_at, status,"
                " title, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (f"{prefix}ev1", "qq:private:1", now, "closed", "第一次对话", now, now),
            )
            conn.execute(
                "INSERT INTO episodes(id, title, start_at, end_at, summary,"
                " memory_ids_json, created_at, session_id, event_ids_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{prefix}ep1", "深夜聊天", now, now + 60, "聊了工作和猫", "[]",
                 now, "qq:private:1", json.dumps([f"{prefix}ev1"])),
            )
            # conversation_turns.id is INTEGER PRIMARY KEY (rowid alias)
            turn = conn.execute(
                "INSERT INTO conversation_turns(session_id, role, content,"
                " created_at, event_id, episode_id) VALUES (?,?,?,?,?,?)",
                ("qq:private:1", "user", "我养了一只猫叫豆豆", now,
                 f"{prefix}ev1", f"{prefix}ep1"),
            )
            turn_id = int(turn.lastrowid)
            conn.execute(
                "INSERT INTO memory_items(id, type, scope, session_id, gist, summary,"
                " observed_at, created_at, updated_at, embedding, event_id, episode_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{prefix}m1", "fact", "private", "qq:private:1", "主人养了一只猫叫豆豆",
                 "宠物信息", now, now, now, _embedding(0.25, dims),
                 f"{prefix}ev1", f"{prefix}ep1"),
            )
            conn.execute(
                "INSERT INTO event_details(id, event_id, episode_id, session_id, seq,"
                " kind, text, detail_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{prefix}d1", f"{prefix}ev1", f"{prefix}ep1", "qq:private:1", 1,
                 "fact", "他说猫叫豆豆", "hash-1", now),
            )
            conn.execute(
                "INSERT INTO rag_chunks(id, source_type, source_id, chunk_index, text,"
                " embedding, hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"{prefix}r1", "memory", f"{prefix}m1", 0, "主人养了一只猫叫豆豆",
                 _embedding(0.75, dims), "rag-hash-1", now),
            )
            conn.execute(
                "INSERT INTO lexicon_entries(id, kind, content, meaning, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (f"{prefix}l1", "slang", "豆豆", "猫的名字", "approved", now, now),
            )
            # entity layer (T-0071): 人物卡 must be able to reach the cloud
            for eid, name in (
                (f"{prefix}person:mom", "妈妈"),
                (f"{prefix}person:t1", "杨老师"),
                (f"{prefix}person:t2", "藤老师"),
            ):
                conn.execute(
                    "INSERT INTO entities(id, kind, canonical_name, aliases_json,"
                    " attrs_json, first_seen_at, last_seen_at, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (eid, "person", name, "[]", "{}", now, now, now),
                )
            conn.execute(
                "INSERT INTO entity_edges(src_id, dst_id, relation, confidence,"
                " created_at) VALUES (?,?,?,?,?)",
                (f"{prefix}person:mom", f"{prefix}person:t1", "colleague", 0.9, now),
            )
            for idx, (eid, surface) in enumerate(
                (
                    (f"{prefix}person:mom", "我妈"),
                    (f"{prefix}person:t1", "杨老师"),
                    (f"{prefix}person:t2", "藤老师"),
                ),
                start=1,
            ):
                conn.execute(
                    "INSERT INTO entity_mentions(entity_id, source_type, source_id,"
                    " surface, turn_id, char_start, char_end, confidence, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (eid, "turn", str(turn_id), surface, turn_id,
                     idx, idx + 2, 0.9, now),
                )
            conn.execute(
                "INSERT INTO outbox(idempotency_key, kind, payload_json, status,"
                " created_at, available_at, attempts, max_attempts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"{prefix}ob1", "proactive_message", "{}", "pending", now, now, 0, 5),
            )
        return db

    # -- export -----------------------------------------------------------
    def test_export_writes_whitelist_only(self) -> None:
        db = self._seed(self.source)
        db.close()

        report = export_bundle(self.source, self.bundle)

        self.assertTrue(self.bundle.exists())
        self.assertEqual(report["tables"]["memory_items"], 1)
        with closing(sqlite3.connect(self.bundle)) as con:
            names = {
                r[0]
                for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue(set(EXPORT_TABLES) <= names)
            # machine-local state must never travel with the bundle
            for excluded in ("outbox", "wakeups", "turn_traces", "owner_bindings",
                             "photo_jobs", "sing_jobs", "qzone_posts"):
                self.assertNotIn(excluded, names)
            meta = dict(con.execute("SELECT key, value FROM bundle_meta").fetchall())
        self.assertEqual(meta["format"], "fatfish-memory-bundle/1")
        self.assertEqual(int(meta["embedding_dims"]), DIMS)
        self.assertEqual(json.loads(meta["table_counts"])["conversation_turns"], 1)

    def test_export_refuses_missing_source(self) -> None:
        with self.assertRaises(BundleError):
            export_bundle(self.dir / "nope.db", self.bundle)

    # -- import -----------------------------------------------------------
    def test_roundtrip_imports_every_table(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)

        target = Database(self.target)
        target.initialize()
        target.close()

        report = import_bundle(self.bundle, self.target)

        self.assertEqual(report["inserted"]["memory_items"], 1)
        self.assertEqual(report["inserted"]["event_details"], 1)
        with closing(sqlite3.connect(self.target)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM memory_items").fetchone()[0], 1)
            gist = con.execute("SELECT gist FROM memory_items").fetchone()[0]
            self.assertEqual(gist, "主人养了一只猫叫豆豆")
            # triggers must not have duplicated FTS rows
            self.assertEqual(
                con.execute("SELECT count(*) FROM memory_fts").fetchone()[0], 1
            )

    def test_entity_cards_reach_the_target(self) -> None:
        """人物卡（妈妈 / 杨老师 / 藤老师）必须跟着记忆一起搬到云端（T-0071）."""
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)

        report = import_bundle(self.bundle, self.target)

        self.assertEqual(report["inserted"]["entities"], 3)
        self.assertEqual(report["inserted"]["entity_edges"], 1)
        self.assertEqual(report["inserted"]["entity_mentions"], 3)
        with closing(sqlite3.connect(self.target)) as con:
            names = {
                r[0] for r in con.execute("SELECT canonical_name FROM entities")
            }
            self.assertEqual(names, {"妈妈", "杨老师", "藤老师"})
            # 云端要能按名字反查出场次数，而不只是留下一串 uin
            hits = con.execute(
                "SELECT e.canonical_name, count(*) FROM entities e"
                " JOIN entity_mentions m ON m.entity_id = e.id"
                " WHERE e.canonical_name = '妈妈' GROUP BY e.id"
            ).fetchone()
            self.assertEqual(hits[1], 1)

        self.assertTrue(verify_bundle(self.bundle, self.target)["ok"])

    def test_entity_mentions_survive_a_second_import(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        import_bundle(self.bundle, self.target)

        second = import_bundle(self.bundle, self.target)

        self.assertEqual(second["inserted"]["entities"], 0)
        self.assertEqual(second["inserted"]["entity_mentions"], 0)
        self.assertEqual(second["skipped"]["entity_mentions"], 3)

    def test_second_import_is_idempotent(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)

        first = import_bundle(self.bundle, self.target)
        second = import_bundle(self.bundle, self.target)

        self.assertEqual(first["inserted"]["memory_items"], 1)
        self.assertEqual(second["inserted"]["memory_items"], 0)
        self.assertEqual(second["skipped"]["memory_items"], 1)
        with closing(sqlite3.connect(self.target)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM memory_items").fetchone()[0], 1)

    def test_dimension_mismatch_aborts_before_writing(self) -> None:
        # bundle carries 256-d embeddings, target already holds 128-d ones
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._seed(self.target, dims=128, prefix="other-").close()
        with closing(sqlite3.connect(self.target)) as con:
            before = con.execute("SELECT count(*) FROM memory_items").fetchone()[0]

        with self.assertRaises(BundleError) as ctx:
            import_bundle(self.bundle, self.target)
        self.assertIn("维度", str(ctx.exception))

        with closing(sqlite3.connect(self.target)) as con:
            self.assertEqual(
                con.execute("SELECT count(*) FROM memory_items").fetchone()[0], before
            )
            self.assertEqual(con.execute("SELECT count(*) FROM episodes").fetchone()[0], 1)

    def test_dry_run_does_not_touch_the_target(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        before_bytes = self.target.stat().st_size

        report = import_bundle(self.bundle, self.target, dry_run=True)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["would_insert"]["memory_items"], 1)
        self.assertEqual(report["inserted"], {})
        self.assertEqual(self.target.stat().st_size, before_bytes)
        with closing(sqlite3.connect(self.target)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM memory_items").fetchone()[0], 0)

    def test_dry_run_reports_conflicts_on_a_second_pass(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        import_bundle(self.bundle, self.target)

        report = import_bundle(self.bundle, self.target, dry_run=True)

        self.assertEqual(report["would_insert"]["memory_items"], 0)
        self.assertEqual(report["would_skip"]["memory_items"], 1)

    def test_pk_collision_aborts_unless_accepted(self) -> None:
        # same turn id, different text: INSERT OR IGNORE would silently drop it
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        with closing(sqlite3.connect(self.target)) as con:
            con.execute(
                "INSERT INTO conversation_turns(id, session_id, role, content,"
                " created_at) VALUES (1,'qq:private:1','user','别的内容',1)"
            )
            con.commit()

        with self.assertRaises(BundleError) as ctx:
            import_bundle(self.bundle, self.target)
        self.assertIn("冲突", str(ctx.exception))

        report = import_bundle(self.bundle, self.target, accept_collisions=True)
        self.assertEqual(report["collided"]["conversation_turns"], 1)
        self.assertEqual(report["inserted"]["memory_items"], 1)

    # -- FTS --------------------------------------------------------------
    def test_fts_is_rebuilt_and_a_chinese_query_hits(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        import_bundle(self.bundle, self.target)

        with closing(sqlite3.connect(self.target)) as con:
            # unicode61 keeps a whole CJK run as one token: query the run itself
            hits = con.execute(
                "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
                ("主人养了一只猫叫豆豆",),
            ).fetchone()[0]
            rag_hits = con.execute(
                "SELECT count(*) FROM rag_fts WHERE rag_fts MATCH ?",
                ("主人养了一只猫叫豆豆",),
            ).fetchone()[0]
            detail_hits = con.execute(
                "SELECT count(*) FROM detail_fts WHERE detail_fts MATCH ?",
                ("他说猫叫豆豆",),
            ).fetchone()[0]
        self.assertGreaterEqual(hits, 1)
        self.assertGreaterEqual(rag_hits, 1)
        self.assertGreaterEqual(detail_hits, 1)

    # -- verify -----------------------------------------------------------
    def test_verify_passes_after_import(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        import_bundle(self.bundle, self.target)

        report = verify_bundle(self.bundle, self.target)

        self.assertTrue(report["ok"], report)
        self.assertTrue(report["fts"]["memory_fts"]["hit"])
        self.assertTrue(all(v == 0 for v in report["orphans"].values()))

    def test_verify_flags_an_orphan_entity_mention(self) -> None:
        """A target that lost a 人物卡 must be reported, not silently accepted."""
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)
        import_bundle(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as con:
            # foreign_keys is OFF on a raw connection: this simulates a card
            # that vanished while its mentions stayed behind
            con.execute("DELETE FROM entities WHERE canonical_name = '妈妈'")
            con.commit()

        report = verify_bundle(self.bundle, self.target)

        self.assertFalse(report["ok"])
        self.assertGreater(
            report["orphans"]["entity_mentions.entity_id->entities"], 0, report
        )

    def test_verify_flags_a_missing_table(self) -> None:
        self._seed(self.source).close()
        export_bundle(self.source, self.bundle)
        self._init(self.target)

        report = verify_bundle(self.bundle, self.target)

        self.assertFalse(report["ok"])
        # a fresh Database has no lexicon_entries (LexiconService creates it
        # lazily), so a missing table is exactly the case worth flagging
        self.assertIn("lexicon_entries", report["mismatched"])
        self.assertIn("memory_items", report["mismatched"])


if __name__ == "__main__":
    unittest.main()
