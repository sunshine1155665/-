"""Atomic memory details and their evidence links.

A summary answers "what was this about"; a detail answers "what exactly
happened".  Nightly turns event text into clause-sized rows and keeps the exact
substring plus the source turn id, so a later recall can reopen the original
sentence instead of trusting a paraphrase.

The extractor here is deliberately rule-based and deterministic.  An LLM can
be layered on later (same schema, same validation), but the default must work
offline and must never invent a quote: every ``quote_text`` is checked as an
exact substring of the source turn.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from ..db import Database
from .sessions import is_test_session

_CLAUSE = re.compile(r"[^。！？!?；;\n]+")
_EMOTION_WORDS = {
    "开心": ("开心", 3.0),
    "高兴": ("开心", 3.0),
    "好耶": ("开心", 4.0),
    "笑": ("开心", 2.0),
    "喜欢": ("喜欢", 3.0),
    "爱": ("喜欢", 3.0),
    "想你": ("想念", 2.5),
    "累": ("疲惫", 3.0),
    "困": ("疲惫", 2.5),
    "难过": ("难过", 3.5),
    "委屈": ("委屈", 4.0),
    "生气": ("生气", 4.0),
    "烦": ("烦躁", 3.0),
    "怕": ("害怕", 3.0),
    "被骂": ("委屈", 4.0),
    "晚安": ("温柔", 1.5),
    "抱抱": ("温柔", 2.0),
}
_PLAN_WORDS = ("明天", "下次", "以后", "打算", "准备", "回头", "周末")
_DECISION_WORDS = ("决定了", "不要了", "不再", "就这么", "说定了")
_CHANGE_WORDS = ("开始", "变成", "不再", "以前", "现在")
_KIND_BY_PREFIX = {
    "plan": "plan",
    "decision": "decision",
    "change": "change",
}


def _is_test_session(session_id: str) -> bool:
    """Backward-compatible alias; the shared list lives in ``sessions.py``."""
    return is_test_session(session_id)


def _clauses(text: str) -> list[str]:
    chunks: list[str] = []
    for raw in _CLAUSE.findall(str(text or "")):
        clause = raw.strip()
        if len(clause) > 160:
            for part in re.split(r"[，,、]", clause):
                part = part.strip()
                if 4 <= len(part) <= 160:
                    chunks.append(part)
        elif 4 <= len(clause) <= 160:
            chunks.append(clause)
    return chunks


def _emotion(text: str) -> tuple[str | None, float]:
    best_label, best_score = None, 0.0
    for word, (label, score) in _EMOTION_WORDS.items():
        if word in text and score > best_score:
            best_label, best_score = label, score
    return best_label, best_score


def _kind(text: str) -> str:
    if any(word in text for word in _PLAN_WORDS):
        return "plan"
    if any(word in text for word in _DECISION_WORDS):
        return "decision"
    if any(word in text for word in _CHANGE_WORDS):
        return "change"
    label, _ = _emotion(text)
    return "emotion" if label else "fact"


def _hash(*parts: object) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def extract_details(
    event_id: str,
    turns: list[Any],
    *,
    session_id: str = "",
    episode_id: str = "",
    date_text: str = "",
) -> list[dict[str, Any]]:
    """Turn rows -> detail dicts.  Quotes are guaranteed exact substrings."""
    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    seq = 0
    for turn in turns:
        content = str(turn["content"] or "")
        turn_id = int(turn["id"])
        role = str(turn["role"] or "")
        occurred = float(turn["created_at"] or 0.0)
        for clause in _clauses(content):
            if clause not in content:
                continue
            kind = _kind(clause)
            label, intensity = _emotion(clause)
            detail_hash = _hash(event_id, turn_id, kind, clause)
            if detail_hash in seen:
                continue
            seen.add(detail_hash)
            details.append({
                "id": uuid.uuid4().hex,
                "event_id": event_id,
                "episode_id": episode_id or None,
                "session_id": session_id,
                "seq": seq,
                "kind": kind,
                "subject": role,
                "predicate": "",
                "object": "",
                "text": clause,
                "speaker": role,
                "quote_turn_id": turn_id,
                "quote_text": clause,
                "quote_hash": _hash(clause),
                "occurred_at": occurred,
                "time_granularity": "minute",
                "time_confidence": 0.8,
                "valid_from": None,
                "valid_to": None,
                "emotion": label,
                "emotion_intensity": intensity or None,
                "importance": min(0.8, 0.35 + 0.08 * intensity),
                "confidence": 0.75,
                "source": "nightly-rules",
                "detail_hash": detail_hash,
                "created_at": time.time(),
            })
            seq += 1
    return details


def _turn_ids_for_events(db: Database, event_ids: list[str]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for event_id in event_ids:
        row = db.query_one(
            "SELECT turn_ids_json FROM conversation_events WHERE id=?", (event_id,)
        )
        if not row:
            continue
        try:
            values = json.loads(row["turn_ids_json"] or "[]")
        except json.JSONDecodeError:
            continue
        for value in values:
            try:
                turn_id = int(value)
            except (TypeError, ValueError):
                continue
            if turn_id not in seen:
                ids.append(turn_id)
                seen.add(turn_id)
    return ids


def _insert_details(db: Database, details: list[dict[str, Any]]) -> int:
    if not details:
        return 0
    columns = list(details[0])
    sql = (
        f"INSERT OR IGNORE INTO event_details ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})"
    )
    created = 0
    for row in details:
        cursor = db.execute(sql, tuple(row[c] for c in columns))
        created += max(0, int(cursor.rowcount or 0))
    return created


def archive_details(db: Database, session: Any, *, now: float | None = None) -> dict[str, Any]:
    """Extract details for every event group in one archived session."""
    now = time.time() if now is None else float(now)
    if _is_test_session(getattr(session, "session_id", "")):
        return {"created": 0, "events": 0, "skipped": "test_session"}

    created = 0
    events = 0
    for group in getattr(session, "event_groups", []):
        turn_ids = _turn_ids_for_events(db, list(group.event_ids))
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
        content_hash = _hash(
            session.session_id,
            *[f"{turn['id']}:{turn['content']}" for turn in turns],
        )
        run_id = f"{session.date_text}:{session.session_id}:{group.source_id}"
        existing = db.query_one(
            "SELECT content_hash FROM archive_runs WHERE id=?", (run_id,)
        )
        if existing and str(existing["content_hash"]) == content_hash:
            continue
        details = extract_details(
            group.primary_event_id,
            turns,
            session_id=session.session_id,
            episode_id=session.episode_id,
            date_text=session.date_text,
        )
        created += _insert_details(db, details)
        db.execute(
            "INSERT INTO archive_runs(id, event_id, content_hash, detail_count, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "content_hash=excluded.content_hash, detail_count=excluded.detail_count, "
            "created_at=excluded.created_at",
            (run_id, group.primary_event_id, content_hash, len(details), now),
        )
        events += 1
    return {"created": created, "events": events, "skipped": ""}


def _match_tokens(text: str) -> list[str]:
    """Cut a query into match tokens: ASCII runs whole, CJK runs as bigrams.

    ``re.findall(r"\\w+", ...)`` returns a whole CJK sentence as one run
    (there are no spaces and unicode61 has no CJK word boundaries either), so
    the run is cut into adjacent character pairs (n chars -> n-1 bigrams).
    Maximal ASCII stretches inside a mixed run stay one token each, a single
    character stands alone, and duplicates collapse keeping first position.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for run in re.findall(r"\w+", str(text or "")):
        if run.isascii():
            parts: list[str] = [run]
        else:
            parts = []
            for piece in re.findall(r"[0-9A-Za-z_]+|[^0-9A-Za-z_]+", run):
                if piece.isascii() or len(piece) == 1:
                    parts.append(piece)
                else:
                    parts.extend(piece[i:i + 2] for i in range(len(piece) - 1))
        for token in parts:
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
    return tokens


