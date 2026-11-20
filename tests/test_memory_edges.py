"""entity_edges 写入与 1 跳关联补充召回的边界条件（ADR-0003 §4 召回表第 6 行）。

设计基准（ActionRAG 的最便宜版）：
- 索引侧：一条 detail 里共同出现的实体两两写 ``entity_edges``（relation=co_mention，
  evidence_detail_id 指回 detail），同时写 detail 级 ``entity_mentions`` 作反查索引；
- 召回侧：直接命中的 detail 经其实体走 1 跳边，把邻居实体的 detail 作为
  ``via_graph`` 补充带出（不是独立命中），预算、置信度、时间窗、隐私闸门全部收口。

边界原则沿用本项目惯例：宁漏记不错归（低置信边不进召回）、原文不可变
（mention/edge 只增不改）、测试数据与真实数据隔离、非主人观众看不到别人的原话。
"""

from __future__ import annotations

import contextlib
import gc
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish import cli
from fatfish.config import Config
from fatfish.memory import MemoryService
from fatfish.memory.edges import (
    CO_MENTION_RELATION,
    GRAPH_MIN_CONFIDENCE,
    backfill_edges,
    expand_entity_neighbors,
    list_edges,
    match_detail_entities,
    record_detail_mentions,
    sync_session_edges,
    write_edges_for_details,
)
from fatfish.memory.entities import (
    DIRECT_CONFIDENCE,
    FRIEND_ENTITY_ID,
    GROUP_ATTRIBUTION_CONFIDENCE,
    OWNER_ENTITY_ID,
    SELF_ENTITY_ID,
    create_person,
    ensure_known_entities,
    merge_entities,
)
from fatfish.db import Database
from tests.test_owner import make_env

T = 86400.0


def add_event(
    db: Database, event_id: str, title: str, when: float, *, session_id: str = "s1"
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO conversation_events(id, session_id, start_at, end_at, "
        "status, title, summary, turn_ids_json, memory_ids_json, topics_json, "
        "importance, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            session_id,
            when,
            when,
            "archived",
            title,
            "",
            "[]",
            "[]",
            "[]",
            0.6,
            when,
            when,
        ),
    )


def add_detail(
    db: Database,
    *,
    detail_id: str,
    event_id: str,
    text: str,
    when: float,
    seq: int = 0,
    quote: str = "",
    session_id: str = "s1",
    importance: float = 0.6,
) -> None:
    db.execute(
        "INSERT INTO event_details(id, event_id, session_id, seq, kind, subject, "
        "text, quote_text, quote_turn_id, occurred_at, importance, detail_hash, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            detail_id,
            event_id,
            session_id,
            seq,
            "fact",
            "",
            text,
            quote or text,
            seq + 1,
            when,
            importance,
            f"hash-{detail_id}",
            when,
        ),
    )


def detail_row(db: Database, detail_id: str) -> dict:
    row = db.query_one("SELECT * FROM event_details WHERE id=?", (detail_id,))
    assert row is not None, detail_id
    return dict(row)


def graph_ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("via_graph")}


def _entity_id_by_name(db: Database, name: str) -> str:
    row = db.query_one("SELECT id FROM entities WHERE canonical_name=?", (name,))
    assert row is not None, name
    return str(row["id"])


class MemoryEdgeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(self.tmp)
        self.addCleanup(self.db.close)
        self.now = time.time()
        ensure_known_entities(self.db, now=self.now)

    def _memory_with(self, **memory_overrides) -> MemoryService:
        data = self.config.as_dict()
        data.setdefault("memory", {}).update(memory_overrides)
        return MemoryService(self.db, Config(data, profile="test"))

    def _seed_pair_edge(self, *, session_id: str = "s1", when: float | None = None):
        """主人哥哥 + 姐姐 同现的桥 detail，产生 owner—friend 边与双方 mention。"""
        when = self.now - 3600 if when is None else when
        add_event(self.db, "ev-bridge", "安慰那晚", when, session_id=session_id)
        add_detail(
            self.db,
            detail_id="d-bridge",
            event_id="ev-bridge",
            text="姐姐安慰了主人哥哥，劝他想开点。",
            when=when,
            session_id=session_id,
        )
        rows = [detail_row(self.db, "d-bridge")]
        record_detail_mentions(self.db, rows, now=self.now)
        return write_edges_for_details(self.db, rows, now=self.now)

    def _seed_hit(self, *, session_id: str = "s1", detail_id: str = "d-hit") -> None:
        """「被骂」锚点 detail：查询「主人哥哥被骂了」唯一直接命中。"""
        add_event(self.db, "ev-hit", "被骂那晚", self.now - T, session_id=session_id)
        add_detail(
            self.db, detail_id=detail_id, event_id="ev-hit",
            text="主人哥哥今天被骂了，好累。", when=self.now - T,
            session_id=session_id,
        )


class MatchDetailEntitiesTests(MemoryEdgeTestBase):
    def test_owner_friend_matched_longest_surface_first(self) -> None:
        hits = match_detail_entities(self.db, "主人哥哥和姐姐一起吃了饭", now=self.now)
        by_entity = {entity_id: surface for entity_id, surface, _conf in hits}
        self.assertEqual(by_entity.get(OWNER_ENTITY_ID), "主人哥哥")
        self.assertEqual(by_entity.get(FRIEND_ENTITY_ID), "姐姐")
        owner_rows = [h for h in hits if h[0] == OWNER_ENTITY_ID]
        self.assertEqual(len(owner_rows), 1, "最长 surface 先占位，不得拆成 主人+哥哥")

    def test_prefix_guard_blocks_xiaojiejie(self) -> None:
        hits = match_detail_entities(self.db, "小姐姐来过一趟", now=self.now)
        self.assertNotIn(FRIEND_ENTITY_ID, {h[0] for h in hits})

    def test_group_scope_lowers_attribution_confidence(self) -> None:
        hits = match_detail_entities(
            self.db, "主人哥哥和鲸鲸都在群里", scope="group", now=self.now
        )
        conf = {entity_id: confidence for entity_id, _s, confidence in hits}
        self.assertEqual(conf[OWNER_ENTITY_ID], GROUP_ATTRIBUTION_CONFIDENCE)
        self.assertEqual(conf[SELF_ENTITY_ID], DIRECT_CONFIDENCE)

    def test_card_matched_by_canonical_and_alias(self) -> None:
        create_person(self.db, name="小蓝", aliases=["蓝蓝"], now=self.now)
        hits = match_detail_entities(self.db, "蓝蓝和小蓝一起来的", now=self.now)
        card_ids = {h[0] for h in hits if h[0].startswith("person:")}
        self.assertEqual(card_ids, {_entity_id_by_name(self.db, "小蓝")})
        self.assertEqual({h[1] for h in hits if h[0].startswith("person:")}, {"小蓝", "蓝蓝"})

    def test_single_char_alias_is_skipped(self) -> None:
        create_person(self.db, name="阿蓝", aliases=["蓝"], now=self.now)
        hits = match_detail_entities(self.db, "蓝今天来了", now=self.now)
        self.assertFalse([h for h in hits if h[0].startswith("person:nick")])

    def test_card_prefix_guard(self) -> None:
        create_person(self.db, name="蓝姐姐", now=self.now)
        guarded = match_detail_entities(self.db, "小蓝姐姐来了", now=self.now)
        self.assertFalse([h for h in guarded if h[1] == "蓝姐姐"])
        matched = match_detail_entities(self.db, "蓝姐姐来了", now=self.now)
        self.assertTrue([h for h in matched if h[1] == "蓝姐姐"])

    def test_named_surfaces_match_inside_quotes(self) -> None:
        hits = match_detail_entities(self.db, "她说「姐姐最好了」", now=self.now)
        self.assertIn(FRIEND_ENTITY_ID, {h[0] for h in hits})

    def test_empty_text_returns_empty(self) -> None:
        self.assertEqual(match_detail_entities(self.db, "", now=self.now), [])
        self.assertEqual(match_detail_entities(self.db, "   ", now=self.now), [])


