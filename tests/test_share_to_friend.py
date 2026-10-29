"""T-0012 (v3): the ``share_to_friend`` action -- proactive private-chat
sharing to the ONE bound friend slot holder, delivered verbatim through the
outbox by the QQ adapter.

Offline only: the OneBot client is faked, every credential-looking string is
synthetic, and the databases live in temp dirs.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fatfish.config import Config, load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.owner import OwnerRegistry
from fatfish.platforms.qq import QQAdapter
from fatfish.runtime import ALLOWED_ACTIONS, Runtime, TurnAction, parse_turn_plan
from fatfish.security import TrustLevel

# 2026-10-24 14:00 / 23:50 in UTC+8: deterministic against both the real
# ZoneInfo and the Windows UTC+8 fallback in clock.get_timezone.
TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
NIGHT = datetime(2026, 10, 24, 23, 50, tzinfo=TZ8).timestamp()

FRIEND_UIN = "20001"
SHARE_TEXT = "姐姐！我刚看到一只海豚，尾巴跟本鲸鱼娘的一模一样！"


def make_env(tmp: Path, **overrides):
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "share_friend.db").as_posix()}"',
        '[memory]',
        'embedding_backend = "hash"',
        'embedding_dim = 64',
        '[share]',
        'max_to_friend_per_day = 2',
    ]
    for key, value in overrides.items():
        if isinstance(value, str):
            lines.append(f'{key} = "{value}"')
        else:
            lines.append(f'{key} = {value}')
    path = tmp / "share_friend.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


class ShareToFriendTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_env(self.tmp)
        self.registry = OwnerRegistry(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _bind_friend(self, session_id: str | None = None) -> None:
        self.registry.bind_friend(
            session_id or f"qq:private:{FRIEND_UIN}", "姐姐"
        )

    def _share(self, text: str = SHARE_TEXT, **kwargs):
        results = self.runtime.execute_actions(
            [TurnAction("share_to_friend", {"text": text, **kwargs})],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        return results[0]


# ------------------------------------------------------------- action policy
class ShareToFriendPolicyMatrixTests(ShareToFriendTestBase):
    def test_action_is_allowlisted_and_parses(self) -> None:
        self.assertIn("share_to_friend", ALLOWED_ACTIONS)
        plan = parse_turn_plan(
            '{"messages":["在吗"],"actions":[{"tool":"share_to_friend",'
            '"args":{"text":"给你看个东西","desire":0.6}}]}'
        )
        assert plan is not None
        self.assertTrue(plan.parsed)
        self.assertEqual(
            [
                TurnAction(
                    "share_to_friend", {"text": "给你看个东西", "desire": 0.6}
                )
            ],
            plan.actions,
        )

    def test_missing_text_is_rejected(self) -> None:
        for args in ({}, {"text": ""}, {"text": "   "}):
            results = self.runtime.execute_actions(
                [TurnAction("share_to_friend", args)],
                session_id="qq:private:1001",
                reply="",
                now=NOON,
            )
            self.assertFalse(results[0]["ok"], args)
            self.assertEqual("missing_text", results[0]["error"], args)
        self.assertEqual({}, self.runtime.outbox.stats())

    def test_group_trust_is_rejected(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("share_to_friend", {"text": SHARE_TEXT})],
            session_id="qq:group:5555",
            reply="",
            now=NOON,
            trust=TrustLevel.GROUP,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual("group_not_allowed", results[0]["error"])
        self.assertEqual({}, self.runtime.outbox.stats())

    def test_third_call_in_one_turn_is_rejected(self) -> None:
        self._bind_friend()
        results = self.runtime.execute_actions(
            [TurnAction("share_to_friend", {"text": "一"}),
             TurnAction("share_to_friend", {"text": "二"}),
             TurnAction("share_to_friend", {"text": "三"})],
            session_id="qq:private:1001",
            reply="",
            now=NOON,
        )
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertFalse(results[2]["ok"])
        self.assertEqual("too_many_tool_calls", results[2]["error"])


# --------------------------------------------------------------- scope
class ShareToFriendScopeTests(ShareToFriendTestBase):
    def test_no_friend_binding_is_refused_not_guessed(self) -> None:
        result = self._share()
        self.assertFalse(result["ok"])
        self.assertEqual("no_friend_bound", result["error"])
        self.assertEqual({}, self.runtime.outbox.stats())
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='friend_share' "
            "AND outcome='no_friend_bound'"
        )
        self.assertIsNotNone(log)

    def test_binding_without_a_qq_session_is_refused(self) -> None:
        # A binding whose session id cannot yield a QQ uin (e.g. a web
        # session) is unreachable: refuse instead of guessing a target.
        self._bind_friend("web:local")
        result = self._share()
        self.assertFalse(result["ok"])
        self.assertEqual("no_friend_bound", result["error"])
        self.assertEqual({}, self.runtime.outbox.stats())

    def test_friend_binding_receives_the_share(self) -> None:
        self._bind_friend()
        result = self._share()
        self.assertTrue(result["ok"], result)
        self.assertEqual(FRIEND_UIN, result["result"]["friend_uin"])
        row = self.db.query_one("SELECT * FROM outbox")
        self.assertEqual("friend_share", row["kind"])
        payload = json.loads(row["payload_json"])
        self.assertEqual(SHARE_TEXT, payload["text"])
        self.assertEqual(FRIEND_UIN, payload["friend_uin"])
        self.assertTrue(row["idempotency_key"].startswith("friend_share:"))
        # Daytime share is due immediately.
        self.assertEqual(NOON, float(row["available_at"]))

    def test_desire_is_recorded_in_the_audit_only(self) -> None:
        self._bind_friend()
        result = self._share(desire="garbage")
        self.assertTrue(result["ok"])
        payload = json.loads(
            self.db.query_one("SELECT payload_json FROM outbox")["payload_json"]
        )
        self.assertNotIn("desire", payload)
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='friend_share' "
            "AND action='share' AND outcome='queued'"
        )
        self.assertIsNotNone(log)
        output = json.loads(log["output_json"])
        self.assertIsNone(output.get("desire"))

        self.db.execute("DELETE FROM outbox")
        self.db.execute("DELETE FROM decision_log")
        result = self._share(desire=0.7)
        self.assertTrue(result["ok"])
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='friend_share' "
            "AND action='share' AND outcome='queued'"
        )
        self.assertIsNotNone(log)
        output = json.loads(log["output_json"])
        self.assertEqual(0.7, output["desire"])

    def test_audit_never_stores_the_text(self) -> None:
        # The decision_log row carries metadata only: the share text rides
        # the outbox payload, not the audit trail.
        self._bind_friend()
        self._share()
        log = self.db.query_one(
            "SELECT output_json FROM decision_log WHERE kind='friend_share'"
        )
        self.assertNotIn(SHARE_TEXT, str(log["output_json"]))


# ------------------------------------------------------------- content gate
class ShareToFriendContentGateTests(ShareToFriendTestBase):
    def test_credential_text_is_blocked(self) -> None:
        self._bind_friend()
        result = self._share("我的cookie是 skey=@SYNTHETIC-VALUE-123 帮我发给姐姐")
        self.assertFalse(result["ok"])
        self.assertEqual("sensitive_content", result["error"])
        self.assertEqual({}, self.runtime.outbox.stats())
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='friend_share' "
            "AND action='blocked' AND outcome='sensitive_content'"
        )
        self.assertIsNotNone(log)
        # The blocked text itself must not land in the audit trail either.
        self.assertNotIn("@SYNTHETIC-VALUE-123", str(log["output_json"]))

    def test_secret_note_text_is_blocked(self) -> None:
        from fatfish.privacy import SecretNotes

        self._bind_friend()
        SecretNotes(self.db).add("我家住在幸福小区3栋502")
        result = self._share("姐姐我今天路过我家住在幸福小区3栋502的门口")
        self.assertFalse(result["ok"])
        self.assertEqual("sensitive_content", result["error"])
        self.assertEqual({}, self.runtime.outbox.stats())

    def test_ordinary_text_passes(self) -> None:
        self._bind_friend()
        result = self._share("姐姐今天出海了吗，鲸鱼娘想你了")
        self.assertTrue(result["ok"])


# ------------------------------------------------------------- daily quota
class ShareToFriendQuotaTests(ShareToFriendTestBase):
    def test_daily_cap_counts_queued_shares(self) -> None:
        self._bind_friend()
        first = self._share("第一条想分享的事")
        second = self._share("第二条想分享的事")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        third = self._share("第三条想分享的事")
        self.assertFalse(third["ok"])
        self.assertEqual("quota_exceeded", third["error"])
        self.assertEqual(2, len(self.db.query("SELECT id FROM outbox")))
        log = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='friend_share' "
            "AND outcome='quota_exceeded'"
        )
        self.assertIsNotNone(log)

    def test_zero_cap_means_unlimited(self) -> None:
        self.config._data["share"]["max_to_friend_per_day"] = 0
        self._bind_friend()
        for index in range(4):
            result = self._share(f"第{index + 1}条")
            self.assertTrue(result["ok"], (index, result))
        self.assertEqual(4, len(self.db.query("SELECT id FROM outbox")))

    def test_quota_resets_next_day(self) -> None:
        self._bind_friend()
        self.assertTrue(self._share("第一条")["ok"])
        # Same day, different text: consumes the second slot.
        self.assertTrue(self._share("第二条")["ok"])
        # Next day the counter starts over.
        next_day = NOON + 86400
        results = self.runtime.execute_actions(
            [TurnAction("share_to_friend", {"text": "明天的第一条"})],
            session_id="qq:private:1001",
            reply="",
            now=next_day,
        )
        self.assertTrue(results[0]["ok"], results)

    def test_same_text_same_day_is_a_duplicate_not_a_new_share(self) -> None:
        self._bind_friend()
        first = self._share(SHARE_TEXT)
        second = self._share(SHARE_TEXT)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["result"].get("duplicate"))
        self.assertEqual(
            first["result"]["outbox_id"], second["result"]["outbox_id"]
        )
        self.assertEqual(1, len(self.db.query("SELECT id FROM outbox")))


# ------------------------------------------------------------- quiet hours
class ShareToFriendQuietHoursTests(ShareToFriendTestBase):
    def test_night_share_defers_to_quiet_end(self) -> None:
        from fatfish.clock import to_local

        self._bind_friend()
        results = self.runtime.execute_actions(
            [TurnAction("share_to_friend", {"text": SHARE_TEXT})],
            session_id="qq:private:1001",
            reply="",
            now=NIGHT,
        )
        self.assertTrue(results[0]["ok"], results)
        row = self.db.query_one("SELECT available_at FROM outbox")
        wake = to_local(float(row["available_at"]), self.config.timezone_name)
        self.assertEqual((8, 0), (wake.hour, wake.minute))
        log = self.db.query_one(
            "SELECT output_json FROM decision_log WHERE kind='friend_share' "
            "AND outcome='queued'"
        )
        self.assertTrue(json.loads(log["output_json"]).get("deferred"))

    def test_daytime_share_is_immediate(self) -> None:
        self._bind_friend()
        self._share()
        row = self.db.query_one("SELECT available_at FROM outbox")
        self.assertLessEqual(float(row["available_at"]), NOON + 1)


# ------------------------------------------------------------- delivery
class _FakeOneBotClient:
    def __init__(self, *args, **kwargs):
        self.connected = True
        self.sent: list[tuple[str, str, str]] = []

    def connect(self):
        return self

    def close(self):
        self.connected = False

    def send_private_text(self, user_id, text):
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def send_group_text(self, group_id, text):
        self.sent.append(("group", str(group_id), str(text)))
        return "echo-group"


class ShareToFriendDeliveryTests(ShareToFriendTestBase):
    def _deliver(self):
        adapter = QQAdapter(self.runtime, self.config)
        adapter.client_factory = _FakeOneBotClient  # type: ignore[assignment]
        results = self.runtime.process_outbox(handlers=adapter.handlers())
        return adapter, results

    def test_friend_share_reaches_the_bound_friend_verbatim(self) -> None:
        self._bind_friend()
        result = self._share(SHARE_TEXT)
        self.assertTrue(result["ok"])
        adapter, delivery = self._deliver()
        self.assertEqual(1, len(delivery), delivery)
        self.assertEqual("done", delivery[0]["status"])
        self.assertEqual("friend_share", delivery[0]["kind"])
        client = adapter.client
        assert isinstance(client, _FakeOneBotClient)
        self.assertEqual([("private", FRIEND_UIN, SHARE_TEXT)], client.sent)

    def test_missing_friend_uin_payload_fails_cleanly(self) -> None:
        # A payload without friend_uin (should not exist) is a handler error,
        # never a guess at a recipient. The handler refuses before it ever
        # builds a client, so nothing can be sent.
        self.runtime.outbox.enqueue(
            "friend_share", {"text": "orphan"}, idempotency_key="friend_share:orphan"
        )
        _adapter, delivery = self._deliver()
        self.assertEqual("failed", delivery[0]["status"])
        self.assertEqual("missing_friend_uin", delivery[0]["error"])

    def test_owner_proactive_path_is_unaffected(self) -> None:
        # The friend share rides its own kind: the existing
        # proactive_message flow for the owner keeps working untouched.
        self.runtime.outbox.enqueue(
            "proactive_message",
            {"text": "主人哥哥在吗", "session_id": "qq:private:1001"},
            idempotency_key="proactive:t0",
        )
        self._bind_friend()
        self._share(SHARE_TEXT)
        adapter, delivery = self._deliver()
        self.assertEqual(2, len(delivery), delivery)
        self.assertTrue(all(r["status"] == "done" for r in delivery), delivery)
        client = adapter.client
        assert isinstance(client, _FakeOneBotClient)
        self.assertEqual(
            [("private", "1001", "主人哥哥在吗"), ("private", FRIEND_UIN, SHARE_TEXT)],
            sorted(client.sent, key=lambda row: row[1]),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
