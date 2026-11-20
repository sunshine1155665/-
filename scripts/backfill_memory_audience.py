#!/usr/bin/env python
"""Backfill ``memory_items.audience`` (T-0073).

The column lands via ``Database`` column migrations, but existing rows all
default to ``owner`` (fail-closed).  This script applies the owner's black-list
policy to the *existing* rows: memories originating in the owner's own session
become ``open`` unless the 作息 pattern matches; everything else stays
``owner``-only.  Dry-run by default -- it prints the proposed classification
for the owner to review first.

Usage::

    python scripts/backfill_memory_audience.py --db data/fatfish.db          # dry-run
    python scripts/backfill_memory_audience.py --db data/fatfish.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.memory.service import SCHEDULE_SECRET_RE  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/fatfish.db")
    parser.add_argument(
        "--apply", action="store_true", help="write the changes (default: dry-run)"
    )
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(f"数据库不存在：{path}")
        return 1
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(memory_items)")}
        if "audience" not in cols:
            print("audience 列不存在——先用新版代码跑一次 Database.initialize()（自动加列），再回填。")
            return 1
        bound = {
            str(r["session_id"]): str(r["role"] or "").strip().lower()
            for r in con.execute("SELECT session_id, role FROM owner_bindings")
        }
        changes: list[tuple[str, str, str, str]] = []
        for row in con.execute(
            "SELECT id, audience, session_id, gist, summary FROM memory_items"
        ):
            sid = str(row["session_id"] or "").strip()
            if bound.get(sid) == "owner" and not SCHEDULE_SECRET_RE.search(
                f"{row['gist']}\n{row['summary']}"
            ):
                want = "open"
            else:
                want = "owner"
            if want != str(row["audience"] or "owner"):
                changes.append(
                    (str(row["id"]), str(row["audience"] or "owner"), want,
                     str(row["gist"] or "")[:48])
                )

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] {len(changes)} 行需要改：")
        for item_id, was, now, gist in changes:
            print(f"  {item_id[:12]}…  {was} -> {now}  {gist}")
        if not args.apply:
            print("（dry-run，未写库；确认无误后加 --apply）")
            return 0
        with con:
            for item_id, _was, now, _gist in changes:
                con.execute(
                    "UPDATE memory_items SET audience=? WHERE id=?", (now, item_id)
                )
        print(f"已写回 {len(changes)} 行。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
