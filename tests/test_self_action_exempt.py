"""T-0042 补丁：主人明确指使的豁免（enabled 默认开 + 显式请求不受软限约束）。

boss 2026-10-26 拍板两条，这份文件是它们的护栏：

1. ``[self_action] enabled`` 三套 profile 默认 **true**（四道软限一个都没松）。
2. 主人原话明确让她做某件事时，**四道闸门全部让路**（dev 语义见
   :mod:`fatfish.self_action.explicit`）。

分包：

* ``DetectionTests`` -- 判定表：四类动作各自的正例、否定句、纯提及、跨工具串味、
  垃圾输入、超长文本/随机串不抛异常。
* ``ServiceTests``   -- ``can_run(explicit=True)`` 越过 enabled / 静默 / 日上限 /
  阈值；``explicit=False`` 时旧语义**逐条不变**。
* ``RuntimeTests``   -- 真跑 ``execute_actions``：豁免真的执行、真留一行审计、
  真抬阈值、真跳过本轮预算；**模型自报的 flag 不算数**；自生成回合（user_text
  空串）拿不到豁免；闸门不可用时非显式仍然 fail-closed。
* ``ConfigTests``    -- 三套 profile 的 ``enabled`` 是 true，``[owner_chat]``
  占位段没被顺手打开，``SELF_ACTION_DEFAULTS.enabled`` 也是 true。

离线：临时库 + 假 memory / qzone / photo / bilibili，不碰网络、不碰 ComfyUI。
"""

from __future__ import annotations

import random
import unittest

from fatfish.config import load_config
from fatfish.desire.settings import SELF_ACTION_DEFAULTS
from fatfish.runtime import TurnAction
from fatfish.security import TrustLevel
from fatfish.self_action.explicit import EXEMPT_TOOLS, explicit_request
from fatfish.self_action.service import (
    REASON_EXPLICIT,
    SELF_ACTION_TOOLS,
    SelfActionService,
)
from tests.test_self_action_gate import (
    ARGS,
    FOUR,
    NIGHT,
    NOON,
    SESSION,
    Env,
    decision_rows,
    evidence_rows,
    make_env,
    settings_for,
)

PROFILES = ("low", "standard", "remote")

#: 每个工具至少 5 条正例：书面说法 + 口语说法 + 只给动作词的说法。
POSITIVES: dict[str, tuple[str, ...]] = {
    "search_memory": (
        "帮我搜一下你以前的记录",
        "去翻翻上次我们说过的话",
        "你还记得我生日吗",
        "查一下你记忆里有没有海豚",
        "搜一下以前的记录，看看那天我说了啥",
    ),
    "read_qzone": (
        "帮我去空间看看有没有人点赞",
        "看看动态",
        "去刷一下说说",
        "空间里看看谁来过",
        "打开动态我瞧瞧",
    ),
    "make_photo": (
        "给我画一张小猫的图",
        "画一张赛博朋克风格的吧",
        "帮我生成一张海边日落的照片",
        "来一张壁纸",
        "渲染个头像出来",
    ),
    "search_videos": (
        "去B站搜一下黑洞的视频",
        "B站有啥好玩的",
        "搜个视频看看",
        "帮我找找关于海洋的vlog",
        "小破站上翻翻番剧",
    ),
}

#: 反例：要么被否定，要么只是提到关键词，要么根本没关系。
NEGATIVES = (
    "别搜了，也别翻记忆",
    "不用查空间了",
    "不要画了",
    "别找视频了",
    "空间今天一个人都没来",
    "我今天画了一天画好累",
    "今天在B站看了一天视频",
    "随便聊聊",
    "今天天气不错",
    "我找了半天记忆里都没有",
    "这是我以前住的地方",
)


