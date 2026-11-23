"""T-0025: the 30-minute self-decision turn (activity rhythm engine).

Offline only: the LLM and the Bilibili collector are faked, no network, every
database lives in a temp dir. The rhythm has two layers, tested separately:

* collection -- ``maybe_browse_bilibili`` (watermark + interval, like the
  QQ Zone poll);
* decision -- ``maybe_activity_turn`` (interval watermark, quiet hours, daily
  quota, the no-content shortcut, the wrapped data menu, owner-binding
  lifecycle, share wakeup retargeting, JSONL ledger).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.tts import TTSResult
from fatfish.owner import OwnerRegistry
from fatfish.photo import wants_photo
from fatfish.runtime import (
    DEFAULT_ACTIVITY_DECISION_PROMPT,
    Runtime,
    llm_routing_session,
    wants_qzone_review,
)
from fatfish.voice import VoiceDecision

# 2026-10-24 14:00 / 23:31 / 08:00 local (UTC+8): deterministic against both
# real ZoneInfo and the Windows UTC+8 fallback in clock.get_timezone.
TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
QUIET = datetime(2026, 10, 24, 23, 31, tzinfo=TZ8).timestamp()
MORNING = datetime(2026, 10, 24, 8, 0, tzinfo=TZ8).timestamp()

OWNER_UIN = "10001"
FRIEND_UIN = "20001"

PLAN = json.dumps(
    {
        "messages": ["这轮我看了看新帖子"],
        "actions": [
            {"tool": "remember", "args": {"text": "主人哥哥今天发了新帖子"}}
        ],
    },
    ensure_ascii=False,
)
REST_PLAN = json.dumps({"messages": ["休息"]}, ensure_ascii=False)
SHARE_OWNER_PLAN = json.dumps(
    {
        "messages": ["主人哥哥！我看到一个好好看的东西"],
        "actions": [
            {
                "tool": "share_with_owner",
                "args": {"text": "主人哥哥！我看到一个好好看的东西", "desire": 0.8},
            }
        ],
    },
    ensure_ascii=False,
)
SHARE_FRIEND_PLAN = json.dumps(
    {
        "messages": ["姐姐你看这个"],
        "actions": [
            {"tool": "share_to_friend", "args": {"text": "姐姐你看这个", "desire": 0.6}}
        ],
    },
    ensure_ascii=False,
)


class FakeLLM:
    available = True

    def __init__(self, text: str = PLAN, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages, session_id=None):
        self.calls.append({"messages": messages, "session_id": session_id})
        if self.error is not None:
            raise self.error
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=4),
            model="fake-model",
        )


class BindingSpyLLM(FakeLLM):
    """Records how many temporary activity bindings exist while she talks."""

    def __init__(self, db: Database, text: str = PLAN):
        super().__init__(text=text)
        self.db = db
        self.bindings_during: int | None = None

    def chat(self, messages, session_id=None):
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM owner_bindings WHERE source='activity'"
        )
        self.bindings_during = int(row["n"]) if row else 0
        return super().chat(messages, session_id=session_id)


class FakeVoicePolicy:
    def decide(self, text, **kwargs):
        return VoiceDecision("voice_only", 1.0, ["fake"])

    def record(self, decision, **kwargs):
        pass


class FakeTTS:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(self, text, *, emotion=None):
        self.calls.append(text)
        return TTSResult(True, audio_path="full.wav")

    def synthesize_segments(self, text, *, emotion=None):
        # short_sentence_mode=true path in low.toml
        self.calls.append(text)
        return [(text[:10], TTSResult(True, audio_path="seg.wav"))]


class FakeBiliCollector:
    def __init__(self, cards=2, error: Exception | None = None):
        self.error = error
        self.calls: list[int] = []
        self.cards = [
            SimpleNamespace(bvid=f"BV{i}", title=f"视频{i}") for i in range(cards)
        ]

    def browse_once(self, limit: int = 3):
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return list(self.cards)


def make_env(
    tmp: Path,
    *,
    activity_enabled: str = "true",
    bilibili_enabled: str = "false",
    friends_to_llm: str = "false",
    bind_owner: bool = True,
    bind_friend: bool = False,
    activity_extra: dict | None = None,
):
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "activity.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        "[qzone]",
        "enabled = false",
        'cookie_path = "qzone_cookies.json"',
        f'owner_uin = "{OWNER_UIN}"',
        f"friends_content_to_llm = {friends_to_llm}",
        "[activity]",
        f"enabled = {activity_enabled}",
        "interval_minutes = 30",
        'ledger_path = "logs/activity-{date}.jsonl"',
    ]
    for key, value in (activity_extra or {}).items():
        if isinstance(value, str):
            lines.append(f'{key} = "{value}"')
        else:
            lines.append(f"{key} = {value}")
    lines += ["[bilibili]", f"enabled = {bilibili_enabled}"]
    path = tmp / "activity.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    runtime = Runtime(db, config)
    if bind_owner:
        OwnerRegistry(db, config).bind(f"qq:private:{OWNER_UIN}", "主人哥哥")
    if bind_friend:
        OwnerRegistry(db, config).bind_friend(f"qq:private:{FRIEND_UIN}", "姐姐")
    return config, db, runtime


class ActivityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_env(self, **kwargs):
        config, db, runtime = make_env(self.tmp, **kwargs)
        self.addCleanup(db.close)
        return config, db, runtime

    def seed_feed(
        self,
        db: Database,
        external_id: str,
        content: str,
        *,
        uin: str = OWNER_UIN,
        scope: str = "self",
        seen_at: float = NOON - 600,
    ) -> None:
        db.execute(
            "INSERT INTO qzone_feed_items(platform, external_id, author_uin, author_name, "
            "author_scope, content, seen_at) VALUES ('qzone',?,?,?,?,?,?)",
            (external_id, uin, "主人" if scope == "self" else "好友", scope, content, seen_at),
        )

    def seed_bili(self, db: Database, source_id: str, title: str) -> None:
        db.execute(
            "INSERT INTO memory_items(id, gist, summary, observed_at, source_type, "
            "source_id, evidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, title[:100], title, NOON - 300, "bilibili", source_id,
             f"{title} @ UP主", NOON - 300, NOON - 300),
        )

    def seed_draft(self, db: Database) -> None:
        db.execute(
            "INSERT INTO qzone_posts(content, status, created_at) VALUES (' draft ', 'draft', ?)",
            (NOON - 3600,),
        )

    def seed_friend_share(self, db: Database, at: float) -> None:
        db.execute(
            "INSERT INTO decision_log(at, kind, output_json, action, outcome, model) "
            "VALUES (?, 'friend_share', '{}', 'share', 'queued', 'runtime')",
            (at,),
        )

    def ledger_rows(self, config, now: float = NOON) -> list[dict]:
        path = config.data_dir / "logs" / "activity-20261024.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class DisabledAndGatingTests(ActivityTestBase):
    def test_disabled_returns_none_and_never_sets_watermark(self) -> None:
        config, db, runtime = self.make_env(activity_enabled="false")
        runtime.llm = FakeLLM()
        self.assertIsNone(runtime.maybe_activity_turn(now=NOON))
        self.assertIsNone(db.get_meta("last_activity_turn_at"))
        db.close()

    def test_interval_watermark_prevents_repeat(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        first = runtime.maybe_activity_turn(now=NOON)
        self.assertTrue(first and first["ok"])
        self.assertEqual("act", first["decision"])
        self.assertIsNotNone(db.get_meta("last_activity_turn_at"))
        # Within the interval: no second turn.
        self.assertIsNone(runtime.maybe_activity_turn(now=NOON + 29 * 60))
        # After the interval: fires again.
        second = runtime.maybe_activity_turn(now=NOON + 31 * 60)
        self.assertTrue(second and second["ok"])
        db.close()

    def test_quiet_hours_skip_without_watermark_then_morning_fire(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        # 23:31 is inside the default quiet window: no turn, watermark untouched.
        self.assertIsNone(runtime.maybe_activity_turn(now=QUIET))
        self.assertIsNone(db.get_meta("last_activity_turn_at"))
        # 08:00 sharp: the first tick of the day fires a turn immediately.
        morning = runtime.maybe_activity_turn(now=MORNING)
        self.assertTrue(morning and morning["ok"])
        self.assertIsNotNone(db.get_meta("last_activity_turn_at"))
        db.close()

    def test_broken_turn_sets_watermark_no_tight_loop(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM(error=RuntimeError("boom"))
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])
        self.assertIsNotNone(db.get_meta("last_activity_turn_at"))
        self.assertIsNone(runtime.maybe_activity_turn(now=NOON + 60))
        db.close()


class DecisionTurnTests(ActivityTestBase):
    def test_llm_routing_session_maps_ephemeral_activity_sessions(self) -> None:
        # 网关按 x-opencode-session 做粘性路由/缓存分区：activity 每轮换 id
        # 会永远冷启动，所以只有 LLM 头用稳定键，真实 session 原样保留。
        self.assertEqual("activity", llm_routing_session("activity-20261124-0800"))
        self.assertEqual("qq:private:ABC", llm_routing_session("qq:private:ABC"))
        self.assertIsNone(llm_routing_session(None))

    def test_turn_calls_llm_with_menu_and_cleans_binding(self) -> None:
        config, db, runtime = self.make_env(bind_friend=True)
        llm = BindingSpyLLM(db)
        runtime.llm = llm
        self.seed_feed(db, "own1", "今天面试好累，想找人说话")
        self.seed_bili(db, "BV1xx", "深海鲸鱼观察报告")
        self.seed_draft(db)

        result = runtime.maybe_activity_turn(now=NOON)

        self.assertTrue(result["ok"])
        self.assertEqual("act", result["decision"])
        self.assertEqual(1, len(llm.calls))
        call = llm.calls[0]
        # 2026-11-24: LLM 的 x-opencode-session 用稳定路由键（缓存分区/粘性
        # 路由按这个头），否则每 30 分钟换一次 id 就永远全量未命中。
        self.assertEqual("activity", call["session_id"])
        prompt_text = json.dumps(call["messages"], ensure_ascii=False)
        # Menu data is all there.
        self.assertIn("今天面试好累", prompt_text)
        self.assertIn("深海鲸鱼观察报告", prompt_text)
        self.assertIn("待审批主页帖子草稿：1", prompt_text)
        self.assertIn("自主决策素材菜单", prompt_text)
        self.assertIn("距上次主动分享", prompt_text)
        # The menu rides the turn as wrapped untrusted evidence, not as the
        # raw user message.
        self.assertIn("activity_menu", prompt_text)
        # The temporary owner binding existed while she was deciding...
        self.assertEqual(1, llm.bindings_during)
        # ...and is gone afterwards.
        self.assertEqual(
            0,
            int(db.query_one(
                "SELECT COUNT(*) AS n FROM owner_bindings WHERE source='activity'"
            )["n"]),
        )
        # Decision audited.
        row = db.query_one(
            "SELECT outcome, session_id FROM decision_log WHERE kind='activity_turn' "
            "AND action='decide'"
        )
        self.assertEqual("act", row["outcome"])
        self.assertTrue(str(row["session_id"]).startswith("activity-20261024-"))
        # 台账/决策日志仍记真实会话；只有 LLM 路由头用了稳定键。
        self.assertEqual("activity", call["session_id"])
        # Ledger row with token accounting (fake usage = 14 per call).
        rows = self.ledger_rows(config)
        self.assertEqual(1, len(rows))
        self.assertEqual("act", rows[0]["decision"])
        self.assertGreaterEqual(rows[0]["tokens"], 14)
        self.assertIn("remember", rows[0]["actions"])
        db.close()

    def test_first_turn_menu_looks_back_one_day(self) -> None:
        config, db, runtime = self.make_env()
        llm = FakeLLM()
        runtime.llm = llm
        # Two hours old: inside the 24h fallback window.
        self.seed_feed(db, "own1", "两天里的第一条", seen_at=NOON - 2 * 3600)
        runtime.maybe_activity_turn(now=NOON)
        prompt_text = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("两天里的第一条", prompt_text)
        db.close()

    def test_rest_reply_is_a_legal_decision(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM(text=REST_PLAN)
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("rest", result["decision"])
        row = db.query_one(
            "SELECT outcome FROM decision_log WHERE kind='activity_turn' AND action='decide'"
        )
        self.assertEqual("rest", row["outcome"])
        self.assertEqual("rest", self.ledger_rows(config)[0]["decision"])
        db.close()

    def test_friend_feed_content_withheld_from_menu(self) -> None:
        config, db, runtime = self.make_env(friends_to_llm="false")
        runtime.llm = FakeLLM()
        self.seed_feed(db, "own1", "主人自己的帖子abc")
        self.seed_feed(
            db, "fr1", "好友的私密动态xyz", uin=FRIEND_UIN, scope="friend"
        )
        runtime.maybe_activity_turn(now=NOON)
        prompt_text = json.dumps(llm_prompt(runtime), ensure_ascii=False)
        self.assertIn("主人自己的帖子abc", prompt_text)
        self.assertNotIn("好友的私密动态xyz", prompt_text)
        self.assertIn("按隐私规则不展示", prompt_text)
        db.close()

    def test_friend_feed_content_shown_when_opted_in(self) -> None:
        config, db, runtime = self.make_env(friends_to_llm="true")
        runtime.llm = FakeLLM()
        self.seed_feed(db, "fr1", "好友的公开动态ok", uin=FRIEND_UIN, scope="friend")
        runtime.maybe_activity_turn(now=NOON)
        prompt_text = json.dumps(llm_prompt(runtime), ensure_ascii=False)
        self.assertIn("好友的公开动态ok", prompt_text)
        db.close()

    def test_llm_unavailable_skips_turn(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        runtime.llm.available = False
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertFalse(result["ok"])
        self.assertEqual("llm_unavailable", result["reason"])
        self.assertEqual([], runtime.llm.calls)
        db.close()

    def test_daily_quota_skips_llm(self) -> None:
        config, db, runtime = self.make_env(
            activity_extra={"max_llm_turns_per_day": 2}
        )
        runtime.llm = FakeLLM()
        for _ in range(2):
            db.execute(
                "INSERT INTO decision_log(at, kind, output_json, action, outcome, model) "
                "VALUES (?, 'activity_turn', '{}', 'decide', 'act', 'runtime')",
                (NOON - 60,),
            )
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertFalse(result["ok"])
        self.assertEqual("daily_quota", result["reason"])
        self.assertEqual([], runtime.llm.calls)
        db.close()


class ShortcutTests(ActivityTestBase):
    def test_no_content_and_recent_share_skips_llm(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        self.seed_friend_share(db, NOON - 3600)  # shared 1h ago (< 6h)
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertTrue(result["ok"])
        self.assertEqual("skipped", result["decision"])
        self.assertEqual("no_new_content", result["reason"])
        self.assertEqual([], runtime.llm.calls)
        rows = self.ledger_rows(config)
        self.assertEqual(1, len(rows))
        self.assertEqual("skipped", rows[0]["decision"])
        self.assertEqual("no_new_content", rows[0]["reason"])
        db.close()

    def test_new_feed_content_breaks_the_shortcut(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        self.seed_friend_share(db, NOON - 3600)
        self.seed_feed(db, "own1", "有新帖子就不能偷懒")
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("act", result["decision"])
        self.assertEqual(1, len(runtime.llm.calls))
        db.close()

    def test_new_bili_content_breaks_the_shortcut(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        self.seed_friend_share(db, NOON - 3600)
        self.seed_bili(db, "BV1xx", "新视频要聊观后感")
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("act", result["decision"])
        db.close()

    def test_old_share_breaks_the_shortcut(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        self.seed_friend_share(db, NOON - 7 * 3600)  # 7h ago (> 6h)
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("act", result["decision"])
        db.close()

    def test_pending_draft_does_not_count_as_share(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        # A draft awaiting review is NOT a share: the shortcut must not fire.
        db.execute(
            "INSERT INTO qzone_posts(content, status, created_at) VALUES ('x', 'draft', ?)",
            (NOON - 600,),
        )
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("act", result["decision"])
        self.assertEqual(1, len(runtime.llm.calls))
        self.assertEqual(1, result["menu"]["drafts_pending"])

    def test_shortcut_counts_share_with_owner_wakeup(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM()
        db.execute(
            "INSERT INTO wakeups(id, session_id, created_at, fire_at, status, intent, "
            "reason, payload_json) VALUES ('w1', 's', ?, ?, 'pending', 'share_with_owner', 'r', '{}')",
            (NOON - 1800, NOON - 1780),
        )
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("skipped", result["decision"])
        self.assertEqual([], runtime.llm.calls)
        db.close()


class ShareRoutingTests(ActivityTestBase):
    def test_share_with_owner_wakeup_retargets_to_owner_qq_session(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM(text=SHARE_OWNER_PLAN)
        self.seed_feed(db, "own1", "值得分享的新帖子")
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual("act", result["decision"])
        self.assertIn("share_with_owner", result["actions"])
        self.assertEqual(1, len(result["shares_retargeted"]))
        wakeup = db.query_one(
            "SELECT session_id, status, intent FROM wakeups WHERE intent='share_with_owner'"
        )
        self.assertIsNotNone(wakeup)
        self.assertEqual(f"qq:private:{OWNER_UIN}", wakeup["session_id"])
        self.assertEqual("pending", wakeup["status"])
        db.close()

    def test_share_with_owner_stays_put_without_reachable_owner(self) -> None:
        config, db, runtime = self.make_env(bind_owner=False)
        # No owner binding at all -> no reachable QQ session -> wakeup keeps
        # the activity session (delivery is then the adapter's "not ours" path).
        OwnerRegistry(db, config).bind("web:local", "主人哥哥")
        runtime.llm = FakeLLM(text=SHARE_OWNER_PLAN)
        self.seed_feed(db, "own1", "新帖子")
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertEqual([], result["shares_retargeted"])
        wakeup = db.query_one(
            "SELECT session_id FROM wakeups WHERE intent='share_with_owner'"
        )
        self.assertTrue(str(wakeup["session_id"]).startswith("activity-"))
        db.close()

    def test_share_to_friend_delivers_through_outbox(self) -> None:
        config, db, runtime = self.make_env(bind_friend=True)
        runtime.llm = FakeLLM(text=SHARE_FRIEND_PLAN)
        self.seed_feed(db, "own1", "新帖子")
        result = runtime.maybe_activity_turn(now=NOON)
        self.assertIn("share_to_friend", result["actions"])
        item = db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='friend_share' ORDER BY id DESC"
        )
        self.assertIsNotNone(item)
        payload = json.loads(item["payload_json"])
        self.assertEqual(FRIEND_UIN, payload["friend_uin"])
        db.close()


class BilibiliBrowseTests(ActivityTestBase):
    def test_disabled_returns_none(self) -> None:
        config, db, runtime = self.make_env(bilibili_enabled="false")
        runtime.bilibili = FakeBiliCollector()
        self.assertIsNone(runtime.maybe_browse_bilibili(now=NOON))
        self.assertIsNone(db.get_meta("last_activity_bili_browse_at"))
        self.assertEqual([], runtime.bilibili.calls)
        db.close()

    def test_interval_watermark_and_repeat(self) -> None:
        config, db, runtime = self.make_env(bilibili_enabled="true")
        collector = FakeBiliCollector()
        runtime.bilibili = collector
        first = runtime.maybe_browse_bilibili(now=NOON)
        self.assertTrue(first["ok"])
        self.assertEqual(2, first["new"])
        self.assertEqual(1, len(collector.calls))
        self.assertIsNotNone(db.get_meta("last_activity_bili_browse_at"))
        self.assertIsNone(runtime.maybe_browse_bilibili(now=NOON + 60 * 60))
        self.assertEqual(1, len(collector.calls))
        again = runtime.maybe_browse_bilibili(now=NOON + 121 * 60)
        self.assertTrue(again["ok"])
        self.assertEqual(2, len(collector.calls))
        db.close()

    def test_error_is_contained_and_watermark_advances(self) -> None:
        config, db, runtime = self.make_env(bilibili_enabled="true")
        runtime.bilibili = FakeBiliCollector(error=RuntimeError("network down"))
        result = runtime.maybe_browse_bilibili(now=NOON)
        self.assertFalse(result["ok"])
        self.assertIn("network down", result["error"])
        self.assertIsNotNone(db.get_meta("last_activity_bili_browse_at"))
        self.assertIsNone(runtime.maybe_browse_bilibili(now=NOON + 60))
        db.close()

    def test_tick_reports_both_layers(self) -> None:
        config, db, runtime = self.make_env(bilibili_enabled="false")
        runtime.llm = FakeLLM()
        result = runtime.tick(now=NOON)
        self.assertIn("bili_browse", result)
        self.assertIsNone(result["bili_browse"])
        self.assertIn("activity", result)
        self.assertEqual("act", result["activity"]["decision"])
        db.close()


class HandleUserMessageParamsTests(ActivityTestBase):
    def test_extra_evidence_reaches_prompt_and_allow_voice_off(self) -> None:
        config, db, runtime = self.make_env()
        llm = FakeLLM()
        runtime.llm = llm
        runtime.voice = FakeVoicePolicy()
        runtime.tts = FakeTTS()
        result = runtime.handle_user_message(
            "qq:private:1001",
            "随便聊聊",
            now=NOON,
            extra_evidence=["[活动菜单 · 外部内容] 菜单数据X"],
            allow_voice=False,
        )
        prompt_text = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("菜单数据X", prompt_text)
        self.assertEqual("text_only", result["voice"]["mode"])
        self.assertIsNone(result["tts"])
        self.assertEqual([], runtime.tts.calls)
        db.close()

    def test_default_turn_still_allows_voice(self) -> None:
        config, db, runtime = self.make_env()
        llm = FakeLLM()
        runtime.llm = llm
        runtime.voice = FakeVoicePolicy()
        runtime.tts = FakeTTS()
        runtime.handle_user_message("qq:private:1001", "随便聊聊", now=NOON)
        self.assertEqual(1, len(runtime.tts.calls))
        db.close()


class PromptSafetyTests(unittest.TestCase):
    def test_decision_prompt_triggers_no_bypass(self) -> None:
        # The wake text is the "user message" of the decision turn: if it ever
        # matches the QQ Zone review bypass (or the photo bypass) keywords, the
        # runtime would inject duplicate evidence / take unintended actions.
        self.assertFalse(wants_qzone_review(DEFAULT_ACTIVITY_DECISION_PROMPT))
        self.assertFalse(wants_photo(DEFAULT_ACTIVITY_DECISION_PROMPT))


def llm_prompt(runtime: Runtime) -> list:
    """The single recorded FakeLLM call of ``runtime.llm``."""
    return runtime.llm.calls[0]["messages"]


if __name__ == "__main__":
    unittest.main()
