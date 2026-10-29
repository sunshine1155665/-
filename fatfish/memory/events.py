"""Incremental conversation event segmentation.

Turns are grouped into :class:`conversation_events` without any LLM call.
Segmentation is deliberately cheap and deterministic:

* a gap larger than ``memory.event_gap_minutes`` always starts a new event;
* a smaller gap starts a new event only when the topic drifted, measured as
  Chinese character-bigram Jaccard similarity against the last few turns;
* everything else is appended to the current open event.

``observe_turn`` is incremental and idempotent: a turn that already carries an
``event_id`` is returned as-is and never appended twice.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Iterable

from ..config import Config
from ..db import Database

_TITLE_LIMIT = 40
_SUMMARY_LIMIT = 300
_SUMMARY_TURN_CHARS = 90
_TOPIC_LIMIT = 12
_RECENT_TURNS_FOR_TOPIC = 3

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,30}|[\u4e00-\u9fff]+")
_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

_TOPIC_STOPWORDS = {
    "一个", "一些", "一下", "不是", "不能", "今天", "什么", "还是", "但是",
    "因为", "所以", "如果", "现在", "然后", "就是", "这个", "那个", "可以",
    "没有", "我们", "你们", "他们", "自己", "真的", "已经", "感觉", "觉得",
}

_IMPORTANT_WORDS = (
    "记住", "别忘", "重要", "承诺", "答应", "约定", "生日",
    "喜欢", "讨厌", "害怕", "第一次", "永远",
)


def _clean_text(text: Any) -> str:
    return " ".join(str(text or "").split())


def _first_line(text: Any) -> str:
    for line in str(text or "").splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return cleaned
    return ""


def _title_from(text: Any) -> str:
    """First meaningful line, capped so event titles stay compact."""
    return _first_line(text)[:_TITLE_LIMIT]


def _char_bigrams(text: Any) -> set[str]:
    """Chinese-friendly bigrams: drop punctuation/whitespace, then pair chars."""
    clean = _CLEAN_RE.sub("", str(text or "").lower())
    if not clean:
        return set()
    if len(clean) == 1:
        return {clean}
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _load_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _unique(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _topics_from(text: Any, limit: int = _TOPIC_LIMIT) -> list[str]:
    """Deterministic keyword-ish topics: latin words plus CJK words/bigrams."""
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    position = 0
    for run in _TOKEN_RE.findall(str(text or "")):
        if run.isascii():
            candidates = [run.lower()]
        elif len(run) <= 4:
            candidates = [run]
        else:
            candidates = [run[i : i + 2] for i in range(len(run) - 1)]
        for candidate in candidates:
            candidate = candidate.strip()
            if len(candidate) < 2 or candidate in _TOPIC_STOPWORDS:
                continue
            if candidate not in order:
                order[candidate] = position
                position += 1
            counts[candidate] = counts.get(candidate, 0) + 1
    ranked = sorted(counts, key=lambda word: (-counts[word], order[word]))
    return ranked[:limit]


def _merge_topics(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    return [str(topic) for topic in _unique([*existing, *incoming])][:_TOPIC_LIMIT]


def _event_importance(text: Any, turn_count: int) -> float:
    score = 0.35 + min(0.30, 0.03 * max(0, int(turn_count) - 1))
    if any(word in str(text or "") for word in _IMPORTANT_WORDS):
        score += 0.25
    return round(max(0.0, min(1.0, score)), 3)


def _summarize_turns(turns: Iterable[tuple[str, str]], fallback: str = "") -> str:
    """Extractive, deterministic summary (<=300 chars) built from the turns."""
    parts: list[str] = []
    truncated = False
    for role, content in turns:
        clean = _first_line(content)
        if not clean:
            continue
        speaker = "主人" if role == "user" else "你"
        piece = f"{speaker}: {clean[:_SUMMARY_TURN_CHARS]}"
        candidate = "；".join([*parts, piece])
        if len(candidate) > _SUMMARY_LIMIT - 1:
            truncated = True
            break
        parts.append(piece)
    text = "；".join(parts) or _clean_text(fallback)
    if truncated and text and len(text) < _SUMMARY_LIMIT:
        text = text[: _SUMMARY_LIMIT - 1] + "…"
    return text[:_SUMMARY_LIMIT]


class EventBuilder:
    """Groups conversation turns into events using gaps and topic drift."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.gap_seconds = max(
            0.0, float(config.get("memory.event_gap_minutes", 45)) * 60.0
        )
        self.topic_gap_seconds = max(
            0.0,
            float(config.get("memory.event_min_gap_for_topic_minutes", 10)) * 60.0,
        )
        self.topic_threshold = float(
            config.get("memory.event_topic_shift_threshold", 0.15)
        )

    # ------------------------------------------------------------- observe
    def observe_turn(
        self,
        turn_id: int,
        session_id: str,
        role: str,
        content: str,
        created_at: float,
    ) -> str:
        """Attach one inserted turn to an event and return the event id."""
        row = self.db.query_one(
            "SELECT id, session_id, role, content, created_at, event_id "
            "FROM conversation_turns WHERE id=?",
            (int(turn_id),),
        )
        if row is None:
            raise ValueError(f"unknown conversation turn: {turn_id}")
        if row["event_id"]:
            return str(row["event_id"])

        session_id = str(row["session_id"])
        role = str(row["role"])
        content = str(row["content"])
        created_at = float(row["created_at"])

        event = self._open_event(session_id)
        if event is not None and self._starts_new_event(event, content, created_at):
            self._close_event(event)
            event = None

        if event is None:
            event_id = self._create_event(session_id, role, content, created_at, int(turn_id))
        else:
            event_id = str(event["id"])

        # Link the turn first: _append_turn reads it back to pick the first
        # user line as the event title.
        self.db.execute(
            "UPDATE conversation_turns SET event_id=? WHERE id=? AND event_id IS NULL",
            (event_id, int(turn_id)),
        )
        if event is not None:
            self._append_turn(event_id, int(turn_id), role, content, created_at)
        return event_id

    # ------------------------------------------------------------- closing
    def close_open_events(
        self, session_id: str | None = None, before: float | None = None
    ) -> int:
        """Close open events, optionally limited to a session and a cutoff.

        ``before`` compares against the event's last known activity
        (``end_at``, falling back to ``start_at``). Returns the event count.
        """
        clauses = ["status='open'"]
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(str(session_id))
        if before is not None:
            clauses.append("COALESCE(end_at, start_at) <= ?")
            params.append(float(before))
        rows = self.db.query(
            "SELECT * FROM conversation_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY start_at, created_at",
            params,
        )
        for row in rows:
            self._close_event(row)
        return len(rows)

    def _close_event(self, event: Any, end_at: float | None = None) -> None:
        event_id = str(event["id"])
        turns = self.db.query(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE event_id=? ORDER BY id",
            (event_id,),
        )
        last_turn_at = max(
            (float(row["created_at"]) for row in turns), default=None
        )
        if end_at is None:
            end_at = event["end_at"]
        if end_at is None:
            end_at = last_turn_at
        end = float(end_at if end_at is not None else event["start_at"])

        summary = _summarize_turns(
            [(str(row["role"]), str(row["content"])) for row in turns],
            fallback=str(event["title"] or ""),
        )
        turn_ids = [int(row["id"]) for row in turns]
        stored_memory_ids = [
            str(value) for value in _load_list(event["memory_ids_json"])
        ]
        linked_memory_ids = [
            str(row["id"])
            for row in self.db.query(
                "SELECT id FROM memory_items WHERE event_id=? ORDER BY created_at",
                (event_id,),
            )
        ]
        memory_ids = _unique([*stored_memory_ids, *linked_memory_ids])
        importance = max(
            float(event["importance"] or 0.0),
            _event_importance(summary, len(turns)),
        )
        now = time.time()
        self.db.execute(
            "UPDATE conversation_events SET status='closed', end_at=?, summary=?, "
            "turn_ids_json=?, memory_ids_json=?, importance=?, updated_at=? WHERE id=?",
            (
                end,
                summary,
                json.dumps(turn_ids, ensure_ascii=False),
                json.dumps(memory_ids, ensure_ascii=False),
                round(importance, 3),
                now,
                event_id,
            ),
        )

    # ------------------------------------------------------------- internals
    def _open_event(self, session_id: str) -> Any | None:
        return self.db.query_one(
            "SELECT * FROM conversation_events WHERE session_id=? AND status='open' "
            "ORDER BY start_at DESC, created_at DESC LIMIT 1",
            (str(session_id),),
        )

    def _starts_new_event(self, event: Any, content: str, created_at: float) -> bool:
        last_at = event["end_at"]
        if last_at is None:
            last_at = event["start_at"]
        gap = float(created_at) - float(last_at)
        if gap > self.gap_seconds:
            return True
        if gap >= self.topic_gap_seconds:
            similarity = self._topic_similarity(
                str(event["session_id"]), str(event["id"]), content
            )
            if similarity < self.topic_threshold:
                return True
        return False

    def _topic_similarity(self, session_id: str, event_id: str, content: str) -> float:
        current = _char_bigrams(content)
        if not current:
            return 0.0
        rows = self.db.query(
            "SELECT content FROM conversation_turns WHERE session_id=? AND event_id=? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, event_id, _RECENT_TURNS_FOR_TOPIC),
        )
        if not rows:
            return 0.0
        return max(_jaccard(current, _char_bigrams(row["content"])) for row in rows)

    def _create_event(
        self,
        session_id: str,
        role: str,
        content: str,
        created_at: float,
        turn_id: int,
    ) -> str:
        event_id = uuid.uuid4().hex
        # Usually the event opens on a user turn; a proactive/assistant turn
        # uses its own first line until the first user line arrives.
        title = _title_from(content)
        topics = _topics_from(content)
        importance = _event_importance(content, 1)
        now = time.time()
        self.db.execute(
            "INSERT INTO conversation_events("
            "id, session_id, start_at, end_at, status, title, summary, "
            "turn_ids_json, memory_ids_json, topics_json, importance, "
            "created_at, updated_at, archived_at"
            ") VALUES (?,?,?,?, 'open', ?, '', ?, '[]', ?, ?, ?, ?, NULL)",
            (
                event_id,
                str(session_id),
                float(created_at),
                float(created_at),
                title,
                json.dumps([int(turn_id)], ensure_ascii=False),
                json.dumps(topics, ensure_ascii=False),
                round(importance, 3),
                now,
                now,
            ),
        )
        return event_id

    def _append_turn(
        self,
        event_id: str,
        turn_id: int,
        role: str,
        content: str,
        created_at: float,
    ) -> None:
        event = self.db.query_one(
            "SELECT * FROM conversation_events WHERE id=?", (event_id,)
        )
        if event is None:
            return
        turn_ids = [int(value) for value in _load_list(event["turn_ids_json"])]
        if turn_id not in turn_ids:
            turn_ids.append(turn_id)
        topics = _merge_topics(
            [str(value) for value in _load_list(event["topics_json"])],
            _topics_from(content),
        )
        title = str(event["title"] or "")
        if role == "user":
            # The first user line names the event, even if the event was
            # opened by an assistant/proactive turn.
            first_user = self.db.query_one(
                "SELECT content FROM conversation_turns WHERE event_id=? AND role='user' "
                "ORDER BY id LIMIT 1",
                (event_id,),
            )
            if first_user is not None:
                title = _title_from(first_user["content"]) or title
        existing_end = event["end_at"]
        end_at = max(
            float(existing_end) if existing_end is not None else float(event["start_at"]),
            float(created_at),
        )
        importance = max(
            float(event["importance"] or 0.0),
            _event_importance(content, len(turn_ids)),
        )
        self.db.execute(
            "UPDATE conversation_events SET title=?, end_at=?, turn_ids_json=?, "
            "topics_json=?, importance=?, updated_at=? WHERE id=?",
            (
                title,
                end_at,
                json.dumps(turn_ids, ensure_ascii=False),
                json.dumps(topics, ensure_ascii=False),
                round(importance, 3),
                time.time(),
                event_id,
            ),
        )