class DetectionTests(unittest.TestCase):
    """纯函数：不碰库、不碰配置。"""

    def test_rule_set_covers_exactly_the_four_actions(self) -> None:
        # 加第五个自主动作却忘了加规则，这里会先炸，而不是线上静默失效。
        # T-0082: talk_to 也带豁免规则，但它不是自主动作——没有渴望闸门，
        # 主人原话是它唯一的门，所以豁免集合 = 自主动作 ∪ {talk_to}。
        self.assertEqual(EXEMPT_TOOLS, SELF_ACTION_TOOLS | {"talk_to"})

    def test_positives(self) -> None:
        for tool, phrases in POSITIVES.items():
            for phrase in phrases:
                with self.subTest(tool=tool, phrase=phrase):
                    self.assertTrue(explicit_request(phrase, tool), phrase)

    def test_negated_requests_are_not_explicit(self) -> None:
        for phrase in NEGATIVES:
            for tool in FOUR:
                with self.subTest(tool=tool, phrase=phrase):
                    self.assertFalse(explicit_request(phrase, tool), phrase)

    def test_mentioning_the_topic_is_not_a_request(self) -> None:
        # 这三个都提到关键词，但都不是「让她去做」。豁免不能靠关键词命中。
        for phrase, tool in (
            ("空间今天一个人都没来", "read_qzone"),
            ("我今天画了一天画好累", "make_photo"),
            ("今天在B站看了一天视频", "search_videos"),
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(explicit_request(phrase, tool))

    def test_cross_tool_never_exempts(self) -> None:
        # 「搜一下视频」不能给生图发豁免——否则一个泛化关键词就能打穿四道闸门。
        matrix = {
            "search_videos": "搜个视频看看",
            "make_photo": "给我画一张小猫的图",
            "read_qzone": "看看动态",
            "search_memory": "帮我搜一下你以前的记录",
        }
        for phrase, owner in matrix.items():
            for tool in FOUR:
                if tool == owner:
                    continue
                with self.subTest(phrase=phrase, tool=tool):
                    self.assertFalse(explicit_request(phrase, tool))

    def test_garbage_input_is_false_never_raises(self) -> None:
        for junk in (None, "", "   ", 0, 1.5, [], {}, object(), b"\xe4\xbd\xa0"):
            for tool in (*FOUR, "", "nope", None):
                with self.subTest(junk=repr(junk), tool=repr(tool)):
                    self.assertIs(explicit_request(junk, tool), False)

    def test_long_text_is_bounded_and_safe(self) -> None:
        # 粘一整篇文档进来不该让这一步变慢，也不该炸。
        blob = ("随便" * 20000) + "帮我搜一下以前的记录"
        self.assertIsInstance(explicit_request(blob, "search_memory"), bool)

    def test_random_strings_do_not_raise(self) -> None:
        rng = random.Random(20261026)
        alphabet = "帮我搜查找看翻画生成拍视频记忆空间动态以前上次说过的记录别不用B站bilibili，。！？123abc \n"
        for _ in range(300):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            for tool in FOUR:
                self.assertIsInstance(explicit_request(text, tool), bool)


class ServiceTests(unittest.TestCase):
    """``SelfActionService.can_run(explicit=...)`` 的四个让路点。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _, self.db, _ = make_env(
            Path(self._tmp.name), name="exempt.toml", db_name="exempt.db"
        )
        self.addCleanup(self.db.close)

    def service(self, **overrides) -> SelfActionService:
        return SelfActionService(
            self.db, settings_for(**overrides), now_fn=lambda: NOON
        )

    def test_explicit_passes_with_zero_desire(self) -> None:
        allowed, reason, meta = self.service().can_run(0.0, now=NOON, explicit=True)
        self.assertTrue(allowed)
        self.assertEqual(reason, REASON_EXPLICIT)
        self.assertTrue(meta["explicit"])
        self.assertEqual(meta["desire"], 0.0)

    def test_without_explicit_zero_desire_is_refused(self) -> None:
        allowed, reason, meta = self.service().can_run(0.0, now=NOON)
        self.assertFalse(allowed)
        self.assertEqual(reason, "below_threshold")
        self.assertFalse(meta["explicit"])

    def test_explicit_beats_disabled(self) -> None:
        allowed, reason, _ = self.service(enabled=False).can_run(
            0.0, now=NOON, explicit=True
        )
        self.assertTrue(allowed, reason)
        self.assertEqual(reason, REASON_EXPLICIT)

    def test_explicit_beats_quiet_hours(self) -> None:
        service = self.service(quiet_hours=["01:00", "08:00"])
        self.assertFalse(service.can_run(1.0, now=NIGHT)[0])  # 02:00 确实在静默里
        allowed, reason, _ = service.can_run(0.0, now=NIGHT, explicit=True)
        self.assertTrue(allowed, reason)
        self.assertEqual(reason, REASON_EXPLICIT)

    def test_explicit_beats_daily_cap(self) -> None:
        service = self.service(daily_cap=1)
        self.db.execute(
            "INSERT INTO self_action_evidence"
            "(session_id, action, source, title, content, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (SESSION, "search_memory", "memory", "海豚", "-", NOON - 600),
        )
        self.assertEqual(service.can_run(1.0, now=NOON)[1], "daily_cap")
        allowed, reason, _ = service.can_run(0.0, now=NOON, explicit=True)
        self.assertTrue(allowed, reason)
        self.assertEqual(reason, REASON_EXPLICIT)

    def test_explicit_meta_still_carries_the_numbers(self) -> None:
        _, _, meta = self.service().can_run(0.42, now=NOON, explicit=True)
        for key in (
            "threshold",
            "desire",
            "sent_last_hour",
            "daily_count",
            "decay_per_minute",
            "explicit",
        ):
            self.assertIn(key, meta)


class RuntimeTests(Env):
    """真跑 ``execute_actions``：豁免在接线这一层也必须成立。"""

    def act(self, tool: str, *, desire: float, user_text, args=None, now: float = NOON):
        return self.runtime.execute_actions(
            [TurnAction(tool, {**(args or ARGS[tool]), "desire": desire})],
            session_id=SESSION,
            reply="",
            now=now,
            trust=TrustLevel.PRIVATE,
            user_text=user_text,
        )[0]

    def test_explicit_request_runs_with_low_desire(self) -> None:
        result = self.act("search_memory", desire=0.1, user_text="你搜一下你以前的记录")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.memory.calls), 1)
        self.assertEqual(len(evidence_rows(self.db)), 1)

    def test_vague_lookup_is_not_explicit(self) -> None:
        # 「帮我查查海豚的事」没有指向"记忆/以前"，按非豁免走：漏判只回落旧路径。
        result = self.act("search_memory", desire=0.1, user_text="帮我查查海豚的事")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "below_threshold")
        self.assertEqual(evidence_rows(self.db), [])

    def test_self_generated_turn_cannot_exempt_itself(self) -> None:
        # 自生成回合传进来的就是空串（runtime 里 ephemeral 分支），永远拿不到豁免。
        result = self.act(
            "search_memory", desire=0.1, user_text="", args={"query": "海豚"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "below_threshold")

    def test_model_written_flag_is_ignored(self) -> None:
        # 模型自己在 args 里写 explicit=true 不算数：判据只有主人原话。
        result = self.act(
            "search_memory",
            desire=0.1,
            user_text="随便聊聊",
            args={"query": "海豚", "explicit": True, "user_requested": True},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "below_threshold")

    def test_explicit_leaves_exactly_one_audit_row(self) -> None:
        self.act("read_qzone", desire=0.0, user_text="去空间看看有没有人点赞")
        rows = decision_rows(self.db)
        self.assertEqual([row["outcome"] for row in rows], [REASON_EXPLICIT])
        self.assertEqual(rows[0]["action"], "read_qzone")

    def test_explicit_still_lifts_the_threshold(self) -> None:
        # 豁免不免记账：做完一次，阈值照抬一档，下次"她自己"主动更难。
        before = self.service().current_threshold(NOON)
        self.act("search_memory", desire=0.0, user_text="去翻翻以前的记录")
        after = self.service().current_threshold(NOON)
        self.assertGreater(after, before)
        self.assertAlmostEqual(after, min(0.90, before + 0.10), places=6)

    def test_explicit_skips_the_per_turn_budget(self) -> None:
        config, db, runtime = self.extra_env("budget", per_turn=1)
        self.addCleanup(db.close)
        runtime.memory, runtime.qzone = self.memory, self.qzone
        runtime.photo, runtime.bilibili = self.photo, self.bilibili
        results = runtime.execute_actions(
            [
                TurnAction("make_photo", {**ARGS["make_photo"], "desire": 0.0}),
                TurnAction("search_videos", {**ARGS["search_videos"], "desire": 0.0}),
            ],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
            user_text="给我画一张海边鲸鱼的图，再去B站搜一下海洋的视频",
        )
        self.assertEqual([r["ok"] for r in results], [True, True], results)

    def test_budget_still_bites_without_exemption(self) -> None:
        config, db, runtime = self.extra_env("budget2", per_turn=1)
        self.addCleanup(db.close)
        runtime.memory, runtime.qzone = self.memory, self.qzone
        runtime.photo, runtime.bilibili = self.photo, self.bilibili
        results = runtime.execute_actions(
            [
                TurnAction("search_memory", {**ARGS["search_memory"], "desire": 1.0}),
                TurnAction("read_qzone", {**ARGS["read_qzone"], "desire": 1.0}),
            ],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
            user_text="随便聊聊",
        )
        self.assertEqual(
            [r.get("error") for r in results], [None, "too_many_self_actions"], results
        )

    def test_disabled_config_but_explicit_still_runs(self) -> None:
        config, db, runtime = self.extra_env("off", enabled=False)
        self.addCleanup(db.close)
        runtime.memory, runtime.qzone = self.memory, self.qzone
        runtime.photo, runtime.bilibili = self.photo, self.bilibili
        refused = runtime.execute_actions(
            [TurnAction("search_memory", {**ARGS["search_memory"], "desire": 1.0})],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
            user_text="随便聊聊",
        )[0]
        self.assertEqual(refused["error"], "self_action_disabled")
        allowed = runtime.execute_actions(
            [TurnAction("search_memory", {**ARGS["search_memory"], "desire": 0.0})],
            session_id=SESSION,
            reply="",
            now=NOON,
            trust=TrustLevel.PRIVATE,
            user_text="你搜一下你以前的记录",
        )[0]
        self.assertTrue(allowed["ok"], allowed)

    def test_unavailable_service_still_fails_closed_without_exemption(self) -> None:
        # 配置/状态读取坏掉 → 非显式一律 refuse（旧行为不变）。
        self.runtime._self_action = None
        self.runtime._self_action_unavailable = True
        refused = self.act("search_memory", desire=1.0, user_text="随便聊聊")
        self.assertEqual(refused["error"], "self_action_disabled")
        self.assertEqual(self.memory.calls, [])

    def test_unavailable_service_but_explicit_still_runs(self) -> None:
        # 闸门读不出来不该让主人的明确指令落空；只是这一轮无法记账。
        self.runtime._self_action = None
        self.runtime._self_action_unavailable = True
        with self.assertLogs("fatfish.runtime", level="WARNING"):
            result = self.act("search_memory", desire=0.0, user_text="你搜一下你以前的记录")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(evidence_rows(self.db)), 1)
        self.assertEqual(decision_rows(self.db)[0]["outcome"], REASON_EXPLICIT)


class ConfigTests(unittest.TestCase):
    def test_self_action_is_on_by_default_in_every_profile(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                section = load_config(profile).section("self_action")
                self.assertIs(section["enabled"], True)

    def test_defaults_agree_with_the_shipped_sections(self) -> None:
        self.assertIs(SELF_ACTION_DEFAULTS.enabled, True)

    def test_owner_chat_placeholder_stays_off(self) -> None:
        # 顺手把占位段打开会让"主动找主人"看起来已经接了，其实没有。
        for profile in PROFILES:
            with self.subTest(profile=profile):
                self.assertIs(load_config(profile).section("owner_chat")["enabled"], False)

    def test_ai_chat_section_untouched(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                section = load_config(profile).section("ai_chat")
                self.assertEqual(section["decay_messages_per_hour"], 50)


if __name__ == "__main__":
    unittest.main()
