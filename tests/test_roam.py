"""ADR-0014 P0: multi-session roaming, offline and temporary.

No network, no real ``data/fatfish.db``.  The scaffold uses the same temp env
as ``test_p0`` and binds owner + family/teacher/friend rooms directly.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fatfish.context import ContextAssembler
from fatfish.db import SCHEMA_VERSION, Database
from fatfish.llm import LLMResult
from fatfish.memory import MemoryService
from fatfish.memory.entities import create_person
from fatfish.owner import OwnerRegistry
from fatfish.roam import (
    ATTENTION_HEADER,
    RECAP_HEADER,
    RoamService,
    deterministic_summary,
)
from fatfish.roam.intent import parse_return_command, parse_visit_command
from fatfish.roam.model import THREAD_BLOCKED, TRIP_ACTIVE, TRIP_INTERRUPTED, TRIP_RETURNED
from fatfish.runtime import Runtime, TurnAction
from fatfish.security import TrustLevel
from fatfish.voice import VoiceDecision
from tests.test_p0 import make_env

OWNER = "qq:private:10001"
MOM = "qq:private:30001"
TEACHER = "qq:private:30002"
SISTER = "qq:private:30003"


class RoamTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-roam-")
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.owner = OwnerRegistry(self.db, self.config)
        self.owner.bind(OWNER, "主人哥哥")
        self.owner.bind(MOM, "妈妈", role="family")
        self.owner.bind(TEACHER, "杨老师", role="teacher")
        self.owner.bind(SISTER, "姐姐", role="friend")
        self.memory = MemoryService(self.db, self.config)
        self.roam = RoamService(self.db, self.config, memory=self.memory)
        self.now = time.time()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _start(self, room: str = "妈妈", *, purpose: str = "问她身体怎么样", now: float | None = None):
        now = self.now if now is None else now
        result = self.roam.start_trip(
            room, purpose=purpose, home_session=OWNER, source="owner", now=now
        )
        self.assertTrue(result["ok"], result)
        self.roam.activate(result["trip"].trip_id, now=now)
        return self.roam.trip(result["trip"].trip_id)

    def _add_turn(self, session: str, role: str, text: str, trip_id: str | None):
        return self.memory.add_turn(session, role, text, trip_id=trip_id)

    def _turns(self, trip_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM conversation_turns WHERE trip_id=?", (trip_id,)
        )
        return int(row["n"]) if row else 0


class SchemaV14Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "v14.db"
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.db.initialize()

    def test_tables_columns_and_indexes(self) -> None:
        self.assertEqual(SCHEMA_VERSION, "17")
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("session_threads", tables)
        self.assertIn("roam_trips", tables)
        self.assertIn("trip_id", self.db.columns("conversation_turns"))
        indexes = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='index'")
        }
        for name in (
            "idx_threads_status",
            "idx_trips_thread_status",
            "idx_trips_home_status",
            "idx_turn_trip",
        ):
            self.assertIn(name, indexes)

    def test_v13_database_upgrades_in_place(self) -> None:
        # Simulate a v13 database in a separate file: the v14 tables are
        # missing and conversation_turns predates ``trip_id``.  The old table
        # shape is written by hand because SQLite's DROP COLUMN chokes on the
        # inline comment schema.sql carries for the new column.
        import sqlite3

        old_path = Path(self._tmp.name) / "v13.db"
        conn = sqlite3.connect(old_path)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '13');
            CREATE TABLE conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                used_refs_json TEXT NOT NULL DEFAULT '[]',
                ephemeral INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.close()

        old = Database(old_path)
        self.addCleanup(old.close)
        old.initialize()
        self.assertEqual("17", old.get_meta("schema_version"))
        self.assertIn("trip_id", old.columns("conversation_turns"))
        tables = {
            row["name"]
            for row in old.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("session_threads", tables)
        self.assertIn("roam_trips", tables)
        self.assertEqual(old.migrate()["applied"], [])

    def test_trip_column_defaults_null(self) -> None:
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) "
            "VALUES ('s', 'user', 'hi', ?)",
            (time.time(),),
        )
        row = self.db.query_one("SELECT trip_id FROM conversation_turns")
        self.assertIsNone(row["trip_id"])


class IntentTests(unittest.TestCase):
    def setUp(self) -> None:
        from fatfish.roam.model import Room

        self.rooms = [
            Room(MOM, "family", "妈妈"),
            Room(TEACHER, "teacher", "南科曼曼", aliases=["杨老师"]),
        ]

    def test_visit_command_with_purpose(self) -> None:
        parsed = parse_visit_command("你去看看妈妈，问她身体怎么样", self.rooms)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["room"].session_id, MOM)
        self.assertEqual(parsed["purpose"], "身体怎么样")

    def test_alias_and_no_purpose(self) -> None:
        parsed = parse_visit_command("去看看杨老师", self.rooms)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["room"].session_id, TEACHER)
        self.assertEqual(parsed["purpose"], "")

    def test_question_is_not_a_departure(self) -> None:
        self.assertIsNone(parse_visit_command("妈妈今天说什么了", self.rooms))
        self.assertIsNone(parse_visit_command("杨老师作业交了吗", self.rooms))

    def test_negation_is_not_a_departure(self) -> None:
        self.assertIsNone(parse_visit_command("别去看看妈妈", self.rooms))
        self.assertIsNone(parse_visit_command("先别去找杨老师", self.rooms))

    def test_unknown_name_is_not_guessed(self) -> None:
        self.assertIsNone(parse_visit_command("去看看楼下保安", self.rooms))

    def test_return_command(self) -> None:
        self.assertTrue(parse_return_command("你回来吧"))
        self.assertTrue(parse_return_command("别去了"))
        self.assertFalse(parse_return_command("今天吃什么"))


class RoomTests(RoamTestCase):
    def test_rooms_are_bound_private_allowed_roles(self) -> None:
        rooms = {room.display_name: room for room in self.roam.rooms()}
        self.assertEqual({"妈妈", "杨老师", "姐姐"}, set(rooms))
        self.assertEqual("family", rooms["妈妈"].role)
        self.assertEqual("30001", rooms["妈妈"].target_uin)

    def test_owner_and_group_are_excluded(self) -> None:
        self.db.execute(
            "INSERT INTO owner_bindings(session_id, display_name, bound_at, role, platform, source) "
            "VALUES (?,?,?,?,?,?)",
            ("qq:group:777", "家庭群", self.now, "family", "qq", "manual"),
        )
        names = {room.display_name for room in self.roam.rooms()}
        self.assertNotIn("主人哥哥", names)
        self.assertNotIn("家庭群", names)

    def test_role_filter_and_extra_session(self) -> None:
        self.config._data.setdefault("roam", {})["allowed_roles"] = ["family"]
        self.assertEqual(["妈妈"], [room.display_name for room in self.roam.rooms()])
        self.config._data["roam"]["allowed_sessions"] = [TEACHER]
        self.assertEqual(
            {"妈妈", "杨老师"}, {room.display_name for room in self.roam.rooms()}
        )

    def test_person_card_aliases_join_the_room(self) -> None:
        create_person(
            self.db,
            name="南科曼曼",
            uin="30002",
            aliases=["杨老师", "藤老师"],
            relation="老师",
            now=self.now,
        )
        room = self.roam.room_for_session(TEACHER)
        self.assertIsNotNone(room)
        self.assertIn("杨老师", room.names())


class TripLifecycleTests(RoamTestCase):
    def test_start_creates_thread_and_trip(self) -> None:
        trip = self._start()
        self.assertEqual(TRIP_ACTIVE, trip.status)
        thread = self.roam.get_thread(MOM)
        self.assertIsNotNone(thread)
        self.assertEqual("妈妈", thread.display_name)
        self.assertEqual("family", thread.role)
        self.assertEqual("30001", thread.target_uin)
        self.assertEqual(1, thread.trip_count)
        self.assertTrue(self.roam.opening_text(trip).startswith("妈妈，主人哥哥让我"))

    def test_second_room_is_busy_and_same_room_is_existing(self) -> None:
        trip = self._start()
        same = self.roam.start_trip(
            MOM, purpose="再说两句", home_session=OWNER, source="owner", now=self.now
        )
        self.assertTrue(same["ok"])
        self.assertTrue(same["existing"])
        self.assertEqual(trip.trip_id, same["trip"].trip_id)
        other = self.roam.start_trip(
            TEACHER, home_session=OWNER, source="owner", now=self.now
        )
        self.assertFalse(other["ok"])
        self.assertEqual("roam_busy", other["error"])

    def test_stranger_and_group_are_refused(self) -> None:
        self.assertFalse(
            self.roam.start_trip("qq:private:39999", home_session=OWNER, now=self.now)["ok"]
        )
        self.db.execute(
            "INSERT INTO owner_bindings(session_id, display_name, bound_at, role, platform, source) "
            "VALUES (?,?,?,?,?,?)",
            ("qq:group:777", "家庭群", self.now, "family", "qq", "manual"),
        )
        result = self.roam.start_trip("qq:group:777", home_session=OWNER, now=self.now)
        self.assertFalse(result["ok"])
        self.assertIn(result["error"], {"room_not_allowed", "roam_group_not_allowed"})

    def test_disabled_service_refuses(self) -> None:
        self.config._data.setdefault("roam", {})["enabled"] = False
        self.assertFalse(
            self.roam.start_trip(MOM, home_session=OWNER, now=self.now)["ok"]
        )

    def test_daily_trip_limit(self) -> None:
        self.config._data.setdefault("roam", {})["max_trips_per_day"] = 1
        first = self._start()
        self.roam.leave_trip(first, reason="idle", now=self.now + 60)
        # A different room: no per-room cooldown, so the daily cap is the gate.
        result = self.roam.start_trip(
            TEACHER, home_session=OWNER, source="scheduler", now=self.now + 120
        )
        self.assertFalse(result["ok"])
        self.assertEqual("daily_trip_limit", result["error"])

    def test_owner_bypasses_cooldown_scheduler_does_not(self) -> None:
        trip = self._start()
        self.roam.leave_trip(trip, reason="idle", now=self.now + 60)
        denied = self.roam.start_trip(
            MOM, home_session=OWNER, source="scheduler", now=self.now + 120
        )
        self.assertFalse(denied["ok"])
        self.assertEqual("thread_cooldown", denied["error"])
        allowed = self.roam.start_trip(
            MOM, home_session=OWNER, source="owner", now=self.now + 120
        )
        self.assertTrue(allowed["ok"], allowed)


class InboundTests(RoamTestCase):
    def test_target_message_resets_idle(self) -> None:
        trip = self._start()
        later = self.now + 10 * 60
        result = self.roam.note_inbound(MOM, now=later)
        self.assertEqual("target", result["kind"])
        thread = self.roam.get_thread(MOM)
        self.assertEqual(later, thread.last_seen_at)
        self.assertEqual(0, thread.unread_count)
        self.assertEqual(1, self.roam.active_trip().message_count)
        # The idle countdown runs from the new message, not the start.
        self.assertIsNone(self.roam.return_reason(trip, now=self.now + 20 * 60 + 1))
        self.assertEqual(
            "idle", self.roam.return_reason(trip, now=later + 20 * 60 + 1)
        )

    def test_other_room_message_only_raises_unread(self) -> None:
        self._start()
        result = self.roam.note_inbound(MOM, now=self.now + 60)
        self.assertEqual("target", result["kind"])
        self.roam.upsert_thread(TEACHER, display_name="杨老师", now=self.now)
        result = self.roam.note_inbound(TEACHER, now=self.now + 120)
        self.assertEqual("other_unread", result["kind"])
        self.assertEqual(1, self.roam.get_thread(TEACHER).unread_count)
        self.assertEqual(MOM, self.roam.active_trip().thread_session)

    def test_owner_message_preempts(self) -> None:
        trip = self._start()
        result = self.roam.note_inbound(OWNER, now=self.now + 60)
        self.assertEqual("owner_preempt", result["kind"])
        self.assertIsNone(self.roam.active_trip())
        updated = self.roam.trip(trip.trip_id)
        self.assertEqual(TRIP_RETURNED, updated.status)
        self.assertEqual("owner_preempt", updated.close_reason)

    def test_target_stop_blocks_the_room_for_the_day(self) -> None:
        trip = self._start()
        self.roam.note_inbound(MOM, now=self.now + 30)
        left = self.roam.target_stop(MOM, now=self.now + 60)
        self.assertTrue(left["ok"])
        thread = self.roam.get_thread(MOM)
        self.assertEqual(THREAD_BLOCKED, thread.status)
        self.assertIsNotNone(thread.cooldown_until)
        # Even an owner order cannot reopen a room the target closed today.
        denied = self.roam.start_trip(
            MOM, home_session=OWNER, source="owner", now=self.now + 120
        )
        self.assertFalse(denied["ok"])
        self.assertEqual("thread_blocked", denied["error"])


class SummaryTests(RoamTestCase):
    def test_deterministic_summary_uses_trip_turns_only(self) -> None:
        payload = deterministic_summary(
            [
                {"role": "assistant", "content": "妈妈，我来看看你"},
                {"role": "user", "content": "腰还是不舒服"},
                {"role": "user", "content": "周末想回来吃饭"},
            ],
            purpose="问她身体",
            display_name="妈妈",
        )
        self.assertIn("问她身体", payload.summary)
        self.assertIn("周末想回来吃饭", payload.summary)
        self.assertTrue(payload.key_facts)
        self.assertTrue(payload.degraded)

    def test_deterministic_summary_handles_no_reply(self) -> None:
        payload = deterministic_summary(
            [{"role": "assistant", "content": "妈妈，我来看看你"}],
            display_name="妈妈",
        )
        self.assertIn("对方还没回话", payload.summary)
        self.assertEqual([], payload.key_facts)

    def test_leave_updates_thread_card_and_owner_memory(self) -> None:
        trip = self._start()
        self._add_turn(MOM, "assistant", "妈妈，我来看看你", trip.trip_id)
        self._add_turn(MOM, "user", "腰还是不舒服", trip.trip_id)
        self._add_turn(MOM, "user", "周末想回来吃饭", trip.trip_id)
        left = self.roam.leave_trip(trip, reason="idle", now=self.now + 21 * 60)
        self.assertTrue(left["ok"])
        self.assertEqual(2, left["user_turns"])
        thread = self.roam.get_thread(MOM)
        self.assertIn("周末想回来吃饭", thread.summary)
        self.assertTrue(thread.key_facts)
        self.assertIsNotNone(thread.last_summary_at)
        updated = self.roam.trip(trip.trip_id)
        self.assertEqual(TRIP_RETURNED, updated.status)
        self.assertEqual("idle", updated.close_reason)

        owner_hits = self.memory.search(
            "妈妈 身体 周末",
            limit=10,
            viewer_session=OWNER,
            viewer_role="owner",
        )
        roam_hits = [hit for hit in owner_hits if hit.item.origin_scope == "roam"]
        self.assertTrue(roam_hits, "owner must be able to recall the roam summary")
        self.assertIn("周末想回来吃饭", roam_hits[0].item.summary)

        guest_hits = self.memory.search(
            "妈妈 身体 周末",
            limit=10,
            viewer_session=TEACHER,
            viewer_role="teacher",
        )
        self.assertFalse(
            [hit for hit in guest_hits if hit.item.origin_scope == "roam"],
            "a target room must not see roam memory",
        )

    def test_owner_raw_detail_recall_excludes_bound_private_rooms(self) -> None:
        self.db.execute(
            "INSERT INTO event_details(id, event_id, session_id, seq, kind, text, "
            "occurred_at, detail_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "detail-1",
                "event-1",
                MOM,
                1,
                "fact",
                "腰还是不舒服，周末想回来吃饭",
                self.now,
                "hash-1",
                self.now,
            ),
        )
        owner_rows = self.memory.recall_details(
            "腰还是不舒服", limit=5, viewer_role="owner"
        )
        self.assertEqual([], owner_rows)
        # Without a viewer role the same row is visible (internal/diagnostic
        # path); the gate is specifically owner-vs-bound-room.
        raw_rows = self.memory.recall_details("腰还是不舒服", limit=5)
        self.assertTrue(raw_rows)

    def test_notify_text_has_duration_and_summary(self) -> None:
        trip = self._start()
        self._add_turn(MOM, "user", "腰还是不舒服", trip.trip_id)
        left = self.roam.leave_trip(trip, reason="idle", now=self.now + 21 * 60)
        text = self.roam.notify_return_text(left["trip"])
        self.assertIn("我回来了", text)
        self.assertIn("妈妈", text)
        self.assertIn("腰还是不舒服", text)

    def test_empty_trip_summary_is_honest(self) -> None:
        trip = self._start()
        left = self.roam.leave_trip(trip, reason="idle", now=self.now + 5 * 60)
        self.assertIn("没有聊到新内容", left["summary"].summary)


class AttentionTests(RoamTestCase):
    def test_target_sees_only_its_own_card(self) -> None:
        self.roam.upsert_thread(
            TEACHER,
            display_name="杨老师",
            summary="作业已经交了",
            now=self.now,
        )
        trip = self._start()
        self._add_turn(MOM, "user", "腰还是不舒服", trip.trip_id)
        self.roam.leave_trip(trip, reason="idle", now=self.now + 21 * 60)
        # A second trip back into the room: the block carries this room's own
        # earlier summary and this trip's purpose, never the teacher's card.
        trip2 = self._start(
            purpose="问问周末回不回来", now=self.now + 2 * 60 * 60
        )
        self._add_turn(MOM, "user", "周末想回来吃饭", trip2.trip_id)
        block = self.roam.attention_block(
            MOM, role="family", now=self.now + 2 * 60 * 60 + 60
        )
        self.assertIn(ATTENTION_HEADER, block)
        self.assertIn("问问周末回不回来", block)
        self.assertIn("腰还是不舒服", block)
        self.assertNotIn("杨老师", block)
        self.assertNotIn("作业已经交了", block)
        self.assertNotIn(OWNER, block)

    def test_owner_block_has_recap_and_active_trip(self) -> None:
        trip = self._start()
        self._add_turn(MOM, "user", "腰还是不舒服", trip.trip_id)
        self.roam.leave_trip(trip, reason="idle", now=self.now + 21 * 60)
        block = self.roam.attention_block(OWNER, role="owner", now=self.now + 22 * 60)
        self.assertIn(RECAP_HEADER, block)
        self.assertIn("妈妈", block)
        self.assertIn("腰还是不舒服", block)

    def test_other_room_gets_no_block(self) -> None:
        self._start()
        self.assertEqual(
            "", self.roam.attention_block(TEACHER, role="teacher", now=self.now)
        )

    def test_block_is_capped(self) -> None:
        self.config._data.setdefault("roam", {})["attention_max_chars"] = 120
        trip = self._start(purpose="问" * 200)
        block = self.roam.attention_block(MOM, role="family", now=self.now)
        self.assertLessEqual(len(block), 120)
        self.assertIn(ATTENTION_HEADER, block)
        self.assertTrue(block)

    def test_recap_watermark_advances_once(self) -> None:
        trip = self._start()
        self.roam.leave_trip(trip, reason="idle", now=self.now + 60)
        ids = self.roam.pending_recap_ids(now=self.now + 120)
        self.assertEqual(1, len(ids))
        self.roam.ack_recaps(ids)
        self.assertEqual([], self.roam.pending_recap_ids(now=self.now + 180))


class ContextOrderingTests(RoamTestCase):
    def test_attention_sits_between_timeline_and_history(self) -> None:
        trip = self._start()
        self._add_turn(MOM, "user", "腰还是不舒服", trip.trip_id)
        assembler = ContextAssembler(self.db, self.config, self.memory)
        bundle = assembler.build(MOM, "在吗", role="family", now=self.now)
        names = [section.name for section in bundle.sections]
        self.assertEqual(
            [
                "stable_prefix",
                "past_timeline",
                "attention_block",
                "history",
                "runtime_context",
                "turn_tail",
            ],
            names,
        )
        self.assertIn(ATTENTION_HEADER, bundle.sections[2].text)
        self.assertNotIn("杨老师", bundle.sections[2].text)
        wire = bundle.model_messages()
        attention_index = next(
            index
            for index, message in enumerate(wire)
            if ATTENTION_HEADER in str(message.get("content") or "")
        )
        self.assertLess(attention_index, len(wire))

    def test_owner_context_carries_recap(self) -> None:
        trip = self._start()
        self.roam.leave_trip(trip, reason="idle", now=self.now + 60)
        assembler = ContextAssembler(self.db, self.config, self.memory)
        bundle = assembler.build(OWNER, "在吗", role="owner", now=self.now + 120)
        block = next(s.text for s in bundle.sections if s.name == "attention_block")
        self.assertIn(RECAP_HEADER, block)

    def test_context_without_roam_tables_degrades_to_empty(self) -> None:
        self.db.execute("DROP TABLE session_threads")
        self.db.execute("DROP TABLE roam_trips")
        assembler = ContextAssembler(self.db, self.config, self.memory)
        bundle = assembler.build(OWNER, "在吗", role="owner", now=self.now)
        block = next(s.text for s in bundle.sections if s.name == "attention_block")
        self.assertEqual("", block)


class RuntimeRoamTests(RoamTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runtime = Runtime(self.db, self.config)
        self.runtime.llm.api_key = "fake"
        self.runtime.llm.chat = lambda *args, **kwargs: LLMResult(
            text='{"messages":["好，我去看看。"],"actions":[]}'
        )
        self.runtime.voice.decide = lambda *args, **kwargs: VoiceDecision(
            "text_only", 0.1, ["test"]
        )

    def _say(self, session: str, text: str, *, now: float):
        return self.runtime.handle_user_message(session, text, now=now)

    def test_owner_command_starts_trip_and_queues_opening(self) -> None:
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        trip = self.runtime.roam.active_trip()
        self.assertIsNotNone(trip)
        self.assertEqual(MOM, trip.thread_session)
        self.assertEqual("身体怎么样", trip.purpose)
        self.assertEqual(1, self._turns(trip.trip_id))
        item = self.db.query_one(
            "SELECT kind, payload_json FROM outbox WHERE kind='roam_message'"
        )
        self.assertIsNotNone(item)
        self.assertIn("妈妈", item["payload_json"])
        self.assertIn("身体怎么样", item["payload_json"])

    def test_target_reply_is_tagged_and_resets_idle(self) -> None:
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        trip = self.runtime.roam.active_trip()
        self._say(MOM, "腰还是不舒服", now=self.now + 600)
        rows = self.db.query(
            "SELECT role, trip_id FROM conversation_turns WHERE session_id=? ORDER BY id",
            (MOM,),
        )
        self.assertTrue(all(row["trip_id"] == trip.trip_id for row in rows))
        thread = self.runtime.roam.get_thread(MOM)
        self.assertEqual(self.now + 600, thread.last_seen_at)

    def test_idle_return_writes_summary_and_notice(self) -> None:
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        self._say(MOM, "腰还是不舒服，周末想回来吃饭", now=self.now + 600)
        result = self.runtime.maybe_roam_return(now=self.now + 600 + 20 * 60 + 1)
        self.assertIsNotNone(result)
        self.assertEqual("idle", result["reason"])
        self.assertIsNone(self.runtime.roam.active_trip())
        self.assertIn("周末想回来吃饭", result["summary"].summary)
        notice = self.db.query_one("SELECT * FROM outbox WHERE kind='roam_notice'")
        self.assertIsNotNone(notice)
        self.assertIn("我回来了", notice["payload_json"])

    def test_owner_preempt_leaves_without_notice(self) -> None:
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        self._say(MOM, "腰还是不舒服", now=self.now + 60)
        self._say(OWNER, "你先回来", now=self.now + 120)
        self.assertIsNone(self.runtime.roam.active_trip())
        self.assertIsNone(
            self.db.query_one("SELECT id FROM outbox WHERE kind='roam_notice'")
        )
        opening = self.db.query_one(
            "SELECT status FROM outbox WHERE kind='roam_message'"
        )
        self.assertIsNotNone(opening)
        self.assertEqual("cancelled", opening["status"])

    def test_target_chat_stop_ends_trip(self) -> None:
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        self._say(MOM, "别聊了", now=self.now + 30)
        self.assertIsNone(self.runtime.roam.active_trip())
        thread = self.runtime.roam.get_thread(MOM)
        self.assertEqual(THREAD_BLOCKED, thread.status)

    def test_restart_reconciliation_interrupts_without_replay(self) -> None:
        trip = self._start()
        self._add_turn(MOM, "user", "腰还是不舒服", trip.trip_id)
        results = self.runtime.reconcile_roam(now=self.now + 60)
        self.assertEqual(1, len(results))
        updated = self.runtime.roam.trip(trip.trip_id)
        self.assertEqual(TRIP_INTERRUPTED, updated.status)
        self.assertIn("腰还是不舒服", updated.summary)
        # Idempotent per process and no opening/notice is queued.
        self.assertEqual([], self.runtime.reconcile_roam(now=self.now + 120))
        self.assertIsNone(
            self.db.query_one("SELECT id FROM outbox WHERE kind='roam_message'")
        )

    def test_roam_visit_action_is_owner_only(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("roam_visit", {"target": "妈妈", "purpose": "问她身体"})],
            session_id=TEACHER,
            reply="",
            now=self.now,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual("owner_only", results[0]["error"])
        self.assertIsNone(self.runtime.roam.active_trip())

    def test_roam_visit_action_works_for_owner(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("roam_visit", {"target": "杨老师", "purpose": "问作业"})],
            session_id=OWNER,
            reply="",
            now=self.now,
            trust=TrustLevel.PRIVATE,
        )
        self.assertTrue(results[0]["ok"], results[0])
        trip = self.runtime.roam.active_trip()
        self.assertEqual(TEACHER, trip.thread_session)
        self.assertIsNotNone(
            self.db.query_one("SELECT id FROM outbox WHERE kind='roam_message'")
        )

    def test_unknown_target_action_is_refused(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("roam_visit", {"target": "楼下保安"})],
            session_id=OWNER,
            reply="",
            now=self.now,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(results[0]["ok"])
        self.assertIsNone(self.runtime.roam.active_trip())

    def test_quiet_return_defers_the_notice(self) -> None:
        from fatfish.clock import to_local

        # Start outside the quiet window, then cross into it while visiting.
        start_at = to_local(self.now + 5 * 60, "Asia/Shanghai").strftime("%H:%M")
        end_at = to_local(self.now + 30 * 60, "Asia/Shanghai").strftime("%H:%M")
        self.config._data.setdefault("share", {})["quiet_hours_start"] = start_at
        self.config._data["share"]["quiet_hours_end"] = end_at
        # Keep idle longer than this check so the quiet branch is the reason.
        self.config._data.setdefault("roam", {})["idle_return_minutes"] = 120
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        self._say(MOM, "腰还是不舒服", now=self.now + 600)
        result = self.runtime.maybe_roam_return(now=self.now + 11 * 60)
        self.assertEqual("quiet_hours", result["reason"])
        notice = self.db.query_one("SELECT available_at FROM outbox WHERE kind='roam_notice'")
        self.assertIsNotNone(notice)
        self.assertGreater(float(notice["available_at"]), self.now + 11 * 60)

    def test_tick_processes_return(self) -> None:
        self._say(OWNER, "你去看看妈妈，问她身体怎么样", now=self.now)
        self._say(MOM, "腰还是不舒服", now=self.now + 600)
        ticked = self.runtime.tick(now=self.now + 600 + 20 * 60 + 1)
        self.assertIsNotNone(ticked["roam"])
        self.assertIsNone(self.runtime.roam.active_trip())


if __name__ == "__main__":
    unittest.main()
