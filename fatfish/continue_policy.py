"""Continue-policy engine (T-0029): should she come back after her own turn?

Replaces the T-0027 "always hang +60s" hook with an explicit three-signal
decision -- topic completeness (``open_loops`` is the source of truth), her
own desire to keep chatting (0-1 self-report), and the other side's stated
will (``chat_stop`` / ``busy`` / ``topic_change`` flags). The engine is pure
bookkeeping over ``schema_meta`` and ``open_loops``: no LLM, no network, no
schema change, fully unit-testable offline.

The coax ladder: up to 3 rungs at +1/+10/+30 minutes, rung count scaled by
desire (<0.5 -> 1, 0.5-0.8 -> 2, >=0.8 -> 3). Rungs live in the
``self_continue`` wakeup lane (cancel-on-reply kills the whole ladder) but
are budgeted here, per session per day, instead of ProactiveGate's shared
90-minute pool -- a +1-minute nudge must not be strangled by a pool that
exists to space rituals apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .clock import human_date

DEFAULT_COAX_INTERVALS_MINUTES: tuple[float, ...] = (1.0, 10.0, 30.0)

#: Per-session daily ladder budget when ``chat.coax_max_rungs_per_day`` is absent.
DEFAULT_MAX_RUNGS_PER_DAY = 6

# One rung beyond the ladder's last interval is never scheduled.
MAX_LADDER_RUNGS = 3

# Inbound fast paths (the LLM's ``user_wants`` tag is the second opinion).
# The sim walkthrough (T-0031) caught real phrasings people actually type:
# "行了不聊了""我去洗澡睡觉了" must land as chat_stop, not as silence-by-luck.
CHAT_STOP_KEYWORDS = (
    "不要聊",
    "别聊了",
    "不聊了",
    "先不聊",
    "不想聊",
    "别烦我",
    "不要说话",
    "别说了",
    "不用回我",
    "睡觉了",
    "我去睡了",
    "我先睡了",
    "晚安",
)
BUSY_KEYWORDS = (
    "在忙",
    "我很忙",
    "忙着",
    "现在忙",
    "我先忙",
    "洗澡",
    "等会再说",
    "等下再说",
    "稍后再说",
    "忙完了再说",
    "开会中",
)
TOPIC_CHANGE_KEYWORDS = ("换个话题", "换话题", "别聊这个", "不聊这个")

STOP_LEVELS = ("chat_stop", "busy", "topic_change")


@dataclass(slots=True)
class ContinueDecision:
    """What the engine decided at the end of one of her outbound turns."""

    action: str  # "arm" | "none"
    rungs: int = 0
    first_delay_seconds: float = 0.0
    reason: str = ""
    loop_id: str | None = None


def classify_stop_keywords(text: str) -> str | None:
    """Keyword fast path for the other side's stated will (most specific first)."""
    body = (text or "").strip()
    if not body:
        return None
    for keyword in TOPIC_CHANGE_KEYWORDS:
        if keyword in body:
            return "topic_change"
    for keyword in CHAT_STOP_KEYWORDS:
        if keyword in body:
            return "chat_stop"
    for keyword in BUSY_KEYWORDS:
        if keyword in body:
            return "busy"
    return None


