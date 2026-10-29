"""T-0072: ``read_chat`` -- read QQ conversations through person cards.

Layers covered (one class per concern):

* ``ResolvePersonTargetTests`` -- the entities helper: name / alias / uin,
  not-found suggestions (never auto-select), ambiguity, empty input, and the
  card shape (uin only in the structured result -- it is a red line that a
  card's uin never reaches prompt material).
* ``PolicyTests``            -- scope is required text, private-only, one per turn.
* ``RegistrationTests``      -- ALLOWED_ACTIONS / TOOL_SCHEMA / SELF_ACTION_TOOLS /
  EXEMPT_TOOLS all know the new action; parse_turn_plan keeps it.
* ``ExplicitTests``          -- owner phrasings exempt the desire gate;
  negations and unrelated chatter do not.
* ``GroupScopeTests``        -- whitelist (default empty refuses), target
  default = current session group, count clamped by config, cooldown
  (watermark written before the fetch), evidence without any uin,
  decision_log rows, focus filter with uin / with name-only card /
  unresolved / ambiguous, empty history is success.
* ``PrivateScopeTests``      -- the nameless private read keeps the T-0072
  behaviour: the target uin comes from the session's owner_bindings row;
  unbound / non-QQ sessions refuse before any fetch.
* ``PrivateByCardTests``     -- T-0080: focus (or target) naming a person
  card with a uin reads THAT contact's friend history -- the card list is
  the owner's handwritten allowlist. No card / a card without a uin / a
  digit string without a card all refuse before any fetch; the cooldown
  keys on the card's uin so aliases share the window; the audit rows carry
  the card's canonical name, never a uin; explicit requests still bypass
  the desire gate.
* ``ForwardScopeTests``      -- explicit target wins, else the per-session
  cached ref written by ``handle_user_message(forward_ref=...)``, else refuse.
* ``AdapterForwardRefTests`` -- the QQ adapter records a merged-forward ref
  into the runtime's session cache when an inbound event carries one.

Offline only: temp database, fake QQ adapter, no network, no NapCat.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import TOOL_SCHEMA, ensure_default_persona
from fatfish.db import Database
from fatfish.memory.entities import (
    create_person,
    resolve_person_target,
)
from fatfish.platforms.qq import QQAdapter
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
COOLDOWN_KEY = f"read_chat_at:{GROUP_SESSION}:group:{GROUP_ID}"
FORWARD_KEY = f"forward_ref:{GROUP_SESSION}"


def make_runtime(
    tmp: Path,
    *,
    name: str = "read_chat",
    read_enabled: bool = True,
    groups: tuple[str, ...] = (GROUP_ID,),
    max_messages: int = 30,
    cooldown_minutes: int = 30,
    initial_threshold: float = 0.0,
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
        # -- this file is about what the read_chat branch does, not the gate.
        "[self_action]",
        "enabled = true",
        f"initial_threshold = {initial_threshold}",
        "threshold_floor = 0.0",
        "threshold_step = 0.01",
        "daily_cap = 100",
        "max_events_per_turn = 10",
        "quiet_hours = []",
        "[qq.read]",
        f"enabled = {'true' if read_enabled else 'false'}",
        f"groups = [{', '.join(groups)}]",
        f"max_messages = {max_messages}",
        f"cooldown_minutes = {cooldown_minutes}",
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
    """Duck-typed QQ adapter: recorded reads, canned rows, no network."""

    def __init__(self, rows=None, *, self_id: int = 999):
        self.rows = list(rows or [])
        self.self_id = self_id
        self.calls: list[dict] = []

    def fetch_group_history(
        self, group_id, count=20, message_seq=0, *, reverse_order=False, on_event=None
    ):
        self.calls.append({"kind": "group", "group_id": group_id, "count": count})
        return list(self.rows)

    def fetch_forward_messages(self, message_id, *, on_event=None):
        self.calls.append({"kind": "forward", "message_id": message_id})
        return list(self.rows)

    def fetch_friend_history(self, user_id, count=20, *, message_seq=0, on_event=None):
        self.calls.append({"kind": "friend", "user_id": user_id, "count": count})
        return list(self.rows)


def history_row(
    text: str,
    sender_id: int | str,
    name: str,
    *,
    when: float = NOW,
) -> dict:
    """One OneBot-shaped message event row (text segment only)."""
    return {
        "time": when,
        "user_id": sender_id,
        "sender": {"user_id": sender_id, "card": name, "nickname": name},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def forward_event(resid: str) -> dict:
    """A group message carrying one modern merged-forward json segment.

    Same ark shape ``extract_forward_ref`` recognises (T-0071's fixture):
    ``app == "com.tencent.multimsg"`` with the fetch key in
    ``meta.detail.resid``.
    """
    ark = {
        "app": "com.tencent.multimsg",
        "config": {"forward": 1, "autosize": 1, "round": 1, "type": "normal", "width": 300},
        "desc": "聊天记录",
        "extra": {"filename": "uniseq-1", "tsum": 3},
        "meta": {
            "detail": {
                "news": [{"text": "阿鱼: 你们看这个"}],
                "resid": resid,
                "source": "群聊的聊天记录",
                "summary": "查看3条转发消息",
                "uniseq": "uniseq-1",
            }
        },
        "news": [{"text": "阿鱼: 你们看这个"}],
        "prompt": "[聊天记录]",
        "ver": "0.0.0.99",
        "view": "contact",
    }
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(GROUP_ID),
        "user_id": 1001,
        "self_id": 999,
        "time": NOW,
        "message": [{"type": "json", "data": {"data": json.dumps(ark, ensure_ascii=False)}}],
    }


# ------------------------------------------------------------ person targets
class ResolvePersonTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-read-chat-")
        self.tmp = Path(self._tmp.name)
        self.db = Database(self.tmp / "entities.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_resolves_exact_name_with_uin(self) -> None:
        create_person(self.db, name="杨老师", uin=10086, relation="teacher")
        result = resolve_person_target(self.db, "杨老师")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["name"], "杨老师")
        self.assertEqual(result["uin"], "10086")
        self.assertEqual(result["relation"], "teacher")
        self.assertEqual(result["id"], "person:qq:10086")

    def test_resolves_alias_and_uin_and_id(self) -> None:
        create_person(self.db, name="李老师", uin=20001, aliases=["老李"])
        by_alias = resolve_person_target(self.db, "老李")
        self.assertTrue(by_alias["ok"])
        self.assertEqual(by_alias["uin"], "20001")
        by_uin = resolve_person_target(self.db, "20001")
        self.assertTrue(by_uin["ok"])
        self.assertEqual(by_uin["name"], "李老师")
        by_id = resolve_person_target(self.db, "person:qq:20001")
        self.assertTrue(by_id["ok"])

    def test_card_without_uin_reports_empty_uin(self) -> None:
        create_person(self.db, name="王同学")
        result = resolve_person_target(self.db, "王同学")
        self.assertTrue(result["ok"])
        self.assertEqual(result["uin"], "")

    def test_not_found_gives_suggestions_never_auto_selects(self) -> None:
        create_person(self.db, name="杨老师")
        create_person(self.db, name="杨教练")
        result = resolve_person_target(self.db, "杨老")
        self.assertFalse(result["ok"])
        self.assertIn("person_not_found", result["error"])
        suggestions = result.get("suggestions") or []
        self.assertTrue(1 <= len(suggestions) <= 3)
        self.assertTrue(all("杨" in name for name in suggestions))

    def test_not_found_without_close_match_has_no_suggestions(self) -> None:
        create_person(self.db, name="杨老师")
        result = resolve_person_target(self.db, "火星人")
        self.assertFalse(result["ok"])
        # The key is always present on person_not_found; an empty list means
        # "nothing close enough to suggest".
        self.assertEqual(result.get("suggestions"), [])

    def test_ambiguous_selector_is_reported_not_resolved(self) -> None:
        # Both cards must expose 「老师」 as their own alias: the selector only
        # reports ambiguity for exact name/alias collisions, not substrings.
        create_person(self.db, name="杨老师", uin=30001, aliases=["老师"])
        create_person(self.db, name="李老师", uin=30002, aliases=["老师"])
        result = resolve_person_target(self.db, "老师")
        self.assertFalse(result["ok"])
        self.assertIn("ambiguous_selector", result["error"])
        self.assertNotIn("uin", result)  # nothing was chosen

    def test_empty_selector_is_reported(self) -> None:
        for text in ("", "   "):
            with self.subTest(text=text):
                result = resolve_person_target(self.db, text)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "empty_selector")


# --------------------------------------------------------------------- policy
class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyEngine()

    def test_private_with_scope_is_allowed(self) -> None:
        decision = self.policy.validate(
            "read_chat", {"scope": "group"}, trust=TrustLevel.PRIVATE
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_missing_scope_is_denied(self) -> None:
        for args in ({}, {"scope": "  "}, {"scope": None}):
            with self.subTest(args=args):
                decision = self.policy.validate(
                    "read_chat", args, trust=TrustLevel.PRIVATE
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "missing_scope")

    def test_group_trust_is_denied(self) -> None:
        decision = self.policy.validate(
            "read_chat", {"scope": "group"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "group_not_allowed")

    def test_one_call_per_turn_and_read_only(self) -> None:
        spec = ACTION_SPECS["read_chat"]
        self.assertEqual(spec.max_calls, 1)
        self.assertFalse(spec.write)
        self.assertFalse(spec.allow_group)
        self.assertEqual(spec.min_trust, TrustLevel.PRIVATE)
        decision = self.policy.validate(
            "read_chat",
            {"scope": "group"},
            trust=TrustLevel.PRIVATE,
            count=1,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "too_many_tool_calls")


# -------------------------------------------------------------- registration
class RegistrationTests(unittest.TestCase):
    def test_allowed_actions_contains_read_chat(self) -> None:
        self.assertIn("read_chat", ALLOWED_ACTIONS)

    def test_self_action_and_exempt_tools_contain_read_chat(self) -> None:
        self.assertIn("read_chat", SELF_ACTION_TOOLS)
        # T-0082: talk_to joins the exempt set WITHOUT being a self-action --
        # it has no desire gate at all, the owner's words are its only gate.
        self.assertEqual(EXEMPT_TOOLS, SELF_ACTION_TOOLS | {"talk_to"})

    def test_tool_schema_documents_read_chat(self) -> None:
        self.assertIn("- read_chat:", TOOL_SCHEMA)
        self.assertIn("scope", TOOL_SCHEMA)
        self.assertIn("count", TOOL_SCHEMA)
        self.assertIn("focus", TOOL_SCHEMA)
        # The schema must teach the model that a nameless card is matched by
        # name, and that the result is next-turn material, not this turn's text.
        self.assertIn("下一轮", TOOL_SCHEMA)
        # T-0080: the private scope must invite card-backed reads (the card
        # list is the owner's handwritten allowlist) instead of teaching the
        # model to refuse them -- the old 「也填不了别人」 line is what made
        # her turn down a read she was allowed to do.
        self.assertIn("手写的名单", TOOL_SCHEMA)
        self.assertNotIn("也填不了别人", TOOL_SCHEMA)

    def test_parse_turn_plan_keeps_read_chat(self) -> None:
        raw = json.dumps(
            {
                "messages": ["嗯"],
                "actions": [
                    {"tool": "read_chat", "args": {"scope": "group", "count": 20}}
                ],
            },
            ensure_ascii=False,
        )
        plan = parse_turn_plan(raw)
        self.assertEqual([a.tool for a in plan.actions], ["read_chat"])
        self.assertEqual(plan.actions[0].args["scope"], "group")


# ------------------------------------------------------------------- explicit
class ExplicitTests(unittest.TestCase):
    POSITIVES = (
        "看看群里说了什么",
        "翻翻聊天记录",
        "帮我看看群消息",
        "群里都在聊什么",
        "群里在聊啥",
        "去翻一下这个群在聊什么",
        "看看杨老师说了什么",
    )
    NEGATIVES = (
        "别翻聊天记录了",
        "不用看群消息了",
        "不要翻群消息",
        "今天群里没人说话",
        "我在群里说了一句话而已",
        "随便聊聊",
        # 「不用看群里说了什么」是已知漏判：否定剔除只摘动词（不用看），剩下的
        # 「群里说了什么」本身就是裸问句规则的正例。宁漏勿误——豁免只是放行
        # 渴望闸门，模型真要动作还得自己输出 read_chat 且过白名单/冷却。
    )

    def test_positives_exempt_the_gate(self) -> None:
        for phrase in self.POSITIVES:
            with self.subTest(phrase=phrase):
                self.assertTrue(explicit_request(phrase, "read_chat"), phrase)

    def test_negatives_do_not_exempt(self) -> None:
        for phrase in self.NEGATIVES:
            with self.subTest(phrase=phrase):
                self.assertFalse(explicit_request(phrase, "read_chat"), phrase)

    def test_other_tools_never_get_read_chat_exemption(self) -> None:
        # 「翻翻聊天记录」is deliberately absent here: 记录 is one of
        # search_memory's own target words, so the pre-existing rules read it
        # as a memory request too -- an accepted overlap, not contamination.
        cross_safe = tuple(p for p in self.POSITIVES if "聊天记录" not in p)
        for phrase in cross_safe:
            for tool in ("search_memory", "read_qzone", "make_photo", "search_videos"):
                with self.subTest(phrase=phrase, tool=tool):
                    self.assertFalse(explicit_request(phrase, tool))

    def test_private_colloquial_phrasings_exempt_the_gate(self) -> None:
        # T-0081: the boss's own phrasings twice produced zero read_chat
        # calls; the colloquial private-scope shapes must be spelled out.
        positives = (
            "看看妈妈的私聊",
            "翻翻妈妈的对话",
            "你现在可以看到妈妈的对话了吗",
            "你能看到妈妈说了什么吗",
            "QQ号357750323你看看有没有",
        )
        for phrase in positives:
            with self.subTest(phrase=phrase):
                self.assertTrue(explicit_request(phrase, "read_chat"), phrase)

    def test_private_colloquial_negatives_do_not_exempt(self) -> None:
        negatives = (
            "我看到消息了",
            "别看了",
            "我看不到他们的对话",
            "妈妈今天跟我说了很多话",
        )
        for phrase in negatives:
            with self.subTest(phrase=phrase):
                self.assertFalse(explicit_request(phrase, "read_chat"), phrase)


# -------------------------------------------------------------- group scope
class RuntimeCase(unittest.TestCase):
    """Shared harness: temp runtime + fake QQ adapter."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-read-chat-")
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
        tool: str = "read_chat",
        session_id: str = GROUP_SESSION,
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

    def read_chat_logs(self) -> list:
        return self.db.query(
            "SELECT * FROM decision_log WHERE kind='read_chat' ORDER BY id"
        )

    def bind_session(
        self, session_id: str = PRIVATE_SESSION, name: str = "主人哥哥"
    ) -> None:
        self.runtime.owner.bind(session_id, name, "test")

    def base_rows(self) -> list[dict]:
        return [
            history_row("早上好", 1001, "小明", when=NOW - 500),
            history_row("大家好", 10086, "杨老师", when=NOW - 400),
            history_row("今天交作业", 10086, "杨老师", when=NOW - 300),
            history_row("好的老师", 1001, "小明", when=NOW - 200),
            history_row("收到", 2002, "小红", when=NOW - 100),
        ]