# Common address/name bigrams. A detail that only shares "主人/人哥/哥哥" with
# the query is about the same person, not the same event: without this guard,
# "主人哥哥答应姐姐看QQ空间了吗" also recalled "主人哥哥喜欢喝冰美式" because
# the three name bigrams alone cleared the coverage gate.  Names are removed
# from the *content* check only; they still count toward coverage/ranking.
_NAME_WORDS = (
    "主人哥哥",
    "主人",
    "哥哥",
    "姐姐",
    "妹妹",
    "鲸鲸",
    "鲸鱼娘",
    "鲸鱼",
    "大肥鱼",
    "大肥",
    "肥鱼",
    "宝宝",
)
_NAME_BIGRAMS = frozenset(
    word[index:index + 2]
    for word in _NAME_WORDS
    for index in range(max(0, len(word) - 1))
)

# Pronouns/particles: a bigram containing one of these is connective tissue,
# not event content.  "姐是/是不/不是/她的" must not count as a content hit,
# or every chat fragment about the same person looks relevant.
_FUNCTION_CHARS = frozenset("的了是不有没在和就也都吗呢吧啊呀哦我你他她它们这那哪个")
_FUNCTION_WORDS = (
    "今天",
    "昨天",
    "明天",
    "上次",
    "那次",
    "那天",
    "之前",
    "以前",
    "当时",
    "现在",
    "最近",
    "什么",
    "怎么",
    "我们",
    "你们",
    "他们",
    "时候",
    "可以",
    "不是",
    "没有",
    "知道",
    "记得",
    "觉得",
    "如果",
    "但是",
    "因为",
    "所以",
    "然后",
    "后来",
)
_FUNCTION_BIGRAMS = frozenset(
    word[index:index + 2]
    for word in _FUNCTION_WORDS
    for index in range(max(0, len(word) - 1))
)
_STOP_BIGRAMS = _NAME_BIGRAMS | _FUNCTION_BIGRAMS


