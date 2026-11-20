"""Tests for fatfish.memory.querytime (ADR-0003 §4.5.3 step 1)."""

from __future__ import annotations

import calendar
import unittest
from datetime import date, datetime, time as dtime, timedelta

from fatfish.clock import from_local, parse_when, to_local
from fatfish.memory.querytime import _parse_cn_number, parse_time_window

TZ = "Asia/Shanghai"
# Fixed "now": 2026-10-24 15:00 local (a Saturday). All expected windows are
# derived from this via fatfish.clock helpers — no hardcoded epoch magic.
NOW = parse_when("2026-10-24 15:00", TZ)
NOW_LOCAL = to_local(NOW, TZ)


def _local(dt: datetime) -> float:
    return from_local(dt, TZ)


def _day_start(day: date) -> float:
    return _local(datetime.combine(day, dtime.min))


def _day_window(day: date) -> tuple[float, float]:
    # end is inclusive: next day start minus 1.0s.
    return _day_start(day), _day_start(day + timedelta(days=1)) - 1.0


def _month_window(year: int, month: int) -> tuple[float, float]:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return _local(datetime(year, month, 1)), _local(nxt) - 1.0


def _year_window(year: int) -> tuple[float, float]:
    return _local(datetime(year, 1, 1)), _local(datetime(year + 1, 1, 1)) - 1.0


