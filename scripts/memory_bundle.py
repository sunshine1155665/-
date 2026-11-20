#!/usr/bin/env python
"""Move *memory* between two fatfish databases (T-0065).

The online tier keeps its own ``data/`` directory: the cloud copy must receive
the local brain without inheriting a single machine-local row.  This tool
therefore copies a **whitelist** of tables into a self-contained SQLite bundle,
imports it with ``INSERT OR IGNORE`` (so a retried import is a no-op), rebuilds
the FTS mirrors, and verifies the result.

Usage::

    python scripts/memory_bundle.py export --db data/fatfish.db --out /tmp/mem.db
    python scripts/memory_bundle.py import --bundle /tmp/mem.db --db data/fatfish.db --dry-run
    python scripts/memory_bundle.py import --bundle /tmp/mem.db --db data/fatfish.db
    python scripts/memory_bundle.py verify --bundle /tmp/mem.db --db data/fatfish.db

Design notes:

* **One file, no archive logic.** A SQLite bundle scps as one object, keeps
  types/BLOBs intact, and can be inspected with any sqlite3 client.
* **Never exports machine state.** ``outbox``/``wakeups`` hold queued sends for
  *this* machine's accounts; ``turn_traces``/``metrics``/job tables are
  local history.  Importing them into the cloud would replay or duplicate work.
* **人物卡 travels.** ``entities``/``entity_edges``/``entity_mentions`` are
  memory, not machine state: without them the cloud cannot recognise 妈妈 or a
  teacher and falls back to bare uin strings.
* **Embeddings are validated before a single row is written.** A silent 256-d
  vs 128-d mix would poison recall months later; the import aborts instead.
* **The source database is opened read-only** (``mode=ro``); export can never
  modify it.

Zero third-party dependencies, Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

FORMAT = "fatfish-memory-bundle/1"

#: Tables that travel.  Order is import order (parents before children).
EXPORT_TABLES: tuple[str, ...] = (
    "episodes",
    "conversation_events",
    "conversation_turns",
    "memory_items",
    "event_details",
    "rag_chunks",
    "lexicon_entries",
    "persona",
    # entity layer (ADR-0003 / T-0071): 人物卡 —— 主人、妈妈、老师都是"记忆"，
    # 不是机器状态。少了这三张表云端认不出熟人，只能按 uin 硬编码。
    "entities",
    "entity_edges",
    "entity_mentions",
)

#: Tables that never travel, with the reason kept next to them for reviewers.
EXPORT_EXCLUDED: dict[str, str] = {
    "outbox": "machine-local send queue",
    "wakeups": "machine-local timers",
    "turn_traces": "local cost/latency history",
    "metrics": "local counters",
    "photo_jobs": "local GPU jobs",
    "sing_jobs": "local GPU jobs",
    "qzone_posts": "local publish ledger",
    "owner_bindings": "owner identity is re-bound on the target",
}

#: Still *not* travelling, and knowingly so: ``tags``/``tag_links`` (topic
#: tagging) and ``memory_links``/``open_loops`` (associative recall + follow-up
#: threads) are memory too.  They are left out until there is a caller that
#: needs them on the cloud side -- every table added here widens the blast
#: radius of an import, so the whitelist grows one deliberate step at a time.

#: FTS mirrors rebuilt after import: (fts table, select that feeds it).
FTS_MIRRORS: tuple[tuple[str, str], ...] = (
    ("memory_fts", "SELECT id, gist, summary FROM memory_items"),
    ("rag_fts", "SELECT id, text FROM rag_chunks"),
    ("detail_fts", "SELECT id, text, COALESCE(quote_text, '') FROM event_details"),
)

#: (child table, column, parent table) references checked by ``verify``.
#:
#: Only single-target foreign keys can live here: the probe assumes the parent's
#: key column is ``id``.  ``entity_mentions.source_id`` is polymorphic
#: (``source_type`` decides whether it points at a memory item, a turn or an
#: event), so it is deliberately absent.
REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("conversation_turns", "event_id", "conversation_events"),
    ("conversation_turns", "episode_id", "episodes"),
    ("memory_items", "event_id", "conversation_events"),
    ("memory_items", "episode_id", "episodes"),
    ("event_details", "event_id", "conversation_events"),
    ("event_details", "episode_id", "episodes"),
    ("entities", "merged_into", "entities"),
    ("entity_edges", "src_id", "entities"),
    ("entity_edges", "dst_id", "entities"),
    ("entity_mentions", "entity_id", "entities"),
    ("entity_mentions", "turn_id", "conversation_turns"),
)

#: Columns holding a float32 BLOB; used for the dimension gate.
EMBEDDING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("memory_items", "embedding"),
    ("rag_chunks", "embedding"),
)

_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")


class BundleError(RuntimeError):
    """A bundle/DB pair that cannot be used safely."""


# ---------------------------------------------------------------- helpers


def _ro_uri(path: Path) -> str:
    return f"file:{quote(path.resolve().as_posix())}?mode=ro"


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise BundleError(f"数据库不存在：{path}")
    con = sqlite3.connect(_ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


def _connect_rw(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _create_sql(con: sqlite3.Connection, table: str) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return None if row is None else str(row[0])


def _pk_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    ordered = sorted((int(r[5]), str(r[1])) for r in rows if int(r[5]) > 0)
    return [name for _, name in ordered]


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})")]


def _schema_version(con: sqlite3.Connection) -> str | None:
    try:
        row = con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def _embedding_widths(con: sqlite3.Connection, table: str, column: str) -> list[int]:
    try:
        rows = con.execute(
            f"SELECT DISTINCT length({column}) FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    return sorted({int(r[0]) for r in rows if r[0] is not None})


def _width_to_dims(widths: Sequence[int]) -> int | None:
    """float32 BLOBs: byte length / 4.  Mixed widths mean a broken database."""
    if not widths:
        return None
    dims = {w // 4 for w in widths if w % 4 == 0}
    if len(dims) != 1 or len(dims) != len({w for w in widths}):
        raise BundleError(f"embedding 长度不一致：{list(widths)} 字节")
    return dims.pop()


def _counts(con: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    existing = _table_names(con)
    out: dict[str, int] = {}
    for table in tables:
        out[table] = (
            int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            if table in existing
            else 0
        )
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- export


def export_bundle(db_path: str | Path, out_path: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Copy the whitelisted tables of ``db_path`` into a single-file bundle."""
    source = Path(db_path)
    out = Path(out_path)
    if out.exists() and not force:
        raise BundleError(f"bundle 已存在（加 --force 覆盖）：{out}")
    if out.exists():
        out.unlink()

    src = _connect_ro(source)
    version = ""
    try:
        present = _table_names(src)
        version = _schema_version(src) or ""
        missing = [t for t in EXPORT_TABLES if t not in present]
        if len(missing) == len(EXPORT_TABLES):
            raise BundleError("源库里一张白名单表都没有，确认 --db 指向 fatfish 库")

        dims: dict[str, int | None] = {}
        for table, column in EMBEDDING_COLUMNS:
            dims[table] = _width_to_dims(_embedding_widths(src, table, column))
        widths = {d for d in dims.values() if d is not None}
        if len(widths) > 1:
            raise BundleError(f"源库两处 embedding 维度不一致：{dims}")
        dim = widths.pop() if widths else None

        out.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(out)
        try:
            dst.execute("CREATE TABLE bundle_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            counts: dict[str, int] = {}
            for table in EXPORT_TABLES:
                if table in missing:
                    counts[table] = 0
                    continue
                ddl = _create_sql(src, table)
                if ddl is None:
                    raise BundleError(f"源库读不到 {table} 的建表语句")
                dst.execute(ddl)
                columns = _columns(src, table)
                placeholders = ",".join("?" for _ in columns)
                rows = src.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
                dst.executemany(
                    f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                    [tuple(r) for r in rows],
                )
                counts[table] = len(rows)

            meta = {
                "format": FORMAT,
                "created_at": _now_iso(),
                "source_db": source.name,
                "schema_version": _schema_version(src) or "",
                "embedding_dims": "" if dim is None else str(dim),
                "table_counts": json.dumps(counts, ensure_ascii=False, sort_keys=True),
                "skipped_tables": json.dumps(sorted(missing), ensure_ascii=False),
                "excluded_tables": json.dumps(sorted(EXPORT_EXCLUDED), ensure_ascii=False),
            }
            dst.executemany(
                "INSERT INTO bundle_meta(key, value) VALUES (?,?)", sorted(meta.items())
            )
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    size = out.stat().st_size
    return {
        "ok": True,
        "bundle": str(out),
        "bytes": size,
        "schema_version": version,
        "embedding_dims": dim,
        "tables": counts,
    }


# ---------------------------------------------------------------- import


def _bundle_meta(con: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = con.execute("SELECT key, value FROM bundle_meta").fetchall()
    except sqlite3.Error as exc:
        raise BundleError(f"不是 memory bundle（缺 bundle_meta）：{exc}") from exc
    meta = {str(r[0]): str(r[1]) for r in rows}
    if meta.get("format") != FORMAT:
        raise BundleError(f"bundle 格式不支持：{meta.get('format')!r}")
    return meta


def _check_dimensions(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    """Abort unless every embedding in the bundle matches the target's width."""
    bundle_dims: dict[str, int | None] = {}
    for table, column in EMBEDDING_COLUMNS:
        if table in _table_names(src):
            bundle_dims[table] = _width_to_dims(_embedding_widths(src, table, column))
    target_dims: dict[str, int | None] = {}
    for table, column in EMBEDDING_COLUMNS:
        if table in _table_names(dst):
            target_dims[table] = _width_to_dims(_embedding_widths(dst, table, column))

    for table, dim in bundle_dims.items():
        if dim is None:
            continue
        other = target_dims.get(table)
        if other is not None and other != dim:
            raise BundleError(
                f"embedding 维度不一致（{table}）：bundle {dim} 维，目标库 {other} 维；"
                "已中止，未写入任何行"
            )
    return {"bundle": bundle_dims, "target": target_dims}


def _bundle_tables(src: sqlite3.Connection) -> list[str]:
    present = _table_names(src)
    return [t for t in EXPORT_TABLES if t in present]


def _require_fatfish_db(con: sqlite3.Connection, path: Path) -> None:
    if "schema_meta" not in _table_names(con):
        raise BundleError(f"{path} 不像 fatfish 库（没有 schema_meta）")


def _create_missing_tables(src: sqlite3.Connection, dst: sqlite3.Connection) -> list[str]:
    """Create whitelisted tables the target has not grown yet.

    ``lexicon_entries`` is created lazily by ``LexiconService``; a freshly
    migrated target legitimately lacks it.  Copying the bundle's DDL keeps the
    import working without touching the app's own schema management.
    """
    existing = _table_names(dst)
    created: list[str] = []
    for table in _bundle_tables(src):
        if table in existing:
            continue
        ddl = _create_sql(src, table)
        if ddl is None:
            continue
        dst.execute(ddl)
        created.append(table)
    return created


def _plan(
    src: sqlite3.Connection, dst: sqlite3.Connection
) -> dict[str, dict[str, int]]:
    """Count rows that would be inserted / skipped / collide, without writing.

    ``skip`` means "the target already has this exact row" (a retried import).
    ``collide`` means "same primary key, different content": ``INSERT OR
    IGNORE`` would silently drop the incoming row, so the caller must decide.
    """
    plan: dict[str, dict[str, int]] = {"insert": {}, "skip": {}, "collide": {}}
    dst_tables = _table_names(dst)
    for table in _bundle_tables(src):
        pk = _pk_columns(src, table)
        columns = _columns(src, table)
        select = ",".join(columns)
        total = int(src.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        if table not in dst_tables:
            # dry-run against a target that has not grown the table yet
            plan["insert"][table] = total
            plan["skip"][table] = 0
            plan["collide"][table] = 0
            continue
        if not pk:
            raise BundleError(f"{table} 没有主键，无法幂等导入")
        where = " AND ".join(f"{c}=?" for c in pk)
        skipped = 0
        collided = 0
        for row in src.execute(f"SELECT {select} FROM {table}"):
            keys = tuple(row[columns.index(c)] for c in pk)
            found = dst.execute(
                f"SELECT {select} FROM {table} WHERE {where} LIMIT 1", keys
            ).fetchone()
            if found is None:
                continue
            if tuple(found) == tuple(row):
                skipped += 1
            else:
                collided += 1
        plan["insert"][table] = total - skipped - collided
        plan["skip"][table] = skipped
        plan["collide"][table] = collided
    return plan


def import_bundle(
    bundle_path: str | Path,
    db_path: str | Path,
    *,
    dry_run: bool = False,
    accept_collisions: bool = False,
) -> dict[str, Any]:
    """Copy a bundle into ``db_path``; idempotent, and safe to re-run."""
    bundle = Path(bundle_path)
    target = Path(db_path)
    if not target.exists():
        raise BundleError(f"目标库不存在：{target}（先用代码 initialize 建库）")

    src = _connect_ro(bundle)
    try:
        src.row_factory = sqlite3.Row
        meta = _bundle_meta(src)

        dst = _connect_ro(target) if dry_run else _connect_rw(target)
        try:
            _require_fatfish_db(dst, target)
            dims = _check_dimensions(src, dst)
            plan = _plan(src, dst)
            collisions = {t: n for t, n in plan["collide"].items() if n}
            if collisions and not accept_collisions:
                detail = ", ".join(f"{t}={n}" for t, n in collisions.items())
                raise BundleError(
                    f"主键冲突且内容不同：{detail}；INSERT OR IGNORE 会丢掉这些行。"
                    "确认要放弃它们再加 --accept-collisions"
                )
            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "bundle": str(bundle),
                    "schema_version": meta.get("schema_version", ""),
                    "embedding_dims": dims,
                    "would_insert": plan["insert"],
                    "would_skip": plan["skip"],
                    "would_collide": collisions,
                    "inserted": {},
                }

            inserted: dict[str, int] = {}
            created: list[str] = []
            dst.execute("BEGIN IMMEDIATE")
            try:
                created = _create_missing_tables(src, dst)
                for table in _bundle_tables(src):
                    columns = _columns(src, table)
                    placeholders = ",".join("?" for _ in columns)
                    sql = (
                        f"INSERT OR IGNORE INTO {table}({','.join(columns)})"
                        f" VALUES ({placeholders})"
                    )
                    written = 0
                    for row in src.execute(f"SELECT {','.join(columns)} FROM {table}"):
                        cur = dst.execute(sql, tuple(row))
                        written += max(int(cur.rowcount), 0)
                    inserted[table] = written
                fts = _rebuild_fts(dst)
                dst.commit()
            except Exception:
                dst.rollback()
                raise
        finally:
            dst.close()
    finally:
        src.close()

    return {
        "ok": True,
        "dry_run": False,
        "bundle": str(bundle),
        "schema_version": meta.get("schema_version", ""),
        "embedding_dims": dims,
        "inserted": inserted,
        "skipped": plan["skip"],
        "collided": collisions,
        "created_tables": created,
        "fts_rebuilt": fts,
    }


def _rebuild_fts(con: sqlite3.Connection) -> list[str]:
    """Recreate the FTS mirrors from the base tables.

    Import fires the ``*_ai`` triggers, but a target whose triggers were lost
    would silently keep an empty index.  Rebuilding from the base tables is
    cheap at this size and makes the post-import state deterministic.
    """
    existing = _table_names(con)
    rebuilt: list[str] = []
    for table, select in FTS_MIRRORS:
        if table not in existing:
            continue
        con.execute(f"DELETE FROM {table}")
        con.execute(f"INSERT INTO {table} {select}")
        rebuilt.append(table)
    return rebuilt


# ---------------------------------------------------------------- verify


#: Where each FTS mirror's probe token comes from (bundle-side column).
FTS_PROBE_SOURCES: dict[str, tuple[str, str]] = {
    "memory_fts": ("memory_items", "gist"),
    "rag_fts": ("rag_chunks", "text"),
    "detail_fts": ("event_details", "text"),
}


def _probe_token(src: sqlite3.Connection, table: str, column: str) -> str | None:
    """A real CJK token from the bundle, so the FTS probe is not a lucky guess.

    ``unicode61`` treats a whole CJK run as one token, so the token has to be
    the *entire* run — a truncated prefix would never match.
    """
    present = _table_names(src)
    if table not in present:
        return None
    for row in src.execute(
        f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
    ):
        match = _CJK.search(str(row[0]))
        if match:
            return match.group(0)
    return None


def verify_bundle(bundle_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Check that every bundle row landed, FTS answers, references resolve."""
    bundle = Path(bundle_path)
    target = Path(db_path)
    src = _connect_ro(bundle)
    try:
        meta = _bundle_meta(src)
        dst = _connect_ro(target)
        try:
            present = _table_names(dst)
            missing = [t for t in _bundle_tables(src) if t not in present]
            mismatched: dict[str, dict[str, int]] = {}
            bundle_counts = {
                t: int(src.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
                for t in _bundle_tables(src)
            }
            for table in missing:
                mismatched[table] = {"bundle": bundle_counts[table], "target": 0}

            dst.execute(f"ATTACH DATABASE ? AS bundle", (_ro_uri(bundle),))
            for table in _bundle_tables(src):
                if table in missing:
                    continue
                pk = _pk_columns(src, table)
                if not pk:
                    continue
                join = " AND ".join(f"t.{c}=b.{c}" for c in pk)
                bundle_rows = bundle_counts[table]
                landed = int(
                    dst.execute(
                        f"SELECT count(*) FROM main.{table} t"
                        f" JOIN bundle.{table} b ON {join}"
                    ).fetchone()[0]
                )
                if landed != bundle_rows:
                    mismatched[table] = {"bundle": bundle_rows, "target": landed}

            fts: dict[str, Any] = {}
            for table, _ in FTS_MIRRORS:
                if table not in present:
                    continue
                source_table, source_column = FTS_PROBE_SOURCES[table]
                token = _probe_token(src, source_table, source_column)
                if token is None:
                    fts[table] = {"query": "", "hits": 0, "hit": None}
                    continue
                hits = int(
                    dst.execute(
                        f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (token,)
                    ).fetchone()[0]
                )
                fts[table] = {"query": token, "hits": hits, "hit": hits > 0}

            orphans: dict[str, int] = {}
            for child, column, parent in REFERENCES:
                key = f"{child}.{column}->{parent}"
                orphans[key] = int(
                    dst.execute(
                        f"SELECT count(*) FROM main.{child} c"
                        f" JOIN bundle.{child} x ON x.rowid = c.rowid"
                        f" WHERE c.{column} IS NOT NULL AND c.{column} != ''"
                        f" AND NOT EXISTS (SELECT 1 FROM main.{parent} p"
                        f" WHERE p.id = c.{column})"
                    ).fetchone()[0]
                )

            dst.execute("DETACH DATABASE bundle")
        finally:
            dst.close()
    finally:
        src.close()

    fts_ok = all(v["hit"] is not False for v in fts.values()) if fts else True
    orphans_ok = all(v == 0 for v in orphans.values())
    return {
        "ok": not mismatched and fts_ok and orphans_ok,
        "bundle": str(bundle),
        "schema_version": meta.get("schema_version", ""),
        "embedding_dims": meta.get("embedding_dims", ""),
        "mismatched": mismatched,
        "fts": fts,
        "orphans": orphans,
    }


# ---------------------------------------------------------------- CLI


def _print(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key in ("ok", "dry_run", "bundle", "schema_version", "embedding_dims"):
        if key in report:
            print(f"{key}: {report[key]}")
    for group in (
        "tables",
        "inserted",
        "skipped",
        "collided",
        "would_insert",
        "would_skip",
        "would_collide",
    ):
        if report.get(group):
            print(f"{group}:")
            for table, count in report[group].items():
                print(f"  {table}: {count}")
    if report.get("created_tables"):
        print(f"created_tables: {', '.join(report['created_tables'])}")
    if report.get("fts"):
        for table, info in report["fts"].items():
            print(f"fts {table}: {info}")
    if report.get("orphans"):
        print(f"orphans: {report['orphans']}")
    if report.get("mismatched"):
        print(f"mismatched: {report['mismatched']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="fatfish 记忆 bundle：导出 / 导入 / 校验（T-0065）"
    )
    subs = parser.add_subparsers(dest="command", required=True)

    exp = subs.add_parser("export", help="把白名单表导成单个 SQLite bundle")
    exp.add_argument("--db", required=True, help="源库（只读打开）")
    exp.add_argument("--out", required=True, help="bundle 输出路径")
    exp.add_argument("--force", action="store_true", help="覆盖已存在的 bundle")
    exp.add_argument("--json", action="store_true")

    imp = subs.add_parser("import", help="把 bundle 导入目标库（幂等）")
    imp.add_argument("--bundle", required=True)
    imp.add_argument("--db", required=True, help="目标库")
    imp.add_argument("--dry-run", action="store_true", help="只报数字，不写库")
    imp.add_argument(
        "--accept-collisions",
        action="store_true",
        help="主键相同但内容不同的行直接放弃（默认中止）",
    )
    imp.add_argument("--json", action="store_true")

    ver = subs.add_parser("verify", help="行数 / FTS / 引用完整性校验")
    ver.add_argument("--bundle", required=True)
    ver.add_argument("--db", required=True)
    ver.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            report = export_bundle(args.db, args.out, force=args.force)
            _print(report, args.json)
            return 0
        if args.command == "import":
            report = import_bundle(
                args.bundle,
                args.db,
                dry_run=args.dry_run,
                accept_collisions=args.accept_collisions,
            )
            _print(report, args.json)
            return 0
        report = verify_bundle(args.bundle, args.db)
        _print(report, args.json)
        return 0 if report["ok"] else 1
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