def _contains(token: str, text: str, quote: str, low_text: str, low_quote: str) -> bool:
    low = token.lower()
    return (
        token in text
        or token in quote
        or (low != token and (low in low_text or low in low_quote))
    )


def _like_pattern(token: str) -> str:
    """``%token%`` with LIKE wildcards escaped (pairs with ``ESCAPE '\\'``)."""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def has_content_overlap(query: str, text: str) -> bool:
    """True when ``query`` shares at least one content token with ``text``.

    Used by the privacy guard: a private memory may be sitting in the recall
    window for an unrelated question, and "it exists" must not be claimed just
    because *some* private hit was retrieved.  Names and function bigrams do
    not count (same person is not the same event).
    """
    content = [
        token
        for token in _match_tokens(query)
        if token not in _STOP_BIGRAMS
        and not any(char in _FUNCTION_CHARS for char in token)
    ]
    if not content:
        return False
    hay = str(text or "")
    return any(token in hay for token in content)


def search_details(
    db: Database,
    query: str,
    *,
    limit: int = 8,
    time_start: float | None = None,
    time_end: float | None = None,
    event_ids: list[str] | None = None,
    min_score: float = 0.2,
) -> list[dict[str, Any]]:
    """Detail-first search.  Returns the detail plus its event/day context.

    Matching is gram-LIKE, not FTS MATCH.  ``detail_fts`` (unicode61) stores a
    whole CJK run as a single token, so a natural-language query like
    「那天她说被批评了，后来我们聊了什么」 can never MATCH the stored clauses;
    the query is therefore cut with :func:`_match_tokens` (ASCII runs whole,
    CJK runs into adjacent bigrams) and candidates are recalled with one
    ``LIKE ... OR ...`` scan over ``text``/``quote_text``.  Gram-LIKE works
    equally for CJK and Latin, and at the current scale (hundreds of rows,
    time/event pre-filters applied first in SQL) the scan cost is negligible,
    so the FTS MATCH branch is dropped -- the ``detail_fts`` table and its
    triggers stay in place untouched for a future gram-tokenized index.

    Each candidate is scored by coverage: the share of query tokens found as a
    substring of ``text`` or ``quote_text`` (Latin compared lowercased).  A
    candidate is kept when ``coverage >= min_score``.  Long natural sentences
    dilute coverage with function-word bigrams (的/她/那), so any candidate
    sharing **two or more distinct** query tokens is always kept -- a single
    incidental shared token (「今天」) must clear the ratio, two content
    windows do not.  If the query contains any non-name token, a candidate
    that only shares address bigrams (主人/人哥/哥哥) is dropped: same person
    is not the same event.  There is no general stopword list yet; real
    queries will tell whether one is needed.  ``min_score <= 0`` keeps every
    candidate (gate off).  Results order by coverage, then importance, then
    recency.

    ``event_ids`` narrows the candidate scan to these events (entity
    pre-filter); ``None`` means no event filter, ``[]`` returns ``[]``
    immediately -- the caller decides whether to retry unfiltered.
    """
    tokens = _match_tokens(query)
    if not tokens:
        return []
    if event_ids is not None and not event_ids:
        return []
    where = ["1=1"]
    params: list[Any] = []
    if time_start is not None:
        where.append("(d.occurred_at IS NULL OR d.occurred_at >= ?)")
        params.append(float(time_start))
    if time_end is not None:
        where.append("(d.occurred_at IS NULL OR d.occurred_at <= ?)")
        params.append(float(time_end))
    if event_ids is not None:
        where.append(f"d.event_id IN ({','.join('?' for _ in event_ids)})")
        params.extend(str(event_id) for event_id in event_ids)
    like = " OR ".join(
        "(d.text LIKE ? ESCAPE '\\' OR d.quote_text LIKE ? ESCAPE '\\')"
        for _ in tokens
    )
    like_params: list[str] = []
    for token in tokens:
        pattern = _like_pattern(token)
        like_params.extend((pattern, pattern))
    rows = db.query(
        "SELECT d.*, e.title AS event_title, e.summary AS event_summary "
        "FROM event_details AS d "
        "LEFT JOIN conversation_events AS e ON e.id = d.event_id "
        "WHERE " + " AND ".join(where) + f" AND ({like})",
        (*params, *like_params),
    )
    threshold = float(min_score)
    content_tokens = [
        token
        for token in tokens
        if token not in _STOP_BIGRAMS
        and not any(char in _FUNCTION_CHARS for char in token)
    ]
    scored: list[tuple[float, float, float, dict[str, Any]]] = []
    for row in rows:
        if is_test_session(row["session_id"]):
            continue
        text = str(row["text"] or "")
        quote = str(row["quote_text"] or "")
        low_text, low_quote = text.lower(), quote.lower()
        hits = sum(
            1 for token in tokens
            if _contains(token, text, quote, low_text, low_quote)
        )
        content_hits = sum(
            1 for token in content_tokens
            if _contains(token, text, quote, low_text, low_quote)
        )
        # Sharing only a name ("主人哥哥") or connective tissue
        # ("姐是/是不/不是") means the same person, not the same event.  If the
        # query has any content token, at least one must hit.  ``min_score=0``
        # remains the explicit "gate off" mode and skips this too.
        if threshold > 0 and content_tokens and content_hits == 0:
            continue
        coverage = hits / len(tokens)
        if threshold > 0 and coverage < threshold and hits < 2:
            continue
        scored.append((
            -coverage,
            -float(row["importance"] or 0.0),
            -float(row["occurred_at"] or 0.0),
            row,
        ))
    scored.sort(key=lambda entry: entry[:3])
    return [dict(entry[3]) for entry in scored[: max(1, int(limit))]]