def clamp_desire(value: Any) -> float | None:
    """A honest 0-1 desire, or None when missing/garbage (None means low)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        desire = float(value)
    except (TypeError, ValueError):
        return None
    if desire != desire:  # NaN
        return None
    return max(0.0, min(1.0, desire))


def _to_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:
        return default
    return out


def _parse_intervals(raw: Any) -> tuple[float, ...]:
    """``[1, 10, 30]`` or ``"1,10,30"``; drops junk, keeps at least one."""
    items: list[Any]
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        items = [part for part in raw.replace(";", ",").split(",")]
    else:
        items = list(DEFAULT_COAX_INTERVALS_MINUTES)
    out: list[float] = []
    for item in items[:MAX_LADDER_RUNGS]:
        try:
            minutes = float(item)
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            out.append(minutes)
    return tuple(out) if out else DEFAULT_COAX_INTERVALS_MINUTES


def _parse_daily_cap(raw: Any) -> int:
    """``chat.coax_max_rungs_per_day`` -> rungs/day; ``0`` means unlimited.

    Positive values keep the old meaning exactly (``max(1, int(raw))`` is the
    identity for >= 1).  A missing/garbage value still falls back to the
    documented default of 6.  ``0`` used to be swallowed by ``... or 6`` and
    became 6; since 2026-11-15 it means "no daily cap".
    """
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNGS_PER_DAY
    if parsed == 0:
        return 0
    return max(1, parsed)


class ContinuePolicy:
    """Signals + ladder bookkeeping; the runtime owns scheduling and turns."""

    def __init__(self, db: Any, config: Any):
        self.db = db
        self.config = config
        self.tz_name = config.timezone_name
        self.enabled = bool(config.get("chat.continue_policy_enabled", True))
        self.intervals = _parse_intervals(
            config.get("chat.coax_intervals_minutes", list(DEFAULT_COAX_INTERVALS_MINUTES))
        )
        self.desire_low = _to_float(config.get("chat.coax_desire_low", 0.5), 0.5)
        self.desire_high = _to_float(config.get("chat.coax_desire_high", 0.8), 0.8)
        # Per-session daily ladder budget; 0 = unlimited (2026-11-15), >0 keeps
        # the historical "stop after N delivered rungs today" rule.
        self.max_rungs_per_day = _parse_daily_cap(
            config.get("chat.coax_max_rungs_per_day", DEFAULT_MAX_RUNGS_PER_DAY)
        )
        self.busy_minutes = max(
            1.0, _to_float(config.get("chat.busy_minutes", 120), 120.0)
        )
        self.llm_retry_minutes = max(
            0.5, _to_float(config.get("chat.coax_rung_llm_retry_minutes", 5), 5.0)
        )

    # ------------------------------------------------------------- stop flag
    def _flag_key(self, session_id: str) -> str:
        return f"chat_stop_flag:{session_id}"

    def stop_flag(self, session_id: str, *, now: float) -> str | None:
        """Active level for this session, or None. ``busy`` expires on its own."""
        raw = self.db.get_meta(self._flag_key(session_id))
        if not raw:
            return None
        try:
            flag = json.loads(raw)
        except (TypeError, ValueError):
            return None
        level = str(flag.get("level") or "")
        if level not in STOP_LEVELS:
            return None
        until = flag.get("until")
        if level == "busy":
            try:
                if float(until or 0) <= float(now):
                    return None  # expired; the stale row hurts nobody
            except (TypeError, ValueError):
                return None
        return level

    def set_stop_flag(self, session_id: str, level: str, *, now: float) -> None:
        if level not in STOP_LEVELS:
            return
        until: float | None
        if level == "busy":
            until = float(now) + self.busy_minutes * 60.0
        else:
            # chat_stop: until the other side opens their mouth again;
            # topic_change is not a flag at all -- it mutes the loop (below).
            until = None
        if level == "topic_change":
            self._mute_latest_open_loop(session_id, now=now)
            return
        self.db.set_meta(
            self._flag_key(session_id),
            json.dumps({"level": level, "until": until}, ensure_ascii=False),
        )

    def clear_stop_flag(self, session_id: str) -> None:
        """The other side spoke again: every level lifts (a fresh stop may
        immediately re-set it in the same inbound turn)."""
        self.db.set_meta(self._flag_key(session_id), "")

    # ------------------------------------------------- daily counters / mute
    def _day(self, now: float) -> str:
        return human_date(float(now), self.tz_name)

    def _rungs_key(self, session_id: str, day: str) -> str:
        return f"coax_rungs:{session_id}:{day}"

    def rungs_today(self, session_id: str, *, now: float) -> int:
        raw = self.db.get_meta(self._rungs_key(session_id, self._day(now)))
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    def count_rung(self, session_id: str, *, now: float) -> None:
        key = self._rungs_key(session_id, self._day(now))
        self.db.set_meta(key, str(self.rungs_today(session_id, now=now) + 1))

    def _muted_key(self, session_id: str, day: str) -> str:
        return f"coax_muted:{session_id}:{day}"

    def is_loop_muted(self, session_id: str, loop_id: str | None, *, now: float) -> bool:
        if not loop_id:
            return False
        raw = self.db.get_meta(self._muted_key(session_id, self._day(now))) or ""
        return str(loop_id) in [part for part in raw.split(",") if part]

    def mute_loop(self, session_id: str, loop_id: str | None, *, now: float) -> None:
        if not loop_id:
            return
        key = self._muted_key(session_id, self._day(now))
        raw = self.db.get_meta(key) or ""
        parts = [part for part in raw.split(",") if part]
        if str(loop_id) not in parts:
            parts.append(str(loop_id))
        self.db.set_meta(key, ",".join(parts))

    # ------------------------------------------------------ open-loop signal
    def open_loop_for(self, session_id: str, *, now: float) -> dict[str, Any] | None:
        """The newest open loop of this session, within ``loop_max_age_days``."""
        row = self.db.query_one(
            "SELECT * FROM open_loops WHERE session_id=? AND status='open' "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        if row is None:
            return None
        max_age_days = _to_float(self.config.get("chat.coax_loop_max_age_days", 7), 7.0)
        try:
            created_at = float(row["created_at"])
        except (TypeError, ValueError):
            return None
        if float(now) - created_at > max_age_days * 86400.0:
            return None
        return {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)

    def _mute_latest_open_loop(self, session_id: str, *, now: float) -> None:
        loop = self.open_loop_for(session_id, now=now)
        if loop is not None:
            self.mute_loop(session_id, str(loop.get("id")), now=now)

    # -------------------------------------------------------------- decision
    def rung_count(self, desire: float | None) -> int:
        d = 0.0 if desire is None else float(desire)
        if d >= self.desire_high:
            rungs = 3
        elif d >= self.desire_low:
            rungs = 2
        else:
            rungs = 1
        return min(rungs, len(self.intervals), MAX_LADDER_RUNGS)

    def should_arm(
        self,
        session_id: str,
        *,
        now: float,
        desire: float | None,
        thread_open: bool,
    ) -> ContinueDecision:
        """Decision 1-4 of the boss-approved flow, at her outbound turn end."""
        if not self.enabled:
            return ContinueDecision("none", reason="disabled")
        flag = self.stop_flag(session_id, now=now)
        if flag:
            return ContinueDecision("none", reason=f"stop_flag:{flag}")
        loop = self.open_loop_for(session_id, now=now)
        if loop is not None and self.is_loop_muted(session_id, str(loop.get("id")), now=now):
            loop = None
        has_thread = loop is not None or bool(thread_open)
        wants_more = desire is not None and desire >= self.desire_low
        if not (has_thread or wants_more):
            return ContinueDecision("none", reason="no_thread_no_desire")
        if self.max_rungs_per_day > 0 and (
            self.rungs_today(session_id, now=now) >= self.max_rungs_per_day
        ):
            return ContinueDecision("none", reason="daily_cap")
        rungs = self.rung_count(desire)
        return ContinueDecision(
            "arm",
            rungs=rungs,
            first_delay_seconds=self.intervals[0] * 60.0,
            reason="open_loop" if loop is not None else "desire",
            loop_id=str(loop.get("id")) if loop is not None else None,
        )

    def next_rung_delay(self, rung: int) -> float | None:
        """Delay for rung ``rung + 1`` after rung ``rung`` burned (1-based)."""
        if rung < 1 or rung >= len(self.intervals) or rung >= MAX_LADDER_RUNGS:
            return None
        return self.intervals[rung] * 60.0
