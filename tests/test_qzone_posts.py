"""Offline tests for the QQ Zone write path: drafts, approval, quota, quiet
hours, outbox delivery and the publish client.

No network: the publish client is exercised through a fake opener with
fixture payloads, and the outbox handler gets a stub client. Every cookie
value below is synthetic.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fatfish.clock import in_quiet_hours, to_local
from fatfish.config import Config, load_config
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.outbox import Outbox
from fatfish.privacy import SecretNotes
from fatfish.qzone import QzoneClient, QzoneError, QzonePostService
from fatfish.qzone.client import EMOTION_PUBLISH_URL, QZONETOKEN_URL
from fatfish.qzone.posts import OUTBOX_KIND, POST_MAX_ATTEMPTS
from fatfish.runtime import Runtime, TurnAction

# 2026-10-24 14:00 / 23:50 in UTC+8: deterministic against both the real
# ZoneInfo and the Windows UTC+8 fallback in clock.get_timezone.
TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
NIGHT = datetime(2026, 10, 24, 23, 50, tzinfo=TZ8).timestamp()

COOKIE_FIELDS = {"uin": "10000", "skey": "@synthetic-skey", "p_skey": "@synthetic-pskey"}


def make_env(tmp: Path, **overrides):
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "qzone_write.db").as_posix()}"',
        '[memory]',
        'embedding_backend = "hash"',
        'embedding_dim = 128',
        '[qzone]',
        'enabled = true',
        'read_only = false',
        'require_approval = true',
        'max_posts_per_day = 1',
        'content_max_chars = 2000',
        'quiet_hours_start = "00:00"',
        'quiet_hours_end = "00:00"',
        'owner_uin = "10000"',
    ]
    for key, value in overrides.items():
        if isinstance(value, str):
            lines.append(f'{key} = "{value}"')
        else:
            lines.append(f'{key} = {value}')
    path = tmp / "qzone_write.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def make_service(config: Config, db: Database, **kwargs) -> QzonePostService:
    outbox = kwargs.pop("outbox", None) or Outbox(db, lease_seconds=1, max_attempts=5)
    return QzonePostService(db, config, outbox=outbox, **kwargs)


class BaseWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------- drafts
class DraftTests(BaseWriteTest):
    def test_normal_draft_lands_pending_approval(self) -> None:
        service = make_service(self.config, self.db)
        result = service.create_post_draft(
            "今天出海回来，尾巴有点酸。", session_id="web:owner", now=NOON
        )
        self.assertTrue(result["ok"])
        self.assertEqual("pending_approval", result["result"]["status"])
        row = self.db.query_one("SELECT * FROM qzone_posts")
        self.assertEqual("draft", row["status"])
        self.assertEqual("今天出海回来，尾巴有点酸。", row["content"])
        self.assertIsNone(row["decided_at"])
        log = self.db.query_one("SELECT * FROM decision_log WHERE kind='qzone_post'")
        self.assertIsNotNone(log)
        self.assertEqual("draft_created", log["action"])

    def test_disabled_qzone_rejects(self) -> None:
        self.config._data["qzone"]["enabled"] = False
        service = make_service(self.config, self.db)
        result = service.create_post_draft("hi", now=NOON)
        self.assertFalse(result["ok"])
        self.assertEqual("qzone_disabled", result["error"])
        self.assertIsNone(self.db.query_one("SELECT * FROM qzone_posts"))

    def test_read_only_rejects(self) -> None:
        self.config._data["qzone"]["read_only"] = True
        service = make_service(self.config, self.db)
        result = service.create_post_draft("hi", now=NOON)
        self.assertFalse(result["ok"])
        self.assertEqual("qzone_read_only", result["error"])

    def test_empty_content_rejects(self) -> None:
        service = make_service(self.config, self.db)
        for text in ("", "   "):
            result = service.create_post_draft(text, now=NOON)
            self.assertFalse(result["ok"], text)
            self.assertEqual("empty_content", result["error"])

    def test_long_content_is_clipped(self) -> None:
        self.config._data["qzone"]["content_max_chars"] = 20
        service = make_service(self.config, self.db)
        result = service.create_post_draft("一" * 50, now=NOON)
        self.assertTrue(result["ok"])
        row = self.db.query_one("SELECT content FROM qzone_posts")
        self.assertLessEqual(len(row["content"]), 20)
        self.assertTrue(row["content"].endswith("…"))

    def test_credential_content_is_blocked_and_redacted(self) -> None:
        service = make_service(self.config, self.db)
        secret = "skey=@SYNTHETIC-VALUE-123"
        result = service.create_post_draft(f"我的cookie是 {secret}，帮我记住", now=NOON)
        self.assertFalse(result["ok"])
        self.assertEqual("sensitive_content", result["error"])
        row = self.db.query_one("SELECT * FROM qzone_posts")
        self.assertEqual("blocked", row["status"])
        # The raw value must not be stored either: the row keeps the redacted text.
        self.assertNotIn("@SYNTHETIC-VALUE-123", row["content"])
        self.assertIn("skey=[已隐藏]", row["content"])
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='qzone_post' AND action='blocked'"
        )
        self.assertIsNotNone(log)

    def test_secret_note_content_is_blocked(self) -> None:
        notes = SecretNotes(self.db)
        notes.add("我家住在幸福小区3栋502")
        service = make_service(self.config, self.db)
        result = service.create_post_draft("今天路过我家住在幸福小区3栋502的门口", now=NOON)
        self.assertFalse(result["ok"])
        self.assertEqual("sensitive_content", result["error"])
        self.assertEqual("blocked", self.db.query_one("SELECT status FROM qzone_posts")["status"])

    def test_ordinary_similarity_is_not_blocked(self) -> None:
        notes = SecretNotes(self.db)
        notes.add("我家住在幸福小区3栋502")
        service = make_service(self.config, self.db)
        result = service.create_post_draft("幸福小区真不错，今天去逛了逛", now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("draft", self.db.query_one("SELECT status FROM qzone_posts")["status"])

    def test_duplicate_idempotency_key_returns_existing_row(self) -> None:
        service = make_service(self.config, self.db)
        first = service.create_post_draft("第一条", now=NOON, idempotency_key="fixed-key")
        second = service.create_post_draft("第一条", now=NOON, idempotency_key="fixed-key")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(first["result"]["post_id"], second["result"]["post_id"])
        self.assertEqual(1, int(self.db.query_one("SELECT COUNT(*) AS n FROM qzone_posts")["n"]))

    def test_runtime_action_creates_draft_only(self) -> None:
        runtime = Runtime(self.db, self.config)
        results = runtime.execute_actions(
            [TurnAction("post_qzone", {"content": "runtime 草稿"})],
            session_id="web:owner",
            reply="",
            now=NOON,
        )
        self.assertEqual("post_qzone", results[0]["tool"])
        self.assertTrue(results[0]["ok"])
        self.assertEqual("pending_approval", results[0]["result"]["status"])
        # Nothing may reach the outbox before the owner approves.
        self.assertEqual({}, runtime.outbox.stats())
        row = self.db.query_one("SELECT status FROM qzone_posts")
        self.assertEqual("draft", row["status"])

    def test_runtime_action_reports_gate_errors(self) -> None:
        self.config._data["qzone"]["read_only"] = True
        runtime = Runtime(self.db, self.config)
        results = runtime.execute_actions(
            [TurnAction("post_qzone", {"content": "runtime 草稿"})],
            session_id="web:owner",
            reply="",
            now=NOON,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual("qzone_read_only", results[0]["error"])


# ---------------------------------------------------------------- review
class ReviewTests(BaseWriteTest):
    def test_approve_enqueues_outbox_once(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("批准我吧", now=NOON)
        post_id = draft["result"]["post_id"]
        outcome = service.review_post(post_id, "approved", now=NOON)
        self.assertTrue(outcome["ok"])
        self.assertEqual("approved", outcome["post"]["status"])
        rows = self.db.query("SELECT * FROM outbox")
        self.assertEqual(1, len(rows))
        self.assertEqual(OUTBOX_KIND, rows[0]["kind"])
        self.assertEqual(f"qzone_post:{post_id}", rows[0]["idempotency_key"])
        self.assertEqual(POST_MAX_ATTEMPTS, int(rows[0]["max_attempts"]))
        self.assertEqual(NOON, float(rows[0]["available_at"]))
        # Approving twice changes nothing else: the row left draft already.
        again = service.review_post(post_id, "approved", now=NOON)
        self.assertFalse(again["ok"])
        self.assertEqual("not_draft", again["error"])
        self.assertEqual(1, len(self.db.query("SELECT * FROM outbox")))

    def test_reject_marks_and_never_enqueues(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("不要我", now=NOON)
        post_id = draft["result"]["post_id"]
        outcome = service.review_post(post_id, "rejected", now=NOON)
        self.assertTrue(outcome["ok"])
        self.assertEqual("rejected", outcome["post"]["status"])
        self.assertEqual({}, Outbox(self.db).stats())
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='qzone_post' AND action='rejected'"
        )
        self.assertIsNotNone(log)

    def test_unknown_post_returns_none(self) -> None:
        service = make_service(self.config, self.db)
        self.assertIsNone(service.review_post(999, "approved", now=NOON))

    def test_invalid_decision_raises(self) -> None:
        service = make_service(self.config, self.db)
        with self.assertRaises(ValueError):
            service.review_post(1, "maybe", now=NOON)

    def test_review_without_outbox_is_a_clear_error(self) -> None:
        service = QzonePostService(self.db, self.config)
        draft = service.create_post_draft("hi", now=NOON)
        with self.assertRaises(RuntimeError):
            service.review_post(draft["result"]["post_id"], "approved", now=NOON)


# ---------------------------------------------------------------- quota
class QuotaTests(BaseWriteTest):
    def test_approved_pending_reserves_today_slot(self) -> None:
        service = make_service(self.config, self.db)
        first = service.create_post_draft("第一条", now=NOON)
        service.review_post(first["result"]["post_id"], "approved", now=NOON)
        second = service.create_post_draft("第二条", now=NOON)
        outcome = service.review_post(second["result"]["post_id"], "approved", now=NOON)
        self.assertFalse(outcome["ok"])
        self.assertEqual("quota_exceeded", outcome["error"])
        self.assertEqual(1, outcome["quota"]["posted_today"])
        # The second draft stays pending so the owner can push it tomorrow.
        self.assertEqual(
            "draft", self.db.query_one(
                "SELECT status FROM qzone_posts WHERE id=?", (second["result"]["post_id"],)
            )["status"]
        )

    def test_posted_counts_towards_quota_and_next_day_resets(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("第一条", now=NOON)
        service.review_post(draft["result"]["post_id"], "approved", now=NOON)
        # Simulate the handler finishing.
        self.db.execute(
            "UPDATE qzone_posts SET status='posted', posted_at=? WHERE id=?",
            (NOON + 60, draft["result"]["post_id"]),
        )
        self.assertEqual(1, service.posts_today(now=NOON + 120))
        next_day = NOON + 86400
        self.assertEqual(0, service.posts_today(now=next_day))
        late = service.create_post_draft("明天的第一条", now=next_day)
        outcome = service.review_post(late["result"]["post_id"], "approved", now=next_day)
        self.assertTrue(outcome["ok"])

    def test_zero_quota_means_unlimited(self) -> None:
        # T-0023 (boss 2026-10-25): max_posts_per_day=0 no longer means "never
        # post"; it means no cap. The quota only bites when the value is > 0.
        self.config._data["qzone"]["max_posts_per_day"] = 0
        service = make_service(self.config, self.db)
        last = None
        for index in range(3):
            draft = service.create_post_draft(f"第{index + 1}条", now=NOON)
            last = service.review_post(draft["result"]["post_id"], "approved", now=NOON)
            self.assertTrue(last["ok"], last)
        self.assertEqual("owner", last["post"]["decided_by"])
        quota = service.quota_status(now=NOON)
        self.assertEqual(3, quota["posted_today"])
        self.assertTrue(quota["unlimited"])
        self.assertEqual(0, quota["max_posts_per_day"])


# ------------------------------------------------------------ quiet hours
class QuietHoursTests(BaseWriteTest):
    def test_real_default_window_covers_night(self) -> None:
        start, end = "23:30", "08:00"
        self.assertTrue(in_quiet_hours(NIGHT, start, end, self.config.timezone_name))
        self.assertFalse(in_quiet_hours(NOON, start, end, self.config.timezone_name))

    def test_auto_path_defers_to_quiet_end(self) -> None:
        self.config._data["qzone"]["require_approval"] = False
        self.config._data["qzone"]["quiet_hours_start"] = "23:30"
        self.config._data["qzone"]["quiet_hours_end"] = "08:00"
        service = make_service(self.config, self.db)
        result = service.create_post_draft("深夜想说的话", desire=0.9, now=NIGHT)
        self.assertTrue(result["ok"])
        self.assertEqual("queued", result["result"]["status"])
        row = self.db.query_one("SELECT * FROM qzone_posts")
        self.assertEqual("approved", row["status"])
        self.assertEqual("desire-threshold", row["decided_by"])
        outbox = self.db.query_one("SELECT * FROM outbox")
        self.assertGreater(float(outbox["available_at"]), NIGHT)
        # 08:00 next morning, not a random delta.
        wake = to_local(float(outbox["available_at"]), self.config.timezone_name)
        self.assertEqual(8, wake.hour)
        self.assertEqual(0, wake.minute)
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='qzone_post' AND action='deferred'"
        )
        self.assertIsNotNone(log)

    def test_daytime_auto_post_is_immediate(self) -> None:
        self.config._data["qzone"]["require_approval"] = False
        service = make_service(self.config, self.db)
        result = service.create_post_draft("白天想说的话", desire=0.9, now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("queued", result["result"]["status"])
        outbox = self.db.query_one("SELECT available_at FROM outbox")
        self.assertLessEqual(float(outbox["available_at"]), NOON + 1)

    def test_auto_path_quota_reject_marks_row_rejected(self) -> None:
        self.config._data["qzone"]["require_approval"] = False
        self.config._data["qzone"]["max_posts_per_day"] = 1
        service = make_service(self.config, self.db)
        first = service.create_post_draft("第一条", desire=0.9, now=NOON)
        self.assertTrue(first["ok"])
        second = service.create_post_draft("第二条", desire=0.9, now=NOON)
        self.assertFalse(second["ok"])
        self.assertEqual("quota_exceeded", second["error"])
        row = self.db.query_one(
            "SELECT status FROM qzone_posts WHERE id=?", (second["result"]["post_id"],)
        )
        self.assertEqual("rejected", row["status"])


# ---------------------------------------------------------------- desire
class DesireThresholdTests(BaseWriteTest):
    """The T-0023 share-desire matrix: her own rating decides only when
    review is explicitly off, and the default is always the safe side."""

    def _service(self) -> QzonePostService:
        self.config._data["qzone"]["require_approval"] = False
        self.config._data["qzone"]["max_posts_per_day"] = 0
        return make_service(self.config, self.db)

    def test_desire_at_threshold_auto_publishes(self) -> None:
        service = self._service()
        # 2026-11-15: auto_post_desire_threshold 0.7 -> 0.45 (boss lowered it).
        result = service.create_post_draft("憋不住了必须发", desire=0.45, now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("queued", result["result"]["status"])
        row = self.db.query_one("SELECT * FROM qzone_posts")
        self.assertEqual("approved", row["status"])
        self.assertAlmostEqual(0.45, row["desire"])
        self.assertEqual("desire-threshold", row["decided_by"])
        self.assertIsNotNone(row["decided_at"])
        # Audit trail records the rating and the threshold that let it out.
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='qzone_post' "
            "AND action='approved' AND outcome='desire_threshold'"
        )
        self.assertIsNotNone(log)
        output = json.loads(log["output_json"])
        self.assertAlmostEqual(0.45, output["desire"])
        self.assertAlmostEqual(0.45, output["threshold"])
        self.assertEqual(1, len(self.db.query("SELECT id FROM outbox")))

    def test_desire_below_threshold_stays_pending(self) -> None:
        service = self._service()
        result = service.create_post_draft("有点想说但没那么想", desire=0.44, now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("pending_approval", result["result"]["status"])
        row = self.db.query_one("SELECT * FROM qzone_posts")
        self.assertEqual("draft", row["status"])
        self.assertIsNone(row["decided_at"])
        self.assertEqual({}, Outbox(self.db).stats())

    def test_missing_desire_defaults_to_below_threshold(self) -> None:
        service = self._service()
        result = service.create_post_draft("没打分", now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("pending_approval", result["result"]["status"])
        row = self.db.query_one("SELECT desire FROM qzone_posts")
        self.assertIsNone(row["desire"])

    def test_invalid_desire_values_are_below_threshold(self) -> None:
        service = self._service()
        for bad in ("abc", None, True, False, 1.5, -0.1, float("nan"), [0.9], {"desire": 1}):
            result = service.create_post_draft(f"非法分 {bad!r}", desire=bad, now=NOON)
            self.assertTrue(result["ok"], bad)
            self.assertEqual("pending_approval", result["result"]["status"], bad)
        rows = self.db.query("SELECT desire FROM qzone_posts")
        self.assertTrue(all(row["desire"] is None for row in rows))
        self.assertEqual({}, Outbox(self.db).stats())

    def test_threshold_config_override_is_clamped(self) -> None:
        self._service()
        self.config._data["qzone"]["auto_post_desire_threshold"] = 1.5
        service = make_service(self.config, self.db)
        self.assertEqual(1.0, service.auto_post_desire_threshold)
        self.config._data["qzone"]["auto_post_desire_threshold"] = "not-a-number"
        service = make_service(self.config, self.db)
        self.assertEqual(0.7, service.auto_post_desire_threshold)

    def test_require_approval_true_records_desire_but_never_acts(self) -> None:
        # Default env: require_approval=true. Even a 1.0 rating must not skip
        # the owner while review is on.
        service = make_service(self.config, self.db)
        result = service.create_post_draft("再想发也要过审", desire=1.0, now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("pending_approval", result["result"]["status"])
        row = self.db.query_one("SELECT * FROM qzone_posts")
        self.assertEqual("draft", row["status"])
        self.assertAlmostEqual(1.0, row["desire"])
        self.assertEqual({}, Outbox(self.db).stats())

    def test_custom_threshold_from_config(self) -> None:
        self.config._data["qzone"]["require_approval"] = False
        self.config._data["qzone"]["auto_post_desire_threshold"] = 0.5
        service = make_service(self.config, self.db)
        result = service.create_post_draft("0.5 就够", desire=0.5, now=NOON)
        self.assertEqual("queued", result["result"]["status"])
        result = service.create_post_draft("0.49 不够", desire=0.49, now=NOON)
        self.assertEqual("pending_approval", result["result"]["status"])

    def test_owner_review_sets_decided_by(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("主人批的", desire=0.4, now=NOON)
        post_id = draft["result"]["post_id"]
        outcome = service.review_post(post_id, "approved", now=NOON)
        self.assertTrue(outcome["ok"])
        self.assertEqual("owner", outcome["post"]["decided_by"])
        rejected = service.create_post_draft("主人毙掉的", now=NOON)
        service.review_post(rejected["result"]["post_id"], "rejected", now=NOON)
        row = self.db.query_one(
            "SELECT decided_by FROM qzone_posts WHERE id=?", (rejected["result"]["post_id"],)
        )
        self.assertEqual("owner", row["decided_by"])

    def test_unlimited_quota_posts_many_times_in_a_day(self) -> None:
        # The core boss mandate: desire above threshold, no daily cap.
        service = self._service()
        for index in range(5):
            result = service.create_post_draft(f"第{index + 1}条想说的话", desire=0.8, now=NOON)
            self.assertTrue(result["ok"], result)
            self.assertEqual("queued", result["result"]["status"])
        self.assertEqual(5, len(self.db.query("SELECT id FROM outbox")))
        quota = service.quota_status(now=NOON)
        self.assertEqual(5, quota["posted_today"])
        self.assertTrue(quota["unlimited"])

    def test_runtime_action_passes_desire_to_draft(self) -> None:
        self._service()
        runtime = Runtime(self.db, self.config)
        results = runtime.execute_actions(
            [TurnAction("post_qzone", {"content": "runtime 想说", "desire": 0.9})],
            session_id="web:owner",
            reply="",
            now=NOON,
        )
        self.assertTrue(results[0]["ok"])
        self.assertEqual("queued", results[0]["result"]["status"])
        row = self.db.query_one("SELECT desire, decided_by FROM qzone_posts")
        self.assertAlmostEqual(0.9, row["desire"])
        self.assertEqual("desire-threshold", row["decided_by"])

    def test_runtime_action_without_desire_stays_draft(self) -> None:
        self._service()
        runtime = Runtime(self.db, self.config)
        results = runtime.execute_actions(
            [TurnAction("post_qzone", {"content": "runtime 没打分"})],
            session_id="web:owner",
            reply="",
            now=NOON,
        )
        self.assertTrue(results[0]["ok"])
        self.assertEqual("pending_approval", results[0]["result"]["status"])
        self.assertEqual({}, runtime.outbox.stats())


# ---------------------------------------------------------------- handler
class _StubClient:
    def __init__(self, results: list | Exception):
        self.results = results if isinstance(results, list) else [results]
        self.calls: list[str] = []

    def publish_text(self, content: str) -> dict:
        self.calls.append(content)
        outcome = self.results.pop(0) if self.results else {"ok": True, "tid": "t0"}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _outbox_item(service: QzonePostService, post_id: int, *, ahead: float = 5.0):
    # Claim ahead of the clock: mark_failed retry windows (retry_after=0.01)
    # are due by then, and a still-leased item is recovered like a dead
    # worker's lease (lease_seconds=1, so +30s always clears it).
    items = service._resolve_outbox().claim(now=time.time() + ahead)
    for item in items:
        payload = item.payload or {}
        if payload.get("post_id") == post_id:
            return item
    raise AssertionError("outbox item for post not found")


class HandlerTests(BaseWriteTest):
    def test_success_posts_and_audits(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("发布我", now=NOON)
        post_id = draft["result"]["post_id"]
        service.review_post(post_id, "approved", now=NOON)
        stub = _StubClient({"ok": True, "tid": "88", "url": "https://u/mood/88"})
        service._client = stub  # type: ignore[assignment]
        item = _outbox_item(service, post_id)
        result = service.execute_outbox_item(item)
        self.assertTrue(result["ok"])
        self.assertEqual("88", result["result"]["tid"])
        row = self.db.query_one("SELECT * FROM qzone_posts WHERE id=?", (post_id,))
        self.assertEqual("posted", row["status"])
        self.assertIsNotNone(row["posted_at"])
        self.assertEqual("88", json.loads(row["post_result_json"])["tid"])
        actions = [r["action"] for r in self.db.query(
            "SELECT action FROM decision_log WHERE kind='qzone_post' ORDER BY id"
        )]
        self.assertIn("publishing", actions)
        self.assertIn("posted", actions)

    def test_runtime_builtin_handler_processes_qzone_post(self) -> None:
        runtime = Runtime(self.db, self.config)
        service = runtime.qzone.posts
        draft = service.create_post_draft("内置通道", now=NOON)
        post_id = draft["result"]["post_id"]
        service.review_post(post_id, "approved", now=NOON)
        stub = _StubClient({"ok": True, "tid": "7"})
        service._client = stub  # type: ignore[assignment]
        results = runtime.process_outbox()
        self.assertEqual(1, len(results))
        self.assertEqual("done", results[0]["status"])
        self.assertEqual("posted", self.db.query_one(
            "SELECT status FROM qzone_posts WHERE id=?", (post_id,)
        )["status"])

    def test_failure_retries_then_terminal_failed(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("会失败的", now=NOON)
        post_id = draft["result"]["post_id"]
        service.review_post(post_id, "approved", now=NOON)
        stub = _StubClient([QzoneError("risk control", code=-123)] * POST_MAX_ATTEMPTS)
        service._client = stub  # type: ignore[assignment]
        outbox = service._resolve_outbox()
        # attempts 1..3: retry scheduled, row stays approved.
        for attempt in range(1, POST_MAX_ATTEMPTS):
            item = _outbox_item(service, post_id)
            self.assertEqual(attempt, item.attempts)
            result = service.execute_outbox_item(item)
            self.assertFalse(result["ok"])
            outbox.mark_failed(item.id, result["error"], retry_after=0.01)
            self.assertEqual(
                "approved", self.db.query_one(
                    "SELECT status FROM qzone_posts WHERE id=?", (post_id,)
                )["status"]
            )
        # attempts == max: terminal.
        item = _outbox_item(service, post_id)
        self.assertEqual(POST_MAX_ATTEMPTS, item.attempts)
        result = service.execute_outbox_item(item)
        self.assertFalse(result["ok"])
        outbox.mark_failed(item.id, result["error"], retry_after=0.01)
        row = self.db.query_one("SELECT * FROM qzone_posts WHERE id=?", (post_id,))
        self.assertEqual("failed", row["status"])
        self.assertIn("risk control", row["error"])
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='qzone_post' AND action='failed' "
            "AND outcome='terminal'"
        )
        self.assertIsNotNone(log)
        # The outbox item itself reached its dead-letter state.
        self.assertEqual({"dead": 1}, outbox.stats())

    def test_redelivery_after_post_is_skipped_not_double_posted(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("只发一次", now=NOON)
        post_id = draft["result"]["post_id"]
        service.review_post(post_id, "approved", now=NOON)
        stub = _StubClient({"ok": True, "tid": "1"})
        service._client = stub  # type: ignore[assignment]
        first = service.execute_outbox_item(_outbox_item(service, post_id))
        self.assertTrue(first["ok"])
        # A replayed item (lease recovery) must not publish again; the first
        # claim still holds a 1s lease, so look far enough past it.
        replay = _outbox_item(service, post_id, ahead=30.0)
        result = service.execute_outbox_item(replay)
        self.assertTrue(result["ok"])
        self.assertIn("skipped", result)
        self.assertEqual(1, len(stub.calls))

    def test_missing_post_and_bad_payload_fail_cleanly(self) -> None:
        service = make_service(self.config, self.db)

        class Item:
            payload = {"post_id": 4242}
            attempts = 1
            max_attempts = 4

        self.assertEqual(
            {"ok": False, "error": "post_missing"}, service.execute_outbox_item(Item())
        )

        class BadItem:
            payload = {"post_id": "not-a-number"}
            attempts = 1
            max_attempts = 4

        result = service.execute_outbox_item(BadItem())
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("invalid_post_id"))

    def test_unexpected_exception_is_reported_not_raised(self) -> None:
        service = make_service(self.config, self.db)
        draft = service.create_post_draft("炸一下", now=NOON)
        post_id = draft["result"]["post_id"]
        service.review_post(post_id, "approved", now=NOON)

        class Boom:
            def publish_text(self, content: str) -> dict:
                raise RuntimeError("boom")

        service._client = Boom()  # type: ignore[assignment]
        item = _outbox_item(service, post_id)
        result = service.execute_outbox_item(item)
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])
        # Not terminal: the row stays approved for the retry path.
        self.assertEqual(
            "approved", self.db.query_one(
                "SELECT status FROM qzone_posts WHERE id=?", (post_id,)
            )["status"]
        )


# ----------------------------------------------------------- publish client
class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self, payloads: list[bytes]):
        self.payloads = list(payloads)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None):
        self.requests.append(request)
        if not self.payloads:  # pragma: no cover - test bug guard
            raise AssertionError("no more fixture payloads")
        return _FakeResponse(self.payloads.pop(0))


def make_client(tmp: Path, payloads: list[bytes]) -> tuple[QzoneClient, _FakeOpener]:
    data = tmp / "data"
    toml = tmp / "c.toml"
    toml.write_text(
        f'[app]\ndata_dir = "{data.as_posix()}"\n'
        '[qzone]\ncookie_path = "qzone_cookies.json"\n',
        encoding="utf-8",
    )
    config = load_config("low", toml)
    config.ensure_dirs()
    client = QzoneClient(config, cookies=dict(COOKIE_FIELDS))
    client.request_interval = 0.0
    opener = _FakeOpener(payloads)
    client._opener = opener  # type: ignore[assignment]
    return client, opener


class PublishClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_publish_text_requests_token_then_posts_form(self) -> None:
        token_body = json.dumps(
            {"code": 0, "data": {"qzonetoken": "synthetic-token-abc"}}
        ).encode("utf-8")
        publish_body = json.dumps(
            {"code": 0, "message": "发表成功", "data": {"tid": "6688"}}
        ).encode("utf-8")
        client, opener = make_client(self.tmp, [token_body, publish_body])
        result = client.publish_text("出海啦")
        self.assertTrue(result["ok"])
        self.assertEqual("6688", result["tid"])
        self.assertIn("/mood/6688", result["url"])
        self.assertEqual(2, len(opener.requests))
        token_req, publish_req = opener.requests
        self.assertEqual(QZONETOKEN_URL, token_req.full_url.split("?")[0])
        self.assertEqual("GET", token_req.get_method())
        self.assertEqual(EMOTION_PUBLISH_URL, publish_req.full_url.split("?")[0])
        self.assertEqual("POST", publish_req.get_method())
        self.assertEqual(
            "application/x-www-form-urlencoded",
            publish_req.headers.get("Content-type"),
        )
        form = urllib.parse.parse_qs(publish_req.data.decode("utf-8"))
        self.assertEqual(["出海啦"], form["con"])
        self.assertEqual(["synthetic-token-abc"], form["qzonetoken"])
        self.assertEqual(["10000"], form["p_uin"])

    def test_publish_text_jsonp_envelopes_parse(self) -> None:
        token_body = b'_Callback({"code":0,"data":{"qzonetoken":"tok"}});'
        publish_body = b'_Callback({"code":0,"tid":"42"});'
        client, _opener = make_client(self.tmp, [token_body, publish_body])
        result = client.publish_text("hello")
        self.assertEqual("42", result["tid"])

    def test_publish_error_envelope_raises(self) -> None:
        token_body = b'_Callback({"code":0,"data":{"qzonetoken":"tok"}});'
        publish_body = b'_Callback({"code":-3000,"message":"not login"});'
        client, _opener = make_client(self.tmp, [token_body, publish_body])
        with self.assertRaises(QzoneError) as ctx:
            client.publish_text("hi")
        self.assertTrue(ctx.exception.login_required)

    def test_missing_token_is_a_hard_error(self) -> None:
        token_body = b'_Callback({"code":0,"data":{}});'
        client, opener = make_client(self.tmp, [token_body])
        with self.assertRaises(QzoneError):
            client.publish_text("hi")
        # The publish POST never fires without a token.
        self.assertEqual(1, len(opener.requests))

    def test_empty_content_and_missing_uin_rejected_locally(self) -> None:
        client, opener = make_client(self.tmp, [])
        with self.assertRaises(QzoneError):
            client.publish_text("   ")
        client.cookies = {"skey": "@x"}
        with self.assertRaises(QzoneError) as ctx:
            client.publish_text("hi")
        self.assertTrue(ctx.exception.login_required)
        self.assertEqual([], opener.requests)


# ---------------------------------------------------------------- web api
TOKEN = "test-token"


class QzoneWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fatfish.web.server import create_server

        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        data_dir = cls.tmp / "data"
        data_dir.mkdir(parents=True)
        cls._env = patch.dict(
            os.environ,
            {
                "FATFISH_DATA_DIR": str(data_dir),
                "FATFISH_DB": str(data_dir / "web.db"),
                "OPENCODE_API_KEY": "",
                "FATFISH_OPENCODE_KEY": "",
            },
        )
        cls._env.start()
        data = load_config("low").as_dict()
        data.setdefault("tts", {})["enabled"] = False
        data.setdefault("qzone", {}).update(
            {
                "enabled": True,
                "read_only": False,
                "require_approval": True,
                "max_posts_per_day": 1,
            }
        )
        cls.config = Config(data, profile="low")
        cls.config.ensure_dirs()
        db = Database(cls.config.db_path)
        db.initialize()
        db.close()
        cls.server = create_server(cls.config, host="127.0.0.1", port=0, token=TOKEN)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._env.stop()
        cls._tmp.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        cookie: bool = True,
    ) -> tuple[int, dict, bytes]:
        request_headers = dict(headers or {})
        if cookie:
            request_headers["Cookie"] = f"fatfish_token={TOKEN}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return response.status, payload, raw
        finally:
            connection.close()

    def _draft(self, content: str) -> int:
        with Database(self.config.db_path) as db:
            service = QzonePostService(db, self.config, outbox=Outbox(db))
            result = service.create_post_draft(content, now=NOON)
            assert result["ok"], result
            return int(result["result"]["post_id"])

    def test_posts_requires_token(self) -> None:
        status, payload, _raw = self._request("GET", "/api/qzone/posts", cookie=False)
        self.assertEqual(401, status)
        self.assertFalse(payload["ok"])

    def test_posts_lists_drafts_quota_and_write_status(self) -> None:
        post_id = self._draft("web 可见的草稿")
        status, payload, _raw = self._request("GET", "/api/qzone/posts")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertIn(post_id, [item["id"] for item in payload["drafts"]])
        self.assertIn(post_id, [item["id"] for item in payload["items"]])
        self.assertEqual(0, payload["quota"]["posted_today"])
        self.assertEqual(1, payload["quota"]["max_posts_per_day"])
        self.assertTrue(payload["write"]["write_enabled"])
        self.assertTrue(payload["write"]["require_approval"])
        for item in payload["items"]:
            self.assertNotIn("raw", item)

    def test_review_approve_reject_and_errors(self) -> None:
        post_id = self._draft("web 审批的草稿")
        status, payload, _raw = self._request(
            "POST",
            "/api/qzone/review",
            body={"id": post_id, "decision": "approved"},
            headers={"X-Fatfish-Request": "1"},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("approved", payload["post"]["status"])

        # Already decided: not an error, but the state machine refuses.
        status, payload, _raw = self._request(
            "POST",
            "/api/qzone/review",
            body={"id": post_id, "decision": "rejected"},
            headers={"X-Fatfish-Request": "1"},
        )
        self.assertEqual(200, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("not_draft", payload["error"])

    def test_review_rejects_unknown_and_invalid(self) -> None:
        status, payload, _raw = self._request(
            "POST",
            "/api/qzone/review",
            body={"id": 9999, "decision": "approved"},
            headers={"X-Fatfish-Request": "1"},
        )
        self.assertEqual(404, status)

        status, payload, _raw = self._request(
            "POST",
            "/api/qzone/review",
            body={"id": 1, "decision": "maybe"},
            headers={"X-Fatfish-Request": "1"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_decision", payload["error"])

        status, payload, _raw = self._request(
            "POST",
            "/api/qzone/review",
            body={"decision": "approved"},
            headers={"X-Fatfish-Request": "1"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_id", payload["error"])

    def test_review_requires_csrf_headers(self) -> None:
        status, payload, _raw = self._request(
            "POST", "/api/qzone/review", body={"id": 1, "decision": "approved"}
        )
        self.assertEqual(403, status)
        self.assertEqual("missing_request_header", payload["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
