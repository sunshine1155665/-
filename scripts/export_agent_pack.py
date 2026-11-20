#!/usr/bin/env python
"""Export a compact, agent-readable memory pack for ZCode / other agents.

    python scripts/export_agent_pack.py
    python scripts/export_agent_pack.py --out data/exports/zcode

Outputs JSON/JSONL plus a ready-to-paste extraction prompt.  Test sessions are
marked (``test_like``) rather than silently dropped, so the agent can see the
data shape while the importer still refuses to write them into memory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "fatfish.db"
TEST_MARKERS = ("verify-", "devcheck", "test-", "live-tts25", "smoke-")


def iso(value: object) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None


def test_like(session_id: str) -> bool:
    return any(marker in str(session_id or "") for marker in TEST_MARKERS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(ROOT / "data" / "exports" / "zcode"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    turns = [
        {
            "id": int(row["id"]),
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "at": iso(row["created_at"]),
            "event_id": row["event_id"],
            "episode_id": row["episode_id"],
            "test_like": test_like(row["session_id"]),
        }
        for row in con.execute(
            "SELECT id, session_id, role, content, created_at, event_id, episode_id "
            "FROM conversation_turns WHERE ephemeral=0 ORDER BY created_at, id"
        )
    ]

    events = []
    for row in con.execute(
        "SELECT id, session_id, title, summary, start_at, end_at, turn_ids_json, "
        "memory_ids_json, importance FROM conversation_events ORDER BY start_at"
    ):
        try:
            turn_ids = json.loads(row["turn_ids_json"] or "[]")
        except json.JSONDecodeError:
            turn_ids = []
        events.append({
            "id": row["id"],
            "session_id": row["session_id"],
            "title": row["title"],
            "summary": row["summary"],
            "start": iso(row["start_at"]),
            "end": iso(row["end_at"]),
            "turn_ids": turn_ids,
            "importance": row["importance"],
            "test_like": test_like(row["session_id"]),
        })

    details = [
        {
            "id": row["id"],
            "event_id": row["event_id"],
            "kind": row["kind"],
            "text": row["text"],
            "quote": row["quote_text"],
            "turn_id": row["quote_turn_id"],
            "at": iso(row["occurred_at"]),
            "emotion": row["emotion"],
            "importance": row["importance"],
        }
        for row in con.execute(
            "SELECT id, event_id, kind, text, quote_text, quote_turn_id, occurred_at, "
            "emotion, importance FROM event_details ORDER BY occurred_at, seq"
        )
    ]

    memories = [
        {
            "id": row["id"],
            "gist": row["gist"],
            "summary": row["summary"],
            "type": row["type"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "entities": json.loads(row["entities_json"] or "[]"),
            "at": iso(row["observed_at"]),
            "importance": row["importance"],
        }
        for row in con.execute(
            "SELECT id, gist, summary, type, tags_json, entities_json, observed_at, "
            "importance FROM memory_items ORDER BY observed_at"
        )
    ]
    con.close()

    def write_json(name: str, payload: object) -> None:
        (out / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    (out / "turns.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in turns) + "\n",
        encoding="utf-8",
    )
    write_json("events.json", events)
    write_json("details.json", details)
    write_json("memories.json", memories)

    real_turns = [t for t in turns if not t["test_like"]]
    real_events = [e for e in events if not e["test_like"]]
    prompt = f"""# 记忆细节抽取任务（ZCode）

输入文件（与本文件同目录）：
- `turns.jsonl`：{len(turns)} 轮对话（其中真实对话 {len(real_turns)} 轮，`test_like=true` 的不要抽取）
- `events.json`：{len(events)} 个事件（真实 {len(real_events)} 个）

## 目标

把每个真实事件拆成**原子细节**。只记录对话里明确出现的内容，不要推测。

## 输出

只输出一个 JSON 数组，不要 Markdown、不要解释：

```json
[
  {{
    "event_id": "事件 id",
    "turn_id": 123,
    "kind": "fact|emotion|plan|decision|change|quote",
    "text": "一句可独立阅读的事实，10-120 字",
    "quote": "该 turn 中的精确原文子串",
    "subject": "谁（主人/鲸鱼娘/姐姐/…）",
    "predicate": "可选，动词/关系",
    "object": "可选，宾语/对象",
    "emotion": "开心/委屈/疲惫/… 或 null",
    "confidence": 0.0-1.0
  }}
]
```

## 硬规则

1. `quote` 必须是该 `turn_id` 的 `content` 的**精确子串**，不能改写、不能拼接。
2. 一条只写一个事实；不要把整段对话压成一句摘要。
3. 不写没有出现过的信息；不确定就不写。
4. `turn_id` 必须属于该 `event_id` 的 `turn_ids`。
5. 不要处理 `test_like=true` 的 session。
6. 时间、地点、人物、情绪、计划、变化优先保留。

## 回导

把结果保存为 `details_llm.json`，运行：

```bash
python scripts/import_llm_details.py data/exports/zcode/details_llm.json --dry-run
python scripts/import_llm_details.py data/exports/zcode/details_llm.json
```
"""
    (out / "prompt-memory-extraction.md").write_text(prompt, encoding="utf-8")

    manifest = {
        "generated_at": iso(time.time()),
        "turns": len(turns),
        "real_turns": len(real_turns),
        "events": len(events),
        "real_events": len(real_events),
        "details": len(details),
        "memories": len(memories),
        "files": sorted(p.name for p in out.iterdir() if p.is_file()),
    }
    write_json("manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
