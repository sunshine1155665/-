"""T-0074: ``watch_video`` -- watch ONE bilibili video on demand.

The cloud box has no video server: subtitles + comments + snapshot *metadata*
are the whole material. ``depth=full`` (the local video_server hook, backlog
T-0076/T-0078) must refuse honestly instead of pretending she watched.

Layers covered (one class per concern):

* ``PolicyTests``          -- private-only, one per turn, no required_text
  (bvid/query is an either-or the runtime validates, not the spec).
* ``RegistrationTests``    -- ALLOWED_ACTIONS / SELF_ACTION_TOOLS / EXEMPT_TOOLS
  / TOOL_SCHEMA / parse_turn_plan all know the new action.
* ``ExplicitTests``        -- owner phrasings exempt the desire gate;
  recollections and negations do not.
* ``ArgValidationTests``   -- depth shape, full refusal, bvid/query either-or,
  invalid bvid, bvid wins when both are given.
* ``QuerySelectionTests``  -- search(5) -> ingested filter -> duration cap ->
  hot_score pick; all-filtered is ``no_candidate``; the cid from video_cid is
  reused for subtitles/snapshot (one view lookup per candidate).
* ``DepthTests``           -- meta = view only; text = +subtitles+comments;
  visual = text + snapshot *metadata* (no image is ever downloaded).
* ``BudgetTests``          -- daily watch_max_per_day (meta watermark written
  before the fetch, so a crashed pull still spends the slot), per-session
  cooldown, and the per-query watched_no_share counter after two consecutive
  "no subtitles and no comments" watches.
* ``EvidenceTests``        -- self_action_evidence row + finding card metadata
  (watch_depth / transcript_source / related_from) + decision_log rows.
* ``GateTests``            -- the desire gate refuses her own initiative and an
  explicit owner ask steps over it; a runtime budget (watched_no_share) stands
  even when the owner explicitly asks.

Offline only: temp database, fake Bilibili client, no network, no media
downloads, no NapCat.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import fatfish.runtime as runtime_module
from fatfish.bilibili import BilibiliError
from fatfish.config import load_config
from fatfish.context import TOOL_SCHEMA, ensure_default_persona
from fatfish.db import Database
from fatfish.runtime import (
    ALLOWED_ACTIONS,
    Runtime,
    TurnAction,
    parse_turn_plan,
)
from fatfish.security import PolicyEngine, TrustLevel
from fatfish.security.policy import ACTION_SPECS
from fatfish.self_action.explicit import EXEMPT_TOOLS, explicit_request
from fatfish.self_action.service import SELF_ACTION_TOOLS

NOW = 1_772_000_000.0
PRIVATE_SESSION = "qq:private:1001"
MINUTE = 60.0

# All bvids are BV + 10 chars so they pass the runtime's shape check.
BV_WATCHED = "BV1ingested1"  # pre-ingested via the collector in dedupe tests
BV_HOT = "BV1hotvid0001"  # hot but too long (duration over the cap)
BV_FRESH = "BV1fresh0001"
BV_META = "BV1meta00001"
BV_DRY = "BV1dryvid001"
BV_CHILL = "BV1chill0001"
BV_SOURCE = "BV1source001"


def make_runtime(
    tmp: Path,
    *,
    name: str = "watch_video",
    bili_enabled: bool = True,
    watch_max_per_day: int = 3,
    watch_max_duration_seconds: int = 1800,
    watch_cooldown_minutes: int = 30,
    initial_threshold: float = 0.0,
    max_events_per_turn: int = 10,
) -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / f"{name}.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        # The desire gate sits in front of every self-action; pinned wide open
        # unless a test is explicitly about the gate.
        "[self_action]",
        "enabled = true",
        f"initial_threshold = {initial_threshold}",
        "threshold_floor = 0.0",
        "threshold_step = 0.01",
        "daily_cap = 100",
        f"max_events_per_turn = {max_events_per_turn}",
        "quiet_hours = []",
        "[bilibili]",
        f"enabled = {'true' if bili_enabled else 'false'}",
        f"watch_max_per_day = {watch_max_per_day}",
        f"watch_max_duration_seconds = {watch_max_duration_seconds}",
        f"watch_cooldown_minutes = {watch_cooldown_minutes}",
    ]
    path = tmp / f"{name}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


class FakeBiliClient:
    """Duck-typed BilibiliClient: canned reads, recorded calls, no network.

    ``search`` answers from ``search_index`` (falling back to
    ``search_results``); every read records a call row so tests can assert
    exactly which endpoints ran -- including the ``cid`` reuse the player
    endpoints are supposed to benefit from.
    """

    def __init__(
        self,
        *,
        search_results: list[dict] | None = None,
        subtitles: dict[str, dict] | None = None,
        comments: dict[str, list[str]] | None = None,
        snapshots: dict[str, dict] | None = None,
        dead_bvids: tuple[str, ...] = (),
        search_raises: bool = False,
    ):
        self.search_results = list(search_results or [])
        self.search_index: dict[str, list[dict]] = {}
        self.view_data: dict[str, dict] = {}
        self.durations: dict[str, int] = {}
        self.cids: dict[str, int] = {}
        self.subtitles = dict(subtitles or {})
        self.comments = dict(comments or {})
        self.snapshots = dict(snapshots or {})
        self.dead_bvids = set(dead_bvids)
        self.search_raises = search_raises
        self.calls: list[dict] = []

    def add_video(
        self,
        bvid: str,
        title: str,
        *,
        up: str = "阿鱼",
        desc: str = "一个视频的简介",
        hot: float = 0.5,
        duration: int = 600,
        cid: int = 424242,
        in_search: bool = True,
    ) -> dict:
        self.view_data[bvid] = {
            "bvid": bvid,
            "title": title,
            "up": up,
            "desc": desc,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "published_at": None,
            "topics": [],
            "hot_score": hot,
        }
        self.durations[bvid] = duration
        self.cids[bvid] = cid
        normalized = dict(self.view_data[bvid])
        if in_search:
            self.search_results.append(normalized)
        return normalized

    def search(self, keyword: str, limit: int = 20):
        self.calls.append({"kind": "search", "keyword": keyword, "limit": limit})
        if self.search_raises:
            raise BilibiliError("search is down")
        pool = self.search_index.get(keyword, self.search_results)
        return list(pool)[:limit]

    def view(self, bvid: str) -> dict:
        self.calls.append({"kind": "view", "bvid": bvid})
        if bvid not in self.view_data:
            raise BilibiliError(f"no such video: {bvid}")
        return dict(self.view_data[bvid])

    def video_cid(self, bvid: str, page: int = 1) -> tuple[int, list[dict]]:
        self.calls.append({"kind": "video_cid", "bvid": bvid})
        if bvid in self.dead_bvids or bvid not in self.cids:
            raise BilibiliError(f"no such video: {bvid}")
        return self.cids[bvid], [
            {"page": 1, "cid": self.cids[bvid], "duration": self.durations.get(bvid, 0)}
        ]

    def video_subtitles(self, bvid: str, page: int = 1, cid: int | None = None) -> dict:
        self.calls.append({"kind": "subtitles", "bvid": bvid, "cid": cid})
        result = self.subtitles.get(bvid)
        if result is None:
            return {"lines": [], "source": "none", "lang": ""}
        return result

    def video_comments(self, bvid: str, limit: int | None = None) -> list[str]:
        self.calls.append({"kind": "comments", "bvid": bvid, "limit": limit})
        return list(self.comments.get(bvid) or [])

    def video_snapshot(
        self, bvid: str, page: int = 1, cid: int | None = None
    ) -> dict | None:
        self.calls.append({"kind": "snapshot", "bvid": bvid, "cid": cid})
        return self.snapshots.get(bvid)


class PolicyTests(unittest.TestCase):
    """The spec pins trust/budget only; bvid/query is runtime's job."""

    def setUp(self) -> None:
        self.engine = PolicyEngine()

    def test_private_without_args_is_allowed_at_policy_level(self) -> None:
        # No required_text on purpose: "she names what to watch" is an
        # either-or (bvid OR query), which only the runtime branch can judge.
        decision = self.engine.validate("watch_video", {}, trust=TrustLevel.PRIVATE)
        self.assertTrue(decision.allowed, decision.reason)

    def test_spec_shape(self) -> None:
        spec = ACTION_SPECS["watch_video"]
        self.assertIs(spec.min_trust, TrustLevel.PRIVATE)
        self.assertEqual(spec.max_calls, 1)
        self.assertFalse(spec.allow_group)
        self.assertFalse(spec.write)
        self.assertIsNone(spec.required_text)

    def test_group_trust_is_denied(self) -> None:
        decision = self.engine.validate(
            "watch_video", {"query": "猫"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "group_not_allowed")

    def test_one_call_per_turn(self) -> None:
        decision = self.engine.validate(
            "watch_video", {"query": "猫"}, trust=TrustLevel.PRIVATE, count=1
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "too_many_tool_calls")


class RegistrationTests(unittest.TestCase):
    def test_allowed_actions_contains_watch_video(self) -> None:
        self.assertIn("watch_video", ALLOWED_ACTIONS)

    def test_self_action_and_exempt_tools_contain_watch_video(self) -> None:
        self.assertIn("watch_video", SELF_ACTION_TOOLS)
        # The rule set and the tool set must never drift apart: a self-action
        # without explicit rules can never be exempted, silently. T-0082:
        # talk_to joins the exempt set without being a self-action (no desire
        # gate -- the owner's words are its only gate).
        self.assertEqual(EXEMPT_TOOLS, SELF_ACTION_TOOLS | {"talk_to"})

    def test_tool_schema_documents_watch_video(self) -> None:
        self.assertIn("watch_video", TOOL_SCHEMA)
        self.assertIn("bvid", TOOL_SCHEMA)
        self.assertIn("depth", TOOL_SCHEMA)
        # The contract she reads: material lands NEXT turn, this turn she
        # still talks; full depth is an honest refusal on the cloud box.
        self.assertIn("下一轮", TOOL_SCHEMA)
        self.assertIn("full", TOOL_SCHEMA)

    def test_parse_turn_plan_keeps_watch_video(self) -> None:
        plan = parse_turn_plan(
            '{"messages": ["我去看看"], '
            '"actions": [{"tool": "watch_video", "args": {"query": "猫", "depth": "text"}}]}'
        )
        assert plan is not None
        self.assertEqual([action.tool for action in plan.actions], ["watch_video"])
        self.assertEqual(plan.actions[0].args, {"query": "猫", "depth": "text"})


class ExplicitTests(unittest.TestCase):
    def test_positives_exempt_the_gate(self) -> None:
        for phrase in (
            "帮我看看这个视频",
            "看看这个视频",
            "你瞅一下这个片子",
            "刷一下这个影片",
            "帮我看看这个链接",
            "看一下这个BV1xx411c7mD",
            "看看这个BV1xx411c7mD",
            "BV17x411w7KC 帮我看看",
            "这个BV号帮我看看",
            "b23.tv/abc123 看看",
            "这个视频帮我看看",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(explicit_request(phrase, "watch_video"), phrase)

    def test_negatives_do_not_exempt(self) -> None:
        for phrase in (
            "别看这个视频了",
            "不要看了，休息吧",
            "不用看这个BV号了",
            "今天在B站看了一天视频",
            "看了一天视频好累",
            "那个视频真好看",
            "随便聊聊",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(explicit_request(phrase, "watch_video"), phrase)

    def test_other_tools_never_get_watch_video_exemption(self) -> None:
        for phrase in ("帮我看看这个视频", "看看这个BV1xx411c7mD"):
            with self.subTest(phrase=phrase):
                self.assertFalse(explicit_request(phrase, "make_photo"))
                self.assertFalse(explicit_request(phrase, "read_qzone"))


class RuntimeCase(unittest.TestCase):
    """Shared harness: temp runtime + fake bilibili client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-watch-video-")
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.client = FakeBiliClient()
        self.runtime.bilibili_watch_client = self.client

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def run_action(
        self,
        args: dict,
        *,
        tool: str = "watch_video",
        session_id: str = PRIVATE_SESSION,
        trust: TrustLevel = TrustLevel.PRIVATE,
        now: float = NOW,
        user_text: str | None = None,
    ) -> dict:
        results = self.runtime.execute_actions(
            [TurnAction(tool, args)],
            session_id=session_id,
            reply="",
            now=now,
            trust=trust,
            user_text=user_text,
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def evidence_rows(self) -> list:
        return self.db.query("SELECT * FROM self_action_evidence ORDER BY id")

    def watch_logs(self) -> list:
        return self.db.query(
            "SELECT * FROM decision_log WHERE kind='watch_video' ORDER BY id"
        )

    def day_count(self) -> int:
        raw = self.db.get_meta("watch_video_day_count")
        try:
            return int(str(raw or "0"))
        except (TypeError, ValueError):
            return 0

    def dry_count(self, query: str) -> float:
        return self.runtime._meta_float(
            self.db, f"watch_video_dry:{query.strip().lower()[:80]}"
        )

    def calls_of(self, kind: str) -> list[dict]:
        return [call for call in self.client.calls if call["kind"] == kind]


class ArgValidationTests(RuntimeCase):
    def test_missing_both_args_refuses(self) -> None:
        result = self.run_action({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_bvid_or_query")
        self.assertEqual(self.client.calls, [])

    def test_invalid_depth_refuses(self) -> None:
        result = self.run_action({"query": "猫", "depth": "hologram"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_depth")
        self.assertEqual(self.client.calls, [])

    def test_depth_full_refuses_without_any_fetch(self) -> None:
        # The cloud box has no video server (backlog T-0076): refuse honestly
        # instead of pretending she watched. No budget, no cooldown, no calls.
        self.client.add_video(BV_FRESH, "新鲜视频")
        result = self.run_action({"bvid": BV_FRESH, "depth": "full"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "video_server_unavailable")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.day_count(), 0)
        self.assertEqual(self.evidence_rows(), [])

    def test_invalid_bvid_refuses(self) -> None:
        for bad in ("BV123", "av170001", "BV17x411w7KCextra"):
            with self.subTest(bvid=bad):
                result = self.run_action({"bvid": bad})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "invalid_bvid")
        self.assertEqual(self.client.calls, [])

    def test_bvid_wins_when_both_args_given(self) -> None:
        self.client.add_video(BV_FRESH, "新鲜视频", duration=600)
        result = self.run_action({"bvid": BV_FRESH, "query": "猫"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["bvid"], BV_FRESH)
        self.assertEqual(self.calls_of("search"), [])


class QuerySelectionTests(RuntimeCase):
    def test_searches_five_and_picks_hottest_within_cap(self) -> None:
        self.client.add_video(BV_CHILL, "冷静的视频", hot=0.4, duration=600)
        self.client.add_video(BV_FRESH, "新鲜视频", hot=0.9, duration=900)
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.client.calls[0], {"kind": "search", "keyword": "鲸鱼", "limit": 5})
        self.assertEqual(result["result"]["bvid"], BV_FRESH)

    def test_already_ingested_bvids_are_skipped(self) -> None:
        self.client.add_video(BV_WATCHED, "已经看过的视频", hot=0.9, duration=600)
        self.client.add_video(BV_FRESH, "新鲜视频", hot=0.4, duration=600)
        self.runtime.bilibili.ingest_video(
            bvid=BV_WATCHED, title="已经看过的视频", subtitle="旧素材"
        )
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["bvid"], BV_FRESH)

    def test_duration_cap_skips_overlong_candidates(self) -> None:
        self.client.add_video(BV_HOT, "两个半小时的合集", hot=0.9, duration=9000)
        self.client.add_video(BV_FRESH, "十分钟的新视频", hot=0.4, duration=600)
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["bvid"], BV_FRESH)
        self.assertEqual(result["result"]["duration_seconds"], 600)

    def test_dead_candidate_is_skipped_not_fatal(self) -> None:
        self.client.add_video(BV_HOT, "没了的视频", hot=0.9, duration=600)
        self.client.dead_bvids.add(BV_HOT)
        self.client.add_video(BV_FRESH, "新鲜视频", hot=0.4, duration=600)
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["bvid"], BV_FRESH)

    def test_all_filtered_is_no_candidate(self) -> None:
        self.client.add_video(BV_WATCHED, "已经看过的视频", hot=0.9, duration=600)
        self.runtime.bilibili.ingest_video(
            bvid=BV_WATCHED, title="已经看过的视频", subtitle="旧素材"
        )
        result = self.run_action({"query": "鲸鱼"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_candidate")
        # The search happened (that is the network exposure worth budgeting);
        # the per-candidate reads did not.
        self.assertEqual(len(self.calls_of("search")), 1)
        self.assertEqual(self.calls_of("subtitles"), [])

    def test_cid_is_reused_for_subtitles_and_comments(self) -> None:
        self.client.add_video(BV_FRESH, "新鲜视频", hot=0.9, duration=600, cid=777)
        self.client.subtitles[BV_FRESH] = {"lines": ["[0:12-0:15] 你好"], "source": "cc"}
        self.run_action({"query": "鲸鱼"})
        sub_calls = self.calls_of("subtitles")
        self.assertEqual(len(sub_calls), 1)
        self.assertEqual(sub_calls[0]["cid"], 777)

    def test_disabled_config_refuses_without_calls(self) -> None:
        self._tmp2 = tempfile.TemporaryDirectory(prefix="fatfish-watch-off-")
        config, db, runtime = make_runtime(
            Path(self._tmp2.name), name="off", bili_enabled=False
        )
        self.addCleanup(self._tmp2.cleanup)
        self.addCleanup(db.close)
        client = FakeBiliClient()
        client.add_video(BV_FRESH, "新鲜视频")
        runtime.bilibili_watch_client = client
        results = runtime.execute_actions(
            [TurnAction("watch_video", {"query": "鲸鱼"})],
            session_id=PRIVATE_SESSION,
            reply="",
            now=NOW,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "bilibili_disabled")
        self.assertEqual(client.calls, [])

    def test_client_build_failure_refuses_as_unavailable(self) -> None:
        # No injected client + a builder that cannot build: refuse, never
        # fall back to "try the network anyway".
        self.runtime.bilibili_watch_client = None
        original = runtime_module.BilibiliClient

        class Broken:
            @classmethod
            def from_config(cls, config: object) -> object:
                raise RuntimeError("no cookie dir")

        runtime_module.BilibiliClient = Broken
        try:
            result = self.run_action({"query": "鲸鱼"})
        finally:
            runtime_module.BilibiliClient = original
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bilibili_unavailable")
        self.assertEqual(self.client.calls, [])


class DepthTests(RuntimeCase):
    def seed(self) -> None:
        self.client.add_video(
            BV_FRESH, "新鲜视频", desc="这是简介", hot=0.9, duration=600
        )
        self.client.subtitles[BV_FRESH] = {
            "lines": ["[0:12-0:15] 你好呀", "[0:16-0:20] 今天讲鲸鱼"],
            "source": "cc",
        }
        self.client.comments[BV_FRESH] = ["小明: 好看", "小红: 学到了"]
        self.client.snapshots[BV_FRESH] = {
            "image_url": "http://i0.hdslb.com/sprite.jpg",
            "img_x_len": 10,
            "img_y_len": 10,
            "index": [0, 10, 20],
        }

    def test_meta_depth_reads_view_only(self) -> None:
        self.seed()
        result = self.run_action({"query": "鲸鱼", "depth": "meta"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.calls_of("subtitles"), [])
        self.assertEqual(self.calls_of("comments"), [])
        self.assertEqual(self.calls_of("snapshot"), [])
        self.assertEqual(result["result"]["transcript_source"], "none")
        content = self.evidence_rows()[-1]["content"]
        self.assertIn("这是简介", content)
        self.assertNotIn("字幕", content)

    def test_text_depth_adds_subtitles_and_comments(self) -> None:
        self.seed()
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["transcript_source"], "cc")
        self.assertEqual(result["result"]["comments"], 2)
        content = self.evidence_rows()[-1]["content"]
        self.assertIn("[0:12-0:15] 你好呀", content)
        self.assertIn("小明: 好看", content)
        self.assertIn("cc", content)

    def test_no_subtitles_says_so(self) -> None:
        self.client.add_video(BV_FRESH, "新鲜视频", desc="这是简介")
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["transcript_source"], "none")
        content = self.evidence_rows()[-1]["content"]
        self.assertIn("没有可用", content)
        self.assertIn("这是简介", content)

    def test_visual_depth_records_snapshot_metadata_only(self) -> None:
        self.seed()
        result = self.run_action({"query": "鲸鱼", "depth": "visual"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.calls_of("snapshot")), 1)
        content = self.evidence_rows()[-1]["content"]
        # Metadata (URL / grid / frame index) is written, never the image.
        self.assertIn("http://i0.hdslb.com/sprite.jpg", content)
        self.assertIn("10x10", content)
        self.assertIn("元数据", content)

    def test_visual_without_snapshot_still_succeeds(self) -> None:
        self.seed()
        del self.client.snapshots[BV_FRESH]
        result = self.run_action({"query": "鲸鱼", "depth": "visual"})
        self.assertTrue(result["ok"], result)

    def test_subtitle_overflow_is_clamped_to_evidence_budget(self) -> None:
        self.seed()
        self.client.subtitles[BV_FRESH] = {
            "lines": [f"[0:{index:02d}-0:{index + 1:02d}] 字幕行{'很' * 40}" for index in range(200)],
            "source": "cc",
        }
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)
        content = self.evidence_rows()[-1]["content"]
        self.assertLessEqual(len(content), 4000)
        self.assertIn("省略", content)


class BudgetTests(RuntimeCase):
    def test_cooldown_blocks_second_watch_in_session(self) -> None:
        self.client.search_index = {
            "鲸鱼": [self.client.add_video(BV_FRESH, "第一条")],
            "海豚": [self.client.add_video(BV_CHILL, "第二条")],
        }
        # desire=1.0 keeps the *desire* gate wide open across the sequence --
        # every successful watch lifts the threshold, and this class is about
        # the runtime budgets, not the gate.
        first = self.run_action({"query": "鲸鱼", "desire": 1.0}, now=NOW)
        self.assertTrue(first["ok"], first)
        second = self.run_action({"query": "海豚", "desire": 1.0}, now=NOW + MINUTE)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "cooldown")
        # The refusal happened before any fetch for the second query.
        self.assertEqual(len(self.calls_of("search")), 1)
        third = self.run_action({"query": "海豚", "desire": 1.0}, now=NOW + 31 * MINUTE)
        self.assertTrue(third["ok"], third)

    def test_daily_budget_stops_after_three_watches(self) -> None:
        self.client.search_index = {
            "一": [self.client.add_video(BV_FRESH, "第一条")],
            "二": [self.client.add_video(BV_CHILL, "第二条")],
            "三": [self.client.add_video(BV_META, "第三条")],
            "四": [self.client.add_video(BV_DRY, "第四条")],
        }
        for offset, query in enumerate(("一", "二", "三")):
            result = self.run_action(
                {"query": query, "desire": 1.0}, now=NOW + offset * 31 * MINUTE
            )
            self.assertTrue(result["ok"], (query, result))
        fourth = self.run_action(
            {"query": "四", "desire": 1.0}, now=NOW + 3 * 31 * MINUTE
        )
        self.assertFalse(fourth["ok"])
        self.assertEqual(fourth["error"], "daily_budget")
        self.assertEqual(len(self.calls_of("search")), 3)
        self.assertEqual(self.day_count(), 3)

    def test_budget_is_spent_before_the_fetch(self) -> None:
        # Watermark-first contract: a crashed pull must not turn the daily
        # budget into a retry loop.
        self._tmp2 = tempfile.TemporaryDirectory(prefix="fatfish-watch-cap1-")
        config, db, runtime = make_runtime(
            Path(self._tmp2.name), name="cap1", watch_max_per_day=1
        )
        self.addCleanup(self._tmp2.cleanup)
        self.addCleanup(db.close)
        client = FakeBiliClient(search_raises=True)
        runtime.bilibili_watch_client = client
        results = runtime.execute_actions(
            [TurnAction("watch_video", {"query": "鲸鱼"})],
            session_id=PRIVATE_SESSION,
            reply="",
            now=NOW,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "watch_video_failed:BilibiliError")
        raw = db.get_meta("watch_video_day_count")
        self.assertEqual(int(str(raw or "0")), 1)
        second = runtime.execute_actions(
            [TurnAction("watch_video", {"query": "鲸鱼"})],
            session_id=PRIVATE_SESSION,
            reply="",
            now=NOW + 31 * MINUTE,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(second[0]["ok"])
        self.assertEqual(second[0]["error"], "daily_budget")
        self.assertEqual(len(client.calls), 1)

    def test_cooldown_is_written_before_the_fetch(self) -> None:
        self.client.search_raises = True
        first = self.run_action({"query": "鲸鱼"})
        self.assertFalse(first["ok"])
        self.assertEqual(first["error"], "watch_video_failed:BilibiliError")
        second = self.run_action({"query": "鲸鱼"}, now=NOW + MINUTE)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "cooldown")

    def test_two_dry_watches_close_the_query(self) -> None:
        # Two distinct dry videos: the first watch ingests its video, so the
        # second watch of the same query lands on the next candidate.
        self.client.search_index = {
            "冷知识": [
                self.client.add_video(BV_DRY, "没字幕没评论的视频"),
                self.client.add_video(BV_CHILL, "也没字幕也没评论"),
            ],
        }
        for offset in (0, 31 * MINUTE):
            result = self.run_action(
                {"query": "冷知识", "desire": 1.0}, now=NOW + offset
            )
            self.assertTrue(result["ok"], result)
        self.assertEqual(self.dry_count("冷知识"), 2.0)
        blocked = self.run_action(
            {"query": "冷知识", "desire": 1.0}, now=NOW + 62 * MINUTE
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "watched_no_share")
        # Still only two searches ever ran for this query: the third attempt
        # is refused before it reaches the client.
        self.assertEqual(len(self.calls_of("search")), 2)

    def test_a_watched_video_with_material_resets_the_dry_counter(self) -> None:
        self.client.add_video(BV_DRY, "没字幕没评论的视频")
        self.run_action({"query": "冷知识", "desire": 1.0})
        self.assertEqual(self.dry_count("冷知识"), 1.0)
        self.client.search_index = {
            "冷知识": [self.client.add_video(BV_FRESH, "有字幕的视频")],
        }
        self.client.subtitles[BV_FRESH] = {"lines": ["[0:01-0:02] 嘿"], "source": "cc"}
        result = self.run_action(
            {"query": "冷知识", "desire": 1.0}, now=NOW + 31 * MINUTE
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.dry_count("冷知识"), 0.0)

    def test_watched_no_share_stands_even_when_owner_explicitly_asks(self) -> None:
        # The dry counter is a runtime budget, not her desire: the owner's
        # "go watch it" lifts the gate, not the counter.
        self.client.search_index = {
            "冷知识": [
                self.client.add_video(BV_DRY, "没字幕没评论的视频"),
                self.client.add_video(BV_CHILL, "也没字幕也没评论"),
            ],
        }
        self.run_action({"query": "冷知识", "desire": 1.0})
        self.run_action({"query": "冷知识", "desire": 1.0}, now=NOW + 31 * MINUTE)
        blocked = self.run_action(
            {"query": "冷知识"},
            now=NOW + 62 * MINUTE,
            user_text="帮我看看这个视频",
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "watched_no_share")


class EvidenceTests(RuntimeCase):
    def test_success_writes_evidence_card_and_log(self) -> None:
        self.client.add_video(BV_FRESH, "新鲜视频", desc="这是简介", hot=0.9, duration=600)
        self.client.subtitles[BV_FRESH] = {"lines": ["[0:12-0:15] 你好"], "source": "cc"}
        self.client.comments[BV_FRESH] = ["小明: 好看"]
        result = self.run_action({"query": "鲸鱼"})
        self.assertTrue(result["ok"], result)

        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        row = rows[-1]
        self.assertEqual(row["action"], "watch_video")
        self.assertEqual(row["source"], "bilibili")
        self.assertEqual(row["title"], "新鲜视频")
        self.assertIn("https://www.bilibili.com/video/" + BV_FRESH, row["content"])

        # Finding card metadata: depth / transcript source / provenance.
        chunk = self.db.query_one(
            "SELECT * FROM rag_chunks WHERE source_type='bilibili' AND source_id=?",
            (BV_FRESH,),
        )
        assert chunk is not None
        metadata = json.loads(chunk["metadata_json"])
        self.assertEqual(metadata["watch_depth"], "text")
        self.assertEqual(metadata["transcript_source"], "cc")
        self.assertEqual(metadata["related_from"], "鲸鱼")
        self.assertEqual(metadata["collected_by"], "watch_video")

        logs = self.watch_logs()
        self.assertEqual(logs[-1]["outcome"], "ok")

    def test_direct_bvid_provenance_and_no_reingest(self) -> None:
        self.client.add_video(BV_FRESH, "新鲜视频", duration=600, in_search=False)
        self.runtime.bilibili.ingest_video(
            bvid=BV_FRESH, title="新鲜视频", subtitle="旧的"
        )
        result = self.run_action(
            {"bvid": BV_FRESH, "related_from": BV_SOURCE}
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["result"]["ingested"])
        chunk = self.db.query_one(
            "SELECT COUNT(*) AS n FROM rag_chunks WHERE source_type='bilibili' AND source_id=?",
            (BV_FRESH,),
        )
        assert chunk is not None
        self.assertEqual(chunk["n"], 1)

    def test_direct_bvid_is_not_duration_capped(self) -> None:
        # He named the video himself: the cap protects the *selection*, not
        # an explicit ask.
        self.client.add_video(BV_FRESH, "三小时的纪录片", duration=11000, in_search=False)
        result = self.run_action({"bvid": BV_FRESH})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["duration_seconds"], 11000)

    def test_refusals_leave_an_audit_trail(self) -> None:
        self.client.add_video(BV_FRESH, "新鲜视频")
        self.run_action({"query": "鲸鱼", "desire": 1.0}, now=NOW)
        self.run_action(
            {"query": "海豚", "desire": 1.0}, now=NOW + MINUTE
        )  # cooldown
        outcomes = [row["outcome"] for row in self.watch_logs()]
        self.assertEqual(outcomes, ["ok", "cooldown"])
        self.assertEqual(self.evidence_rows()[-1]["action"], "watch_video")


class GateTests(RuntimeCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-watch-gate-")
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(
            self.tmp, name="gate", initial_threshold=0.99
        )
        self.client = FakeBiliClient()
        self.client.add_video(BV_FRESH, "新鲜视频")
        self.runtime.bilibili_watch_client = self.client

    def test_own_initiative_below_threshold_is_refused(self) -> None:
        result = self.run_action({"query": "鲸鱼"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "below_threshold")
        self.assertEqual(self.client.calls, [])
        # Refusals that never reach the fetch path never spend the budget.
        self.assertEqual(self.day_count(), 0)

    def test_explicit_owner_ask_steps_over_the_gate(self) -> None:
        result = self.run_action({"query": "鲸鱼"}, user_text="帮我看看这个视频")
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
