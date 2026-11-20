"""Namespaced tag governance (ADR-0003 §1 schema, §3.6 lifecycle).

``memory_items.tags_json`` historically holds flat values such as
``["archive", "2026-10-22"]``: no namespace, no dedup, no lifecycle.  This
module routes every raw tag into one of five *closed* namespaces
(``domain`` / ``relation`` / ``emotion`` / ``time`` / ``topic``) and persists
it into the ``tags`` / ``tag_links`` tables created by schema v6.

Explosion control (ADR-0003 §3.6, "标签爆炸") rests on three mechanisms:

1. Closed namespaces: ``classify_tag`` is pure rule matching against fixed
   word sets (emotion labels aligned with ``details._EMOTION_WORDS`` and the
   relation vocabulary), so the vocabulary can only grow through explicit
   code changes, never through free-form input.
2. candidate -> active promotion: a brand-new tag is inserted with
   ``status='candidate'`` and only becomes ``active`` once ``usage_count``
   reaches 2 real links, i.e. it recurred across at least two targets.
3. ``aliases_json`` is reserved on every ``tags`` row for future synonym
   merging (``status='merged'``); no merge logic is implemented yet.

This module never deletes or rewrites existing rows; demotion and merging
are left to later tooling.  Stdlib only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fatfish.db import Database

__all__ = [
    "NAMESPACES",
    "EMOTION_LABELS",
    "RELATION_WORDS",
    "classify_tag",
    "ensure_tag",
    "link_tag",
    "sync_tags",
    "backfill_tags",
]

NAMESPACES = ("domain", "relation", "emotion", "time", "topic")

# Emotion label vocabulary.  Superset-aligned with the normalized labels of
# ``fatfish/memory/details.py::_EMOTION_WORDS`` so details extracted there can
# be linked without re-classification surprises.
EMOTION_LABELS: frozenset[str] = frozenset({
    "开心", "高兴", "喜欢", "想念", "疲惫", "难过",
    "委屈", "生气", "烦躁", "害怕", "温柔",
})

# Relation vocabulary: ascii identifiers (for pipeline/code use, matched
# case-insensitively) plus the Chinese surface forms actually seen in chat.
RELATION_WORDS: frozenset[str] = frozenset({
    "owner", "friend", "group", "external", "unknown",
    "主人", "主人哥哥", "姐姐", "群聊", "自己",
})

_TIME_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

_TOPIC_NAME_MAX = 48


def classify_tag(raw: str) -> tuple[str, str] | None:
    """Classify one raw tag string into ``(namespace, name)``.

    Returns ``None`` for blank input.  Rules are applied in order:

    1. ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``  -> ``("time", as-is)``
    2. known emotion label                      -> ``("emotion", as-is)``
    3. relation word (case-insensitive)         -> ``("relation", as-is)``
    4. anything else                            -> ``("topic", truncated to 48 chars)``
    """
    text = (raw or "").strip()
    if not text:
        return None
    if _TIME_RE.match(text):
        return ("time", text)
    if text in EMOTION_LABELS:
        return ("emotion", text)
    if text in RELATION_WORDS or text.lower() in RELATION_WORDS:
        return ("relation", text)
    return ("topic", text[:_TOPIC_NAME_MAX])


def ensure_tag(db: "Database", namespace: str, name: str, *, now: float | None = None) -> int:
    """Return the id of the ``(namespace, name)`` tag, creating it if absent.

    A newly created tag starts with ``status='candidate'`` and
    ``usage_count=0`` (ADR-0003 §3.6).  Idempotent and race-safe against the
    ``UNIQUE(namespace, name)`` constraint.
    """
    row = db.query_one(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?",
        (namespace, name),
    )
    if row is not None:
        return int(row["id"])
    created_at = time.time() if now is None else float(now)
    db.execute(
        "INSERT OR IGNORE INTO tags(namespace, name, status, usage_count, created_at) "
        "VALUES (?, ?, 'candidate', 0, ?)",
        (namespace, name, created_at),
    )
    row = db.query_one(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?",
        (namespace, name),
    )
    if row is None:  # pragma: no cover - UNIQUE(namespace, name) guarantees a row
        raise RuntimeError(f"failed to ensure tag ({namespace!r}, {name!r})")
    return int(row["id"])


def link_tag(
    db: "Database",
    tag_id: int,
    target_type: str,
    target_id: str,
    *,
    source: str = "extract",
    weight: float = 1.0,
    now: float | None = None,
) -> bool:
    """Link ``tag_id`` to ``(target_type, target_id)``.

    Returns ``True`` when a new link row was inserted, ``False`` when the
    link already existed.  Only real inserts bump ``usage_count`` and may
    promote a ``candidate`` tag to ``active`` (usage_count >= 2).
    """
    created_at = time.time() if now is None else float(now)
    cursor = db.execute(
        "INSERT OR IGNORE INTO tag_links(tag_id, target_type, target_id, weight, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (int(tag_id), target_type, target_id, float(weight), source, created_at),
    )
    if cursor.rowcount != 1:
        return False
    # Within one UPDATE all SET expressions see the pre-update row, so
    # ``usage_count + 1`` is the new count: promote exactly when it hits >= 2.
    db.execute(
        "UPDATE tags SET "
        "usage_count = usage_count + 1, "
        "status = CASE "
        "  WHEN status = 'candidate' AND usage_count + 1 >= 2 THEN 'active' "
        "  ELSE status END "
        "WHERE id = ?",
        (int(tag_id),),
    )
    return True


def sync_tags(
    db: "Database",
    tags: Iterable[str],
    target_type: str,
    target_id: str,
    *,
    now: float | None = None,
) -> dict:
    """Classify -> ensure -> link every raw tag onto one target.

    Blank/unclassifiable entries are skipped.  Returns
    ``{"created": <new tag rows>, "linked": <new link rows>}``.
    """
    created = 0
    linked = 0
    for raw in tags or ():
        classified = classify_tag(raw) if isinstance(raw, str) else None
        if classified is None:
            continue
        namespace, name = classified
        existed = db.query_one(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        )
        tag_id = ensure_tag(db, namespace, name, now=now)
        if existed is None:
            created += 1
        if link_tag(db, tag_id, target_type, target_id, now=now):
            linked += 1
    return {"created": created, "linked": linked}


def backfill_tags(db: "Database", *, now: float | None = None) -> dict:
    """Re-ingest ``memory_items.tags_json`` into ``tags``/``tag_links``.

    Walks every non-deleted ``memory_items`` row, parses ``tags_json``
    (malformed JSON counts as ``[]``) and syncs the tags onto the memory
    item.  Idempotent: existing links are not re-counted.  Returns
    ``{"items": <scanned rows>, "tags": <non-empty tag strings>,
    "linked": <newly created links>}``.
    """
    rows = db.query(
        "SELECT id, tags_json FROM memory_items WHERE status <> 'deleted'"
    )
    items = 0
    tag_total = 0
    linked = 0
    for row in rows:
        items += 1
        try:
            raw_tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, ValueError):
            raw_tags = []
        if not isinstance(raw_tags, list):
            raw_tags = []
        for entry in raw_tags:
            if isinstance(entry, str) and entry.strip():
                tag_total += 1
        result = sync_tags(db, raw_tags, "memory", str(row["id"]), now=now)
        linked += int(result["linked"])
    return {"items": items, "tags": tag_total, "linked": linked}