def _shift_month(dt: datetime, months: int) -> datetime:
    """Independent expectation helper: calendar month shift, clamped to month end."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(total, 12)
    day = min(dt.day, calendar.monthrange(year, month0 + 1)[1])
    return dt.replace(year=year, month=month0 + 1, day=day)


def _months_ago(months: int) -> float:
    return _local(_shift_month(NOW_LOCAL, -months))


def _years_ago(years: int) -> float:
    return _local(NOW_LOCAL.replace(year=NOW_LOCAL.year - years))


class QueryTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW

    def _check(self, query: str, expected: tuple[float | None, float | None]) -> None:
        self.assertEqual(parse_time_window(query, TZ, now=self.now), expected)

    def test_fixture_sanity(self) -> None:
        # Pin the fixture: 2026-10-24 15:00 local, a Saturday (weekday 5).
        self.assertEqual(NOW_LOCAL.date(), date(2026, 10, 24))
        self.assertEqual(NOW_LOCAL.hour, 15)
        self.assertEqual(NOW_LOCAL.weekday(), 5)

    def test_day_words(self) -> None:
        today = _day_window(date(2026, 10, 24))
        self._check("今天", today)
        self._check("今日", today)
        self._check("今早", today)
        self._check("今晚", today)
        self._check("今天晚上", today)
        yesterday = _day_window(date(2026, 10, 23))
        self._check("昨天", yesterday)
        self._check("昨日", yesterday)
        self._check("昨晚", yesterday)
        self._check("前天", _day_window(date(2026, 10, 22)))
        self._check("大前天", _day_window(date(2026, 10, 21)))

    def test_days_ago(self) -> None:
        three = _day_window(date(2026, 10, 21))
        self._check("3天前", three)
        self._check("三天前", three)
        self._check("3天之前", three)
        ten = _day_window(date(2026, 10, 14))
        self._check("10天前", ten)
        self._check("十天前", ten)

    def test_no_match(self) -> None:
        self._check("你好呀", (None, None))
        self._check("", (None, None))
        self._check("帮我写个周报", (None, None))

    def test_recent_ranges(self) -> None:
        self._check("最近", (self.now - 30 * 86400.0, None))
        self._check("近期", (self.now - 30 * 86400.0, None))
        self._check("近来", (self.now - 30 * 86400.0, None))
        self._check("最近几天", (self.now - 7 * 86400.0, None))
        self._check("这几天", (self.now - 7 * 86400.0, None))
        self._check("前几天", (self.now - 7 * 86400.0, None))

    def test_weeks_anchor_on_monday(self) -> None:
        monday = NOW_LOCAL.date() - timedelta(days=NOW_LOCAL.date().weekday())
        self.assertEqual(monday, date(2026, 10, 19))  # fixture sanity
        this_week = (
            _local(datetime.combine(monday, dtime.min)),
            _local(datetime.combine(monday + timedelta(days=7), dtime.min)) - 1.0,
        )
        self._check("这周", this_week)
        self._check("本周", this_week)
        self._check("这一周", this_week)
        self._check("这个星期", this_week)
        self._check("本星期", this_week)
        last_monday = monday - timedelta(days=7)
        last_week = (
            _local(datetime.combine(last_monday, dtime.min)),
            _local(datetime.combine(monday, dtime.min)) - 1.0,
        )
        self._check("上周", last_week)
        self._check("上个星期", last_week)
        self._check("上星期", last_week)
        self._check("上个周", last_week)

    def test_natural_months(self) -> None:
        self._check("这个月", _month_window(2026, 10))
        self._check("本月", _month_window(2026, 10))
        self._check("上个月", _month_window(2026, 9))
        self._check("上月", _month_window(2026, 9))

    def test_natural_years(self) -> None:
        self._check("今年", _year_window(2026))
        self._check("这一年", _year_window(2026))
        self._check("去年", _year_window(2025))
        self._check("前年", _year_window(2024))

    def test_three_months_ago_window(self) -> None:
        # Centered 3 months ago +/- 1 month: [2026-06-24 15:00, 2026-08-24 15:00].
        start = _months_ago(4)
        end = _months_ago(2)
        self._check("三个月前", (start, end))
        self._check("3个月前", (start, end))
        inside = _local(datetime(2026, 7, 24, 12, 0))
        self.assertLessEqual(start, inside)
        self.assertLessEqual(inside, end)
        # Mid-June and mid-September must stay outside the window.
        for probe in (datetime(2026, 6, 10, 12, 0), datetime(2026, 9, 10, 12, 0)):
            ts = _local(probe)
            self.assertFalse(start <= ts <= end, f"{probe} should be outside")

    def test_fuzzy_months_ago(self) -> None:
        # 几个月前/数个月前 -> [now-6mo, now-1mo].
        expected = (_months_ago(6), _months_ago(1))
        self._check("几个月前", expected)
        self._check("数个月前", expected)

    def test_combo_months_ago(self) -> None:
        # 两三 -> 2.5: each bound interpolates between the two adjacent
        # integer month shifts. The fixture is whole-second, so exact
        # equality holds.
        start = (_months_ago(3) + _months_ago(4)) / 2
        end = (_months_ago(1) + _months_ago(2)) / 2
        self._check("两三个月前", (start, end))

    def test_half_month_ago(self) -> None:
        self._check("半个月前", (self.now - 30 * 86400.0, self.now - 7 * 86400.0))
        self._check("半个月之前", (self.now - 30 * 86400.0, self.now - 7 * 86400.0))

    def test_one_month_ago_is_not_january(self) -> None:
        # Regression: 「一个月前」 must be the months-ago window, not the
        # bare-month rule reading 「一月」 as January.
        self._check("一个月前", (_months_ago(2), self.now))

    def test_explicit_dates(self) -> None:
        self._check("2026-07-14", _day_window(date(2026, 7, 14)))
        self._check("2026-07-14那天我们聊了什么", _day_window(date(2026, 7, 14)))
        self._check("2026-07", _month_window(2026, 7))
        self._check("7月", _month_window(2026, 7))
        self._check("7月14日", _day_window(date(2026, 7, 14)))
        self._check("去年7月", _month_window(2025, 7))
        self._check("今年7月", _month_window(2026, 7))

    def test_first_match_wins(self) -> None:
        # Rule order is specificity-first: any single-day expression outranks
        # any month expression, regardless of word position in the query.
        # 「大前天」 spans one day; 「上个月」 spans ~30 days, so 「大前天」 wins
        # even though 「上个月」 appears earlier in the string.
        self._check("上个月或者大前天", _day_window(date(2026, 10, 21)))
        # 「去年7月」 must resolve via the qualified-month rule (July 2025),
        # not the bare-year rule (all of 2025), even though 「去年」 also matches.
        self._check("去年7月发生的事", _month_window(2025, 7))

    def test_years_ago(self) -> None:
        self._check("三年前", (_years_ago(4), _years_ago(2)))
        self._check("3年前", (_years_ago(4), _years_ago(2)))
        self._check("十年前", (_years_ago(11), _years_ago(9)))
        self._check("几年前", (_years_ago(10), _years_ago(2)))
        self._check("数年前", (_years_ago(10), _years_ago(2)))
        # ADR caps N at 10; larger counts are treated as no match.
        self._check("二十年前", (None, None))


class CnNumberTests(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(_parse_cn_number("7"), 7.0)
        self.assertEqual(_parse_cn_number("十"), 10.0)
        self.assertEqual(_parse_cn_number("十二"), 12.0)
        self.assertEqual(_parse_cn_number("二十"), 20.0)
        self.assertEqual(_parse_cn_number("二十五"), 25.0)
        self.assertEqual(_parse_cn_number("两三"), 2.5)
        self.assertIsNone(_parse_cn_number("几"))
        self.assertIsNone(_parse_cn_number("百"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
