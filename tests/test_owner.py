from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from fatfish import cli
from fatfish.clock import parse_when
from fatfish.config import load_config
from fatfish.context import ContextAssembler, ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.memory import MemoryService
from fatfish.owner import OwnerRegistry
from fatfish.relationship import RelationshipRituals
from fatfish.runtime import Runtime
from fatfish.voice import VoiceDecision
from fatfish.wakeup import WakeupScheduler

OWNER_SESSION = "qq:private:10001"
OTHER_SESSION = "qq:private:20002"
CLAIM_CODE = "fish-1234"

RESULT_KEYS = {
    "reply",
    "messages",
    "actions",
    "voice",
    "tts",
    "sticker",
    "context",
    "llm",
    "security",
}


def make_env(tmp: Path) -> tuple[object, Database, Path]:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'owner.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[relationship]
enabled = true
session_id = "default"
morning_greeting = "08:30"
night_checkin = "22:30"
neglect_days = 3
owner_claim_code = "{CLAIM_CODE}"
owner_display_name = "主人哥哥"

[voice]
enabled = false

[tts]
enabled = false

[sticker]
enabled = false

[wakeup]
max_per_day = 10
max_chain_depth = 3
"""
    path = tmp / "owner.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, path


def fake_runtime(runtime: Runtime) -> list[dict]:
    """Wire a deterministic LLM and voice policy; return the call log."""
    calls: list[dict] = []

    def chat(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        return LLMResult(
            text='{"messages":["普通回复"],"actions":[]}',
            usage=LLMUsage(prompt_tokens=10, completion_tokens=3, cache_hit_tokens=2),
            model="fake-model",
        )

    runtime.llm.api_key = "fake"
    runtime.llm.chat = chat
    runtime.voice.decide = lambda *args, **kwargs: VoiceDecision(
        "text_only", 0.1, ["test"]
    )
    return calls


class OwnerRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_bind_replace_unbind_and_status(self) -> None:
        owner = OwnerRegistry(self.db, self.config)
        self.assertFalse(owner.status()["bound"])
        self.assertIsNone(owner.get())
        self.assertTrue(owner.status()["claim_code_configured"])

        binding = owner.bind(OWNER_SESSION, display_name="阿主", source="cli")
        self.assertEqual(binding["session_id"], OWNER_SESSION)
        self.assertEqual(binding["platform"], "qq")
        self.assertEqual(binding["display_name"], "阿主")
        self.assertEqual(binding["source"], "cli")
        self.assertEqual(binding["role"], "owner")
        self.assertGreater(binding["bound_at"], 0)
        self.assertTrue(owner.is_owner(OWNER_SESSION))
        self.assertFalse(owner.is_owner(OTHER_SESSION))
        self.assertTrue(owner.status()["bound"])

        # Single-owner app: a second bind replaces the first one.
        owner.bind(OTHER_SESSION, display_name="阿主", source="cli")
        self.assertFalse(owner.is_owner(OWNER_SESSION))
        self.assertTrue(owner.is_owner(OTHER_SESSION))
        rows = self.db.query("SELECT * FROM owner_bindings")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "owner")

        self.assertTrue(owner.unbind())
        self.assertFalse(owner.unbind())
        self.assertFalse(owner.is_owner(OTHER_SESSION))

    def test_bind_rejects_empty_and_group_sessions(self) -> None:
        owner = OwnerRegistry(self.db, self.config)
        with self.assertRaises(ValueError):
            owner.bind("  ")
        with self.assertRaises(ValueError):
            owner.bind("qq:group:12345")
        with self.assertRaises(ValueError):
            owner.bind("group:12345")
        self.assertFalse(owner.status()["bound"])

    def test_default_display_name_and_persistence(self) -> None:
        owner = OwnerRegistry(self.db, self.config)
        binding = owner.bind(OWNER_SESSION, source="cli")
        self.assertEqual(binding["display_name"], "主人哥哥")

        reopened = OwnerRegistry(self.db, self.config)
        self.assertTrue(reopened.is_owner(OWNER_SESSION))
        self.assertEqual(reopened.get()["display_name"], "主人哥哥")

    def test_status_never_exposes_claim_code(self) -> None:
        owner = OwnerRegistry(self.db, self.config)
        payload = json.dumps(owner.status(), ensure_ascii=False)
        self.assertNotIn(CLAIM_CODE, payload)
        self.assertTrue(owner.status()["claim_code_configured"])


class OwnerClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.config_path = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_claim_success_is_deterministic_and_skips_llm(self) -> None:
        runtime = Runtime(self.db, self.config)
        calls = fake_runtime(runtime)

        result = runtime.handle_user_message(OWNER_SESSION, f"/认主 {CLAIM_CODE}")

        self.assertEqual(calls, [])
        self.assertEqual(set(result.keys()), RESULT_KEYS)
        self.assertIn("认主成功", result["reply"])
        self.assertEqual(result["messages"], [result["reply"]])
        self.assertEqual(result["actions"], [])
        self.assertIsNone(result["tts"])
        self.assertIsNone(result["sticker"])
        self.assertEqual(result["context"], {})
        self.assertFalse(result["security"]["blocked"])
        self.assertEqual(result["security"]["trust"], "private")
        self.assertTrue(result["security"]["trace_id"])

        binding = runtime.owner.get()
        self.assertIsNotNone(binding)
        self.assertEqual(binding["session_id"], OWNER_SESSION)
        self.assertEqual(binding["source"], "claim")
        self.assertTrue(runtime.owner.is_owner(OWNER_SESSION))

        decision = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='owner_bind' ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "bind")
        self.assertEqual(decision["outcome"], "ok")
        self.assertNotIn(CLAIM_CODE, decision["output_json"])

        trace = self.db.query_one("SELECT * FROM turn_traces ORDER BY at DESC LIMIT 1")
        self.assertIsNotNone(trace)
        self.assertEqual(trace["model"], "owner")
        self.assertEqual(trace["scope"], "private")
        self.assertIn('"owner_bound": true', trace["result_json"])
        self.assertNotIn(CLAIM_CODE, json.dumps(dict(trace), ensure_ascii=False))

        # The claim message (and therefore the code) is not stored in history.
        turns = self.db.query_one("SELECT COUNT(*) AS n FROM conversation_turns")
        self.assertEqual(int(turns["n"]), 0)

    def test_plain_prefix_claim_and_persistence_across_runtime(self) -> None:
        runtime = Runtime(self.db, self.config)
        fake_runtime(runtime)
        result = runtime.handle_user_message(OWNER_SESSION, f"认主 {CLAIM_CODE}")
        self.assertIn("认主成功", result["reply"])

        reopened = Runtime(self.db, self.config)
        self.assertTrue(reopened.owner.is_owner(OWNER_SESSION))
        bundle = reopened.context.build(OWNER_SESSION, "你好")
        self.assertIn("已认主的主人哥哥", bundle.text())
        self.assertIn(OWNER_SESSION, bundle.text())

    def test_wrong_code_logs_failure_and_flows_to_normal_chat(self) -> None:
        runtime = Runtime(self.db, self.config)
        calls = fake_runtime(runtime)

        result = runtime.handle_user_message(OWNER_SESSION, "认主 not-the-code")

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["reply"], "普通回复")
        self.assertIsNone(runtime.owner.get())
        decision = self.db.query_one(
            "SELECT * FROM decision_log WHERE kind='owner_claim_failed' ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "reject")
        self.assertEqual(decision["outcome"], "denied")
        self.assertNotIn("not-the-code", decision["output_json"])
        bound = self.db.query_one("SELECT COUNT(*) AS n FROM owner_bindings")
        self.assertEqual(int(bound["n"]), 0)

    def test_group_and_already_bound_sessions_ignore_claim(self) -> None:
        runtime = Runtime(self.db, self.config)
        calls = fake_runtime(runtime)

        runtime.handle_user_message(
            "qq:group:55555", f"/认主 {CLAIM_CODE}", is_group=True
        )
        self.assertIsNone(runtime.owner.get())
        self.assertEqual(len(calls), 1)

        runtime.owner.bind(OWNER_SESSION, source="cli")
        runtime.handle_user_message(OTHER_SESSION, f"/认主 {CLAIM_CODE}")
        self.assertEqual(runtime.owner.get()["session_id"], OWNER_SESSION)
        self.assertEqual(len(calls), 2)
        bound = self.db.query_one(
            "SELECT COUNT(*) AS n FROM decision_log WHERE kind='owner_bind'"
        )
        self.assertEqual(int(bound["n"]), 0)

    def test_claim_is_disabled_when_code_is_empty(self) -> None:
        runtime = Runtime(self.db, self.config)
        runtime.owner.claim_code = ""
        calls = fake_runtime(runtime)

        runtime.handle_user_message(OWNER_SESSION, f"/认主 {CLAIM_CODE}")

        self.assertEqual(len(calls), 1)
        self.assertIsNone(runtime.owner.get())
        failed = self.db.query_one(
            "SELECT COUNT(*) AS n FROM decision_log WHERE kind='owner_claim_failed'"
        )
        self.assertEqual(int(failed["n"]), 0)


class OwnerContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _assembler(self) -> ContextAssembler:
        return ContextAssembler(self.db, self.config, MemoryService(self.db, self.config))

    def test_owner_non_owner_and_group_hints_are_dynamic_only(self) -> None:
        assembler = self._assembler()
        owner_bundle = assembler.build(OWNER_SESSION, "你好", is_owner=True)
        other_bundle = assembler.build(OTHER_SESSION, "你好", is_owner=False)
        group_bundle = assembler.build("qq:group:77777", "你好", is_owner=True)

        self.assertIn("已认主的主人哥哥", owner_bundle.text())
        self.assertIn(OWNER_SESSION, owner_bundle.text())
        self.assertIn("不是主人", other_bundle.text())
        self.assertIn("不要透露主人的私事", other_bundle.text())
        self.assertIn("主人在群里", group_bundle.text())
        self.assertIn("照样称呼主人", group_bundle.text())
        self.assertNotIn("不要使用主人称呼", group_bundle.text())
        self.assertNotIn("不是私聊主人", owner_bundle.text())

        for bundle in (owner_bundle, other_bundle, group_bundle):
            self.assertNotIn("【当前对话者】", bundle.sections[0].text)
            # Look the section up by name: a new block was inserted between the
            # prefix and the transcript (past timeline), so positions move.
            dynamic = {s.name: s.text for s in bundle.sections}["runtime_context"]
            self.assertIn("【当前对话者】", dynamic)

        # The stable prefix (and therefore prompt caching) is untouched.
        self.assertEqual(
            owner_bundle.stable_prefix_hash, other_bundle.stable_prefix_hash
        )
        self.assertEqual(
            owner_bundle.stable_prefix_hash, group_bundle.stable_prefix_hash
        )
        self.assertNotEqual(owner_bundle.dynamic_hash, other_bundle.dynamic_hash)

    def test_none_flag_auto_resolves_and_missing_table_is_safe(self) -> None:
        assembler = self._assembler()
        # No owner_bindings table exists yet; this must not raise.
        self.assertIn("不是主人", assembler.build(OTHER_SESSION, "你好").text())

        owner = OwnerRegistry(self.db, self.config)
        owner.bind(OWNER_SESSION, source="cli")
        self.assertIn(
            "已认主的主人哥哥", assembler.build(OWNER_SESSION, "你好").text()
        )
        self.assertIn("不是主人", assembler.build(OTHER_SESSION, "你好").text())

    def test_runtime_passes_owner_flag_into_prompt(self) -> None:
        runtime = Runtime(self.db, self.config)
        calls = fake_runtime(runtime)
        runtime.owner.bind(OWNER_SESSION, source="cli")

        # The role hint lives in the runtime-context message, not in the
        # invariant prefix -- the prefix must stay byte-identical to cache.
        def system_text(turn: int) -> str:
            return "\n".join(
                m["content"] for m in calls[turn]["messages"] if m["role"] == "system"
            )

        runtime.handle_user_message(OWNER_SESSION, "你好")
        self.assertIn("已认主的主人哥哥", system_text(0))

        runtime.handle_user_message(OTHER_SESSION, "你好")
        self.assertIn("不是主人", system_text(1))


class OwnerRitualTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_rituals_target_owner_session_then_fallback(self) -> None:
        owner = OwnerRegistry(self.db, self.config)
        wake = WakeupScheduler(self.db, self.config)
        rituals = RelationshipRituals(self.db, self.config, wake, owner=owner)

        self.assertEqual(rituals.target_session_id, "default")
        owner.bind(OWNER_SESSION, source="cli")
        self.assertEqual(rituals.target_session_id, OWNER_SESSION)

        now = parse_when("2026-10-20 09:00", self.config.timezone_name)
        scheduled = rituals.tick(now=now)
        self.assertTrue(scheduled)
        pending = wake.list("pending")
        self.assertTrue(pending)
        self.assertTrue(all(item.session_id == OWNER_SESSION for item in pending))
        self.assertTrue(any("早安" in item.reason for item in pending))

        owner.unbind()
        self.assertEqual(rituals.target_session_id, "default")


class OwnerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.config_path = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--config", str(self.config_path), *argv])
        return code, json.loads(buffer.getvalue())

    def test_cli_bind_status_unbind(self) -> None:
        code, bound = self._run(
            "owner", "bind", "--session", OWNER_SESSION, "--name", "阿主"
        )
        self.assertEqual(code, 0)
        self.assertTrue(bound["ok"])
        self.assertEqual(bound["binding"]["session_id"], OWNER_SESSION)
        self.assertEqual(bound["binding"]["display_name"], "阿主")

        code, status = self._run("owner", "status")
        self.assertEqual(code, 0)
        self.assertTrue(status["ok"])
        self.assertTrue(status["bound"])
        self.assertEqual(status["owner"]["session_id"], OWNER_SESSION)
        self.assertNotIn(CLAIM_CODE, json.dumps(status, ensure_ascii=False))

        code, removed = self._run("owner", "unbind")
        self.assertEqual(code, 0)
        self.assertTrue(removed["removed"])
        self.assertFalse(removed["status"]["bound"])

    def test_cli_bind_rejects_group_session(self) -> None:
        code, payload = self._run(
            "owner", "bind", "--session", "qq:group:12345"
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("private", payload["error"])
