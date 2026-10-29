"""1-hop entity edges: co-mention indexing + graph-backed recall expansion.

ADR-0003 §4 召回表第 6 行（「后来呢」→ 1 跳 ``entity_edges``）的最便宜实现，
思路来自 ActionRAG（arXiv:2610.32752）的「图修正初始相似度」：直接命中只有
词面重叠，相关事实之间靠实体关系相连，命中后沿边走一步把邻居实体的 detail
带回来。

写侧（索引）：
- 一条 detail 的文本/原话里共同出现的实体两两写一条 ``entity_edges``
  （relation=``co_mention``，``evidence_detail_id`` 指回该 detail）——
  边是证据，不是结论，所以同一对实体在几个 detail 里出现就留几行；
- 顺带写 detail 级 ``entity_mentions``（source_type='detail'），作为
  「detail ↔ 实体」的反查索引，召回侧据此取邻居实体的 detail。

读侧（召回，:func:`expand_entity_neighbors`）：
- 锚点 = 直接命中的 detail 的实体（detail 级 mention 优先，缺失时按文本
  兜底识别，回填前的旧库也能用）；
- 沿 1 跳边找邻居实体（家族折叠，过期边/低置信边不参与），取邻居的
  detail 标记 ``via_graph`` 返回，调用方按「不是独立命中」渲染。

边界原则：宁漏记不错归——群聊同现的边（置信 0.4）默认低于召回阈值
（0.5）不参与扩散；实体合并后旧 id 的边与 mention 经家族折叠仍可达；
测试 session 的数据一步都不进；写入只增不改，全部幂等。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..db import Database
from .details import _is_test_session
from .entities import (
    _PREFIX_GUARD_CHARS,
    _PREFIX_GUARD_SURFACES,
    _SURFACE_TO_ENTITY,
    _mention_confidence,
    _scan_surfaces,
    entity_family_ids,
    record_mention,
    resolve_entity_id,
    session_scope,
)

#: 同一条 detail 里共同出现的实体之间的关系。
CO_MENTION_RELATION = "co_mention"

#: 召回侧的最低边置信度。群聊同现（0.4）默认进不了扩散，直接会话（0.8）可以。
GRAPH_MIN_CONFIDENCE = 0.5

DEFAULT_MAX_EXTRA = 3
DEFAULT_PER_NEIGHBOR = 2

#: detail 级 mention 的 source_type。
_DETAIL_SOURCE = "detail"


def _person_card_surfaces(db: Database) -> list[tuple[str, list[str]]]:
    """Person-card name surfaces (canonical first, then aliases, ≥2 chars).

    已知三张卡的常用称呼已由 ``_scan_surfaces`` 覆盖；这里补齐自定义
    canonical 名与人物卡别名。1 字名不参与：一个「蓝」字会命中所有含蓝
    文本，宁漏记不错归。
    """
    cards: list[tuple[str, list[str]]] = []
    rows = db.query(
        "SELECT id, canonical_name, aliases_json FROM entities "
        "WHERE kind='person' AND merged_into IS NULL ORDER BY id"
    )
    for row in rows:
        names: list[str] = []
        canonical = str(row["canonical_name"] or "").strip()
        if len(canonical) >= 2:
            names.append(canonical)
        for raw in _load_aliases(row["aliases_json"]):
            alias = raw.strip()
            if len(alias) >= 2 and alias not in names:
                names.append(alias)
        if names:
            cards.append((str(row["id"]), names))
    return cards


def _load_aliases(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def match_detail_entities(
    db: Database,
    text: str,
    *,
    scope: str = "direct",
    now: float | None = None,
) -> list[tuple[str, str, float]]:
    """Match known entities in one detail text.

    Returns (entity_id, surface, confidence), deduplicated per
    (entity_id, surface).  Person-card names claim their spans first (a full
    card name is more specific than a known nickname it contains — 「蓝姐姐」
    is not 姐姐), then the entity layer's known surfaces run with their own
    quote/prefix guards, dropping hits fully inside a claimed card span.
    Known mappings stay authoritative: a card whose name equals a known
    surface never overrides it.  ``role`` stays empty because a detail has no
    reliable speaker, so 「我」 is never attributed here.
    """
    text = str(text or "")
    if not text.strip():
        return []
    matched: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()
    claimed: list[tuple[int, int]] = []
    guard_suffixes = tuple(_PREFIX_GUARD_SURFACES)
    for entity_id, names in _person_card_surfaces(db):
        for name in names:
            if name in _SURFACE_TO_ENTITY:
                continue
            start = text.find(name)
            while start != -1:
                end = start + len(name)
                guarded_prefix = (
                    name in _PREFIX_GUARD_SURFACES or name.endswith(guard_suffixes)
                ) and start > 0 and text[start - 1] in _PREFIX_GUARD_CHARS
                overlaps = any(
                    start < busy_end and busy_start < end
                    for busy_start, busy_end in claimed
                )
                if not guarded_prefix and not overlaps:
                    claimed.append((start, end))
                    seen.add((entity_id, name))
                    matched.append(
                        (entity_id, name, _mention_confidence(entity_id, scope))
                    )
                start = text.find(name, start + 1)
    for surface, entity_id, start, end, confidence in _scan_surfaces(text, "", scope):
        if any(start < busy_end and busy_start < end for busy_start, busy_end in claimed):
            continue
        key = (entity_id, surface)
        if key in seen:
            continue
        seen.add(key)
        matched.append((entity_id, surface, confidence))
    return matched


def record_detail_mentions(
    db: Database, detail_rows: list[Any], *, now: float | None = None
) -> int:
    """Write detail-level entity mentions; idempotent, returns new-row count.

    Spans are only meaningful inside ``text`` (the fact sentence); quote-only
    matches get NULL spans — the verbatim anchor already lives on the detail
    row itself (``quote_turn_id``), and the mention's turn stays NULL because
    a synthesized fact sentence cannot be pinned to one turn.
    """
    now_value = time.time() if now is None else float(now)
    written = 0
    for row in detail_rows or []:
        detail_id = str(row["id"] or "")
        session_id = str(row["session_id"] or "")
        if not detail_id or _is_test_session(session_id):
            continue
        scope = session_scope(session_id)
        seen: set[tuple[str, str]] = set()
        for chunk, with_spans in (
            (str(row["text"] or ""), True),
            (str(row["quote_text"] or ""), False),
        ):
            if not chunk:
                continue
            for entity_id, surface, confidence in match_detail_entities(
                db, chunk, scope=scope
            ):
                key = (entity_id, surface)
                if key in seen:
                    continue
                seen.add(key)
                char_start = char_end = None
                if with_spans:
                    index = chunk.find(surface)
                    if index >= 0:
                        char_start, char_end = index, index + len(surface)
                if record_mention(
                    db,
                    entity_id,
                    _DETAIL_SOURCE,
                    detail_id,
                    surface,
                    turn_id=None,
                    char_start=char_start,
                    char_end=char_end,
                    confidence=confidence,
                    now=now_value,
                ):
                    written += 1
    return written


def write_edges_for_details(
    db: Database, detail_rows: list[Any], *, now: float | None = None
) -> int:
    """Write one co_mention edge per entity pair per detail; idempotent.

    Pair direction is canonical (sorted by entity id) so the same pair never
    lands twice as A-B and B-A; identity is (src, dst, relation,
    evidence_detail_id), so several details co-mentioning the same pair keep
    several evidence rows.  Confidence is the weaker of the two mentions.
    """
    now_value = time.time() if now is None else float(now)
    written = 0
    for row in detail_rows or []:
        detail_id = str(row["id"] or "")
        session_id = str(row["session_id"] or "")
        if not detail_id or _is_test_session(session_id):
            continue
        scope = session_scope(session_id)
        confidences: dict[str, float] = {}
        for chunk in (str(row["text"] or ""), str(row["quote_text"] or "")):
            if not chunk:
                continue
            for entity_id, _surface, confidence in match_detail_entities(
                db, chunk, scope=scope
            ):
                if entity_id not in confidences or confidence > confidences[entity_id]:
                    confidences[entity_id] = confidence
        if len(confidences) < 2:
            continue
        ids = sorted(confidences)
        for index, src in enumerate(ids[:-1]):
            for dst in ids[index + 1:]:
                existing = db.query_one(
                    "SELECT id FROM entity_edges WHERE src_id=? AND dst_id=? "
                    "AND relation=? AND evidence_detail_id=?",
                    (src, dst, CO_MENTION_RELATION, detail_id),
                )
                if existing is not None:
                    continue
                db.execute(
                    "INSERT INTO entity_edges(src_id, dst_id, relation, valid_from, "
                    "valid_to, confidence, evidence_detail_id, created_at) "
                    "VALUES (?,?,?,NULL,NULL,?,?,?)",
                    (
                        src,
                        dst,
                        CO_MENTION_RELATION,
                        min(confidences[src], confidences[dst]),
                        detail_id,
                        now_value,
                    ),
                )
                written += 1
    return written


def backfill_edges(db: Database, *, now: float | None = None) -> dict[str, int]:
    """Backfill detail mentions + co-mention edges for all archived details.

    Idempotent (only adds); test-session details are skipped, mirroring the
    archive path.  This is what turns ``entity_edges`` on for an existing
    library — the CLI ``memory backfill`` command runs it.
    """
    now_value = time.time() if now is None else float(now)
    rows = db.query(
        "SELECT id, session_id, text, quote_text FROM event_details "
        "ORDER BY created_at, id"
    )
    scanned = skipped_test = 0
    mentions = edges = 0
    for row in rows:
        if _is_test_session(str(row["session_id"] or "")):
            skipped_test += 1
            continue
        scanned += 1
        mentions += record_detail_mentions(db, [row], now=now_value)
        edges += write_edges_for_details(db, [row], now=now_value)
    return {
        "details": scanned,
        "mentions": mentions,
        "edges": edges,
        "skipped_test": skipped_test,
    }


def sync_session_edges(db: Database, session: Any, *, now: float | None = None) -> dict[str, int]:
    """Index edges for one archived session's details (nightly hook)."""
    now_value = time.time() if now is None else float(now)
    empty = {"details": 0, "mentions": 0, "edges": 0, "skipped_test": 0}
    session_id = str(getattr(session, "session_id", "") or "")
    if not session_id or _is_test_session(session_id):
        return empty
    rows = db.query(
        "SELECT id, session_id, text, quote_text FROM event_details "
        "WHERE session_id=? ORDER BY created_at, id",
        (session_id,),
    )
    mentions = edges = 0
    for row in rows:
        mentions += record_detail_mentions(db, [row], now=now_value)
        edges += write_edges_for_details(db, [row], now=now_value)
    return {
        "details": len(rows),
        "mentions": mentions,
        "edges": edges,
        "skipped_test": 0,
    }


