#!/usr/bin/env python
"""Purge test/demo leftovers from a fatfish database (owner request 2026-10-30).

Deletes every row belonging to known *test* sessions -- never real chats:

    qq:private:verify-*      injection-eval fake dialogues (背景第N句/背景回N)
    web:devcheck             dev web-console smoke messages
    *-demo-*                 feature demo sessions (desire-demo / bili-demo)
    activity-YYYYMMDD-*      autonomous-activity turn test samples

Owner decision 2026-10-30: A 档（上面这些）删，B 档（night-* 真实夜间活动）留。
Standing rule: 测试写真实库的数据，测试完就删 —— 以后任何往 data/fatfish.db
写东西的测试/演示，跑完必须清理或直接用临时库。

Dry-run by default; --apply writes. FTS mirrors are rebuilt after deletion.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

TEST_SESSION_RE = re.compile(
    r"(^|:)verify-[0-9a-f]|^web:devcheck$|-demo-\d|^activity-\d{8}-"
)


def test_sessions(con: sqlite3.Connection) -> list[str]:
    sessions: set[str] = set()
    for table in ("conversation_turns", "memory_items", "conversation_events", "episodes"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if "session_id" not in cols:
            continue
        for row in con.execute(f"SELECT DISTINCT session_id s FROM {table}"):
            sid = str(row["s"] or "")
            if sid and TEST_SESSION_RE.search(sid):
                sessions.add(sid)
    return sorted(sessions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/fatfish.db")
    parser.add_argument("--apply", action="store_true", help="write (default dry-run)")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(f"数据库不存在：{path}")
        return 1
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        sessions = test_sessions(con)
        if not sessions:
            print("没有找到测试 session，无需清理。")
            return 0
        marks = ",".join("?" for _ in sessions)

        def ids(sql: str) -> list:
            return [r[0] for r in con.execute(sql, sessions)]

        event_ids = ids(
            "SELECT id FROM conversation_events WHERE session_id IN (" + marks + ")"
        )
        turn_ids = ids(
            "SELECT id FROM conversation_turns WHERE session_id IN (" + marks + ")"
        )
        memory_ids = ids(
            "SELECT id FROM memory_items WHERE session_id IN (" + marks + ")"
        )
        episode_ids = ids(
            "SELECT id FROM episodes WHERE session_id IN (" + marks + ")"
        )

        def child_count(table: str, column: str, values: list) -> int:
            if not values:
                return 0
            m = ",".join("?" for _ in values)
            return int(
                con.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} IN ({m})", values
                ).fetchone()[0]
            )

        plan = {
            "sessions": sessions,
            "conversation_turns": len(turn_ids),
            "conversation_events": len(event_ids),
            "episodes": len(episode_ids),
            "memory_items": len(memory_ids),
            "event_details(by event)": child_count("event_details", "event_id", event_ids),
            "entity_mentions(by turn)": child_count("entity_mentions", "turn_id", turn_ids),
            "tag_links(by event)": child_count("tag_links", "target_id", event_ids),
            "memory_links(by memory)": child_count("memory_links", "from_id", memory_ids)
            + child_count("memory_links", "to_id", memory_ids),
            "rag_chunks(by memory)": child_count("rag_chunks", "source_id", memory_ids),
        }
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] 测试 session {len(sessions)} 个：")
        for sid in sessions:
            print(f"  - {sid}")
        for key, n in plan.items():
            if key != "sessions":
                print(f"  {key}: {n}")
        if not args.apply:
            print("（dry-run，未写库；确认后加 --apply）")
            return 0

        with con:
            def delete(table: str, column: str, values: list) -> int:
                if not values:
                    return 0
                m = ",".join("?" for _ in values)
                cur = con.execute(
                    f"DELETE FROM {table} WHERE {column} IN ({m})", values
                )
                return max(cur.rowcount, 0)

            deleted = {
                "entity_mentions(turn)": delete("entity_mentions", "turn_id", turn_ids),
                "tag_links(event)": delete("tag_links", "target_id", event_ids),
                "memory_links(from)": delete("memory_links", "from_id", memory_ids),
                "memory_links(to)": delete("memory_links", "to_id", memory_ids),
                "rag_chunks(memory)": delete("rag_chunks", "source_id", memory_ids),
                "event_details(event)": delete("event_details", "event_id", event_ids),
                "memory_items": delete("memory_items", "id", memory_ids),
                "conversation_turns": delete("conversation_turns", "id", turn_ids),
                "episodes": delete("episodes", "id", episode_ids),
                "conversation_events": delete(
                    "conversation_events", "id", event_ids
                ),
            }
            for key, n in deleted.items():
                print(f"  deleted {key}: {n}")
            # FTS mirrors: rebuild so deleted rows leave the index deterministically
            for fts, select in (
                ("memory_fts", "SELECT id, gist, summary FROM memory_items"),
                ("detail_fts", "SELECT id, text, COALESCE(quote_text, '') FROM event_details"),
            ):
                con.execute(f"DELETE FROM {fts}")
                con.execute(f"INSERT INTO {fts} {select}")
                print(f"  rebuilt {fts}")
        print("完成。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
