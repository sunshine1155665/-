"""Deterministic time-window parsing for recall routing.

This is step 1 of the recall route in ``docs/adr/0003-memory-hierarchy.md``
§4.5.3: turn the time words of a natural-language (Chinese) query into a
concrete ``[start, end]`` Unix-seconds window, so the store can hard-filter
rows in SQL *before* the vector search.  A 24h pre-filter was measured ~148x
faster than the unfiltered search, and the hard filter also removes
"semantically similar but wrong time" recalls.

Contract
--------
``parse_time_window(query, tz_name, *, now=None)`` returns ``(start, end)``.
Either side may be ``None`` meaning unbounded; ``(None, None)`` means "no time
expression found" and the caller must skip the time filter entirely.  ``end``
is *inclusive*: calendar windows end at "next period start minus 1.0 second",
so recall code filters with ``created_at <= end``.

Only ONE expression wins per query.  Rules are tried in a fixed
specificity-first order (explicit date > single day > months-ago ranges >
week > month > year > fuzzy recency) and the first rule that matches anywhere
in the query decides the window, regardless of word position.

Known limitations (accepted trade-offs; revisit once real queries exist):

- Non-event questions like 「今年多大了」 also hit the 「今年」 window and would
  over-narrow recall — a known trade-off, kept because cheap hard filtering
  matters more than the occasional wrong window.
- 「2026年前」 parses as the whole of 2026 (bare-year rule), not "before 2026".
- 「半年前」 is not recognised (only 「半个月前」); weekday combos (「这周三」)
  and future expressions (「明天」「下周」「明年」) are out of scope.
- Leading-前 ranges like 「前N个月」 are not recognised; 「前三个月」 falls
  through to the bare-month rule and reads as March.
- 「N年前」 only accepts N <= 10, as specified in the ADR; larger N is treated
  as no match.
- Fractional counts from combos like 「两三」(= 2.5) are linearly interpolated
  between the two adjacent calendar-anchored month/year shifts.
"""

from __future__ import annotations

import calendar
import re
import time
from collections.abc import Callable
from datetime import date, datetime, time as dtime, timedelta

from fatfish.clock import day_bounds, from_local, to_local

__all__ = ["parse_time_window"]

# ---------------------------------------------------------------------------
# Chinese / Arabic numeral parsing (kept small and unit-tested on its own).
# ---------------------------------------------------------------------------

_CN_DIGITS: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _parse_cn_number(text: str) -> float | None:
    """Parse a short Arabic or Chinese numeral; ``None`` when unsupported.

    「两三」-style two-single-digit combos average to the midpoint (2.5).
    Chinese forms support 十 / 十X / X十 / X十X up to 99.
    """
    if text.isdigit():
        return float(int(text))
    if "十" in text:
        head, tail = text.split("十", 1)
        if head and head not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[head] if head else 1
        if not tail:
            return float(tens * 10)
        if tail not in _CN_DIGITS:
            return None
        return float(tens * 10 + _CN_DIGITS[tail])
    if len(text) == 1 and text in _CN_DIGITS:
        return float(_CN_DIGITS[text])
    if len(text) == 2 and all(ch in _CN_DIGITS for ch in text):
        return (_CN_DIGITS[text[0]] + _CN_DIGITS[text[1]]) / 2.0
    return None


# ---------------------------------------------------------------------------
# Calendar arithmetic (datetime based, never 86400-approximated).
# ---------------------------------------------------------------------------


