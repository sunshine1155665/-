#!/usr/bin/env python
"""Import LLM-extracted memory details with strict evidence validation.

    python scripts/import_llm_details.py details_llm.json --dry-run
    python scripts/import_llm_details.py details_llm.json

A row is accepted only when its turn exists, belongs to the claimed event, and
its ``quote`` is an exact substring of that turn.  Nothing is paraphrased on the
way in: rejected rows are reported, never repaired silently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "fatfish.db"
KINDS = {"fact", "emotion", "plan", "decision", "change", "quote"}
TEST_MARKERS = ("verify-", "devcheck", "test-", "live-tts25", "smoke-")


def digest(*parts: object) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    rows = payload.get("details", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        print("expected a JSON array or {'details': [...]}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    accepted, rejected = 0, Counter()
    samples: list[str] = []
    now = time.time()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            rejected["not_object"] += 1
            continue
        event_id = str(row.get("event_id") or "").strip()
        kind = str(row.get("kind") or "fact").strip()
        text = str(row.get("text") or "").strip()
        quote = str(row.get("quote") or "").strip()
        try:
            turn_id = int(row.get("turn_id"))
        except (TypeError, ValueError):
            rejected["bad_turn_id"] += 1
            continue
        if kind not in KINDS:
            rejected["bad_kind"] += 1
            continue
        if not 4 <= len(text) <= 200:
            rejected["bad_text_length"] += 1
            continue
        event = con.execute(
            "SELECT session_id, turn_ids_json FROM conversation_events WHERE id=?",
            (event_id,),
        ).fetchone()
        if not event:
            rejected["missing_event"] += 1
            continue
        session_id = str(event["session_id"] or "")
        if any(marker in session_id for marker in TEST_MARKERS):
            rejected["test_session"] += 1
            continue
        try:
            event_turns = {int(x) for x in json.loads(event["turn_ids_json"] or "[]")}
        except (ValueError, json.JSONDecodeError):
            event_turns = set()
        if turn_id not in event_turns:
            rejected["turn_not_in_event"] += 1
            continue
        turn = con.execute(
            "SELECT content, created_at, episode_id FROM conversation_turns WHERE id=?",
            (turn_id,),
        ).fetchone()
        if not turn:
            rejected["missing_turn"] += 1
            continue
        content = str(turn["content"] or "")
        if kind == "quote" and not quote:
            rejected["quote_required"] += 1
            continue
        if quote and quote not in content:
            rejected["quote_not_exact"] += 1
            if len(samples) < 5:
                samples.append(f"event={event_id} turn={turn_id} quote={quote[:40]!r}")
            continue
        detail_hash = digest(event_id, turn_id, kind, text, quote)
        detail_id = uuid.uuid4().hex
        cursor = con.execute(
            "INSERT OR IGNORE INTO event_details("
            "id, event_id, episode_id, session_id, seq, kind, subject, predicate, object, "
            "text, speaker, quote_turn_id, quote_text, quote_hash, occurred_at, "
            "time_granularity, time_confidence, valid_from, valid_to, emotion, "
            "emotion_intensity, importance, confidence, source, detail_hash, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                detail_id, event_id, turn["episode_id"], session_id, index, kind,
                str(row.get("subject") or ""), str(row.get("predicate") or ""),
                str(row.get("object") or ""), text, str(row.get("subject") or ""),
                turn_id, quote or None, digest(quote) if quote else None,
                float(turn["created_at"] or now), "minute", 0.8, None, None,
                row.get("emotion") or None, None,
                max(0.0, min(1.0, float(row.get("importance") or 0.5))),
                max(0.0, min(1.0, float(row.get("confidence") or 0.75))),
                "llm", detail_hash, now,
            ),
        )
        if cursor.rowcount:
            accepted += 1
        else:
            rejected["duplicate"] += 1

    if args.dry_run:
        con.rollback()
        print("dry run: no rows written")
    else:
        con.commit()
    con.close()
    print(json.dumps({
        "input": len(rows),
        "accepted": accepted,
        "rejected": dict(rejected),
        "samples": samples,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
