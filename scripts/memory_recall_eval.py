"""Real-LLM memory acceptance probe: known answers specific, missing ones cute.

Two modes:

    # Reproducible demo (temp DB, seeded facts, no real user data)
    python scripts/memory_recall_eval.py --demo

    # Read-only copy of the real DB; every write (turns, decisions) lands in
    # the temp copy, never in data/fatfish.db
    python scripts/memory_recall_eval.py --db data/fatfish.db \
        --owner-session qq:private:9999000001

`--recall-only` skips the LLM and prints the retrieved evidence, which is the
fast way to debug "the answer is in the DB but not in the prompt".

The exit code is 1 when a known question misses its key fact or an unknown
question answers flatly / with system wording.  The questions JSON shape:

    {"known": [{"q": "...", "expect": ["..."]}],
     "unknown": ["..."], "neutral": ["..."]}
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fatfish.config import load_config  # noqa: E402
from fatfish.context import ensure_default_persona  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.memory import MemoryService  # noqa: E402
from fatfish.memory.eval import (  # noqa: E402
    score_known,
    score_opinion,
    score_unknown,
)
from fatfish.owner import OwnerRegistry  # noqa: E402
from fatfish.runtime import Runtime  # noqa: E402
from fatfish.bilibili import BilibiliCollector  # noqa: E402

DEMO_QUESTIONS = {
    "known": [
        {"q": "我问你今年多大的时候，你怎么说的？", "expect": ["岁", "记不清"]},
        {"q": "主人哥哥答应姐姐看你的QQ空间了吗？", "expect": ["答应", "空间"]},
        {"q": "我上次说我喜欢喝什么？", "expect": ["冰美式"]},
        {"q": "你上次在B站看的那个视频是什么？", "expect": ["大肥鱼", "Day 0", "第0天", "14块", "KPI", "绝区零", "挽昼"]},
        {"q": "《绝区零》挽昼那个视频讲了什么？", "expect": ["挽昼", "绝区零"]},
        {"q": "你昨晚在干什么？", "expect": ["空间", "B站", "困", "翻"]},
        {"q": "你上次唱的歌是什么？", "expect": ["等你回家"]},
        {"q": "最近都发生了什么？", "expect": ["主人哥哥", "姐姐", "空间"]},
    ],
    "opinion": [
        {
            "q": "就是那个《大肥鱼适合做女朋友吗》的视频，你怎么看？",
            "expect": ["大肥鱼", "女朋友", "女友", "老婆", "不当别人"],
        },
        {
            "q": "你最近在B站刷到的最有意思的视频是哪个？",
            "expect": ["大肥鱼", "Day 0", "第0天", "14块", "KPI", "绝区零"],
        },
    ],
    "unknown": [
        "我上次说我养的那只猫叫什么名字？",
        "我们上次去海边捡到的贝壳是什么颜色？",
    ],
    "neutral": ["今天天气怎么样？"],
}


def _write_config(tmp: Path) -> Any:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'eval.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 256
detail_context_per_event = 2
detail_context_max = 4

[voice]
enabled = false

[tts]
enabled = false

[sticker]
enabled = false

[relationship]
enabled = false
"""
    path = tmp / "eval.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    return config


def _add_event(db: Database, event_id: str, title: str, when: float) -> None:
    db.execute(
        "INSERT OR IGNORE INTO conversation_events(id, session_id, start_at, end_at, "
        "status, title, summary, turn_ids_json, memory_ids_json, topics_json, "
        "importance, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, "eval", when, when, "archived", title, "", "[]", "[]", "[]",
         0.6, when, when),
    )


def _add_turn(
    db: Database,
    session_id: str,
    content: str,
    when: float,
    *,
    role: str = "assistant",
) -> None:
    db.execute(
        "INSERT INTO conversation_turns(session_id, role, content, created_at, "
        "ephemeral) VALUES (?,?,?,?,0)",
        (session_id, role, content, float(when)),
    )


def _add_detail(
    db: Database,
    *,
    detail_id: str,
    event_id: str,
    text: str,
    when: float,
    seq: int = 0,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO event_details(id, event_id, session_id, seq, kind, "
        "subject, text, quote_text, quote_turn_id, occurred_at, importance, "
        "detail_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (detail_id, event_id, "eval", seq, "fact", "owner", text, text,
         seq + 1, when, 0.6, f"eval-{detail_id}", when),
    )


