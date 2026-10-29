"""Schema v13 sender attribution + inbound person cards (T-0057)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fatfish.config import Config, load_config
from fatfish.context import ensure_default_persona
from fatfish.db import SCHEMA_VERSION, Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.memory import MemoryService
from fatfish.platforms.qq import QQAdapter
from fatfish.runtime import Runtime
from fatfish.voice import VoiceDecision

OWNER_SESSION = "qq:private:10001"
GROUP_SESSION = "qq:group:5555"


def make_config(tmp: Path, *, autocreate: str = "private") -> Config:
    data = tmp / "data"
    return Config(
        {
            "app": {"data_dir": str(data), "timezone": "Asia/Shanghai"},
            "storage": {"db_path": str(data / "sender.db")},
            "memory": {
                "embedding_backend": "hash",
                "embedding_dim": 64,
                "person_autocreate": autocreate,
            },
            "relationship": {
                "owner_display_name": "主人哥哥",
                "friend_display_name": "姐姐",
            },
            "voice": {"enabled": False},
            "tts": {"enabled": False},
            "sticker": {"enabled": False},
            "qq": {"catchup_enabled": False},
            "security": {"kernel_path": "fatfish/kernel.json"},
        },
        profile="low",
    )


def make_runtime(tmp: Path, *, autocreate: str = "private") -> tuple[Runtime, Database]:
    config = make_config(tmp, autocreate=autocreate)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    runtime = Runtime(db, config)
    runtime.llm.api_key = "fake"
    runtime.llm.chat = lambda messages, **kwargs: LLMResult(
        text='{"messages":["好呀"],"actions":[]}',
        usage=LLMUsage(prompt_tokens=5, completion_tokens=2),
        model="fake-model",
    )
    runtime.voice.decide = lambda *args, **kwargs: VoiceDecision(
        "text_only", 0.1, ["test"]
    )
    return runtime, db


def event(
    *,
    message_type: str = "private",
    user_id: int = 10001,
    text: str = "你好",
    card: str = "",
    nickname: str = "",
    message_id: int = 1,
    group_id: int = 5555,
    mention: bool = True,
) -> dict:
    sender: dict = {}
    if card:
        sender["card"] = card
    if nickname:
        sender["nickname"] = nickname
    segments: list[dict] = []
    if message_type == "group" and mention:
        segments.append({"type": "at", "data": {"qq": 999}})
    segments.append({"type": "text", "data": {"text": text}})
    payload = {
        "post_type": "message",
        "message_type": message_type,
        "self_id": 999,
        "user_id": user_id,
        "message_id": message_id,
        "message": segments,
    }
    if message_type == "group":
        payload["group_id"] = group_id
    if sender:
        payload["sender"] = sender
    return payload


_PRE_V13_TURNS_TABLE = """CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    used_refs_json TEXT NOT NULL DEFAULT '[]',
    ephemeral INTEGER NOT NULL DEFAULT 0,
    event_id TEXT,
    episode_id TEXT,
    archived_at REAL
);"""


def _prev13_schema_sql(sql: str) -> str:
    """Rewrite schema.sql into the v12 shape (sender columns absent)."""
    import re

    return re.sub(
        r"CREATE TABLE IF NOT EXISTS conversation_turns \(.*?\n\);",
        _PRE_V13_TURNS_TABLE,
        sql,
        count=1,
        flags=re.DOTALL,
    )


class SchemaV13Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-sender-")
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "schema.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fresh_database_has_sender_columns_and_index(self) -> None:
        db = Database(self.path)
        db.initialize()
        try:
            columns = db.columns("conversation_turns")
            self.assertIn("sender_id", columns)
            self.assertIn("sender_name", columns)
            index = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_turn_session_sender'"
            )
            self.assertIsNotNone(index)
            self.assertEqual(SCHEMA_VERSION, db.get_meta("schema_version"))
        finally:
            db.close()

    def test_v12_database_migrates_to_v13_idempotently(self) -> None:
        # Build a genuine pre-v13 database: schema.sql with the sender columns
        # and their index removed.  (ALTER TABLE DROP COLUMN on the live table
        # trips over the column comments SQLite stores in the CREATE text.)
        import sqlite3

        from fatfish.db import SCHEMA_PATH

        sql = _prev13_schema_sql(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn = sqlite3.connect(str(self.path))
        try:
            conn.executescript(sql)
            conn.execute("UPDATE schema_meta SET value='12' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()

        db = Database(self.path)
        try:
            self.assertNotIn("sender_id", db.columns("conversation_turns"))
            result = db.migrate()
            self.assertIn("conversation_turns.sender_id", result["applied"])
            self.assertIn("conversation_turns.sender_name", result["applied"])
            self.assertEqual(SCHEMA_VERSION, result["schema_version"])
            again = db.migrate()
            self.assertEqual([], again["applied"])
            columns = db.columns("conversation_turns")
            self.assertIn("sender_id", columns)
            self.assertIn("sender_name", columns)
            index = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_turn_session_sender'"
            )
            self.assertIsNotNone(index)
        finally:
            db.close()

    def test_v12_database_opens_through_ensure_schema(self) -> None:
        # The CLI and runtime enter through ensure_schema(), which runs the
        # full schema.sql script BEFORE migrate().  The sender index must not
        # appear in that script, or old databases fail to open at all
        # (regression: "no such column: sender_id" on the real v12 library).
        import sqlite3

        from fatfish.db import SCHEMA_PATH

        sql = _prev13_schema_sql(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn = sqlite3.connect(str(self.path))
        try:
            conn.executescript(sql)
            conn.execute("UPDATE schema_meta SET value='12' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()

        db = Database(self.path)
        try:
            self.assertTrue(db.ensure_schema())
            self.assertFalse(db.ensure_schema())
            columns = db.columns("conversation_turns")
            self.assertIn("sender_id", columns)
            self.assertIn("sender_name", columns)
            index = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_turn_session_sender'"
            )
            self.assertIsNotNone(index)
            self.assertEqual(SCHEMA_VERSION, db.get_meta("schema_version"))
        finally:
            db.close()


class AddTurnSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-addturn-")
        self.tmp = Path(self._tmp.name)
        config = make_config(self.tmp)
        config.ensure_dirs()
        self.db = Database(config.db_path)
        self.db.initialize()
        self.memory = MemoryService(self.db, config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_user_turn_stores_sender(self) -> None:
        turn_id = self.memory.add_turn(
            GROUP_SESSION,
            "user",
            "今天吃什么",
            sender_id="10001",
            sender_name="小明",
        )
        row = self.db.query_one(
            "SELECT sender_id, sender_name FROM conversation_turns WHERE id=?",
            (turn_id,),
        )
        self.assertEqual("10001", row["sender_id"])
        self.assertEqual("小明", row["sender_name"])

    def test_assistant_turn_leaves_sender_empty(self) -> None:
        turn_id = self.memory.add_turn(GROUP_SESSION, "assistant", "吃鱼")
        row = self.db.query_one(
            "SELECT sender_id, sender_name FROM conversation_turns WHERE id=?",
            (turn_id,),
        )
        self.assertIsNone(row["sender_id"])
        self.assertEqual("", row["sender_name"])

    def test_sender_name_is_capped(self) -> None:
        turn_id = self.memory.add_turn(
            GROUP_SESSION, "user", "hi", sender_id="1", sender_name="名" * 100
        )
        row = self.db.query_one(
            "SELECT sender_name FROM conversation_turns WHERE id=?", (turn_id,)
        )
        self.assertEqual(64, len(row["sender_name"]))


class QqSenderParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = QQAdapter(
            SimpleNamespace(), {"qq": {"catchup_enabled": False}}
        )

    def test_group_card_wins_over_nickname(self) -> None:
        message = self.adapter.parse_event(
            event(message_type="group", card="群名片", nickname="账号昵称")
        )
        self.assertIsNotNone(message)
        self.assertEqual("群名片", message.sender_name)

    def test_private_nickname_is_used(self) -> None:
        message = self.adapter.parse_event(event(nickname="账号昵称"))
        self.assertIsNotNone(message)
        self.assertEqual("账号昵称", message.sender_name)

    def test_missing_sender_block_yields_empty_name(self) -> None:
        message = self.adapter.parse_event(event())
        self.assertIsNotNone(message)
        self.assertEqual("", message.sender_name)

    def test_handle_event_forwards_sender_identity(self) -> None:
        captured: list[dict] = []

        def fake_turn_and_deliver(**kwargs):
            captured.append(kwargs)
            return {"ok": True}

        self.adapter._turn_and_deliver = fake_turn_and_deliver
        self.adapter.handle_event(
            event(message_type="group", user_id=10001, card="群名片")
        )
        self.assertEqual(1, len(captured))
        self.assertEqual("10001", captured[0]["sender_id"])
        self.assertEqual("群名片", captured[0]["sender_name"])


class RuntimeSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-runtime-people-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, *, autocreate: str = "private") -> tuple[Runtime, Database]:
        runtime, db = make_runtime(self.tmp, autocreate=autocreate)
        # Registered after the temp-dir cleanup, so LIFO closes the DB first.
        self.addCleanup(db.close)
        return runtime, db
    def test_private_turn_records_sender_and_creates_a_card(self) -> None:
        runtime, db = self._runtime()
        runtime.handle_user_message(
            OWNER_SESSION,
            "在吗",
            sender_id="10001",
            sender_name="小明",
        )
        turn = db.query_one(
            "SELECT sender_id, sender_name FROM conversation_turns "
            "WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (OWNER_SESSION,),
        )
        self.assertEqual("10001", turn["sender_id"])
        self.assertEqual("小明", turn["sender_name"])
        card = db.query_one(
            "SELECT canonical_name FROM entities WHERE id='person:qq:10001'"
        )
        self.assertIsNotNone(card)
        self.assertEqual("小明", card["canonical_name"])

    def test_group_turn_records_sender_but_skips_the_card_by_default(self) -> None:
        runtime, db = self._runtime()
        runtime.handle_user_message(
            GROUP_SESSION,
            "@她 今天吃什么",
            is_group=True,
            sender_id="20002",
            sender_name="小红",
        )
        turn = db.query_one(
            "SELECT sender_id, sender_name FROM conversation_turns "
            "WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (GROUP_SESSION,),
        )
        self.assertEqual("20002", turn["sender_id"])
        self.assertEqual("小红", turn["sender_name"])
        self.assertIsNone(
            db.query_one("SELECT id FROM entities WHERE id='person:qq:20002'")
        )

    def test_group_turn_creates_a_card_when_autocreate_is_all(self) -> None:
        runtime, db = self._runtime(autocreate="all")
        runtime.handle_user_message(
            GROUP_SESSION,
            "@她 今天吃什么",
            is_group=True,
            sender_id="20002",
            sender_name="小红",
        )
        self.assertIsNotNone(
            db.query_one("SELECT id FROM entities WHERE id='person:qq:20002'")
        )

    def test_off_keeps_turn_attribution_without_a_card(self) -> None:
        runtime, db = self._runtime(autocreate="off")
        runtime.handle_user_message(
            OWNER_SESSION, "在吗", sender_id="10001", sender_name="小明"
        )
        turn = db.query_one(
            "SELECT sender_id FROM conversation_turns WHERE session_id=? "
            "AND role='user' ORDER BY id DESC LIMIT 1",
            (OWNER_SESSION,),
        )
        self.assertEqual("10001", turn["sender_id"])
        self.assertIsNone(
            db.query_one("SELECT id FROM entities WHERE id='person:qq:10001'")
        )

    def test_ephemeral_turn_creates_no_card(self) -> None:
        runtime, db = self._runtime()
        runtime.handle_user_message(
            OWNER_SESSION,
            "（内部提示）",
            sender_id="10001",
            sender_name="小明",
            ephemeral_input=True,
        )
        self.assertIsNone(
            db.query_one("SELECT id FROM entities WHERE id='person:qq:10001'")
        )

    def test_card_failure_never_breaks_the_turn(self) -> None:
        runtime, db = self._runtime()
        with mock.patch(
            "fatfish.memory.entities.note_inbound_person",
            side_effect=RuntimeError("boom"),
        ):
            result = runtime.handle_user_message(
                OWNER_SESSION, "在吗", sender_id="10001", sender_name="小明"
            )
        self.assertTrue(result.get("reply"))
        turn = db.query_one(
            "SELECT sender_id FROM conversation_turns WHERE session_id=? "
            "AND role='user' ORDER BY id DESC LIMIT 1",
            (OWNER_SESSION,),
        )
        self.assertEqual("10001", turn["sender_id"])

    def test_no_sender_kwargs_keeps_old_behaviour(self) -> None:
        runtime, db = self._runtime()
        runtime.handle_user_message(OWNER_SESSION, "在吗")
        turn = db.query_one(
            "SELECT sender_id FROM conversation_turns WHERE session_id=? "
            "AND role='user' ORDER BY id DESC LIMIT 1",
            (OWNER_SESSION,),
        )
        self.assertIsNone(turn["sender_id"])
        self.assertIsNone(
            db.query_one("SELECT id FROM entities WHERE id='person:qq:10001'")
        )


if __name__ == "__main__":
    unittest.main()