class DetailMentionTests(MemoryEdgeTestBase):
    def test_mentions_are_idempotent(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d1", event_id="ev1",
                   text="主人哥哥和姐姐聊了很久", when=self.now)
        rows = [detail_row(self.db, "d1")]
        first = record_detail_mentions(self.db, rows, now=self.now)
        second = record_detail_mentions(self.db, rows, now=self.now)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)
        count = self.db.query_one(
            "SELECT count(*) AS n FROM entity_mentions WHERE source_type='detail' "
            "AND source_id='d1'"
        )
        self.assertEqual(count["n"], 2)

    def test_quote_only_mention_has_no_char_span(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d1", event_id="ev1",
                   text="她安慰了好久", quote="「姐姐最好了」", when=self.now)
        record_detail_mentions(self.db, [detail_row(self.db, "d1")], now=self.now)
        row = self.db.query_one(
            "SELECT * FROM entity_mentions WHERE source_type='detail' AND source_id='d1'"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["entity_id"], FRIEND_ENTITY_ID)
        self.assertIsNone(row["char_start"])
        self.assertIsNone(row["char_end"])


class EdgeWriteTests(MemoryEdgeTestBase):
    def test_co_mention_edge_canonical_order_and_confidence(self) -> None:
        written = self._seed_pair_edge()
        self.assertEqual(written, 1)
        edges = list_edges(self.db)
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["relation"], CO_MENTION_RELATION)
        self.assertEqual(
            {edge["src_id"], edge["dst_id"]}, {OWNER_ENTITY_ID, FRIEND_ENTITY_ID}
        )
        self.assertLess(edge["src_id"], edge["dst_id"], "方向按 id 排序，防止 A-B/B-A 双写")
        self.assertAlmostEqual(edge["confidence"], DIRECT_CONFIDENCE)
        self.assertEqual(edge["evidence_detail_id"], "d-bridge")

    def test_no_self_edge_from_two_surfaces_of_same_entity(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d1", event_id="ev1",
                   text="主人哥哥和哥哥一起出门了", when=self.now)
        rows = [detail_row(self.db, "d1")]
        record_detail_mentions(self.db, rows, now=self.now)
        self.assertEqual(write_edges_for_details(self.db, rows, now=self.now), 0)

    def test_one_or_zero_entities_writes_no_edge(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d-only", event_id="ev1",
                   text="主人哥哥一个人加班", when=self.now)
        add_detail(self.db, detail_id="d-none", event_id="ev1",
                   text="天气很热", when=self.now)
        rows = [detail_row(self.db, "d-only"), detail_row(self.db, "d-none")]
        record_detail_mentions(self.db, rows, now=self.now)
        self.assertEqual(write_edges_for_details(self.db, rows, now=self.now), 0)
        self.assertEqual(list_edges(self.db), [])

    def test_same_pair_in_two_details_keeps_both_evidence_rows(self) -> None:
        self._seed_pair_edge()
        when = self.now - 2 * T
        add_event(self.db, "ev2", "散步那晚", when)
        add_detail(self.db, detail_id="d2", event_id="ev2",
                   text="姐姐陪主人哥哥散步", when=when)
        rows = [detail_row(self.db, "d2")]
        record_detail_mentions(self.db, rows, now=self.now)
        write_edges_for_details(self.db, rows, now=self.now)
        self.assertEqual(len(list_edges(self.db)), 2)
        # 重跑同一批 detail：按 (src,dst,relation,evidence) 幂等，一条都不多。
        again = write_edges_for_details(
            self.db,
            [detail_row(self.db, "d-bridge"), detail_row(self.db, "d2")],
            now=self.now,
        )
        self.assertEqual(again, 0)
        self.assertEqual(len(list_edges(self.db)), 2)

    def test_group_scope_edge_gets_low_confidence(self) -> None:
        self._seed_pair_edge(session_id="qq:group:123")
        edge = list_edges(self.db)[0]
        self.assertAlmostEqual(edge["confidence"], GROUP_ATTRIBUTION_CONFIDENCE)

    def test_list_edges_joins_canonical_names(self) -> None:
        self._seed_pair_edge()
        edge = list_edges(self.db)[0]
        self.assertEqual({edge["src_name"], edge["dst_name"]}, {"主人哥哥", "姐姐"})


