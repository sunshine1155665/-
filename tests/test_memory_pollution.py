"""Read-path isolation of test/night-test/demo memories.

The rows stay in the DB (no destructive cleanup without owner approval); these
tests only pin that production recall never surfaces them.  Night-shift
activity keeps its own conversation_turns path.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fatfish.memory import MemoryService
from fatfish.memory.details import search_details
from fatfish.memory.sessions import is_test_bvid, is_test_session
from fatfish.runtime import Runtime
from tests.test_owner import make_env


class TestSessionClassifierTests(unittest.TestCase):
    def test_patterns(self) -> None:
        for value in (
            "qq:private:verify-a6c58214",
            "web:devcheck",
            "night-20261025-w02",
            "test-session",
            "live-tts25-live",
            "smoke-run",
            "demo-activity",
        ):
            self.assertTrue(is_test_session(value), value)
        for value in ("qq:private:9999000001", "s1", "eval", "demo", "activity-20261025-2018"):
            self.assertFalse(is_test_session(value), value)
        self.assertTrue(is_test_bvid("BV_TEST"))
        self.assertTrue(is_test_bvid("BV_DEMO"))
        self.assertFalse(is_test_bvid("BV166nW6REyR"))


class PollutionRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        self.memory = MemoryService(self.db, self.config)

    def _add_detail(self, detail_id: str, session_id: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO event_details(id, event_id, session_id, seq, kind, text, "
            "quote_text, occurred_at, importance, detail_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                detail_id,
                f"ev-{detail_id}",
                session_id,
                0,
                "fact",
                text,
                text,
                time.time(),
                0.7,
                f"hash-{detail_id}",
                time.time(),
            ),
        )

    def test_details_drop_night_and_verify_sessions(self) -> None:
        self._add_detail("d-real", "s1", "我今天被骂了，好累")
        self._add_detail("d-night", "night-20261025-w02", "夜测里也被骂了")
        self._add_detail("d-verify", "qq:private:verify-x", "验证时被骂了")
        rows = search_details(self.db, "被骂", min_score=0)
        ids = {row["id"] for row in rows}
        self.assertIn("d-real", ids)
        self.assertNotIn("d-night", ids)
        self.assertNotIn("d-verify", ids)

    def test_memory_search_drops_test_sessions_and_test_bvids(self) -> None:
        self.memory.add_item(
            "主人哥哥喜欢喝冰美式", session_id="s1", source_type="model_action"
        )
        self.memory.add_item(
            "夜测里主人哥哥喜欢喝冰美式",
            session_id="night-20261025-w02",
            source_type="model_action",
        )
        self.memory.add_item(
            "面试准备视频",
            source_type="bilibili",
            source_id="BV_TEST",
        )
        hits = self.memory.search("冰美式", limit=10)
        texts = [hit.item.gist for hit in hits]
        self.assertTrue(any("喜欢喝冰美式" in text for text in texts))
        self.assertFalse(any("夜测" in text for text in texts))
        self.assertFalse(any("面试准备" in text for text in texts))
        bili_hits = self.memory.search("面试准备", limit=10)
        self.assertFalse(
            any(hit.item.source_id == "BV_TEST" for hit in bili_hits)
        )

    def test_rag_drops_test_bvids(self) -> None:
        self.memory.add_rag_chunk(
            "bilibili",
            "BV_TEST",
            "面试准备：提前查公司",
            metadata={"title": "面试准备", "source": "bilibili"},
        )
        self.memory.add_rag_chunk(
            "bilibili",
            "BV_REAL",
            "真实 B 站视频：大肥鱼适合做女朋友吗",
            metadata={"title": "大肥鱼适合做女朋友吗", "source": "bilibili"},
        )
        hits = self.memory.search_rag("大肥鱼 面试", limit=10)
        ids = {hit.source_id for hit in hits}
        self.assertIn("BV_REAL", ids)
        self.assertNotIn("BV_TEST", ids)


class PollutionPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        self.runtime = Runtime(self.db, self.config)

    def test_bilibili_stock_skips_test_bvids(self) -> None:
        self.runtime.bilibili.ingest_video(
            bvid="BV_TEST",
            title="面试准备",
            up="某UP",
            subtitle="测试视频",
        )
        self.runtime.bilibili.ingest_video(
            bvid="BV_REAL",
            title="大肥鱼适合做女朋友吗？",
            up="学姐圆",
            subtitle="女友不适合，适合当老婆",
        )
        text = "\n".join(self.runtime._bili_stock_evidence())
        self.assertIn("大肥鱼适合做女朋友吗", text)
        self.assertNotIn("面试准备", text)


if __name__ == "__main__":
    unittest.main()
