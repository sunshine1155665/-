from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.clock import parse_when
from fatfish.config import load_config
from fatfish.context import ContextAssembler
from fatfish.db import Database
from fatfish.memory import MemoryService

_HEADER_RE = re.compile(r"^\[今日时间线 (\d{4}-\d{2}-\d{2})\]$")
_LINE_RE = re.compile(r"^(\d{2}:\d{2}) (主人|你): .+")


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'timeline.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128
event_gap_minutes = 45
event_min_gap_for_topic_minutes = 10
event_topic_shift_threshold = 0.15
today_timeline_chars = 1200
"""
    path = tmp / "timeline.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


class TodayTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.service = MemoryService(self.db, self.config)
        self.tz = self.config.timezone_name
        self.now = parse_when("2026-10-22 18:00", self.tz)
        self.base = parse_when("2026-10-22 09:00", self.tz)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def add_turn(
        self, session_id: str, role: str, content: str, at: float
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO conversation_turns("
            "session_id, role, content, created_at, token_count, used_refs_json, ephemeral"
            ") VALUES (?,?,?,?,0,'[]',0)",
            (session_id, role, content, float(at)),
        )
        turn_id = int(cur.lastrowid)
        self.service.events.observe_turn(
            turn_id, session_id, role, content, float(at)
        )
        return turn_id

    def test_empty_timeline(self) -> None:
        self.assertEqual("", self.service.today_timeline("s", now=self.now))
        self.add_turn("other", "user", "别的会话", self.base)
        self.assertEqual("", self.service.today_timeline("s", now=self.now))

    def test_format_roles_and_chronological_order(self) -> None:
        self.add_turn("s", "user", "早上好呀", parse_when("2026-10-22 09:05", self.tz))
        self.add_turn("s", "assistant", "早呀，主人哥哥", parse_when("2026-10-22 08:50", self.tz))
        self.add_turn("s", "user", "今天要做什么", parse_when("2026-10-22 09:06", self.tz))

        text = self.service.today_timeline("s", now=self.now)
        lines = text.splitlines()
        self.assertEqual("[今日时间线 2026-10-22]", lines[0])
        self.assertEqual("08:50 你: 早呀，主人哥哥", lines[1])
        self.assertEqual("09:05 主人: 早上好呀", lines[2])
        self.assertEqual("09:06 主人: 今天要做什么", lines[3])
        for line in lines[1:]:
            self.assertRegex(line, _LINE_RE)

    def test_session_isolation(self) -> None:
        self.add_turn("s1", "user", "只属于会话一", self.base)
        self.add_turn("s2", "user", "只属于会话二", self.base + 30)
        text = self.service.today_timeline("s1", now=self.now)
        self.assertIn("只属于会话一", text)
        self.assertNotIn("只属于会话二", text)
        other = self.service.today_timeline("s2", now=self.now)
        self.assertIn("只属于会话二", other)
        self.assertNotIn("只属于会话一", other)

    def test_only_local_today_up_to_now_is_included(self) -> None:
        yesterday = parse_when("2026-10-21 23:59", self.tz)
        tomorrow = parse_when("2026-10-23 00:01", self.tz)
        later_today = parse_when("2026-10-22 20:00", self.tz)
        self.add_turn("s", "user", "昨天的话", yesterday)
        self.add_turn("s", "user", "今天中午的话", parse_when("2026-10-22 12:00", self.tz))
        self.add_turn("s", "user", "今晚八点的话", later_today)
        self.add_turn("s", "user", "明天的话", tomorrow)

        text = self.service.today_timeline("s", now=self.now)
        self.assertIn("今天中午的话", text)
        self.assertNotIn("昨天的话", text)
        self.assertNotIn("今晚八点的话", text)
        self.assertNotIn("明天的话", text)

    def test_each_line_is_truncated(self) -> None:
        long_text = "很长的一句话" * 100
        self.add_turn("s", "user", long_text, self.base)
        text = self.service.today_timeline("s", now=self.now)
        self.assertNotIn(long_text, text)
        self.assertIn("…", text)
        line = text.splitlines()[1]
        self.assertLessEqual(len(line), 120)

    def test_more_than_twenty_turns_keeps_recent_and_early_summary(self) -> None:
        self.add_turn(
            "s", "user", "早上一起喝豆浆然后去公园散步", self.base
        )
        self.add_turn("s", "assistant", "好呀我们出门吧，主人哥哥", self.base + 60)
        # Topic shift after 19 minutes closes the morning event.
        self.add_turn(
            "s",
            "user",
            "帮我写一段Python快速排序的代码",
            self.base + 20 * 60,
        )
        for index in range(30):
            self.add_turn(
                "s",
                "user" if index % 2 == 0 else "assistant",
                f"继续聊第{index:02d}个话题，关于代码、测试、部署、项目进度和明天计划的具体细节内容",
                self.base + (21 + index) * 60,
            )

        text = self.service.today_timeline("s", now=self.now)
        self.assertLessEqual(len(text), 1200)
        lines = text.splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertIn("事件:", text)
        self.assertIn("早上一起喝豆浆", text)
        self.assertIn("继续聊第29个话题", text)

        stamps = [match.group(1) for line in lines[1:] if (match := re.match(r"^(\d{2}:\d{2}) ", line))]
        self.assertEqual(stamps, sorted(stamps))
        self.assertRegex(lines[0], _HEADER_RE)

    def test_max_chars_override_respects_cap(self) -> None:
        self.add_turn("s", "user", "早上一起喝豆浆然后去公园散步", self.base)
        self.add_turn("s", "assistant", "好呀我们出门吧，主人哥哥", self.base + 60)
        self.add_turn(
            "s",
            "user",
            "帮我写一段Python快速排序的代码",
            self.base + 20 * 60,
        )
        for index in range(12):
            self.add_turn(
                "s",
                "user",
                f"继续聊第{index:02d}个话题，关于代码和测试的具体细节内容",
                self.base + (21 + index) * 60,
            )

        text = self.service.today_timeline("s", now=self.now, max_chars=260)
        self.assertLessEqual(len(text), 260)
        self.assertIn("事件:", text)
        self.assertIn("早上一起喝豆浆", text)
        self.assertIn("继续聊第11个话题", text)

    def test_context_adds_timeline_only_to_dynamic_section(self) -> None:
        # [最近对话] carries the newest 20 turns, so the timeline only has
        # something to add once the day is longer than that window.
        for i in range(25):
            self.service.add_turn("ctx", "user", f"第{i}句")
            self.service.add_turn("ctx", "assistant", f"回{i}")
        assembler = ContextAssembler(self.db, self.config, self.service)
        bundle = assembler.build("ctx", "在吗", now=time.time())
        sections = {section.name: section.text for section in bundle.sections}
        self.assertIn("[今日时间线", sections["runtime_context"])
        self.assertNotIn("[今日时间线", sections["stable_prefix"])
        self.assertNotIn("[今日时间线", sections["history"])

    def test_timeline_does_not_repeat_turns_already_in_history(self) -> None:
        # A short day is fully covered by history; replaying it in the timeline
        # would burn tokens twice for zero extra information.
        self.service.add_turn("dup", "user", "今天想你了")
        assembler = ContextAssembler(self.db, self.config, self.service)
        sections = {
            section.name: section.text
            for section in assembler.build("dup", "在吗", now=time.time()).sections
        }
        self.assertNotIn("[今日时间线", sections["runtime_context"])
        self.assertIn("今天想你了", sections["history"])

    def test_empty_timeline_is_omitted_from_dynamic_context(self) -> None:
        assembler = ContextAssembler(self.db, self.config, self.service)
        bundle = assembler.build("empty-session", "在吗", now=self.now)
        sections = {section.name: section.text for section in bundle.sections}
        self.assertNotIn("[今日时间线", sections["runtime_context"])


if __name__ == "__main__":
    unittest.main()