class BackfillEdgesTests(MemoryEdgeTestBase):
    def test_backfill_idempotent_and_skips_test_sessions(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d1", event_id="ev1",
                   text="主人哥哥和姐姐吃饭", when=self.now)
        add_event(self.db, "ev2", "测试事件", self.now, session_id="qq:private:verify-x")
        add_detail(self.db, detail_id="d-test", event_id="ev2",
                   text="主人哥哥和姐姐测试", when=self.now,
                   session_id="qq:private:verify-x")
        first = backfill_edges(self.db, now=self.now)
        self.assertEqual(first["edges"], 1)
        self.assertEqual(first["skipped_test"], 1)
        self.assertGreaterEqual(first["mentions"], 2)
        second = backfill_edges(self.db, now=self.now)
        self.assertEqual(second["edges"], 0)
        self.assertEqual(second["mentions"], 0)
        self.assertEqual(second["skipped_test"], 1)
        self.assertEqual(len(list_edges(self.db)), 1)


class SyncSessionEdgesTests(MemoryEdgeTestBase):
    def test_sync_session_edges_idempotent_and_skips_test_session(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d1", event_id="ev1",
                   text="主人哥哥和姐姐吃饭", when=self.now)
        first = sync_session_edges(
            self.db, SimpleNamespace(session_id="s1"), now=self.now
        )
        self.assertEqual(first["edges"], 1)
        second = sync_session_edges(
            self.db, SimpleNamespace(session_id="s1"), now=self.now
        )
        self.assertEqual(second["edges"], 0)
        skipped = sync_session_edges(
            self.db, SimpleNamespace(session_id="qq:private:verify-x"), now=self.now
        )
        self.assertEqual(skipped["details"], 0)


