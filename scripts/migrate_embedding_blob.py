#!/usr/bin/env python
"""Convert JSON embedding columns to compact float32 BLOBs.

SQLite is dynamically typed, so this needs no ALTER TABLE: the same column
stores TEXT for old rows and BLOB for new ones.  ``fatfish.memory.embedder.loads``
reads both, which means a half-migrated database keeps working.

    python scripts/migrate_embedding_blob.py --dry-run
    python scripts/migrate_embedding_blob.py --vacuum

Always back up ``data/fatfish.db`` first (``python -m fatfish backup``).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fatfish.memory.embedder import pack_embedding  # noqa: E402

TARGETS = (
    ("memory_items", "embedding"),
    ("rag_chunks", "embedding"),
    ("stickers", "embedding_text"),
)


def convert(db_path: Path, *, dry_run: bool, vacuum: bool) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    total = 0
    for table, column in TARGETS:
        try:
            rows = con.execute(
                f'SELECT rowid AS rid, "{column}" AS raw, length("{column}") AS n '
                f'FROM "{table}" WHERE "{column}" IS NOT NULL '
                f"AND typeof(\"{column}\")='text'"
            ).fetchall()
        except sqlite3.Error as exc:
            print(f"skip {table}.{column}: {exc}")
            continue
        before = sum(int(r["n"] or 0) for r in rows)
        after = 0
        changed = 0
        for row in rows:
            try:
                vec = json.loads(row["raw"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(vec, list) or not vec:
                continue
            blob = pack_embedding([float(v) for v in vec])
            if blob is None:
                continue
            after += len(blob)
            changed += 1
            if not dry_run:
                con.execute(
                    f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',
                    (blob, row["rid"]),
                )
        total += changed
        print(
            f"{table}.{column}: {changed} rows  "
            f"{before:,} -> {after:,} bytes "
            f"({100 * (1 - after / before) if before else 0:.1f}% saved)"
        )
    if dry_run:
        con.rollback()
        print(f"dry run: {total} rows would change")
    else:
        con.commit()
        print(f"converted {total} rows")
        if vacuum:
            print("vacuuming...")
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print(f"file size now: {db_path.stat().st_size:,} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "fatfish.db"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"database not found: {path}", file=sys.stderr)
        return 2
    print(f"before: {path.stat().st_size:,} bytes")
    return convert(path, dry_run=args.dry_run, vacuum=args.vacuum)


if __name__ == "__main__":
    raise SystemExit(main())
