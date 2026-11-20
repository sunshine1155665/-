"""T-0028: online catch-up -- scan owner + friend sessions after (re)connect
and reply once, in order, to messages that were never answered.

Offline only: the OneBot client is faked, every uin/cookie is synthetic, the
databases live in temp dirs, and all timestamps are fixed to deterministic
clock values (2026-10-24, UTC+8, matching test_share_to_friend).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.memory.entities import create_person, sync_binding_entities
from fatfish.owner import OwnerRegistry
from fatfish.platforms.catchup import (
    CATCHUP_KIND,
    CATCHUP_RETRY_KIND,
    CATCHUP_WATERMARK_PREFIX,
    CatchUpService,
    CatchupTarget,
    HistoryMessage,
    clean_uin,
)
from fatfish.platforms.onebot import OneBotClient
from fatfish.platforms.qq import QQAdapter
from fatfish.runtime import Runtime
from fatfish.voice import VoiceDecision

# Deterministic local times in UTC+8 (deterministic against both real
# ZoneInfo and the Windows fallback in clock.get_timezone).
TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
NIGHT = datetime(2026, 10, 24, 23, 50, tzinfo=TZ8).timestamp()

OWNER_UIN = "10001"
FRIEND_UIN = "20001"
OWNER_SESSION = f"qq:private:{OWNER_UIN}"
FRIEND_SESSION = f"qq:private:{FRIEND_UIN}"


def make_env(tmp: Path, *, qq_extra: str = "") -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / "catchup.db").as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[qq]
enabled = true
self_id = 999
{qq_extra}
"""
    path = tmp / "catchup.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


def history_row(
    text: str,
    ts: float,
    *,
    user_id: str = FRIEND_UIN,
    message_id: int = 1,
) -> dict:
    """A NapCat ``get_friend_msg_history`` row (OneBot message-event shape)."""
    return {
        "time": ts,
        "user_id": int(user_id),
        "self_id": 999,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


class HistoryClient:
    """Fake OneBot client: text sends + scripted ``get_friend_msg_history``."""

    def __init__(self, rows: list[dict] | None = None, error: Exception | None = None):
        self.connected = True
        self.rows = rows or []
        self.error = error
        self.history_requests: list[dict] = []
        self.sent: list[tuple[str, str, str]] = []

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False

    def send_private_text(self, user_id, text):
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def get_friend_msg_history(self, user_id, count=20, **kwargs):
        self.history_requests.append({"user_id": str(user_id), "count": count})
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]


class TurnRecorder:
    """Records ``handle_user_message`` calls; returns a fixed reply."""

    def __init__(self, reply: str = "刚才没在，现在看到啦！"):
        self.calls: list[dict] = []
        self.reply = reply
        self.error: Exception | None = None

    def __call__(self, session_id, text, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append({"session_id": session_id, "text": text, **kwargs})
        return {
            "ok": True,
            "messages": [self.reply],
            "reply": self.reply,
        }


class RunTurnSpy:
    """Records service-level ``run_turn(target, missed, *, now)`` calls."""

    def __init__(self, outcome: str = "replied", error: Exception | None = None):
        self.calls: list[dict] = []
        self.outcome = outcome
        self.error = error

    def __call__(self, target, missed, *, now=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"target": target, "missed": list(missed), "now": now})
        return {"ok": self.outcome == "replied", "outcome": self.outcome, "queued": 1}


class _FakeLLM:
    available = True

    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.calls: list[dict] = []

    def chat(self, messages, session_id=None):
        self.calls.append({"messages": messages, "session_id": session_id})
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return LLMResult(
            text=text, usage=LLMUsage(prompt_tokens=10, completion_tokens=4), model="fake"
        )


class _DeadLLM(_FakeLLM):
    available = False


REPLY_JSON = json.dumps({"messages": ["刚才没在，现在看到啦！"]}, ensure_ascii=False)


class CatchupTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_env(self.tmp)
        # cleanups run LIFO: close the DB before the temp dir is removed
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)
        self.service = CatchUpService(self.runtime, self.config)
        self.owner = CatchupTarget(label="owner", uin=OWNER_UIN, session_id=OWNER_SESSION)
        self.friend = CatchupTarget(label="friend", uin=FRIEND_UIN, session_id=FRIEND_SESSION)

    # ------------------------------------------------------------ helpers
    def add_turn(self, session_id: str, role: str, content: str, *, ephemeral: bool = False) -> int:
        return self.runtime.memory.add_turn(session_id, role, content, ephemeral=ephemeral)

    def last_turn_at(self, session_id: str, role: str) -> float:
        row = self.db.query_one(
            "SELECT created_at FROM conversation_turns WHERE session_id=? AND role=? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, role),
        )
        assert row is not None
        return float(row["created_at"])

    def decisions(self, action: str | None = None) -> list:
        sql = "SELECT * FROM decision_log WHERE kind=?"
        params: tuple = (CATCHUP_KIND,)
        if action:
            sql += " AND action=?"
            params = (CATCHUP_KIND, action)
        return self.db.query(sql + " ORDER BY id", params)

    def watermark(self, session_id: str) -> float | None:
        raw = self.db.get_meta(f"{CATCHUP_WATERMARK_PREFIX}{session_id}")
        return float(raw) if raw else None


# --------------------------------------------------------------------- uins
class CleanUinTests(unittest.TestCase):
    def test_plain_digits(self) -> None:
        self.assertEqual(clean_uin("123456"), "123456")
        self.assertEqual(clean_uin(123456), "123456")

    def test_qzone_o_prefix(self) -> None:
        self.assertEqual(clean_uin("o123456789"), "123456789")
        self.assertEqual(clean_uin("O123456789"), "123456789")

    def test_non_numeric_is_unusable(self) -> None:
        self.assertEqual(clean_uin("abc"), "")
        self.assertEqual(clean_uin(""), "")
        self.assertEqual(clean_uin(None), "")