class ExpandEntityNeighborsTests(MemoryEdgeTestBase):
    """走 MemoryService.recall_details 的端到端边界。"""

    def setUp(self) -> None:
        super().setUp()
        self.memory = MemoryService(self.db, self.config)

    def _seed_scene(
        self,
        *,
        neighbor_session: str = "s1",
        neighbor_when: float | None = None,
    ) -> None:
        """D1(主人哥哥被骂) 是唯一直接命中；D2(姐姐约海边) 与查询零词面重叠。"""
        self._seed_pair_edge()
        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        when = self.now - 2 * T if neighbor_when is None else neighbor_when
        add_event(self.db, "ev-sea", "海边", when, session_id=neighbor_session)
        add_detail(
            self.db, detail_id="d-sea", event_id="ev-sea",
            text="姐姐约了朋友去海边散心。", when=when,
            session_id=neighbor_session,
        )
        record_detail_mentions(self.db, [detail_row(self.db, "d-sea")], now=self.now)

    def test_neighbor_detail_rides_in_via_graph(self) -> None:
        self._seed_scene()
        rows = self.memory.recall_details("主人哥哥被骂了")
        direct_ids = {row["id"] for row in rows if not row.get("via_graph")}
        self.assertIn("d-hit", direct_ids)
        self.assertNotIn("d-sea", direct_ids, "零词面重叠的 detail 不得走直接命中")
        self.assertIn("d-sea", graph_ids(rows))
        # 桥 detail（边证据本身）也该被带出：它正是「后来呢」的答案。
        self.assertIn("d-bridge", graph_ids(rows))
        sea = next(row for row in rows if row["id"] == "d-sea")
        self.assertIn("主人哥哥", sea["graph_via"])
        self.assertIn("姐姐", sea["graph_via"])
        for row in rows:
            if row.get("via_graph"):
                self.assertFalse(row.get("context"), "graph 补充不复用同事件标记")

    def test_no_edges_means_no_graph_rows(self) -> None:
        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        rows = self.memory.recall_details("主人哥哥被骂了")
        self.assertTrue(rows)
        self.assertEqual(graph_ids(rows), set())

    def test_time_window_filters_neighbor_details(self) -> None:
        self._seed_scene(neighbor_when=self.now - 90 * T)
        rows = self.memory.recall_details(
            "主人哥哥被骂了", time_start=self.now - T, time_end=self.now
        )
        self.assertNotIn("d-sea", graph_ids(rows), "时间窗外的邻居 detail 不得带出")
        self.assertIn("d-hit", {row["id"] for row in rows})

    def test_test_session_neighbor_excluded(self) -> None:
        self._seed_scene(neighbor_session="qq:private:verify-x")
        rows = self.memory.recall_details("主人哥哥被骂了")
        self.assertNotIn("d-sea", graph_ids(rows) | {row["id"] for row in rows})

    def test_per_neighbor_cap_and_importance_ordering(self) -> None:
        self._seed_pair_edge()
        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        for suffix, importance in (
            ("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6),
        ):
            add_event(self.db, f"ev-{suffix}", f"事件{suffix}", self.now - 3 * T)
            add_detail(
                self.db, detail_id=f"d-n{suffix}", event_id=f"ev-{suffix}",
                text=f"姐姐在准备{suffix}号计划。", when=self.now - 3 * T,
                importance=importance,
            )
            record_detail_mentions(
                self.db, [detail_row(self.db, f"d-n{suffix}")], now=self.now
            )
        rows = self.memory.recall_details("主人哥哥被骂了")
        # per_neighbor=2：同一邻居实体只带重要度最高的两条。
        self.assertEqual(graph_ids(rows), {"d-na", "d-nb"})

    def test_total_budget_cap_via_config(self) -> None:
        self._seed_pair_edge()
        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        add_event(self.db, "ev-a", "事件a", self.now - 3 * T)
        add_detail(self.db, detail_id="d-na", event_id="ev-a",
                   text="姐姐在准备a号计划。", when=self.now - 3 * T, importance=0.9)
        add_event(self.db, "ev-b", "事件b", self.now - 3 * T)
        add_detail(self.db, detail_id="d-nb", event_id="ev-b",
                   text="姐姐在准备b号计划。", when=self.now - 3 * T, importance=0.8)
        for detail_id in ("d-na", "d-nb"):
            record_detail_mentions(
                self.db, [detail_row(self.db, detail_id)], now=self.now
            )
        memory = self._memory_with(graph_expand_max=1)
        rows = memory.recall_details("主人哥哥被骂了")
        self.assertEqual(
            [row["id"] for row in rows if row.get("via_graph")], ["d-na"]
        )

    def test_neighbor_already_direct_hit_is_not_duplicated(self) -> None:
        self._seed_pair_edge()
        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        add_event(self.db, "ev2", "海边", self.now - 2 * T)
        add_detail(self.db, detail_id="d-both", event_id="ev2",
                   text="姐姐听说了被骂的事，约主人哥哥去海边。", when=self.now - 2 * T)
        record_detail_mentions(self.db, [detail_row(self.db, "d-both")], now=self.now)
        rows = self.memory.recall_details("主人哥哥被骂了")
        hits = [row["id"] for row in rows if row["id"] == "d-both"]
        self.assertEqual(len(hits), 1, "已在直接命中里的 detail 不得经图重复带出")
        self.assertEqual(graph_ids(rows), set(), "锚点实体彼此相连时无新邻居，不产生图补充")

    def test_low_confidence_edge_stays_out_by_default(self) -> None:
        # 群聊同现 → 边置信度 0.4 < 默认阈值 0.5，宁漏记不错归。
        self._seed_pair_edge(session_id="qq:group:123")
        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        add_event(self.db, "ev-sea", "海边", self.now - 2 * T)
        add_detail(self.db, detail_id="d-sea", event_id="ev-sea",
                   text="姐姐约了朋友去海边散心。", when=self.now - 2 * T)
        record_detail_mentions(self.db, [detail_row(self.db, "d-sea")], now=self.now)
        rows = self.memory.recall_details("主人哥哥被骂了")
        self.assertEqual(graph_ids(rows), set())
        self.assertGreater(GRAPH_MIN_CONFIDENCE, GROUP_ATTRIBUTION_CONFIDENCE)

    def test_graph_expand_disabled_by_config(self) -> None:
        self._seed_scene()
        rows = self._memory_with(graph_expand=0).recall_details("主人哥哥被骂了")
        self.assertTrue(rows)
        self.assertEqual(graph_ids(rows), set())

    def test_merged_edge_still_reaches_neighbor_details(self) -> None:
        """实体合并后，旧 id 上的边与 mention 仍可达（家族折叠）。"""
        self._seed_pair_edge()
        create_person(self.db, name="小蓝", now=self.now)
        card_id = _entity_id_by_name(self.db, "小蓝")
        add_event(self.db, "ev-hotpot", "火锅", self.now - 2 * T)
        add_detail(self.db, detail_id="d-hotpot", event_id="ev-hotpot",
                   text="小蓝和主人哥哥一起吃了火锅。", when=self.now - 2 * T)
        add_event(self.db, "ev-treat", "请客", self.now - 3 * T)
        add_detail(self.db, detail_id="d-treat", event_id="ev-treat",
                   text="小蓝说下周请客。", when=self.now - 3 * T)
        record_detail_mentions(
            self.db,
            [detail_row(self.db, "d-hotpot"), detail_row(self.db, "d-treat")],
            now=self.now,
        )
        # 索引侧同一批写：detail 级 mention + co_mention 边（真实走 backfill/nightly）。
        write_edges_for_details(self.db, [detail_row(self.db, "d-hotpot")], now=self.now)
        # 建新卡并把小蓝合进去：旧 id 变墓碑。
        create_person(self.db, name="阿蓝", uin="123456789", now=self.now)
        merge_entities(self.db, card_id, _entity_id_by_name(self.db, "阿蓝"), now=self.now)

        self._seed_hit()
        record_detail_mentions(self.db, [detail_row(self.db, "d-hit")], now=self.now)
        rows = self.memory.recall_details("主人哥哥被骂了")
        ids = graph_ids(rows)
        self.assertIn("d-hotpot", ids, "合并前的边经家族折叠仍可达")
        self.assertIn("d-treat", ids, "旧 id 上的 detail mention 仍可达")
        via = next(
            row for row in rows
            if row.get("via_graph") and row["id"] == "d-hotpot"
        )["graph_via"]
        self.assertIn("阿蓝", via, "graph_via 用合并后存活卡的 canonical 名")

    def test_anchor_without_mentions_falls_back_to_text_match(self) -> None:
        """真实库回填前（无 detail 级 mention），锚点实体按文本兜底识别。"""
        self._seed_pair_edge()
        self._seed_hit()  # 故意不给 d-hit 写 mention
        rows = self.memory.recall_details("主人哥哥被骂了")
        self.assertIn("d-bridge", graph_ids(rows))

    def test_viewer_gate_applies_to_graph_rows(self) -> None:
        """非主人观众只看自己 session 的行，图补充同样过闸（T-0073）。"""
        self._seed_scene(neighbor_session="s-other")
        # 给观众自己的 session 一份锚点（_seed_scene 的 d-hit 在 s1，会被闸掉）。
        self._seed_hit(session_id="qq:private:888", detail_id="d-hit-viewer")
        record_detail_mentions(
            self.db, [detail_row(self.db, "d-hit-viewer")], now=self.now
        )
        rows = self.memory.recall_details(
            "主人哥哥被骂了", viewer_session="qq:private:888", viewer_role="friend"
        )
        self.assertIn("d-hit-viewer", {row["id"] for row in rows})
        self.assertNotIn("d-sea", graph_ids(rows) | {row["id"] for row in rows},
                         "别人 session 的邻居 detail 不得外流")


class FormatRecallGraphTests(MemoryEdgeTestBase):
    def test_graph_rows_render_under_own_label(self) -> None:
        base = {
            "occurred_at": self.now - T,
            "event_title": "事件",
            "quote_text": "",
            "context": False,
            "via_graph": False,
        }
        direct = {**base, "id": "d1", "text": "直接命中的一条", "graph_via": ""}
        graph = {
            **base,
            "id": "d2",
            "text": "图带出的一条",
            "quote_text": "原话B",
            "via_graph": True,
            "graph_via": "主人哥哥 ↔ 姐姐",
        }
        result = MemoryService(self.db, self.config).format_recall(
            "被骂", [], [], details=[direct, graph], include_quotes=True
        )
        text = result.ephemeral_text
        self.assertIn("[关联补充", text)
        self.assertIn("图带出的一条", text)
        self.assertIn("原话B", text)
        self.assertIn("主人哥哥 ↔ 姐姐", text)
        # 直接命中在细节召回段，图补充在关联段，互不混排。
        self.assertLess(text.index("直接命中的一条"), text.index("[关联补充"))
        self.assertGreater(text.index("图带出的一条"), text.index("[关联补充"))
        self.assertIn("detail:d2", result.refs)

    def test_graph_row_without_via_label_does_not_crash(self) -> None:
        row = {
            "id": "d2", "text": "缺标签的一条", "occurred_at": self.now,
            "event_title": "", "quote_text": "", "context": False,
            "via_graph": True, "graph_via": "",
        }
        result = MemoryService(self.db, self.config).format_recall(
            "随便", [], [], details=[row]
        )
        self.assertIn("缺标签的一条", result.ephemeral_text)


class CliEdgesTests(MemoryEdgeTestBase):
    def setUp(self) -> None:
        super().setUp()
        # CLI 经 _bootstrap 另开连接，toml 指到与 make_env 相同的库。
        data = self.tmp / "data"
        self.cli_toml = self.tmp / "edges-cli.toml"
        self.cli_toml.write_text(
            f'[app]\ndata_dir = "{data.as_posix()}"\n\n'
            f'[storage]\ndb_path = "{(data / "owner.db").as_posix()}"\n\n'
            '[memory]\nembedding_backend = "hash"\nembedding_dim = 64\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        # CLI 命令经 _bootstrap 打开的 Database 不会主动关闭（产品行为），
        # Windows 上文件句柄要等 GC 才释放（同 test_cli_people 的处理）。
        gc.collect()

    def _main(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--config", str(self.cli_toml), *argv])
        return code, buffer.getvalue()

    def test_edges_cli_lists_edges(self) -> None:
        self._seed_pair_edge()
        self.db.close()  # Windows：CLI 的 _bootstrap 要再开一次库
        code, out = self._main("memory", "edges")
        self.assertEqual(code, 0)
        self.assertIn("主人哥哥", out)
        self.assertIn("姐姐", out)
        self.assertIn(CO_MENTION_RELATION, out)

    def test_backfill_cli_includes_edges(self) -> None:
        add_event(self.db, "ev1", "事件", self.now)
        add_detail(self.db, detail_id="d1", event_id="ev1",
                   text="主人哥哥和姐姐吃饭", when=self.now)
        self.db.close()
        code, out = self._main("memory", "backfill")
        self.assertEqual(code, 0)
        payload = json.loads(out[out.index("{"):])
        self.assertIn("edges", payload)
        self.assertGreaterEqual(payload["edges"]["edges"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
