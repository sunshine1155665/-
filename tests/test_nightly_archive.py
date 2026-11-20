from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fatfish.clock import parse_when
from fatfish.config import load_config
from fatfish.context import ContextAssembler, ensure_default_persona
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.nightly import archive_day, run_nightly
from fatfish.owner import OwnerRegistry
from fatfish.runtime import Runtime

DAY = "2026-10-22"
NEXT_DAY = "2026-10-23"


def make_env(tmp: Path, *, name: str = "nightly.db", extra: str = ""):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / name).as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64
event_gap_minutes = 45
event_min_gap_for_topic_minutes = 10
event_topic_shift_threshold = 0.15
today_timeline_chars = 1200
compact_hour = 0
compact_minute = 30
nightly_voice = true
nightly_voice_max_tokens = 350
archive_backfill_days = 3
event_max_per_day = 20

[voice]
enabled = false

[sticker]
enabled = false

[relationship]
enabled = false

[llm]
enabled = false
{extra}
"""
    path = tmp / "nightly.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def add_turn(
    db: Database,
    memory: MemoryService,
    session_id: str,
    role: str,
    content: str,
    at: float,
) -> int:
    cur = db.execute(
        "INSERT INTO conversation_turns("
        "session_id, role, content, created_at, token_count, used_refs_json, ephemeral"
        ") VALUES (?,?,?,?,0,'[]',0)",
        (session_id, role, content, float(at)),
    )
    turn_id = int(cur.lastrowid)
    memory.events.observe_turn(turn_id, session_id, role, content, float(at))
    return turn_id


class _Usage:
    prompt_tokens = 120
    completion_tokens = 40
    total_tokens = 160


class _Result:
    def __init__(self, text: str):
        self.text = text
        self.usage = _Usage()


class FakeLLM:
    available = True

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def chat(
        self,
        messages,
        *,
        max_tokens=None,
        temperature=None,
        session_id=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "session_id": session_id,
            }
        )
        return _Result(self.text)


class NightlyArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.tz = self.config.timezone_name

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def ts(self, value: str) -> float:
        return parse_when(value, self.tz)

    def episode(self, episode_id: str):
        return self.db.query_one("SELECT * FROM episodes WHERE id=?", (episode_id,))

    def memories(self, source_type: str):
        return self.db.query(
            "SELECT * FROM memory_items WHERE source_type=? ORDER BY event_time_start",
            (source_type,),
        )

    # ------------------------------------------------------------------ core
    def test_0030_archives_previous_day_only(self) -> None:
        add_turn(
            self.db, self.memory, "s1", "user",
            "早上一起喝豆浆然后去公园散步", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "s1", "assistant",
            "好呀主人哥哥，我们出发吧", self.ts("2026-10-22 09:01"),
        )
        add_turn(
            self.db, self.memory, "s1", "user",
            "下午帮我看一段Python代码", self.ts("2026-10-22 10:30"),
        )
        add_turn(
            self.db, self.memory, "s1", "assistant",
            "这段代码逻辑没问题", self.ts("2026-10-22 10:31"),
        )
        add_turn(
            self.db, self.memory, "s1", "user",
            "凌晨刚开始的话不应该进昨天的日记", self.ts("2026-10-23 00:10"),
        )

        now = self.ts("2026-10-23 00:30")
        result = run_nightly(self.db, self.config, self.memory, now=now, write_voice=False)

        self.assertEqual(DAY, result["target_date"])
        self.assertEqual(DAY, result["date"])
        self.assertEqual(DAY, self.db.get_meta("last_nightly_date"))
        self.assertFalse(result["archive"]["skipped"])
        self.assertEqual(1, result["archive"]["sessions"])

        episode = self.episode(f"ep:{DAY}:s1")
        self.assertIsNotNone(episode)
        self.assertEqual(DAY, episode["title"])
        self.assertEqual(4, int(episode["turn_count"]))
        self.assertAlmostEqual(self.ts("2026-10-22 09:00"), episode["start_at"], places=3)
        self.assertAlmostEqual(self.ts("2026-10-22 10:31"), episode["end_at"], places=3)
        self.assertEqual(2, len(json.loads(episode["event_ids_json"])))
        self.assertEqual(3, len(json.loads(episode["memory_ids_json"])))

        # Factual, chronological, and limited to the target day.
        summary = episode["summary"]
        self.assertIn("2026-10-22 事实时间线", summary)
        self.assertIn("早上一起喝豆浆", summary)
        self.assertIn("下午帮我看一段Python代码", summary)
        self.assertLess(summary.index("早上一起喝豆浆"), summary.index("下午帮我看一段Python代码"))
        self.assertNotIn("凌晨刚开始的话", summary)

        # Event-level memories, one per event, ordered by event start.
        events = self.memories("event_archive")
        self.assertEqual(2, len(events))
        self.assertIn("早上一起喝豆浆", events[0]["gist"])
        self.assertIn("下午帮我看一段Python代码", events[1]["gist"])
        self.assertLess(events[0]["event_time_start"], events[1]["event_time_start"])
        self.assertEqual(f"ep:{DAY}:s1", events[0]["episode_id"])
        self.assertIsNotNone(events[0]["event_id"])
        for item in events:
            self.assertEqual("episodic", item["type"])
            self.assertGreaterEqual(float(item["importance"]), 0.5)
            self.assertLessEqual(float(item["importance"]), 0.7)
            self.assertIn("archive", json.loads(item["tags_json"]))
            self.assertIn(DAY, json.loads(item["tags_json"]))

        # One daily memory with the voice narrative; the episode keeps facts.
        daily = self.memories("episode")
        self.assertEqual(1, len(daily))
        self.assertEqual(f"{DAY} 的日常", daily[0]["gist"])
        self.assertEqual(f"ep:{DAY}:s1", daily[0]["source_id"])
        self.assertEqual(episode["summary_voice"], daily[0]["summary"])
        self.assertTrue(daily[0]["summary"].startswith("今天和主人哥哥聊了 2 件事"))
        self.assertNotIn("今天和主人哥哥聊了", episode["summary"])
        self.assertAlmostEqual(
            self.ts("2026-10-22 00:00"), daily[0]["event_time_start"], places=3
        )
        self.assertAlmostEqual(
            self.ts("2026-10-23 00:00"), daily[0]["event_time_end"], places=3
        )

        # Day rows are marked; the 23rd is untouched.
        day_turns = self.db.query(
            "SELECT episode_id, archived_at FROM conversation_turns "
            "WHERE created_at>=? AND created_at<?",
            (self.ts("2026-10-22 00:00"), self.ts("2026-10-23 00:00")),
        )
        self.assertEqual(4, len(day_turns))
        for row in day_turns:
            self.assertEqual(f"ep:{DAY}:s1", row["episode_id"])
            self.assertIsNotNone(row["archived_at"])
        next_turn = self.db.query_one(
            "SELECT episode_id, archived_at FROM conversation_turns WHERE content LIKE '凌晨刚开始%'"
        )
        self.assertIsNone(next_turn["episode_id"])
        self.assertIsNone(next_turn["archived_at"])

        day_events = self.db.query(
            "SELECT status, archived_at FROM conversation_events "
            "WHERE start_at>=? AND start_at<?",
            (self.ts("2026-10-22 00:00"), self.ts("2026-10-23 00:00")),
        )
        self.assertEqual(2, len(day_events))
        for row in day_events:
            self.assertEqual("archived", row["status"])
            self.assertIsNotNone(row["archived_at"])
        next_event = self.db.query_one(
            "SELECT status, archived_at FROM conversation_events WHERE start_at>=?",
            (self.ts("2026-10-23 00:00"),),
        )
        self.assertEqual("open", next_event["status"])
        self.assertIsNone(next_event["archived_at"])

    def test_rerun_is_idempotent(self) -> None:
        add_turn(
            self.db, self.memory, "s1", "user",
            "早上一起喝豆浆", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "s1", "assistant",
            "好呀主人哥哥", self.ts("2026-10-22 09:01"),
        )
        add_turn(
            self.db, self.memory, "s1", "user",
            "下午看看代码", self.ts("2026-10-22 10:30"),
        )

        now = self.ts("2026-10-23 00:30")
        first = archive_day(
            self.db, self.config, self.memory, date(2026, 10, 22),
            now=now, write_voice=False,
        )
        episode_before = self.episode(f"ep:{DAY}:s1")
        ids_before = {
            row["id"] for row in self.db.query("SELECT id FROM memory_items")
        }
        archived_at_before = self.db.query_one(
            "SELECT archived_at FROM conversation_turns ORDER BY id LIMIT 1"
        )["archived_at"]
        self.assertEqual(4, first["memories_created"])

        second = archive_day(
            self.db, self.config, self.memory, DAY,
            now=now + 3600, write_voice=False,
        )

        self.assertEqual(1, self.db.query_one("SELECT COUNT(*) n FROM episodes")["n"])
        self.assertEqual(4, self.db.query_one("SELECT COUNT(*) n FROM memory_items")["n"])
        self.assertEqual(0, second["memories_created"])
        self.assertEqual(4, second["memories_updated"])
        self.assertEqual(ids_before, {row["id"] for row in self.db.query("SELECT id FROM memory_items")})
        duplicates = self.db.query(
            "SELECT source_type, source_id, COUNT(*) AS n FROM memory_items "
            "WHERE source_type IN ('event_archive','episode','day_summary') "
            "GROUP BY source_type, source_id HAVING n > 1"
        )
        self.assertEqual([], duplicates)
        self.assertEqual(episode_before["created_at"], self.episode(f"ep:{DAY}:s1")["created_at"])
        self.assertEqual(archived_at_before, self.db.query_one(
            "SELECT archived_at FROM conversation_turns ORDER BY id LIMIT 1"
        )["archived_at"])

    def test_day_summary_rolls_up_real_sessions_and_skips_tests(self) -> None:
        OwnerRegistry(self.db, self.config).bind("qq:private:1", "主人哥哥")
        OwnerRegistry(self.db, self.config).bind_friend("qq:private:2", "姐姐")
        add_turn(
            self.db, self.memory, "qq:private:1", "user",
            "早上一起喝豆浆", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "qq:private:2", "user",
            "姐姐说她在学吉他", self.ts("2026-10-22 10:00"),
        )
        add_turn(
            self.db, self.memory, "night-20261025-w02", "user",
            "夜测内容不要进日汇总", self.ts("2026-10-22 11:00"),
        )
        add_turn(
            self.db, self.memory, "activity-20261022-0900", "assistant",
            "休息", self.ts("2026-10-22 09:30"),
        )
        add_turn(
            self.db, self.memory, "activity-20261022-1000", "assistant",
            "翻空间看到萌的动态，B站没新东西", self.ts("2026-10-22 10:00"),
        )
        add_turn(
            self.db, self.memory, "qq:group:9", "user",
            "群友小李说明天结婚", self.ts("2026-10-22 11:30"),
        )
        result = archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:30"), write_voice=False,
        )
        rows = self.memories("day_summary")
        self.assertEqual(1, len(rows))
        summary = str(rows[0]["summary"])
        self.assertIn("2026-10-22 日汇总", summary)
        self.assertIn("主人哥哥", summary)
        self.assertIn("姐姐", summary)
        self.assertNotIn("夜测内容", summary)
        self.assertIn("小李", summary)
        self.assertIn("群聊", summary)
        self.assertEqual(1, summary.count("她的活动"))
        self.assertIn("翻空间", summary)
        self.assertLess(summary.index("主人哥哥"), summary.index("她的活动"))
        self.assertEqual(result["day_summary"]["memory_id"], rows[0]["id"])

        second = archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 01:30"), write_voice=False,
        )
        self.assertEqual(1, len(self.memories("day_summary")))
        self.assertEqual(0, second["day_summary"]["created"])

    def test_missing_days_backfill_oldest_to_newest(self) -> None:
        add_turn(
            self.db, self.memory, "s", "user",
            "十月二十一号留下的话", self.ts("2026-10-21 09:00"),
        )
        add_turn(
            self.db, self.memory, "s", "user",
            "十月二十二号留下的话", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "s", "user",
            "二十三号凌晨的话", self.ts("2026-10-23 00:10"),
        )
        runtime = Runtime(self.db, self.config)

        self.assertIsNone(
            runtime.maybe_nightly(now=self.ts("2026-10-23 00:20"))
        )
        result = runtime.maybe_nightly(now=self.ts("2026-10-23 00:30"))
        self.assertIsNotNone(result)
        self.assertEqual(
            ["2026-10-20", "2026-10-21", "2026-10-22"], result["dates"]
        )
        self.assertTrue(result["archives"][0]["skipped"])
        self.assertEqual("no_data", result["archives"][0]["reason"])
        self.assertFalse(result["archives"][1]["skipped"])
        self.assertFalse(result["archives"][2]["skipped"])
        self.assertEqual(
            {"ep:2026-10-21:s", "ep:2026-10-22:s"},
            {row["id"] for row in self.db.query("SELECT id FROM episodes")},
        )
        self.assertEqual("2026-10-22", self.db.get_meta("last_nightly_date"))
        self.assertIsNone(runtime.maybe_nightly(now=self.ts("2026-10-23 01:00")))

        next_turn = self.db.query_one(
            "SELECT archived_at FROM conversation_turns WHERE content LIKE '二十三号%'"
        )
        self.assertIsNone(next_turn["archived_at"])

    # ----------------------------------------------------------------- voice
    def test_voice_fallback_without_llm_preserves_factual_summary(self) -> None:
        add_turn(
            self.db, self.memory, "s", "user",
            "早上一起喝豆浆", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "s", "user",
            "下午看看代码", self.ts("2026-10-22 10:30"),
        )
        report = archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:30"), write_voice=True,
        )
        self.assertEqual(0, report["llm_calls"])
        self.assertEqual(0, report["voice"]["llm_calls"])
        self.assertIn(report["voice"]["source"], {"template", "template_fallback"})
        self.assertTrue(report["voice"]["selection"].endswith(":unavailable"))

        episode = self.episode(f"ep:{DAY}:s")
        daily = self.memories("episode")[0]
        self.assertTrue(episode["summary_voice"].startswith("今天和主人哥哥聊了 2 件事："))
        self.assertTrue(episode["summary_voice"].endswith("。我都记着呢。"))
        self.assertEqual(episode["summary_voice"], daily["summary"])
        self.assertIn("2026-10-22 事实时间线", episode["summary"])
        self.assertIn("早上一起喝豆浆", episode["summary"])
        self.assertIn("下午看看代码", episode["summary"])
        self.assertNotIn("今天和主人哥哥聊了", episode["summary"])

    def test_owner_session_gets_exactly_one_llm_call_per_day(self) -> None:
        OwnerRegistry(self.db, self.config).bind("qq:owner")
        add_turn(
            self.db, self.memory, "qq:owner", "user",
            "主人今天去公园散步", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "qq:owner", "assistant",
            "好呀主人哥哥，风很舒服", self.ts("2026-10-22 09:01"),
        )
        # The other session has more turns, but the owner must still win.
        for index in range(4):
            add_turn(
                self.db, self.memory, "other", "user" if index % 2 == 0 else "assistant",
                f"别的会话第{index}句", self.ts(f"2026-10-22 11:0{index}"),
            )

        fake = FakeLLM("主人哥哥今天去了公园，鲸鱼尾巴都开心地摇起来了呢。")
        with patch("fatfish.nightly._build_llm_client", return_value=fake):
            first = archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:30"), write_voice=True,
            )
            second = archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:31"), write_voice=True,
            )

        self.assertEqual(1, len(fake.calls))
        call = fake.calls[0]
        self.assertEqual(f"nightly:{DAY}", call["session_id"])
        self.assertEqual(350, call["max_tokens"])
        self.assertEqual(0.5, call["temperature"])
        facts = call["messages"][-1]["content"]
        self.assertIn("主人今天去公园散步", facts)
        self.assertNotIn("别的会话", facts)
        self.assertEqual("qq:owner", first["voice"]["target_session"])
        self.assertEqual("owner", first["voice"]["selection"])
        self.assertEqual(160, first["voice"]["total_tokens"])

        owner_episode = self.episode(f"ep:{DAY}:qq:owner")
        other_episode = self.episode(f"ep:{DAY}:other")
        self.assertEqual(fake.text, owner_episode["summary_voice"])
        self.assertTrue(other_episode["summary_voice"].startswith("今天和主人哥哥聊了"))
        self.assertIn("主人今天去公园散步", owner_episode["summary"])
        self.assertNotIn("鲸鱼尾巴", owner_episode["summary"])
        owner_daily = self.db.query_one(
            "SELECT summary FROM memory_items WHERE source_type='episode' AND source_id=?",
            (f"ep:{DAY}:qq:owner",),
        )
        self.assertEqual(fake.text, owner_daily["summary"])

        # The second run reuses the stored narrative instead of paying again.
        self.assertEqual(1, len(fake.calls))
        self.assertEqual("cached", second["voice"]["source"])
        self.assertEqual(0, second["voice"]["llm_calls"])

    def test_nightly_growth_json_updates_learned_layer_not_core(self) -> None:
        ensure_default_persona(self.db)
        OwnerRegistry(self.db, self.config).bind("qq:owner")
        add_turn(
            self.db, self.memory, "qq:owner", "user",
            "主人今天说有点累，只想安静待一会儿", self.ts("2026-10-22 21:00"),
        )
        core_before = self.db.query_one(
            "SELECT core_json FROM persona WHERE id='default'"
        )["core_json"]
        payload = json.dumps(
            {
                "diary": "今天主人哥哥说有点累，我就安静陪着，没有一直追问。",
                "growth": ["对方说累的时候，先安静陪着，不急着追问", "被夸时先把开心收下。"],
            },
            ensure_ascii=False,
        )
        fake = FakeLLM(payload)
        with patch("fatfish.nightly._build_llm_client", return_value=fake):
            first = archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:30"), write_voice=True,
            )
            second = archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:31"), write_voice=True,
            )

        self.assertEqual(1, len(fake.calls))
        self.assertEqual(2, first["voice"]["growth_added"])
        self.assertEqual(0, first["voice"]["growth_rejected"])
        self.assertEqual(
            "今天主人哥哥说有点累，我就安静陪着，没有一直追问。",
            self.episode(f"ep:{DAY}:qq:owner")["summary_voice"],
        )
        learned = json.loads(
            self.db.query_one("SELECT learned_json FROM persona WHERE id='default'")[
                "learned_json"
            ]
        )
        self.assertEqual(2, len(learned["growth"]))
        self.assertEqual(
            core_before,
            self.db.query_one("SELECT core_json FROM persona WHERE id='default'")["core_json"],
        )
        stable = ContextAssembler(
            self.db, self.config, MemoryService(self.db, self.config)
        ).stable_prefix()
        self.assertIn("【成长层】", stable)
        self.assertIn("先安静陪着", stable)

        self.assertEqual("cached", second["voice"]["source"])
        self.assertEqual(0, second["voice"]["growth_added"])
        self.assertEqual(
            core_before,
            self.db.query_one("SELECT core_json FROM persona WHERE id='default'")["core_json"],
        )

    def test_without_owner_the_busiest_session_is_chosen(self) -> None:
        add_turn(
            self.db, self.memory, "small", "user",
            "小小会话", self.ts("2026-10-22 09:00"),
        )
        for index in range(6):
            add_turn(
                self.db, self.memory, "big", "user" if index % 2 == 0 else "assistant",
                f"忙碌会话第{index}句", self.ts(f"2026-10-22 10:0{index}"),
            )
        fake = FakeLLM("主人哥哥今天说了好多话，我都记在小本本上了。")
        with patch("fatfish.nightly._build_llm_client", return_value=fake):
            report = archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:30"), write_voice=True,
            )
        self.assertEqual(1, len(fake.calls))
        self.assertEqual("big", report["voice"]["target_session"])
        self.assertEqual("busiest", report["voice"]["selection"])
        self.assertEqual(
            fake.text, self.episode(f"ep:{DAY}:big")["summary_voice"]
        )
        self.assertTrue(
            self.episode(f"ep:{DAY}:small")["summary_voice"].startswith(
                "今天和主人哥哥聊了"
            )
        )

    def test_invalid_voice_output_falls_back_per_day(self) -> None:
        cases = [
            ("2026-10-19", "", "empty"),
            ("2026-10-20", "作为AI，我不能写日记。", "meta_reference"),
            ("2026-10-21", "主人哥哥的助手今天很开心。", "meta_reference"),
            ("2026-10-22", "很" * 601, "too_long"),
        ]
        for index, (day, text, reason) in enumerate(cases):
            hour = 9 + index % 2
            add_turn(
                self.db, self.memory, "s", "user",
                f"{day} 的一件小事", self.ts(f"{day} {hour:02d}:00"),
            )
            fake = FakeLLM(text)
            with patch("fatfish.nightly._build_llm_client", return_value=fake):
                report = archive_day(
                    self.db, self.config, self.memory, day,
                    now=self.ts("2026-10-23 00:30"), write_voice=True,
                )
            with self.subTest(day=day):
                self.assertEqual(1, len(fake.calls))
                self.assertEqual(1, report["voice"]["llm_calls"])
                self.assertEqual(reason, report["voice"]["rejected"])
                self.assertEqual("template_fallback", report["voice"]["source"])
                episode = self.episode(f"ep:{day}:s")
                self.assertTrue(
                    episode["summary_voice"].startswith("今天和主人哥哥聊了")
                )
                if text:
                    self.assertNotIn(text[:10], episode["summary_voice"])
                self.assertIn("事实时间线", episode["summary"])

    def test_voice_once_per_day_marker_survives_missing_episode_voice(self) -> None:
        add_turn(
            self.db, self.memory, "s", "user",
            "早上一起喝豆浆", self.ts("2026-10-22 09:00"),
        )
        fake = FakeLLM("主人哥哥今天早上喝了豆浆，我记得呢。")
        with patch("fatfish.nightly._build_llm_client", return_value=fake):
            archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:30"), write_voice=True,
            )
        self.assertEqual(1, len(fake.calls))
        # Simulate a crash that lost the episode narrative after the call.
        self.db.execute(
            "UPDATE episodes SET summary_voice='' WHERE id=?", (f"ep:{DAY}:s",)
        )
        with patch("fatfish.nightly._build_llm_client", return_value=fake):
            second = archive_day(
                self.db, self.config, self.memory, DAY,
                now=self.ts("2026-10-23 00:31"), write_voice=True,
            )
        self.assertEqual(1, len(fake.calls))
        self.assertEqual(0, second["voice"]["llm_calls"])
        self.assertTrue(second["voice"]["selection"].endswith(":already_called"))

    # ------------------------------------------------------------- edge cases
    def test_archive_writes_entities_and_tags_idempotently(self) -> None:
        add_turn(
            self.db, self.memory, "s1", "user",
            "主人哥哥今天被骂了，好委屈", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "s1", "assistant",
            "抱抱，我陪着你呢", self.ts("2026-10-22 09:01"),
        )
        report = archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:30"), write_voice=False,
        )
        session_report = report["sessions_detail"][0]
        self.assertGreaterEqual(session_report["entities"]["mentions"], 1)
        self.assertGreaterEqual(report["entities_mentions"], 1)
        self.assertGreaterEqual(session_report["tags_linked"], 2)
        self.assertGreaterEqual(report["tags_linked"], 2)

        event_mentions = self.db.query(
            "SELECT entity_id, source_id FROM entity_mentions WHERE source_type='event'"
        )
        self.assertTrue(event_mentions)

        event_memory = self.memories("event_archive")[0]
        self.assertIn("主人哥哥", json.loads(event_memory["entities_json"]))

        time_tags = {
            row["name"]
            for row in self.db.query("SELECT name FROM tags WHERE namespace='time'")
        }
        self.assertIn(DAY, time_tags)
        links_before = int(
            self.db.query_one("SELECT COUNT(*) AS n FROM tag_links")["n"]
        )
        self.assertGreater(links_before, 0)
        mentions_before = int(
            self.db.query_one("SELECT COUNT(*) AS n FROM entity_mentions")["n"]
        )
        tags_before = int(self.db.query_one("SELECT COUNT(*) AS n FROM tags")["n"])

        # Same-day rerun: nothing new is written (幂等).
        second = archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:31"), write_voice=False,
        )
        self.assertEqual(0, second["entities_mentions"])
        self.assertEqual(0, second["tags_linked"])
        self.assertEqual(0, second["sessions_detail"][0]["entities"]["mentions"])
        self.assertEqual(0, second["sessions_detail"][0]["tags_linked"])
        self.assertEqual(
            mentions_before,
            int(self.db.query_one("SELECT COUNT(*) AS n FROM entity_mentions")["n"]),
        )
        self.assertEqual(
            tags_before, int(self.db.query_one("SELECT COUNT(*) AS n FROM tags")["n"])
        )
        self.assertEqual(
            links_before, int(self.db.query_one("SELECT COUNT(*) AS n FROM tag_links")["n"])
        )

    def test_no_data_day_is_skipped(self) -> None:
        report = archive_day(
            self.db, self.config, self.memory, "2026-10-20",
            now=self.ts("2026-10-23 00:30"), write_voice=False,
        )
        self.assertTrue(report["skipped"])
        self.assertEqual("no_data", report["reason"])
        self.assertEqual(0, report["episodes"])
        self.assertEqual([], report["memory_ids"])
        self.assertEqual(0, self.db.query_one("SELECT COUNT(*) n FROM episodes")["n"])
        self.assertEqual(0, self.db.query_one("SELECT COUNT(*) n FROM memory_items")["n"])

        result = run_nightly(
            self.db, self.config, self.memory,
            now=self.ts("2026-10-23 00:30"),
            target_date="2026-10-20",
            write_voice=False,
        )
        self.assertTrue(result["archive"]["skipped"])
        self.assertFalse(result["diary_written"])
        self.assertEqual("2026-10-20", self.db.get_meta("last_nightly_date"))

    def test_event_memories_are_capped_and_tiny_events_merge(self) -> None:
        base = self.ts("2026-10-22 00:00")
        for index in range(25):
            add_turn(
                self.db, self.memory, "s", "user",
                f"第{index:02d}件事的具体内容", base + index * 50 * 60,
            )
            add_turn(
                self.db, self.memory, "s", "assistant",
                f"回应第{index:02d}件事", base + index * 50 * 60 + 30,
            )
        episode_id = f"ep:{DAY}:s"
        episode_before = self.episode(episode_id)
        self.assertIsNone(episode_before)

        archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:30"), write_voice=False,
        )
        rows = self.memories("event_archive")
        self.assertEqual(20, len(rows))
        self.assertTrue(any(str(row["source_id"]).startswith("merged:") for row in rows))
        self.assertEqual(
            [float(row["event_time_start"]) for row in rows],
            sorted(float(row["event_time_start"]) for row in rows),
        )
        merged = [row for row in rows if str(row["source_id"]).startswith("merged:")]
        self.assertTrue(any("等" in row["gist"] and "件事" in row["gist"] for row in merged))
        episode = self.episode(episode_id)
        self.assertEqual(25, len(json.loads(episode["event_ids_json"])))
        self.assertEqual(21, len(json.loads(episode["memory_ids_json"])))
        self.assertIn("第00件事", episode["summary"])
        self.assertIn("第24件事", episode["summary"])

    def test_factual_timeline_stays_within_four_thousand_chars(self) -> None:
        base = self.ts("2026-10-22 00:00")
        long_text = "这是一段很长的对话内容，用来测试事实时间线的长度上限。" * 4
        for index in range(28):
            at = base + index * 50 * 60
            add_turn(self.db, self.memory, "s", "user", f"事件{index:02d} {long_text}", at)
            add_turn(
                self.db, self.memory, "s", "assistant", f"回应{index:02d} {long_text}", at + 30
            )
        archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:30"), write_voice=False,
        )
        episode = self.episode(f"ep:{DAY}:s")
        self.assertLessEqual(len(episode["summary"]), 4000)
        self.assertIn("时间线已截断", episode["summary"])
        self.assertIn("事件00", episode["summary"])

    def test_fresh_archive_survives_compaction(self) -> None:
        add_turn(
            self.db, self.memory, "s", "user",
            "早上一起喝豆浆", self.ts("2026-10-22 09:00"),
        )
        add_turn(
            self.db, self.memory, "s", "user",
            "下午看看代码", self.ts("2026-10-22 10:30"),
        )
        now = self.ts("2026-10-23 00:30")
        result = run_nightly(self.db, self.config, self.memory, now=now, write_voice=False)
        self.assertEqual(0, result["compaction"]["archived"])

        statuses = {
            row["source_type"]: row["status"]
            for row in self.db.query(
                "SELECT source_type, status FROM memory_items "
                "WHERE source_type IN ('event_archive','episode')"
            )
        }
        self.assertEqual(
            {"event_archive": "active", "episode": "active"}, statuses
        )

        stats = self.memory.compact(dry_run=False, now=now + 3600)
        self.assertGreaterEqual(stats["protected"], 3)
        self.assertEqual(
            {"event_archive": "active", "episode": "active"},
            {
                row["source_type"]: row["status"]
                for row in self.db.query(
                    "SELECT source_type, status FROM memory_items "
                    "WHERE source_type IN ('event_archive','episode')"
                )
            },
        )

    def test_dry_run_writes_nothing(self) -> None:
        add_turn(
            self.db, self.memory, "s", "user",
            "早上一起喝豆浆", self.ts("2026-10-22 09:00"),
        )
        report = archive_day(
            self.db, self.config, self.memory, DAY,
            now=self.ts("2026-10-23 00:30"),
            write_voice=False,
            dry_run=True,
        )
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["skipped"])
        self.assertEqual(0, self.db.query_one("SELECT COUNT(*) n FROM episodes")["n"])
        self.assertEqual(0, self.db.query_one("SELECT COUNT(*) n FROM memory_items")["n"])
        turn = self.db.query_one("SELECT archived_at FROM conversation_turns")
        self.assertIsNone(turn["archived_at"])
        self.assertEqual(
            "open", self.db.query_one("SELECT status FROM conversation_events")["status"]
        )


if __name__ == "__main__":
    unittest.main()