def _canonical_names(db: Database, entity_ids: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for entity_id in set(entity_ids):
        row = db.query_one(
            "SELECT canonical_name FROM entities WHERE id=?", (entity_id,)
        )
        if row is not None:
            names[entity_id] = str(row["canonical_name"] or entity_id)
    return names


def expand_entity_neighbors(
    db: Database,
    anchors: list[dict[str, Any]],
    existing: list[dict[str, Any]] | None,
    *,
    time_start: float | None = None,
    time_end: float | None = None,
    max_extra: int = DEFAULT_MAX_EXTRA,
    per_neighbor: int = DEFAULT_PER_NEIGHBOR,
    min_confidence: float = GRAPH_MIN_CONFIDENCE,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Ride in neighbor-entity details along 1-hop co-mention edges.

    ``anchors`` are the directly matched detail rows; ``existing`` is every
    row already in the result (direct hits + same-event context) so nothing
    is duplicated.  Each returned row is a copy of an ``event_details`` row
    flagged ``via_graph=True`` with ``graph_via`` = "锚点名 ↔ 邻居名" — the
    prompt renders it as related context, never as an independent hit.
    """
    now_value = time.time() if now is None else float(now)
    if not anchors or max_extra <= 0 or per_neighbor <= 0:
        return []
    existing_ids = {str(row["id"]) for row in existing or []}
    existing_ids.update(str(row["id"]) for row in anchors)

    # 1) anchor entities: detail mentions first; fall back to matching the
    #    anchor text so an un-backfilled library still expands (read-only).
    anchor_entities: list[str] = []
    anchor_of_family: dict[str, str] = {}
    for row in anchors:
        anchor_id = str(row.get("id") or "")
        if not anchor_id:
            continue
        scope = session_scope(str(row.get("session_id") or ""))
        found = [
            str(m["entity_id"])
            for m in db.query(
                "SELECT DISTINCT entity_id FROM entity_mentions "
                f"WHERE source_type='{_DETAIL_SOURCE}' AND source_id=?",
                (anchor_id,),
            )
        ]
        if not found:
            for chunk in (str(row.get("text") or ""), str(row.get("quote_text") or "")):
                found.extend(
                    entity_id
                    for entity_id, _surface, _confidence in match_detail_entities(
                        db, chunk, scope=scope
                    )
                )
        for entity_id in found:
            live = resolve_entity_id(db, entity_id)
            if live is None or live in anchor_entities:
                continue
            anchor_entities.append(live)
            for family_id in entity_family_ids(db, live):
                anchor_of_family[family_id] = live
    if not anchor_entities:
        return []
    anchor_set = set(anchor_entities)

    # 2) 1-hop edges over the whole anchor family; expired and low-confidence
    #    edges stay out (宁漏记不错归).
    family_ids = list(anchor_of_family)
    placeholders = ",".join("?" for _ in family_ids)
    edge_rows = db.query(
        "SELECT src_id, dst_id, relation, confidence FROM entity_edges "
        "WHERE confidence >= ? "
        "AND (valid_from IS NULL OR valid_from <= ?) "
        "AND (valid_to IS NULL OR valid_to > ?) "
        f"AND (src_id IN ({placeholders}) OR dst_id IN ({placeholders})) "
        "ORDER BY id",
        (
            float(min_confidence),
            now_value,
            now_value,
            *family_ids,
            *family_ids,
        ),
    )
    neighbor_conf: dict[str, float] = {}
    neighbor_anchor: dict[str, str] = {}
    for edge in edge_rows:
        confidence = float(edge["confidence"])
        for local, other in (
            (str(edge["src_id"]), str(edge["dst_id"])),
            (str(edge["dst_id"]), str(edge["src_id"])),
        ):
            anchor_id = anchor_of_family.get(local)
            if anchor_id is None:
                continue
            live = resolve_entity_id(db, other)
            # 锚点实体彼此相连没有新信息，不扩散。
            if live is None or live in anchor_set:
                continue
            if confidence > neighbor_conf.get(live, -1.0):
                neighbor_conf[live] = confidence
                neighbor_anchor[live] = anchor_id
    if not neighbor_conf:
        return []

    names = _canonical_names(db, [*anchor_entities, *neighbor_conf])
    # 3) neighbor details via detail-level mentions (family-folded), time
    #    window applied, test sessions and already-present rows skipped.
    results: list[dict[str, Any]] = []
    ordered = sorted(neighbor_conf.items(), key=lambda item: (-item[1], item[0]))
    for neighbor_id, _confidence in ordered:
        if len(results) >= max_extra:
            break
        family = entity_family_ids(db, neighbor_id)
        placeholders = ",".join("?" for _ in family)
        sql = (
            "SELECT d.* FROM event_details AS d "
            "JOIN entity_mentions AS m ON m.source_type=? AND m.source_id=d.id "
            f"WHERE m.entity_id IN ({placeholders})"
        )
        params: list[Any] = [_DETAIL_SOURCE, *family]
        if time_start is not None:
            sql += " AND (d.occurred_at IS NULL OR d.occurred_at >= ?)"
            params.append(float(time_start))
        if time_end is not None:
            sql += " AND (d.occurred_at IS NULL OR d.occurred_at <= ?)"
            params.append(float(time_end))
        candidates = db.query(sql, params)
        candidates.sort(
            key=lambda row: (
                -float(row["importance"] or 0.0),
                -float(row["occurred_at"] or 0.0),
                str(row["id"]),
            )
        )
        picked = 0
        for candidate in candidates:
            if len(results) >= max_extra or picked >= per_neighbor:
                break
            detail_id = str(candidate["id"])
            if detail_id in existing_ids:
                continue
            if _is_test_session(str(candidate["session_id"] or "")):
                continue
            item = dict(candidate)
            item["context"] = False
            item["via_graph"] = True
            item["graph_via"] = (
                f"{names.get(neighbor_anchor[neighbor_id], '')} ↔ "
                f"{names.get(neighbor_id, '')}".strip(" ↔")
            )
            results.append(item)
            existing_ids.add(detail_id)
            picked += 1
    return results


def list_edges(db: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    """Edges joined with canonical names, newest first (CLI/debug view)."""
    rows = db.query(
        "SELECT e.id, e.src_id, e.dst_id, e.relation, e.confidence, e.valid_from, "
        "e.valid_to, e.evidence_detail_id, e.created_at, "
        "s.canonical_name AS src_name, t.canonical_name AS dst_name "
        "FROM entity_edges AS e "
        "JOIN entities AS s ON s.id = e.src_id "
        "JOIN entities AS t ON t.id = e.dst_id "
        "ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
        (int(limit),),
    )
    return [dict(row) for row in rows]
