"""Offline tests for T-0073 memory isolation (owner decision 2026-10-30).

The rule the owner set:

* only 主人哥哥 may learn what other people told 鲸鱼娘 in private;
* non-owner speakers may lean on the owner's non-confidential daily life
  (black-list: 作息 / secret notes / other people's conversations stay sealed);
* the owner keeps the old, unrestricted recall view.

Every database here is a temp file; the real ``data/fatfish.db`` is untouched.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.context import ContextAssembler, PERSONA_VERSION, ensure_default_persona  # noqa: E402
from fatfish.config import load_config  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.memory.service import (  # noqa: E402
    SCHEDULE_SECRET_RE,
    MemoryService,
)
from fatfish.owner import OwnerRegistry  # noqa: E402
from fatfish.privacy import SecretNotes  # noqa: E402

OWNER_SID = "qq:private:10001"
FRIEND_SID = "qq:private:10002"
STRANGER_SID = "qq:private:10003"


class IsolationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "t.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.config = load_config("low")
        self.memory = MemoryService(self.db, self.config)
        self.owner = OwnerRegistry(self.db, self.config)
        self.owner.bind(OWNER_SID, display_name="主人哥哥", source="test")
        self.owner.bind(FRIEND_SID, display_name="姐姐", source="test", role="friend")

    def _seed(self) -> dict[str, str]:
        """Four rows whose visibility the tests pin."""
        own = self.memory.add_item(
            "姐姐说她周末想去海边。",
            session_id=FRIEND_SID,
            type="episodic",
        )
        owner_open = self.memory.add_item(
            "主人喜欢喝美式咖啡。",
            session_id=OWNER_SID,
            type="preference",
        )
        owner_secret = self.memory.add_item(
            "主人每天十二点睡觉，早上八点起床。",
            session_id=OWNER_SID,
            type="episodic",
        )
        stranger = self.memory.add_item(
            "陌生人说他收集邮票。",
            session_id=STRANGER_SID,
            type="episodic",
        )
        return {
            "own": own.id,
            "owner_open": owner_open.id,
            "owner_secret": owner_secret.id,
            "stranger": stranger.id,
        }

    # -- write side --------------------------------------------------------
    def test_schedule_pattern_flags_the_blacklist(self) -> None:
        self.assertIsNotNone(SCHEDULE_SECRET_RE.search("十二点睡觉"))
        self.assertIsNotNone(SCHEDULE_SECRET_RE.search("早上八点起床"))
        self.assertIsNotNone(SCHEDULE_SECRET_RE.search("他现在在哪"))
        self.assertIsNone(SCHEDULE_SECRET_RE.search("主人喜欢喝美式咖啡"))
        self.assertIsNone(SCHEDULE_SECRET_RE.search("主人上周打游戏赢了"))

    def test_owner_rows_default_open_unless_schedule(self) -> None:
        self.assertEqual(self.memory.audience_for("主人喜欢喝美式", OWNER_SID), "open")
        self.assertEqual(
            self.memory.audience_for("主人十二点睡觉", OWNER_SID), "owner"
        )
        # 口径修正（主人 15:00）：只有主人哥哥的作息被封——鲸鱼娘自己的
        # 作息照常共享，督促别人睡觉也不会被误伤。
        self.assertEqual(
            self.memory.audience_for("鲸鲸雷打不动十一点半就钻被窝", OWNER_SID),
            "open",
        )
        self.assertEqual(
            self.memory.audience_for("本鲸鱼娘白天要打个盹", OWNER_SID), "open"
        )
        # 非主人起源 / 无会话：一律 fail-closed
        self.assertEqual(self.memory.audience_for("随便什么", FRIEND_SID), "owner")
        self.assertEqual(self.memory.audience_for("随便什么", None), "owner")

    def test_add_item_derives_audience(self) -> None:
        ids = self._seed()
        audiences = {}
        for key, item_id in ids.items():
            row = self.db.query_one(
                "SELECT audience FROM memory_items WHERE id=?", (item_id,)
            )
            audiences[key] = row["audience"]
        self.assertEqual(audiences["own"], "owner")
        self.assertEqual(audiences["owner_open"], "open")
        self.assertEqual(audiences["owner_secret"], "owner")
        self.assertEqual(audiences["stranger"], "owner")

    def test_add_item_rejects_unknown_audience(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.add_item("x", session_id=OWNER_SID, audience="everyone")

    # -- read side ---------------------------------------------------------
    def _hit_ids(self, **kwargs) -> set[str]:
        return {h.item.id for h in self.memory.search("他说了什么", limit=20, **kwargs)}

    def test_owner_sees_everything(self) -> None:
        ids = self._seed()
        seen = self._hit_ids(viewer_session=OWNER_SID, viewer_role="owner")
        self.assertTrue(set(ids.values()) <= seen)

    def test_friend_sees_own_and_owner_open_only(self) -> None:
        ids = self._seed()
        seen = self._hit_ids(viewer_session=FRIEND_SID, viewer_role="friend")
        self.assertIn(ids["own"], seen)
        self.assertIn(ids["owner_open"], seen)
        self.assertNotIn(ids["owner_secret"], seen)
        self.assertNotIn(ids["stranger"], seen)

    def test_unbound_stranger_is_gated_the_same_way(self) -> None:
        ids = self._seed()
        seen = self._hit_ids(viewer_session=STRANGER_SID, viewer_role=None)
        self.assertIn(ids["stranger"], seen)
        self.assertIn(ids["owner_open"], seen)
        self.assertNotIn(ids["owner_secret"], seen)
        self.assertNotIn(ids["own"], seen)

    def test_no_viewer_means_ungated_console_view(self) -> None:
        ids = self._seed()
        seen = self._hit_ids()
        self.assertTrue(set(ids.values()) <= seen)

    def test_rag_inherits_the_source_memory_visibility(self) -> None:
        ids = self._seed()
        # a chunk cut from the owner-secret item must not reach a friend
        self.memory.add_rag_chunk(
            "memory", ids["owner_secret"], "主人每天十二点睡觉"
        )
        self.memory.add_rag_chunk("bilibili", "BV1xx", "鲸鲸今天看了视频")
        friend_hits = self.memory.search_rag(
            "睡觉", limit=5, viewer_session=FRIEND_SID, viewer_role="friend"
        )
        self.assertTrue(all(h.source_id != ids["owner_secret"] for h in friend_hits))
        owner_hits = self.memory.search_rag(
            "睡觉", limit=5, viewer_session=OWNER_SID, viewer_role="owner"
        )
        self.assertTrue(any(h.source_id == ids["owner_secret"] for h in owner_hits))

    def test_details_never_cross_sessions_for_non_owner(self) -> None:
        now = 1_700_000_000.0
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO conversation_events(id, session_id, start_at, status,"
                " title, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                ("ev-owner", OWNER_SID, now, "closed", "主人的对话", now, now),
            )
            conn.execute(
                "INSERT INTO event_details(id, event_id, episode_id, session_id, seq,"
                " kind, text, detail_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("det-owner", "ev-owner", None, OWNER_SID, 1, "fact",
                 "主人说他十二点睡觉", "h1", now),
            )
        rows = self.memory.recall_details(
            "睡觉",
            viewer_session=FRIEND_SID,
            viewer_role="friend",
        )
        self.assertEqual(rows, [])
        rows = self.memory.recall_details(
            "睡觉", viewer_session=OWNER_SID, viewer_role="owner"
        )
        self.assertTrue(any(r["id"] == "det-owner" for r in rows))

    # -- roles -------------------------------------------------------------
    def test_multi_holder_roles_coexist(self) -> None:
        self.owner.bind(
            "qq:private:20001", display_name="妈妈", source="test", role="family"
        )
        self.owner.bind(
            "qq:private:20002", display_name="杨老师", source="test", role="teacher"
        )
        self.owner.bind(
            "qq:private:20003", display_name="藤老师", source="test", role="teacher"
        )
        self.assertEqual(self.owner.role_of("qq:private:20001"), "family")
        self.assertEqual(self.owner.role_of("qq:private:20002"), "teacher")
        self.assertEqual(self.owner.role_of("qq:private:20003"), "teacher")
        # rebinding one teacher must not evict the other
        self.owner.bind(
            "qq:private:20002", display_name="杨老师", source="test", role="teacher"
        )
        self.assertEqual(self.owner.role_of("qq:private:20003"), "teacher")
        # unbind by session, not by role
        self.assertTrue(
            self.owner.unbind("teacher", session_id="qq:private:20002")
        )
        self.assertIsNone(self.owner.role_of("qq:private:20002"))
        self.assertEqual(self.owner.role_of("qq:private:20003"), "teacher")

    def test_role_never_collapses_into_owner(self) -> None:
        self.owner.bind(
            "qq:private:20001", display_name="妈妈", source="test", role="family"
        )
        assembler = ContextAssembler(self.db, self.config, self.memory)
        self.assertEqual(assembler._resolve_role("qq:private:20001", None, None), "family")
        # a bound row with an unknown role string stays non-owner
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE owner_bindings SET role='wizard' WHERE session_id='qq:private:20001'"
            )
        self.assertIsNone(assembler._resolve_role("qq:private:20001", None, None))

    def test_secret_block_frames_family_like_friend(self) -> None:
        SecretNotes(self.db).add("哥哥的期末成绩")
        assembler = ContextAssembler(self.db, self.config, self.memory)
        for role in ("friend", "family", "teacher"):
            block = assembler._secret_block(role)
            self.assertIn("绝对不要", block)
        self.assertIn("不能对姐姐说", assembler._secret_block("owner"))
        self.assertEqual(assembler._secret_block(None), "")

    # -- persona v8 --------------------------------------------------------
    def test_persona_v8_carries_the_isolation_rules(self) -> None:
        ensure_default_persona(self.db)
        row = self.db.query_one("SELECT version, core_json FROM persona WHERE id='default'")
        self.assertEqual(int(row["version"]), PERSONA_VERSION)
        self.assertIn("别的朋友", row["core_json"])
        self.assertIn("非涉密日常", row["core_json"])


if __name__ == "__main__":
    unittest.main()
