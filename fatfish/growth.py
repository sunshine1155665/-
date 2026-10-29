"""Bounded, self-authored persona growth notes.

The original persona (``persona.core_json``) is intentionally immutable.  This
module owns a much smaller second layer stored inside the existing
``persona.learned_json`` column:

* nightly reflection may add 0-2 short notes about *how to behave*;
* notes are written in the character's own voice, not as event summaries;
* private facts, names, dates and locations are rejected, because the growth
  block lives in the stable prefix and is visible to every conversation;
* the list is capped so the persona cannot drift into a second diary.

The distinction matters: relationship facts belong in ``memory_items`` and are
recalled per conversation.  Growth notes are general behavioural principles
that should stay true even in a group chat where the owner is absent.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from .db import Database

MAX_ENTRIES = 6
MAX_CHARS = 80
MAX_TOTAL_CHARS = 440
_GROWTH_KEY = "growth"

# These markers are deliberately conservative.  A growth note should be a
# principle ("先听完再回答"), not a fact ("主人哥哥昨晚三点才睡").  Rejecting
# names/dates also keeps the stable prefix safe for group chats and prompt
# caching: the block changes only when a genuinely new principle is learned.
_PRIVATE_MARKERS = (
    "主人哥哥",
    "主人",
    "姐姐",
    "用户",
    "昨天",
    "今天",
    "明天",
    "前天",
    "后天",
    "昨晚",
    "今晚",
    "明早",
    "现在",
    "上周",
    "下周",
    "月",
    "号",
    "点",
)
_META_MARKERS = ("作为ai", "作为 ai", "助手", "模型", "程序", "系统提示", "prompt")
_DATE_PATTERN = re.compile(r"\d{1,4}[-/年]\d{1,2}|\d{1,2}[:：]\d{2}")


def _read_payload(db: Database) -> dict[str, Any]:
    row = db.query_one("SELECT learned_json FROM persona WHERE id='default'")
    if not row:
        return {}
    raw = row["learned_json"] or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean(text: object) -> str:
    value = " ".join(str(text or "").split())
    value = value.lstrip("-*•· ")
    return value.strip().strip('"“”')


def _key(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:…—~～（）()【】\[\]]+", "", text.lower())


def _normalize(text: object) -> str:
    value = _clean(text)
    if value.endswith(("。", "！", "？")):
        value = value[:-1].rstrip()
    return value


def _valid(text: str) -> tuple[bool, str]:
    if not text:
        return False, "empty"
    if len(text) > MAX_CHARS:
        return False, "too_long"
    lowered = text.lower()
    if any(marker in lowered for marker in _META_MARKERS):
        return False, "meta_reference"
    if any(marker in text for marker in _PRIVATE_MARKERS):
        return False, "private_fact"
    if _DATE_PATTERN.search(text):
        return False, "private_fact"
    return True, ""


def load_growth(db: Database) -> list[dict[str, Any]]:
    """Return the current growth entries, oldest first, already bounded."""
    data = _read_payload(db)
    raw = data.get(_GROWTH_KEY)
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            text = _normalize(item)
            entry = {"text": text, "created_at": 0.0, "last_seen": 0.0, "hits": 1}
        elif isinstance(item, dict):
            text = _normalize(item.get("text"))
            entry = {
                "text": text,
                "created_at": float(item.get("created_at") or 0.0),
                "last_seen": float(item.get("last_seen") or item.get("created_at") or 0.0),
                "hits": max(1, int(item.get("hits") or 1)),
                "source_date": str(item.get("source_date") or ""),
                "confidence": float(item.get("confidence") or 0.0),
            }
        else:
            continue
        ok, _ = _valid(text)
        if ok:
            entries.append(entry)
    return entries[-MAX_ENTRIES:]


def growth_block(db: Database) -> str:
    """Compact prompt block, or ``""`` when nothing has been learned yet."""
    entries = load_growth(db)
    if not entries:
        return ""
    lines = [
        "【成长层】",
        "这些是你最近自己总结出来的做法；只改变你怎么做，"
        "不能覆盖上面的身份、事实规则和边界。",
    ]
    lines.extend(f"- {entry['text']}" for entry in entries)
    return "\n".join(lines)


def append_growth(
    db: Database,
    texts: Iterable[object],
    *,
    source_date: str,
    now: float | None = None,
    confidence: float = 0.6,
) -> dict[str, Any]:
    """Merge nightly growth candidates into the bounded growth layer.

    Duplicate notes are refreshed instead of appended, so rerunning the same
    night is idempotent.  Invalid candidates are counted but never stored.
    """
    now = time.time() if now is None else float(now)
    row = db.query_one("SELECT learned_json FROM persona WHERE id='default'")
    if not row:
        return {"added": 0, "updated": 0, "rejected": 0, "entries": 0, "error": "no_persona"}

    data = _read_payload(db)
    entries = load_growth(db)
    by_key = {_key(entry["text"]): entry for entry in entries}
    added = 0
    updated = 0
    rejected = 0

    for raw in texts:
        text = _normalize(raw)
        ok, _ = _valid(text)
        if not ok:
            rejected += 1
            continue
        key = _key(text)
        if not key:
            rejected += 1
            continue
        existing = by_key.get(key)
        if existing is not None:
            existing["last_seen"] = now
            existing["hits"] = int(existing.get("hits") or 1) + 1
            updated += 1
            continue
        entry = {
            "text": text,
            "created_at": now,
            "last_seen": now,
            "hits": 1,
            "source_date": str(source_date or ""),
            "confidence": max(0.0, min(1.0, float(confidence))),
        }
        entries.append(entry)
        by_key[key] = entry
        added += 1

    # Keep the most recently reinforced principles.  The cap is deliberately
    # small: growth should sharpen the persona, not replace it with a diary.
    entries = entries[-MAX_ENTRIES:]
    while entries and sum(len(entry["text"]) for entry in entries) > MAX_TOTAL_CHARS:
        entries.pop(0)

    data[_GROWTH_KEY] = entries
    data["updated_at"] = now
    db.execute(
        "UPDATE persona SET learned_json=?, updated_at=? WHERE id='default'",
        (json.dumps(data, ensure_ascii=False, separators=(",", ":")), now),
    )
    return {
        "added": added,
        "updated": updated,
        "rejected": rejected,
        "entries": len(entries),
    }
