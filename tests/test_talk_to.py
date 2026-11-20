"""T-0082: ``talk_to`` -- 按主人原话让她跟指定的人聊天（群内接话 + 私聊单发）。

安全核心（不可妥协）：``requires_explicit``。talk_to 只存在于主人的原话里 --
``explicit_request(user_text, "talk_to")`` 不过就一律拒绝
（error=needs_owner_instruction），信任再高、配置再全、模型再想都不行；
自生成/临时轮的 user_text 是空串，天然拿不到豁免。她自己永远不能主动
找第三人说话。

分层（一类一个关注点）：

* ``PolicyTests``        -- spec 契约：requires_explicit / max_calls=2 /
  required_text="text" / 群信任可达（scope=group 只在群轮里合法）。
* ``ExplicitTests``      -- 主人原话正则：正例（「去跟杨老师聊几句」「帮我跟妈妈
  说一声」方向）、否定与陈述反例（「别跟他说了」「我跟你说」不是指令）、跨工具零串味。
* ``RegistrationTests``  -- ALLOWED_ACTIONS / TOOL_SCHEMA / EXEMPT_TOOLS 都认识它；
  它**不在** SELF_ACTION_TOOLS 里（没有渴望闸门，只归主人原话管）。
* ``GateTests``          -- 运行时闸门：没有主人原话一律 needs_owner_instruction，
  且不写水位、不入队、不留发送痕迹；模型在 args 里自报 explicit 不算数。
* ``GroupScopeTests``    -- 群内接话：成员按昵称/群名片/人物卡解析、发到**群**
  （不是私发成员）、成员不存在、非群会话拒绝、模型报的裸 QQ 号不认。
* ``PrivateScopeTests``  -- 私聊单发：仅 owner 私聊会话；人物卡解析（无卡/无
  uin/歧义各自报错）。
* ``CooldownAndTextTests`` -- 同目标 10 分钟冷却（先写水位再发送）、文本截断、
  enabled=false 一律拒绝、scope 二值校验。
* ``AuditTests``         -- outbox qq_send 形状正确；decision_log 全程无 uin
  （只有名字）；tool_audit 照常落行。

离线：临时库 + 假 QQ 适配器 + 真 Outbox（落在临时库），不碰网络、不碰 NapCat。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import TOOL_SCHEMA, ensure_default_persona
from fatfish.db import Database
from fatfish.memory.entities import create_person, resolve_person_target
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
GROUP_SESSION = "qq:group:778899"
GROUP_ID = "778899"

# 主人原话的两个方向（任务卡给的示例），门和分支都要认。
GROUP_INSTRUCTION = "去跟杨老师聊几句"
PRIVATE_INSTRUCTION = "帮我跟妈妈说一声我到家了"

GROUP_ARGS = {"scope": "group", "target": "杨老师", "text": "杨老师好呀～我是大肥鱼～"}
PRIVATE_ARGS = {"scope": "private", "target": "妈妈", "text": "妈妈，主人哥哥到家啦～"}


def make_runtime(
    tmp: Path,
    *,
    name: str = "talk_to",
    talk_enabled: bool = True,
    cooldown_minutes: int = 10,
    max_text_chars: int = 500,
) -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / f"{name}.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        "[qq.talk]",
        f"enabled = {'true' if talk_enabled else 'false'}",
        f"cooldown_minutes = {cooldown_minutes}",
        f"max_text_chars = {max_text_chars}",
    ]
    path = tmp / f"{name}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


class FakeQQ:
    """Duck-typed QQ adapter: canned member list, recorded calls, no network."""

    def __init__(self, members: list[dict] | None = None):
        self.members = list(members if members is not None else DEFAULT_MEMBERS)
        self.calls: list[dict] = []

    def fetch_group_members(self, group_id):
        self.calls.append({"kind": "members", "group_id": group_id})
        return list(self.members)


DEFAULT_MEMBERS = [
    {"user_id": 1001, "nickname": "主人", "card": ""},
    {"user_id": 10086, "nickname": "YangLaoshi", "card": "杨老师"},
    {"user_id": 2002, "nickname": "小红", "card": ""},
]


# --------------------------------------------------------------------- policy
class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyEngine()

    def test_spec_pins_the_owner_verbatim_contract(self) -> None:
        spec = ACTION_SPECS["talk_to"]
        self.assertTrue(spec.requires_explicit)
        self.assertEqual(spec.max_calls, 2)
        self.assertEqual(spec.required_text, "text")
        self.assertTrue(spec.allow_group)
        self.assertFalse(spec.write)  # can_write 会把群轮整档挡死，见 policy 注释
        self.assertEqual(spec.min_trust, TrustLevel.GROUP)

    def test_requires_explicit_defaults_off_for_other_actions(self) -> None:
        for tool in ("remember", "read_chat", "share_to_friend"):
            with self.subTest(tool=tool):
                self.assertFalse(ACTION_SPECS[tool].requires_explicit)

    def test_group_trust_reaches_the_branch(self) -> None:
        # scope=group 只在群轮里合法：群信任必须过 policy，否则该档永远死代码。
        decision = self.policy.validate(
            "talk_to",
            {"scope": "group", "target": "杨老师", "text": "hi"},
            trust=TrustLevel.GROUP,
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_private_trust_is_allowed(self) -> None:
        decision = self.policy.validate(
            "talk_to",
            {"scope": "private", "target": "妈妈", "text": "hi"},
            trust=TrustLevel.PRIVATE,
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_low_trust_is_denied(self) -> None:
        decision = self.policy.validate(
            "talk_to", {"scope": "group", "target": "x", "text": "hi"},
            trust=TrustLevel.MEMORY,
        )
        self.assertFalse(decision.allowed)

    def test_missing_text_is_denied(self) -> None:
        for args in (
            {"scope": "group", "target": "杨老师"},
            {"scope": "group", "target": "杨老师", "text": "  "},
            {"scope": "group", "target": "杨老师", "text": None},
        ):
            with self.subTest(args=args):
                decision = self.policy.validate(
                    "talk_to", args, trust=TrustLevel.PRIVATE
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "missing_text")

    def test_two_calls_per_turn_then_refused(self) -> None:
        args = {"scope": "group", "target": "杨老师", "text": "hi"}
        self.assertTrue(
            self.policy.validate("talk_to", args, trust=TrustLevel.GROUP, count=1).allowed
        )
        decision = self.policy.validate(
            "talk_to", args, trust=TrustLevel.GROUP, count=2
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "too_many_tool_calls")


# ------------------------------------------------------------------- explicit
class ExplicitTests(unittest.TestCase):
    POSITIVES = (
        "去跟杨老师聊几句",
        "帮我跟妈妈说一声我到家了",
        "你去和老师说一下",
        "顺便跟杨老师问个好",
        "帮我给妈妈带个话",
        "请跟王阿姨转告一声",
        "快跟妈妈说一声",
        "赶紧跟小明说句话",
    )
    NEGATIVES = (
        # 任务卡点名的两个反例
        "别跟他说了",
        "我跟你说",
        # 陈述句 / 他人动作
        "我今天跟老板说了好多话",
        "他跟我说了个笑话",
        "刚才妈妈跟我说她到家了",
        "跟他聊天很开心",
        "你再跟我说说",
        "和我说说今天的事",
        "别去找他聊了",
        "今天天气不错",
    )

    def test_positives_are_owner_instructions(self) -> None:
        for phrase in self.POSITIVES:
            with self.subTest(phrase=phrase):
                self.assertTrue(explicit_request(phrase, "talk_to"), phrase)

    def test_negatives_are_not_instructions(self) -> None:
        for phrase in self.NEGATIVES:
            with self.subTest(phrase=phrase):
                self.assertFalse(explicit_request(phrase, "talk_to"), phrase)

    def test_garbage_input_is_false_never_raises(self) -> None:
        for junk in (None, "", "   ", 0, 1.5, [], {}, b"\xe4\xbd\xa0"):
            with self.subTest(junk=repr(junk)):
                self.assertIs(explicit_request(junk, "talk_to"), False)
        self.assertIs(explicit_request("去跟杨老师聊几句", None), False)

    def test_cross_tool_never_exempts(self) -> None:
        for phrase in self.POSITIVES:
            for tool in ("search_memory", "read_qzone", "make_photo", "search_videos", "read_chat"):
                with self.subTest(phrase=phrase, tool=tool):
                    self.assertFalse(explicit_request(phrase, tool))


# -------------------------------------------------------------- registration
class RegistrationTests(unittest.TestCase):
    def test_allowed_actions_contains_talk_to(self) -> None:
        self.assertIn("talk_to", ALLOWED_ACTIONS)

    def test_talk_to_is_not_a_self_action_but_is_explicit_gated(self) -> None:
        # 不进 SELF_ACTION_TOOLS：没有渴望闸门，只归主人原话管；
        # 但豁免规则表必须认识它（EXEMPT_TOOLS 是全部支持 explicit 的工具）。
        self.assertNotIn("talk_to", SELF_ACTION_TOOLS)
        self.assertIn("talk_to", EXEMPT_TOOLS)
        self.assertEqual(EXEMPT_TOOLS, SELF_ACTION_TOOLS | {"talk_to"})

    def test_tool_schema_documents_talk_to(self) -> None:
        self.assertIn("- talk_to:", TOOL_SCHEMA)
        self.assertIn("scope", TOOL_SCHEMA)
        self.assertIn("needs_owner_instruction", TOOL_SCHEMA)
        # 教会模型：她自己永远不能主动用；只在主人明确让她去说时用。
        self.assertIn("你自己永远不能主动", TOOL_SCHEMA)
        self.assertIn("主人明确", TOOL_SCHEMA)
        self.assertIn("500", TOOL_SCHEMA)
        self.assertIn("1-2", TOOL_SCHEMA)
        # 两个示例方向都要在
        self.assertIn("去跟杨老师聊几句", TOOL_SCHEMA)
        self.assertIn("帮我跟妈妈说一声我到家了", TOOL_SCHEMA)

    def test_parse_turn_plan_keeps_talk_to(self) -> None:
        raw = json.dumps(
            {
                "messages": ["嗯"],
                "actions": [{"tool": "talk_to", "args": dict(GROUP_ARGS)}],
            },
            ensure_ascii=False,
        )
        plan = parse_turn_plan(raw)
        self.assertEqual([a.tool for a in plan.actions], ["talk_to"])
        self.assertEqual(plan.actions[0].args["scope"], "group")


# --------------------------------------------------------------- runtime gate
class RuntimeCase(unittest.TestCase):
    """Shared harness: temp runtime + fake QQ adapter + real outbox."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-talk-to-")
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.qq = FakeQQ()
        self.runtime.qq = self.qq

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def run_action(
        self,
        args: dict,
        *,
        tool: str = "talk_to",
        session_id: str = GROUP_SESSION,
        trust: TrustLevel | None = None,
        now: float = NOW,
        user_text: str | None = GROUP_INSTRUCTION,
    ) -> dict:
        if trust is None:
            trust = TrustLevel.GROUP if session_id == GROUP_SESSION else TrustLevel.PRIVATE
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

    def outbox_rows(self) -> list:
        return self.db.query("SELECT * FROM outbox WHERE kind='qq_send' ORDER BY id")

    def talk_logs(self) -> list:
        return self.db.query(
            "SELECT * FROM decision_log WHERE kind='talk_to' ORDER BY id"
        )

    def tool_audit_rows(self) -> list:
        return self.db.query("SELECT * FROM tool_audit WHERE tool='talk_to' ORDER BY id")

    def bind_owner(self, session_id: str = PRIVATE_SESSION) -> None:
        self.runtime.owner.bind(session_id, "主人哥哥", "test")


