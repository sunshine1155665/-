"""Platform prompt variants (2026-11-15).

Pins three things:

* the full owner prefix is byte-identical to the committed golden fixture, so a
  future edit cannot silently move the gateway's cache key;
* each platform variant contains only the tool descriptions it can execute
  (desktop = full minus ``send_voice`` plus desktop rules; QQ group = chat +
  stickers; roam = full plus roam rules);
* the runtime drops capabilities the process cannot serve -- notably QQ history
  reads on the official Tencent bot adapter.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.context import ContextAssembler
from fatfish.memory import MemoryService
from fatfish.prompt_blocks import ACTION_BLOCKS, ACTION_ORDER, TOOL_SCHEMA, build_tool_schema
from fatfish.prompt_variants import (
    FULL_CAPABILITIES,
    actions_for_capabilities,
    capabilities_for,
    capability_signature,
    filter_capabilities,
    platform_from_session,
    variant_for_platform,
)
from fatfish.runtime import Runtime, TurnAction
from tests.test_owner import OWNER_SESSION, fake_runtime, make_env

GOLDEN = Path(__file__).parent / "fixtures" / "stable_prefix_full.txt"


class _AssemblerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.assembler = ContextAssembler(
            self.db, self.config, MemoryService(self.db, self.config)
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


class GoldenPrefixTests(_AssemblerCase):
    def test_full_prefix_matches_golden_fixture(self) -> None:
        self.assertEqual(
            self.assembler.stable_prefix(),
            GOLDEN.read_text(encoding="utf-8"),
        )

    def test_explicit_full_capabilities_is_the_default(self) -> None:
        self.assertEqual(
            self.assembler.stable_prefix(FULL_CAPABILITIES),
            self.assembler.stable_prefix(),
        )

    def test_tool_blocks_reassemble_into_the_public_schema(self) -> None:
        self.assertEqual(build_tool_schema(None), TOOL_SCHEMA)
        for name in ACTION_ORDER:
            self.assertIn(ACTION_BLOCKS[name], TOOL_SCHEMA)

    def test_build_records_the_variant(self) -> None:
        bundle = self.assembler.build(OWNER_SESSION, "在吗", role="owner")
        self.assertEqual(bundle.variant, "full")
        group = self.assembler.build(
            "qq:group:10001", "在吗", role="owner", platform="qq_group"
        )
        self.assertEqual(group.variant, "qq_group")
        self.assertNotEqual(group.stable_prefix_hash, bundle.stable_prefix_hash)


class VariantContentTests(_AssemblerCase):
    def _prefix(self, variant: str) -> str:
        return self.assembler.stable_prefix(capabilities_for(variant))

    def test_group_variant_is_minimal(self) -> None:
        text = self._prefix("qq_group")
        self.assertIn("【群聊】", text)
        self.assertIn("- send_sticker:", text)
        for forbidden in (
            "- send_voice:",
            "- post_qzone:",
            "- read_chat:",
            "- talk_to:",
            "- make_photo:",
            "- sing:",
            "- roam_visit:",
            "- remember:",
            "- share_with_owner:",
        ):
            self.assertNotIn(forbidden, text)

    def test_desktop_variant_keeps_the_partner_abilities(self) -> None:
        text = self._prefix("desktop")
        self.assertIn("【桌面】", text)
        for required in (
            "- post_qzone:",
            "- share_with_owner:",
            "- share_to_friend:",
            "- read_qzone:",
            "- read_chat:",
            "- make_photo:",
            "- sing:",
        ):
            self.assertIn(required, text)
        # The shell speaks every bubble itself; a second voice action would be
        # an invisible channel (2026-11-15 boss decision).
        self.assertNotIn("- send_voice:", text)

    def test_roam_variant_is_full_plus_rules(self) -> None:
        text = self._prefix("roam_room")
        self.assertIn("【漫游】", text)
        for required in ("- sing:", "- roam_visit:", "- send_voice:", "- post_qzone:"):
            self.assertIn(required, text)

    def test_group_prefix_is_much_cheaper(self) -> None:
        full = self._prefix("full")
        group = self._prefix("qq_group")
        self.assertLess(len(group), len(full) * 0.6)

    def test_prefixes_are_stable_per_variant(self) -> None:
        for variant in ("full", "desktop", "qq_group", "roam_room"):
            first = self._prefix(variant)
            second = self._prefix(variant)
            self.assertEqual(first, second, variant)
        self.assertNotEqual(self._prefix("full"), self._prefix("roam_room"))
        self.assertNotEqual(self._prefix("full"), self._prefix("desktop"))


class CapabilityMappingTests(unittest.TestCase):
    def test_platform_to_variant(self) -> None:
        self.assertEqual(variant_for_platform("qq_private"), "full")
        self.assertEqual(variant_for_platform("desktop"), "desktop")
        self.assertEqual(variant_for_platform("qq_group"), "qq_group")
        self.assertEqual(variant_for_platform("roam_room"), "roam_room")
        self.assertEqual(variant_for_platform(None), "full")
        self.assertEqual(variant_for_platform("nonsense"), "full")

    def test_session_inference(self) -> None:
        self.assertEqual(platform_from_session("desktop:owner"), "desktop")
        self.assertEqual(platform_from_session("qq:group:123"), "qq_group")
        self.assertEqual(platform_from_session("qq:private:123"), "full")
        self.assertEqual(platform_from_session("activity-20261115-1200"), "full")

    def test_actions_follow_capabilities(self) -> None:
        group = actions_for_capabilities(capabilities_for("qq_group"))
        self.assertEqual(group, frozenset({"send_sticker"}))
        desktop = actions_for_capabilities(capabilities_for("desktop"))
        self.assertIn("post_qzone", desktop)
        self.assertNotIn("send_voice", desktop)
        self.assertEqual(
            actions_for_capabilities(capabilities_for("full")),
            frozenset(ACTION_ORDER),
        )

    def test_runtime_filters_unavailable_capabilities(self) -> None:
        full = capabilities_for("full")
        with_history = filter_capabilities(full)
        self.assertIn("qq.read", with_history)
        no_history = filter_capabilities(full, qq_history=False)
        self.assertNotIn("qq.read", no_history)
        self.assertNotIn("qq.talk", no_history)
        no_photo = filter_capabilities(full, photo=False)
        self.assertNotIn("photo", no_photo)
        self.assertNotIn("make_photo", actions_for_capabilities(no_photo))

    def test_signature_distinguishes_variants(self) -> None:
        signatures = {
            capability_signature(capabilities_for(name))
            for name in ("full", "desktop", "qq_group", "roam_room")
        }
        self.assertEqual(len(signatures), 4)


class RuntimeVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.calls = fake_runtime(self.runtime)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _first_prompt(self) -> str:
        return str(self.calls[0]["messages"][0]["content"])

    def test_owner_private_turn_is_full_and_drops_unavailable_history_tools(self) -> None:
        result = self.runtime.handle_user_message(OWNER_SESSION, "在吗")
        self.assertEqual(result["context"]["variant"], "full")
        prompt = self._first_prompt()
        # No QQ adapter is wired in this process, so the official-bot reality
        # applies: history reads must not be promised.
        self.assertNotIn("- read_chat:", prompt)
        self.assertNotIn("- talk_to:", prompt)
        self.assertIn("- send_voice:", prompt)

    def test_history_capability_returns_when_adapter_supports_it(self) -> None:
        def _fake(*_args: object, **_kwargs: object) -> list[dict]:
            return []

        self.runtime.qq = SimpleNamespace(
            fetch_friend_history=_fake,
            fetch_group_history=_fake,
            fetch_forward_messages=_fake,
            fetch_group_members=_fake,
        )
        self.runtime.handle_user_message(OWNER_SESSION, "在吗")
        prompt = self._first_prompt()
        self.assertIn("- read_chat:", prompt)
        self.assertIn("- talk_to:", prompt)

    def test_group_turn_uses_the_minimal_prompt(self) -> None:
        result = self.runtime.handle_user_message(
            "qq:group:10001", "在吗", is_group=True, platform="qq_group"
        )
        self.assertEqual(result["context"]["variant"], "qq_group")
        prompt = self._first_prompt()
        self.assertIn("【群聊】", prompt)
        self.assertNotIn("- post_qzone:", prompt)
        self.assertNotIn("- send_voice:", prompt)

    def test_desktop_turn_uses_the_desktop_prompt(self) -> None:
        result = self.runtime.handle_user_message(
            "desktop:owner", "在吗", platform="desktop"
        )
        self.assertEqual(result["context"]["variant"], "desktop")
        prompt = self._first_prompt()
        self.assertIn("【桌面】", prompt)
        self.assertNotIn("- send_voice:", prompt)
        self.assertIn("- post_qzone:", prompt)

    def test_group_capability_gate_refuses_owner_only_actions(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("post_qzone", {"content": "发一条说说"})],
            session_id="qq:group:10001",
            reply="好呀",
            allowed_tools=actions_for_capabilities(capabilities_for("qq_group")),
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "unsupported_on_platform")

    def test_desktop_capability_gate_refuses_voice_files(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("send_voice", {"text": "你好"})],
            session_id="desktop:owner",
            reply="你好",
            allowed_tools=actions_for_capabilities(capabilities_for("desktop")),
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "unsupported_on_platform")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