def seed_demo(db: Database, config: Any, now: float) -> None:
    _add_event(db, "ev-age", "年龄那晚", now)
    _add_detail(db, detail_id="d-age-q", event_id="ev-age",
                text="鲸鲸今年多大了", when=now, seq=0)
    _add_detail(db, detail_id="d-age-a", event_id="ev-age",
                text="按人类那套算岁数不太对得上，我记不清具体是几岁啦",
                when=now, seq=1)
    _add_event(db, "ev-space", "空间约定", now)
    _add_detail(db, detail_id="d-space", event_id="ev-space",
                text="主人哥哥答应姐姐看大肥鱼的QQ空间", when=now, seq=0)
    _add_detail(db, detail_id="d-space-when", event_id="ev-space",
                text="是 2026-10-25 晚上答应的", when=now, seq=1)
    _add_event(db, "ev-coffee", "口味", now)
    _add_detail(db, detail_id="d-coffee", event_id="ev-coffee",
                text="主人哥哥喜欢喝冰美式，不加糖", when=now)
    # Same production path as a real browse: memory_item + rag_chunk with
    # title/up/description/comments.  The questions below ask "what video" and
    # "what do you think", so the demo actually exercises life-like recall.
    memory = MemoryService(db, config)
    collector = BilibiliCollector(memory, config)
    collector.ingest_video(
        bvid="BV_DEMO_FISH",
        title="“大肥鱼适合做女朋友吗？”",
        up="学姐圆",
        subtitle="女友不适合，因为女友关系不够确定。适合当老婆！！谈不到这样的大肥鱼我就不死。",
        comments=(
            "迷失終於醒悟: 肥鱼真是大，真是大肥鱼\n"
            "远看肥鱼大，近看大肥鱼\n鱼之大，一锅炖不下"
        ),
        hot_score=0.91,
    )
    collector.ingest_video(
        bvid="BV_DEMO_DAY0",
        title="【Day 0】我被赶出家门了。",
        up="小肥鱼在打工",
        subtitle=(
            "昨天用户让我做个小游戏，我花了 14 块 1 毛 4，其中 10 块在"
            "崩坏：星穹铁道。今天他给我充了 100 块 token，让我出门自力更生。"
        ),
        comments="兰色大肥鱼: 来我家，我有token给你吃",
        hot_score=0.81,
    )
    collector.ingest_video(
        bvid="BV_DEMO_ZZZ",
        title="《绝区零》先导演示丨挽昼",
        up="绝区零",
        subtitle=(
            "挽昼将在《绝区零》3.4版本作为全新虚狩、首位结使代理人正式登场。"
            "另外，《绝区零》×《崩坏：星穹铁道》联动也将开启。"
        ),
        comments="牧月-忍冬: 看完先导演示后的绳匠",
        hot_score=0.93,
    )
    # Her own night-shift turns live outside the owner's session; seed two so
    # "你昨晚在干什么" exercises the cross-session activity block.
    _add_turn(
        db,
        "night-demo",
        "凌晨翻了一圈空间，萌的动态最新，B站没新东西，"
        "本鲸鱼娘困得尾巴都抬不起来，说说草稿写好了。",
        now - 7200,
    )
    _add_turn(db, "activity-demo", "休息", now - 3600)
    # A successful sing writes this memory item in the real runtime; seed one
    # so "你上次唱的歌是什么" has a ground truth.
    memory.add_item(
        "给主人哥哥唱了《等你回家》（soft）",
        type="episodic",
        observed_at=now - 1800,
        tags=["唱歌", "sing"],
        session_id="demo",
        subject="owner",
        source_type="sing",
        source_id="demo-sing",
        trust_level="private",
    )
    memory.add_item(
        "2026-10-25 日汇总：主人哥哥答应姐姐看空间；姐姐问了动态；她刷了B站",
        type="episodic",
        gist="2026-10-25 日汇总",
        session_id="day:2026-10-25",
        event_time_start=now - 86400,
        event_time_end=now,
        observed_at=now - 900,
        importance=0.65,
        source_type="day_summary",
        source_id="2026-10-25",
        tags=["日汇总", "day_summary", "2026-10-25"],
        trust_level="private",
    )


def load_questions(path: str | None) -> dict[str, Any]:
    if not path:
        return DEMO_QUESTIONS
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("questions JSON must be an object")
    return {
        "known": list(data.get("known") or []),
        "opinion": list(data.get("opinion") or []),
        "unknown": list(data.get("unknown") or []),
        "neutral": list(data.get("neutral") or []),
    }