class GateTests(RuntimeCase):
    """安全核心：主人原话没让 → 一律 needs_owner_instruction，任何东西都补不回来。"""

    def test_without_owner_words_always_refused(self) -> None:
        for kwargs in (
            {"session_id": GROUP_SESSION, "trust": TrustLevel.GROUP},
            {"session_id": PRIVATE_SESSION, "trust": TrustLevel.PRIVATE},
        ):
            with self.subTest(**kwargs):
                result = self.run_action(dict(GROUP_ARGS), user_text=None, **kwargs)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "needs_owner_instruction")
                self.assertEqual(self.outbox_rows(), [])
                self.assertEqual(self.qq.calls, [])

    def test_ephemeral_turn_empty_user_text_is_refused(self) -> None:
        # 自生成/临时轮 runtime 传来的就是空串——永远拿不到豁免。
        result = self.run_action(dict(GROUP_ARGS), user_text="")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "needs_owner_instruction")

    def test_denial_phrases_are_not_instructions(self) -> None:
        for phrase in ("别跟他说了", "我跟你说", "随便聊聊"):
            with self.subTest(phrase=phrase):
                result = self.run_action(dict(GROUP_ARGS), user_text=phrase)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "needs_owner_instruction")

    def test_model_written_flags_do_not_count(self) -> None:
        # 判据只有主人原话；args 里自报 explicit/user_requested 一律不看。
        args = {**GROUP_ARGS, "explicit": True, "user_requested": True}
        result = self.run_action(args, user_text="随便聊聊")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "needs_owner_instruction")

    def test_refusal_writes_no_watermark_and_one_audit_row(self) -> None:
        self.run_action(dict(GROUP_ARGS), user_text=None)
        self.assertIsNone(self.db.get_meta(f"talk_to_at:{GROUP_SESSION}:10086", None))
        logs = self.talk_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["outcome"], "needs_owner_instruction")
        self.assertFalse(json.loads(logs[0]["output_json"])["explicit"])
        # tool_audit 照常落行（policy 层放行、运行时闸门拦下）。
        self.assertEqual(len(self.tool_audit_rows()), 1)

    def test_owner_words_in_group_turn_open_the_gate(self) -> None:
        result = self.run_action(dict(GROUP_ARGS), user_text=GROUP_INSTRUCTION)
        self.assertTrue(result["ok"], result)


