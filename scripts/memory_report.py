#!/usr/bin/env python
"""Read-only memory health report for the local SQLite database.

    python scripts/memory_report.py
    python scripts/memory_report.py --json

It answers the questions that matter after a nightly run:

* did the archive produce anything (episodes / memories / rag chunks)?
* which sessions are in memory, and are any of them test sessions?
* are tags/entities actually populated, or just ["archive", "<date>"]?
* do several sessions collide on the same gist?
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "fatfish.db"
TEST_SESSION_MARKERS = ("verify-", "devcheck", "test-", "live-tts25", "smoke-")


def _ts(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return "—"


def _tags(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
        return [str(x) for x in value] if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def collect(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    def count(table: str) -> int:
        try:
            return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        except sqlite3.Error:
            return -1

    tables = ["conversation_turns", "conversation_events", "episodes",
              "event_details", "memory_items", "rag_chunks", "memory_fts", "rag_fts"]
    counts = {t: count(t) for t in tables}

    sessions = [
        dict(r) for r in con.execute(
            "SELECT session_id, count(*) AS turns, min(created_at) AS first_at, "
            "max(created_at) AS last_at FROM conversation_turns "
            "GROUP BY session_id ORDER BY turns DESC"
        )
    ]
    for s in sessions:
        s["first"] = _ts(s.pop("first_at"))
        s["last"] = _ts(s.pop("last_at"))
        s["test_like"] = any(m in str(s["session_id"]) for m in TEST_SESSION_MARKERS)

    tag_counter: collections.Counter[str] = collections.Counter()
    entity_rows = 0
    gists: collections.Counter[str] = collections.Counter()
    for row in con.execute(
        "SELECT gist, tags_json, entities_json FROM memory_items"
    ):
        tag_counter.update(_tags(row["tags_json"]))
        try:
            if json.loads(str(row["entities_json"] or "[]")):
                entity_rows += 1
        except json.JSONDecodeError:
            pass
        gists[str(row["gist"] or "")] += 1

    episodes = [
        dict(r) for r in con.execute(
            "SELECT id, title, session_id, turn_count, created_at "
            "FROM episodes ORDER BY created_at DESC"
        )
    ]
    for e in episodes:
        e["created"] = _ts(e.pop("created_at"))

    schema = {}
    try:
        for r in con.execute("SELECT key, value FROM schema_meta"):
            schema[str(r["key"])] = str(r["value"])
    except sqlite3.Error:
        pass

    return {
        "db": str(db_path),
        "counts": counts,
        "schema": schema,
        "sessions": sessions,
        "episodes": episodes,
        "top_tags": tag_counter.most_common(15),
        "entity_coverage": f"{entity_rows}/{counts.get('memory_items', 0)}",
        "duplicate_gists": [(g, n) for g, n in gists.most_common(10) if n > 1],
        "test_sessions": [s["session_id"] for s in sessions if s["test_like"]],
    }


def render(report: dict) -> str:
    c = report["counts"]
    lines = [
        f"DB: {report['db']}",
        f"schema_version={report['schema'].get('schema_version', '?')} "
        f"last_nightly={report['schema'].get('last_nightly_date', '—')}",
        "",
        "表计数",
    ]
    for name, value in c.items():
        lines.append(f"  {name:22} {value}")
    lines.append("")
    lines.append("session")
    for s in report["sessions"]:
        flag = "  [测试痕迹]" if s["test_like"] else ""
        lines.append(
            f"  {s['session_id']:34} turns={s['turns']:<4} {s['first']} → {s['last']}{flag}"
        )
    lines.append("")
    lines.append(f"episodes={len(report['episodes'])}")
    for e in report["episodes"][:10]:
        lines.append(
            f"  {e['id']:44} turns={e['turn_count']:<4} created={e['created']}"
        )
    lines.append("")
    lines.append(f"标签 Top: {report['top_tags'] or '（空）'}")
    lines.append(f"实体覆盖: {report['entity_coverage']}")
    if report["duplicate_gists"]:
        lines.append(f"同名 gist: {report['duplicate_gists']}")
    if report["test_sessions"]:
        lines.append(f"测试 session: {report['test_sessions']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"database not found: {path}", file=sys.stderr)
        return 2
    report = collect(path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