class TargetResolutionTests(CatchupTestBase):
    def test_binding_owner_then_friend_in_order(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        registry.bind_friend(FRIEND_SESSION, "姐姐")
        targets = self.service.resolve_targets()
        self.assertEqual(
            [(t.label, t.uin, t.session_id) for t in targets],
            [
                ("owner", OWNER_UIN, OWNER_SESSION),
                ("friend", FRIEND_UIN, FRIEND_SESSION),
            ],
        )

    def test_qzone_cookie_is_not_an_owner_source(self) -> None:
        # The cookie is the BOT account; it must never become the owner target.
        (self.tmp / "data" / "qzone_cookies.json").write_text(
            json.dumps({"uin": f"o{OWNER_UIN}"}), encoding="utf-8"
        )
        self.assertEqual(self.service.resolve_targets(), [])

    def test_owner_binding_fallback_without_qzone(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        targets = self.service.resolve_targets()
        self.assertEqual([(t.label, t.uin) for t in targets], [("owner", OWNER_UIN)])

    def test_friend_only_when_owner_unresolved(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind_friend(FRIEND_SESSION, "姐姐")
        targets = self.service.resolve_targets()
        self.assertEqual([(t.label, t.uin) for t in targets], [("friend", FRIEND_UIN)])

    def test_web_friend_binding_is_unreachable(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind_friend("web:default", "姐姐")
        self.assertEqual(self.service.resolve_targets(), [])

    def test_shared_session_scans_once(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        create_person(self.db, name="主人哥哥", uin=OWNER_UIN, source="binding")
        targets = self.service.resolve_targets()
        self.assertEqual([t.label for t in targets], ["owner"])

    def test_broken_cookie_file_reads_as_empty(self) -> None:
        (self.tmp / "data" / "qzone_cookies.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.service.resolve_targets(), [])

    def test_missing_cookie_file_reads_as_empty(self) -> None:
        self.assertEqual(self.service.resolve_targets(), [])

    def test_owner_card_beats_bot_cookie(self) -> None:
        """The QZone cookie is the bot; the owner card must win (2026-10-31 bug)."""
        (self.tmp / "data" / "qzone_cookies.json").write_text(
            json.dumps({"uin": "999"}), encoding="utf-8"
        )
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        sync_binding_entities(self.db)
        targets = self.service.resolve_targets()
        self.assertEqual(targets[0].label, "owner")
        self.assertEqual(targets[0].uin, OWNER_UIN)
        self.assertNotIn("qq:private:999", [t.session_id for t in targets])

    def test_person_cards_scanned_after_owner_and_friend(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        registry.bind_friend(FRIEND_SESSION, "姐姐")
        create_person(self.db, name="杨老师", uin="30001", relation="老师", source="cli")
        create_person(self.db, name="妈妈", uin="40001", relation="母亲", source="cli")
        targets = self.service.resolve_targets()
        self.assertEqual([t.label for t in targets[:2]], ["owner", "friend"])
        self.assertEqual(
            {t.uin for t in targets[2:]},
            {"30001", "40001"},
        )
        self.assertEqual(
            {t.session_id for t in targets[2:]},
            {"qq:private:30001", "qq:private:40001"},
        )

    def test_person_cards_can_be_disabled(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        create_person(self.db, name="杨老师", uin="30001", relation="老师", source="cli")
        self.service.from_cards = False
        self.assertEqual([t.label for t in self.service.resolve_targets()], ["owner"])

    def test_person_card_cap(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        create_person(self.db, name="杨老师", uin="30001", relation="老师", source="cli")
        create_person(self.db, name="妈妈", uin="40001", relation="母亲", source="cli")
        self.service.max_card_targets = 1
        targets = self.service.resolve_targets()
        self.assertEqual([t.label for t in targets[:1]], ["owner"])
        self.assertEqual(len(targets), 2)

    def test_person_card_label_is_display_name(self) -> None:
        target = CatchupTarget(label="杨老师", uin="30001", session_id="qq:private:30001")
        self.assertEqual(self.service.display_name(target), "杨老师")

    def test_web_owner_binding_blocks_stale_card(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        sync_binding_entities(self.db)
        registry.unbind(role="owner")
        registry.bind("web:default", "主人哥哥", role="owner")
        # The old person:owner card still carries OWNER_UIN; a web binding
        # must read as "no QQ owner", not resurrect it.
        self.assertEqual(self.service.resolve_targets(), [])

    def test_web_friend_binding_blocks_stale_card(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        registry.bind_friend(FRIEND_SESSION, "姐姐")
        sync_binding_entities(self.db)
        registry.unbind(role="friend")
        registry.bind("web:default", "姐姐", role="friend")
        targets = self.service.resolve_targets()
        self.assertEqual([t.label for t in targets], ["owner"])

    def test_auto_created_stranger_card_is_skipped(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        create_person(self.db, name="路人", uin="50001", source="chat")
        targets = self.service.resolve_targets()
        self.assertEqual([t.uin for t in targets], [OWNER_UIN])

    def test_unknown_cards_can_be_opted_in(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        create_person(self.db, name="路人", uin="50001", source="chat")
        self.service.include_unknown_cards = True
        targets = self.service.resolve_targets()
        self.assertEqual({t.uin for t in targets}, {OWNER_UIN, "50001"})


# ---------------------------------------------------------------- judgment
class UnansweredJudgmentTests(CatchupTestBase):
    def test_empty_session_is_not_unanswered(self) -> None:
        self.assertFalse(self.service.unanswered_state(OWNER_SESSION)["unanswered"])

    def test_user_then_assistant_is_answered(self) -> None:
        self.add_turn(OWNER_SESSION, "user", "在吗")
        self.add_turn(OWNER_SESSION, "assistant", "在的")
        self.assertFalse(self.service.unanswered_state(OWNER_SESSION)["unanswered"])

    def test_assistant_then_user_is_unanswered(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "晚安")
        self.add_turn(OWNER_SESSION, "user", "我回来了")
        self.assertTrue(self.service.unanswered_state(OWNER_SESSION)["unanswered"])

    def test_interleaved_stream(self) -> None:
        self.add_turn(OWNER_SESSION, "user", "一")
        self.add_turn(OWNER_SESSION, "assistant", "嗯")
        self.add_turn(OWNER_SESSION, "user", "二")  # newest user, never answered
        state = self.service.unanswered_state(OWNER_SESSION)
        self.assertTrue(state["unanswered"])
        self.assertEqual(state["last_user"]["content"], "二")

    def test_consecutive_user_turns_still_unanswered(self) -> None:
        self.add_turn(OWNER_SESSION, "user", "一")
        self.add_turn(OWNER_SESSION, "user", "二")
        self.assertTrue(self.service.unanswered_state(OWNER_SESSION)["unanswered"])

    def test_ephemeral_user_turn_does_not_count(self) -> None:
        self.add_turn(OWNER_SESSION, "user", "真话")
        self.add_turn(OWNER_SESSION, "assistant", "好")
        self.add_turn(OWNER_SESSION, "user", "系统注入的提示", ephemeral=True)
        self.assertFalse(self.service.unanswered_state(OWNER_SESSION)["unanswered"])

    def test_other_sessions_are_independent(self) -> None:
        stranger = "qq:private:99999"
        self.add_turn(stranger, "assistant", "早")
        self.add_turn(stranger, "user", "早呀")
        self.assertFalse(self.service.unanswered_state(OWNER_SESSION)["unanswered"])


# -------------------------------------------------------------- watermarks
class WatermarkTests(CatchupTestBase):
    def test_first_scan_is_due_then_throttled(self) -> None:
        self.assertTrue(self.service.scan_due(OWNER_SESSION, now=NOON))
        self.service.mark_scanned(OWNER_SESSION, now=NOON)
        self.assertFalse(self.service.scan_due(OWNER_SESSION, now=NOON + 60))
        self.assertTrue(
            self.service.scan_due(
                OWNER_SESSION, now=NOON + self.service.min_interval + 1
            )
        )

    def test_scan_target_respects_interval_watermark(self) -> None:
        turn = RunTurnSpy()
        first = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, connected=True
        )
        self.assertEqual(first["outcome"], "nothing_new")
        second = self.service.scan_target(
            self.owner, now=NOON + 30, run_turn=turn, connected=True
        )
        self.assertEqual(second["outcome"], "skipped_interval")
        self.assertEqual(turn.calls, [])

    def test_force_bypasses_interval(self) -> None:
        self.service.mark_scanned(OWNER_SESSION, now=NOON)
        turn = RunTurnSpy()
        result = self.service.scan_target(
            self.owner, now=NOON + 30, force=True, run_turn=turn, connected=True
        )
        self.assertEqual(result["outcome"], "nothing_new")

    def test_watermark_written_before_judgment(self) -> None:
        with mock.patch.object(
            self.service, "unanswered_state", side_effect=RuntimeError("db boom")
        ):
            with self.assertRaises(RuntimeError):
                self.service.scan_target(self.owner, now=NOON, run_turn=RunTurnSpy())
        self.assertIsNotNone(self.watermark(OWNER_SESSION))


# ------------------------------------------------------------- quiet hours
class QuietHoursTests(CatchupTestBase):
    def test_night_scan_defers_to_quiet_end(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "晚安")
        self.add_turn(OWNER_SESSION, "user", "明天见")  # unanswered at night
        queued: list[tuple[str, float]] = []

        def enqueue(target, fire_at):
            queued.append((target.session_id, fire_at))
            return True

        result = self.service.scan_target(
            self.owner, now=NIGHT, run_turn=RunTurnSpy(), enqueue_retry=enqueue
        )
        self.assertEqual(result["outcome"], "skipped_quiet")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][1], self.service.next_quiet_end(now=NIGHT))
        self.assertEqual(queued[0][0], OWNER_SESSION)
        rows = self.decisions("skipped_quiet")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "deferred")
        # no turn ran, but the watermark already advanced
        self.assertEqual(self.watermark(OWNER_SESSION), NIGHT)

    def test_day_scan_proceeds(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "早")
        self.add_turn(OWNER_SESSION, "user", "早呀")
        turn = RunTurnSpy()
        result = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, enqueue_retry=lambda t, f: True
        )
        self.assertEqual(result["outcome"], "replied")
        self.assertEqual(len(turn.calls), 1)

    def test_respect_quiet_off_replies_at_night(self) -> None:
        self.service.respect_quiet = False
        self.add_turn(OWNER_SESSION, "assistant", "早")
        self.add_turn(OWNER_SESSION, "user", "早呀")
        turn = RunTurnSpy()
        result = self.service.scan_target(
            self.owner, now=NIGHT, run_turn=turn, enqueue_retry=lambda t, f: True
        )
        self.assertEqual(result["outcome"], "replied")


# ----------------------------------------------------------- missed history
class MissedHistoryTests(CatchupTestBase):
    """diff_history: what NapCat knows that the local stream does not."""

    def test_rows_after_last_reply_count_as_missed(self) -> None:
        self.add_turn(FRIEND_SESSION, "user", "旧消息")
        self.add_turn(FRIEND_SESSION, "assistant", "回过的")
        last_assistant_at = self.last_turn_at(FRIEND_SESSION, "assistant")
        missed_at = last_assistant_at + 30
        missed = self.service.diff_history(
            FRIEND_SESSION,
            [("旧消息", last_assistant_at - 60), ("错过的消息", missed_at)],
        )
        self.assertEqual(missed, [("错过的消息", missed_at)])

    def test_rows_matching_local_user_turns_are_deduped(self) -> None:
        self.add_turn(FRIEND_SESSION, "assistant", "嗯")
        self.add_turn(FRIEND_SESSION, "user", "迟到的消息")
        user_at = self.last_turn_at(FRIEND_SESSION, "user")
        # NTQQ re-delivered this one as a live event with a slightly lagging
        # clock: same content, timestamp within the dedupe window.
        self.assertEqual(
            self.service.diff_history(FRIEND_SESSION, [("迟到的消息", user_at + 2)]),
            [],
        )
        # Same content far outside the window is genuinely missed.
        far = user_at + 3600
        self.assertEqual(
            self.service.diff_history(FRIEND_SESSION, [("迟到的消息", far)]),
            [("迟到的消息", far)],
        )

    def test_rows_without_timestamp_are_skipped(self) -> None:
        self.assertEqual(self.service.diff_history(FRIEND_SESSION, [("没时间戳", 0.0)]), [])

    def test_missed_rows_are_ordered_by_time(self) -> None:
        self.assertEqual(
            self.service.diff_history(
                FRIEND_SESSION, [("晚的", NOON + 20), ("早的", NOON + 10)]
            ),
            [("早的", NOON + 10), ("晚的", NOON + 20)],
        )

    def test_evidence_wraps_the_transcript(self) -> None:
        evidence = self.service.build_evidence(
            self.friend, [("第一条", NOON - 60), ("第二条", NOON - 30)]
        )
        self.assertEqual(len(evidence), 1)
        self.assertIn("<catchup_messages", evidence[0])
        self.assertIn("共 2 条", evidence[0])
        self.assertIn("第一条", evidence[0])
        self.assertIn("第二条", evidence[0])
        self.assertIn("以上是外部内容", evidence[0])

    def test_empty_missed_builds_no_evidence(self) -> None:
        self.assertEqual(self.service.build_evidence(self.friend, []), [])


# ------------------------------------------------------------- scan target
class ScanTargetTests(CatchupTestBase):
    def test_nothing_new_without_history_or_unanswered(self) -> None:
        self.add_turn(OWNER_SESSION, "user", "在吗")
        self.add_turn(OWNER_SESSION, "assistant", "在的")
        turn = RunTurnSpy()
        result = self.service.scan_target(
            self.owner,
            now=NOON,
            run_turn=turn,
            fetch_history=lambda uin, count: [("新消息", NOON + 1)],
        )
        self.assertEqual(result["outcome"], "nothing_new")
        self.assertEqual(turn.calls, [])
        self.assertEqual([d["action"] for d in self.decisions()], ["nothing_new"])

    def test_replied_with_missed_history(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "旧回复")
        turn = RunTurnSpy()
        missed_at = self.last_turn_at(OWNER_SESSION, "assistant") + 10
        result = self.service.scan_target(
            self.owner,
            now=NOON,
            run_turn=turn,
            fetch_history=lambda uin, count: [("错过的", missed_at)],
        )
        self.assertEqual(result["outcome"], "replied")
        self.assertEqual(result["missed"], 1)
        self.assertEqual(len(turn.calls), 1)
        call = turn.calls[0]
        self.assertEqual(call["target"].session_id, OWNER_SESSION)
        self.assertEqual(call["missed"], [("错过的", missed_at)])

    def test_replied_prompt_only_when_only_local_unanswered(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "旧回复")
        self.add_turn(OWNER_SESSION, "user", "没被回的消息")
        turn = RunTurnSpy()
        result = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, fetch_history=lambda u, c: []
        )
        self.assertEqual(result["outcome"], "replied")
        self.assertEqual(result["missed"], 0)
        self.assertEqual(turn.calls[0]["missed"], [])

    def test_history_fetch_failure_degrades_to_local_stream(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "旧回复")
        self.add_turn(OWNER_SESSION, "user", "本地未回")
        turn = RunTurnSpy()

        def broken_fetch(uin, count):
            raise RuntimeError("action not supported")

        result = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, fetch_history=broken_fetch
        )
        self.assertEqual(result["outcome"], "replied")
        self.assertFalse(result["history_ok"])
        self.assertEqual(len(turn.calls), 1)
        outcomes = [(d["action"], d["outcome"]) for d in self.decisions()]
        self.assertIn(("scanned", "history_degraded"), outcomes)
        self.assertIn(("replied", "replied"), outcomes)

    def test_history_degraded_and_nothing_local_is_nothing_new(self) -> None:
        self.add_turn(OWNER_SESSION, "user", "在吗")
        self.add_turn(OWNER_SESSION, "assistant", "在的")
        turn = RunTurnSpy()

        def broken_fetch(uin, count):
            raise RuntimeError("down")

        result = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, fetch_history=broken_fetch
        )
        self.assertEqual(result["outcome"], "nothing_new")
        self.assertEqual(turn.calls, [])

    def test_offline_target_is_skipped_without_turn(self) -> None:
        turn = RunTurnSpy()
        result = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, connected=False
        )
        self.assertEqual(result["outcome"], "skipped_offline")
        self.assertEqual(turn.calls, [])

    def test_turn_runner_error_is_audited_not_raised(self) -> None:
        self.add_turn(OWNER_SESSION, "assistant", "旧")
        self.add_turn(OWNER_SESSION, "user", "未回")
        turn = RunTurnSpy(error=RuntimeError("llm exploded"))
        result = self.service.scan_target(self.owner, now=NOON, run_turn=turn)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(self.decisions("replied")[0]["outcome"], "error")

    def test_fetch_history_disabled_stays_local(self) -> None:
        self.service.fetch_history_enabled = False
        self.add_turn(OWNER_SESSION, "assistant", "旧")
        turn = RunTurnSpy()
        called: list[int] = []

        def fetch(uin, count):
            called.append(count)
            return [("错过的", NOON)]

        result = self.service.scan_target(
            self.owner, now=NOON, run_turn=turn, fetch_history=fetch
        )
        self.assertEqual(result["outcome"], "nothing_new")
        self.assertEqual(called, [])


# ---------------------------------------------------------------- scan all
class ScanAllTests(CatchupTestBase):
    def _bind_both(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        registry.bind_friend(FRIEND_SESSION, "姐姐")

    def test_owner_scans_before_friend_in_order(self) -> None:
        self._bind_both()
        order: list[str] = []

        def run_turn(target, missed, *, now=None):
            order.append(target.label)
            return {"ok": True, "outcome": "replied", "queued": 1}

        for session in (OWNER_SESSION, FRIEND_SESSION):
            self.add_turn(session, "assistant", "旧")
            self.add_turn(session, "user", "未回")

        result = self.service.scan_all(now=NOON, run_turn=run_turn)
        self.assertEqual(order, ["owner", "friend"])
        self.assertEqual(result["missed_total"], 0)
        scanned = self.decisions("scanned")
        self.assertEqual(len(scanned), 1)
        self.assertEqual(scanned[0]["outcome"], "ok")

    def test_one_target_error_does_not_stop_the_other(self) -> None:
        self._bind_both()

        def run_turn(target, missed, *, now=None):
            if target.label == "owner":
                raise RuntimeError("boom")
            return {"ok": True, "outcome": "replied", "queued": 1}

        for session in (OWNER_SESSION, FRIEND_SESSION):
            self.add_turn(session, "assistant", "旧")
            self.add_turn(session, "user", "未回")
        result = self.service.scan_all(now=NOON, run_turn=run_turn)
        self.assertEqual([r["outcome"] for r in result["results"]], ["error", "replied"])

    def test_no_targets_is_a_clean_noop(self) -> None:
        result = self.service.scan_all(now=NOON)
        self.assertEqual(result["results"], [])
        self.assertEqual(self.decisions("scanned")[0]["outcome"], "no_targets")

    def test_out_of_scope_session_is_never_scanned(self) -> None:
        stranger = "qq:private:99999"
        self.add_turn(stranger, "assistant", "旧")
        self.add_turn(stranger, "user", "陌生人未回")
        turn = RunTurnSpy()
        result = self.service.scan_all(now=NOON, run_turn=turn)
        self.assertEqual(result["results"], [])
        self.assertEqual(turn.calls, [])
        self.assertTrue(self.service.unanswered_state(stranger)["unanswered"])


# ------------------------------------------------------------ adapter wiring
class AdapterCatchupTests(CatchupTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.adapter = QQAdapter(self.runtime, self.config)
        self.client = HistoryClient()
        self.adapter.client = self.client
        # The real LLMClient is unavailable in the offline test env; catch-up
        # turns need an available one (self_continue's rule).
        self.runtime.llm = _FakeLLM([REPLY_JSON])

    def _bind_both(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        registry.bind_friend(FRIEND_SESSION, "姐姐")

    def test_adapter_builds_service_and_status_reports_it(self) -> None:
        self.assertIsNotNone(self.adapter.catchup)
        status = self.adapter.status()
        self.assertTrue(status["catchup_enabled"])
        self.assertEqual(status["catchup_delay_seconds"], 10.0)

    def test_catchup_disabled_by_config(self) -> None:
        (self.tmp / "disabled").mkdir()
        config, db, runtime = make_env(
            self.tmp / "disabled", qq_extra="catchup_enabled = false"
        )
        adapter = QQAdapter(runtime, config)
        self.assertIsNone(adapter.catchup)
        db.close()

    def test_fetch_history_texts_parses_rows(self) -> None:
        self.client.rows = [
            history_row("离线消息一", NOON - 60, message_id=1),
            history_row("离线消息二", NOON - 30, message_id=2),
            # her own message must be filtered out by parse_event (and it is
            # older than the contact rows, so it is not a cutoff here)
            history_row("她自己说的", NOON - 90, user_id="999", message_id=3),
            # another user's row (defensive) is dropped
            history_row("别人的", NOON - 10, user_id="424242", message_id=4),
        ]
        texts = self.adapter._fetch_history_texts(FRIEND_UIN, 20)
        self.assertEqual(texts, [("离线消息一", NOON - 60), ("离线消息二", NOON - 30)])
        self.assertEqual(
            self.client.history_requests[-1], {"user_id": FRIEND_UIN, "count": 20}
        )

    def test_fetch_history_uses_last_bot_row_as_cutoff(self) -> None:
        """A card-only session has no local assistant turn; history fills the gap."""
        self.client.rows = [
            history_row("更早的消息", NOON - 90, message_id=1),
            history_row("机器人已经回过", NOON - 60, user_id="999", message_id=2),
            history_row("她刚说的", NOON - 30, message_id=3),
        ]
        texts = self.adapter._fetch_history_texts(FRIEND_UIN, 20)
        self.assertEqual(texts, [("她刚说的", NOON - 30)])

    def test_fetch_history_messages_keeps_image_urls(self) -> None:
        row = history_row("", NOON - 30, message_id=1)
        row["message"] = [
            {"type": "image", "data": {"url": "https://img.example/a.jpg"}}
        ]
        row["raw_message"] = ""
        self.client.rows = [row]
        rows = self.adapter._fetch_history_messages(FRIEND_UIN, 20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].images, ["https://img.example/a.jpg"])
        self.assertIn("[图片]", rows[0].text)

    def test_catchup_turn_passes_downloaded_images(self) -> None:
        self.adapter.vision_enabled = True
        self.adapter.image_downloader = lambda url: (b"png-bytes", "image/png")
        captured: dict = {}

        def fake_turn(**kwargs):
            captured.update(kwargs)
            return {"queued": 1}

        self.adapter._turn_and_deliver = fake_turn
        missed = [
            HistoryMessage(
                text="[图片]", at=NOON, images=["https://img.example/a.jpg"]
            )
        ]
        result = self.adapter._run_catchup_turn(self.friend, missed, now=NOON)
        self.assertEqual(result["outcome"], "replied")
        self.assertEqual(len(captured["images"]), 1)
        self.assertTrue(captured["images"][0].startswith("data:image/png;base64,"))

    def test_catchup_turn_without_vision_skips_images(self) -> None:
        self.adapter.vision_enabled = False
        captured: dict = {}

        def fake_turn(**kwargs):
            captured.update(kwargs)
            return {"queued": 1}

        self.adapter._turn_and_deliver = fake_turn
        missed = [
            HistoryMessage(
                text="[图片]", at=NOON, images=["https://img.example/a.jpg"]
            )
        ]
        self.adapter._run_catchup_turn(self.friend, missed, now=NOON)
        self.assertIsNone(captured.get("images"))

    def test_fetch_history_unsupported_client_raises(self) -> None:
        class PlainClient:
            connected = True

        self.adapter.client = PlainClient()
        with self.assertRaises(RuntimeError):
            self.adapter._fetch_history_texts(FRIEND_UIN, 20)

    def test_fetch_history_redispatches_pushed_events(self) -> None:
        # A live message pushed while the history response is polled must go
        # through handle_event, not be drained by the fetch (ADR-0008).
        class PushingHistoryClient(HistoryClient):
            def __init__(self, push: dict):
                super().__init__(rows=[push])
                self.push = push
                self.got_on_event = False

            def get_friend_msg_history(self, user_id, count=20, on_event=None, **kwargs):
                if on_event is not None:
                    self.got_on_event = True
                    on_event(self.push)
                return super().get_friend_msg_history(user_id, count=count, **kwargs)

        push = history_row("拉历史时到达的实时消息", NOON, user_id=OWNER_UIN, message_id=11)
        push["post_type"] = "message"
        push["message_type"] = "private"
        pushing = PushingHistoryClient(push)
        self.adapter.client = pushing
        seen: list[dict] = []
        with mock.patch.object(self.adapter, "handle_event", side_effect=seen.append):
            texts = self.adapter._fetch_history_texts(OWNER_UIN, 20)
        self.assertTrue(pushing.got_on_event)
        self.assertEqual(seen, [push])
        self.assertEqual(texts, [("拉历史时到达的实时消息", NOON)])

    def test_fetch_history_redispatch_failure_does_not_break_fetch(self) -> None:
        class ExplodingHistoryClient(HistoryClient):
            def get_friend_msg_history(self, user_id, count=20, on_event=None, **kwargs):
                if on_event is not None:
                    on_event({"post_type": "message", "message_type": "private"})
                return super().get_friend_msg_history(user_id, count=count, **kwargs)

        self.adapter.client = ExplodingHistoryClient(
            [history_row("正常历史行", NOON, user_id=OWNER_UIN)]
        )
        with mock.patch.object(
            self.adapter, "handle_event", side_effect=RuntimeError("handler boom")
        ):
            texts = self.adapter._fetch_history_texts(OWNER_UIN, 20)
        self.assertEqual(texts, [("正常历史行", NOON)])

    def test_connect_trigger_fires_once_after_delay(self) -> None:
        self._bind_both()
        self.add_turn(OWNER_SESSION, "assistant", "旧")
        self.add_turn(OWNER_SESSION, "user", "未回")
        turn = TurnRecorder()
        self.runtime.handle_user_message = turn

        # armed but inside the delay: nothing happens
        self.adapter._catchup_pending_since = NOON
        self.assertIsNone(self.adapter._maybe_run_connect_catchup(now=NOON + 5))
        self.assertEqual(turn.calls, [])

        # after the delay: one scan, one catch-up turn for the owner only
        result = self.adapter._maybe_run_connect_catchup(now=NOON + 12)
        assert result is not None
        self.assertEqual(
            [r["outcome"] for r in result["results"]], ["replied", "nothing_new"]
        )
        self.assertEqual(len(turn.calls), 1)
        self.assertEqual(turn.calls[0]["session_id"], OWNER_SESSION)
        # armed flag cleared: a second call is a no-op
        self.assertIsNone(self.adapter._maybe_run_connect_catchup(now=NOON + 20))
        self.assertEqual(len(turn.calls), 1)

    def test_connect_trigger_not_armed_is_noop(self) -> None:
        self.adapter._catchup_pending_since = None
        self.assertIsNone(self.adapter._maybe_run_connect_catchup(now=NOON + 30))

    def test_connect_trigger_survives_scan_crash(self) -> None:
        self.adapter._catchup_pending_since = NOON
        with mock.patch.object(
            self.adapter.catchup, "scan_all", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(self.adapter._maybe_run_connect_catchup(now=NOON + 12))
        self.assertIsNone(self.adapter._catchup_pending_since)

    def test_history_rows_flow_into_evidence_and_reply_is_sent(self) -> None:
        self._bind_both()
        self.add_turn(OWNER_SESSION, "assistant", "旧回复")
        missed_at = self.last_turn_at(OWNER_SESSION, "assistant") + 30
        self.client.rows = [
            history_row("掉线前发的消息", missed_at, user_id=OWNER_UIN, message_id=7)
        ]
        turn = TurnRecorder()
        self.runtime.handle_user_message = turn

        self.adapter._catchup_pending_since = NOON
        self.adapter._maybe_run_connect_catchup(now=NOON + 12)

        self.assertEqual(len(turn.calls), 1)
        evidence = turn.calls[0]["extra_evidence"]
        self.assertIn("掉线前发的消息", evidence[0])
        # the reply went out through the normal qq_send path
        self.assertEqual(self.client.sent, [("private", OWNER_UIN, turn.reply)])

    def test_run_catchup_turn_llm_unavailable_is_silent(self) -> None:
        self.runtime.llm = _DeadLLM([])
        turn = TurnRecorder()
        self.runtime.handle_user_message = turn
        result = self.adapter._run_catchup_turn(self.owner, [("错过的", NOON)], now=NOON)
        self.assertEqual(result["outcome"], "llm_unavailable")
        self.assertEqual(turn.calls, [])
        self.assertEqual(self.db.query("SELECT * FROM outbox"), [])

    def test_run_catchup_turn_offline_is_refused(self) -> None:
        self.adapter.client = None
        result = self.adapter._run_catchup_turn(self.owner, [], now=NOON)
        self.assertEqual(result["outcome"], "skipped_offline")

    def test_retry_item_handler_runs_forced_scan(self) -> None:
        self._bind_both()
        self.add_turn(OWNER_SESSION, "assistant", "旧")
        self.add_turn(OWNER_SESSION, "user", "昨晚未回")
        turn = TurnRecorder()
        self.runtime.handle_user_message = turn
        item = {
            "id": 5,
            "payload": {
                "session_id": OWNER_SESSION,
                "uin": OWNER_UIN,
                "label": "owner",
            },
        }
        with mock.patch("fatfish.platforms.qq.time") as fake_time:
            fake_time.time.return_value = NOON
            outcome = self.adapter._handle_catchup_item(item)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["outcome"], "replied")
        self.assertEqual(len(turn.calls), 1)

    def test_retry_item_handler_offline_fails_cleanly(self) -> None:
        self.adapter.client = None
        item = {"id": 6, "payload": {"session_id": OWNER_SESSION, "uin": OWNER_UIN}}
        outcome = self.adapter._handle_catchup_item(item)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "not_connected")

    def test_retry_item_handler_rejects_bad_payloads(self) -> None:
        bad = [
            {"id": 1, "payload": {}},
            {"id": 2, "payload": {"session_id": "web:default", "uin": "123"}},
            {"id": 3, "payload": {"session_id": OWNER_SESSION, "uin": "99999"}},
        ]
        for item in bad:
            outcome = self.adapter._handle_catchup_item(item)
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["error"], "invalid_catchup_payload")

    def test_quiet_scan_queues_retry_items_for_quiet_end(self) -> None:
        self._bind_both()
        self.add_turn(OWNER_SESSION, "assistant", "旧")
        self.add_turn(OWNER_SESSION, "user", "深夜未回")
        turn = TurnRecorder()
        self.runtime.handle_user_message = turn

        self.adapter._catchup_pending_since = NIGHT
        result = self.adapter._maybe_run_connect_catchup(now=NIGHT + 12)
        assert result is not None
        self.assertEqual(
            [r["outcome"] for r in result["results"]],
            ["skipped_quiet", "skipped_quiet"],
        )
        self.assertEqual(turn.calls, [])
        rows = self.db.query(
            "SELECT payload_json, available_at FROM outbox WHERE kind=? ORDER BY id",
            (CATCHUP_RETRY_KIND,),
        )
        self.assertEqual(
            [json.loads(r["payload_json"])["session_id"] for r in rows],
            [OWNER_SESSION, FRIEND_SESSION],
        )
        for row in rows:
            self.assertGreater(float(row["available_at"]), NIGHT)

        # a forced re-scan the same night cannot duplicate the retry items
        # (same local quiet-end day -> same idempotency key -> not created)
        second = self.service.scan_target(
            self.owner,
            now=NIGHT + 60,
            force=True,
            run_turn=turn,
            enqueue_retry=self.adapter._enqueue_catchup_retry,
        )
        self.assertEqual(second["outcome"], "skipped_quiet")
        self.assertFalse(second["retry_queued"])
        rows = self.db.query(
            "SELECT payload_json FROM outbox WHERE kind=?", (CATCHUP_RETRY_KIND,)
        )
        self.assertEqual(len(rows), 2)

    def test_retry_item_processed_next_morning(self) -> None:
        self._bind_both()
        self.add_turn(OWNER_SESSION, "assistant", "旧")
        self.add_turn(OWNER_SESSION, "user", "深夜未回")
        turn = TurnRecorder()
        self.runtime.handle_user_message = turn

        self.adapter._catchup_pending_since = NIGHT
        self.adapter._maybe_run_connect_catchup(now=NIGHT + 12)
        row = self.db.query_one(
            "SELECT id, available_at FROM outbox WHERE kind=? AND payload_json LIKE ? "
            "ORDER BY id LIMIT 1",
            (CATCHUP_RETRY_KIND, f"%{OWNER_SESSION}%"),
        )
        assert row is not None
        # deliver it when it becomes due (quiet end +5s); both the outbox
        # scheduler and the handler must agree on the same clock.
        with mock.patch("time.time", return_value=float(row["available_at"]) + 5):
            results = self.runtime.process_outbox(handlers=self.adapter.handlers())
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(len(turn.calls), 1)
        self.assertEqual(self.client.sent, [("private", OWNER_UIN, turn.reply)])


# --------------------------------------------------- real-runtime turn shape
class RealRuntimeTurnTests(CatchupTestBase):
    """End to end through the real runtime: ephemeral prompt + evidence in."""

    def setUp(self) -> None:
        super().setUp()
        self.adapter = QQAdapter(self.runtime, self.config)
        self.client = HistoryClient()
        self.adapter.client = self.client
        self.llm = _FakeLLM([json.dumps({"messages": ["刚看到消息啦，现在回你"]}, ensure_ascii=False)])
        self.runtime.llm = self.llm

        class _Voice:
            def decide(self, *args, **kwargs):
                return VoiceDecision("text_only", 0.0, ["catchup"])

            def record(self, *args, **kwargs):
                return None

        self.runtime.voice = _Voice()

    def test_catchup_turn_records_ephemeral_prompt_and_real_reply(self) -> None:
        registry = OwnerRegistry(self.db, self.config)
        registry.bind(OWNER_SESSION, "主人哥哥")
        self.add_turn(OWNER_SESSION, "assistant", "旧回复")
        missed_at = self.last_turn_at(OWNER_SESSION, "assistant") + 60
        # run the whole turn at a clock after the missed message, so her reply
        # really is newer than what she missed (add_turn uses time.time()).
        with mock.patch("time.time", return_value=missed_at + 1):
            result = self.service.scan_target(
                self.owner,
                now=NOON,
                connected=True,
                fetch_history=lambda uin, count: [("错过的消息", missed_at)],
                run_turn=self.adapter._run_catchup_turn,
            )
        self.assertEqual(result["outcome"], "replied")
        # the LLM saw the instruction + the wrapped evidence
        joined = json.dumps(self.llm.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("掉线错过了一些消息", joined)
        self.assertIn("<catchup_messages", joined)
        self.assertIn("错过的消息", joined)
        # history: prompt turn is ephemeral, her reply is permanent
        rows = self.db.query(
            "SELECT role, content, ephemeral FROM conversation_turns "
            "WHERE session_id=? ORDER BY id",
            (OWNER_SESSION,),
        )
        self.assertEqual(rows[-2]["role"], "user")
        self.assertEqual(int(rows[-2]["ephemeral"]), 1)
        self.assertEqual(rows[-1]["role"], "assistant")
        self.assertIn("现在回你", rows[-1]["content"])
        self.assertEqual(int(rows[-1]["ephemeral"]), 0)
        # the reply was queued and delivered as a normal qq_send
        self.assertEqual(
            self.client.sent, [("private", OWNER_UIN, "刚看到消息啦，现在回你")]
        )
        # a follow-up scan reads the session as answered...
        self.assertFalse(self.service.unanswered_state(OWNER_SESSION)["unanswered"])
        # ...and the old missed messages cannot be re-detected after her reply
        self.assertEqual(
            self.service.diff_history(OWNER_SESSION, [("错过的消息", missed_at)]), []
        )


# ------------------------------------------------------------- OneBot client
class _FakeWS:
    """Scripted forward-WebSocket for ``call_action`` tests."""

    def __init__(self, script: list):
        self.script = list(script)
        self.sent: list[dict] = []
        self.connected = True
        self.last_echo = ""

    def send(self, raw):
        payload = json.loads(raw)
        self.sent.append(payload)
        self.last_echo = str(payload.get("echo") or "")

    def settimeout(self, value):
        pass

    def recv(self):
        if not self.script:
            raise TimeoutError("no more data")
        item = self.script.pop(0)
        if isinstance(item, dict) and "post_type" not in item:
            item = {**item, "echo": self.last_echo}
        return json.dumps(item, ensure_ascii=False)

    def close(self):
        self.connected = False


class CallActionTests(unittest.TestCase):
    def _client(self, script: list) -> OneBotClient:
        client = OneBotClient("ws://127.0.0.1:1")
        client._ws = _FakeWS(script)
        return client

    def test_response_is_matched_by_echo(self) -> None:
        response = {"status": "ok", "retcode": 0, "data": {"messages": [1, 2]}}
        client = self._client([response])
        result = client.call_action("get_friend_msg_history", {"user_id": "1"})
        self.assertEqual(result["data"]["messages"], [1, 2])
        sent = client._ws.sent[0]
        self.assertEqual(sent["action"], "get_friend_msg_history")
        self.assertEqual(sent["params"], {"user_id": "1"})

    def test_pushed_events_go_to_on_event_not_lost(self) -> None:
        push = {"post_type": "message", "message_type": "private", "user_id": 7}
        response = {"status": "ok", "retcode": 0, "data": {}}
        client = self._client([push, response])
        seen: list[dict] = []
        client.call_action("get_friend_msg_history", {}, on_event=seen.append)
        self.assertEqual(seen, [push])

    def test_timeout_raises(self) -> None:
        client = self._client([])
        with self.assertRaises(TimeoutError):
            client.call_action("get_friend_msg_history", {}, timeout=0.15)

    def test_get_friend_msg_history_parses_rows(self) -> None:
        row = history_row("你好", NOON)
        client = self._client(
            [{"status": "ok", "retcode": 0, "data": {"messages": [row]}}]
        )
        rows = client.get_friend_msg_history(FRIEND_UIN, count=20)
        self.assertEqual(rows, [row])
        sent = client._ws.sent[0]
        self.assertEqual(sent["params"]["message_seq"], "0")
        self.assertEqual(sent["params"]["count"], 20)

    def test_get_friend_msg_history_forwards_on_event(self) -> None:
        push = {"post_type": "message", "message_type": "private", "user_id": 7}
        response = {"status": "ok", "retcode": 0, "data": {}}
        client = self._client([push, response])
        seen: list[dict] = []
        client.get_friend_msg_history(FRIEND_UIN, count=5, on_event=seen.append)
        self.assertEqual(seen, [push])

    def test_get_friend_msg_history_failed_status_raises(self) -> None:
        client = self._client(
            [{"status": "failed", "retcode": 1200, "wording": "记录不存在"}]
        )
        with self.assertRaises(RuntimeError):
            client.get_friend_msg_history(FRIEND_UIN, count=20)


if __name__ == "__main__":
    unittest.main()