def expand_event_context(
    db: Database,
    rows: list[dict[str, Any]],
    *,
    per_event: int = 2,
    max_extra: int = 4,
) -> list[dict[str, Any]]:
    """Nearest sibling details from the event a matched detail came from.

    A memory question often matches only one clause: "鲸鲸今年多大了" hits the
    question, while the actual answer ("我记不清具体是几岁啦") is a separate
    detail in the same event and shares no query bigrams.  Without the sibling,
    the model sees the question but not the answer and defaults to a vague
    "记不清".  Siblings are explicitly marked ``context=True`` (and point back
    with ``context_for``) so the prompt can present them as surrounding
    conversation rather than as independent search hits.

    Only siblings of already-matched details ride along; the relevance gate is
    never weakened.  ``per_event`` and ``max_extra`` cap prompt growth.
    """
    if not rows or per_event <= 0 or max_extra <= 0:
        return []
    matched_ids = {str(row.get("id")) for row in rows}
    seen_events: set[str] = set()
    extras: list[dict[str, Any]] = []
    for parent in rows:
        event_id = str(parent.get("event_id") or "")
        if not event_id or event_id in seen_events:
            continue
        seen_events.add(event_id)
        siblings = db.query(
            "SELECT d.*, e.title AS event_title, e.summary AS event_summary "
            "FROM event_details AS d "
            "LEFT JOIN conversation_events AS e ON e.id = d.event_id "
            "WHERE d.event_id = ?",
            (event_id,),
        )
        parent_time = float(parent.get("occurred_at") or 0.0)
        parent_seq = int(parent.get("seq") or 0)
        ordered: list[tuple[float, float, Any]] = []
        for sibling in siblings:
            sibling_id = str(sibling["id"])
            if sibling_id in matched_ids:
                continue
            sibling_time = float(sibling["occurred_at"] or 0.0)
            distance = abs(sibling_time - parent_time) if sibling_time else 1e18
            ordered.append(
                (
                    distance,
                    abs(int(sibling["seq"] or 0) - parent_seq),
                    sibling,
                )
            )
        ordered.sort(key=lambda item: item[:2])
        for _, _, sibling in ordered[:per_event]:
            item = dict(sibling)
            item["context"] = True
            item["context_for"] = str(parent.get("id") or "")
            extras.append(item)
            matched_ids.add(str(item["id"]))
            if len(extras) >= max_extra:
                return extras
    return extras