# -------------------------------------------------------------- group scope
class GroupScopeTests(RuntimeCase):
    def test_instruction_sends_to_the_group_not_the_member(self) -> None:
        result = self.run_action(dict(GROUP_ARGS), user_text=GROUP_INSTRUCTION)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["scope"], "group")
        self.assertEqual(result["result"]["target"], "杨老师")
        self.assertFalse(result["result"]["truncated"])
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["target_type"], "group")
        # 发到群，不是私发成员：target_id 是群号，成员号只用来定冷却对象。
        self.assertEqual(payload["target_id"], GROUP_ID)
        self.assertEqual(payload["text"], GROUP_ARGS["text"])
        self.assertEqual(
            self.db.get_meta(f"talk_to_at:{GROUP_SESSION}:10086", None), str(NOW)
        )

    def test_member_resolved_by_nickname_and_card(self) -> None:
        result = self.run_action(
            {"scope": "group", "target": "小红", "text": "小红你好呀"}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["target"], "小红")

    def test_member_resolved_through_person_card(self) -> None:
        # 卡片路线：target 是人物卡的名字/别名，落到群成员身上。
        create_person(self.db, name="老李", uin=3003, aliases=["李师傅"])
        self.qq.members = [{"user_id": 3003, "nickname": "小李", "card": "李师傅"}]
        result = self.run_action({"scope": "group", "target": "老李", "text": "李师傅好"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["target"], "李师傅")

    def test_member_matched_by_card_uin(self) -> None:
        # 名字对不上、但卡上的 uin 就是群成员 → 认（卡是主人手写的名单）。
        create_person(self.db, name="王同学", uin=2002)
        result = self.run_action({"scope": "group", "target": "王同学", "text": "你好"})
        self.assertTrue(result["ok"], result)

    def test_digit_target_without_card_refuses(self) -> None:
        # 模型报的裸 QQ 号不是通行证：没有人物卡背书就不认。
        result = self.run_action({"scope": "group", "target": "2002", "text": "你好"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "target_not_in_group")
        self.assertEqual(self.outbox_rows(), [])

    def test_unknown_member_refuses_target_not_in_group(self) -> None:
        result = self.run_action(
            {"scope": "group", "target": "王大爷", "text": "你好"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "target_not_in_group")
        self.assertEqual(self.outbox_rows(), [])
        self.assertEqual(self.qq.calls, [{"kind": "members", "group_id": GROUP_ID}])

    def test_missing_target_refuses(self) -> None:
        result = self.run_action({"scope": "group", "text": "你好"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_target")

    def test_non_group_session_refuses_group_scope(self) -> None:
        result = self.run_action(
            dict(GROUP_ARGS), session_id=PRIVATE_SESSION, trust=TrustLevel.PRIVATE
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_session_required")
        self.assertEqual(self.qq.calls, [])
        self.assertEqual(self.outbox_rows(), [])

    def test_invalid_scope_refuses(self) -> None:
        result = self.run_action({"scope": "everyone", "target": "杨老师", "text": "hi"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_scope")


# ------------------------------------------------------------ private scope
class PrivateScopeTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind_owner()
        create_person(self.db, name="妈妈", uin=4242)

    def test_instruction_dms_the_card_holder(self) -> None:
        result = self.run_action(
            dict(PRIVATE_ARGS),
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
            user_text=PRIVATE_INSTRUCTION,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["scope"], "private")
        self.assertEqual(result["result"]["target"], "妈妈")
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["target_type"], "private")
        self.assertEqual(payload["target_id"], "4242")
        self.assertEqual(payload["text"], PRIVATE_ARGS["text"])
        self.assertEqual(
            self.db.get_meta(f"talk_to_at:{PRIVATE_SESSION}:4242", None), str(NOW)
        )

    def test_alias_resolves_to_canonical_name(self) -> None:
        create_person(self.db, name="妈妈", uin=4242, aliases=["老妈"])
        result = self.run_action(
            {"scope": "private", "target": "老妈", "text": "妈我到家啦"},
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["target"], "妈妈")

    def test_non_owner_session_refuses(self) -> None:
        # 未绑定的私聊会话：role_of 返回 None → 拒绝。
        result = self.run_action(
            dict(PRIVATE_ARGS),
            session_id="qq:private:999999",
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "private_requires_owner_session")
        self.assertEqual(self.outbox_rows(), [])

    def test_friend_session_refuses_private_scope(self) -> None:
        # 绑的是姐姐（friend）也不行：私聊单发只归 owner 会话。
        self.runtime.owner.bind_friend("qq:private:5566", "姐姐", "test")
        result = self.run_action(
            dict(PRIVATE_ARGS), session_id="qq:private:5566", trust=TrustLevel.PRIVATE
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "private_requires_owner_session")

    def test_owner_session_inside_group_turn_still_refuses(self) -> None:
        # 群轮里 scope=private 一律拒绝（role_of 只认私聊会话绑定）。
        result = self.run_action(dict(PRIVATE_ARGS), session_id=GROUP_SESSION)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "private_requires_owner_session")

    def test_card_without_uin_refuses_person_no_uin(self) -> None:
        create_person(self.db, name="王同学")
        result = self.run_action(
            {"scope": "private", "target": "王同学", "text": "你好"},
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "person_no_uin")
        self.assertEqual(self.outbox_rows(), [])

    def test_unknown_name_refuses_person_not_found(self) -> None:
        result = self.run_action(
            {"scope": "private", "target": "隔壁阿姨", "text": "你好"},
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "person_not_found")
        self.assertEqual(self.outbox_rows(), [])

    def test_ambiguous_name_refuses_as_person_not_found(self) -> None:
        create_person(self.db, name="杨老师", uin=30001, aliases=["老师"])
        create_person(self.db, name="李老师", uin=30002, aliases=["老师"])
        result = self.run_action(
            {"scope": "private", "target": "老师", "text": "你好"},
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "person_not_found:ambiguous_selector")
        self.assertEqual(self.outbox_rows(), [])

    def test_missing_target_refuses(self) -> None:
        result = self.run_action(
            {"scope": "private", "text": "你好"},
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_target")


# --------------------------------------------------- cooldown / text / config
class CooldownAndTextTests(RuntimeCase):
    def test_cooldown_blocks_second_send_to_same_target(self) -> None:
        first = self.run_action(dict(GROUP_ARGS))
        self.assertTrue(first["ok"])
        second = self.run_action(dict(GROUP_ARGS))
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "talk_cooldown")
        self.assertEqual(len(self.outbox_rows()), 1)

        third = self.run_action(dict(GROUP_ARGS), now=NOW + 11 * 60)
        self.assertTrue(third["ok"])
        self.assertEqual(len(self.outbox_rows()), 2)

    def test_cooldown_is_per_target(self) -> None:
        first = self.run_action(dict(GROUP_ARGS))
        self.assertTrue(first["ok"])
        second = self.run_action({"scope": "group", "target": "小红", "text": "小红好"})
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.outbox_rows()), 2)

    def test_private_and_group_targets_cool_down_separately(self) -> None:
        # 同一个 uin、不同会话：水位按 <session>:<target> 分键。
        self.bind_owner()
        create_person(self.db, name="妈妈", uin=10086)
        group = self.run_action(dict(GROUP_ARGS))
        self.assertTrue(group["ok"])
        private = self.run_action(
            {"scope": "private", "target": "妈妈", "text": "hi"},
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
        )
        self.assertTrue(private["ok"])

    def test_long_text_is_truncated_and_flagged(self) -> None:
        long_text = "啊" * 600
        result = self.run_action({"scope": "group", "target": "杨老师", "text": long_text})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["truncated"])
        self.assertEqual(result["result"]["chars"], 500)
        payload = json.loads(self.outbox_rows()[0]["payload_json"])
        self.assertEqual(payload["text"], "啊" * 500)

    def test_disabled_config_refuses_before_anything(self) -> None:
        self._tmp2 = tempfile.TemporaryDirectory(prefix="fatfish-talk-to-off-")
        config, db, runtime = make_runtime(
            Path(self._tmp2.name), name="off", talk_enabled=False
        )
        # Cleanups run LIFO: close the db before removing the temp dir.
        self.addCleanup(self._tmp2.cleanup)
        self.addCleanup(db.close)
        qq = FakeQQ()
        runtime.qq = qq
        result = runtime.execute_actions(
            [TurnAction("talk_to", dict(GROUP_ARGS))],
            session_id=GROUP_SESSION,
            reply="",
            now=NOW,
            trust=TrustLevel.GROUP,
            user_text=GROUP_INSTRUCTION,
        )[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "talk_disabled")
        self.assertEqual(qq.calls, [])
        self.assertEqual(
            db.query("SELECT * FROM outbox WHERE kind='qq_send'"), []
        )


# ----------------------------------------------------------------- audit
class AuditTests(RuntimeCase):
    def test_outbox_payload_shape_is_exact(self) -> None:
        self.run_action(dict(GROUP_ARGS))
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(
            set(payload.keys()), {"target_type", "target_id", "text"}
        )

    def test_decision_log_carries_names_never_uins(self) -> None:
        self.run_action(dict(GROUP_ARGS), user_text=GROUP_INSTRUCTION)
        self.bind_owner()
        create_person(self.db, name="妈妈", uin=4242)
        self.run_action(
            dict(PRIVATE_ARGS),
            session_id=PRIVATE_SESSION,
            trust=TrustLevel.PRIVATE,
            user_text=PRIVATE_INSTRUCTION,
        )
        # 拒绝行也在：裸数字目标进日志时被码掉。
        self.run_action(
            {"scope": "group", "target": "2002", "text": "hi"}, user_text=None
        )

        logs = self.talk_logs()
        self.assertGreaterEqual(len(logs), 3)
        ok_payloads = [
            json.loads(row["output_json"])
            for row in logs
            if row["outcome"] == "ok"
        ]
        self.assertEqual(len(ok_payloads), 2)
        self.assertEqual({p["target"] for p in ok_payloads}, {"杨老师", "妈妈"})
        blob = "".join(row["output_json"] for row in logs)
        for uin in ("10086", "4242", "2002"):
            self.assertNotIn(uin, blob)

    def test_tool_audit_records_allowed_attempts(self) -> None:
        self.run_action(dict(GROUP_ARGS))
        rows = self.tool_audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "allowed")


if __name__ == "__main__":
    unittest.main()
