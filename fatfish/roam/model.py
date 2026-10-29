"""Data types for multi-session roaming (ADR-0014).

A *room* is a bound private session.  Every room has one persistent
``ThreadCard`` -- the partial memory she carries between rooms.  A ``RoamTrip``
is one continuous stay; its raw transcript stays in ``conversation_turns``
under the room's own session id and is never copied into another room.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

# Trip lifecycle (ADR-0014 §7).  P0 uses entering -> active -> returned /
# interrupted; the remaining statuses are reserved for the P1 proposal flow.
TRIP_PROPOSED = "proposed"
TRIP_ENTERING = "entering"
TRIP_ACTIVE = "active"
TRIP_LEAVING = "leaving"
TRIP_RETURNED = "returned"
TRIP_CANCELLED = "cancelled"
TRIP_EXPIRED = "expired"
TRIP_INTERRUPTED = "interrupted"

OPEN_TRIP_STATUSES = (TRIP_ENTERING, TRIP_ACTIVE, TRIP_LEAVING)

# Thread card statuses.
THREAD_OPEN = "open"
THREAD_IDLE = "idle"
THREAD_PAUSED = "paused"
THREAD_BLOCKED = "blocked"

ROLE_OWNER = "owner"
ROLE_FAMILY = "family"
ROLE_TEACHER = "teacher"
ROLE_FRIEND = "friend"

DEFAULT_ALLOWED_ROLES = (ROLE_FAMILY, ROLE_TEACHER, ROLE_FRIEND)


def _loads(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return text[: max(1, limit - 1)] + "…"
    return text


@dataclass(slots=True)
class ThreadCard:
    """One room's persistent partial memory."""

    session_id: str
    role: str = ""
    display_name: str = ""
    target_uin: str = ""
    status: str = THREAD_IDLE
    summary: str = ""
    last_topic: str = ""
    open_loops: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    mood: str = ""
    unread_count: int = 0
    attention_score: float = 0.0
    last_seen_at: float | None = None
    last_summary_at: float | None = None
    last_turn_id: int | None = None
    cooldown_until: float | None = None
    last_trip_at: float | None = None
    trip_count: int = 0
    total_seconds: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: Any) -> "ThreadCard":
        data = dict(row)
        return cls(
            session_id=str(data.get("session_id") or ""),
            role=str(data.get("role") or ""),
            display_name=str(data.get("display_name") or ""),
            target_uin=str(data.get("target_uin") or ""),
            status=str(data.get("status") or THREAD_IDLE),
            summary=str(data.get("summary") or ""),
            last_topic=str(data.get("last_topic") or ""),
            open_loops=[str(x) for x in _loads(data.get("open_loops_json"), [])],
            commitments=[str(x) for x in _loads(data.get("commitments_json"), [])],
            key_facts=[str(x) for x in _loads(data.get("key_facts_json"), [])],
            mood=str(data.get("mood") or ""),
            unread_count=int(data.get("unread_count") or 0),
            attention_score=float(data.get("attention_score") or 0.0),
            last_seen_at=_float_or_none(data.get("last_seen_at")),
            last_summary_at=_float_or_none(data.get("last_summary_at")),
            last_turn_id=int(data["last_turn_id"]) if data.get("last_turn_id") is not None else None,
            cooldown_until=_float_or_none(data.get("cooldown_until")),
            last_trip_at=_float_or_none(data.get("last_trip_at")),
            trip_count=int(data.get("trip_count") or 0),
            total_seconds=float(data.get("total_seconds") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )

    def prompt_line(self, *, max_chars: int = 180) -> str:
        """One prompt-safe line: a name, a summary, an open loop. No uin."""
        name = self.display_name or "这个房间"
        parts: list[str] = []
        if self.summary:
            parts.append(self.summary)
        if self.last_topic and self.last_topic not in self.summary:
            parts.append(f"上次聊到：{self.last_topic}")
        if self.open_loops:
            parts.append("待办：" + "；".join(self.open_loops[:2]))
        body = "；".join(parts) or "还没有线程卡记录"
        return f"- {name}：{_clip(body, max_chars)}"


@dataclass(slots=True)
class RoamTrip:
    trip_id: str
    thread_session: str
    home_session: str
    purpose: str = ""
    purpose_json: dict[str, Any] = field(default_factory=dict)
    status: str = TRIP_PROPOSED
    score: float = 0.0
    proposal_expires_at: float | None = None
    approved_at: float | None = None
    authorization_json: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    ended_at: float | None = None
    close_reason: str = ""
    summary: str = ""
    summary_json: dict[str, Any] = field(default_factory=dict)
    message_count: int = 0
    created_at: float = 0.0
    id: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "RoamTrip":
        data = dict(row)
        return cls(
            trip_id=str(data.get("trip_id") or ""),
            thread_session=str(data.get("thread_session") or ""),
            home_session=str(data.get("home_session") or ""),
            purpose=str(data.get("purpose") or ""),
            purpose_json=_loads(data.get("purpose_json"), {}),
            status=str(data.get("status") or TRIP_PROPOSED),
            score=float(data.get("score") or 0.0),
            proposal_expires_at=_float_or_none(data.get("proposal_expires_at")),
            approved_at=_float_or_none(data.get("approved_at")),
            authorization_json=_loads(data.get("authorization_json"), {}),
            started_at=_float_or_none(data.get("started_at")),
            ended_at=_float_or_none(data.get("ended_at")),
            close_reason=str(data.get("close_reason") or ""),
            summary=str(data.get("summary") or ""),
            summary_json=_loads(data.get("summary_json"), {}),
            message_count=int(data.get("message_count") or 0),
            created_at=float(data.get("created_at") or 0.0),
            id=int(data.get("id") or 0),
        )

    def duration_seconds(self, *, now: float | None = None) -> float:
        start = float(self.started_at or self.created_at or 0.0)
        end = float(self.ended_at if self.ended_at is not None else (now or start))
        return max(0.0, end - start)

    def duration_minutes(self, *, now: float | None = None) -> int:
        return max(0, int(round(self.duration_seconds(now=now) / 60.0)))


@dataclass(slots=True)
class SummaryPayload:
    """Structured trip summary (ADR-0014 §7.3).

    ``summary`` and ``key_facts`` are the only fields that reach owner-side
    memory search.  They are structured statements, not the room's raw
    transcript; raw chats stay in their own session.
    """

    summary: str = ""
    topics: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    mood: str = ""
    open_loops: list[str] = field(default_factory=list)
    report_to_owner: str = ""
    privacy_flags: list[str] = field(default_factory=list)
    degraded: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "topics": list(self.topics),
            "key_facts": list(self.key_facts),
            "commitments": list(self.commitments),
            "mood": self.mood,
            "open_loops": list(self.open_loops),
            "report_to_owner": self.report_to_owner,
            "privacy_flags": list(self.privacy_flags),
            "degraded": self.degraded,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "SummaryPayload":
        data = _loads(raw, {}) if not isinstance(raw, dict) else raw
        return cls(
            summary=str(data.get("summary") or ""),
            topics=_string_list(data.get("topics")),
            key_facts=_string_list(data.get("key_facts")),
            commitments=_string_list(data.get("commitments")),
            mood=str(data.get("mood") or ""),
            open_loops=_string_list(data.get("open_loops")),
            report_to_owner=str(data.get("report_to_owner") or ""),
            privacy_flags=_string_list(data.get("privacy_flags")),
            degraded=bool(data.get("degraded")),
        )

    def memory_text(self) -> str:
        """What an owner-side memory search may index (structured facts only)."""
        lines = [self.summary]
        if self.key_facts:
            lines.append("关键事实：" + "；".join(self.key_facts))
        return "\n".join(line for line in lines if line).strip()


@dataclass(slots=True)
class Room:
    session_id: str
    role: str
    display_name: str
    target_uin: str = ""
    aliases: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        values = [self.display_name, *self.aliases]
        return [value for value in dict.fromkeys(v for v in values if v)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "role": self.role,
            "display_name": self.display_name,
            # uin is local-only; callers must not render it into prompts.
            "target_uin": self.target_uin,
            "aliases": list(self.aliases),
        }


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


__all__ = [
    "DEFAULT_ALLOWED_ROLES",
    "OPEN_TRIP_STATUSES",
    "ROLE_FAMILY",
    "ROLE_FRIEND",
    "ROLE_OWNER",
    "ROLE_TEACHER",
    "Room",
    "RoamTrip",
    "SummaryPayload",
    "THREAD_BLOCKED",
    "THREAD_IDLE",
    "THREAD_OPEN",
    "THREAD_PAUSED",
    "TRIP_ACTIVE",
    "TRIP_CANCELLED",
    "TRIP_ENTERING",
    "TRIP_EXPIRED",
    "TRIP_INTERRUPTED",
    "TRIP_LEAVING",
    "TRIP_PROPOSED",
    "TRIP_RETURNED",
    "ThreadCard",
]