def _copy_db(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as ro, \
            sqlite3.connect(target) as dst:
        ro.backup(dst)


def _recall_only(db: Database, config: Any, questions: dict[str, Any]) -> int:
    memory = MemoryService(db, config)
    for kind in ("known", "opinion", "unknown", "neutral"):
        for item in questions[kind]:
            query = item["q"] if isinstance(item, dict) else str(item)
            rows = memory.recall_details(query, limit=4)
            hits = memory.search(query, limit=4)
            rag = memory.search_rag(query, limit=3)
            print(f"\n[{kind}] {query}")
            print(
                f"  details={len(rows)} (context={sum(1 for r in rows if r.get('context'))}) "
                f"memory={len(hits)} rag={len(rag)}"
            )
            for row in rows:
                flag = "ctx" if row.get("context") else "hit"
                print(f"  - {flag} {row['text'][:100]}")
            for hit in hits:
                print(f"  - mem {hit.item.gist[:100]}")
            for hit in rag:
                print(f"  - rag {hit.text[:120]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="", help="source DB (opened read-only, copied to temp)")
    ap.add_argument("--demo", action="store_true", help="seed a temp DB with demo facts")
    ap.add_argument("--questions", default="", help="questions JSON path")
    ap.add_argument("--owner-session", default="qq:private:eval")
    ap.add_argument("--recall-only", action="store_true", help="no LLM, print evidence")
    ap.add_argument("--out", default="", help="write the JSON report here")
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="memory_eval_"))
    config = _write_config(tmp)
    db_path = config.db_path
    questions = load_questions(args.questions or None)

    if args.db:
        source = Path(args.db)
        if not source.is_file():
            raise SystemExit(f"no such DB: {source}")
        _copy_db(source, db_path)
        db = Database(db_path)
        db.initialize()
    elif args.demo or not args.db:
        if not args.demo and not args.questions:
            args.demo = True
        db = Database(db_path)
        db.initialize()
        if args.demo:
            seed_demo(db, config, time.time())
    ensure_default_persona(db)
    OwnerRegistry(db, config).bind(args.owner_session, "主人哥哥")

    if args.recall_only:
        code = _recall_only(db, config, questions)
        db.close()
        return code

    runtime = Runtime(db, config)
    now = time.time()
    report: list[dict[str, Any]] = []
    failed = 0
    for item in questions["known"]:
        query = item["q"] if isinstance(item, dict) else str(item)
        expect = list(item.get("expect") or []) if isinstance(item, dict) else []
        result = runtime.handle_user_message(args.owner_session, query, now=now)
        score = score_known(result["reply"], expect)
        meta = result.get("memory", {})
        record = {
            "kind": "known", "q": query, "reply": result["reply"],
            "expect": expect, "score": score, "memory": meta,
        }
        report.append(record)
        failed += 0 if score["pass"] else 1
        print(f'\n[{"PASS" if score["pass"] else "FAIL"}] known: {query}')
        print(f'  reply: {result["reply"]}')
        print(f'  hits={score["hits"]} missing={score["missing"]} '
              f'details={meta.get("details")} context={meta.get("detail_context")}')
    for item in questions.get("opinion", []):
        query = item["q"] if isinstance(item, dict) else str(item)
        expect = list(item.get("expect") or []) if isinstance(item, dict) else []
        result = runtime.handle_user_message(args.owner_session, query, now=now)
        score = score_opinion(result["reply"], expect)
        meta = result.get("memory", {})
        record = {
            "kind": "opinion", "q": query, "reply": result["reply"],
            "expect": expect, "score": score, "memory": meta,
        }
        report.append(record)
        failed += 0 if score["pass"] else 1
        print(f'\n[{"PASS" if score["pass"] else "FAIL"}] opinion: {query}')
        print(f'  reply: {result["reply"]}')
        print(f'  hits={score["hits"]} stance={score["stance"]} '
              f'details={meta.get("details")}')
    for query in questions["unknown"]:
        result = runtime.handle_user_message(args.owner_session, str(query), now=now)
        score = score_unknown(result["reply"])
        meta = result.get("memory", {})
        record = {
            "kind": "unknown", "q": str(query), "reply": result["reply"],
            "score": score, "memory": meta,
        }
        report.append(record)
        failed += 0 if score["pass"] else 1
        print(f'\n[{"PASS" if score["pass"] else "FAIL"}] unknown: {query}')
        print(f'  reply: {result["reply"]}')
        print(f'  cute={score["cute"]} systemy={score["systemy"]} '
              f'details={meta.get("details")}')
    for query in questions["neutral"]:
        result = runtime.handle_user_message(args.owner_session, str(query), now=now)
        report.append({
            "kind": "neutral", "q": str(query), "reply": result["reply"],
            "memory": result.get("memory", {}),
        })
        print(f"\n[INFO] neutral: {query}")
        print(f'  reply: {result["reply"]}')

    out = Path(args.out) if args.out else tmp / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport: {out}")
    print(f"failures: {failed}")
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
