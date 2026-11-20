"""Agenda module (T-0018): open loops and draft share candidates in the prompt.

The module renders two sections at the head of the per-turn tail -- after the
cacheable history, before recall evidence -- so she sees her own commitments
and pending shares every round. Red lines pinned here:

* position: strictly before ``[本轮检索证据]``, never in the stable prefix;
* three states: loops only / drafts only / nothing renders at all;
* due-today marking, 80-char clipping, per-section item cap;
* a draft disappears from the prompt the moment it leaves ``draft`` status;
* nightly expiry flips stale open loops to ``expired``;
* zero extra SQL when ``context.agenda_enabled=false`` and exactly one
  statement per store when enabled (no N+1).
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fatfish.config import Config
from fatfish.context import ContextAssembler
from fatfish.memory import MemoryService, RecallResult
from fatfish.nightly import expire_open_loops, finalize_nightly
from tests.test_owner import OWNER_SESSION, make_env


class _QueryCounter:
    """Trace SQL statements executed on the connection inside a with-block."""

    def __init__(self, db):
        self.db = db
        self.statements: list[str] = []

    def __enter__(self) -> "_QueryCounter":
        self.db.conn.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc) -> bool:
        self.db.conn.set_trace_callback(None)
        return False

    def touching(self, table: str) -> int:
        return sum(1 for statement in self.statements if table in statement)


class AgendaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def _section(self, bundle, name: str) -> str:
        return next(s.text for s in bundle.sections if s.name == name)

    def _add_loop(self, text: str, *, due_at: float | None = None, subject: str = "",
                  created_at: float | None = None) -> str:
        loop_id = self.memory.add_open_loop(text, due_at=due_at, subject=subject)
        if created_at is not None:
            self.db.execute(
                "UPDATE open_loops SET created_at=? WHERE id=?", (created_at, loop_id)
            )
        return loop_id

    def _add_draft(self, content: str, *, status: str = "draft") -> int:
        cursor = self.db.execute(
            "INSERT INTO qzone_posts(content, status, created_by, created_at) "
            "VALUES (?, ?, 'llm', ?)",
            (content, status, time.time()),
        )
        return int(cursor.lastrowid)

    # ------------------------------------------------------- rendering 3-state
    def test_open_loops_render_in_tail_before_recall(self) -> None:
        self._add_loop("帮主人哥哥查显卡价格", due_at=time.time() + 60)
        recall = RecallResult(query="显卡", ephemeral_text="检索到的证据原文")
        bundle = self.assembler.build(
            OWNER_SESSION, "在吗", recall=recall, role="owner"
        )
        tail = self._section(bundle, "turn_tail")

        self.assertIn("[待办与约定]", tail)
        self.assertIn("帮主人哥哥查显卡价格", tail)
        self.assertIn("今日到期", tail)
        self.assertIn("[本轮检索证据", tail)
        self.assertLess(
            tail.index("[待办与约定]"),
            tail.index("[本轮检索证据"),
            "agenda must sit before recall evidence in the turn tail",
        )
        # And it never leaks into the cacheable prefix or the dynamic section.
        self.assertNotIn("[待办与约定]", self._section(bundle, "stable_prefix"))
        self.assertNotIn("[待办与约定]", self._section(bundle, "runtime_context"))

    def test_draft_candidates_render_with_id(self) -> None:
        post_id = self._add_draft("今天的日落好美，想去海边看一眼")
        bundle = self.assembler.build(OWNER_SESSION, "写完了", role="owner")
        tail = self._section(bundle, "turn_tail")

        self.assertIn("[待分享候选]", tail)
        self.assertIn(f"(草稿#{post_id})", tail)
        self.assertIn("今天的日落好美", tail)
        self.assertNotIn("[待办与约定]", tail)

    def test_empty_agenda_renders_nothing(self) -> None:
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        text = bundle.text()
        self.assertNotIn("[待办与约定]", text)
        self.assertNotIn("[待分享候选]", text)

    def test_approval_moves_draft_out_of_the_prompt(self) -> None:
        post_id = self._add_draft("一条待审批的说说")
        first = self.assembler.build(OWNER_SESSION, "嗯", role="owner")
        self.assertIn("[待分享候选]", self._section(first, "turn_tail"))

        for status in ("approved", "rejected", "posted"):
            self.db.execute(
                "UPDATE qzone_posts SET status=? WHERE id=?", (status, post_id)
            )
            bundle = self.assembler.build(OWNER_SESSION, "嗯", role="owner")
            self.assertNotIn(
                "[待分享候选]",
                self._section(bundle, "turn_tail"),
                f"status={status} must leave the candidate list",
            )
            self.assertNotIn("一条待审批的说说", self._section(bundle, "turn_tail"))

    def test_close_loop_removes_commitment_from_prompt(self) -> None:
        loop_id = self._add_loop("三天内回姐姐的消息")
        first = self.assembler.build(OWNER_SESSION, "嗯", role="owner")
        self.assertIn("三天内回姐姐的消息", self._section(first, "turn_tail"))
        self.memory.close_open_loop(loop_id)
        second = self.assembler.build(OWNER_SESSION, "嗯", role="owner")
        self.assertNotIn("三天内回姐姐的消息", self._section(second, "turn_tail"))

    # ------------------------------------------------------------ due + limits
    def test_due_labels(self) -> None:
        from fatfish.clock import to_local

        now = time.time()
        tz = self.config.timezone_name
        yesterday = to_local(now - 86400, tz).date()
        later = to_local(now + 3 * 86400, tz).date()
        # 「今天到期」不能被运行时刻左右：`now + 1h` 在 23:00 之后就已经是
        # 明天了（2026-10-29 23:28 实测踩到）。固定到今天的 23:59；已经
        # 过了 23:59 就取今天 00:01（同样属于「今日到期」，只是已过期）。
        today = to_local(now, tz)
        due_today = today.replace(hour=23, minute=59, second=0, microsecond=0)
        if due_today.timestamp() <= now:
            due_today = today.replace(hour=0, minute=1, second=0, microsecond=0)
        self._add_loop("今天的事", due_at=due_today.timestamp())
        self._add_loop("三天后的事", due_at=now + 3 * 86400)
        self._add_loop("昨天就该做的事", due_at=now - 86400)
        self._add_loop("没定期限的事")
        tail = self._section(self.assembler.build(OWNER_SESSION, "嗯", role="owner"), "turn_tail")

        self.assertIn("今天的事（今日到期）", tail)
        self.assertIn(f"三天后的事（{later.month:02d}-{later.day:02d}到期）", tail)
        self.assertIn(f"昨天就该做的事（{yesterday.month:02d}-{yesterday.day:02d}到期）", tail)
        self.assertIn("没定期限的事\n", tail)
        self.assertEqual(tail.count("今日到期"), 1)

    def test_items_clipped_and_capped(self) -> None:
        base = time.time() - 1000
        long_text = "很" * 200
        self._add_loop(long_text, created_at=base)  # oldest: inside the cap, clipped
        for i in range(9):
            self._add_loop(f"约定{i}", created_at=base + 1 + i)
        self._add_draft("很" * 200)
        bundle = self.assembler.build(OWNER_SESSION, "嗯", role="owner")
        tail = self._section(bundle, "turn_tail")
        agenda = tail.split("[用户消息]")[0]
        loops_section = agenda.split("[待分享候选]")[0]

        self.assertEqual(loops_section.count("\n- "), 8, "at most agenda_max_items entries")
        self.assertNotIn(long_text, tail)
        self.assertNotIn("很" * 80, tail, "text must be clipped to agenda_item_chars")
        self.assertIn("很" * 79 + "…", loops_section)

    def test_draft_cap(self) -> None:
        for i in range(10):
            self._add_draft(f"草稿{i}")
        bundle = self.assembler.build(OWNER_SESSION, "嗯", role="owner")
        agenda = self._section(bundle, "turn_tail").split("[用户消息]")[0]
        self.assertEqual(agenda.count("\n- "), 8)

    # ------------------------------------------------------------- visibility
    def test_stranger_gets_no_agenda_and_no_queries(self) -> None:
        self._add_loop("秘密约定")
        self._add_draft("秘密草稿")
        with _QueryCounter(self.db) as counter:
            bundle = self.assembler.build(OWNER_SESSION, "在吗")
        text = bundle.text()
        self.assertNotIn("[待办与约定]", text)
        self.assertNotIn("[待分享候选]", text)
        self.assertNotIn("秘密约定", text)
        self.assertEqual(counter.touching("open_loops"), 0)
        self.assertEqual(counter.touching("qzone_posts"), 0)

    def test_friend_sees_attribution_prefix(self) -> None:
        self._add_loop("主人哥哥的约定", subject="owner")
        bundle = self.assembler.build(OWNER_SESSION, "嗯", role="friend")
        tail = self._section(bundle, "turn_tail")
        self.assertIn("[待办与约定]", tail)
        self.assertIn("[主人哥哥] 主人哥哥的约定", tail)

    # ------------------------------------------------------------ performance
    def test_enabled_runs_exactly_two_statements(self) -> None:
        self._add_loop("一条约定")
        self._add_draft("一条草稿")
        with _QueryCounter(self.db) as counter:
            self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        self.assertEqual(counter.touching("open_loops"), 1)
        self.assertEqual(counter.touching("qzone_posts"), 1)

    def test_disabled_runs_zero_extra_queries(self) -> None:
        data = self.config.as_dict()
        data.setdefault("context", {})["agenda_enabled"] = False
        config = Config(data, profile=self.config.profile, source=self.config.source)
        assembler = ContextAssembler(self.db, config, self.memory)
        self._add_loop("一条约定")
        self._add_draft("一条草稿")
        with _QueryCounter(self.db) as counter:
            bundle = assembler.build(OWNER_SESSION, "在吗", role="owner")
        text = bundle.text()
        self.assertNotIn("[待办与约定]", text)
        self.assertNotIn("[待分享候选]", text)
        self.assertEqual(counter.touching("open_loops"), 0)
        self.assertEqual(counter.touching("qzone_posts"), 0)


class AgendaExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _add_loop(self, text: str, *, created_at: float) -> str:
        loop_id = self.memory.add_open_loop(text)
        self.db.execute(
            "UPDATE open_loops SET created_at=? WHERE id=?", (created_at, loop_id)
        )
        return loop_id

    def test_stale_open_loop_is_expired_and_unrendered(self) -> None:
        now = time.time()
        stale = self._add_loop("两周前许的愿", created_at=now - 20 * 86400)
        fresh = self._add_loop("昨天的约定", created_at=now - 86400)

        expired = expire_open_loops(self.db, self.config, now=now)
        self.assertEqual(expired, 1)
        statuses = {
            row["id"]: row["status"]
            for row in self.db.query("SELECT id, status FROM open_loops")
        }
        self.assertEqual(statuses[stale], "expired")
        self.assertEqual(statuses[fresh], "open")

        assembler = ContextAssembler(self.db, self.config, self.memory)
        tail = next(
            s.text for s in assembler.build(OWNER_SESSION, "嗯", role="owner").sections
            if s.name == "turn_tail"
        )
        self.assertIn("昨天的约定", tail)
        self.assertNotIn("两周前许的愿", tail)

    def test_expiry_is_idempotent_and_dry_run_counts_only(self) -> None:
        now = time.time()
        self._add_loop("很老的约定", created_at=now - 30 * 86400)

        self.assertEqual(expire_open_loops(self.db, self.config, now=now, dry_run=True), 1)
        status = self.db.query_one("SELECT status FROM open_loops LIMIT 1")["status"]
        self.assertEqual(status, "open", "dry run must not write")

        self.assertEqual(expire_open_loops(self.db, self.config, now=now), 1)
        self.assertEqual(expire_open_loops(self.db, self.config, now=now), 0)

    def test_zero_disables_the_sweep(self) -> None:
        data = self.config.as_dict()
        data.setdefault("context", {})["agenda_expire_days"] = 0
        config = Config(data, profile=self.config.profile, source=self.config.source)
        now = time.time()
        self._add_loop("很老的约定", created_at=now - 400 * 86400)
        self.assertEqual(expire_open_loops(self.db, config, now=now), 0)
        self.assertEqual(
            self.db.query_one("SELECT status FROM open_loops LIMIT 1")["status"], "open"
        )

    def test_finalize_nightly_reports_expired_loops(self) -> None:
        now = time.time()
        self._add_loop("很老的约定", created_at=now - 20 * 86400)
        report = {"date": "2026-10-24", "skipped": True, "reason": "no_data"}
        result = finalize_nightly(self.db, self.config, self.memory, [report], now=now)
        self.assertEqual(result["expired_loops"], 1)
        self.assertEqual(
            self.db.query_one("SELECT status FROM open_loops LIMIT 1")["status"],
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
