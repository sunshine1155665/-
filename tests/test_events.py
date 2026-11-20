from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fatfish.clock import parse_when
from fatfish.config import load_config
from fatfish.db import SCHEMA_VERSION, Database
from fatfish.memory import MemoryService
from fatfish.memory.events import EventBuilder


def make_env(tmp: Path, name: str = "events.db"):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / name).as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128
event_gap_minutes = 45
event_min_gap_for_topic_minutes = 10
event_topic_shift_threshold = 0.15
today_timeline_chars = 1200
"""
    path = tmp / "events.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def insert_turn(
    db: Database,
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
    return int(cur.lastrowid)


def observe(
    builder: EventBuilder,
    db: Database,
    session_id: str,
    role: str,
    content: str,
    at: float,
) -> tuple[int, str]:
    turn_id = insert_turn(db, session_id, role, content, at)
    return turn_id, builder.observe_turn(turn_id, session_id, role, content, at)


def event_row(db: Database, event_id: str):
    return db.query_one("SELECT * FROM conversation_events WHERE id=?", (event_id,))


class EventBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.builder = EventBuilder(self.db, self.config)
        self.tz = self.config.timezone_name
        self.base = parse_when("2026-10-20 10:00", self.tz)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_turns_one_minute_apart_share_one_event(self) -> None:
        first_turn, first = observe(
            self.builder, self.db, "s", "user", "主人哥哥，今天中午吃什么呀", self.base
        )
        second_turn, second = observe(
            self.builder,
            self.db,
            "s",
            "assistant",
            "吃番茄牛腩饭好不好，主人哥哥？",
            self.base + 60,
        )
        self.assertEqual(first, second)
        row = event_row(self.db, first)
        self.assertEqual("open", row["status"])
        self.assertEqual(
            [first_turn, second_turn], json.loads(row["turn_ids_json"])
        )
        self.assertEqual(self.base + 60, row["end_at"])
        self.assertEqual("主人哥哥，今天中午吃什么呀", row["title"])

    def test_gap_over_45_minutes_starts_a_new_event(self) -> None:
        _, first = observe(
            self.builder, self.db, "s", "user", "我们去公园散步吧", self.base
        )
        _, second = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "我们接着去看电影吧",
            self.base + 60 * 60,
        )
        self.assertNotEqual(first, second)
        old = event_row(self.db, first)
        self.assertEqual("closed", old["status"])
        self.assertEqual(self.base, old["end_at"])
        self.assertTrue(old["summary"])
        self.assertLessEqual(len(old["summary"]), 300)
        self.assertIn("主人", old["summary"])
        self.assertIsNone(old["archived_at"])

        new = event_row(self.db, second)
        self.assertEqual("open", new["status"])
        self.assertEqual(self.base + 60 * 60, new["start_at"])

    def test_large_topic_shift_after_ten_minutes_starts_a_new_event(self) -> None:
        _, first = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "今天天气真好我们出去散步吧",
            self.base,
        )
        _, second = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "帮我写一段Python快速排序的代码",
            self.base + 15 * 60,
        )
        self.assertNotEqual(first, second)
        self.assertEqual("closed", event_row(self.db, first)["status"])

    def test_similar_topic_after_ten_minutes_stays_in_same_event(self) -> None:
        _, first = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "今天天气真好我们出去散步吧",
            self.base,
        )
        _, second = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "今天天气真好我们去公园散步吧",
            self.base + 15 * 60,
        )
        self.assertEqual(first, second)
        self.assertEqual("open", event_row(self.db, first)["status"])

    def test_assistant_turns_are_included(self) -> None:
        _, event_id = observe(
            self.builder, self.db, "s", "user", "陪我聊会儿天", self.base
        )
        _, assistant_event = observe(
            self.builder, self.db, "s", "assistant", "好呀，我在这边陪你", self.base + 5
        )
        self.assertEqual(event_id, assistant_event)
        rows = self.db.query(
            "SELECT role FROM conversation_turns WHERE event_id=? ORDER BY id",
            (event_id,),
        )
        self.assertEqual(["user", "assistant"], [row["role"] for row in rows])

    def test_observe_turn_is_idempotent(self) -> None:
        turn_id, event_id = observe(
            self.builder, self.db, "s", "user", "重复观察同一条消息", self.base
        )
        again = self.builder.observe_turn(
            turn_id, "s", "user", "重复观察同一条消息", self.base
        )
        self.assertEqual(event_id, again)
        row = event_row(self.db, event_id)
        self.assertEqual([turn_id], json.loads(row["turn_ids_json"]))
        count = self.db.query_one("SELECT COUNT(*) AS n FROM conversation_events")
        self.assertEqual(1, int(count["n"]))
        turn = self.db.query_one(
            "SELECT event_id FROM conversation_turns WHERE id=?", (turn_id,)
        )
        self.assertEqual(event_id, turn["event_id"])

    def test_title_comes_from_first_user_line(self) -> None:
        _, event_id = observe(
            self.builder,
            self.db,
            "s",
            "assistant",
            "主人哥哥怎么还不回来呀",
            self.base,
        )
        _, same = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "我回来了，路上有点堵\n晚饭吃了吗",
            self.base + 30,
        )
        self.assertEqual(event_id, same)
        self.assertEqual("我回来了，路上有点堵", event_row(self.db, event_id)["title"])
        self.assertLessEqual(len(event_row(self.db, event_id)["title"]), 40)

    def test_topics_are_updated_and_deterministic(self) -> None:
        _, event_id = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "今天我们聊了天气、散步和晚饭",
            self.base,
        )
        _, event_id = observe(
            self.builder,
            self.db,
            "s",
            "user",
            "晚饭想吃火锅",
            self.base + 60,
        )
        topics = json.loads(event_row(self.db, event_id)["topics_json"])
        self.assertTrue(topics)
        self.assertTrue(all(isinstance(topic, str) for topic in topics))
        self.assertEqual(topics, json.loads(event_row(self.db, event_id)["topics_json"]))

    def test_add_turn_creates_events_and_preserves_int_return(self) -> None:
        service = MemoryService(self.db, self.config)
        first = service.add_turn("s2", "user", "集成测试的第一句话")
        second = service.add_turn("s2", "assistant", "集成测试的第二句话")
        self.assertIsInstance(first, int)
        self.assertIsInstance(second, int)
        rows = self.db.query(
            "SELECT id, event_id FROM conversation_turns WHERE session_id='s2' ORDER BY id"
        )
        self.assertEqual(2, len(rows))
        self.assertEqual(rows[0]["event_id"], rows[1]["event_id"])
        self.assertIsNotNone(rows[0]["event_id"])

    def test_close_open_events_sets_summary_and_end(self) -> None:
        _, event_id = observe(
            self.builder, self.db, "s", "user", "第一句话", self.base
        )
        observe(self.builder, self.db, "s", "assistant", "第二句话", self.base + 30)
        observe(self.builder, self.db, "s", "user", "第三句话" * 200, self.base + 60)

        closed = self.builder.close_open_events(session_id="s")
        self.assertEqual(1, closed)
        row = event_row(self.db, event_id)
        self.assertEqual("closed", row["status"])
        self.assertEqual(self.base + 60, row["end_at"])
        self.assertTrue(row["summary"])
        self.assertLessEqual(len(row["summary"]), 300)
        self.assertIn("第一句话", row["summary"])
        self.assertEqual(
            3, len(json.loads(row["turn_ids_json"]))
        )
        self.assertIsNone(row["archived_at"])

        # Nothing left to close.
        self.assertEqual(0, self.builder.close_open_events(session_id="s"))

    def test_close_open_events_can_limit_by_session(self) -> None:
        _, a = observe(self.builder, self.db, "a", "user", "会话A", self.base)
        _, b = observe(self.builder, self.db, "b", "user", "会话B", self.base)
        self.assertEqual(1, self.builder.close_open_events(session_id="a"))
        self.assertEqual("closed", event_row(self.db, a)["status"])
        self.assertEqual("open", event_row(self.db, b)["status"])


class SchemaV3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp, name="migration.db")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_v4_migration_is_idempotent_and_preserves_data(self) -> None:
        self.assertEqual(SCHEMA_VERSION, self.db.get_meta("schema_version"))
        turn_id = insert_turn(
            self.db, "legacy", "user", "迁移前就存在的一句话", self.base_ts()
        )
        self.db.execute(
            "INSERT INTO memory_items(id, gist, observed_at, created_at, updated_at) "
            "VALUES ('mem-1', '旧记忆', ?, ?, ?)",
            (self.base_ts(), self.base_ts(), self.base_ts()),
        )
        self.db.execute(
            "INSERT INTO episodes(id, title, summary, created_at) VALUES ('ep-1', '旧片段', '旧摘要', ?)",
            (self.base_ts(),),
        )

        # Simulate a schema-v2 database by removing the v3 additions.
        self.db.execute("DROP INDEX IF EXISTS idx_turn_event")
        self.db.execute("DROP INDEX IF EXISTS idx_memory_event")
        for table, columns in {
            "conversation_turns": ("event_id", "episode_id", "archived_at"),
            "memory_items": ("event_id", "episode_id"),
            "episodes": (
                "session_id",
                "event_ids_json",
                "summary_voice",
                "turn_count",
                "updated_at",
            ),
        }.items():
            for column in columns:
                self.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        self.db.execute("DROP TABLE IF EXISTS conversation_events")
        self.db.set_meta("schema_version", "2")
        path = self.db.path
        self.db.close()

        self.db = Database(path)
        self.db.initialize()
        self.assertEqual(SCHEMA_VERSION, self.db.get_meta("schema_version"))

        turn_cols = self.db.columns("conversation_turns")
        for column in ("event_id", "episode_id", "archived_at"):
            self.assertIn(column, turn_cols)
        memory_cols = self.db.columns("memory_items")
        for column in ("event_id", "episode_id"):
            self.assertIn(column, memory_cols)
        episode_cols = self.db.columns("episodes")
        for column in ("session_id", "event_ids_json", "summary_voice", "turn_count", "updated_at"):
            self.assertIn(column, episode_cols)
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("conversation_events", tables)

        turn = self.db.query_one(
            "SELECT content, event_id FROM conversation_turns WHERE id=?", (turn_id,)
        )
        self.assertEqual("迁移前就存在的一句话", turn["content"])
        self.assertIsNone(turn["event_id"])
        memory = self.db.query_one("SELECT gist FROM memory_items WHERE id='mem-1'")
        self.assertEqual("旧记忆", memory["gist"])
        episode = self.db.query_one(
            "SELECT title, summary, updated_at, event_ids_json FROM episodes WHERE id='ep-1'"
        )
        self.assertEqual("旧片段", episode["title"])
        self.assertEqual("旧摘要", episode["summary"])
        self.assertEqual(self.base_ts(), episode["updated_at"])
        self.assertEqual("[]", episode["event_ids_json"])

        second = self.db.migrate()
        self.assertEqual([], second["applied"])
        self.assertEqual(SCHEMA_VERSION, second["schema_version"])
        self.assertEqual(
            0, int(self.db.query_one("SELECT COUNT(*) AS n FROM conversation_events")["n"])
        )

        # The migrated schema accepts new events and turn links.
        self.db.execute(
            "INSERT INTO conversation_events(id, session_id, start_at, status, created_at, updated_at) "
            "VALUES ('ev-new', 'legacy', ?, 'open', ?, ?)",
            (self.base_ts(), self.base_ts(), self.base_ts()),
        )
        self.db.execute(
            "UPDATE conversation_turns SET event_id='ev-new' WHERE id=?", (turn_id,)
        )
        linked = self.db.query_one(
            "SELECT event_id FROM conversation_turns WHERE id=?", (turn_id,)
        )
        self.assertEqual("ev-new", linked["event_id"])

    def test_ensure_schema_only_initialises_when_version_is_stale(self) -> None:
        # The web server calls this per request; the whole point is that the
        # common path does not re-run schema.sql.
        with mock.patch.object(self.db, "initialize") as initialize:
            self.assertFalse(self.db.ensure_schema())
            initialize.assert_not_called()

            self.db.set_meta("schema_version", "2")
            self.assertTrue(self.db.ensure_schema())
            initialize.assert_called_once_with()

    @staticmethod
    def base_ts() -> float:
        return parse_when("2026-10-20 10:00", "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()

