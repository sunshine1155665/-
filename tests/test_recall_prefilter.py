"""Recall pre-filter wiring: query time windows + entity narrowing (ADR-0003 §4.5.3).

The details are produced through the real ``archive_details`` pipeline (turns
-> event_details + detail_fts) so the search behaves exactly like nightly
archives.  Time anchors are built from the *real* current date (turns on
"yesterday noon" and "40 days ago noon") so the test never depends on a
frozen clock, while ``parse_time_window``'s ``now=time.time()`` still lands
the 昨天 window on the right day.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.clock import from_local, to_local
from fatfish.config import load_config
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.memory.details import archive_details, search_details
from fatfish.memory.entities import extract_from_turns


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / "prefilter.db").as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[voice]
enabled = false

[sticker]
enabled = false

[relationship]
enabled = false

[llm]
enabled = false
"""
    path = tmp / "prefilter.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


class RecallPrefilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.tz = self.config.timezone_name

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _noon(self, days_ago: int) -> float:
        """Noon of the local day ``days_ago`` days before today (real clock)."""
        local_day = to_local(time.time(), self.tz).date() - dt.timedelta(days=days_ago)
        return from_local(dt.datetime.combine(local_day, dt.time(12, 0)), self.tz)

    def _add_turn(self, content: str, when: float) -> int:
        cursor = self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) "
            "VALUES ('s1', 'user', ?, ?)",
            (content, float(when)),
        )
        return int(cursor.lastrowid)

    def _make_detail(self, event_key: str, content: str, when: float) -> str:
        """One turn -> one event -> archived details; returns the event id."""
        turn_id = self._add_turn(content, when)
        event_id = f"ev-{event_key}"
        self.db.execute(
            "INSERT INTO conversation_events(id, session_id, start_at, end_at, status, "
            "title, summary, turn_ids_json, memory_ids_json, topics_json, importance, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, "s1", float(when), float(when), "archived",
                f"事件{event_key}", f"摘要{event_key}", json.dumps([turn_id]),
                "[]", "[]", 0.7, float(when), float(when),
            ),
        )
        date_text = to_local(when, self.tz).date().isoformat()
        session = SimpleNamespace(
            session_id="s1",
            date_text=date_text,
            episode_id=f"ep:{date_text}:s1",
            event_groups=[
                SimpleNamespace(
                    event_ids=[event_id], primary_event_id=event_id, source_id=event_id,
                )
            ],
        )
        result = archive_details(self.db, session)
        self.assertGreaterEqual(result["created"], 1)
        return event_id

    def _turn_row(self, content_fragment: str):
        return self.db.query_one(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE content LIKE ?",
            (f"%{content_fragment}%",),
        )

    def _mention_event(self, event_id: str, content_fragment: str) -> list[str]:
        """Record entity mentions for one event from its matching turn."""
        row = self._turn_row(content_fragment)
        assert row is not None, f"turn with {content_fragment!r} not found"
        return extract_from_turns(
            self.db, [row], "event", event_id
        )

    # ------------------------------------------------------------------ tests
    def test_recall_details_applies_query_time_window(self) -> None:
        self._make_detail("y", "我今天被骂了，好累", self._noon(1))
        self._make_detail("o", "四十天前也被骂了一次", self._noon(40))

        # 「昨天 被骂」: the space keeps the two LIKE tokens separate, and
        # 昨天 parses to exactly the local day of the first detail.
        rows = self.memory.recall_details("昨天 被骂", limit=4)
        self.assertTrue(rows)
        self.assertTrue(any("我今天被骂了" in row["text"] for row in rows))
        self.assertFalse(any("四十天前" in row["text"] for row in rows))

        # The full recall() path passes the parsed window to details too.
        result = self.memory.recall("昨天 被骂")
        texts = [row["text"] for row in result.details]
        self.assertTrue(any("我今天被骂了" in text for text in texts))
        self.assertFalse(any("四十天前" in text for text in texts))

    def test_recall_details_applies_entity_prefilter(self) -> None:
        when = self._noon(3)
        event_a = self._make_detail("a", "主人哥哥说他被骂了", when)
        self._make_detail("b", "那天又被骂了一顿", when)
        names = self._mention_event(event_a, "主人哥哥")
        self.assertIn("主人哥哥", names)

        # The query mentions 主人哥哥, so only event A (which has the
        # event-level mention) should come back; event B never mentioned it.
        rows = self.memory.recall_details("主人哥哥 被骂", limit=4)
        self.assertTrue(rows)
        self.assertTrue(any("主人哥哥说他被骂了" in row["text"] for row in rows))
        self.assertFalse(any("那天又被骂了一顿" in row["text"] for row in rows))

        # Without an entity word the narrowing does not apply: both survive.
        both = self.memory.recall_details("被骂", limit=8)
        texts = [row["text"] for row in both]
        self.assertTrue(any("主人哥哥" in text for text in texts))
        self.assertTrue(any("那天又被骂了一顿" in text for text in texts))

    def test_recall_details_falls_back_when_entity_filter_empties(self) -> None:
        # Event A owns the owner-entity mention, but its detail text matches
        # neither query token; event B matches 被骂 without any mention.
        when = self._noon(2)
        event_a = self._make_detail("a", "我出门了一趟", when)
        self._make_detail("b", "那天又被骂了一顿", when)
        self._mention_event(event_a, "我出门")

        rows = self.memory.recall_details("主人哥哥 被骂", limit=4)
        self.assertTrue(rows)
        self.assertTrue(any("那天又被骂了一顿" in row["text"] for row in rows))
        self.assertFalse(any("我出门了" in row["text"] for row in rows))

    def test_search_details_with_empty_event_ids_returns_empty(self) -> None:
        self._make_detail("a", "我今天被骂了，好累", self._noon(1))
        self.assertEqual([], search_details(self.db, "被骂", event_ids=[]))
        # None keeps the old behaviour: the row is still there.
        self.assertTrue(search_details(self.db, "被骂"))

    def test_search_details_without_event_ids_is_unchanged(self) -> None:
        self._make_detail("a", "我今天被骂了，好累", self._noon(1))
        self._make_detail("b", "四十天前也被骂了一次", self._noon(40))

        rows = search_details(self.db, "被骂")
        self.assertEqual(2, len(rows))
        texts = {row["text"] for row in rows}
        self.assertTrue(any("我今天被骂了" in text for text in texts))
        self.assertTrue(any("四十天前" in text for text in texts))

        # The time filter still narrows the unfiltered search.
        local_day = to_local(time.time(), self.tz).date() - dt.timedelta(days=1)
        window_start = from_local(
            dt.datetime.combine(local_day, dt.time.min), self.tz
        )
        recent = search_details(self.db, "被骂", time_start=window_start)
        self.assertEqual(1, len(recent))
        self.assertIn("我今天被骂了", recent[0]["text"])

        # Rows carry the event context columns the prompt needs.
        for row in rows:
            self.assertIn("event_title", row)
            self.assertIn("occurred_at", row)

    def test_natural_sentence_query_hits_detail(self) -> None:
        # T-0010 acceptance: a natural sentence (no spaces, CJK-only) used to
        # tokenise as one unsplittable run and recall nothing; bigram
        # matching must reach the archived clause through recall_details.
        self._make_detail("y", "我今天被骂了，好累，一点都不想动了", self._noon(1))
        rows = self.memory.recall_details("那天她说被骂了，后来我们聊了什么", limit=4)
        self.assertTrue(rows)
        self.assertTrue(any("我今天被骂了" in row["text"] for row in rows))

    def test_detail_min_score_wired_from_config(self) -> None:
        self._make_detail("w", "我今天被骂了，好累", self._noon(0))
        # 今天天气怎么样 parses to today's window; the detail is in-window
        # but shares only 今天, so the default gate (memory.detail_min_score
        # = 0.2) drops it.
        self.assertEqual([], self.memory.recall_details("今天天气怎么样"))
        # Lowering the config knob restores the weak candidate.
        self.config._data["memory"]["detail_min_score"] = 0.0
        rows = self.memory.recall_details("今天天气怎么样")
        self.assertTrue(any("我今天被骂了" in row["text"] for row in rows))


if __name__ == "__main__":
    unittest.main()
