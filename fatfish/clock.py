"""Time parsing and timezone helpers.

All persisted timestamps are Unix seconds. Display uses the configured timezone.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REL = re.compile(r"^\+(?P<num>\d+)\s*(?P<unit>s|m|h|d|w)$", re.I)
_REL_COMPOSITE = re.compile(r"^\+(?P<body>(?:\d+\s*[smhdw]\s*)+)$", re.I)
_CLOCK = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")


def get_timezone(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Windows without tzdata: fall back to UTC+8 for Asia/Shanghai.
        if name in {"Asia/Shanghai", "Asia/Chongqing", "PRC"}:
            return timezone(timedelta(hours=8))
        return timezone.utc


def now_ts() -> float:
    return time.time()


def local_now(tz_name: str) -> datetime:
    return datetime.now(get_timezone(tz_name))


def to_local(ts: float, tz_name: str) -> datetime:
    return datetime.fromtimestamp(float(ts), get_timezone(tz_name))


def from_local(dt: datetime, tz_name: str) -> float:
    tz = get_timezone(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).timestamp()


def parse_when(value: str, tz_name: str, *, now: float | None = None) -> float:
    """Parse ISO date/datetime, +2h/+30m, or HH:MM.

    Naive values are interpreted in the configured timezone.
    """
    raw = value.strip()
    now = time.time() if now is None else now
    if not raw:
        raise ValueError("empty time")

    rel = _REL.match(raw)
    if rel:
        num = int(rel.group("num"))
        unit = rel.group("unit").lower()
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return now + num * seconds

    composite = _REL_COMPOSITE.match(raw)
    if composite:
        seconds = 0
        for num_text, unit in re.findall(r"(\d+)\s*([smhdw])", composite.group("body"), re.I):
            seconds += int(num_text) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit.lower()]
        return now + seconds

    clock = _CLOCK.match(raw)
    if clock:
        tz = get_timezone(tz_name)
        current = datetime.fromtimestamp(now, tz)
        candidate = current.replace(
            hour=int(clock.group("h")), minute=int(clock.group("m")), second=0, microsecond=0
        )
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported time format: {value!r}") from exc

    if dt.tzinfo is None:
        tz = get_timezone(tz_name)
        if isinstance(dt, datetime) and dt.time() == dtime(0, 0) and len(raw) <= 10:
            # Date-only -> local midnight.
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.replace(tzinfo=tz)
    return dt.timestamp()


def day_bounds(ts: float, tz_name: str) -> tuple[float, float]:
    tz = get_timezone(tz_name)
    local = datetime.fromtimestamp(ts, tz)
    start = datetime.combine(local.date(), dtime.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def in_quiet_hours(ts: float, start: str, end: str, tz_name: str) -> bool:
    tz = get_timezone(tz_name)
    local = datetime.fromtimestamp(ts, tz)
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    minute = local.hour * 60 + local.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= minute < end_min
    return minute >= start_min or minute < end_min


def next_quiet_end(ts: float, end: str, tz_name: str) -> float:
    tz = get_timezone(tz_name)
    local = datetime.fromtimestamp(ts, tz)
    eh, em = _parse_hhmm(end)
    candidate = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM value: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM value: {value!r}")
    return hour, minute


def human_time(ts: float, tz_name: str) -> str:
    return to_local(ts, tz_name).strftime("%Y-%m-%d %H:%M")


def human_date(ts: float, tz_name: str) -> str:
    return to_local(ts, tz_name).strftime("%Y-%m-%d")


def day_phase(ts: float, tz_name: str) -> str:
    hour = to_local(ts, tz_name).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"