def _shift_months(dt: datetime, months: int) -> datetime:
    """Calendar month shift anchored at ``dt``'s day, clamped to month end."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _shift_years(dt: datetime, years: int) -> datetime:
    year = dt.year + years
    day = min(dt.day, calendar.monthrange(year, dt.month)[1])
    return dt.replace(year=year, day=day)


def _months_ago(local: datetime, tz_name: str, months: float) -> float:
    """Timestamp of ``months`` months before ``local`` (may be fractional).

    Integer counts use calendar arithmetic anchored at ``local``'s day of
    month (clamped to month end).  Fractional counts interpolate linearly
    between the two adjacent calendar-anchored shifts.
    """
    whole = int(months)
    frac = months - whole
    near = from_local(_shift_months(local, -whole), tz_name)
    if frac <= 0:
        return near
    far = from_local(_shift_months(local, -(whole + 1)), tz_name)
    return near + (far - near) * frac


def _years_ago(local: datetime, tz_name: str, years: float) -> float:
    whole = int(years)
    frac = years - whole
    near = from_local(_shift_years(local, -whole), tz_name)
    if frac <= 0:
        return near
    far = from_local(_shift_years(local, -(whole + 1)), tz_name)
    return near + (far - near) * frac


# ---------------------------------------------------------------------------
# Window constructors.  ``end`` is inclusive: next period start minus 1.0s.
# ---------------------------------------------------------------------------


def _day_window(day: date, tz_name: str) -> tuple[float, float]:
    anchor = from_local(datetime.combine(day, dtime(12, 0)), tz_name)
    start, next_start = day_bounds(anchor, tz_name)
    return start, next_start - 1.0


def _span_window(start: datetime, end: datetime, tz_name: str) -> tuple[float, float]:
    return from_local(start, tz_name), from_local(end, tz_name) - 1.0


def _month_window(year: int, month: int, tz_name: str) -> tuple[float, float]:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return _span_window(datetime(year, month, 1), nxt, tz_name)


def _year_window(year: int, tz_name: str) -> tuple[float, float]:
    return _span_window(datetime(year, 1, 1), datetime(year + 1, 1, 1), tz_name)


def _week_window(monday: date, tz_name: str) -> tuple[float, float]:
    return _span_window(
        datetime.combine(monday, dtime.min),
        datetime.combine(monday + timedelta(days=7), dtime.min),
        tz_name,
    )


# ---------------------------------------------------------------------------
# Rule patterns, checked strictly in the order of _RULES below.
# ---------------------------------------------------------------------------

_NUM_ALT = r"[0-9]+|[一二两三四五六七八九十]{1,3}"
_NUM_GUARD = r"(?<![0-9.一二两三四五六七八九十])"

_ISO_DATE = re.compile(r"(?<![0-9./])([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})(?![0-9])")
_ISO_MONTH = re.compile(r"(?<![0-9./])([0-9]{4})[-/.]([0-9]{1,2})(?![0-9/-])")

_TODAY = re.compile(r"今天|今日|今早|今晚")
_YESTERDAY = re.compile(r"昨天|昨日|昨晚")
_BEFORE_YESTERDAY = re.compile(r"前天")
_BIG_BEFORE_YESTERDAY = re.compile(r"大前天")
_DAYS_AGO = re.compile(_NUM_GUARD + r"(?P<num>" + _NUM_ALT + r")\s*天\s*之?前")

_HALF_MONTH_AGO = re.compile(r"半个?月之?前")
_MONTHS_AGO = re.compile(
    _NUM_GUARD + r"(?P<num>[0-9]+|[一二两三四五六七八九十]{1,3}|[几数])个\s*月之?前"
)
_QUAL_MONTH = re.compile(
    r"(?:(?P<rel>去年|今年|前年)|(?P<yr>(?<!\d)[0-9]{4})年)?"
    r"(?<![0-9一二两三四五六七八九十])"
    r"(?P<mon>[0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*月"
    r"(?:\s*(?P<day>[0-9]{1,2}|[一二两三四五六七八九十]{1,3})(?:\s*[日号])?)?"
)
_THIS_MONTH = re.compile(r"这个月|本月")
_LAST_MONTH = re.compile(r"上个月|上月")

_THIS_WEEK = re.compile(r"这周|这一周|本周|这个星期|这星期|本星期")
_LAST_WEEK = re.compile(r"上周|上个星期|上星期|上个周")

_YEARS_AGO = re.compile(_NUM_GUARD + r"(?P<num>" + _NUM_ALT + r")\s*年\s*之?前")
_FUZZY_YEARS_AGO = re.compile(r"[几数]年之?前")
_THIS_YEAR = re.compile(r"今年|这一年")
_LAST_YEAR = re.compile(r"去年")
_YEAR_BEFORE_LAST = re.compile(r"前年")
_ABS_YEAR = re.compile(r"(?<!\d)([0-9]{4})年")

_FEW_DAYS = re.compile(r"最近几天|这几天|前几天")
_RECENT = re.compile(r"最近|近期|近来")

_DAY = 86400.0  # only for the coarse "最近"-style ranges, never for calendar math


class _Ctx:
    __slots__ = ("now", "local", "tz_name")

    def __init__(self, now: float, tz_name: str) -> None:
        self.now = now
        self.tz_name = tz_name
        self.local = to_local(now, tz_name)


Handler = Callable[[re.Match[str], _Ctx], "tuple[float, float] | None"]


def _h_rel_day(offset: int) -> Handler:
    def handler(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
        return _day_window(ctx.local.date() + timedelta(days=offset), ctx.tz_name)

    return handler


def _h_iso_date(match: re.Match[str], ctx: _Ctx) -> tuple[float, float] | None:
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return _day_window(day, ctx.tz_name)


def _h_iso_month(match: re.Match[str], ctx: _Ctx) -> tuple[float, float] | None:
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return _month_window(int(match.group(1)), month, ctx.tz_name)


def _h_days_ago(match: re.Match[str], ctx: _Ctx) -> tuple[float, float] | None:
    value = _parse_cn_number(match.group("num"))
    if value is None or value < 1:
        return None
    # "两三天前" style combos average to 2.5; a day offset must be a whole
    # day, so truncate toward zero.
    return _day_window(ctx.local.date() - timedelta(days=int(value)), ctx.tz_name)


def _h_half_month_ago(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return ctx.now - 30 * _DAY, ctx.now - 7 * _DAY


def _h_months_ago(match: re.Match[str], ctx: _Ctx) -> tuple[float, float] | None:
    num = match.group("num")
    if num in ("几", "数"):
        return (
            _months_ago(ctx.local, ctx.tz_name, 6.0),
            _months_ago(ctx.local, ctx.tz_name, 1.0),
        )
    value = _parse_cn_number(num)
    if value is None or value < 1:
        return None
    start = _months_ago(ctx.local, ctx.tz_name, value + 1.0)
    end = _months_ago(ctx.local, ctx.tz_name, max(value - 1.0, 0.0))
    return start, end


def _h_qualified_month(match: re.Match[str], ctx: _Ctx) -> tuple[float, float] | None:
    value = _parse_cn_number(match.group("mon"))
    if value is None or not 1 <= value <= 12:
        return None
    month = int(value)
    rel, yr = match.group("rel"), match.group("yr")
    if rel == "去年":
        year = ctx.local.year - 1
    elif rel == "前年":
        year = ctx.local.year - 2
    elif yr:
        year = int(yr)
    else:
        year = ctx.local.year  # 「今年7月」 and bare 「7月」 default to the current year
    day_text = match.group("day")
    if day_text:
        day_value = _parse_cn_number(day_text)
        if day_value is None:
            return None
        try:
            return _day_window(date(year, month, int(day_value)), ctx.tz_name)
        except ValueError:
            return None
    return _month_window(year, month, ctx.tz_name)


def _h_this_week(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    monday = ctx.local.date() - timedelta(days=ctx.local.weekday())
    return _week_window(monday, ctx.tz_name)


def _h_last_week(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    monday = ctx.local.date() - timedelta(days=ctx.local.weekday()) - timedelta(days=7)
    return _week_window(monday, ctx.tz_name)


def _h_this_month(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return _month_window(ctx.local.year, ctx.local.month, ctx.tz_name)


def _h_last_month(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    prev = _shift_months(ctx.local, -1)
    return _month_window(prev.year, prev.month, ctx.tz_name)


def _h_years_ago(match: re.Match[str], ctx: _Ctx) -> tuple[float, float] | None:
    value = _parse_cn_number(match.group("num"))
    if value is None or not 1 <= value <= 10:  # ADR caps N at 10
        return None
    start = _years_ago(ctx.local, ctx.tz_name, value + 1.0)
    end = _years_ago(ctx.local, ctx.tz_name, max(value - 1.0, 0.0))
    return start, end


def _h_fuzzy_years(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return _years_ago(ctx.local, ctx.tz_name, 10.0), _years_ago(ctx.local, ctx.tz_name, 2.0)


def _h_this_year(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return _year_window(ctx.local.year, ctx.tz_name)


def _h_last_year(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return _year_window(ctx.local.year - 1, ctx.tz_name)


def _h_year_before_last(_match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return _year_window(ctx.local.year - 2, ctx.tz_name)


def _h_abs_year(match: re.Match[str], ctx: _Ctx) -> tuple[float, float]:
    return _year_window(int(match.group(1)), ctx.tz_name)


def _h_recent_days(_match: re.Match[str], ctx: _Ctx) -> tuple[float, None]:
    return ctx.now - 7 * _DAY, None


def _h_recent(_match: re.Match[str], ctx: _Ctx) -> tuple[float, None]:
    return ctx.now - 30 * _DAY, None


# Fixed priority: more specific (narrower, more explicit) expressions first.
# The first rule matching ANYWHERE in the query wins; word position inside the
# query is not considered.
_RULES: tuple[tuple[re.Pattern[str], Handler], ...] = (
    (_ISO_DATE, _h_iso_date),                # 2026-07-14
    (_ISO_MONTH, _h_iso_month),              # 2026-07
    (_BIG_BEFORE_YESTERDAY, _h_rel_day(-3)),  # 大前天
    (_BEFORE_YESTERDAY, _h_rel_day(-2)),      # 前天
    (_YESTERDAY, _h_rel_day(-1)),             # 昨天 / 昨日 / 昨晚
    (_TODAY, _h_rel_day(0)),                  # 今天 / 今日 / 今早 / 今晚
    (_DAYS_AGO, _h_days_ago),                 # 3天前 / 三天前 / 十天前
    (_HALF_MONTH_AGO, _h_half_month_ago),     # 半个月前
    (_MONTHS_AGO, _h_months_ago),             # 一个月前 / 三个月前 / 两三个月前 / 几(数)个月前
    (_QUAL_MONTH, _h_qualified_month),        # 去年7月 / 今年7月 / 7月(14日) / 2026年7月
    (_THIS_WEEK, _h_this_week),               # 这周 / 本周 / 这一周 / 这个星期 / 本星期
    (_LAST_WEEK, _h_last_week),               # 上周 / 上个星期 / 上星期 / 上个周
    (_THIS_MONTH, _h_this_month),             # 这个月 / 本月
    (_LAST_MONTH, _h_last_month),             # 上个月 / 上月
    (_YEARS_AGO, _h_years_ago),               # 三年前 / 3年前 (1 <= N <= 10)
    (_FUZZY_YEARS_AGO, _h_fuzzy_years),       # 几年前 / 数年前
    (_THIS_YEAR, _h_this_year),               # 今年 / 这一年
    (_LAST_YEAR, _h_last_year),               # 去年
    (_YEAR_BEFORE_LAST, _h_year_before_last),  # 前年
    (_ABS_YEAR, _h_abs_year),                 # 2026年
    (_FEW_DAYS, _h_recent_days),              # 最近几天 / 这几天 / 前几天
    (_RECENT, _h_recent),                     # 最近 / 近期 / 近来
)


def parse_time_window(
    query: str, tz_name: str, *, now: float | None = None
) -> tuple[float | None, float | None]:
    """Parse the first time expression of ``query`` into a Unix-seconds window.

    Returns ``(start, end)`` with inclusive ``end`` (recall filters with
    ``<=``); either side may be ``None`` for an unbounded side, and
    ``(None, None)`` when no supported time expression is present.  See the
    module docstring for the rule order and known limitations.
    """
    if not query or not query.strip():
        return (None, None)
    ctx = _Ctx(time.time() if now is None else float(now), tz_name)
    for pattern, handler in _RULES:
        match = pattern.search(query)
        if match:
            window = handler(match, ctx)
            if window is not None:
                return window
    return (None, None)
