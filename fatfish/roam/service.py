"""Multi-session roaming service (ADR-0014).

P0 scope: owner-directed single-room trips, persistent thread cards, the
attention block, idle return, deterministic summary/key facts, owner recap and
restart reconciliation.  Scheduling/proposals (``autonomy='ask'``) and daily
scoring are P1; this module keeps their data model in place.

Privacy invariants enforced here:
* a room must be a bound private session (groups and strangers are refused);
* a thread card carries a summary, never another room's transcript;
* target rooms only ever see their own card -- the recap is owner-only;
* summaries are written to owner-side memory under ``origin_scope='roam'`` and
  are not visible from the target session.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from ..clock import day_bounds, human_time, in_quiet_hours, to_local
from ..db import Database
from ..owner import ROLE_OWNER, uin_from_session_id
from .intent import parse_return_command, parse_visit_command
from .model import (
    DEFAULT_ALLOWED_ROLES,
    OPEN_TRIP_STATUSES,
    Room,
    RoamTrip,
    SummaryPayload,
    THREAD_BLOCKED,
    THREAD_IDLE,
    THREAD_OPEN,
    TRIP_ACTIVE,
    TRIP_ENTERING,
    TRIP_INTERRUPTED,
    TRIP_LEAVING,
    TRIP_RETURNED,
    ThreadCard,
)
from .summary import deterministic_summary

DEFAULT_IDLE_RETURN_MINUTES = 20
DEFAULT_MAX_TRIP_MINUTES = 60
DEFAULT_DAILY_MINUTES = 30
DEFAULT_MAX_TRIPS_PER_DAY = 6
DEFAULT_MAX_OPEN_THREADS = 6
DEFAULT_PER_THREAD_COOLDOWN_MINUTES = 60
DEFAULT_ATTENTION_MAX_CHARS = 800
DEFAULT_SUMMARY_MAX_CHARS = 600
DEFAULT_PROPOSAL_TTL_MINUTES = 30
DEFAULT_MAX_PROPOSALS_PER_DAY = 6

ATTENTION_HEADER = "【当前注意力】"
RECAP_HEADER = "【漫游回顾】"

_OWNER_COMMAND_FORCE = True


class RoamService:
    def __init__(self, db: Database, config: Any, *, memory: Any | None = None):
        self.db = db
        self.config = config
        self.memory = memory

    # ------------------------------------------------------------------ config
    def enabled(self) -> bool:
        return bool(self.config.get("roam.enabled", True))

    def idle_return_minutes(self) -> int:
        return max(1, int(self.config.get("roam.idle_return_minutes", DEFAULT_IDLE_RETURN_MINUTES)))

    def max_trip_minutes(self) -> int:
        return max(1, int(self.config.get("roam.max_trip_minutes", DEFAULT_MAX_TRIP_MINUTES)))

    def daily_minutes(self) -> int:
        return max(0, int(self.config.get("roam.daily_minutes", DEFAULT_DAILY_MINUTES)))

    def max_trips_per_day(self) -> int:
        return max(0, int(self.config.get("roam.max_trips_per_day", DEFAULT_MAX_TRIPS_PER_DAY)))

    def max_open_threads(self) -> int:
        return max(1, int(self.config.get("roam.max_open_threads", DEFAULT_MAX_OPEN_THREADS)))

    def cooldown_minutes(self) -> int:
        return max(0, int(self.config.get("roam.per_thread_cooldown_minutes", DEFAULT_PER_THREAD_COOLDOWN_MINUTES)))

    def attention_max_chars(self) -> int:
        return max(120, int(self.config.get("roam.attention_max_chars", DEFAULT_ATTENTION_MAX_CHARS)))

    def summary_max_chars(self) -> int:
        return max(80, int(self.config.get("roam.summary_max_chars", DEFAULT_SUMMARY_MAX_CHARS)))

    def summary_key_facts(self) -> bool:
        return bool(self.config.get("roam.summary_key_facts", True))

    def notify_return(self) -> bool:
        return bool(self.config.get("roam.notify_return", True))

    def quiet_hours_respect(self) -> bool:
        return bool(self.config.get("roam.quiet_hours_respect", True))

    def allowed_roles(self) -> list[str]:
        raw = self.config.get("roam.allowed_roles", list(DEFAULT_ALLOWED_ROLES))
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        roles = [str(role or "").strip().lower() for role in (raw or []) if str(role or "").strip()]
        return roles or list(DEFAULT_ALLOWED_ROLES)

    def include_whitelisted_groups(self) -> bool:
        return bool(self.config.get("roam.include_whitelisted_groups", False))

    def quiet_window(self) -> tuple[str, str]:
        start = str(self.config.get("share.quiet_hours_start", "23:30")).strip() or "23:30"
        end = str(self.config.get("share.quiet_hours_end", "08:00")).strip() or "08:00"
        return start, end

    def quiet_now(self, now: float | None = None) -> bool:
        if not self.quiet_hours_respect():
            return False
        now = time.time() if now is None else float(now)
        start, end = self.quiet_window()
        try:
            return in_quiet_hours(now, start, end, self.config.timezone_name)
        except Exception:
            return False

    # ------------------------------------------------------------------- rooms
    def rooms(self) -> list[Room]:
        """Every private session the standing grant allows, in binding order."""
        try:
            rows = self.db.query(
                "SELECT session_id, display_name, role FROM owner_bindings ORDER BY bound_at"
            )
        except Exception:
            return []
        allowed = set(self.allowed_roles())
        extra = self.config.get("roam.allowed_sessions", []) or []
        if isinstance(extra, str):
            extra = [extra]
        extra_sessions = {
            str(item or "").strip() for item in extra if str(item or "").strip()
        }
        rooms: list[Room] = []
        seen: set[str] = set()
        for row in rows:
            session_id = str(row["session_id"] or "").strip()
            role = str(row["role"] or "").strip().lower()
            if not session_id or session_id in seen:
                continue
            if role == ROLE_OWNER:
                continue
            is_private = session_id.startswith("qq:private:") or ":private:" in session_id
            if not is_private:
                if not (self.include_whitelisted_groups() and session_id.startswith("qq:group:")):
                    continue
            if role not in allowed and session_id not in extra_sessions:
                continue
            uin = uin_from_session_id(session_id)
            display = str(row["display_name"] or "").strip() or "联系人"
            rooms.append(
                Room(
                    session_id=session_id,
                    role=role,
                    display_name=display,
                    target_uin=uin,
                    aliases=self._aliases_for_uin(uin),
                )
            )
            seen.add(session_id)
        return rooms

    def room_for_session(self, session_id: str) -> Room | None:
        sid = str(session_id or "").strip()
        for room in self.rooms():
            if room.session_id == sid:
                return room
        return None

    def resolve_room(self, name_or_session: str) -> Room | None:
        value = str(name_or_session or "").strip()
        if not value:
            return None
        rooms = self.rooms()
        for room in rooms:
            if room.session_id == value:
                return room
        for room in rooms:
            if value in room.names():
                return room
        matches = [room for room in rooms if any(value in name for name in room.names())]
        return matches[0] if len(matches) == 1 else None

    def _aliases_for_uin(self, uin: str) -> list[str]:
        if not uin:
            return []
        try:
            from ..memory.entities import entity_card, entity_for_uin

            entity_id = entity_for_uin(self.db, uin)
            if not entity_id:
                return []
            card = entity_card(self.db, entity_id) or {}
        except Exception:
            return []
        aliases = card.get("aliases") or []
        return [str(alias).strip() for alias in aliases if str(alias or "").strip()]

    def owner_session(self) -> str:
        try:
            row = self.db.query_one(
                "SELECT session_id FROM owner_bindings WHERE role=? LIMIT 1",
                (ROLE_OWNER,),
            )
        except Exception:
            return ""
        return str(row["session_id"]) if row else ""

    # ---------------------------------------------------------------- threads
    def get_thread(self, session_id: str) -> ThreadCard | None:
        row = self.db.query_one(
            "SELECT * FROM session_threads WHERE session_id=?",
            (str(session_id),),
        )
        return ThreadCard.from_row(row) if row else None

    def thread_count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM session_threads")
        return int(row["n"]) if row else 0

    def upsert_thread(
        self,
        session_id: str,
        *,
        role: str = "",
        display_name: str = "",
        target_uin: str = "",
        status: str | None = None,
        now: float | None = None,
        **fields: Any,
    ) -> ThreadCard:
        now = time.time() if now is None else float(now)
        current = self.get_thread(session_id)
        values: dict[str, Any] = {
            "role": role or (current.role if current else ""),
            "display_name": display_name or (current.display_name if current else ""),
            "target_uin": target_uin or (current.target_uin if current else ""),
            "status": status or (current.status if current else THREAD_IDLE),
        }
        json_fields = {"open_loops": "open_loops_json", "commitments": "commitments_json", "key_facts": "key_facts_json"}
        for key, column in json_fields.items():
            if key in fields and fields[key] is not None:
                values[column] = json.dumps(list(fields.pop(key)), ensure_ascii=False)
        for key in (
            "summary",
            "last_topic",
            "mood",
            "unread_count",
            "attention_score",
            "last_seen_at",
            "last_summary_at",
            "last_turn_id",
            "cooldown_until",
            "last_trip_at",
            "trip_count",
            "total_seconds",
        ):
            if key in fields and fields[key] is not None:
                values[key] = fields[key]
        if current is None:
            values.setdefault("last_seen_at", now)
            columns = ", ".join(["session_id", *values.keys(), "created_at", "updated_at"])
            marks = ",".join("?" for _ in range(len(values) + 3))
            self.db.execute(
                f"INSERT INTO session_threads({columns}) VALUES ({marks})",
                (session_id, *values.values(), now, now),
            )
        else:
            assignments = ", ".join(f"{column}=?" for column in values)
            self.db.execute(
                f"UPDATE session_threads SET {assignments}, updated_at=? WHERE session_id=?",
                (*values.values(), now, session_id),
            )
        return self.get_thread(session_id) or ThreadCard(session_id=session_id)

    # ------------------------------------------------------------------ trips
    def active_trip(self) -> RoamTrip | None:
        row = self.db.query_one(
            "SELECT * FROM roam_trips WHERE status IN (?,?,?) "
            "ORDER BY id DESC LIMIT 1",
            OPEN_TRIP_STATUSES,
        )
        return RoamTrip.from_row(row) if row else None

    def trip(self, trip_id: str) -> RoamTrip | None:
        row = self.db.query_one(
            "SELECT * FROM roam_trips WHERE trip_id=?", (str(trip_id),)
        )
        return RoamTrip.from_row(row) if row else None

    def trips_today(self, now: float) -> int:
        start, end = day_bounds(now, self.config.timezone_name)
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM roam_trips "
            "WHERE started_at>=? AND started_at<? AND status IN (?,?,?,?,?)",
            (
                start,
                end,
                TRIP_ENTERING,
                TRIP_ACTIVE,
                TRIP_LEAVING,
                TRIP_RETURNED,
                TRIP_INTERRUPTED,
            ),
        )
        return int(row["n"]) if row else 0

    def minutes_today(self, now: float) -> float:
        start, end = day_bounds(now, self.config.timezone_name)
        row = self.db.query_one(
            "SELECT COALESCE(SUM(COALESCE(ended_at, ?) - COALESCE(started_at, created_at)), 0) AS s "
            "FROM roam_trips WHERE started_at>=? AND started_at<? AND status IN (?,?,?,?,?)",
            (
                float(now),
                start,
                end,
                TRIP_ENTERING,
                TRIP_ACTIVE,
                TRIP_LEAVING,
                TRIP_RETURNED,
                TRIP_INTERRUPTED,
            ),
        )
        return float(row["s"] or 0.0) / 60.0 if row else 0.0

    def start_trip(
        self,
        thread_session: str,
        *,
        purpose: str = "",
        home_session: str | None = None,
        source: str = "owner",
        force: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Open one trip into a bound room.

        Refuses (never guesses) when the room is not in the standing grant, a
        trip is already running, the room is cooling down, or the daily budget
        is spent.  ``source='owner'`` bypasses only the per-room cooldown -- the
        owner can ask again immediately; daily caps still apply.
        """
        now = time.time() if now is None else float(now)
        if not self.enabled():
            return {"ok": False, "error": "roam_disabled"}
        room = self.room_for_session(thread_session)
        if room is None:
            resolved = self.resolve_room(thread_session)
            if resolved is None:
                return {"ok": False, "error": "room_not_allowed"}
            room = resolved
        if room.session_id.startswith(("qq:group:", "group:")):
            # Boss decision 2026-11-01: groups are outside roaming, and P0
            # has no group path at all (the QQ outbox refuses it too).
            return {"ok": False, "error": "roam_group_not_allowed"}
        active = self.active_trip()
        if active is not None:
            if active.thread_session == room.session_id:
                return {"ok": True, "existing": True, "trip": active, "room": room}
            return {"ok": False, "error": "roam_busy", "trip": active}
        thread = self.get_thread(room.session_id)
        if (
            thread is not None
            and thread.status == THREAD_BLOCKED
            and thread.cooldown_until
            and now < float(thread.cooldown_until)
        ):
            # The target's own "别聊了" outranks an owner order: a blocked
            # room stays closed even when the owner asks again today.
            return {"ok": False, "error": "thread_blocked"}
        if thread is not None and thread.cooldown_until and now < thread.cooldown_until:
            if not (force or source == "owner"):
                return {"ok": False, "error": "thread_cooldown", "cooldown_until": thread.cooldown_until}
        if not force:
            if self.max_trips_per_day() and self.trips_today(now) >= self.max_trips_per_day():
                return {"ok": False, "error": "daily_trip_limit"}
            if self.daily_minutes() and self.minutes_today(now) >= self.daily_minutes():
                return {"ok": False, "error": "daily_minute_limit"}
            if thread is None and self.thread_count() >= self.max_open_threads():
                return {"ok": False, "error": "too_many_threads"}
        home = str(home_session or self.owner_session() or "").strip()
        trip_id = f"roam-{int(now * 1000)}-{uuid.uuid4().hex[:8]}"
        thread = self.upsert_thread(
            room.session_id,
            role=room.role,
            display_name=room.display_name,
            target_uin=room.target_uin,
            status=THREAD_OPEN,
            now=now,
            last_seen_at=now,
            last_trip_at=now,
            trip_count=(thread.trip_count + 1 if thread else 1),
        )
        purpose_text = str(purpose or "").strip()[:300]
        self.db.execute(
            "INSERT INTO roam_trips(trip_id, thread_session, home_session, purpose, "
            "purpose_json, status, authorization_json, started_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                trip_id,
                room.session_id,
                home,
                purpose_text,
                _json({"source": source, "display_name": room.display_name}),
                TRIP_ENTERING,
                _json({"roles": self.allowed_roles(), "source": source}),
                now,
                now,
            ),
        )
        self._log(
            "departed",
            f"trip:{trip_id}",
            {"room": room.session_id, "purpose": purpose_text, "source": source},
            session_id=room.session_id,
            now=now,
        )
        trip = self.trip(trip_id)
        return {"ok": True, "existing": False, "trip": trip, "room": room, "thread": thread}

    def activate(self, trip_id: str, *, now: float | None = None) -> RoamTrip | None:
        now = time.time() if now is None else float(now)
        self.db.execute(
            "UPDATE roam_trips SET status=? WHERE trip_id=? AND status IN (?,?)",
            (TRIP_ACTIVE, str(trip_id), TRIP_ENTERING, TRIP_ACTIVE),
        )
        return self.trip(trip_id)

    def trip_id_for(self, session_id: str) -> str | None:
        active = self.active_trip()
        if active is not None and active.thread_session == str(session_id):
            return active.trip_id
        return None

    # ---------------------------------------------------------------- inbound
    def note_inbound(
        self, session_id: str, *, now: float | None = None, is_group: bool = False
    ) -> dict[str, Any]:
        """Account for a message arriving while a trip may be running.

        A target message resets the idle clock.  An owner message (in the home
        session) preempts the trip immediately, before the owner turn is
        assembled, so that turn already carries the recap.  Messages from other
        rooms only raise their unread count; they never switch the trip.
        """
        now = time.time() if now is None else float(now)
        sid = str(session_id or "").strip()
        active = self.active_trip()
        if active is None:
            thread = self.get_thread(sid) if sid else None
            if thread is not None and sid != self.owner_session():
                self.db.execute(
                    "UPDATE session_threads SET unread_count=unread_count+1, updated_at=? "
                    "WHERE session_id=?",
                    (now, sid),
                )
            return {"kind": "none"}
        if active.thread_session == sid:
            self.db.execute(
                "UPDATE roam_trips SET message_count=message_count+1 WHERE trip_id=?",
                (active.trip_id,),
            )
            self.db.execute(
                "UPDATE session_threads SET unread_count=0, last_seen_at=?, "
                "status=?, updated_at=? WHERE session_id=?",
                (now, THREAD_OPEN, now, sid),
            )
            return {"kind": "target", "trip": active}
        is_owner = sid == active.home_session or sid == self.owner_session()
        if not is_owner and not is_group and sid:
            thread = self.get_thread(sid)
            if thread is not None:
                self.db.execute(
                    "UPDATE session_threads SET unread_count=unread_count+1, updated_at=? "
                    "WHERE session_id=?",
                    (now, sid),
                )
                return {"kind": "other_unread", "trip": active}
        if is_owner:
            left = self.leave_trip(active, reason="owner_preempt", now=now, notify=False)
            return {"kind": "owner_preempt", "trip": active, "left": left}
        return {"kind": "other", "trip": active}

    def target_stop(self, session_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        """The target asked her to stop: leave now and block the room today."""
        now = time.time() if now is None else float(now)
        active = self.active_trip()
        if active is None or active.thread_session != str(session_id or "").strip():
            return None
        left = self.leave_trip(active, reason="target_stop", now=now, notify=False)
        _, end = day_bounds(now, self.config.timezone_name)
        self.db.execute(
            "UPDATE session_threads SET status=?, cooldown_until=?, updated_at=? "
            "WHERE session_id=?",
            (THREAD_BLOCKED, end, now, active.thread_session),
        )
        self._log(
            "target_stop",
            f"trip:{active.trip_id}",
            {"room": active.thread_session},
            session_id=active.thread_session,
            now=now,
        )
        return left

    # --------------------------------------------------------------- leaving
    def return_reason(self, trip: RoamTrip | None = None, *, now: float | None = None) -> str | None:
        now = time.time() if now is None else float(now)
        trip = trip or self.active_trip()
        if trip is None:
            return None
        start = float(trip.started_at or trip.created_at or now)
        if now - start >= self.max_trip_minutes() * 60:
            return "max_duration"
        last = self._last_seen(trip)
        if now - last >= self.idle_return_minutes() * 60:
            return "idle"
        if self.quiet_now(now) and not self.quiet_now(start):
            # A trip the owner explicitly forced *into* quiet hours keeps its
            # normal idle/max lifetime; only crossing into quiet ends a trip.
            return "quiet_hours"
        return None

    def leave_trip(
        self,
        trip: RoamTrip | str,
        *,
        reason: str = "idle",
        summary: SummaryPayload | None = None,
        now: float | None = None,
        notify: bool | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        if isinstance(trip, str):
            started = self.trip(trip)
        else:
            started = trip
        if started is None:
            return {"ok": False, "error": "trip_not_found"}
        if started.status not in OPEN_TRIP_STATUSES:
            return {"ok": False, "error": "trip_not_open", "trip": started}
        turns = self._trip_turns(started)
        thread = self.get_thread(started.thread_session)
        room = self.room_for_session(started.thread_session)
        display = (
            (thread.display_name if thread else "")
            or (room.display_name if room else "")
            or "对方"
        )
        payload = summary or deterministic_summary(
            turns,
            purpose=started.purpose,
            display_name=display,
            max_chars=self.summary_max_chars(),
            include_quotes=bool(self.config.get("roam.summary_include_quotes", False)),
        )
        if not self.summary_key_facts():
            payload.key_facts = []
        duration = max(0.0, now - float(started.started_at or started.created_at or now))
        user_turns = sum(1 for turn in turns if str(turn.get("role") or "") == "user")
        status = TRIP_INTERRUPTED if reason == "interrupted" else TRIP_RETURNED
        last_turn_id = max((int(turn["id"]) for turn in turns if turn.get("id") is not None), default=None)
        self.db.execute(
            "UPDATE roam_trips SET status=?, ended_at=?, close_reason=?, summary=?, "
            "summary_json=?, message_count=? WHERE trip_id=?",
            (
                status,
                now,
                str(reason or ""),
                payload.summary,
                _json(payload.to_json()),
                len(turns),
                started.trip_id,
            ),
        )
        existing = thread or ThreadCard(session_id=started.thread_session)
        cooldown_until = now + self.cooldown_minutes() * 60
        # An opening still waiting in the outbox must not land after the trip
        # ended (owner preempt / target stop / restart).  A sent opening is
        # already 'done' and is left untouched.
        self.db.execute(
            "UPDATE outbox SET status='cancelled' "
            "WHERE idempotency_key=? AND status='pending'",
            (f"roam:{started.trip_id}:opening",),
        )
        self.upsert_thread(
            started.thread_session,
            role=existing.role,
            display_name=display,
            target_uin=existing.target_uin,
            status=THREAD_IDLE,
            now=now,
            summary=payload.summary,
            last_topic=(payload.topics[-1] if payload.topics else existing.last_topic),
            key_facts=payload.key_facts,
            open_loops=payload.open_loops,
            commitments=payload.commitments,
            mood=payload.mood,
            last_seen_at=max(float(existing.last_seen_at or 0.0), now),
            last_summary_at=now,
            last_turn_id=last_turn_id or existing.last_turn_id,
            cooldown_until=cooldown_until,
            total_seconds=float(existing.total_seconds or 0.0) + duration,
        )
        self._write_memory(started, payload, turns=turns, now=now)
        self._log(
            "left",
            reason,
            {
                "trip": started.trip_id,
                "room": started.thread_session,
                "minutes": round(duration / 60.0, 2),
                "turns": len(turns),
                "degraded": payload.degraded,
            },
            session_id=started.thread_session,
            now=now,
        )
        should_notify = self.notify_return() if notify is None else bool(notify)
        updated = self.trip(started.trip_id) or started
        return {
            "ok": True,
            "trip": updated,
            "thread": self.get_thread(started.thread_session),
            "summary": payload,
            "duration_seconds": duration,
            "duration_minutes": max(0, int(round(duration / 60.0))),
            "user_turns": user_turns,
            "notify": should_notify and reason not in {"owner_preempt", "target_stop", "interrupted"},
            "display_name": display,
        }

    def reconcile_open_trips(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Startup reconciliation: close open trips without replaying them.

        A trip that was ``entering/active`` when the process died is marked
        ``interrupted`` and its thread card is updated with whatever turns were
        recorded.  No opening message is sent, no follow-up is scheduled, and
        the room's unread counter stays for the scheduler (P1).
        """
        now = time.time() if now is None else float(now)
        results: list[dict[str, Any]] = []
        for trip in self._open_trips():
            results.append(
                self.leave_trip(trip, reason="interrupted", now=now, notify=False)
            )
        return results

    def _open_trips(self) -> list[RoamTrip]:
        rows = self.db.query(
            "SELECT * FROM roam_trips WHERE status IN (?,?,?) ORDER BY id",
            OPEN_TRIP_STATUSES,
        )
        return [RoamTrip.from_row(row) for row in rows]

    def _trip_turns(self, trip: RoamTrip) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE trip_id=? ORDER BY id",
            (trip.trip_id,),
        )
        if rows:
            return [dict(row) for row in rows]
        start = float(trip.started_at or trip.created_at or 0.0)
        rows = self.db.query(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE session_id=? AND created_at>=? AND created_at<=? "
            "ORDER BY id",
            (trip.thread_session, start, float(trip.ended_at or time.time())),
        )
        return [dict(row) for row in rows]

    def _last_seen(self, trip: RoamTrip) -> float:
        row = self.db.query_one(
            "SELECT last_seen_at FROM session_threads WHERE session_id=?",
            (trip.thread_session,),
        )
        value = float(row["last_seen_at"] or 0.0) if row and row["last_seen_at"] else 0.0
        return max(value, float(trip.started_at or trip.created_at or 0.0))

    def _write_memory(
        self,
        trip: RoamTrip,
        payload: SummaryPayload,
        *,
        turns: list[dict[str, Any]],
        now: float,
    ) -> None:
        if self.memory is None:
            return
        if not trip.home_session:
            # Without the owner's home session the row would have no non-public
            # session id, and the non-owner recall gate treats a NULL/empty
            # session as "global".  Skip rather than risk a cross-session leak.
            return
        text = payload.memory_text()
        if not text:
            return
        thread = self.get_thread(trip.thread_session)
        display = (
            (thread.display_name if thread else "")
            or trip.purpose_json.get("display_name")
            or "对方"
        )
        try:
            self.memory.add_item(
                text,
                type="episodic",
                scope="global",
                session_id=trip.home_session or None,
                event_time_start=float(trip.started_at or trip.created_at or now),
                event_time_end=now,
                importance=0.5,
                confidence=0.8,
                source_type="roam",
                source_id=trip.trip_id,
                # Owner-only by construction: the room's raw transcript is not
                # copied anywhere, and the target session never sees this row.
                origin_scope="roam",
                audience="owner",
                tags=["漫游", display],
                subject="",
            )
        except Exception:
            # Summary persistence must never undo the trip close.
            pass

    # -------------------------------------------------------------- attention
    def opening_text(self, trip: RoamTrip | dict[str, Any]) -> str:
        trip_obj = trip if isinstance(trip, RoamTrip) else self.trip(str(trip.get("trip_id") or ""))
        if trip_obj is None:
            return ""
        thread = self.get_thread(trip_obj.thread_session)
        name = (thread.display_name if thread else "") or "你"
        purpose = " ".join(str(trip_obj.purpose or "").split())
        if purpose:
            text = f"{name}，主人哥哥让我来问问你：{purpose}。现在方便说话吗？"
        else:
            text = f"{name}，主人哥哥让我来看看你。最近怎么样？"
        return text[:200]

    def notify_return_text(
        self, trip: RoamTrip | dict[str, Any], *, summary: SummaryPayload | None = None
    ) -> str:
        trip_obj = trip if isinstance(trip, RoamTrip) else self.trip(str(trip.get("trip_id") or ""))
        if trip_obj is None:
            return ""
        payload = summary or SummaryPayload.from_json(trip_obj.summary_json)
        thread = self.get_thread(trip_obj.thread_session)
        name = (thread.display_name if thread else "") or "对方"
        minutes = trip_obj.duration_minutes()
        body = payload.summary or trip_obj.summary or "没聊什么特别的。"
        if minutes >= 1:
            text = f"我回来了。刚和{name}聊了 {minutes} 分钟：{body}"
        else:
            text = f"我回来了。刚去看了一下{name}：{body}"
        return " ".join(text.split())[:300]

    def recap_lines(
        self, *, now: float | None = None, limit: int = 3, pending_only: bool = True
    ) -> list[str]:
        now = time.time() if now is None else float(now)
        seen = self._recap_watermark()
        rows = self.db.query(
            "SELECT * FROM roam_trips WHERE status IN (?,?) AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT ?",
            (TRIP_RETURNED, TRIP_INTERRUPTED, max(1, limit * 3)),
        )
        lines: list[str] = []
        for row in rows:
            trip = RoamTrip.from_row(row)
            if pending_only and trip.id <= seen:
                continue
            if trip.ended_at is None or now - float(trip.ended_at) > 48 * 3600:
                continue
            thread = self.get_thread(trip.thread_session)
            name = (thread.display_name if thread else "") or "对方"
            windows = _window_label(
                float(trip.started_at or trip.created_at or now),
                float(trip.ended_at),
                self.config.timezone_name,
                now,
            )
            summary = " ".join(str(trip.summary or "").split())[:120]
            lines.append(f"{windows} 去了{name}：{summary}")
            if len(lines) >= limit:
                break
        return lines

    def pending_recap_ids(self, *, now: float | None = None, limit: int = 3) -> list[int]:
        now = time.time() if now is None else float(now)
        seen = self._recap_watermark()
        rows = self.db.query(
            "SELECT id, ended_at FROM roam_trips WHERE status IN (?,?) AND id>? "
            "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT ?",
            (TRIP_RETURNED, TRIP_INTERRUPTED, seen, max(1, limit)),
        )
        return [
            int(row["id"])
            for row in rows
            if row["ended_at"] is not None and now - float(row["ended_at"]) <= 48 * 3600
        ]

    def ack_recaps(self, ids: Iterable[int]) -> None:
        values = [int(value) for value in ids if value is not None]
        if not values:
            return
        top = max(values)
        if top > self._recap_watermark():
            self.db.set_meta("roam_recap_seen", str(top))

    def _recap_watermark(self) -> int:
        raw = self.db.get_meta("roam_recap_seen", "0") or "0"
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def attention_block(
        self, session_id: str, *, role: str | None = None, now: float | None = None
    ) -> str:
        """Prompt block placed after ``past_timeline`` and before ``history``.

        * Target room: only its own card and this trip's purpose.
        * Owner room: the recap of recent trips (and where she is right now).
        * Everywhere else: empty.
        """
        now = time.time() if now is None else float(now)
        sid = str(session_id or "").strip()
        active = self.active_trip()
        if active is not None and active.thread_session == sid:
            thread = self.get_thread(sid)
            name = (thread.display_name if thread else "") or "对方"
            summary = (thread.summary if thread else "") or "还没有线程卡，第一次来"
            topic = (thread.last_topic if thread else "") or ""
            loops = (thread.open_loops if thread else [])[:2]
            lines = [
                ATTENTION_HEADER,
                f"本次目的：{active.purpose or '看看对方最近怎么样'}",
                f"线程卡（{name}）：{summary}",
            ]
            if topic and topic not in summary:
                lines.append(f"上次话题：{topic}")
            if loops:
                lines.append("待办：" + "；".join(loops))
            lines.append(
                f"返回：{self.idle_return_minutes()} 分钟没有新消息，或主人叫我回去；"
                f"最长 {self.max_trip_minutes()} 分钟"
            )
            return self._cap_block("\n".join(lines))
        if role == ROLE_OWNER or sid == self.owner_session():
            lines: list[str] = []
            recap = self.recap_lines(now=now, limit=3)
            if recap:
                lines.append(RECAP_HEADER)
                lines.extend(f"- {line}" for line in recap)
            if active is not None:
                thread = self.get_thread(active.thread_session)
                name = (thread.display_name if thread else "") or "对方"
                minutes = active.duration_minutes(now=now)
                lines.append(ATTENTION_HEADER)
                lines.append(
                    f"你现在在{name}那边：{active.purpose or '看看对方'}（已经 {minutes} 分钟）。"
                    "主人说话要先回主人。"
                )
            return self._cap_block("\n".join(lines))
        return ""

    def _cap_block(self, text: str) -> str:
        limit = self.attention_max_chars()
        if len(text) <= limit:
            return text
        lines: list[str] = []
        used = 0
        for line in text.splitlines():
            if used + len(line) + 1 > limit:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    # --------------------------------------------------------------- commands
    def maybe_owner_command(self, text: str) -> dict[str, Any] | None:
        if not self.enabled():
            return None
        return parse_visit_command(text, self.rooms())

    def is_return_command(self, text: str) -> bool:
        return parse_return_command(text)

    # ------------------------------------------------------------------- logs
    def _log(
        self,
        action: str,
        outcome: str = "",
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        now: float | None = None,
    ) -> None:
        if self.memory is None:
            return
        try:
            self.memory.log_decision(
                "roam",
                payload or {},
                action=action,
                outcome=str(outcome or ""),
                session_id=session_id,
                at=now,
            )
        except Exception:
            pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _window_label(start: float, end: float, tz_name: str, now: float) -> str:
    start_local = to_local(start, tz_name)
    end_local = to_local(end, tz_name)
    today = to_local(now, tz_name).date()
    if start_local.date() == today and end_local.date() == today:
        prefix = "今天"
    elif start_local.date() == end_local.date():
        prefix = start_local.strftime("%m-%d")
    else:
        prefix = f"{start_local.strftime('%m-%d')}–{end_local.strftime('%m-%d')}"
    return f"{prefix} {start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"


__all__ = [
    "ATTENTION_HEADER",
    "RECAP_HEADER",
    "RoamService",
]
