"""Offline tests for the QQ Zone collector: idempotency, privacy scope,
digest, circuit breaker, poll gating. No network, no real uins/cookies."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.qzone import QzoneCollector
from fatfish.qzone.client import QzoneClient, QzoneError
from fatfish.runtime import Runtime

OWNER_UIN = "10000"
FRIEND_UIN = "20001"


def make_env(
    tmp: Path,
    *,
    qzone_enabled: str = "false",
    qzone_feeds_optional: str = "",
    friend_uins: list[str] | None = None,
    friend_fallback: str = "",
):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'qzone.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[qzone]
enabled = {qzone_enabled}
cookie_path = "qzone_cookies.json"
owner_uin = "{OWNER_UIN}"
poll_interval_minutes = 60
max_feed_items_per_poll = 20
friends_content_to_llm = false
"""
    if qzone_feeds_optional:
        toml += f"feeds_optional = {qzone_feeds_optional}\n"
    if friend_fallback:
        toml += f"friend_fallback = {friend_fallback}\n"
    if friend_uins:
        rendered = ", ".join(f'"{uin}"' for uin in friend_uins)
        toml += f"friend_uins = [{rendered}]\n"
    # T-0025: low.toml 默认开启决策轮；tick 测试不测它，显式关掉，
    # 否则会真的调 LLM（联网）/写台账。必须放在 feeds_optional 追加之后，
    # 不然它会落进 [activity] 节而不是 [qzone]。
    toml += '[activity]' + chr(10) + 'enabled = false' + chr(10)
    path = tmp / "qzone.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def feed_item(external_id: str, uin: str, content: str, name: str = ""):
    return {
        "external_id": external_id,
        "author_uin": uin,
        "author_name": name or ("主人" if uin == OWNER_UIN else "好友甲"),
        "content": content,
        "media": [],
        "published_at": 1761200000.0,
        "url": "",
        "raw": {"feedid": external_id},
    }


class FakeClient:
    """Stand-in for QzoneClient: returns fixtures or raises, never network."""

    def __init__(
        self,
        items=None,
        emotions=None,
        error: Exception | None = None,
        feeds_error: Exception | None = None,
        emotions_error: Exception | None = None,
        friend_emotions: dict[str, list] | None = None,
        friend_errors: dict[str, Exception] | None = None,
    ):
        self.items = items if items is not None else []
        self.emotions = emotions if emotions is not None else []
        # ``error`` fails both endpoints (cookie/login outage); the split
        # variants simulate a single-endpoint failure.
        self.error = error
        self.feeds_error = feeds_error or error
        self.emotions_error = emotions_error or error
        self.friend_emotions = friend_emotions or {}
        self.friend_errors = friend_errors or {}
        self.emotion_uins: list[str] = []
        self.uin = OWNER_UIN
        self.fetch_calls = 0

    def fetch_feeds(self, count=None):
        self.fetch_calls += 1
        if self.feeds_error is not None:
            raise self.feeds_error
        return list(self.items)

    def fetch_emotions(self, count=None, *, uin=None):
        if uin is not None:
            self.emotion_uins.append(str(uin))
            if self.emotions_error is not None:
                raise self.emotions_error
            if str(uin) in self.friend_errors:
                raise self.friend_errors[str(uin)]
            return list(self.friend_emotions.get(str(uin), []))
        if self.emotions_error is not None:
            raise self.emotions_error
        return list(self.emotions)

    def cookie_status(self):
        return {"full_read": True, "missing": []}


class CollectorIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.collector = QzoneCollector(self.memory, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_origin_scope_privacy_grading(self) -> None:
        items = [
            feed_item("own1", OWNER_UIN, "主人自己的动态"),
            feed_item("fr1", FRIEND_UIN, "好友的动态"),
        ]
        stored = self.collector.store_items(items, now=time.time())
        self.assertEqual(
            [i["author_scope"] for i in stored], ["self", "friend"]
        )
        self.collector.ingest_into_memory(stored)
        own = self.db.query_one(
            "SELECT origin_scope, trust_level FROM memory_items WHERE source_type='qzone' AND source_id='own1'"
        )
        friend = self.db.query_one(
            "SELECT origin_scope FROM memory_items WHERE source_type='qzone' AND source_id='fr1'"
        )
        self.assertEqual(own["origin_scope"], "qzone")
        self.assertEqual(own["trust_level"], "external")
        self.assertEqual(friend["origin_scope"], "qzone_friend")
        rag = self.db.query_one(
            "SELECT metadata_json FROM rag_chunks WHERE source_type='qzone' AND source_id='fr1'"
        )
        self.assertIn("qzone_friend", rag["metadata_json"])

    def test_fetch_and_ingest_is_idempotent(self) -> None:
        client = FakeClient(items=[feed_item("own1", OWNER_UIN, "第一条"), feed_item("fr1", FRIEND_UIN, "好友的")])
        first = self.collector.fetch_and_ingest(client, manual=True, now=1_761_200_000.0)
        self.assertTrue(first["ok"])
        self.assertEqual(first["new"], 2)
        second = self.collector.fetch_and_ingest(client, manual=True, now=1_761_200_010.0)
        self.assertEqual(second["new"], 0)
        rows = self.db.query("SELECT COUNT(*) AS n FROM qzone_feed_items")
        self.assertEqual(int(rows[0]["n"]), 2)
        memories = self.db.query(
            "SELECT COUNT(*) AS n FROM memory_items WHERE source_type='qzone'"
        )
        # 2 items + 1 daily digest, not duplicated by the second poll.
        self.assertEqual(int(memories[0]["n"]), 3)

    def test_digest_one_per_day_and_scope(self) -> None:
        day = 1_761_200_000.0
        client = FakeClient(items=[feed_item("fr1", FRIEND_UIN, "好友动态")])
        self.collector.fetch_and_ingest(client, manual=True, now=day)
        digest = self.db.query_one(
            "SELECT origin_scope, importance, source_id FROM memory_items "
            "WHERE source_type='qzone' AND source_id LIKE 'digest:%'"
        )
        self.assertIsNotNone(digest)
        # A digest that mentions friend content carries the strict scope.
        self.assertEqual(digest["origin_scope"], "qzone_friend")
        self.assertLessEqual(float(digest["importance"]), 0.3)
        # Only-self content digests stay readable in-prompt.
        self.collector.forget(all_items=True)
        client2 = FakeClient(items=[feed_item("own2", OWNER_UIN, "只有自己")])
        self.collector.fetch_and_ingest(client2, manual=True, now=day + 86400)
        digest2 = self.db.query_one(
            "SELECT origin_scope FROM memory_items "
            "WHERE source_type='qzone' AND source_id LIKE 'digest:%' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        self.assertEqual(digest2["origin_scope"], "qzone")

    def test_circuit_breaker_and_resume(self) -> None:
        client = FakeClient(error=QzoneError("qzone http 403: blocked", login_required=True))
        for _ in range(QzoneCollector(self.memory, self.config).breaker_threshold):
            result = self.collector.fetch_and_ingest(client, manual=True)
            self.assertFalse(result["ok"])
        self.assertTrue(self.collector.breaker_open)
        decision = self.db.query_one(
            "SELECT action FROM decision_log WHERE kind='qzone_poll' AND action='circuit_break'"
        )
        self.assertIsNotNone(decision)
        # Once open, even a healthy client is skipped.
        healthy = FakeClient(items=[feed_item("own1", OWNER_UIN, "x")])
        skipped = self.collector.fetch_and_ingest(healthy, manual=True)
        self.assertEqual(skipped.get("skipped"), "circuit_break")
        self.assertEqual(healthy.fetch_calls, 0)
        self.collector.resume()
        self.assertFalse(self.collector.breaker_open)
        ok = self.collector.fetch_and_ingest(healthy, manual=True)
        self.assertTrue(ok["ok"])

    def test_feeds_optional_partial_poll_never_trips_breaker(self) -> None:
        """feeds_optional=true: feeds down + emotions up = partial, no breaker."""
        with tempfile.TemporaryDirectory() as raw:
            config, db = make_env(Path(raw), qzone_feeds_optional="true")
            memory = MemoryService(db, config)
            collector = QzoneCollector(memory, config)
            try:
                client = FakeClient(
                    emotions=[feed_item("own1", OWNER_UIN, "自己的说说")],
                    feeds_error=QzoneError("qzone api error -4003: denied", code=-4003),
                )
                for index in range(5):  # more than the breaker threshold
                    result = collector.fetch_and_ingest(client, manual=True)
                    self.assertTrue(result["ok"])
                    self.assertEqual(
                        result["errors"], ["feeds: qzone api error -4003: denied"]
                    )
                    self.assertEqual(result["new"], 1 if index == 0 else 0)
                self.assertFalse(collector.breaker_open)
                self.assertEqual(collector.consecutive_failures(), 0)
                decision = db.query_one(
                    "SELECT outcome, output_json FROM decision_log "
                    "WHERE kind='qzone_poll' AND action='poll' ORDER BY id DESC LIMIT 1"
                )
                self.assertEqual(decision["outcome"], "partial")
                self.assertIn("feeds_error", decision["output_json"])
            finally:
                db.close()

    def test_feeds_failure_default_config_still_counts(self) -> None:
        """Default (feeds_optional absent): feeds failure counts toward the breaker."""
        client = FakeClient(
            emotions=[feed_item("own1", OWNER_UIN, "自己的说说")],
            feeds_error=QzoneError("qzone api error -4003: denied", code=-4003),
        )
        result = self.collector.fetch_and_ingest(client, manual=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.collector.consecutive_failures(), 1)
        self.assertFalse(self.collector.breaker_open)  # one failure, threshold not hit

    def test_enabled_gate_and_manual_bypass(self) -> None:
        client = FakeClient(items=[feed_item("own1", OWNER_UIN, "x")])
        gated = self.collector.fetch_and_ingest(client)
        self.assertEqual(gated.get("skipped"), "disabled")
        self.assertEqual(client.fetch_calls, 0)
        manual = self.collector.fetch_and_ingest(client, manual=True)
        self.assertTrue(manual["ok"])

    def test_embedded_comments_go_to_evidence_and_rag_meta(self) -> None:
        item = feed_item("ownc1", OWNER_UIN, "有评论的动态")
        item["comments"] = [
            {"author": "评论者甲", "text": "说得好"},
            {"author": "", "text": "无昵称评论"},
            "坏数据不是dict",
        ]
        self.collector.store_items([item], now=time.time())
        self.collector.ingest_into_memory([{**item, "author_scope": "self"}])
        row = self.db.query_one(
            "SELECT text, metadata_json FROM rag_chunks WHERE source_type='qzone' AND source_id='ownc1'"
        )
        meta = json.loads(row["metadata_json"])
        self.assertEqual(
            meta["comments"],
            [
                {"author": "评论者甲", "text": "说得好"},
                {"author": "", "text": "无昵称评论"},
            ],
        )
        mem = self.db.query_one(
            "SELECT evidence, importance, origin_scope FROM memory_items "
            "WHERE source_type='qzone' AND source_id='ownc1'"
        )
        self.assertIn("评论 评论者甲: 说得好", mem["evidence"])
        self.assertIn("评论 匿名: 无昵称评论", mem["evidence"])
        # Comments never lift importance and inherit the item scope.
        self.assertLessEqual(float(mem["importance"]), 0.3)
        self.assertEqual(mem["origin_scope"], "qzone")

    def test_friend_item_comments_inherit_friend_scope(self) -> None:
        item = feed_item("frc1", FRIEND_UIN, "好友动态带评论")
        item["comments"] = [{"author": "路人", "text": "插一句"}]
        stored = self.collector.store_items([item], now=time.time())
        self.assertEqual(stored[0]["author_scope"], "friend")
        self.collector.ingest_into_memory(stored)
        mem = self.db.query_one(
            "SELECT origin_scope, evidence FROM memory_items "
            "WHERE source_type='qzone' AND source_id='frc1'"
        )
        self.assertEqual(mem["origin_scope"], "qzone_friend")
        self.assertIn("评论 路人: 插一句", mem["evidence"])
        rag = self.db.query_one(
            "SELECT metadata_json FROM rag_chunks WHERE source_type='qzone' AND source_id='frc1'"
        )
        self.assertIn("插一句", rag["metadata_json"])
        self.assertIn("qzone_friend", rag["metadata_json"])


class FriendFallbackTests(unittest.TestCase):
    """聚合 feed 被 ic2 判未登录时，逐个好友拉 emotion_cgi（2026-10-29 实测）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.closers = []

    def tearDown(self) -> None:
        for db in self.closers:
            db.close()
        self._tmp.cleanup()

    def _collector(self, **kwargs):
        config, db = make_env(self.tmp, **kwargs)
        self.closers.append(db)
        return QzoneCollector(MemoryService(db, config), config), db

    def _dead_feeds(self) -> QzoneError:
        return QzoneError(
            "qzone api error -3000: need login", code=-3000, login_required=True
        )

    def test_feeds_failure_falls_back_to_friends(self) -> None:
        collector, db = self._collector(friend_uins=[FRIEND_UIN])
        client = FakeClient(
            feeds_error=self._dead_feeds(),
            friend_emotions={
                FRIEND_UIN: [feed_item("fr-x1", FRIEND_UIN, "好友今天发了一条")]
            },
        )
        result = collector.fetch_and_ingest(client, manual=True, now=time.time())
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["fetched"], 1)
        self.assertEqual([FRIEND_UIN], client.emotion_uins)
        row = db.query_one(
            "SELECT author_scope FROM qzone_feed_items WHERE external_id='fr-x1'"
        )
        self.assertIsNotNone(row)
        self.assertEqual("friend", row["author_scope"])
        # 端点挂了但有数据：不算连续失败，breaker 不动。
        self.assertEqual(0, collector.consecutive_failures())
        fresh = db.get_meta("qzone_breaker_open")
        self.assertFalse(bool(fresh and fresh != "0"))

    def test_fallback_can_be_disabled(self) -> None:
        collector, _db = self._collector(
            friend_uins=[FRIEND_UIN], friend_fallback="false"
        )
        client = FakeClient(
            feeds_error=self._dead_feeds(),
            friend_emotions={FRIEND_UIN: [feed_item("fr-x2", FRIEND_UIN, "不该被读到")]},
        )
        result = collector.fetch_and_ingest(client, manual=True, now=time.time())
        self.assertFalse(result["ok"])
        self.assertEqual([], client.emotion_uins)
        self.assertEqual(1, collector.consecutive_failures())

    def test_one_friend_failure_does_not_kill_the_others(self) -> None:
        other = "20002"
        collector, db = self._collector(friend_uins=[FRIEND_UIN, other])
        client = FakeClient(
            feeds_error=self._dead_feeds(),
            friend_emotions={other: [feed_item("fr-ok", other, "另一个好友的动态")]},
            friend_errors={FRIEND_UIN: QzoneError("qzone api error -3000", code=-3000)},
        )
        result = collector.fetch_and_ingest(client, manual=True, now=time.time())
        self.assertTrue(result["ok"], result)
        self.assertEqual([FRIEND_UIN, other], client.emotion_uins)
        self.assertIsNotNone(
            db.query_one("SELECT 1 FROM qzone_feed_items WHERE external_id='fr-ok'")
        )
        self.assertTrue(any("friend:" in e for e in result.get("errors", [])))

    def test_no_fallback_when_feeds_works(self) -> None:
        collector, _db = self._collector(friend_uins=[FRIEND_UIN])
        client = FakeClient(items=[feed_item("own-ok", OWNER_UIN, "聚合 feed 正常")])
        result = collector.fetch_and_ingest(client, manual=True, now=time.time())
        self.assertTrue(result["ok"])
        self.assertEqual([], client.emotion_uins, "feed 正常时不该逐好友拉")

    def test_friend_uins_config_parsing(self) -> None:
        collector, _db = self._collector(
            friend_uins=[FRIEND_UIN, FRIEND_UIN, "abc", " 20003 "]
        )
        self.assertEqual([FRIEND_UIN, "20003"], collector.friend_uins)


class RuntimePollTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_maybe_poll_qzone_gating_and_watermark(self) -> None:
        config, db = make_env(self.tmp, qzone_enabled="true")
        try:
            runtime = Runtime(db, config)
            client = FakeClient(items=[feed_item("own1", OWNER_UIN, "轮询到的")])
            runtime.qzone = QzoneCollector(runtime.memory, config, client=client)
            first = runtime.maybe_poll_qzone(now=1_761_200_000.0)
            self.assertTrue(first and first["ok"])
            self.assertIsNotNone(db.get_meta("last_qzone_poll_at"))
            # Within the clamped interval: no second poll.
            second = runtime.maybe_poll_qzone(now=1_761_200_000.0 + 20 * 60)
            self.assertIsNone(second)
            # After the interval: polls again.
            third = runtime.maybe_poll_qzone(now=1_761_200_000.0 + 61 * 60)
            self.assertTrue(third and third["ok"])
        finally:
            db.close()

    def test_maybe_poll_qzone_disabled_returns_none(self) -> None:
        config, db = make_env(self.tmp, qzone_enabled="false")
        try:
            runtime = Runtime(db, config)
            self.assertIsNone(runtime.maybe_poll_qzone(now=1_761_200_000.0))
            self.assertIsNone(db.get_meta("last_qzone_poll_at"))
        finally:
            db.close()

    def test_tick_reports_qzone_key(self) -> None:
        config, db = make_env(self.tmp, qzone_enabled="false")
        try:
            runtime = Runtime(db, config)
            runtime.voice.decide = lambda *a, **k: None
            result = runtime.tick(now=1_761_200_000.0)
            self.assertIn("qzone", result)
            self.assertIsNone(result["qzone"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
