"""Nightly day archival with a grounded whale-girl diary.

The archive is local-first and deterministic:

* ``archive_day`` turns one local day of conversation events into an
  ``episodes`` row plus event-level and daily ``memory_items``;
* the factual timeline is built from EventBuilder output only, never by an LLM;
* at most one LLM call is made per archived day, for the owner session (or the
  busiest session when no owner is bound), and only for the voice narrative;
* rerunning a day is idempotent because every archived memory has an explicit
  ``source_type``/``source_id`` identity.

``run_nightly`` defaults to *the previous local day*: at 00:30 the new day has
only just started and must not be summarized.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Iterable

from .clock import get_timezone, to_local
from .config import Config
from .db import Database
from .growth import append_growth
from .life import LifeEngine
from .llm import LLMClient
from .memory.details import archive_details
from .memory.edges import sync_session_edges
from .memory.entities import archive_session_entities
from .memory.sessions import is_test_session
from .memory.tags import sync_tags
from .memory import MemoryService

FACTUAL_TIMELINE_MAX_CHARS = 4000
VOICE_FACTS_MAX_CHARS = 2200
VOICE_MAX_CHARS = 600
_EVENT_SUMMARY_MAX_CHARS = 600
_EXCERPT_CHARS = 90
_TITLE_CHARS = 40
_GIST_CHARS = 80

_EVENT_SOURCE = "event_archive"
_DAILY_SOURCE = "episode"
_DAY_SUMMARY_SOURCE = "day_summary"

_VOICE_SYSTEM_PROMPT = (
    "你是“蓝色大肥鱼”家的鲸鱼娘，称呼对方为“主人哥哥”。"
    "你在写只给自己看的日记，口吻亲密、自然，可以有一点撒娇。"
    "只允许使用下面“事实清单”里的内容：不得添加任何没有出现的时间、地点、人物、"
    "事件、原因或感受细节。写 2 到 4 句中文。不要列清单，不要客服腔，"
    "不要描述写作过程，不要提到模型、AI 或程序。"
    "同时，从今天的事情里提炼 0 到 2 条“成长层”笔记：只写你以后要怎么说话、"
    "怎么做，用第一人称，像在心里提醒自己；不要复述具体人物、时间、地点、"
    "事件或隐私，不要改写上面的核心人格。"
    "只输出一个 JSON 对象，不要 Markdown，格式："
    '{"diary":"2-4句日记","growth":["成长笔记1","成长笔记2"]}'
)


class _DryRunRollback(Exception):
    """Raised inside a transaction to preview an archive without writing."""


@dataclass(slots=True)
class _EventGroup:
    event_ids: list[str]
    title: str
    summary: str
    start_at: float
    end_at: float
    importance: float
    turn_count: int

    @property
    def primary_event_id(self) -> str:
        return self.event_ids[0]

    @property
    def source_id(self) -> str:
        if len(self.event_ids) == 1:
            return self.event_ids[0]
        return f"merged:{self.event_ids[0]}:{self.event_ids[-1]}"


@dataclass(slots=True)
class _DaySession:
    session_id: str
    date_text: str
    day_start: float
    day_end: float
    start_at: float
    end_at: float
    turn_count: int
    events: list[Any]
    factual: str
    episode_id: str
    existing_voice: str = ""
    voice: str = ""
    voice_source: str = "template"
    event_groups: list[_EventGroup] = field(default_factory=list)

    @property
    def month_day(self) -> str:
        return self.date_text[5:]


# --------------------------------------------------------------------- dates
def _resolve_target_date(
    target_date: str | date | datetime | float | int | None,
    tz_name: str,
    *,
    now: float | None = None,
) -> tuple[str, float, float]:
    """Return ``(YYYY-MM-DD, day_start, day_end)`` for a local date."""
    tz = get_timezone(tz_name)
    if target_date is None:
        current = to_local(time.time() if now is None else float(now), tz_name)
        day = current.date() - timedelta(days=1)
    elif isinstance(target_date, datetime):
        day = target_date.date()
        if target_date.tzinfo is not None:
            day = target_date.astimezone(tz).date()
    elif isinstance(target_date, date):
        day = target_date
    elif isinstance(target_date, (int, float)):
        day = to_local(float(target_date), tz_name).date()
    else:
        raw = str(target_date).strip()
        if not raw:
            raise ValueError("target_date must not be empty")
        try:
            day = date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise ValueError(f"invalid date (expected YYYY-MM-DD): {target_date!r}") from exc
    start_dt = datetime.combine(day, dtime.min, tzinfo=tz)
    end_dt = start_dt + timedelta(days=1)
    return day.isoformat(), start_dt.timestamp(), end_dt.timestamp()


# ---------------------------------------------------------------- collection
def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clip(text: Any, limit: int) -> str:
    cleaned = _clean(text)
    if limit <= 0:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _event_end(row: Any) -> float:
    end = row["end_at"]
    return float(end if end is not None else row["start_at"])


def _group_events(events: list[Any], cap: int) -> list[_EventGroup]:
    groups = [
        _EventGroup(
            event_ids=[str(row["id"])],
            title=_clean(row["title"]),
            summary=_clean(row["summary"]) or _clean(row["title"]),
            start_at=float(row["start_at"]),
            end_at=max(float(row["start_at"]), _event_end(row)),
            importance=float(row["importance"] or 0.4),
            turn_count=max(1, len(_json_list(row["turn_ids_json"]))),
        )
        for row in events
    ]
    cap = max(1, int(cap))
    # Merge the two adjacent events with the fewest turns until the day fits.
    # Deterministic: ties go to the earliest pair.
    while len(groups) > cap:
        index = min(
            range(len(groups) - 1),
            key=lambda i: (groups[i].turn_count + groups[i + 1].turn_count, i),
        )
        left, right = groups[index], groups[index + 1]
        summary = _clip(
            "；".join(part for part in (left.summary, right.summary) if part),
            _EVENT_SUMMARY_MAX_CHARS,
        )
        groups[index] = _EventGroup(
            event_ids=[*left.event_ids, *right.event_ids],
            title=left.title or right.title,
            summary=summary,
            start_at=min(left.start_at, right.start_at),
            end_at=max(left.end_at, right.end_at),
            importance=max(left.importance, right.importance),
            turn_count=left.turn_count + right.turn_count,
        )
        del groups[index + 1]
    return groups


def _selected_turns(turns: list[Any]) -> list[Any]:
    """Pick a small, deterministic set of user/assistant excerpts."""
    if not turns:
        return []
    chosen: list[Any] = []
    first_user = next((row for row in turns if row["role"] == "user"), None)
    first_assistant = next((row for row in turns if row["role"] == "assistant"), None)
    if first_user is not None:
        chosen.append(first_user)
    if first_assistant is not None:
        chosen.append(first_assistant)
    if not chosen or len(turns) > 1:
        chosen.append(turns[-1])
    if len(chosen) == 1 and first_user is not None:
        for row in turns:
            if row["id"] != first_user["id"]:
                chosen.append(row)
                break
    unique: dict[Any, Any] = {}
    for row in chosen:
        unique[row["id"]] = row
    return sorted(unique.values(), key=lambda row: (float(row["created_at"]), int(row["id"])))


def _time_range(start_at: float, end_at: float, tz_name: str) -> str:
    start_text = to_local(start_at, tz_name).strftime("%H:%M")
    end_text = to_local(end_at, tz_name).strftime("%H:%M")
    if end_text == start_text:
        return start_text
    return f"{start_text}-{end_text}"


def _excerpt_lines(turns: list[Any], tz_name: str) -> list[str]:
    lines: list[str] = []
    for row in _selected_turns(turns):
        speaker = "主人" if row["role"] == "user" else "你"
        content = _clip(row["content"], _EXCERPT_CHARS)
        if content:
            lines.append(f"  {speaker}: {content}")
    return lines


def _factual_timeline(
    date_text: str,
    events: list[Any],
    turns_by_event: dict[str, list[Any]],
    unassigned: list[Any],
    tz_name: str,
    *,
    max_chars: int = FACTUAL_TIMELINE_MAX_CHARS,
) -> str:
    """Deterministic chronology of event headers plus selected excerpts."""
    blocks: list[tuple[float, list[str]]] = []
    for event in events:
        event_id = str(event["id"])
        start_at = float(event["start_at"])
        end_at = max(start_at, _event_end(event))
        title = _clip(event["title"] or event["summary"] or "片段", _TITLE_CHARS)
        header = f"[{_time_range(start_at, end_at, tz_name)}] {title}"
        blocks.append((start_at, [header, *_excerpt_lines(turns_by_event.get(event_id, []), tz_name)]))

    # Turns whose event started on another day (or has no event) still belong
    # to today's record; keep them as compact extra blocks in time order.
    grouped: dict[str, list[Any]] = {}
    for row in unassigned:
        grouped.setdefault(str(row["event_id"] or "__none__"), []).append(row)
    for event_id, rows in grouped.items():
        rows.sort(key=lambda row: (float(row["created_at"]), int(row["id"])))
        start_at = float(rows[0]["created_at"])
        end_at = float(rows[-1]["created_at"])
        label = "跨日片段" if event_id != "__none__" else "零散片段"
        header = f"[{_time_range(start_at, end_at, tz_name)}] {label}"
        blocks.append((start_at, [header, *_excerpt_lines(rows, tz_name)]))

    blocks.sort(key=lambda item: item[0])
    header = f"{date_text} 事实时间线"
    lines: list[str] = [header]
    for _, block in blocks:
        candidate = "\n".join([*lines, *block])
        if len(candidate) <= max_chars:
            lines.extend(block)
            continue
        marker = "…（时间线已截断）"
        if len("\n".join(lines)) + 1 + len(marker) <= max_chars:
            lines.append(marker)
        break
    text = "\n".join(lines)
    return text[:max_chars]


def _collect_day_sessions(
    db: Database,
    config: Config,
    date_text: str,
    start: float,
    end: float,
) -> list[_DaySession]:
    tz_name = config.timezone_name
    turn_rows = db.query(
        "SELECT id, session_id, role, content, created_at, event_id "
        "FROM conversation_turns "
        "WHERE created_at>=? AND created_at<? AND ephemeral=0 "
        "ORDER BY created_at, id",
        (start, end),
    )
    event_rows = db.query(
        "SELECT * FROM conversation_events "
        "WHERE start_at>=? AND start_at<? "
        "ORDER BY start_at, created_at",
        (start, end),
    )

    turns_by_session: dict[str, list[Any]] = {}
    for row in turn_rows:
        turns_by_session.setdefault(str(row["session_id"]), []).append(row)
    events_by_session: dict[str, list[Any]] = {}
    for row in event_rows:
        events_by_session.setdefault(str(row["session_id"]), []).append(row)

    sessions: list[_DaySession] = []
    for session_id in sorted(set(turns_by_session) | set(events_by_session)):
        turns = turns_by_session.get(session_id, [])
        events = events_by_session.get(session_id, [])
        turns_by_event: dict[str, list[Any]] = {}
        event_ids = {str(row["id"]) for row in events}
        for row in turns:
            event_id = str(row["event_id"] or "")
            if event_id in event_ids:
                turns_by_event.setdefault(event_id, []).append(row)
        unassigned = [
            row for row in turns if str(row["event_id"] or "") not in event_ids
        ]
        factual = _factual_timeline(
            date_text, events, turns_by_event, unassigned, tz_name
        )
        turn_starts = [float(row["created_at"]) for row in turns]
        turn_ends = [float(row["created_at"]) for row in turns]
        event_starts = [float(row["start_at"]) for row in events]
        event_ends = [_event_end(row) for row in events]
        span_starts = [*turn_starts, *event_starts]
        span_ends = [*turn_ends, *event_ends]
        session_start = min(span_starts) if span_starts else start
        session_end = max(span_ends) if span_ends else end
        episode_id = f"ep:{date_text}:{session_id}"
        existing = db.query_one(
            "SELECT summary_voice FROM episodes WHERE id=?", (episode_id,)
        )
        sessions.append(
            _DaySession(
                session_id=session_id,
                date_text=date_text,
                day_start=start,
                day_end=end,
                start_at=session_start,
                end_at=session_end,
                turn_count=len(turns),
                events=events,
                factual=factual,
                episode_id=episode_id,
                existing_voice=_clean(existing["summary_voice"]) if existing else "",
                event_groups=_group_events(
                    events, int(config.get("memory.event_max_per_day", 20))
                ),
            )
        )
    return sessions


# ---------------------------------------------------------------- voice text
def _template_narrative(session: _DaySession) -> str:
    """Factual-only fallback used when no voice call is allowed or it fails."""
    count = len(session.events) or 1
    summaries: list[str] = []
    for event in session.events:
        text = _clip(event["title"] or event["summary"], _TITLE_CHARS + 20)
        if text and text not in summaries:
            summaries.append(text)
        if len(summaries) >= 3:
            break
    if not summaries:
        summaries = ["一些零碎的小事"]
    return f"今天和主人哥哥聊了 {count} 件事：{'；'.join(summaries)}。我都记着呢。"


def _low_reasoning_config(config: Config) -> Config:
    effort = str(config.get("llm.reasoning_effort", "low") or "").strip().lower()
    if effort == "low":
        return config
    data = config.as_dict()
    llm = data.setdefault("llm", {})
    if isinstance(llm, dict):
        llm["reasoning_effort"] = "low"
    return Config(data, profile=config.profile, source=config.source)


def _build_llm_client(db: Database, config: Config) -> LLMClient:
    return LLMClient(db=db, config=_low_reasoning_config(config))


def _voice_target_session(
    db: Database, sessions: list[_DaySession]
) -> tuple[_DaySession | None, str]:
    """Owner session if bound, otherwise the session with the most turns."""
    if not sessions:
        return None, "empty"
    row = None
    if db.query_one(
        "SELECT 1 AS hit FROM sqlite_master WHERE type='table' AND name='owner_bindings'"
    ):
        row = db.query_one("SELECT session_id FROM owner_bindings LIMIT 1")
    if row:
        owner_session = str(row["session_id"])
        for session in sessions:
            if session.session_id == owner_session:
                return session, "owner"
        return None, "owner_absent"
    return max(sessions, key=lambda s: (s.turn_count, s.session_id)), "busiest"


def _validate_voice(text: str) -> tuple[str, str]:
    cleaned = _clean(text).strip().strip('"“”')
    if not cleaned:
        return "", "empty"
    if len(cleaned) > VOICE_MAX_CHARS:
        return "", "too_long"
    lowered = cleaned.lower()
    if "作为ai" in lowered or "作为 ai" in lowered or "助手" in cleaned:
        return "", "meta_reference"
    return cleaned, ""


def _parse_voice_payload(text: str) -> tuple[str, list[str]]:
    """Split a nightly LLM reply into ``(diary, growth_candidates)``.

    The prompt asks for JSON so one call can produce both the diary and the
    growth notes.  Old prompts, local models and plain-text fakes still work:
    anything that is not a valid object is treated as the diary itself.
    """
    raw = _clean(text)
    if not raw:
        return "", []
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(candidate[start : end + 1])
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            diary = payload.get("diary")
            growth = payload.get("growth") or []
            if isinstance(diary, str):
                if isinstance(growth, str):
                    growth = [growth]
                candidates = (
                    [str(item) for item in growth if isinstance(item, (str, int, float))]
                    if isinstance(growth, list)
                    else []
                )
                return diary, candidates
        # A truncated or malformed object must not be stored as a diary.
        if candidate.startswith("{") or "```" in raw:
            return "", []
    return raw, []


def _generate_voice(
    db: Database,
    config: Config,
    sessions: list[_DaySession],
    *,
    write_voice: bool,
) -> dict[str, Any]:
    """One LLM call at most, only for the chosen session; everyone else uses
    the deterministic template."""
    enabled = bool(config.get("memory.nightly_voice", True))
    target, reason = _voice_target_session(db, sessions)
    info: dict[str, Any] = {
        "enabled": enabled,
        "target_session": target.session_id if target else None,
        "selection": reason,
        "source": "template",
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "rejected": "",
        "growth_added": 0,
        "growth_updated": 0,
        "growth_rejected": 0,
    }
    if target is not None:
        target.voice_source = "template"
        target.voice = _template_narrative(target)
    for session in sessions:
        if session is not target:
            session.voice_source = "template"
            session.voice = _template_narrative(session)

    if target is None:
        return info
    if target.existing_voice:
        # An archived day already has a narrative: reuse it even for a
        # --no-voice rerun so the archive is never downgraded or duplicated.
        target.voice = target.existing_voice
        target.voice_source = "cached"
        info.update(source="cached", selection=f"{reason}:cached")
        return info
    # Hard once-per-day guard: a crash between the call and persistence (or a
    # changed owner) must not spend a second call on the same date.
    if db.get_meta("last_nightly_voice_date") == target.date_text:
        info["selection"] = f"{reason}:already_called"
        return info
    if not write_voice:
        info["selection"] = f"{reason}:voice_disabled"
        return info
    if not enabled:
        info["selection"] = f"{reason}:config_disabled"
        return info

    facts = target.factual.replace("…（时间线已截断）", "")
    if len(facts) > VOICE_FACTS_MAX_CHARS:
        facts = facts[: VOICE_FACTS_MAX_CHARS - 1] + "…"
    max_tokens = max(1, int(config.get("memory.nightly_voice_max_tokens", 350)))
    messages = [
        {"role": "system", "content": _VOICE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"日期：{target.date_text}\n"
                f"事实清单：\n{facts}\n\n"
                "请写 2-4 句今天的日记，并给 0-2 条成长笔记。"
            ),
        },
    ]
    try:
        client = _build_llm_client(db, config)
    except Exception as exc:  # pragma: no cover - defensive
        info["rejected"] = f"client_error:{type(exc).__name__}"
        return info
    if client is None or not getattr(client, "available", False):
        info["selection"] = f"{reason}:unavailable"
        return info

    try:
        result = client.chat(
            messages,
            max_tokens=max_tokens,
            temperature=0.5,
            session_id=f"nightly:{target.date_text}",
        )
    except Exception as exc:  # pragma: no cover - defensive
        info["rejected"] = f"call_error:{type(exc).__name__}"
        return info
    info["llm_calls"] = 1
    info["source"] = "llm"
    db.set_meta("last_nightly_voice_date", target.date_text)
    if result is not None:
        usage = getattr(result, "usage", None)
        info["prompt_tokens"] = int(getattr(usage, "prompt_tokens", 0) or 0)
        info["completion_tokens"] = int(getattr(usage, "completion_tokens", 0) or 0)
        info["total_tokens"] = int(getattr(usage, "total_tokens", 0) or 0)
    diary, growth_candidates = _parse_voice_payload(
        getattr(result, "text", "") if result else ""
    )
    text, rejected = _validate_voice(diary)
    if rejected:
        info["rejected"] = rejected
        info["source"] = "template_fallback"
        return info
    target.voice = text
    target.voice_source = "llm"
    if growth_candidates:
        try:
            merged = append_growth(
                db,
                growth_candidates,
                source_date=target.date_text,
            )
        except Exception as exc:  # pragma: no cover - defensive
            info["growth_rejected"] = len(growth_candidates)
            info["growth_error"] = type(exc).__name__
        else:
            info["growth_added"] = int(merged.get("added", 0))
            info["growth_updated"] = int(merged.get("updated", 0))
            info["growth_rejected"] = int(merged.get("rejected", 0))
    return info


# ------------------------------------------------------------------ persistence
def _event_gist(group: _EventGroup) -> str:
    base = _clean(group.title) or _clean(group.summary) or "片段"
    if len(group.event_ids) > 1:
        base = f"{base} 等{len(group.event_ids)}件事"
    return _clip(base, _GIST_CHARS)


def _event_importance(group: _EventGroup) -> float:
    return round(max(0.5, min(0.7, 0.5 + 0.2 * group.importance)), 3)


def _count_existing(db: Database, source_type: str, source_id: str) -> bool:
    row = db.query_one(
        "SELECT 1 AS hit FROM memory_items WHERE source_type=? AND source_id=? "
        "AND status='active' LIMIT 1",
        (source_type, source_id),
    )
    return row is not None


def _session_label(
    db: Database,
    session_id: str,
    *,
    owner_sid: str = "",
    friend_sid: str = "",
) -> str:
    if owner_sid and session_id == owner_sid:
        return "主人哥哥"
    if friend_sid and session_id == friend_sid:
        return "姐姐"
    if "group" in session_id:
        return "群聊"
    if session_id.startswith("activity-"):
        return "她的活动"
    return "另一个会话"


def _binding_session(db: Database, role: str) -> str:
    """Session id for an owner/friend role; empty when the table is absent."""
    try:
        if db.query_one(
            "SELECT 1 AS hit FROM sqlite_master WHERE type='table' "
            "AND name='owner_bindings'"
        ) is None:
            return ""
        row = db.query_one(
            "SELECT session_id FROM owner_bindings WHERE role=? LIMIT 1", (role,)
        )
    except Exception:  # an older/partial DB must not break the rollup
        return ""
    return str(row["session_id"]) if row else ""


def _activity_rollup(
    db: Database,
    sessions: list[_DaySession],
    start: float,
    end: float,
) -> str:
    """One compact line for all activity-* sessions of the day."""
    activity = [s for s in sessions if s.session_id.startswith("activity-")]
    if not activity:
        return ""
    try:
        rows = db.query(
            "SELECT content FROM conversation_turns "
            "WHERE role='assistant' AND session_id LIKE 'activity-%' "
            "AND created_at>=? AND created_at<? ORDER BY created_at DESC LIMIT 8",
            (start, end),
        )
    except Exception:
        rows = []
    latest = ""
    for row in rows:
        text = " ".join(str(row["content"] or "").split())
        if text and text not in {"休息", "休息。", "先休息"} and len(text) > 8:
            latest = text
            break
    detail = _clip(latest, 120) if latest else "主要是休息"
    return f"她的活动：{len(activity)} 轮，{detail}"


def _persist_day_summary(
    db: Database,
    memory: MemoryService,
    sessions: list[_DaySession],
    date_text: str,
    start: float,
    end: float,
    *,
    now: float,
) -> dict[str, Any]:
    """One cross-session daily rollup, idempotent by date.

    Per-session ``episode`` memories still exist; this row is the "what
    happened today across owner/friend/group/activity" view that a question
    like 昨天都发生了什么 can recall without reading four separate diaries.
    Test/night-test sessions are excluded so fixtures never become the day.
    """
    real = [s for s in sessions if not is_test_session(s.session_id)]
    if not real:
        return {
            "created": False,
            "memory_id": None,
            "sessions": 0,
            "reason": "no_real_sessions",
            "summary": "",
        }
    owner_sid = _binding_session(db, "owner")
    friend_sid = _binding_session(db, "friend")
    ordered: list[tuple[int, str]] = []
    for session in real:
        label = _session_label(
            db,
            session.session_id,
            owner_sid=owner_sid,
            friend_sid=friend_sid,
        )
        if label == "她的活动":
            continue
        detail = session.voice if label == "主人哥哥" and session.voice else session.factual
        detail = _clean(detail) or "有对话记录"
        priority = {"主人哥哥": 0, "姐姐": 1, "群聊": 2}.get(label, 3)
        ordered.append((priority, f"{label}：{_clip(detail, 150)}"))
    ordered.sort(key=lambda item: item[0])
    parts = [part for _, part in ordered]
    activity = _activity_rollup(db, real, start, end)
    if activity:
        parts.append(activity)
    summary = _clip(f"{date_text} 日汇总：" + "；".join(parts), 1000)
    tags = ["日汇总", "day_summary", date_text]
    existing = db.query_one(
        "SELECT id FROM memory_items WHERE source_type=? AND source_id=? "
        "AND status='active' LIMIT 1",
        (_DAY_SUMMARY_SOURCE, date_text),
    )
    if existing:
        item_id = str(existing["id"])
        _refresh_item(
            db,
            item_id,
            session_id=f"day:{date_text}",
            gist=f"{date_text} 日汇总",
            summary=summary,
            event_time_start=start,
            event_time_end=end,
            importance=0.65,
            source_type=_DAY_SUMMARY_SOURCE,
            source_id=date_text,
            evidence="nightly cross-session rollup",
            tags_json=json.dumps(tags, ensure_ascii=False),
        )
        created = False
    else:
        item = memory.add_item(
            summary,
            type="episodic",
            gist=f"{date_text} 日汇总",
            session_id=f"day:{date_text}",
            event_time_start=start,
            event_time_end=end,
            observed_at=now,
            importance=0.65,
            source_type=_DAY_SUMMARY_SOURCE,
            source_id=date_text,
            evidence="nightly cross-session rollup",
            tags=tags,
        )
        item_id = item.id
        created = True
    sync_tags(db, tags, "memory", item_id, now=now)
    return {
        "created": created,
        "memory_id": item_id,
        "sessions": len(real),
        "reason": "",
        "summary": summary,
    }


def _refresh_item(db: Database, item_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{key}=?" for key in fields)
    db.execute(
        f"UPDATE memory_items SET {assignments} WHERE id=?",
        (*fields.values(), item_id),
    )


def _persist_session(
    db: Database,
    memory: MemoryService,
    session: _DaySession,
    *,
    now: float,
) -> dict[str, Any]:
    event_ids = [str(row["id"]) for row in session.events]
    existing_episode = db.query_one(
        "SELECT created_at FROM episodes WHERE id=?", (session.episode_id,)
    )
    created_at = (
        float(existing_episode["created_at"]) if existing_episode else float(now)
    )
    db.execute(
        """
        INSERT INTO episodes(
            id, title, start_at, end_at, summary, memory_ids_json, session_id,
            event_ids_json, summary_voice, turn_count, created_at, updated_at
        ) VALUES (?,?,?,?,?, '[]', ?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            start_at=excluded.start_at,
            end_at=excluded.end_at,
            summary=excluded.summary,
            session_id=excluded.session_id,
            event_ids_json=excluded.event_ids_json,
            summary_voice=excluded.summary_voice,
            turn_count=excluded.turn_count,
            updated_at=excluded.updated_at
        """,
        (
            session.episode_id,
            session.date_text,
            session.start_at,
            session.end_at,
            session.factual,
            session.session_id,
            json.dumps(event_ids, ensure_ascii=False),
            session.voice,
            int(session.turn_count),
            created_at,
            now,
        ),
    )

    memory_ids: list[str] = []
    created = 0
    updated = 0
    tags_linked = 0
    event_to_memory: dict[str, str] = {}
    for group in session.event_groups:
        existed = _count_existing(db, _EVENT_SOURCE, group.source_id)
        item = memory.add_item(
            group.summary or group.title or session.date_text,
            type="episodic",
            gist=_event_gist(group),
            session_id=session.session_id,
            event_id=group.primary_event_id,
            episode_id=session.episode_id,
            event_time_start=group.start_at,
            event_time_end=group.end_at,
            observed_at=now,
            importance=_event_importance(group),
            source_type=_EVENT_SOURCE,
            source_id=group.source_id,
            evidence="nightly event archive",
            tags=["archive", session.date_text],
        )
        # Refresh updates an existing row (including older archives); the
        # explicit source identity makes this safe to repeat.
        _refresh_item(
            db,
            item.id,
            session_id=session.session_id,
            event_id=group.primary_event_id,
            episode_id=session.episode_id,
            gist=_event_gist(group),
            summary=group.summary or group.title or session.date_text,
            event_time_start=group.start_at,
            event_time_end=group.end_at,
            importance=_event_importance(group),
            source_type=_EVENT_SOURCE,
            source_id=group.source_id,
            tags_json=json.dumps(["archive", session.date_text], ensure_ascii=False),
        )
        tags_linked += int(
            sync_tags(db, ["archive", session.date_text], "memory", item.id, now=now)[
                "linked"
            ]
        )
        memory_ids.append(item.id)
        created += 0 if existed else 1
        updated += 1 if existed else 0
        for event_id in group.event_ids:
            event_to_memory[event_id] = item.id

    # Drop (archive) event memories that no longer correspond to the current
    # grouping, so a data correction cannot leave duplicate active rows.
    current_sources = {group.source_id for group in session.event_groups}
    for row in db.query(
        "SELECT id, source_id FROM memory_items WHERE source_type=? AND episode_id=? "
        "AND status='active'",
        (_EVENT_SOURCE, session.episode_id),
    ):
        if str(row["source_id"]) not in current_sources:
            db.execute(
                "UPDATE memory_items SET status='archived', updated_at=? WHERE id=?",
                (now, row["id"]),
            )

    daily_existed = _count_existing(db, _DAILY_SOURCE, session.episode_id)
    daily = memory.add_item(
        session.voice,
        type="episodic",
        gist=f"{session.date_text} 的日常",
        session_id=session.session_id,
        episode_id=session.episode_id,
        event_time_start=session.day_start,
        event_time_end=session.day_end,
        observed_at=now,
        importance=0.6,
        source_type=_DAILY_SOURCE,
        source_id=session.episode_id,
        evidence=f"nightly daily archive; voice={session.voice_source}",
        tags=["daily_archive", "archive", session.date_text],
    )
    _refresh_item(
        db,
        daily.id,
        session_id=session.session_id,
        episode_id=session.episode_id,
        gist=f"{session.date_text} 的日常",
        summary=session.voice,
        event_time_start=session.day_start,
        event_time_end=session.day_end,
        importance=0.6,
        source_type=_DAILY_SOURCE,
        source_id=session.episode_id,
        evidence=f"nightly daily archive; voice={session.voice_source}",
        tags_json=json.dumps(
            ["daily_archive", "archive", session.date_text], ensure_ascii=False
        ),
    )
    tags_linked += int(
        sync_tags(
            db,
            ["daily_archive", "archive", session.date_text],
            "memory",
            daily.id,
            now=now,
        )["linked"]
    )
    memory_ids.append(daily.id)
    created += 0 if daily_existed else 1
    updated += 1 if daily_existed else 0

    db.execute(
        "UPDATE episodes SET memory_ids_json=?, updated_at=? WHERE id=?",
        (json.dumps(memory_ids, ensure_ascii=False), now, session.episode_id),
    )

    for event in session.events:
        event_id = str(event["id"])
        existing = [str(value) for value in _json_list(event["memory_ids_json"])]
        memory_id = event_to_memory.get(event_id)
        if memory_id and memory_id not in existing:
            existing.append(memory_id)
        db.execute(
            "UPDATE conversation_events SET status='archived', "
            "archived_at=COALESCE(archived_at, ?), memory_ids_json=?, updated_at=? "
            "WHERE id=?",
            (now, json.dumps(existing, ensure_ascii=False), now, event_id),
        )

    db.execute(
        "UPDATE conversation_turns SET episode_id=?, archived_at=COALESCE(archived_at, ?) "
        "WHERE session_id=? AND created_at>=? AND created_at<? AND ephemeral=0",
        (session.episode_id, now, session.session_id, session.day_start, session.day_end),
    )

    detail_stats = archive_details(db, session, now=now)
    entity_stats = archive_session_entities(db, session, now=now)
    edge_stats = sync_session_edges(db, session, now=now)

    return {
        "session_id": session.session_id,
        "episode_id": session.episode_id,
        "turn_count": session.turn_count,
        "event_count": len(session.events),
        "event_memories": len(session.event_groups),
        "daily_memories": 1,
        "memories_created": created,
        "memories_updated": updated,
        "memory_ids": memory_ids,
        "details_created": detail_stats["created"],
        "details_events": detail_stats["events"],
        "details_skipped": detail_stats.get("skipped", ""),
        "entities": entity_stats,
        "tags_linked": tags_linked,
        "edges": edge_stats,
        "voice_source": session.voice_source,
    }


# ------------------------------------------------------------------ archive_day
def archive_day(
    db: Database,
    config: Config,
    memory: MemoryService,
    target_date: str | date | datetime | float | int | None,
    *,
    now: float | None = None,
    write_voice: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Archive one local day into ``episodes`` and ``memory_items``.

    ``target_date`` is a local date (``date``, ``datetime`` or ISO string);
    bounds are always 00:00-24:00 in ``config.timezone_name``. A day with no
    turns and no events is skipped and reported instead of fabricating an
    episode. The function is idempotent: rerunning the same day updates the
    same episode, event memories and daily memory.
    """
    now = time.time() if now is None else float(now)
    date_text, start, end = _resolve_target_date(target_date, config.timezone_name, now=now)

    if dry_run:
        try:
            with db.transaction():
                report = _archive_day(
                    db, config, memory, date_text, start, end,
                    now=now, write_voice=False, dry_run=True,
                )
                raise _DryRunRollback
        except _DryRunRollback:
            report["dry_run"] = True
            return report

    return _archive_day(
        db, config, memory, date_text, start, end,
        now=now, write_voice=write_voice, dry_run=False,
    )


def _archive_day(
    db: Database,
    config: Config,
    memory: MemoryService,
    date_text: str,
    start: float,
    end: float,
    *,
    now: float,
    write_voice: bool,
    dry_run: bool,
) -> dict[str, Any]:
    # Close events whose last activity is before the day's end. Events that
    # continued into a later day keep their real end and are archived by their
    # start date.
    memory.events.close_open_events(before=end)

    sessions = _collect_day_sessions(db, config, date_text, start, end)
    if not sessions:
        return {
            "date": date_text,
            "skipped": True,
            "reason": "no_data",
            "sessions": 0,
            "episodes": 0,
            "turns": 0,
            "events": 0,
            "event_memories": 0,
            "daily_memories": 0,
            "memories_created": 0,
            "memories_updated": 0,
            "details_created": 0,
            "details_events": 0,
            "entities": {"mentions": 0, "entities": 0, "items_updated": 0, "skipped": ""},
            "tags_linked": 0,
            "memory_ids": [],
            "episode_ids": [],
            "day_summary": {
                "created": False,
                "memory_id": None,
                "sessions": 0,
                "reason": "no_data",
                "summary": "",
            },
            "voice": {
                "enabled": bool(config.get("memory.nightly_voice", True)),
                "target_session": None,
                "selection": "empty",
                "source": "template",
                "llm_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "rejected": "",
            },
            "llm_calls": 0,
            "dry_run": dry_run,
        }

    voice_info = _generate_voice(db, config, sessions, write_voice=write_voice)

    session_reports: list[dict[str, Any]] = []
    for session in sessions:
        session_reports.append(_persist_session(db, memory, session, now=now))

    day_summary = _persist_day_summary(
        db, memory, sessions, date_text, start, end, now=now
    )
    memory_ids: list[str] = []
    for report in session_reports:
        memory_ids.extend(report["memory_ids"])
    if day_summary["memory_id"]:
        memory_ids.append(str(day_summary["memory_id"]))
    return {
        "date": date_text,
        "skipped": False,
        "reason": "",
        "sessions": len(session_reports),
        "episodes": len(session_reports),
        "turns": sum(report["turn_count"] for report in session_reports),
        "events": sum(report["event_count"] for report in session_reports),
        "event_memories": sum(report["event_memories"] for report in session_reports),
        "daily_memories": sum(report["daily_memories"] for report in session_reports),
        "memories_created": sum(
            report["memories_created"] for report in session_reports
        )
        + (1 if day_summary["created"] else 0),
        "memories_updated": sum(
            report["memories_updated"] for report in session_reports
        )
        + (
            0
            if day_summary["created"]
            else (1 if day_summary["memory_id"] else 0)
        ),
        "daily_summary_memories": 1 if day_summary["memory_id"] else 0,
        "details_created": sum(report.get("details_created", 0) for report in session_reports),
        "details_events": sum(report.get("details_events", 0) for report in session_reports),
        "entities_mentions": sum(
            int(report.get("entities", {}).get("mentions", 0))
            for report in session_reports
        ),
        "tags_linked": sum(int(report.get("tags_linked", 0)) for report in session_reports),
        "memory_ids": memory_ids,
        "episode_ids": [report["episode_id"] for report in session_reports],
        "day_summary": day_summary,
        "sessions_detail": session_reports,
        "voice": voice_info,
        "llm_calls": int(voice_info.get("llm_calls", 0)),
        "dry_run": dry_run,
    }


# ----------------------------------------------------------------- retention
def _retention_days(config: Config, key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _prune_rows(
    db: Database,
    table: str,
    time_column: str,
    cutoff: float,
    *,
    where: str = "",
    dry_run: bool = False,
) -> int:
    """Count or delete rows older than ``cutoff`` in one internal table."""
    clause = f"{time_column} < ?"
    if where:
        clause += f" AND ({where})"
    if dry_run:
        row = db.query_one(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {clause}", (cutoff,)
        )
        return int(row["n"]) if row else 0
    cursor = db.execute(f"DELETE FROM {table} WHERE {clause}", (cutoff,))
    return max(0, int(cursor.rowcount or 0))


def prune_history(
    db: Database,
    config: Config,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Trim internal history that is not needed for memory or conversation.

    Deliberately conservative: conversation turns, memories, episodes and
    open loops are never touched.  Only disposable telemetry/queue rows are
    removed, and every window can be disabled by setting it to ``0``.
    """
    now = time.time() if now is None else float(now)
    enabled = bool(config.get("nightly.retention_enabled", True))
    rules = (
        # table, time column, config key, default days, extra WHERE
        ("outbox", "processed_at", "nightly.outbox_retention_days", 7,
         "status IN ('done','dead') AND processed_at IS NOT NULL"),
        ("metrics", "at", "nightly.metrics_retention_days", 30, ""),
        ("decision_log", "at", "nightly.decision_log_retention_days", 180, ""),
        ("turn_traces", "at", "nightly.turn_traces_retention_days", 180, ""),
        ("tool_audit", "at", "nightly.tool_audit_retention_days", 365, ""),
        ("voice_usage", "at", "nightly.voice_usage_retention_days", 365, ""),
        ("photo_jobs", "created_at", "nightly.photo_jobs_retention_days", 90, ""),
    )
    deleted: dict[str, int] = {}
    for table, column, key, default_days, where in rules:
        days = _retention_days(config, key, default_days)
        if not enabled or days <= 0:
            continue
        cutoff = now - days * 86400.0
        count = _prune_rows(
            db, table, column, cutoff, where=where, dry_run=dry_run
        )
        if count:
            deleted[table] = count
    if not dry_run and deleted:
        # Refresh the planner's statistics after a large delete; this is a
        # no-op when the nightly job removed nothing.
        try:
            db.execute("PRAGMA optimize")
        except Exception:
            pass
    return {
        "enabled": enabled,
        "dry_run": dry_run,
        "deleted": deleted,
        "total": sum(deleted.values()),
    }


def expire_open_loops(
    db: Database,
    config: Config,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> int:
    """Mark open loops older than ``context.agenda_expire_days`` as expired.

    An open loop nobody closed for two weeks is a dead promise: it would sit
    in the agenda block every turn and teach the model to ignore it. The
    sweep is a single idempotent UPDATE over ``status='open'`` rows created
    before the cutoff; ``expired`` is a final status, so the row stays as an
    audit trail but never renders again. ``0`` disables the sweep. Runs
    regardless of ``context.agenda_enabled``: open loops exist independently
    of the prompt module, and this is data hygiene, same semantics as
    :func:`prune_history`.
    """
    now = time.time() if now is None else float(now)
    days = _retention_days(config, "context.agenda_expire_days", 14)
    if days <= 0:
        return 0
    cutoff = now - days * 86400.0
    if dry_run:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM open_loops WHERE status='open' AND created_at<?",
            (cutoff,),
        )
        return int(row["n"]) if row else 0
    cursor = db.execute(
        "UPDATE open_loops SET status='expired' WHERE status='open' AND created_at<?",
        (cutoff,),
    )
    return max(0, int(cursor.rowcount or 0))


# ------------------------------------------------------------------ finalize
def finalize_nightly(
    db: Database,
    config: Config,
    memory: MemoryService,
    reports: Iterable[dict[str, Any]],
    life: LifeEngine | None = None,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run compaction once after one or more archived days."""
    now = time.time() if now is None else float(now)
    reports = list(reports)
    compaction = memory.compact(dry_run=dry_run, now=now)
    if life is not None and not dry_run:
        life.tick(now=now)
    retention = prune_history(db, config, now=now, dry_run=dry_run)
    expired_loops = expire_open_loops(db, config, now=now, dry_run=dry_run)
    meta = memory.meta_directory(limit=12)
    last_date = reports[-1]["date"] if reports else None
    episodes = sum(int(report.get("episodes", 0)) for report in reports)
    memories_created = sum(
        int(report.get("memories_created", 0)) for report in reports
    )
    llm_calls = sum(int(report.get("llm_calls", 0)) for report in reports)
    if not dry_run:
        if last_date:
            # Last successfully processed target date, not "today".
            db.set_meta("last_nightly_date", last_date)
        memory.log_decision(
            "nightly",
            {
                "dates": [report.get("date") for report in reports],
                "episodes": episodes,
                "memories_created": memories_created,
                "llm_calls": llm_calls,
                "skipped": [
                    report.get("date")
                    for report in reports
                    if report.get("skipped")
                ],
                "pruned": retention.get("total", 0),
                "expired_loops": expired_loops,
            },
            action="archive",
            outcome="ok",
            model="llm" if llm_calls else "rules",
        )
    return {
        "date": last_date,
        "dates": [report.get("date") for report in reports],
        "archives": reports,
        "episodes": episodes,
        "memories_created": memories_created,
        "diary_written": episodes > 0,
        "compaction": compaction,
        "retention": retention,
        "expired_loops": expired_loops,
        "llm_calls": llm_calls,
        "meta_topics": len(meta),
        "dry_run": dry_run,
    }


def run_nightly(
    db: Database,
    config: Config,
    memory: MemoryService,
    life: LifeEngine | None = None,
    *,
    now: float | None = None,
    target_date: str | date | datetime | float | int | None = None,
    write_voice: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Archive the previous local day (or ``target_date``), then compact once."""
    now = time.time() if now is None else float(now)
    report = archive_day(
        db,
        config,
        memory,
        target_date,
        now=now,
        write_voice=write_voice,
        dry_run=dry_run,
    )
    result = finalize_nightly(
        db, config, memory, [report], life, now=now, dry_run=dry_run
    )
    result["target_date"] = report["date"]
    result["archive"] = report
    result["dry_run"] = dry_run
    return result
