"""Deterministic entity normalization for the memory hierarchy (ADR-0003).

ADR-0003 §1 adds an entity layer (``entities`` plus an ``entity_mentions``
log anchored to turn id and character offsets) and §3 defines the write
path: alias exact match first, then deterministic appellation
normalization ("主人哥哥" -> the owner entity).  This module implements
that deterministic, offline-safe subset for the current phase: a fixed set
of known person entities (owner / friend / self) recognized from a fixed
surface table, with role-aware handling of the first-person pronoun,
scope-aware (group vs direct) confidence for third-person attributions, and
conservative quote/prefix guards against misattribution.

Open-domain NER, embedding-based merge candidates and the ``merged_into``
resolution are deliberately left to a later phase; the schema fields are
already reserved and mentions are never rewritten once written.

Everything here is standard library only and safe to re-run: every write
is an idempotent upsert or a guarded merge.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from typing import Any, Iterable

from ..db import Database
from ..owner import (
    ROLE_FAMILY,
    ROLE_FRIEND,
    ROLE_OWNER,
    ROLE_TEACHER,
    friend_uin_from_binding,
    uin_from_binding,
)
from .details import _is_test_session, _turn_ids_for_events

OWNER_ENTITY_ID = "person:owner"
FRIEND_ENTITY_ID = "person:friend"
SELF_ENTITY_ID = "person:self"

# Known nickname surfaces -> entity id.  Matching always goes longest
# surface first, so "主人哥哥" claims the span before "主人" or "哥哥".
_SURFACE_TO_ENTITY: dict[str, str] = {
    "主人哥哥": OWNER_ENTITY_ID,
    "主人": OWNER_ENTITY_ID,
    "哥哥": OWNER_ENTITY_ID,
    "姐姐": FRIEND_ENTITY_ID,
    "鲸鲸": SELF_ENTITY_ID,
    "鲸鱼娘": SELF_ENTITY_ID,
    "大肥鱼": SELF_ENTITY_ID,
}

# (entity_id, kind, fallback canonical name).  The owner canonical name is
# overridden by owner_bindings.role='owner'.display_name when available.
_KNOWN_ENTITIES: tuple[tuple[str, str, str], ...] = (
    (OWNER_ENTITY_ID, "person", "主人哥哥"),
    (FRIEND_ENTITY_ID, "person", "姐姐"),
    (SELF_ENTITY_ID, "person", "鲸鲸"),
)

# Mention confidence values.  Direct chats attribute at full strength; in
# group chats owner/friend attributions are only weak evidence (a group
# member may be called 哥哥/主人 by others), so they are recorded at reduced
# confidence instead of being dropped - mentions are append-only evidence
# for later disambiguation, never deleted.
DIRECT_CONFIDENCE = 0.8
GROUP_ATTRIBUTION_CONFIDENCE = 0.4

# Prefix guard: these bare surfaces directly preceded by one of the guard
# chars form a different word ("小姐姐" is not 姐姐).  A targeted, extensible
# stoplist heuristic, not a general tokenizer; the full appellation
# "主人哥哥" is deliberately exempt from the guard.
_PREFIX_GUARD_SURFACES = frozenset({"哥哥", "姐姐", "主人"})
_PREFIX_GUARD_CHARS = frozenset({"小", "大", "老"})

# Quoted speech spans for the first-person quote guard: the common Chinese
# brackets plus curly and straight double quotes (unclosed delimiters simply
# do not match, which leaves the text unguarded - the safe direction).
_QUOTE_RE = re.compile(r"「[^「」]*」|『[^『』]*』|“[^“”]*”|‘[^‘’]*’|\"[^\"]*\"")


def _load_json_list(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _clean_aliases(aliases: Iterable[str] | None) -> list[str]:
    if not aliases:
        return []
    seen: set[str] = set()
    for alias in aliases:
        text = str(alias or "").strip()
        if text:
            seen.add(text)
    return sorted(seen)


def _turn_field(turn: Any, name: str) -> Any:
    """Read a turn field from a mapping row (sqlite3.Row / dict)."""
    try:
        return turn[name]
    except (TypeError, KeyError, IndexError):
        return getattr(turn, name, None)


def _owner_display_name(db: Database) -> str:
    """Live owner display name; the table may not exist yet (see service.party_names)."""
    try:
        row = db.query_one(
            "SELECT display_name FROM owner_bindings WHERE role='owner' LIMIT 1"
        )
    except Exception:
        return ""
    if row is None:
        return ""
    return str(row["display_name"] or "").strip()


def _friend_display_name(db: Database) -> str:
    """Live friend-slot display name; ``""`` when unbound or too early."""
    try:
        row = db.query_one(
            "SELECT display_name FROM owner_bindings WHERE role='friend' LIMIT 1"
        )
    except Exception:
        return ""
    if row is None:
        return ""
    return str(row["display_name"] or "").strip()


def _existing_canonical(db: Database, entity_id: str) -> str:
    try:
        row = db.query_one("SELECT canonical_name FROM entities WHERE id=?", (entity_id,))
    except Exception:
        return ""
    if row is None:
        return ""
    return str(row["canonical_name"] or "").strip()


def _ensure_known_entities(db: Database, *, now: float) -> tuple[dict[str, str], int]:
    """Upsert the three known entities; returns (canonical names, created count).

    A binding display name wins over an existing name (renaming the owner in
    the binding is the authoritative act); without a binding an existing
    canonical name is preserved so a manual rename is not undone by tonight's
    archive.
    """
    fallback = {entity_id: default for entity_id, _kind, default in _KNOWN_ENTITIES}
    for entity_id, live in (
        (OWNER_ENTITY_ID, _owner_display_name(db)),
        (FRIEND_ENTITY_ID, _friend_display_name(db)),
        (SELF_ENTITY_ID, ""),
    ):
        fallback[entity_id] = (
            live or _existing_canonical(db, entity_id) or fallback[entity_id]
        )
    names: dict[str, str] = {}
    created = 0
    for entity_id, kind, _default in _KNOWN_ENTITIES:
        canonical = fallback[entity_id]
        if upsert_entity(db, entity_id, kind, canonical, now=now):
            created += 1
        names[entity_id] = canonical
    return names, created


def ensure_known_entities(db: Database, *, now: float | None = None) -> dict[str, str]:
    """Idempotently upsert owner/friend/self; returns {entity_id: canonical_name}."""
    now_value = time.time() if now is None else float(now)
    names, _created = _ensure_known_entities(db, now=now_value)
    return names


def upsert_entity(
    db: Database,
    entity_id: str,
    kind: str,
    canonical_name: str,
    aliases: Iterable[str] | None = None,
    *,
    now: float | None = None,
) -> bool:
    """Insert the entity or merge aliases into it.  True only when newly created.

    ``first_seen_at`` is written once at creation; ``last_seen_at`` is bumped on
    every call.  When the canonical name changes, the previous name is kept as
    an alias so old attributions stay reachable.
    """
    now_value = time.time() if now is None else float(now)
    alias_list = _clean_aliases(aliases)
    existing = db.query_one(
        "SELECT canonical_name, aliases_json FROM entities WHERE id=?", (entity_id,)
    )
    if existing is None:
        db.execute(
            "INSERT INTO entities(id, kind, canonical_name, aliases_json, attrs_json, "
            "first_seen_at, last_seen_at, merged_into, created_at) "
            "VALUES (?,?,?,?,'{}',?,?,NULL,?)",
            (
                entity_id,
                kind,
                canonical_name,
                json.dumps(alias_list, ensure_ascii=False),
                now_value,
                now_value,
                now_value,
            ),
        )
        return True

    current_aliases = _load_json_list(existing["aliases_json"])
    merged_aliases = sorted(set(current_aliases) | set(alias_list))
    current_name = str(existing["canonical_name"] or "")
    if current_name and current_name != canonical_name:
        merged_aliases = sorted(set(merged_aliases) | {current_name})
    if current_name != canonical_name or merged_aliases != current_aliases:
        db.execute(
            "UPDATE entities SET canonical_name=?, aliases_json=?, last_seen_at=? WHERE id=?",
            (canonical_name, json.dumps(merged_aliases, ensure_ascii=False), now_value, entity_id),
        )
    else:
        db.execute(
            "UPDATE entities SET last_seen_at=? WHERE id=?", (now_value, entity_id)
        )
    return False


def record_mention(
    db: Database,
    entity_id: str,
    source_type: str,
    source_id: str,
    surface: str,
    turn_id: int | None = None,
    *,
    char_start: int | None = None,
    char_end: int | None = None,
    confidence: float = 0.8,
    now: float | None = None,
) -> bool:
    """Append one mention; True only when a new row was written.

    Deduplication relies on the schema UNIQUE(entity_id, source_type,
    source_id, surface, turn_id).  With ``turn_id=None`` SQLite treats NULLs
    as distinct in the unique index, so the existence check is done here.
    """
    now_value = time.time() if now is None else float(now)
    if turn_id is None:
        existing = db.query_one(
            "SELECT id FROM entity_mentions WHERE entity_id=? AND source_type=? "
            "AND source_id=? AND surface=? AND turn_id IS NULL",
            (entity_id, source_type, source_id, surface),
        )
        if existing is not None:
            return False
        cursor = db.execute(
            "INSERT INTO entity_mentions(entity_id, source_type, source_id, surface, "
            "turn_id, char_start, char_end, confidence, created_at) "
            "VALUES (?,?,?,?,NULL,?,?,?,?)",
            (entity_id, source_type, source_id, surface, char_start, char_end, confidence, now_value),
        )
    else:
        cursor = db.execute(
            "INSERT OR IGNORE INTO entity_mentions(entity_id, source_type, source_id, "
            "surface, turn_id, char_start, char_end, confidence, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                entity_id,
                source_type,
                source_id,
                surface,
                int(turn_id),
                char_start,
                char_end,
                confidence,
                now_value,
            ),
        )
    return int(cursor.rowcount or 0) > 0


# ------------------------------------------------ QQ Zone authors (T-0013)
# REBASE POINT (zcode-memory's module): everything in this section is purely
# additive -- no existing function changes. An author's card is keyed by QQ
# uin, so a nickname change merges as an alias instead of creating a second
# person; without a uin the nickname itself keys the card.
QZONE_AUTHOR_PREFIX = "person:qq:"
_FRIEND_SESSION_RE = re.compile(r"^[^:]+:private:([0-9]+)$")


def _bound_friend_uin(db: Database) -> str | None:
    """The friend-slot holder's uin from ``owner_bindings``, or ``None``.

    Delegates to the single parser in :mod:`fatfish.owner` (the audit's
    dedupe item) so the entity layer, runtime and catch-up cannot drift.
    """
    return friend_uin_from_binding(db)


def qzone_author_identity(
    db: Database, *, uin: str, name: str, scope: str
) -> tuple[str, str, str]:
    """``(entity_id, kind, canonical_name)`` for one QQ Zone author.

    ``scope`` maps onto the known people when possible: ``self`` -> the owner
    entity, the bound friend-slot holder -> the friend entity. Everyone else
    gets a ``person:qq:<uin>`` card (or ``person:nick:<name>`` when the uin is
    missing). Known entities keep their established canonical name -- the
    author nickname becomes an alias instead of overwriting it.
    """
    nickname = str(name or "").strip()
    author = str(uin or "").strip()
    if scope == "self":
        return OWNER_ENTITY_ID, "person", _owner_display_name(db) or "主人哥哥"
    if author and author == (_bound_friend_uin(db) or ""):
        return FRIEND_ENTITY_ID, "person", _friend_display_name(db) or "姐姐"
    if author.isdigit():
        canonical = nickname or f"QQ用户{author[-4:]}"
        return f"{QZONE_AUTHOR_PREFIX}{author}", "person", canonical
    safe = re.sub(r"\s+", "", nickname)[:24] or "未署名"
    return f"person:nick:{safe}", "person", nickname or "未署名"


def sync_qzone_author(
    db: Database,
    *,
    uin: str,
    name: str,
    scope: str,
    source_id: str,
    confidence: float = 0.8,
    now: float | None = None,
) -> dict[str, Any]:
    """Upsert a Zone author's entity and anchor one mention to the feed item.

    Idempotent: a replayed ingest bumps ``last_seen_at`` and writes no
    duplicate mention (``record_mention`` dedupes on entity/source/surface).
    """
    now_value = time.time() if now is None else float(now)
    entity_id, kind, canonical = qzone_author_identity(
        db, uin=uin, name=name, scope=scope
    )
    nickname = str(name or "").strip()
    aliases = [nickname] if nickname and nickname != canonical else None
    created = upsert_entity(db, entity_id, kind, canonical, aliases=aliases, now=now_value)
    mention = record_mention(
        db,
        entity_id,
        "qzone",
        str(source_id or ""),
        nickname or canonical,
        confidence=confidence,
        now=now_value,
    )
    return {
        "entity_id": entity_id,
        "entity": canonical,
        "created": created,
        "mention": mention,
    }


def session_scope(session_id: str) -> str:
    """Classify a session id as "group" or "direct".

    Rule: a case-insensitive ``group`` substring anywhere in the id marks a
    group chat (covers ``qq:group:123``, hypothetical ``irc:#group-lab`` and
    any other platform naming without a per-platform registry); everything
    else is direct.  The residual risk of the substring match is a direct id
    that happens to contain "group": it only lowers mention confidence and
    stops entities_json merging, and mentions are never deleted, so a wrong
    "group" verdict is cheap - while a wrong "direct" verdict would
    misattribute a group member's nickname to the owner, which is the
    highest-frequency real-world false positive this guards against.
    """
    return "group" if "group" in str(session_id or "").lower() else "direct"


def _mention_confidence(entity_id: str, scope: str) -> float:
    """Confidence for one mention under the session scope.

    direct: 0.8.  group: owner/friend attributions drop to 0.4 because group
    members address each other with the owner's nicknames; self surfaces keep
    0.8 because in a group chat the addressee of 鲸鲸/鲸鱼娘/大肥鱼 (and the
    assistant's own "我") is unambiguously the bot.
    """
    if scope == "group" and entity_id != SELF_ENTITY_ID:
        return GROUP_ATTRIBUTION_CONFIDENCE
    return DIRECT_CONFIDENCE


def _quote_spans(content: str) -> list[tuple[int, int]]:
    """Char spans of quoted speech (start inclusive, end exclusive)."""
    return [(match.start(), match.end()) for match in _QUOTE_RE.finditer(content)]


def _scan_surfaces(
    content: str, role: str, scope: str = "direct"
) -> list[tuple[str, str, int, int, float]]:
    """Find known surfaces in ``content``.

    Returns (surface, entity_id, start, end, confidence) with ``end``
    exclusive.  Surfaces are tried longest first; a match is skipped when it
    overlaps a span already claimed by a longer surface, so "主人哥哥" yields
    one owner mention and never a second "主人" match.

    Two per-occurrence guards apply:

    - Quote guard (first person only): a "我" hit inside quoted speech
      (「…」『…』“…”‘’"…") is dropped for every role - the words inside the
      quotes are someone else's voice, so the surrounding turn's role does
      not tell who "我" is.  Deliberately conservative: rather miss a quoted
      "我" than file it under the wrong person.  All other surfaces still
      match inside quotes (a quote that names 姐姐 does mention 姐姐).
    - Prefix guard: 哥哥/姐姐/主人 directly preceded by 小/大/老 is a
      different word ("小姐姐" is not 姐姐).  A targeted stoplist heuristic
      (extensible word list), not a general tokenizer; "主人哥哥" as a whole
      is exempt.

    Confidence: 0.8 in direct scopes; in group scopes owner/friend mentions
    drop to 0.4 (see :func:`_mention_confidence`).
    """
    surface_map = dict(_SURFACE_TO_ENTITY)
    if role == "assistant":
        surface_map["我"] = SELF_ENTITY_ID
    elif role == "user":
        surface_map["我"] = OWNER_ENTITY_ID
    quotes = _quote_spans(content)
    occupied: list[tuple[int, int]] = []
    hits: list[tuple[str, str, int, int, float]] = []
    for surface in sorted(surface_map, key=lambda item: (-len(item), item)):
        entity_id = surface_map[surface]
        start = content.find(surface)
        while start != -1:
            end = start + len(surface)
            guarded_prefix = (
                surface in _PREFIX_GUARD_SURFACES
                and start > 0
                and content[start - 1] in _PREFIX_GUARD_CHARS
            )
            in_quote = any(q_start < end and start < q_end for q_start, q_end in quotes)
            if (
                not any(start < busy_end and busy_start < end for busy_start, busy_end in occupied)
                and not guarded_prefix
                and not (in_quote and surface == "我")
            ):
                occupied.append((start, end))
                hits.append(
                    (surface, entity_id, start, end, _mention_confidence(entity_id, scope))
                )
            start = content.find(surface, start + 1)
    return hits


def _extract_from_turns(
    db: Database,
    turns: list[Any],
    source_type: str,
    source_id: str,
    *,
    now: float,
    scope: str = "direct",
) -> tuple[list[str], int, int]:
    """Core extractor; returns (canonical names found, new mentions, new entities).

    ``scope`` comes from :func:`session_scope` of the owning session and only
    shapes mention confidence (see :func:`_mention_confidence`); mentions are
    written in every scope.
    """
    names, created_entities = _ensure_known_entities(db, now=now)
    discovered: set[str] = set()
    new_mentions = 0
    for turn in turns or []:
        content = str(_turn_field(turn, "content") or "")
        if not content:
            continue
        role = str(_turn_field(turn, "role") or "")
        raw_turn_id = _turn_field(turn, "id")
        try:
            turn_id = int(raw_turn_id) if raw_turn_id is not None else None
        except (TypeError, ValueError):
            turn_id = None
        for surface, entity_id, char_start, char_end, confidence in _scan_surfaces(
            content, role, scope
        ):
            if record_mention(
                db,
                entity_id,
                source_type,
                source_id,
                surface,
                turn_id=turn_id,
                char_start=char_start,
                char_end=char_end,
                confidence=confidence,
                now=now,
            ):
                new_mentions += 1
            discovered.add(names[entity_id])
    return sorted(discovered), new_mentions, created_entities


def extract_from_turns(
    db: Database,
    turns: list[Any],
    source_type: str,
    source_id: str,
    *,
    now: float | None = None,
    scope: str = "direct",
) -> list[str]:
    """Scan turns for known surfaces, record mentions, return sorted canonical names.

    ``scope`` defaults to "direct"; pass :func:`session_scope`(session_id)
    when the owning session is known.
    """
    now_value = time.time() if now is None else float(now)
    names, _mentions, _entities = _extract_from_turns(
        db, turns, source_type, source_id, now=now_value, scope=scope
    )
    return names


def entities_for_query(db: Database, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Match entities whose canonical name or alias is a substring of ``query``.

    Hits are ordered by the length of the matched string (longest first) and
    returned as [{'id', 'canonical_name', 'kind'}].
    """
    text = str(query or "")
    if not text.strip():
        return []
    rows = db.query(
        "SELECT id, kind, canonical_name, aliases_json FROM entities "
        "WHERE merged_into IS NULL"
    )
    hits: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        candidates = [str(row["canonical_name"] or "")]
        candidates.extend(_load_json_list(row["aliases_json"]))
        matched = ""
        for name in candidates:
            name = name.strip()
            if name and name in text and len(name) > len(matched):
                matched = name
        if matched:
            hits.append(
                (
                    matched,
                    str(row["id"]),
                    {
                        "id": str(row["id"]),
                        "canonical_name": str(row["canonical_name"]),
                        "kind": str(row["kind"]),
                    },
                )
            )
    hits.sort(key=lambda item: (-len(item[0]), item[1]))
    return [item[2] for item in hits[: max(1, int(limit))]]


def _merge_entity_names(
    db: Database, source_type: str, source_id: str, names: Iterable[str], *, now: float
) -> int:
    """Union ``names`` into memory_items.entities_json; write only on change."""
    wanted = {str(name).strip() for name in names if str(name).strip()}
    if not wanted:
        return 0
    rows = db.query(
        "SELECT id, entities_json FROM memory_items WHERE source_type=? AND source_id=?",
        (source_type, source_id),
    )
    updated = 0
    for row in rows:
        current = set(_load_json_list(row["entities_json"]))
        if wanted <= current:
            continue
        merged = sorted(current | wanted)
        db.execute(
            "UPDATE memory_items SET entities_json=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), now, str(row["id"])),
        )
        updated += 1
    return updated


def archive_session_entities(db: Database, session: Any, *, now: float | None = None) -> dict[str, Any]:
    """Nightly-archive entry point: per-event and per-day entity extraction.

    Every event group's turns are extracted once at event granularity and the
    whole session's turns once more at episode granularity; the canonical
    names are merged into the matching memory_items.  Test sessions are
    skipped entirely.

    Group sessions (``session_scope(session_id) == "group"``) still record
    every mention, but the entities_json merge is skipped: in a group chat a
    哥哥/姐姐 hit may refer to another member, so the archive does not claim
    attribution for the owner there - the reduced-confidence mentions stay as
    evidence for later disambiguation.
    """
    now_value = time.time() if now is None else float(now)
    session_id = str(getattr(session, "session_id", "") or "")
    if _is_test_session(session_id):
        return {"skipped": "test_session", "mentions": 0, "entities": 0, "items_updated": 0}
    scope = session_scope(session_id)

    mentions = 0
    entities_created = 0
    items_updated = 0
    day_turns: list[Any] = []
    seen_day_turns: set[int] = set()

    for group in getattr(session, "event_groups", None) or []:
        turn_ids = _turn_ids_for_events(db, list(getattr(group, "event_ids", None) or []))
        if not turn_ids:
            continue
        placeholders = ",".join("?" for _ in turn_ids)
        turns = db.query(
            f"SELECT id, role, content, created_at FROM conversation_turns "
            f"WHERE id IN ({placeholders}) ORDER BY created_at, id",
            tuple(turn_ids),
        )
        if not turns:
            continue
        for row in turns:
            turn_key = int(row["id"])
            if turn_key not in seen_day_turns:
                seen_day_turns.add(turn_key)
                day_turns.append(row)
        group_names, group_mentions, group_entities = _extract_from_turns(
            db, turns, "event", str(getattr(group, "primary_event_id", "") or ""),
            now=now_value, scope=scope,
        )
        mentions += group_mentions
        entities_created += group_entities
        if scope != "group":
            items_updated += _merge_entity_names(
                db, "event_archive", str(getattr(group, "source_id", "") or ""), group_names, now=now_value
            )

    if day_turns:
        episode_id = str(getattr(session, "episode_id", "") or "")
        day_names, day_mentions, day_entities = _extract_from_turns(
            db, day_turns, "episode", episode_id, now=now_value, scope=scope
        )
        mentions += day_mentions
        entities_created += day_entities
        if scope != "group":
            items_updated += _merge_entity_names(db, "episode", episode_id, day_names, now=now_value)

    return {
        "mentions": mentions,
        "entities": entities_created,
        "items_updated": items_updated,
        "skipped": "",
    }


def backfill_entities(db: Database, *, now: float | None = None) -> dict[str, Any]:
    """Backfill entities for already-archived memory items; idempotent.

    Every non-deleted event_archive / episode memory item has its source turns
    re-extracted: mentions land twice (memory-level on the item, plus an
    event/episode-level anchor), and the item's entities_json is union-merged
    without rewriting unchanged rows.

    Group sessions (per ``session_scope`` of the item's session_id) still get
    both mention passes, but their entities_json merge is skipped - the
    archive must not claim owner attribution from group-chat nicknames.
    """
    now_value = time.time() if now is None else float(now)
    rows = db.query(
        "SELECT id, session_id, event_id, episode_id, source_type, source_id "
        "FROM memory_items WHERE status <> 'deleted' "
        "AND source_type IN ('event_archive','episode')"
    )
    scanned = 0
    mentions = 0
    items_updated = 0
    skipped_test = 0
    for row in rows:
        session_id = str(row["session_id"] or "")
        if _is_test_session(session_id):
            skipped_test += 1
            continue
        scope = session_scope(session_id)
        item_id = str(row["id"] or "")
        item_type = str(row["source_type"] or "")
        if item_type == "event_archive":
            anchor_id = str(row["event_id"] or "")
            anchor_column, second_type, second_id = "event_id", "event", anchor_id
        else:
            anchor_id = str(row["episode_id"] or "")
            anchor_column, second_type, second_id = "episode_id", "episode", anchor_id
        scanned += 1
        if not anchor_id or not item_id:
            continue
        turns = db.query(
            f"SELECT id, role, content, created_at FROM conversation_turns "
            f"WHERE {anchor_column}=? ORDER BY created_at, id",
            (anchor_id,),
        )
        if not turns:
            continue
        names, item_mentions, _entities = _extract_from_turns(
            db, turns, "memory", item_id, now=now_value, scope=scope
        )
        mentions += item_mentions
        _names, anchor_mentions, _entities2 = _extract_from_turns(
            db, turns, second_type, second_id, now=now_value, scope=scope
        )
        mentions += anchor_mentions
        if scope != "group":
            items_updated += _merge_entity_names(
                db, item_type, str(row["source_id"] or ""), names, now=now_value
            )
    return {
        "items": scanned,
        "mentions": mentions,
        "items_updated": items_updated,
        "skipped_test": skipped_test,
    }


# ------------------------------------------------- person cards (T-0057)
# A person card is one ``entities`` row (kind='person'):
#
#   * keyed ``person:qq:<uin>`` as soon as the account id is known -- the same
#     key QQ Zone authors already use -- so a nickname change only ever adds an
#     alias;
#   * keyed ``person:nick:<name>`` only while there is no uin (a name seen in a
#     mention or typed by the owner);
#   * ``attrs_json`` carries ``uin`` / ``relation`` / ``remark`` / ``source``;
#   * merging never moves or deletes a mention: the losing row keeps its
#     ``entity_mentions`` and points at the winner through ``merged_into``,
#     and every read path expands the family.
#
# The QQ uin only ever lives in the id and in ``attrs_json``.  It is never
# written to ``canonical_name`` / ``aliases_json``: ``entities_for_query``
# matches those against the user's query text, and that text can end up in a
# prompt.
PERSON_QQ_PREFIX = QZONE_AUTHOR_PREFIX
PERSON_NICK_PREFIX = "person:nick:"
#: Platform-stated sender identities are stronger evidence than a nickname
#: guess, but still below a human binding: 0.9.
SENDER_MENTION_CONFIDENCE = 0.9
_PLACEHOLDER_NAME_RE = re.compile(r"^QQ用户[0-9]{0,4}$")


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """Read a field from a sqlite3.Row / dict / object (same idea as _turn_field)."""
    try:
        value = row[name]
    except (TypeError, KeyError, IndexError):
        value = getattr(row, name, default)
    return default if value is None else value


def _clean_name(value: Any, limit: int = 64) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalise_uin(value: Any) -> str:
    """Digits-only account id, or ``""`` when the input is not a uin."""
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text if text.isdigit() else ""


def _placeholder_name(uin: str) -> str:
    return f"QQ用户{uin[-4:]}" if uin else "未署名"


def _is_placeholder_name(name: str) -> bool:
    return not name.strip() or bool(_PLACEHOLDER_NAME_RE.match(name.strip()))


def _load_attrs(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _person_nick_id(name: str) -> str:
    safe = re.sub(r"\s+", "", _clean_name(name))[:24] or "未署名"
    return f"{PERSON_NICK_PREFIX}{safe}"


def _uin_from_session_id(session_id: str) -> str:
    match = _FRIEND_SESSION_RE.match(str(session_id or "").strip())
    return match.group(1) if match else ""


def _entity_row(db: Database, entity_id: str) -> Any | None:
    if not entity_id:
        return None
    return db.query_one(
        "SELECT id, kind, canonical_name, aliases_json, attrs_json, "
        "first_seen_at, last_seen_at, merged_into FROM entities WHERE id=?",
        (str(entity_id),),
    )


def _merge_map(db: Database) -> dict[str, str | None]:
    """``{id: merged_into or None}`` for every entity (one query)."""
    rows = db.query("SELECT id, merged_into FROM entities")
    mapping: dict[str, str | None] = {}
    for row in rows:
        raw = row["merged_into"]
        mapping[str(row["id"])] = str(raw).strip() if raw is not None and str(raw).strip() else None
    return mapping


def _resolve_in_map(merge_map: dict[str, str | None], entity_id: str) -> str:
    current = str(entity_id or "").strip()
    if not current:
        return ""
    seen: set[str] = set()
    for _ in range(8):
        if current in seen or current not in merge_map:
            return current
        seen.add(current)
        target = merge_map.get(current)
        if not target:
            return current
        current = target
    # A cycle is corrupt data (merge_entities refuses to create one); stop at
    # the last id instead of looping forever.
    return current


def resolve_entity_id(db: Database, entity_id: str) -> str | None:
    """Follow ``merged_into`` to the live card; ``None`` when it does not exist."""
    key = str(entity_id or "").strip()
    if not key:
        return None
    merge_map = _merge_map(db)
    if key not in merge_map:
        return None
    return _resolve_in_map(merge_map, key)


def entity_family_ids(db: Database, entity_id: str) -> list[str]:
    """The live card plus every card merged into it (target first)."""
    merge_map = _merge_map(db)
    target = _resolve_in_map(merge_map, entity_id)
    if not target or target not in merge_map:
        return []
    family = [target]
    family.extend(
        sorted(
            row_id
            for row_id in merge_map
            if row_id != target and _resolve_in_map(merge_map, row_id) == target
        )
    )
    return family


def _entity_uin_from_row(row: Any) -> str:
    attrs = _load_attrs(_row_value(row, "attrs_json", "{}"))
    stored = _normalise_uin(attrs.get("uin"))
    if stored:
        return stored
    entity_id = str(_row_value(row, "id", ""))
    if entity_id.startswith(PERSON_QQ_PREFIX):
        return _normalise_uin(entity_id[len(PERSON_QQ_PREFIX):])
    return ""


def _entity_uin(db: Database, entity_id: str) -> str:
    row = _entity_row(db, entity_id)
    return _entity_uin_from_row(row) if row is not None else ""


def entity_for_uin(db: Database, uin: Any) -> str | None:
    """Live entity id for a QQ account id, or ``None``.

    The owner and friend slots win: a bound session's uin belongs to that
    person, never to a second ``person:qq:<uin>`` card.  Cards carrying the
    uin in ``attrs_json`` (a manually keyed card, or one that predates the
    uin-key convention) are found too, so the lookup does not depend on the id
    shape alone.
    """
    uin_text = _normalise_uin(uin)
    if not uin_text:
        return None
    try:
        rows = db.query("SELECT role, session_id FROM owner_bindings")
    except Exception:
        rows = []
    for row in rows:
        if _uin_from_session_id(str(row["session_id"] or "")) == uin_text:
            role = str(row["role"] or "").strip().lower()
            if role == "owner":
                return OWNER_ENTITY_ID
            if role == "friend":
                return FRIEND_ENTITY_ID
    keyed = PERSON_QQ_PREFIX + uin_text
    if _entity_row(db, keyed) is not None:
        return resolve_entity_id(db, keyed)
    for row in db.query(
        "SELECT id, attrs_json FROM entities WHERE merged_into IS NULL AND kind='person'"
    ):
        attrs = _load_attrs(row["attrs_json"])
        if _normalise_uin(attrs.get("uin")) == uin_text:
            return str(row["id"])
    return None


def _find_person_ids_by_name(db: Database, name: str) -> list[str]:
    """Live person ids whose canonical name or an alias equals ``name``."""
    wanted = _clean_name(name).lower()
    if not wanted:
        return []
    merge_map = _merge_map(db)
    found: list[str] = []
    for row in db.query(
        "SELECT id, kind, canonical_name, aliases_json FROM entities"
    ):
        if str(row["kind"] or "") != "person":
            continue
        names = [str(row["canonical_name"] or "")]
        names.extend(_load_json_list(row["aliases_json"]))
        if not any(_clean_name(candidate).lower() == wanted for candidate in names):
            continue
        resolved = _resolve_in_map(merge_map, str(row["id"]))
        if resolved and resolved not in found:
            found.append(resolved)
    return found


def _write_attrs(db: Database, entity_id: str, attrs: dict[str, Any], *, now: float) -> None:
    db.execute(
        "UPDATE entities SET attrs_json=?, last_seen_at=? WHERE id=?",
        (json.dumps(attrs, ensure_ascii=False, sort_keys=True), now, entity_id),
    )


def _mention_counts(db: Database) -> dict[str, int]:
    rows = db.query(
        "SELECT entity_id, COUNT(*) AS n FROM entity_mentions GROUP BY entity_id"
    )
    return {str(row["entity_id"]): int(row["n"]) for row in rows}


def entity_card(db: Database, entity_id: str) -> dict[str, Any] | None:
    """One live card with its merged-in family folded in; ``None`` if absent."""
    merge_map = _merge_map(db)
    target = _resolve_in_map(merge_map, entity_id)
    if not target or target not in merge_map:
        return None
    row = _entity_row(db, target)
    if row is None:
        return None
    family = [target] + sorted(
        row_id
        for row_id in merge_map
        if row_id != target and _resolve_in_map(merge_map, row_id) == target
    )
    counts = _mention_counts(db)
    attrs = _load_attrs(_row_value(row, "attrs_json", "{}"))
    uin = _entity_uin_from_row(row)
    return {
        "id": target,
        "kind": str(_row_value(row, "kind", "person")),
        "name": str(_row_value(row, "canonical_name", "")),
        "aliases": _load_json_list(_row_value(row, "aliases_json", "[]")),
        "attrs": attrs,
        "uin": uin,
        "relation": str(attrs.get("relation") or ""),
        "remark": str(attrs.get("remark") or ""),
        "source": str(attrs.get("source") or ""),
        "mentions": sum(counts.get(row_id, 0) for row_id in family),
        "first_seen_at": _row_value(row, "first_seen_at"),
        "last_seen_at": _row_value(row, "last_seen_at"),
        "merged_from": family[1:],
    }


def _card_lite(db: Database, entity_id: str) -> dict[str, Any] | None:
    card = entity_card(db, entity_id)
    if card is None:
        return None
    return {
        "id": card["id"],
        "name": card["name"],
        "aliases": card["aliases"],
        "uin": card["uin"],
        "mentions": card["mentions"],
    }


def create_person(
    db: Database,
    *,
    name: str = "",
    uin: Any = "",
    aliases: Iterable[str] | None = None,
    relation: str | None = None,
    remark: str | None = None,
    source: str = "manual",
    now: float | None = None,
) -> dict[str, Any]:
    """Create or refresh one person card.

    With a uin the card is keyed ``person:qq:<uin>`` (a bound uin resolves to
    the owner/friend card instead).  An existing name-only card with the same
    exact name is adopted -- and re-keyed through ``merge_entities`` when a uin
    is supplied -- because the uin is the authoritative key once known.
    Without a uin, an exact-name card is refreshed rather than duplicated.
    """
    now_value = time.time() if now is None else float(now)
    display = _clean_name(name)
    uin_text = _normalise_uin(uin)
    if not display and not uin_text:
        raise ValueError("name or uin is required")

    candidates = _find_person_ids_by_name(db, display) if display else []
    merged: list[str] = []

    bound = entity_for_uin(db, uin_text) if uin_text else None
    created = False
    if bound:
        entity_id = bound
        row = _entity_row(db, bound)
        if row is None:
            # A binding can exist before the card does; create the known
            # entities so the bound name wins over the nickname.
            _ensure_known_entities(db, now=now_value)
            row = _entity_row(db, bound)
        canonical = (
            str(_row_value(row, "canonical_name", ""))
            if row is not None
            else (display or _placeholder_name(uin_text))
        ) or (display or _placeholder_name(uin_text))
    elif uin_text:
        entity_id = PERSON_QQ_PREFIX + uin_text
        existing = _entity_row(db, entity_id)
        canonical = (
            str(_row_value(existing, "canonical_name", ""))
            if existing is not None
            else (display or _placeholder_name(uin_text))
        ) or (display or _placeholder_name(uin_text))
        # The re-key target must exist before placeholders can merge into it.
        created = upsert_entity(db, entity_id, "person", canonical, now=now_value)
        # Re-key name-only cards that are certainly this person (exact name,
        # no uin of their own).  Cards that already carry a different uin are
        # left alone and reported as merge candidates instead.
        for candidate in candidates:
            if candidate == entity_id:
                continue
            if _entity_uin(db, candidate):
                continue
            merge_entities(db, candidate, entity_id, now=now_value)
            merged.append(candidate)
    elif candidates:
        entity_id = candidates[0]
        row = _entity_row(db, entity_id)
        canonical = str(_row_value(row, "canonical_name", "")) or display
    else:
        entity_id = _person_nick_id(display)
        canonical = display

    alias_values = list(aliases or ())
    if display and display != canonical:
        alias_values.append(display)
    created = (
        upsert_entity(
            db, entity_id, "person", canonical, aliases=alias_values, now=now_value
        )
        or created
    )

    row = _entity_row(db, entity_id)
    attrs = _load_attrs(_row_value(row, "attrs_json", "{}")) if row is not None else {}
    if uin_text:
        attrs["uin"] = uin_text
    if relation is not None:
        attrs["relation"] = _clean_name(relation, 32)
    if remark is not None:
        attrs["remark"] = _clean_name(remark, 120)
    attrs.setdefault("source", str(source or "manual")[:16])
    _write_attrs(db, entity_id, attrs, now=now_value)

    others = [
        card
        for card in (
            _card_lite(db, candidate)
            for candidate in _find_person_ids_by_name(db, canonical)
        )
        if card is not None and card["id"] != resolve_entity_id(db, entity_id)
    ]
    return {
        "ok": True,
        "created": created,
        "entity": entity_card(db, entity_id),
        "merged": merged,
        "merge_candidates": others,
    }


def update_person(
    db: Database,
    entity_id: str,
    *,
    name: str | None = None,
    aliases_add: Iterable[str] | None = None,
    aliases_remove: Iterable[str] | None = None,
    relation: str | None = None,
    remark: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Rename / re-alias / re-annotate one live card.

    ``None`` leaves a field alone; an empty string clears ``relation`` /
    ``remark``.  A rename keeps the old name as an alias (``upsert_entity``
    does that), and the canonical name can never be removed from the aliases.
    """
    now_value = time.time() if now is None else float(now)
    target = resolve_entity_id(db, entity_id)
    row = _entity_row(db, target or "")
    if row is None:
        raise ValueError("entity_not_found")
    assert target is not None
    old_name = str(_row_value(row, "canonical_name", ""))
    canonical = old_name
    if name is not None:
        new_name = _clean_name(name)
        if not new_name:
            raise ValueError("name must not be empty")
        canonical = new_name
    current_aliases = _load_json_list(_row_value(row, "aliases_json", "[]"))
    merged_aliases = set(current_aliases)
    for alias in aliases_add or ():
        cleaned = _clean_name(alias)
        if cleaned:
            merged_aliases.add(cleaned)
    for alias in aliases_remove or ():
        cleaned = _clean_name(alias)
        if cleaned and cleaned != canonical:
            merged_aliases.discard(cleaned)
    if canonical != old_name and old_name:
        merged_aliases.add(old_name)
    merged_aliases.discard(canonical)
    # Written directly instead of through upsert_entity: that helper unions
    # aliases, and an alias removal must actually remove.
    db.execute(
        "UPDATE entities SET canonical_name=?, aliases_json=?, last_seen_at=? WHERE id=?",
        (
            canonical,
            json.dumps(sorted(merged_aliases), ensure_ascii=False),
            now_value,
            target,
        ),
    )
    attrs = _load_attrs(_row_value(row, "attrs_json", "{}"))
    if relation is not None:
        attrs["relation"] = _clean_name(relation, 32)
    if remark is not None:
        attrs["remark"] = _clean_name(remark, 120)
    _write_attrs(db, target, attrs, now=now_value)
    return {"ok": True, "entity": entity_card(db, target)}


def merge_entities(
    db: Database,
    source_id: str,
    target_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Merge ``source`` into ``target``: aliases and attrs move, mentions stay.

    ``source.merged_into`` becomes the tombstone every read path follows.
    Mentions are evidence and are therefore never moved or deleted -- the
    family fold in :func:`entity_card` / :func:`list_people` counts them on the
    surviving card.  Refuses self-merges, unknown cards and cycles.
    """
    now_value = time.time() if now is None else float(now)
    source = resolve_entity_id(db, source_id)
    target = resolve_entity_id(db, target_id)
    if source is None:
        raise ValueError("source_not_found")
    if target is None:
        raise ValueError("target_not_found")
    if source == target:
        return {
            "ok": True,
            "merged": False,
            "reason": "same_entity",
            "entity": entity_card(db, target),
        }
    if resolve_entity_id(db, target) == source:
        raise ValueError("merge_cycle")
    source_row = _entity_row(db, source)
    target_row = _entity_row(db, target)
    assert source_row is not None and target_row is not None

    source_name = str(_row_value(source_row, "canonical_name", ""))
    target_name = str(_row_value(target_row, "canonical_name", ""))
    canonical = target_name
    if _is_placeholder_name(target_name) and not _is_placeholder_name(source_name):
        canonical = source_name
    elif not target_name:
        canonical = source_name

    aliases = set(_load_json_list(_row_value(target_row, "aliases_json", "[]")))
    aliases.update(_load_json_list(_row_value(source_row, "aliases_json", "[]")))
    for name in (source_name, target_name):
        cleaned = _clean_name(name)
        if cleaned and cleaned != canonical:
            aliases.add(cleaned)
    aliases.discard(canonical)

    source_attrs = _load_attrs(_row_value(source_row, "attrs_json", "{}"))
    target_attrs = _load_attrs(_row_value(target_row, "attrs_json", "{}"))
    for key, value in source_attrs.items():
        if key not in target_attrs or target_attrs.get(key) in (None, ""):
            target_attrs[key] = value

    upsert_entity(
        db,
        target,
        str(_row_value(target_row, "kind", "person")),
        canonical,
        aliases=sorted(aliases),
        now=now_value,
    )
    _write_attrs(db, target, target_attrs, now=now_value)
    db.execute(
        "UPDATE entities SET merged_into=? WHERE id=? AND merged_into IS NULL",
        (target, source),
    )
    return {
        "ok": True,
        "merged": True,
        "source": source,
        "target": target,
        "entity": entity_card(db, target),
    }


def list_people(
    db: Database, *, query: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """Live person cards, newest activity first.

    ``query`` matches canonical names / aliases as a case-insensitive
    substring, or a uin when the query is all digits.  Merged-away cards are
    folded into their target (one line per person).
    """
    merge_map = _merge_map(db)
    counts = _mention_counts(db)
    needle = _clean_name(query).lower()
    wanted_uin = _normalise_uin(query)
    cards: list[dict[str, Any]] = []
    for row in db.query(
        "SELECT id, kind, canonical_name, aliases_json, attrs_json, "
        "first_seen_at, last_seen_at FROM entities WHERE kind='person'"
    ):
        entity_id = str(row["id"])
        if entity_id not in merge_map:
            continue
        target = _resolve_in_map(merge_map, entity_id)
        if target != entity_id:
            continue
        attrs = _load_attrs(row["attrs_json"])
        uin = _entity_uin_from_row(row)
        names = [str(row["canonical_name"] or "")]
        names.extend(_load_json_list(row["aliases_json"]))
        if needle:
            name_hit = any(needle in _clean_name(name).lower() for name in names if name)
            uin_hit = bool(wanted_uin) and wanted_uin == uin
            if not (name_hit or uin_hit):
                continue
        family = [entity_id] + sorted(
            row_id
            for row_id in merge_map
            if row_id != entity_id and _resolve_in_map(merge_map, row_id) == entity_id
        )
        cards.append(
            {
                "id": entity_id,
                "name": str(row["canonical_name"] or ""),
                "aliases": _load_json_list(row["aliases_json"]),
                "attrs": attrs,
                "uin": uin,
                "relation": str(attrs.get("relation") or ""),
                "remark": str(attrs.get("remark") or ""),
                "mentions": sum(counts.get(row_id, 0) for row_id in family),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "merged_from": family[1:],
            }
        )
    cards.sort(
        key=lambda card: (
            -(float(card["last_seen_at"] or 0.0)),
            -int(card["mentions"]),
            str(card["name"]),
        )
    )
    return cards[: max(1, int(limit))]


# --------------------------------------------------- prompt roster (T-0060)
# A small "who do you know" block for the per-turn dynamic prompt section.
# Prompt-facing on purpose: the output carries ONLY name / relation / remark /
# aliases.  No entity id, no uin -- the uin red line (see the person-card
# section header above) extends to everything a card contributes to a prompt.
ROSTER_REMARK_LIMIT = 60


def _clip_remark(text: str, limit: int = ROSTER_REMARK_LIMIT) -> str:
    """One roster remark: whitespace collapsed, clipped to ``limit`` chars."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def roster_for_prompt(
    db: Database, query_text: str = "", *, limit: int = 10
) -> list[dict[str, Any]]:
    """Prompt-safe people roster: query hits first, then a deterministic fill.

    Members are live ``kind='person'`` cards.  The query text picks the people
    the current message is about (:func:`entities_for_query`); any remaining
    slots are filled from the most-mentioned cards so an empty-ish query still
    shows a stable small circle.  The fallback ranking is
    ``(mentions desc, last_seen_at desc, id asc)`` -- fully deterministic, so
    the rendered block cannot jitter between turns and invalidate caching.

    The owner/friend/self cards are excluded on both paths: the stable prefix
    (【关系】) and the persona hint already describe them, and a stranger must
    never learn the owner's slot identity through this block.  Their
    auto-created ``person:qq:<uin>`` duplicates are excluded too, otherwise
    the owner shows up as "another person" in the roster.  Cards without a
    single mention normally never enter via the fallback (a freshly typed name
    is not automatically someone she knows); a **bound family/teacher** card
    is the exception -- the binding is explicit consent, so mum/teachers show
    up before their first message.  A query hit keeps any card reachable
    because the user named it explicitly.

    Returns at most ``limit`` dicts of ``{name, relation, remark, aliases}``
    with ``remark`` clipped to :data:`ROSTER_REMARK_LIMIT` characters.  No
    ``id`` and no ``uin`` field ever leaves this function.
    """
    limit = max(0, int(limit))
    if limit == 0:
        return []
    exclude = {OWNER_ENTITY_ID, FRIEND_ENTITY_ID, SELF_ENTITY_ID}
    # The owner/friend auto-created QQ cards duplicate the slot cards; keep
    # them out too or the roster shows "me" as another person.
    for role in (ROLE_OWNER, ROLE_FRIEND):
        uin = uin_from_binding(db, role)
        if uin:
            exclude.add(f"person:qq:{uin}")
    # A bound family/teacher is known even before they have a mention; without
    # this, a freshly bound mum stays invisible until her first message.
    bound_people: set[str] = set()
    try:
        bound_rows = db.query(
            "SELECT session_id FROM owner_bindings WHERE role IN (?, ?)",
            (ROLE_FAMILY, ROLE_TEACHER),
        )
    except Exception:  # the table may not exist very early
        bound_rows = []
    for row in bound_rows:
        uin = _uin_from_session_id(str(row["session_id"] or ""))
        if uin:
            bound_people.add(f"person:qq:{uin}")
    picked: dict[str, dict[str, Any]] = {}

    def _entry(entity_id: str) -> dict[str, Any] | None:
        card = entity_card(db, entity_id)
        if card is None:
            return None
        name = str(card.get("name") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "relation": str(card.get("relation") or "").strip(),
            "remark": _clip_remark(str(card.get("remark") or "")),
            "aliases": [
                alias for alias in (card.get("aliases") or []) if str(alias).strip()
            ],
        }

    text = str(query_text or "")
    if text.strip():
        # Over-fetch a little: hits outside the person kind or the known three
        # are dropped below.
        for hit in entities_for_query(db, text, limit=limit * 2):
            entity_id = str(hit.get("id") or "")
            if entity_id in exclude or str(hit.get("kind") or "") != "person":
                continue
            if entity_id in picked:
                continue
            entry = _entry(entity_id)
            if entry is not None:
                picked[entity_id] = entry

    if len(picked) < limit:
        counts = _mention_counts(db)
        merge_map = _merge_map(db)
        ranked: list[tuple[int, float, str]] = []
        for row in db.query(
            "SELECT id, last_seen_at FROM entities "
            "WHERE kind='person' AND merged_into IS NULL"
        ):
            entity_id = str(row["id"])
            if entity_id in exclude or entity_id in picked:
                continue
            family = [entity_id] + [
                row_id
                for row_id in merge_map
                if row_id != entity_id and _resolve_in_map(merge_map, row_id) == entity_id
            ]
            mentions = sum(counts.get(row_id, 0) for row_id in family)
            if mentions < 1 and entity_id not in bound_people:
                continue
            ranked.append((mentions, float(row["last_seen_at"] or 0.0), entity_id))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        for _mentions, _last_seen, entity_id in ranked:
            if len(picked) >= limit:
                break
            entry = _entry(entity_id)
            if entry is not None:
                picked[entity_id] = entry

    return list(picked.values())[:limit]


def duplicate_candidates(db: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    """Pairs of live person cards sharing an exact name.

    Only a suggestion list: two cards with the same nickname may be two
    people, so nothing is merged here.  The caller decides which pair is real
    and calls :func:`merge_entities`.
    """
    merge_map = _merge_map(db)
    by_name: dict[str, list[str]] = {}
    rows: dict[str, Any] = {}
    for row in db.query(
        "SELECT id, kind, canonical_name, aliases_json FROM entities WHERE kind='person'"
    ):
        entity_id = str(row["id"])
        if entity_id not in merge_map:
            continue
        target = _resolve_in_map(merge_map, entity_id)
        if target != entity_id:
            continue
        rows[entity_id] = row
        names = [str(row["canonical_name"] or "")]
        names.extend(_load_json_list(row["aliases_json"]))
        for name in {_clean_name(candidate).lower() for candidate in names if candidate}:
            if name:
                by_name.setdefault(name, []).append(entity_id)

    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, ids in sorted(by_name.items()):
        unique = sorted(set(ids))
        for index, first in enumerate(unique):
            for second in unique[index + 1:]:
                key = (first, second)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((first, second, name))
    pairs.sort(key=lambda item: (item[2], item[0], item[1]))
    result: list[dict[str, Any]] = []
    for first, second, name in pairs[: max(1, int(limit))]:
        first_row = rows.get(first)
        second_row = rows.get(second)
        result.append(
            {
                "shared": name,
                "a": {
                    "id": first,
                    "name": str(_row_value(first_row, "canonical_name", "")),
                    "uin": _entity_uin_from_row(first_row) if first_row is not None else "",
                },
                "b": {
                    "id": second,
                    "name": str(_row_value(second_row, "canonical_name", "")),
                    "uin": _entity_uin_from_row(second_row) if second_row is not None else "",
                },
            }
        )
    return result


def sync_binding_entities(db: Database, *, now: float | None = None) -> dict[str, Any]:
    """Mirror ``owner_bindings`` onto the owner/friend cards.

    The binding is authoritative for the display name and the uin; the card
    keeps its id (``person:owner`` / ``person:friend``) so every existing
    mention and memory label stays attached.  Idempotent.
    """
    now_value = time.time() if now is None else float(now)
    names, _created = _ensure_known_entities(db, now=now_value)
    result: dict[str, Any] = {}
    for role, entity_id in (("owner", OWNER_ENTITY_ID), ("friend", FRIEND_ENTITY_ID)):
        try:
            row = db.query_one(
                "SELECT session_id, display_name FROM owner_bindings WHERE role=? LIMIT 1",
                (role,),
            )
        except Exception:
            row = None
        if row is None:
            continue
        uin = _uin_from_session_id(str(row["session_id"] or ""))
        display = _clean_name(row["display_name"]) or names.get(entity_id, "")
        if display:
            upsert_entity(db, entity_id, "person", display, now=now_value)
        card = entity_card(db, entity_id)
        attrs = dict((card or {}).get("attrs") or {})
        if uin:
            attrs["uin"] = uin
        attrs["role"] = role
        attrs.setdefault("source", "binding")
        _write_attrs(db, entity_id, attrs, now=now_value)
        result[role] = {
            "id": entity_id,
            "name": display or (card or {}).get("name", ""),
            "uin": uin,
        }
    return result


def note_inbound_person(
    db: Database,
    *,
    session_id: str,
    uin: Any,
    name: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Refresh the sender's card after an inbound chat message (v13).

    Called by the runtime for platform messages that carry a sender identity.
    Deliberately light: one upsert plus at most one session-level mention
    (``turn_id=NULL``), so a chatty person does not grow the mention log by
    one row per message -- the per-message attribution already lives on
    ``conversation_turns.sender_id``.  Never invents a card from a uin alone:
    without a display name and without an existing card it does nothing.
    """
    now_value = time.time() if now is None else float(now)
    uin_text = _normalise_uin(uin)
    if not uin_text:
        return {"skipped": "no_uin"}
    display = _clean_name(name)
    target = entity_for_uin(db, uin_text)
    existing = _entity_row(db, target or "")
    if existing is None and not display:
        return {"skipped": "no_name"}
    if existing is None and target in {
        OWNER_ENTITY_ID,
        FRIEND_ENTITY_ID,
        SELF_ENTITY_ID,
    }:
        # A binding can exist before the card does; the bound name must win
        # over the sender's current nickname.
        _ensure_known_entities(db, now=now_value)
        existing = _entity_row(db, target or "")

    if target is None:
        target = PERSON_QQ_PREFIX + uin_text
    known = target in {OWNER_ENTITY_ID, FRIEND_ENTITY_ID, SELF_ENTITY_ID}
    if existing is not None:
        canonical = str(_row_value(existing, "canonical_name", "")) or (
            display or _placeholder_name(uin_text)
        )
    else:
        canonical = display or _placeholder_name(uin_text)
    aliases = [display] if display and display != canonical else []
    created = upsert_entity(
        db, target, "person", canonical, aliases=aliases, now=now_value
    )
    attrs = _load_attrs(_row_value(existing, "attrs_json", "{}")) if existing is not None else {}
    attrs["uin"] = uin_text
    if known:
        attrs["role"] = "owner" if target == OWNER_ENTITY_ID else (
            "friend" if target == FRIEND_ENTITY_ID else "self"
        )
    attrs.setdefault("source", "chat")
    _write_attrs(db, target, attrs, now=now_value)
    mention = record_mention(
        db,
        target,
        "session",
        str(session_id or ""),
        display or canonical,
        turn_id=None,
        confidence=SENDER_MENTION_CONFIDENCE,
        now=now_value,
    )
    return {
        "entity_id": target,
        "name": canonical,
        "created": created,
        "mention": mention,
    }


def resolve_person_selector(db: Database, text: str) -> str:
    """Resolve ``id | uin | 姓名 | 别名`` to one live entity id (T-0057 P2).

    Shared by the CLI ``person`` commands and the web ``/api/people``
    endpoints.  Resolution order:

    1. exact entity id (``merged_into`` is followed to the live card);
    2. a pure-digit uin (the owner/friend binding wins, see
       :func:`entity_for_uin`);
    3. an exact canonical-name or alias match among live cards.

    Raises ``ValueError("empty_selector")`` on blank input,
    ``ValueError("person_not_found: <text>")`` when nothing matches and
    ``ValueError("ambiguous_selector: id1, id2")`` when several cards share
    the requested name/alias (the caller decides which one is real).
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty_selector")
    direct = resolve_entity_id(db, raw)
    if direct:
        return direct
    if raw.isdigit():
        found = entity_for_uin(db, raw)
        if found:
            return found
    matches = [
        card["id"]
        for card in list_people(db, query=raw, limit=20)
        if card["name"] == raw or raw in card["aliases"]
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"person_not_found: {raw}")
    raise ValueError("ambiguous_selector: " + ", ".join(matches[:5]))


def resolve_person_target(db: Database, name: str) -> dict[str, Any]:
    """Resolve one person-card target for an action (T-0072 ``read_chat`` focus).

    A thin, never-raising wrapper over :func:`resolve_person_selector` plus
    :func:`entity_card`: the same resolution order (entity id, uin, canonical
    name / alias) with no fuzzy matching of its own.  Returns

    * ``{"ok": True, "id", "name", "uin", "aliases", "relation"}`` on success;
    * ``{"ok": False, "error": <why>}`` on failure -- ``empty_selector``,
      ``person_not_found: <text>`` (with up to three ``suggestions`` from
      :mod:`difflib`, **suggestions only**: nothing is ever auto-selected) or
      ``ambiguous_selector: ...``.
    """
    raw = str(name or "").strip()
    if not raw:
        return {"ok": False, "error": "empty_selector"}
    try:
        entity_id = resolve_person_selector(db, raw)
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}
        if str(exc).startswith("person_not_found"):
            # Always present on not-found; an empty list means nothing was
            # close enough to suggest. Hints only -- never a pick.
            result["suggestions"] = _person_name_suggestions(db, raw)
        return result
    except Exception:  # a db hiccup is a refusal, never a wrong read
        return {"ok": False, "error": "resolve_failed"}
    card = entity_card(db, entity_id)
    if card is None:  # pragma: no cover - selector and card agree by construction
        return {"ok": False, "error": f"person_not_found: {raw}"}
    return {
        "ok": True,
        "id": card["id"],
        "name": card["name"],
        "uin": card["uin"],
        "aliases": list(card["aliases"]),
        "relation": card["relation"],
    }


def _person_name_suggestions(db: Database, name: str, *, limit: int = 3) -> list[str]:
    """Close matches over live card names/aliases; display hints, never picks."""
    needle = _clean_name(name).lower()
    if not needle:
        return []
    pool: dict[str, str] = {}  # lower -> original spelling
    for card in list_people(db, limit=200):
        for candidate in [card["name"], *(card.get("aliases") or [])]:
            cleaned = _clean_name(str(candidate or ""))
            if cleaned:
                pool.setdefault(cleaned.lower(), cleaned)
    if not pool:
        return []
    lowered = difflib.get_close_matches(needle, sorted(pool), n=limit, cutoff=0.6)
    return [pool[match] for match in lowered if match in pool][:limit]