class GroupScopeTests(RuntimeCase):
    def test_disabled_by_config_refuses_without_fetch(self) -> None:
        self._tmp2 = tempfile.TemporaryDirectory(prefix="fatfish-read-chat-off-")
        config, db, runtime = make_runtime(
            Path(self._tmp2.name), name="off", read_enabled=False
        )
        # Cleanups run LIFO: close the db before removing the temp dir.
        self.addCleanup(self._tmp2.cleanup)
        self.addCleanup(db.close)
        qq = FakeQQ()
        runtime.qq = qq
        results = runtime.execute_actions(
            [TurnAction("read_chat", {"scope": "group"})],
            session_id=GROUP_SESSION,
            reply="",
            now=NOW,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "read_chat_disabled")
        self.assertEqual(qq.calls, [])

    def test_whitelisted_group_reads_history_into_evidence(self) -> None:
        self.qq.rows = self.base_rows()
        result = self.run_action({"scope": "group"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["scope"], "group")
        self.assertEqual(result["result"]["count"], 5)
        # target defaults to the current session's group
        self.assertEqual(
            self.qq.calls, [{"kind": "group", "group_id": GROUP_ID, "count": 30}]
        )

        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "read_chat")
        self.assertEqual(row["source"], "qq")
        self.assertEqual(row["session_id"], GROUP_SESSION)
        self.assertIn("杨老师", row["content"])
        self.assertIn("今天交作业", row["content"])

    def test_evidence_never_contains_uins(self) -> None:
        self.qq.rows = self.base_rows()
        result = self.run_action({"scope": "group"})
        self.assertTrue(result["ok"])
        content = self.evidence_rows()[0]["content"]
        for uin in ("1001", "10086", "2002", "999"):
            self.assertNotIn(uin, content)
        blob = json.dumps(result, ensure_ascii=False)
        for uin in ("1001", "10086", "2002"):
            self.assertNotIn(uin, blob)

    def test_group_outside_whitelist_refuses_before_fetch(self) -> None:
        self.qq.rows = self.base_rows()
        result = self.run_action({"scope": "group", "target": "424242"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_not_whitelisted")
        self.assertEqual(self.qq.calls, [])
        # The cooldown watermark is a fetch-adjacent write: a refusal that
        # never fetched must not consume the next window either.
        self.assertIsNone(self.db.get_meta(COOLDOWN_KEY, None))
        self.assertEqual(self.read_chat_logs()[0]["outcome"], "group_not_whitelisted")

    def test_empty_whitelist_refuses_every_group(self) -> None:
        self._tmp2 = tempfile.TemporaryDirectory(prefix="fatfish-read-chat-empty-")
        config, db, runtime = make_runtime(
            Path(self._tmp2.name), name="empty", groups=()
        )
        # Cleanups run LIFO: close the db before removing the temp dir.
        self.addCleanup(self._tmp2.cleanup)
        self.addCleanup(db.close)
        qq = FakeQQ(rows=self.base_rows())
        runtime.qq = qq
        results = runtime.execute_actions(
            [TurnAction("read_chat", {"scope": "group"})],
            session_id=GROUP_SESSION,
            reply="",
            now=NOW,
            trust=TrustLevel.PRIVATE,
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "group_not_whitelisted")
        self.assertEqual(qq.calls, [])

    def test_non_group_session_needs_explicit_target(self) -> None:
        result = self.run_action({"scope": "group"}, session_id=PRIVATE_SESSION)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_group_target")
        self.assertEqual(self.qq.calls, [])

    def test_invalid_scope_refuses(self) -> None:
        result = self.run_action({"scope": "everything"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_scope")

    def test_count_is_clamped_by_config_and_hard_cap(self) -> None:
        self.qq.rows = self.base_rows()
        # config max_messages=30 caps a greedy ask; the hard cap keeps 50.
        # ``desire`` rides along so the desire gate never trips, and the
        # clock advances past the cooldown between the calls.
        self.run_action({"scope": "group", "count": 99, "desire": 1.0})
        self.assertEqual(self.qq.calls[0]["count"], 30)
        self.run_action(
            {"scope": "group", "count": 5, "desire": 1.0}, now=NOW + 31 * 60
        )
        self.assertEqual(self.qq.calls[1]["count"], 5)
        self.run_action(
            {"scope": "group", "count": "很多", "desire": 1.0}, now=NOW + 62 * 60
        )
        self.assertEqual(self.qq.calls[2]["count"], 30)

    def test_cooldown_blocks_second_read_until_window_passes(self) -> None:
        self.qq.rows = self.base_rows()
        first = self.run_action({"scope": "group", "desire": 1.0})
        self.assertTrue(first["ok"])
        self.assertEqual(self.db.get_meta(COOLDOWN_KEY, None), str(NOW))

        second = self.run_action({"scope": "group", "desire": 1.0})
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "cooldown")
        self.assertEqual(len(self.qq.calls), 1)
        outcomes = [row["outcome"] for row in self.read_chat_logs()]
        self.assertIn("cooldown", outcomes)

        third = self.run_action(
            {"scope": "group", "desire": 1.0}, now=NOW + 31 * 60
        )
        self.assertTrue(third["ok"])
        self.assertEqual(len(self.qq.calls), 2)

    def test_empty_history_is_success_with_zero_rows(self) -> None:
        result = self.run_action({"scope": "group"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["count"], 0)
        self.assertEqual(
            self.evidence_rows()[0]["content"], "（这段时间没有可读的消息）"
        )

    def test_group_trust_is_refused_at_policy_level(self) -> None:
        result = self.run_action({"scope": "group"}, trust=TrustLevel.GROUP)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_not_allowed")
        self.assertEqual(self.qq.calls, [])

    def test_decision_log_records_scope_count_and_cooldown(self) -> None:
        self.qq.rows = self.base_rows()
        self.run_action({"scope": "group", "focus": "", "desire": 1.0})
        self.run_action({"scope": "group", "desire": 1.0})
        rows = self.read_chat_logs()
        ok_rows = [row for row in rows if row["outcome"] == "ok"]
        self.assertEqual(len(ok_rows), 1)
        payload = json.loads(ok_rows[0]["output_json"])
        self.assertEqual(payload["scope"], "group")
        self.assertEqual(payload["count"], 5)
        self.assertIn("explicit", payload)
        # The audit must stay identity-free too.
        self.assertNotIn("uin", ok_rows[0]["output_json"])


class FocusTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.qq.rows = self.base_rows()

    def make_card(self, **kwargs) -> None:
        create_person(self.db, **kwargs)

    def test_focus_with_uin_filters_by_sender_id_with_context(self) -> None:
        self.make_card(name="杨老师", uin=10086)
        result = self.run_action({"scope": "group", "focus": "杨老师"})

        self.assertTrue(result["ok"], result)
        # Hits: rows 2 and 3 (indexes 1, 2). Context ±2 keeps 0..4 minus none
        # beyond the end -- here the whole page is inside the window.
        self.assertEqual(result["result"]["count"], 5)
        content = self.evidence_rows()[0]["content"]
        self.assertIn("杨老师: 今天交作业", content)
        self.assertNotIn("10086", content)

    def test_focus_uin_match_does_not_leak_other_senders_outside_context(self) -> None:
        rows = [
            history_row(f"话{index}", 3000 + index, f"同学{index}", when=NOW - index)
            for index in range(12)
        ]
        rows[6] = history_row("大家好", 10086, "杨老师", when=NOW - 6)
        self.qq.rows = rows
        self.make_card(name="杨老师", uin=10086)
        result = self.run_action({"scope": "group", "focus": "杨老师"})

        self.assertTrue(result["ok"])
        # hit at index 6 -> window 4..8 -> five rows.
        self.assertEqual(result["result"]["count"], 5)
        content = self.evidence_rows()[0]["content"]
        self.assertIn("杨老师: 大家好", content)
        self.assertNotIn("话2", content)
        self.assertNotIn("话10", content)

    def test_focus_without_uin_matches_name_and_aliases_exactly(self) -> None:
        self.make_card(name="小明同学", aliases=["小明"])
        result = self.run_action({"scope": "group", "focus": "小明"})

        self.assertTrue(result["ok"])
        content = self.evidence_rows()[0]["content"]
        self.assertIn("小明: 早上好", content)
        self.assertIn("小明: 好的老师", content)
        # 杨老师's rows are neither hits nor inside a ±2 window of both hits
        # here (indexes 0 and 3 -> window 0..5 covers everything on this page,
        # so assert on the count instead of absence).
        self.assertEqual(result["result"]["count"], 5)

    def test_focus_name_match_is_exact_not_substring(self) -> None:
        self.make_card(name="明")
        result = self.run_action({"scope": "group", "focus": "明"})
        self.assertTrue(result["ok"])
        # 「明」 is not an exact sender_name on this page -> no hits.
        self.assertEqual(result["result"]["count"], 0)
        self.assertIn("没有找到", self.evidence_rows()[0]["content"])

    def test_unresolved_focus_refuses_before_fetch(self) -> None:
        result = self.run_action({"scope": "group", "focus": "陌生人"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "focus_unresolved")
        self.assertEqual(self.qq.calls, [])
        self.assertEqual(self.evidence_rows(), [])
        self.assertEqual(self.read_chat_logs()[0]["outcome"], "focus_unresolved")

    def test_ambiguous_focus_refuses(self) -> None:
        self.make_card(name="杨老师", uin=30001, aliases=["老师"])
        self.make_card(name="李老师", uin=30002, aliases=["老师"])
        result = self.run_action({"scope": "group", "focus": "老师"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "focus_unresolved")
        self.assertEqual(self.qq.calls, [])

    def test_suggestions_are_not_auto_selected(self) -> None:
        self.make_card(name="杨老师")
        result = self.run_action({"scope": "group", "focus": "杨老"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.qq.calls, [])


# ----------------------------------------------------------- private scope
class PrivateScopeTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.qq.rows = self.base_rows()

    def test_unbound_session_refuses_before_fetch(self) -> None:
        result = self.run_action({"scope": "private"}, session_id=PRIVATE_SESSION)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "session_not_bound")
        self.assertEqual(self.qq.calls, [])

    def test_non_qq_session_shape_refuses(self) -> None:
        self.bind_session("web:private:abc")
        result = self.run_action({"scope": "private"}, session_id="web:private:abc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "private_requires_bound_session")
        self.assertEqual(self.qq.calls, [])

    def test_bound_session_reads_friend_history_by_binding(self) -> None:
        self.bind_session()
        result = self.run_action({"scope": "private"}, session_id=PRIVATE_SESSION)

        self.assertTrue(result["ok"], result)
        # No name given: the legacy T-0072 behaviour stands -- the uin comes
        # from the session binding, not from anything the model supplied.
        self.assertEqual(
            self.qq.calls, [{"kind": "friend", "user_id": "1001", "count": 30}]
        )
        self.assertEqual(result["result"]["scope"], "private")
        content = self.evidence_rows()[0]["content"]
        self.assertNotIn("1001", content)

    def test_private_scope_audit_has_no_uin(self) -> None:
        self.bind_session()
        self.run_action({"scope": "private"}, session_id=PRIVATE_SESSION)
        row = self.read_chat_logs()[0]
        self.assertEqual(row["outcome"], "ok")
        self.assertNotIn("1001", row["output_json"])

    def test_private_scope_also_respects_cooldown(self) -> None:
        self.bind_session()
        first = self.run_action(
            {"scope": "private", "desire": 1.0}, session_id=PRIVATE_SESSION
        )
        self.assertTrue(first["ok"])
        second = self.run_action(
            {"scope": "private", "desire": 1.0}, session_id=PRIVATE_SESSION
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "cooldown")
        self.assertEqual(len(self.qq.calls), 1)


# ------------------------------------------------- private scope via cards
class PrivateByCardTests(RuntimeCase):
    """T-0080: a person-card name reads that contact's private chat.

    The card list is the owner's handwritten allowlist: focus (or target,
    focus winning) naming a card that carries a uin reads THAT person's
    friend history. A name with no card, or a card without a uin, refuses
    before any fetch. The nameless read keeps the legacy session-bound
    behaviour (pinned in PrivateScopeTests).
    """

    def make_card(self, **kwargs) -> None:
        create_person(self.db, **kwargs)

    def friend_rows(self) -> list[dict]:
        # A real both-sides conversation. The bot's own rows (self_id 999)
        # are dropped by extract_history_rows, mom's rows stay.
        return [
            history_row("儿子好啊", 4242, "妈妈", when=NOW - 300),
            history_row("妈我回来了", 999, "大肥鱼", when=NOW - 200),
            history_row("记得吃饭", 4242, "妈妈", when=NOW - 100),
        ]

    def test_card_with_uin_reads_that_persons_history(self) -> None:
        self.make_card(name="妈妈", uin=4242)
        self.qq.rows = self.friend_rows()
        result = self.run_action(
            {"scope": "private", "focus": "妈妈", "desire": 1.0},
            session_id=PRIVATE_SESSION,
        )

        self.assertTrue(result["ok"], result)
        # The fetch goes to the CARD's uin -- the session (1001) is beside
        # the point.
        self.assertEqual(
            self.qq.calls, [{"kind": "friend", "user_id": "4242", "count": 30}]
        )
        self.assertEqual(result["result"]["scope"], "private")
        self.assertEqual(result["result"]["count"], 2)
        row = self.evidence_rows()[0]
        # The title is the card's canonical name; the body renders names only.
        self.assertEqual(row["title"], "私聊记录·妈妈")
        for uin in ("4242", "1001", "999"):
            self.assertNotIn(uin, row["content"])
        self.assertIn("妈妈: 儿子好啊", row["content"])
        self.assertIn("妈妈: 记得吃饭", row["content"])
        self.assertNotIn("妈我回来了", row["content"])
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("4242", blob)
        # The audit row is outcome=private_by_card and names the card, not
        # the uin.
        ok_rows = [
            log for log in self.read_chat_logs() if log["outcome"] == "private_by_card"
        ]
        self.assertEqual(len(ok_rows), 1)
        payload = json.loads(ok_rows[0]["output_json"])
        self.assertEqual(payload["scope"], "private")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["focus"], "妈妈")
        self.assertNotIn("4242", ok_rows[0]["output_json"])

    def test_by_card_read_does_not_need_a_bound_session(self) -> None:
        # The card list is the authorization; the session binding only gates
        # the nameless legacy read.
        self.make_card(name="杨老师", uin=10086)
        self.qq.rows = [history_row("作业交了吗", 10086, "杨老师", when=NOW - 60)]
        result = self.run_action(
            {"scope": "private", "focus": "杨老师"}, session_id=PRIVATE_SESSION
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qq.calls[0]["user_id"], "10086")

    def test_target_field_resolves_when_focus_is_missing(self) -> None:
        self.make_card(name="妈妈", uin=4242, aliases=["老妈"])
        self.qq.rows = self.friend_rows()
        result = self.run_action(
            {"scope": "private", "target": "老妈", "desire": 1.0},
            session_id=PRIVATE_SESSION,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qq.calls[0]["user_id"], "4242")
        # The title still carries the canonical name, not the alias.
        self.assertEqual(self.evidence_rows()[0]["title"], "私聊记录·妈妈")

    def test_card_without_uin_refuses_person_no_uin(self) -> None:
        self.make_card(name="王同学")
        result = self.run_action(
            {"scope": "private", "focus": "王同学"}, session_id=PRIVATE_SESSION
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "person_no_uin")
        self.assertEqual(self.qq.calls, [])
        self.assertEqual(self.evidence_rows(), [])
        self.assertEqual(self.read_chat_logs()[0]["outcome"], "person_no_uin")

    def test_unknown_name_refuses_person_not_found(self) -> None:
        self.make_card(name="妈妈", uin=4242)
        result = self.run_action(
            {"scope": "private", "focus": "隔壁阿姨"}, session_id=PRIVATE_SESSION
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "person_not_found")
        self.assertEqual(self.qq.calls, [])
        self.assertEqual(self.evidence_rows(), [])
        self.assertEqual(self.read_chat_logs()[0]["outcome"], "person_not_found")

    def test_ambiguous_name_refuses_as_person_not_found(self) -> None:
        self.make_card(name="杨老师", uin=30001, aliases=["老师"])
        self.make_card(name="李老师", uin=30002, aliases=["老师"])
        result = self.run_action(
            {"scope": "private", "focus": "老师"}, session_id=PRIVATE_SESSION
        )
        self.assertFalse(result["ok"])
        # The resolve error kind rides along (morning-report trail), but no
        # entity id / uin ever does.
        self.assertEqual(result["error"], "person_not_found:ambiguous_selector")
        self.assertEqual(self.qq.calls, [])

    def test_digit_name_without_card_refuses(self) -> None:
        # A raw uin is not a passport: without a card bound to it the read
        # refuses, so the model can never smuggle a target past the allowlist.
        self.make_card(name="妈妈", uin=4242)
        result = self.run_action(
            {"scope": "private", "target": "88888"}, session_id=PRIVATE_SESSION
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "person_not_found")
        self.assertEqual(self.qq.calls, [])
        # The attempt itself is auditable (that is the point of the trail):
        # the raw name is what the model typed, and no card uin appears.
        row = self.read_chat_logs()[0]
        self.assertEqual(row["outcome"], "person_not_found")
        self.assertIn("88888", row["output_json"])
        self.assertNotIn("4242", row["output_json"])

    def test_digit_name_with_card_reads_by_card(self) -> None:
        # resolve_person_target matches a digit name by uin, so an existing
        # card is reachable -- through the same card gate as any name.
        self.make_card(name="妈妈", uin=4242)
        self.qq.rows = self.friend_rows()
        result = self.run_action(
            {"scope": "private", "focus": "4242", "desire": 1.0},
            session_id=PRIVATE_SESSION,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qq.calls[0]["user_id"], "4242")
        self.assertEqual(self.evidence_rows()[0]["title"], "私聊记录·妈妈")
        # Once the identity resolves, the audit carries the card name -- the
        # digit string the model typed never reaches the ledger.
        row = self.read_chat_logs()[-1]
        self.assertEqual(row["outcome"], "private_by_card")
        self.assertNotIn("4242", row["output_json"])

    def test_cooldown_is_shared_by_aliases_of_same_card(self) -> None:
        self.make_card(name="妈妈", uin=4242, aliases=["老妈"])
        self.qq.rows = self.friend_rows()
        first = self.run_action(
            {"scope": "private", "focus": "妈妈", "desire": 1.0},
            session_id=PRIVATE_SESSION,
        )
        self.assertTrue(first["ok"])
        # Same person via her alias -> the same uin -> the same cooldown key.
        second = self.run_action(
            {"scope": "private", "focus": "老妈", "desire": 1.0},
            session_id=PRIVATE_SESSION,
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "cooldown")
        self.assertEqual(len(self.qq.calls), 1)
        cooldown_rows = [
            log for log in self.read_chat_logs() if log["outcome"] == "cooldown"
        ]
        self.assertEqual(len(cooldown_rows), 1)
        self.assertEqual(json.loads(cooldown_rows[0]["output_json"])["focus"], "妈妈")
        # After the window the same alias reads again.
        third = self.run_action(
            {"scope": "private", "focus": "老妈", "desire": 1.0},
            session_id=PRIVATE_SESSION,
            now=NOW + 31 * 60,
        )
        self.assertTrue(third["ok"])
        self.assertEqual(len(self.qq.calls), 2)

    def test_no_name_keeps_the_legacy_bound_read_on_the_same_cooldown(self) -> None:
        # A card bound to the session uin resolves to the same target the
        # legacy read uses, so both routes share one cooldown window.
        self.make_card(name="主人哥哥", uin=1001)
        self.bind_session()
        self.qq.rows = self.friend_rows()
        legacy = self.run_action({"scope": "private"}, session_id=PRIVATE_SESSION)
        self.assertTrue(legacy["ok"])
        by_card = self.run_action(
            {"scope": "private", "focus": "主人哥哥", "desire": 1.0},
            session_id=PRIVATE_SESSION,
        )
        self.assertFalse(by_card["ok"])
        self.assertEqual(by_card["error"], "cooldown")
        self.assertEqual(len(self.qq.calls), 1)

    def test_explicit_request_bypasses_the_desire_gate(self) -> None:
        self._tmp2 = tempfile.TemporaryDirectory(prefix="fatfish-read-chat-explicit-")
        config, db, runtime = make_runtime(
            Path(self._tmp2.name), name="explicit", initial_threshold=0.9
        )
        # Cleanups run LIFO: close the db before removing the temp dir.
        self.addCleanup(self._tmp2.cleanup)
        self.addCleanup(db.close)
        qq = FakeQQ(rows=self.friend_rows())
        runtime.qq = qq
        create_person(db, name="妈妈", uin=4242)
        args = {"scope": "private", "focus": "妈妈", "desire": 0.1}

        # Without the owner's words the same ask dies at the desire gate,
        # before the read_chat branch ever runs.
        blocked = runtime.execute_actions(
            [TurnAction("read_chat", args)],
            session_id=PRIVATE_SESSION,
            reply="",
            now=NOW,
            trust=TrustLevel.PRIVATE,
        )[0]
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "below_threshold")
        self.assertEqual(qq.calls, [])

        # 「翻翻聊天记录」 is an explicit read_chat request: the gate steps
        # aside and the by-card read proceeds.
        allowed = runtime.execute_actions(
            [TurnAction("read_chat", args)],
            session_id=PRIVATE_SESSION,
            reply="",
            now=NOW + 1,
            trust=TrustLevel.PRIVATE,
            user_text="翻翻聊天记录",
        )[0]
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(qq.calls[0]["user_id"], "4242")


# ------------------------------------------------------------ forward scope
class ForwardScopeTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.qq.rows = self.base_rows()

    def test_explicit_target_wins(self) -> None:
        result = self.run_action({"scope": "forward", "target": "ResId-1"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.qq.calls, [{"kind": "forward", "message_id": "ResId-1"}]
        )

    def test_cached_forward_ref_is_used_when_target_missing(self) -> None:
        self.db.set_meta(FORWARD_KEY, "ResId-9")
        result = self.run_action({"scope": "forward"})
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.qq.calls, [{"kind": "forward", "message_id": "ResId-9"}]
        )

    def test_no_cache_and_no_target_refuses(self) -> None:
        result = self.run_action({"scope": "forward"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_forward_cached")
        self.assertEqual(self.qq.calls, [])

    def test_handle_user_message_caches_latest_forward_ref(self) -> None:
        self.runtime._remember_forward_ref(GROUP_SESSION, "ResId-1")
        self.runtime._remember_forward_ref(GROUP_SESSION, "ResId-2")
        self.assertEqual(self.db.get_meta(FORWARD_KEY, None), "ResId-2")

    def test_remember_forward_ref_ignores_blank(self) -> None:
        self.runtime._remember_forward_ref(GROUP_SESSION, "  ")
        self.assertIsNone(self.db.get_meta(FORWARD_KEY, None))


# ------------------------------------------------------- adapter plumbing
class RecordingRuntime:
    """Minimal runtime double: only what the forward-ref cache touches."""

    def __init__(self):
        # CatchUpService (built by QQAdapter) grabs runtime.db at construction.
        self.db = None
        self.forward_refs: list[tuple[str, str]] = []

    def _remember_forward_ref(self, session_id: str, ref: str) -> None:
        self.forward_refs.append((session_id, str(ref)))


class AdapterForwardRefTests(unittest.TestCase):
    """The adapter caches a merged-forward ref before anything else.

    A pure forward card has no text segment, so ``parse_event`` drops it --
    the ref must still reach the runtime's per-session cache (T-0072), which
    is exactly what these tests pin. No turn runs here, so the runtime double
    stays minimal.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fatfish-read-chat-adapter-")
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _runtime = make_runtime(self.tmp, name="adapter")
        # Cleanups run LIFO: close the db before removing the temp dir.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)
        self.runtime = RecordingRuntime()
        self.adapter = QQAdapter(self.runtime, self.config)

    def test_textless_forward_event_is_dropped_but_cached(self) -> None:
        result = self.adapter.handle_event(forward_event("ResId-77"))
        self.assertIsNone(result)  # no turn: nothing readable in the card
        self.assertEqual(self.runtime.forward_refs, [(GROUP_SESSION, "ResId-77")])

    def test_forward_cache_keeps_only_the_latest_ref(self) -> None:
        self.adapter.handle_event(forward_event("ResId-1"))
        self.adapter.handle_event(forward_event("ResId-2"))
        self.assertEqual(self.runtime.forward_refs[-1], (GROUP_SESSION, "ResId-2"))
        self.assertEqual(len(self.runtime.forward_refs), 2)

    def test_event_without_forward_ref_caches_nothing(self) -> None:
        self.adapter.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": int(GROUP_ID),
                "user_id": 1001,
                "self_id": 999,
                "time": NOW,
                "message": [],
            }
        )
        self.assertEqual(self.runtime.forward_refs, [])

    def test_cache_direct_targets_private_session_shape(self) -> None:
        self.adapter._cache_forward_ref(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 1001,
                "message": [],
            }
        )
        # No ref in the event -> nothing to cache.
        self.assertEqual(self.runtime.forward_refs, [])
        self.adapter._cache_forward_ref(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 1001,
                "message": forward_event("ResId-9")["message"],
            }
        )
        self.assertEqual(self.runtime.forward_refs, [("qq:private:1001", "ResId-9")])

    def test_cache_survives_a_runtime_without_the_helper(self) -> None:
        class Bare:
            pass

        self.adapter.runtime = Bare()
        self.adapter._cache_forward_ref(forward_event("ResId-1"))  # must not raise


if __name__ == "__main__":
    unittest.main()
