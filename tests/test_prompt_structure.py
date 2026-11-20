"""Prompt structure guards.

The gateway caches at *whole-message* granularity: a message only keeps its
cache slot while its content stays byte-identical. Appending a timestamp to
the same system message as the persona therefore voided the entire prefix and
drove the observed hit rate to 0%. These tests pin the ordering down so a
future edit cannot quietly reintroduce the regression.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.context import ContextAssembler
from fatfish.memory import MemoryService
from tests.test_owner import OWNER_SESSION, make_env

# Markers that can only appear when live turn data got injected. Section names
# such as "[最近对话]" are quoted inside the fact rules, so they are not usable
# as markers on their own.
VOLATILE_MARKERS = (
    "现在：",
    "能量 ",
    "当前活动 ",
    "距离上一次聊天",
    "今天已经聊了",
    "今日时间线",
    "短期记忆摘要",
    "当前对话者】",
)
TS_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}|\b\d{1,2}:\d{2}\b")


class PromptStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_invariant_section_is_message_zero_and_alone(self) -> None:
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        messages = bundle.model_messages()

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], self.assembler.stable_prefix())
        self.assertEqual(bundle.model_messages(), messages)

    def test_invariant_section_has_no_volatile_content(self) -> None:
        stable = self.assembler.stable_prefix()
        for marker in VOLATILE_MARKERS:
            self.assertNotIn(marker, stable, f"volatile marker leaked into prefix: {marker}")
        self.assertIsNone(TS_PATTERN.search(stable), "date/time leaked into the prefix")

    def test_volatile_sections_come_after_the_cacheable_ones(self) -> None:
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "user", "上一句", time.time()),
        )
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        messages = bundle.model_messages()
        roles = [m["role"] for m in messages]

        self.assertGreaterEqual(len(messages), 3, roles)
        joined_tail = "\n".join(m["content"] for m in messages[1:])
        # Everything volatile lives strictly after message #0.
        for marker in ("现在：", "当前对话者", "[用户消息]"):
            self.assertIn(marker, joined_tail, f"{marker} should sit after the prefix")

    def test_prefix_is_byte_identical_across_turns(self) -> None:
        first = self.assembler.build(OWNER_SESSION, "今天有点累", role="owner")
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "user", "今天有点累", time.time()),
        )
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "assistant", "先歇会儿吧", time.time()),
        )
        second = self.assembler.build(
            OWNER_SESSION, "那我躺一下", now=time.time() + 600, role="owner"
        )

        self.assertEqual(
            first.model_messages()[0]["content"], second.model_messages()[0]["content"]
        )
        self.assertEqual(first.stable_prefix_hash, second.stable_prefix_hash)
        # ...while the volatile tail really did move.
        self.assertNotEqual(
            first.model_messages()[-1]["content"], second.model_messages()[-1]["content"]
        )

    def test_no_duplicate_transcript_between_history_and_timeline(self) -> None:
        for i in range(3):
            self.db.execute(
                "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
                (OWNER_SESSION, "user", f"第{i}句", time.time() - 60 + i),
            )
            self.db.execute(
                "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
                (OWNER_SESSION, "assistant", f"回{i}", time.time() - 59 + i),
            )
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        history = self.assembler._history_text(OWNER_SESSION)
        dynamic = next(s.text for s in bundle.sections if s.name == "runtime_context")

        body: list[str] = []
        for line in history.splitlines()[1:]:
            if ": " not in line:
                continue
            payload = line.split(": ", 1)[-1].strip()
            # Assistant turns are rendered as the tool envelope; compare the
            # bubbles inside it, not the JSON wrapper, or the check passes
            # vacuously.
            if payload.startswith("{"):
                try:
                    body.extend(json.loads(payload).get("messages") or [])
                    continue
                except json.JSONDecodeError:
                    pass
            body.append(payload)
        self.assertTrue(body)
        duplicated = [ln for ln in body if ln in dynamic]
        self.assertEqual(
            duplicated,
            [],
            f"transcript injected twice (history + timeline): {duplicated[:2]}",
        )


    def test_history_goes_out_as_one_message_per_turn(self) -> None:
        """The transcript must not be collapsed into a single joined string.

        A joined block changes in its very first characters as soon as the
        window moves, and the gateway then discards the whole prefix.
        """
        now = time.time()
        for i in range(3):
            self.db.execute(
                "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
                (OWNER_SESSION, "user", f"问{i}", now + i),
            )
            self.db.execute(
                "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
                (OWNER_SESSION, "assistant", f"答{i}", now + i + 0.5),
            )
        messages = self.assembler.build(OWNER_SESSION, "在吗", role="owner").model_messages()

        self.assertEqual(messages[0]["role"], "system")
        transcript = messages[1:7]
        self.assertEqual(
            [m["role"] for m in transcript],
            ["user", "assistant"] * 3,
            "history must alternate user/assistant, one message per turn",
        )
        for message in transcript:
            self.assertNotIn("[最近对话]", message["content"])
            self.assertNotIn("\n", message["content"])

    def test_appending_a_turn_keeps_earlier_messages_identical(self) -> None:
        """Appending must only grow the list, never rewrite earlier entries."""
        now = time.time()
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "user", "第一问", now),
        )
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "assistant", "第一答", now + 0.5),
        )
        first = self.assembler.build(OWNER_SESSION, "在吗", now=now + 1, role="owner").model_messages()

        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "user", "第二问", now + 2),
        )
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "assistant", "第二答", now + 2.5),
        )
        second = self.assembler.build(OWNER_SESSION, "那我也躺一下", now=now + 3, role="owner").model_messages()

        self.assertGreater(len(second), len(first))
        self.assertEqual(first[:3], second[:3], "stable prefix + older turns must stay byte-identical")

    def test_history_window_evicts_in_blocks(self) -> None:
        """The window must not slide on every single turn."""
        from fatfish.context import HISTORY_LIMIT

        now = time.time()
        for i in range(HISTORY_LIMIT + 5):
            self.db.execute(
                "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
                (OWNER_SESSION, "user", f"t{i}", now + i),
            )
        before = self.assembler._history_turns(OWNER_SESSION)

        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at) VALUES (?,?,?,?)",
            (OWNER_SESSION, "user", "又一句", now + 999),
        )
        after = self.assembler._history_turns(OWNER_SESSION)

        self.assertEqual(
            before[0]["content"],
            after[0]["content"],
            "one extra turn must not move the window start",
        )
        self.assertEqual(len(after), len(before) + 1)

    def test_bounded_history_query_matches_full_scan(self) -> None:
        """The last-40 fetch must produce the same window as the old full scan."""
        from fatfish.context import HISTORY_BLOCK, HISTORY_LIMIT

        now = time.time()
        for total in (0, 1, 19, 20, 21, 25, 26, 39, 40, 41, 59, 60, 61, 99, 100):
            session = f"window:{total}"
            for i in range(total):
                self.db.execute(
                    "INSERT INTO conversation_turns"
                    "(session_id, role, content, created_at) VALUES (?,?,?,?)",
                    (session, "user", f"w{total}-{i}", now + i),
                )
            got = [
                turn["content"]
                for turn in self.assembler._history_turns(session)
            ]
            rows = self.db.query(
                "SELECT role, content, created_at FROM conversation_turns "
                "WHERE session_id=? AND ephemeral=0 ORDER BY id",
                (session,),
            )
            turns = [dict(row) for row in rows]
            excess = len(turns) - HISTORY_LIMIT
            if excess <= 0:
                expected = turns
            else:
                start = (excess // HISTORY_BLOCK) * HISTORY_BLOCK
                expected = turns[start:]
            self.assertEqual(
                [turn["content"] for turn in expected],
                got,
                f"history window drifted at total={total}",
            )


if __name__ == "__main__":
    unittest.main()
