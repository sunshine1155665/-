"""T-0042 L2: read-only self actions -- ``search_memory`` and ``read_qzone``.

Scope of this layer: the action runs, the result is stored verbatim in
``self_action_evidence``, and a failure never escapes. The desire gate and the
injection into the next turn belong to L4 and are deliberately absent here.

Offline only: fake memory / fake qzone, temp database, no network.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.config import load_config
from fatfish.context import TOOL_SCHEMA, ensure_default_persona
from fatfish.db import Database
from fatfish.runtime import ALLOWED_ACTIONS, Runtime, TurnAction, parse_turn_plan
from fatfish.security import PolicyEngine, TrustLevel
from fatfish.security.policy import ACTION_SPECS

NOW = 1_772_000_000.0
SESSION = "qq:private:1001"
SCHEMA_KEYS = ("search_memory", "read_qzone")


def make_runtime(tmp: Path) -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "self_action_read.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        # T-0042 L4 runs after this file: the desire gate now sits in front of
        # every self-action, so these L2 tests have to open it explicitly. The
        # threshold is pinned to 0 and the step is tiny, i.e. "always willing"
        # -- this file is about what the branches do, not about the gate.
        "[self_action]",
        "enabled = true",
        "initial_threshold = 0.0",
        "threshold_floor = 0.0",
        "threshold_step = 0.01",
        "daily_cap = 100",
        "max_events_per_turn = 10",
        "quiet_hours = []",
    ]
    path = tmp / "self_action_read.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


class FakeMemory:
    """Duck-typed ``MemoryService.search`` -- hits or a raise, nothing else."""

    def __init__(self, hits=None, *, error: Exception | None = None):
        self.hits = list(hits or [])
        self.error = error
        self.calls: list[dict] = []

    def search(self, query, limit=8, *, session_id=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "session_id": session_id})
        if self.error is not None:
            raise self.error
        return list(self.hits)


class FakeQzone:
    """Duck-typed collector: local rows only, fetch recorded not performed."""

    def __init__(
        self,
        items=None,
        *,
        enabled: bool = False,
        breaker: bool = False,
        fetch_error: Exception | None = None,
        recent_error: Exception | None = None,
    ):
        self.items = list(items or [])
        self.enabled = enabled
        self._breaker = breaker
        self.fetch_error = fetch_error
        self.recent_error = recent_error
        self.fetch_calls: list[dict] = []
        self.recent_limits: list[int] = []

    @property
    def breaker_open(self) -> bool:
        return self._breaker

    def fetch_and_ingest(self, client=None, *, manual=False, now=None):
        self.fetch_calls.append({"manual": manual, "now": now})
        if self.fetch_error is not None:
            raise self.fetch_error
        return {"ok": True, "new": 1}

    def recent_items(self, limit=10, *, include_friend=False, include_hidden=False):
        self.recent_limits.append(limit)
        if self.recent_error is not None:
            raise self.recent_error
        return self.items[:limit]


class FakeQzoneMethodBreaker(FakeQzone):
    """A fake exposing ``breaker_open`` as a method, like the task card wrote it."""

    def breaker_open(self) -> bool:  # type: ignore[override]
        return self._breaker


def make_hit(gist: str, *, type_: str = "episodic", when: float = NOW):
    item = SimpleNamespace(
        gist=gist,
        summary="",
        type=type_,
        event_time_start=when,
        observed_at=when,
    )
    return SimpleNamespace(item=item, score=1.0)


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.memory = FakeMemory()
        self.qzone = FakeQzone()
        self.runtime.memory = self.memory
        self.runtime.qzone = self.qzone

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def run_action(self, tool: str, args: dict, *, trust: TrustLevel = TrustLevel.PRIVATE):
        results = self.runtime.execute_actions(
            [TurnAction(tool, args)],
            session_id=SESSION,
            reply="",
            now=NOW,
            trust=trust,
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def evidence_rows(self):
        return self.db.query(
            "SELECT * FROM self_action_evidence ORDER BY id"
        )


# --------------------------------------------------------------------- policy
class PolicyMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyEngine()

    def test_private_allows_search_memory(self) -> None:
        decision = self.policy.validate(
            "search_memory", {"query": "海豚"}, trust=TrustLevel.PRIVATE
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_private_allows_read_qzone(self) -> None:
        decision = self.policy.validate("read_qzone", {"refresh": True}, trust=TrustLevel.PRIVATE)
        self.assertTrue(decision.allowed, decision.reason)

    def test_group_denied_for_both(self) -> None:
        for tool, args in (
            ("search_memory", {"query": "海豚"}),
            ("read_qzone", {"refresh": True}),
        ):
            with self.subTest(tool=tool):
                decision = self.policy.validate(tool, args, trust=TrustLevel.GROUP)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "group_not_allowed")

    def test_max_calls_enforced(self) -> None:
        for tool, args in (
            ("search_memory", {"query": "海豚"}),
            ("read_qzone", {}),
        ):
            with self.subTest(tool=tool):
                decision = self.policy.validate(
                    tool, args, trust=TrustLevel.PRIVATE, count=1
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "too_many_tool_calls")

    def test_missing_query_denied(self) -> None:
        for args in ({}, {"query": "   "}, {"query": None}):
            with self.subTest(args=args):
                decision = self.policy.validate(
                    "search_memory", args, trust=TrustLevel.PRIVATE
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "missing_query")

    def test_read_qzone_needs_no_text(self) -> None:
        decision = self.policy.validate("read_qzone", None, trust=TrustLevel.PRIVATE)
        self.assertTrue(decision.allowed, decision.reason)

    def test_specs_are_read_only_and_private(self) -> None:
        for tool in SCHEMA_KEYS:
            with self.subTest(tool=tool):
                spec = ACTION_SPECS[tool]
                self.assertFalse(spec.write, f"{tool} must not claim a write")
                self.assertFalse(spec.allow_group)
                self.assertEqual(spec.max_calls, 1)
                self.assertEqual(spec.min_trust, TrustLevel.PRIVATE)


class RegistrationTests(unittest.TestCase):
    def test_allowed_actions_contains_both(self) -> None:
        for tool in SCHEMA_KEYS:
            self.assertIn(tool, ALLOWED_ACTIONS)

    def test_tool_schema_documents_both(self) -> None:
        for tool in SCHEMA_KEYS:
            with self.subTest(tool=tool):
                self.assertIn(f"- {tool}:", TOOL_SCHEMA)
        self.assertIn('"desire"', TOOL_SCHEMA)
        self.assertIn("低于当前阈值时这个动作不会执行", TOOL_SCHEMA)

    def test_parse_turn_plan_keeps_the_new_actions(self) -> None:
        raw = json.dumps(
            {
                "messages": ["嗯"],
                "actions": [
                    {"tool": "search_memory", "args": {"query": "海豚", "limit": 2}},
                    {"tool": "read_qzone", "args": {"refresh": False}},
                ],
            },
            ensure_ascii=False,
        )
        plan = parse_turn_plan(raw)
        self.assertEqual([a.tool for a in plan.actions], list(SCHEMA_KEYS))


# ------------------------------------------------------------- search_memory
class SearchMemoryTests(RuntimeCase):
    def test_hits_are_stored_as_evidence(self) -> None:
        self.memory.hits = [
            make_hit("主人哥哥喜欢海豚", type_="preference"),
            make_hit("上次去海洋馆是十月"),
        ]
        result = self.run_action("search_memory", {"query": "海豚", "limit": 4})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["hits"], 2)
        self.assertEqual(self.memory.calls, [
            {"query": "海豚", "limit": 4, "session_id": SESSION}
        ])

        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], result["result"]["evidence_id"])
        self.assertEqual(row["session_id"], SESSION)
        self.assertEqual(row["action"], "search_memory")
        self.assertEqual(row["source"], "memory")
        self.assertEqual(row["title"], "海豚")
        self.assertEqual(row["created_at"], NOW)
        self.assertIsNone(row["consumed_at"])
        # Raw text: L4 wraps once at injection time. Escaping here would double-wrap.
        self.assertIn("主人哥哥喜欢海豚", row["content"])
        self.assertIn("preference", row["content"])
        self.assertNotIn("<untrusted_content", row["content"])

    def test_limit_is_clamped_to_four(self) -> None:
        self.memory.hits = [make_hit(f"记忆{i}") for i in range(6)]
        result = self.run_action("search_memory", {"query": "记忆", "limit": 99})
        self.assertTrue(result["ok"])
        self.assertEqual(self.memory.calls[0]["limit"], 4)
        self.assertEqual(result["result"]["hits"], 4)
        self.assertEqual(self.evidence_rows()[0]["content"].count("- "), 4)

    def test_zero_limit_falls_back_to_default(self) -> None:
        # ``int(args.get("limit") or 4)``: 0 is falsy, so it reads as "not given".
        self.memory.hits = [make_hit("记忆0"), make_hit("记忆1")]
        self.run_action("search_memory", {"query": "记忆", "limit": 0})
        self.assertEqual(self.memory.calls[0]["limit"], 4)

    def test_bad_limit_falls_back_to_default(self) -> None:
        self.run_action("search_memory", {"query": "记忆", "limit": "很多"})
        self.assertEqual(self.memory.calls[0]["limit"], 4)

    def test_no_hits_is_success(self) -> None:
        self.memory.hits = []
        result = self.run_action("search_memory", {"query": "鲸落"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["hits"], 0)
        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "（没有查到相关记忆）")

    def test_missing_query_is_a_refusal_not_an_exception(self) -> None:
        result = self.run_action("search_memory", {"query": "  "})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_query")
        self.assertEqual(self.evidence_rows(), [])
        self.assertEqual(self.memory.calls, [])

    def test_search_failure_does_not_escape(self) -> None:
        self.memory.error = RuntimeError("embedder offline")
        result = self.run_action("search_memory", {"query": "海豚"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "action_failed:RuntimeError")
        self.assertEqual(self.evidence_rows(), [])

    def test_group_session_is_refused_before_running(self) -> None:
        result = self.run_action(
            "search_memory", {"query": "海豚"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_not_allowed")
        self.assertEqual(self.memory.calls, [])
        self.assertEqual(self.evidence_rows(), [])

    def test_content_is_truncated_to_4000_chars(self) -> None:
        self.memory.hits = [make_hit("鲸" * 5000)]
        result = self.run_action("search_memory", {"query": "鲸"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.evidence_rows()[0]["content"]), 4000)

    def test_timestamp_falls_back_when_missing(self) -> None:
        hit = make_hit("没有时间的记忆")
        hit.item.event_time_start = None
        hit.item.observed_at = None
        self.memory.hits = [hit]
        result = self.run_action("search_memory", {"query": "记忆"})
        self.assertTrue(result["ok"])
        self.assertIn("—", self.evidence_rows()[0]["content"])


# ---------------------------------------------------------------- read_qzone
class ReadQzoneTests(RuntimeCase):
    def test_disabled_never_fetches(self) -> None:
        self.qzone = FakeQzone(
            [{"author_scope": "self", "author_name": "大肥鱼", "content": "今天下雨", "published_at": NOW}],
            enabled=False,
        )
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {"refresh": True})

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qzone.fetch_calls, [])
        self.assertEqual(result["result"]["refresh_skipped"], "disabled")
        self.assertEqual(result["result"]["items"], 1)
        self.assertEqual(self.qzone.recent_limits, [5])

        row = self.evidence_rows()[0]
        self.assertEqual(row["action"], "read_qzone")
        self.assertEqual(row["source"], "qzone")
        self.assertEqual(row["title"], "空间动态")
        self.assertIn("大肥鱼: 今天下雨", row["content"])
        self.assertNotIn("<untrusted_content", row["content"])

    def test_open_breaker_never_fetches(self) -> None:
        self.qzone = FakeQzone(enabled=True, breaker=True)
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {"refresh": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qzone.fetch_calls, [])
        self.assertEqual(result["result"]["refresh_skipped"], "breaker_open")

    def test_breaker_exposed_as_method_still_skips(self) -> None:
        self.qzone = FakeQzoneMethodBreaker(enabled=True, breaker=True)
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {"refresh": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qzone.fetch_calls, [])
        self.assertEqual(result["result"]["refresh_skipped"], "breaker_open")

    def test_refresh_runs_one_manual_fetch(self) -> None:
        self.qzone = FakeQzone(enabled=True)
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {"refresh": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qzone.fetch_calls, [{"manual": True, "now": NOW}])
        self.assertNotIn("refresh_error", result["result"])

    def test_no_refresh_means_local_only(self) -> None:
        self.qzone = FakeQzone(enabled=True)
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {"refresh": False})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.qzone.fetch_calls, [])

    def test_fetch_failure_degrades_to_local_rows(self) -> None:
        self.qzone = FakeQzone(
            [
                {"author_scope": "self", "author_name": "大肥鱼", "content": "旧动态", "seen_at": NOW},
            ],
            enabled=True,
            fetch_error=TimeoutError("qzone timeout"),
        )
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {"refresh": True})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["refresh_error"], "TimeoutError: qzone timeout")
        self.assertEqual(result["result"]["items"], 1)
        self.assertIn("旧动态", self.evidence_rows()[0]["content"])

    def test_empty_feed_is_success(self) -> None:
        result = self.run_action("read_qzone", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["items"], 0)
        self.assertEqual(self.evidence_rows()[0]["content"], "（空间里暂时没有新动态）")

    def test_local_read_failure_does_not_escape(self) -> None:
        self.qzone = FakeQzone(recent_error=RuntimeError("no such table"))
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "action_failed:RuntimeError")

    def test_group_session_is_refused(self) -> None:
        result = self.run_action("read_qzone", {"refresh": True}, trust=TrustLevel.GROUP)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_not_allowed")
        self.assertEqual(self.qzone.fetch_calls, [])
        self.assertEqual(self.evidence_rows(), [])

    def test_items_are_capped_at_five(self) -> None:
        self.qzone = FakeQzone(
            [
                {"author_scope": "self", "author_name": "大肥鱼", "content": f"动态{i}", "published_at": NOW}
                for i in range(8)
            ]
        )
        self.runtime.qzone = self.qzone
        result = self.run_action("read_qzone", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["items"], 5)

    def test_friend_rows_are_labelled_not_leaked_as_self(self) -> None:
        self.qzone = FakeQzone(
            [{"author_scope": "friend", "author_name": "", "content": "好友动态", "published_at": NOW}]
        )
        self.runtime.qzone = self.qzone
        self.run_action("read_qzone", {})
        self.assertIn("QQ好友: 好友动态", self.evidence_rows()[0]["content"])


if __name__ == "__main__":
    unittest.main()
