"""Past timeline: the days the transcript window no longer carries.

Context used to stop at the window: 20-39 turns of raw lines plus whatever
happened earlier *today*.  Anything older vanished, even though ``nightly``
had already archived every day into ``episodes`` (factual timeline + her own
one-line summary).  These tests pin the composition, the ordering that keeps
the prompt cache intact, and the two ways the block must stay quiet:
days still visible as raw lines, and sessions with no archive at all.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fatfish.context import (
    PAST_TIMELINE_MAX_CHARS,
    ContextAssembler,
    _episode_day_label,
    _episode_time_points,
    _episode_voice_gist,
)
from fatfish.memory import MemoryService
from tests.test_owner import OWNER_SESSION, make_env

TZ = timezone(timedelta(hours=8))


def _at(month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp()


NOW = _at(10, 26, 20, 0)

FACTUAL = """2026-10-22 事实时间线
[13:57-14:03] 现在你在干什么呀
  主人: 现在你在干什么呀
  你: 就在这儿待着呀
[19:06] 👌🏻
  主人: 👌🏻
  你: 乖～
[19:53] 零散片段
  主人: 今天有点累了
  你: 嗯，我在的。
[21:14-21:35] 跟我说声晚安，可以吗？
  主人: 跟我说声晚安，可以吗？
  你: 晚安，主人哥哥。"""

VOICE = "今天和主人哥哥聊了 8 件事：现在你在干什么呀；晚上好；晚安。我都记着呢。"


class PastTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------- fixtures
    def seed_turn(self, at: float, text: str = "在吗", role: str = "user") -> None:
        self.db.execute(
            "INSERT INTO conversation_turns"
            "(session_id, role, content, created_at, token_count, used_refs_json, ephemeral) "
            "VALUES (?,?,?,?,?,?,0)",
            (OWNER_SESSION, role, text, at, 3, "[]"),
        )

    def seed_episode(
        self,
        day: str,
        *,
        end_at: float,
        summary: str = FACTUAL,
        voice: str = VOICE,
        session: str = OWNER_SESSION,
    ) -> None:
        self.db.execute(
            "INSERT INTO episodes"
            "(id, title, start_at, end_at, summary, memory_ids_json, created_at, "
            " session_id, event_ids_json, summary_voice, turn_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"ep:{day}:{session}",
                day,
                end_at - 3600,
                end_at,
                summary,
                "[]",
                end_at,
                session,
                "[]",
                voice,
                12,
            ),
        )

    def block(self) -> str:
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner", now=NOW)
        for section in bundle.sections:
            if section.name == "past_timeline":
                return section.text
        return ""

    # ---------------------------------------------------------- composition
    def test_nothing_old_enough_means_no_section_at_all(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        self.assertEqual(self.block(), "")
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner", now=NOW)
        self.assertNotIn("past_timeline", [s.name for s in bundle.sections if s.text])

    def test_archived_day_is_rendered_with_heading_and_points(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        text = self.block()

        self.assertIn("[过段时间线]", text)
        self.assertIn("10-22", text)
        self.assertIn("13:57 现在你在干什么呀", text)
        self.assertIn("21:14 跟我说声晚安，可以吗？", text)
        # The day in her own words, without the itemised tail the points repeat.
        self.assertIn("那天和主人哥哥聊了 8 件事", text)
        self.assertNotIn("我都记着呢", text)

    def test_relative_days_never_leak_into_the_block(self) -> None:
        # "今天" is what she wrote on the day; read three days later it points
        # at the wrong day, and the block is cached across turns.
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        text = self.block()
        self.assertNotIn("今天", text)
        self.assertNotIn("昨天", text)
        self.assertNotIn("前天", text)

    def test_emoji_only_and_leftover_headers_are_dropped(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        points = _episode_time_points(FACTUAL)
        joined = "\n".join(points)
        self.assertNotIn("👌🏻", joined)
        self.assertNotIn("零散片段", joined)
        self.assertEqual(len(points), 2)

    def test_block_ends_with_a_do_not_quote_me_note(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        text = self.block()
        self.assertIn("不是原话", text)
        self.assertIn("search_memory", text)

    # ---------------------------------------------------------- complementarity
    def test_day_still_visible_in_the_window_is_not_repeated(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        # Ends *after* the oldest turn in the window: those hours are still
        # in ``[最近对话]`` as raw lines.
        self.seed_episode("2026-10-26", end_at=_at(10, 26, 11))
        self.assertEqual(self.block(), "")

    def test_only_the_newest_days_are_kept(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        for day in ("20", "21", "22", "23", "24"):
            self.seed_episode(f"2026-10-{day}", end_at=_at(10, int(day), 22))
        text = self.block()
        self.assertIn("10-24", text)
        self.assertIn("10-22", text)
        self.assertNotIn("10-20", text)
        self.assertNotIn("10-21", text)

    def test_other_sessions_are_never_mixed_in(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode(
            "2026-10-22", end_at=_at(10, 22, 22), session="qq:private:20002"
        )
        self.assertEqual(self.block(), "")

    def test_char_cap_keeps_the_newest_day(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        long_summary = "2026-10-22 事实时间线\n" + "\n".join(
            f"[{hour:02d}:00] 一件很长很长而且说了很多遍的事情标题{i}"
            for i, hour in enumerate(range(10, 20))
        )
        for day in ("23", "24", "25"):
            self.seed_episode(
                f"2026-10-{day}", end_at=_at(10, int(day), 22), summary=long_summary
            )
        text = self.block()
        self.assertLessEqual(len(text), PAST_TIMELINE_MAX_CHARS)
        self.assertIn("10-25", text)

    def test_empty_window_falls_back_to_local_midnight(self) -> None:
        # No turns at all: today is owned by ``[今日时间线]``, so the block
        # must still start from yesterday and not re-tell today.
        self.seed_episode("2026-10-25", end_at=_at(10, 25, 22))
        self.seed_episode("2026-10-26", end_at=_at(10, 26, 1))
        text = self.block()
        self.assertIn("10-25", text)
        self.assertNotIn("10-26", text)

    # ------------------------------------------------------------- cache safety
    def test_stable_prefix_is_byte_identical_with_and_without_archive(self) -> None:
        before = self.assembler.stable_prefix()
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        self.assertEqual(self.assembler.stable_prefix(), before)

    def test_block_is_byte_identical_across_rebuilds(self) -> None:
        # Anything that ticks between two turns (a clock, a counter) would void
        # the cache prefix every round for nothing.
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        first = self.block()
        time.sleep(0.01)
        self.assertEqual(self.block(), first)

    def test_section_sits_between_prefix_and_history(self) -> None:
        self.seed_turn(_at(10, 26, 10))
        self.seed_episode("2026-10-22", end_at=_at(10, 22, 22))
        messages = self.assembler.build(
            OWNER_SESSION, "在吗", role="owner", now=NOW
        ).model_messages()

        self.assertEqual(messages[0]["content"], self.assembler.stable_prefix())
        self.assertEqual(messages[1]["role"], "system")
        self.assertTrue(messages[1]["content"].startswith("[过段时间线]"))
        self.assertEqual(messages[2]["role"], "user")
        self.assertIn("[10-26 10:00]", messages[2]["content"])


class HelperTests(unittest.TestCase):
    def test_day_label_uses_month_day(self) -> None:
        self.assertEqual(_episode_day_label({"title": "2026-10-22"}), "10-22")

    def test_day_label_tolerates_odd_titles(self) -> None:
        self.assertEqual(_episode_day_label({"title": ""}), "")
        self.assertEqual(_episode_day_label({}), "")
        self.assertEqual(_episode_day_label({"title": "某天"}), "某天")

    def test_voice_gist_cuts_at_the_colon(self) -> None:
        self.assertEqual(
            _episode_voice_gist("今天和主人哥哥聊了 8 件事：A；B；C。我都记着呢。"),
            "那天和主人哥哥聊了 8 件事",
        )

    def test_voice_gist_handles_prose_diaries(self) -> None:
        self.assertEqual(
            _episode_voice_gist("主人哥哥今天很累。我没多说话。"),
            "主人哥哥那天很累",
        )

    def test_voice_gist_empty_inputs(self) -> None:
        for value in (None, "", "   ", 0):
            self.assertEqual(_episode_voice_gist(value), "")

    def test_time_points_collapse_a_range_to_its_start(self) -> None:
        self.assertEqual(
            _episode_time_points("[13:57-14:03] 现在你在干什么呀"),
            ["  13:57 现在你在干什么呀"],
        )

    def test_time_points_survive_garbage(self) -> None:
        for value in (None, "", "没有时间戳的一行", 0, []):
            self.assertEqual(_episode_time_points(value), [])


if __name__ == "__main__":
    unittest.main()
