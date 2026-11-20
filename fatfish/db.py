"""SQLite connection and schema management."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
# Bump this whenever schema.sql or migrate() changes so ``ensure_schema`` knows
# it has real work to do.  It must match the value written by ``migrate``.
SCHEMA_VERSION = "17"

_SCHEMA_SQL_CACHE: str | None = None


def _statement_from_schema(name: str) -> str:
    """One CREATE TABLE/INDEX statement out of schema.sql -- single DDL source.

    ``migrate()`` rebuilds an old database's missing tables with the exact
    statement a fresh database executes, so the DDL cannot drift between the
    two paths. Raises instead of guessing when schema.sql has no such object.
    """
    global _SCHEMA_SQL_CACHE
    if _SCHEMA_SQL_CACHE is None:
        _SCHEMA_SQL_CACHE = SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"^(CREATE (?:TABLE|INDEX) IF NOT EXISTS {re.escape(name)}\b.*?;)",
        _SCHEMA_SQL_CACHE,
        re.DOTALL | re.MULTILINE,
    )
    if match is None:
        raise RuntimeError(f"schema.sql has no CREATE statement for {name}")
    return match.group(1)


# ``PRAGMA integrity_check`` reads every page in the database (tens of
# milliseconds on a 50 MB file, seconds as it grows).  The health panel is
# polled by the UI, so running it on every request was pure overhead.  The
# result is keyed by path because the web opens a fresh connection per request.
_INTEGRITY_CACHE: dict[str, tuple[float, str]] = {}
_INTEGRITY_CACHE_TTL = 300.0
_INTEGRITY_LOCK = threading.Lock()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-65536")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    def initialize(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self.migrate()
        self.conn.commit()

    def ensure_schema(self) -> bool:
        """Run the full schema script only when the stored version is stale.

        The web server opens a fresh connection per request; calling
        ``initialize`` there re-executed ~100 statements (and several
        ``PRAGMA table_info`` probes) on every single HTTP call.  That is not
        just slow, it also creates a large amount of transient garbage.  A
        version marker in ``schema_meta`` turns the common case into one tiny
        indexed lookup.  Returns ``True`` when the schema was (re)initialised.
        """
        try:
            row = self.query_one(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )
        except sqlite3.Error:
            # Brand-new or damaged database: let initialize() create/repair it.
            row = None
        if row is not None and str(row["value"]) == SCHEMA_VERSION:
            return False
        self.initialize()
        return True

    def columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(self, table: str, column: str, definition: str) -> bool:
        if column in self.columns(table):
            return False
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True

    # Column backfills added since the first release, in application order:
    # (table, column, definition). Deliberately NOT version-gated: every step
    # checks before it writes, so re-running migrate() on every initialize()
    # doubles as self-healing for damaged databases (a version gate would skip
    # recreating a table an accident dropped).
    _COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        ("memory_items", "trust_level", "TEXT NOT NULL DEFAULT 'internal'"),
        ("memory_items", "origin_scope", "TEXT NOT NULL DEFAULT 'self'"),
        # Attribution: who a memory belongs to. ``session_id`` alone cannot
        # answer it -- 姐姐 may report a fact about 主人哥哥.
        ("memory_items", "subject", "TEXT NOT NULL DEFAULT ''"),
        ("memory_items", "about_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("open_loops", "session_id", "TEXT"),
        ("open_loops", "subject", "TEXT NOT NULL DEFAULT ''"),
        ("outbox", "max_attempts", "INTEGER NOT NULL DEFAULT 5"),
        ("outbox", "lease_until", "REAL"),
        # Schema v3: conversation events, episode links and archived turn markers.
        ("memory_items", "event_id", "TEXT"),
        ("memory_items", "episode_id", "TEXT"),
        ("conversation_turns", "event_id", "TEXT"),
        ("conversation_turns", "episode_id", "TEXT"),
        ("conversation_turns", "archived_at", "REAL"),
        ("episodes", "session_id", "TEXT"),
        ("episodes", "event_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("episodes", "summary_voice", "TEXT NOT NULL DEFAULT ''"),
        ("episodes", "turn_count", "INTEGER NOT NULL DEFAULT 0"),
        ("episodes", "updated_at", "REAL"),
        # Schema v13: per-turn sender attribution.  A group session is shared
        # by every member, so the session id cannot answer "who said this".
        ("conversation_turns", "sender_id", "TEXT"),
        ("conversation_turns", "sender_name", "TEXT NOT NULL DEFAULT ''"),
        # Schema v14 (ADR-0014): the roam trip a turn belongs to.  Summary
        # generation reads only its own trip's rows; ordinary turns stay NULL.
        ("conversation_turns", "trip_id", "TEXT"),
        # T-0073 memory isolation: who may recall a row across sessions.
        # 'owner' = owner sessions only (fail-closed default, incl. 作息-class
        # content); 'open' = owner-origin non-confidential facts that any
        # session may be fed.  Other people's rows never leave their own
        # session regardless of this flag.
        ("memory_items", "audience", "TEXT NOT NULL DEFAULT 'owner'"),
    )

    # Tables whole features added after the first release; DDL comes straight
    # out of schema.sql (see _statement_from_schema) plus their indexes.
    _TABLE_MIGRATIONS: tuple[str, ...] = (
        "photo_jobs",
        "sticker_jobs",
        "qzone_feed_items",
        "qzone_posts",
        "sing_jobs",
        "ai_chat_messages",
        "self_action_evidence",
        # ADR-0014: thread cards + the roam trip ledger (v14).
        "session_threads",
        "roam_trips",
    )
    # Columns for migrated tables: must run AFTER the table exists (a pre-v8
    # database has no qzone_posts to ALTER).
    _POST_TABLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
        # Schema v9 (share-desire model, T-0023): the persona's own 0-1
        # self-rating at draft time, and who let the row through. Columns
        # only -- the v8-shaped table is never rebuilt.
        ("qzone_posts", "desire", "REAL"),
        ("qzone_posts", "decided_by", "TEXT"),
        # Schema v15: async photo jobs (ModelScope submit -> tick poll ->
        # download -> adapter image outbox).  Old synchronous rows keep the
        # 'local' provider default and never enter the polling query.
        ("photo_jobs", "provider", "TEXT NOT NULL DEFAULT 'local'"),
        ("photo_jobs", "task_id", "TEXT"),
        ("photo_jobs", "request_text", "TEXT NOT NULL DEFAULT ''"),
        ("photo_jobs", "updated_at", "REAL"),
        # Schema v17: the inbound message id the async photo answers.  Kept
        # across ticks so the finished image can be sent as a passive reply
        # (official QQ bot) instead of an easy-to-reject proactive message.
        ("photo_jobs", "reply_to", "TEXT NOT NULL DEFAULT ''"),
    )
    _INDEX_MIGRATIONS: tuple[str, ...] = (
        "idx_photo_jobs_created",
        "idx_sticker_jobs_created",
        "idx_qzone_feed_seen",
        "idx_qzone_feed_ingested",
        "idx_qzone_posts_status",
        "idx_qzone_posts_created",
        "idx_sing_jobs_created",
        "idx_ai_chat_peer_created",
        "idx_self_action_evidence_pending",
        "idx_threads_status",
        "idx_trips_thread_status",
        "idx_trips_home_status",
    )

    def migrate(self) -> dict[str, Any]:
        """Lightweight, idempotent migrations for existing databases.

        Safe to run any number of times on any vintage: every column step
        checks ``PRAGMA table_info`` first, every table/index step is guarded
        by sqlite_master / ``IF NOT EXISTS``. The applied list is what a
        health report or morning script can diff.
        """
        applied: list[str] = []
        for table, column, definition in self._COLUMN_MIGRATIONS:
            if self._ensure_column(table, column, definition):
                applied.append(f"{table}.{column}")
        for table in self._TABLE_MIGRATIONS:
            if self.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ) is None:
                self.conn.execute(_statement_from_schema(table))
                applied.append(table)
        for table, column, definition in self._POST_TABLE_COLUMNS:
            if self._ensure_column(table, column, definition):
                applied.append(f"{table}.{column}")
        for index in self._INDEX_MIGRATIONS:
            self.conn.execute(_statement_from_schema(index))

        # Backfill only rows that predate the column; safe to re-run.
        self.conn.execute(
            "UPDATE episodes SET updated_at=created_at WHERE updated_at IS NULL"
        )
        # Indexes on columns added above cannot live in schema.sql: the script
        # runs before migrations on old databases.  v13's sender index needs
        # the sender_id column, so it is created here, after the ALTERs.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turn_session_sender "
            "ON conversation_turns(session_id, sender_id, id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turn_event ON conversation_turns(event_id)"
        )
        # v14's trip index sits on a column added above, same reason.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turn_trip ON conversation_turns(trip_id, id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_event ON memory_items(event_id)"
        )
        self.set_meta("schema_version", SCHEMA_VERSION)
        return {"applied": applied, "schema_version": SCHEMA_VERSION}

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM schema_meta WHERE key=?", (key,))
        return row["value"] if row else default

    def health_check(self, *, full: bool = False) -> dict[str, Any]:
        """Return the health snapshot, reusing a recent integrity result.

        ``full=True`` forces a fresh ``PRAGMA integrity_check`` (for example a
        manual "check now" button); the default path is what the polled health
        endpoint uses.
        """
        integrity = self._integrity_result(force=full)
        fts = self.query_one(
            "SELECT sqlite_compileoption_used('ENABLE_FTS5') AS enabled"
        )
        return {
            "path": str(self.path),
            "sqlite_version": sqlite3.sqlite_version,
            "integrity": integrity,
            "fts5": bool(fts and fts["enabled"]),
            "schema_version": self.get_meta("schema_version", "0"),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def _integrity_result(self, *, force: bool = False) -> str:
        key = str(self.path.resolve())
        now = time.monotonic()
        if not force:
            with _INTEGRITY_LOCK:
                cached = _INTEGRITY_CACHE.get(key)
            if cached is not None and now - cached[0] < _INTEGRITY_CACHE_TTL:
                return cached[1]
        row = self.query_one("PRAGMA integrity_check")
        result = str(row[0]) if row else "unknown"
        with _INTEGRITY_LOCK:
            if result == "ok":
                _INTEGRITY_CACHE[key] = (time.monotonic(), result)
            else:
                # Never keep serving a stale "ok" after a failed fresh check.
                _INTEGRITY_CACHE.pop(key, None)
        return result

    def backup_to(self, target: str | Path) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        return target

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
