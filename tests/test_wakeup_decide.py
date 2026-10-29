"""T-00xx: 唤醒到点后的动作决策（chat / browse / silent）。

离线：LLM 与 B 站采集都打桩，数据库在临时目录，全程不联网。覆盖的契约：

* ``chat`` 照旧进 outbox（一条 ``proactive_message``）；
* ``browse`` 不发消息、只采集一次，且不进主动消息的 90 分钟闸门/每日额度；
* ``silent`` 什么都不发，只留审计；
* 承诺优先：payload 带 ``text`` 的唤醒永远原话发出（T-0031 回归点）；
* ``decide_action=false`` 时逐字节退回旧行为（不调 LLM、不写决策行）；
* 超时/异常/不可用/读不懂 → 回退模板，绝不静默吞消息；
* browse 没得刷时的降级（等于 silent）。
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
from fatfish.owner import OwnerRegistry
from fatfish.relationship import proactive_text_for_reason
from fatfish.runtime import Runtime
from fatfish.wakeup import build_prompt, parse_action

TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
OWNER_UIN = "10001"
OWNER_SESSION = f"qq:private:{OWNER_UIN}"
REASON = "早安：问主人哥哥今天怎么样"


class FakeLLM:
    """Answers the decision with one word (or explodes / stays unavailable)."""

    def __init__(
        self,
        text: str = "chat",
        *,
        error: Exception | None = None,
        available: bool = True,
        result: bool = True,
    ):
        self.text = text
        self.error = error
        self.available = available
        self.return_result = result
        self.calls: list[dict] = []

    def chat(
        self,
        messages,
        *,
        max_tokens=None,
        temperature=None,
        session_id=None,
        timeout=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "session_id": session_id,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        if not self.return_result:
            return None
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=12, completion_tokens=1),
            model="fake-model",
        )


class FakeBiliCollector:
    def __init__(self, cards: int = 2, error: Exception | None = None):
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


def make_env(tmp: Path, *, decide: str = "true", bilibili: str = "false", extra: dict | None = None):
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "wakeup_decide.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        "[qzone]",
        "enabled = false",
        f'owner_uin = "{OWNER_UIN}"',
        "[wakeup]",
        "max_per_day = 12",
        f"decide_action = {decide}",
        "decide_timeout_seconds = 3",
        "[activity]",
        "enabled = false",
        "[bilibili]",
        f"enabled = {bilibili}",
    ]
    for key, value in (extra or {}).items():
        lines.append(f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {value}")
    path = tmp / "wakeup_decide.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    runtime = Runtime(db, config)
    OwnerRegistry(db, config).bind(OWNER_SESSION, "主人哥哥")
    return config, db, runtime


class WakeupDecideTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_env(self, **kwargs):
        config, db, runtime = make_env(self.tmp, **kwargs)
        self.addCleanup(db.close)
        return config, db, runtime

    def schedule(self, runtime, *, reason: str = REASON, payload: dict | None = None, at: float = NOON):
        return runtime.wakeup.schedule(
            "+1m",
            intent="check_user",
            reason=reason,
            session_id=OWNER_SESSION,
            payload=payload,
            now=at,
        )

    def outbox_rows(self, db: Database) -> list[dict]:
        rows = db.query("SELECT kind, payload_json FROM outbox ORDER BY id")
        return [
            {"kind": row["kind"], "payload": json.loads(row["payload_json"] or "{}")}
            for row in rows
        ]

    def actions(self, db: Database) -> list[dict]:
        rows = db.query(
            "SELECT action, outcome, output_json FROM decision_log "
            "WHERE kind='wakeup_action' ORDER BY id"
        )
        return [
            {
                "action": row["action"],
                "outcome": row["outcome"],
                "input": json.loads(row["output_json"] or "{}"),
            }
            for row in rows
        ]

    def counts(self, db: Database, kind: str, action: str) -> int:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM decision_log WHERE kind=? AND action=?",
            (kind, action),
        )
        return int(row["n"]) if row else 0


class ChatActionTests(WakeupDecideTestBase):
    def test_chat_queues_one_proactive_message(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("chat")
        wakeup = self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["acted"])
        self.assertEqual(results[0]["action"], "chat")
        self.assertEqual(results[0]["outcome"], "queued")
        rows = self.outbox_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "proactive_message")
        # 决策走模型时，文案仍是老模板（决定的是"发不发"，不是"发什么"）
        self.assertEqual(rows[0]["payload"]["text"], proactive_text_for_reason(REASON))
        # 决策只花一次，且带上了紧预算
        self.assertEqual(len(runtime.llm.calls), 1)
        self.assertEqual(runtime.llm.calls[0]["timeout"], 3.0)
        self.assertEqual(runtime.llm.calls[0]["max_tokens"], 24)
        self.assertEqual(self.counts(db, "proactive_message", "sent"), 1)
        logged = self.actions(db)
        self.assertEqual([row["action"] for row in logged], ["chat"])
        self.assertEqual(logged[0]["input"]["why"], "model")
        self.assertEqual(logged[0]["input"]["model"], "fake-model")

    def test_decision_input_is_small_and_carries_the_signals(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("chat")
        db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at, token_count) "
            "VALUES (?,?,?,?,1)",
            (OWNER_SESSION, "user", "今天好累啊", NOON - 3600),
        )
        self.schedule(runtime)

        runtime.process_due_wakeups(now=NOON + 120)

        prompt = runtime.llm.calls[0]["messages"][-1]["content"]
        self.assertIn("距上次互动：1.0 小时前", prompt)
        self.assertIn("今天已经主动找过他 0 次", prompt)
        self.assertIn("今天好累啊", prompt)
        self.assertIn("B站：没开", prompt)
        # ~300 token 的量级：整段上下文 < 400 字
        self.assertLess(len(prompt), 800)

    def test_log_payload_has_the_documented_keys(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("chat")
        self.schedule(runtime)

        runtime.process_due_wakeups(now=NOON + 120)

        payload = self.actions(db)[0]["input"]
        for key in ("wakeup_id", "intent", "reason", "action", "why", "model"):
            self.assertIn(key, payload)


class BrowseActionTests(WakeupDecideTestBase):
    def test_browse_does_not_message_and_collects_once(self) -> None:
        config, db, runtime = self.make_env(bilibili="true")
        runtime.llm = FakeLLM("browse")
        runtime.bilibili = FakeBiliCollector(cards=2)
        self.schedule(runtime)

        first = runtime.process_due_wakeups(now=NOON + 120)
        self.assertFalse(first[0]["acted"])
        self.assertEqual(first[0]["action"], "browse")
        self.assertEqual(first[0]["outcome"], "browsed")
        self.assertEqual(runtime.bilibili.calls, [3])
        self.assertEqual(self.outbox_rows(db), [])
        self.assertEqual(self.counts(db, "proactive_message", "sent"), 0)
        self.assertEqual([row["action"] for row in self.actions(db)], ["browse"])

        # 同一个 wakeup 不会再触发第二次采集
        again = runtime.process_due_wakeups(now=NOON + 300)
        self.assertEqual(again, [])
        self.assertEqual(runtime.bilibili.calls, [3])

    def test_browse_does_not_consume_the_proactive_budget(self) -> None:
        config, db, runtime = self.make_env(bilibili="true")
        runtime.llm = FakeLLM("browse")
        runtime.bilibili = FakeBiliCollector()
        self.schedule(runtime)
        runtime.process_due_wakeups(now=NOON + 120)

        # 闸门看不到这次 browse：90 分钟池与每日额度都还是干净的
        runtime.llm = FakeLLM("chat")
        second = self.schedule(runtime, at=NOON + 130)
        ok, reason = runtime.gate.should_act(second, now=NOON + 200)
        self.assertTrue(ok, reason)
        results = runtime.process_due_wakeups(now=NOON + 200)
        self.assertTrue(results[0]["acted"])
        self.assertEqual(len(self.outbox_rows(db)), 1)

    def test_browse_degrades_to_silence_when_there_is_nothing_to_browse(self) -> None:
        # 采集层开着但刚刚刷过（间隔未到）：这轮没得刷 → 等于沉默，不发消息
        config, db, runtime = self.make_env(bilibili="true")
        runtime.llm = FakeLLM("browse")
        runtime.bilibili = FakeBiliCollector()
        db.set_meta("last_activity_bili_browse_at", str(NOON + 100))
        self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertEqual(results[0]["outcome"], "browse_skipped")
        self.assertEqual(runtime.bilibili.calls, [])
        self.assertEqual(self.outbox_rows(db), [])
        prompt = runtime.llm.calls[0]["messages"][-1]["content"]
        self.assertIn("不要选 browse", prompt)

    def test_browse_degrades_to_silence_when_bilibili_is_disabled(self) -> None:
        config, db, runtime = self.make_env(bilibili="false")
        runtime.llm = FakeLLM("browse")
        runtime.bilibili = FakeBiliCollector()
        self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertEqual(results[0]["outcome"], "browse_skipped")
        self.assertEqual(runtime.bilibili.calls, [])
        self.assertEqual(self.outbox_rows(db), [])

    def test_browse_failure_does_not_retry_or_message(self) -> None:
        config, db, runtime = self.make_env(bilibili="true")
        runtime.llm = FakeLLM("browse")
        runtime.bilibili = FakeBiliCollector(error=RuntimeError("network down"))
        self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertEqual(results[0]["outcome"], "browse_failed")
        self.assertEqual(len(runtime.bilibili.calls), 1)
        self.assertEqual(self.outbox_rows(db), [])
        self.assertEqual(runtime.process_due_wakeups(now=NOON + 400), [])


class SilentActionTests(WakeupDecideTestBase):
    def test_silent_sends_nothing_but_leaves_an_audit_row(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("silent")
        self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertFalse(results[0]["acted"])
        self.assertEqual(results[0]["action"], "silent")
        self.assertEqual(results[0]["outcome"], "silent")
        self.assertEqual(self.outbox_rows(db), [])
        self.assertEqual(self.counts(db, "proactive_message", "sent"), 0)
        logged = self.actions(db)
        self.assertEqual([row["action"] for row in logged], ["silent"])
        self.assertEqual(logged[0]["outcome"], "silent")


class PromiseWinsTests(WakeupDecideTestBase):
    def test_payload_text_beats_a_silent_decision(self) -> None:
        # T-0031：她亲口许下的"到点我喊你"不能被决策改成沉默
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("silent")
        self.schedule(runtime, reason="分享：晚安", payload={"text": "我到点喊你啦"})

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertTrue(results[0]["acted"])
        rows = self.outbox_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["text"], "我到点喊你啦")
        # 承诺走的是确定路径：不花决策调用
        self.assertEqual(runtime.llm.calls, [])
        logged = self.actions(db)
        self.assertEqual(logged[0]["action"], "chat")
        self.assertEqual(logged[0]["input"]["why"], "promise_text")

    def test_payload_text_beats_a_browse_decision(self) -> None:
        config, db, runtime = self.make_env(bilibili="true")
        runtime.llm = FakeLLM("browse")
        runtime.bilibili = FakeBiliCollector()
        self.schedule(runtime, payload={"text": "记得喝水"})

        runtime.process_due_wakeups(now=NOON + 120)

        self.assertEqual(runtime.bilibili.calls, [])
        self.assertEqual(self.outbox_rows(db)[0]["payload"]["text"], "记得喝水")


class ToggleTests(WakeupDecideTestBase):
    def test_decide_action_false_is_the_old_behaviour(self) -> None:
        config, db, runtime = self.make_env(decide="false")
        runtime.llm = FakeLLM("silent")  # 会被无视
        self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertTrue(results[0]["acted"])
        self.assertNotIn("action", results[0])
        self.assertEqual(runtime.llm.calls, [])
        rows = self.outbox_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["text"], proactive_text_for_reason(REASON))
        self.assertEqual(rows[0]["payload"]["reason"], REASON)
        # 旧路径没有决策行
        self.assertEqual(self.actions(db), [])
        self.assertEqual(self.counts(db, "proactive_message", "sent"), 1)

    def test_decide_action_false_keeps_the_promise_path(self) -> None:
        config, db, runtime = self.make_env(decide="false")
        runtime.llm = FakeLLM("silent")
        self.schedule(runtime, payload={"text": "我到点喊你啦"})

        runtime.process_due_wakeups(now=NOON + 120)

        self.assertEqual(runtime.llm.calls, [])
        self.assertEqual(self.outbox_rows(db)[0]["payload"]["text"], "我到点喊你啦")
        self.assertEqual(self.actions(db), [])


class FallbackTests(WakeupDecideTestBase):
    def _assert_fallback(self, runtime, db) -> None:
        rows = self.outbox_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["text"], proactive_text_for_reason(REASON))
        logged = self.actions(db)
        self.assertEqual(logged[0]["action"], "chat")

    def test_timeout_falls_back_to_the_template(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("silent", error=TimeoutError("timed out"))
        self.schedule(runtime)

        results = runtime.process_due_wakeups(now=NOON + 120)

        self.assertTrue(results[0]["acted"])
        self._assert_fallback(runtime, db)
        self.assertEqual(self.actions(db)[0]["input"]["why"], "error:TimeoutError")

    def test_exception_falls_back_to_the_template(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("silent", error=RuntimeError("boom"))
        self.schedule(runtime)

        runtime.process_due_wakeups(now=NOON + 120)

        self._assert_fallback(runtime, db)
        self.assertEqual(self.actions(db)[0]["input"]["why"], "error:RuntimeError")

    def test_no_response_falls_back_to_the_template(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM(result=False)  # llm.chat 超时返回 None
        self.schedule(runtime)

        runtime.process_due_wakeups(now=NOON + 120)

        self._assert_fallback(runtime, db)
        self.assertEqual(self.actions(db)[0]["input"]["why"], "no_response")

    def test_unreadable_reply_falls_back_to_the_template(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("嗯……我也不知道该说什么，算了先这样吧随便写点什么" * 3)
        self.schedule(runtime)

        runtime.process_due_wakeups(now=NOON + 120)

        self._assert_fallback(runtime, db)
        self.assertEqual(self.actions(db)[0]["input"]["why"], "unparseable")

    def test_llm_unavailable_falls_back_to_the_template(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("chat", available=False)
        self.schedule(runtime)

        runtime.process_due_wakeups(now=NOON + 120)

        self._assert_fallback(runtime, db)
        self.assertEqual(self.actions(db)[0]["input"]["why"], "llm_unavailable")


class ParseActionTests(unittest.TestCase):
    def test_accepts_the_three_words(self) -> None:
        self.assertEqual(parse_action("chat"), "chat")
        self.assertEqual(parse_action(" Browse\n"), "browse")
        self.assertEqual(parse_action("silent。"), "silent")

    def test_accepts_json_and_chinese(self) -> None:
        self.assertEqual(parse_action('{"action": "browse"}'), "browse")
        self.assertEqual(parse_action("沉默"), "silent")
        self.assertEqual(parse_action("去刷B站"), "browse")
        self.assertEqual(parse_action("想跟他说话"), "chat")

    def test_position_decides_when_both_appear(self) -> None:
        self.assertEqual(parse_action("不想说话，选 silent"), "silent")

    def test_rejects_empty_junk_and_rambling(self) -> None:
        self.assertIsNone(parse_action(""))
        self.assertIsNone(parse_action("？？？"))
        self.assertIsNone(parse_action("x" * 200))

    def test_prompt_holds_the_context(self) -> None:
        prompt = build_prompt("现在：14:00（非静默时段）")
        self.assertIn("chat", prompt)
        self.assertIn("browse", prompt)
        self.assertIn("silent", prompt)
        self.assertIn("现在：14:00", prompt)


class SelfContinueLaneUntouchedTests(WakeupDecideTestBase):
    def test_self_continue_never_reaches_the_decision(self) -> None:
        config, db, runtime = self.make_env()
        runtime.llm = FakeLLM("silent")
        runtime.wakeup.schedule(
            "+1m", intent="self_continue", session_id="web-default", now=NOON
        )

        results = runtime.process_due_wakeups(now=NOON + 120)

        # 独立通道的判定（web 会话不可投递）照旧，决策不介入、不花调用
        self.assertFalse(results[0]["acted"])
        self.assertEqual(runtime.llm.calls, [])
        self.assertEqual(self.actions(db), [])


if __name__ == "__main__":
    unittest.main()
