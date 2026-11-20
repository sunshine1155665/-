from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.db import Database
from fatfish.memory.details import (
    _match_tokens,
    archive_details,
    extract_details,
    search_details,
)


def make_db(tmp: Path) -> Database:
    db = Database(tmp / "details.db")
    db.initialize()
    return db


class DetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-details-")
        self.tmp = Path(self._tmp.name)
        self.db = make_db(self.tmp)
        self.now = time.time()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _add_turn(self, content: str, *, role: str = "user", when: float | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) "
            "VALUES ('s1', ?, ?, ?)",
            (role, content, self.now if when is None else when),
        )
        return int(cursor.lastrowid)

    def _add_event(self, event_id: str, turn_ids: list[int]) -> None:
        self.db.execute(
            "INSERT INTO conversation_events(id, session_id, start_at, end_at, status, "
            "title, summary, turn_ids_json, memory_ids_json, topics_json, importance, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "s1", self.now, self.now, "archived", "被骂那晚",
             "主人被骂后很累。", json.dumps(turn_ids), "[]", "[]", 0.7,
             self.now, self.now),
        )

    def _session(self, session_id: str = "s1") -> SimpleNamespace:
        return SimpleNamespace(
            session_id=session_id,
            date_text="2026-10-24",
            episode_id=f"ep:2026-10-24:{session_id}",
            event_groups=[
                SimpleNamespace(
                    event_ids=["ev1"],
                    primary_event_id="ev1",
                    source_id="ev1",
                )
            ],
        )

    def test_extract_keeps_exact_quote_and_kind(self) -> None:
        content = "我今天被骂了，好累，一点都不想动了。明天想早点睡。"
        turns = [{"id": 1, "role": "user", "content": content, "created_at": self.now}]
        details = extract_details("ev1", turns, session_id="s1", date_text="2026-10-24")
        self.assertGreaterEqual(len(details), 2)
        for detail in details:
            self.assertIn(detail["quote_text"], content)
            self.assertEqual(detail["quote_turn_id"], 1)
        self.assertTrue(any(d["kind"] == "emotion" for d in details))
        self.assertTrue(any(d["kind"] == "plan" for d in details))

    def test_archive_is_idempotent_and_searchable(self) -> None:
        turn_id = self._add_turn("我今天被骂了，好累。")
        self._add_event("ev1", [turn_id])
        session = self._session()
        first = archive_details(self.db, session, now=self.now)
        self.assertGreater(first["created"], 0)
        second = archive_details(self.db, session, now=self.now)
        self.assertEqual(second["created"], 0)
        rows = search_details(self.db, "被骂")
        self.assertTrue(rows)
        self.assertIn("被骂", rows[0]["quote_text"])
        self.assertEqual(rows[0]["event_title"], "被骂那晚")

    def test_test_sessions_are_skipped(self) -> None:
        turn_id = self._add_turn("背景第0句")
        self._add_event("ev1", [turn_id])
        result = archive_details(
            self.db, self._session("qq:private:verify-a6c58214"), now=self.now
        )
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], "test_session")
        count = self.db.query_one("SELECT count(*) AS n FROM event_details")
        self.assertEqual(count["n"], 0)

    def test_match_tokens_rules(self) -> None:
        # A mixed run: the CJK stretch becomes bigrams, ASCII stays whole.
        self.assertEqual(["昨天", "ab12"], _match_tokens("昨天ab12"))
        # A CJK run of n chars yields n-1 adjacent bigrams.
        self.assertEqual(["被骂", "骂了"], _match_tokens("被骂了"))
        # Single-character runs stand alone; ASCII runs stay whole.
        self.assertEqual(["好"], _match_tokens("好"))
        self.assertEqual(["ab12"], _match_tokens("ab12"))
        # Duplicates collapse, first-occurrence order wins.
        self.assertEqual(["被骂", "ab12"], _match_tokens("被骂 ab12 被骂 ab12"))
        # No word characters at all -> no tokens.
        self.assertEqual([], _match_tokens("！？。，"))

    def test_search_hits_natural_sentence_via_bigrams(self) -> None:
        # T-0010 headline regression: the whole sentence used to be one
        # \w+ run and one unicode61 token, so neither FTS MATCH nor the
        # whole-run LIKE fallback could find the stored clause.
        turn_id = self._add_turn("我今天被骂了，好累，一点都不想动了。")
        self._add_event("ev1", [turn_id])
        archive_details(self.db, self._session(), now=self.now)
        rows = search_details(self.db, "那天她说被骂了，后来我们聊了什么")
        self.assertTrue(rows)
        self.assertIn("我今天被骂了", rows[0]["text"])
        self.assertEqual(rows[0]["event_title"], "被骂那晚")

    def test_search_gate_drops_single_incidental_token(self) -> None:
        turn_id = self._add_turn("我今天被骂了，好累。")
        self._add_event("ev1", [turn_id])
        archive_details(self.db, self._session(), now=self.now)
        # Only 今天 is shared with the query: 1 of 6 query bigrams is below
        # the 0.2 default gate, and one shared token never passes alone.
        self.assertEqual([], search_details(self.db, "今天天气怎么样"))
        # The knob: min_score=0 keeps every candidate again.
        rows = search_details(self.db, "今天天气怎么样", min_score=0)
        self.assertTrue(rows)
        self.assertIn("我今天被骂了", rows[0]["text"])


if __name__ == "__main__":
    unittest.main()
