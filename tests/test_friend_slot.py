"""The single friend slot (好友位) and memory attribution.

Three invariants are covered here:

* the friend slot never weakens owner semantics -- ``is_owner()`` stays false
  for the friend and a friend row never blocks 认主;
* every memory written through the runtime carries *who said it* and *who it is
  about*, so a two-person memory bank cannot silently merge;
* when neither party is the bound speaker (a stranger, a group), the memory
  digests are not injected at all -- showing the right label does not make
  someone else's memory safe to disclose.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from fatfish import cli
from fatfish.context import ContextAssembler
from fatfish.memory import MemoryService, attribution_line
from fatfish.owner import OwnerRegistry
from fatfish.privacy import SecretNotes
from fatfish.runtime import Runtime, TurnAction
from fatfish.security import TrustLevel

from tests.test_owner import make_env

OWNER_SESSION = "qq:private:10001"
FRIEND_SESSION = "qq:private:9999000002"
STRANGER_SESSION = "qq:private:99999"
GROUP_SESSION = "qq:group:77777"


class FriendSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.owner = OwnerRegistry(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_owner_and_friend_coexist_without_evicting(self) -> None:
        self.owner.bind(OWNER_SESSION, source="cli")
        self.owner.bind_friend(FRIEND_SESSION, display_name="姐姐", source="cli")
        self.assertEqual(self.owner.get()["session_id"], OWNER_SESSION)
        self.assertEqual(self.owner.get_friend()["session_id"], FRIEND_SESSION)

        # Re-binding the owner must not evict the friend.
        self.owner.bind(OWNER_SESSION, source="cli")
        self.assertIsNotNone(self.owner.get_friend())

        # And re-binding the friend must not evict the owner.
        self.owner.bind_friend(FRIEND_SESSION, display_name="姐姐", source="cli")
        self.assertEqual(self.owner.get()["session_id"], OWNER_SESSION)

    def test_friend_is_never_the_owner(self) -> None:
        self.owner.bind(OWNER_SESSION, source="cli")
        self.owner.bind_friend(FRIEND_SESSION, source="cli")
        self.assertTrue(self.owner.is_friend(FRIEND_SESSION))
        self.assertFalse(self.owner.is_owner(FRIEND_SESSION))
        self.assertEqual(self.owner.role_of(FRIEND_SESSION), "friend")
        self.assertEqual(self.owner.role_of(OWNER_SESSION), "owner")
        self.assertIsNone(self.owner.role_of(STRANGER_SESSION))

    def test_friend_cannot_share_the_owner_session(self) -> None:
        self.owner.bind(OWNER_SESSION, source="cli")
        with self.assertRaises(ValueError):
            self.owner.bind_friend(OWNER_SESSION, source="cli")
        self.assertEqual(self.owner.get()["session_id"], OWNER_SESSION)

    def test_friend_rejects_group_session(self) -> None:
        with self.assertRaises(ValueError):
            self.owner.bind_friend(GROUP_SESSION, source="cli")

    def test_unbind_friend_keeps_owner(self) -> None:
        self.owner.bind(OWNER_SESSION, source="cli")
        self.owner.bind_friend(FRIEND_SESSION, source="cli")
        self.assertTrue(self.owner.unbind_friend())
        self.assertIsNone(self.owner.get_friend())
        self.assertIsNotNone(self.owner.get())
        self.assertFalse(self.owner.unbind_friend())

    def test_friend_row_does_not_block_owner_claim(self) -> None:
        self.owner.bind_friend(FRIEND_SESSION, source="cli")
        self.assertIsNone(self.owner.get())
        self.assertTrue(self.owner.status()["friend_bound"])
        self.assertFalse(self.owner.status()["bound"])


class AttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.owner = OwnerRegistry(self.db, self.config)
        self.owner.bind(OWNER_SESSION, source="cli")
        self.owner.bind_friend(FRIEND_SESSION, display_name="姐姐", source="cli")
        self.memory = MemoryService(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_item_keeps_speaker_and_subject(self) -> None:
        item = self.memory.add_item(
            "主人哥哥昨天加班到三点",
            session_id=OWNER_SESSION,
            subject="owner",
            about=["owner"],
            tags=["加班"],
        )
        reloaded = self.memory.get(item.id)
        self.assertEqual(reloaded.subject, "owner")
        self.assertEqual(reloaded.about, ["owner"])
        self.assertEqual(reloaded.session_id, OWNER_SESSION)

    def test_friend_can_report_an_owner_fact(self) -> None:
        item = self.memory.add_item(
            "主人哥哥在考虑换工作",
            session_id=FRIEND_SESSION,
            subject="姐姐",
            about=["主人哥哥"],
        )
        reloaded = self.memory.get(item.id)
        self.assertEqual(reloaded.subject, "friend")
        self.assertEqual(reloaded.about, ["owner"])
        self.assertEqual(
            attribution_line(reloaded, {"owner": "主人哥哥", "friend": "姐姐"}),
            "[姐姐说的·关于主人哥哥]",
        )

    def test_digest_renders_attribution_labels(self) -> None:
        self.memory.add_item(
            "主人哥哥昨天加班到三点",
            session_id=OWNER_SESSION,
            subject="owner",
            about=["owner"],
        )
        assembler = ContextAssembler(self.db, self.config, self.memory)
        text = assembler.build(OWNER_SESSION, "在吗", role="owner").text()
        self.assertIn("[主人哥哥]", text)
        self.assertIn("主人哥哥昨天加班到三点", text)

    def test_open_loop_keeps_session_and_subject(self) -> None:
        loop_id = self.memory.add_open_loop(
            "问问姐姐吉他学得怎么样", session_id=FRIEND_SESSION, subject="friend"
        )
        row = self.db.query_one(
            "SELECT session_id, subject FROM open_loops WHERE id=?", (loop_id,)
        )
        self.assertEqual(row["session_id"], FRIEND_SESSION)
        self.assertEqual(row["subject"], "friend")

    def test_stranger_gets_no_digest_but_group_shares_it(self) -> None:
        marker = "主人哥哥把车钥匙丢在冰箱里了"
        self.memory.add_item(
            marker, session_id=OWNER_SESSION, subject="owner", about=["owner"]
        )
        assembler = ContextAssembler(self.db, self.config, self.memory)
        stranger = assembler.build(STRANGER_SESSION, "在吗", role=None).text()
        self.assertNotIn("短期记忆摘要", stranger)
        self.assertNotIn(marker, stranger)
        # Groups share the owner's memory (boss decision 2026-10-26).
        group = assembler.build(GROUP_SESSION, "在吗", role=None).text()
        self.assertIn("短期记忆摘要", group)
        self.assertIn(marker, group)

    def test_friend_prompt_gets_friend_framing(self) -> None:
        assembler = ContextAssembler(self.db, self.config, self.memory)
        text = assembler.build(FRIEND_SESSION, "在吗", role="friend").text()
        self.assertIn("是主人哥哥亲近的朋友，不是主人", text)
        self.assertIn("称呼她「姐姐」", text)
        self.assertNotIn("已认主的主人哥哥", text)


class SecretNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.owner = OwnerRegistry(self.db, self.config)
        self.owner.bind(OWNER_SESSION, source="cli")
        self.owner.bind_friend(FRIEND_SESSION, display_name="姐姐", source="cli")
        self.memory = MemoryService(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_owner_and_friend_get_different_framings(self) -> None:
        SecretNotes(self.db).add("主人哥哥在考虑换工作，还没定")
        assembler = ContextAssembler(self.db, self.config, self.memory)
        owner_text = assembler.build(OWNER_SESSION, "在吗", role="owner").text()
        friend_text = assembler.build(FRIEND_SESSION, "在吗", role="friend").text()

        self.assertIn("绝对不能对姐姐说", owner_text)
        self.assertIn("绝对不要主动提起", friend_text)
        self.assertIn("换工作", owner_text)
        self.assertIn("换工作", friend_text)

    def test_stranger_gets_no_secret_block(self) -> None:
        SecretNotes(self.db).add("主人哥哥在考虑换工作，还没定")
        assembler = ContextAssembler(self.db, self.config, self.memory)
        text = assembler.build(STRANGER_SESSION, "在吗", role=None).text()
        self.assertNotIn("保密", text)
        self.assertNotIn("换工作", text)

    def test_add_is_idempotent_and_remove_works(self) -> None:
        notes = SecretNotes(self.db)
        first = notes.add("不许说的事")
        second = notes.add("不许说的事")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(notes.count(), 1)
        self.assertTrue(notes.remove(first["id"]))
        self.assertEqual(notes.count(), 0)


class RuntimeAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.runtime.owner.bind_friend(
            FRIEND_SESSION, display_name="姐姐", source="cli"
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_speaker_party_resolution(self) -> None:
        self.assertEqual(
            self.runtime._speaker_party(OWNER_SESSION, TrustLevel.PRIVATE), "owner"
        )
        self.assertEqual(
            self.runtime._speaker_party(FRIEND_SESSION, TrustLevel.PRIVATE), "friend"
        )
        self.assertEqual(
            self.runtime._speaker_party(GROUP_SESSION, TrustLevel.GROUP), "group"
        )
        self.assertEqual(
            self.runtime._speaker_party(STRANGER_SESSION, TrustLevel.PRIVATE), "unknown"
        )

    def test_about_parsing(self) -> None:
        self.assertEqual(self.runtime._about_parties(None, "owner"), ["owner"])
        self.assertEqual(
            self.runtime._about_parties("主人哥哥、姐姐", "friend"),
            ["主人哥哥", "姐姐"],
        )
        self.assertEqual(
            self.runtime._about_parties(["主人哥哥"], "friend"), ["主人哥哥"]
        )

    def test_remember_action_writes_attribution(self) -> None:
        results = self.runtime.execute_actions(
            [
                TurnAction(
                    "remember",
                    {"text": "姐姐在学吉他", "about": "姐姐", "tags": ["吉他"]},
                )
            ],
            session_id=FRIEND_SESSION,
            reply="嗯嗯",
            trust=TrustLevel.PRIVATE,
        )
        self.assertTrue(results[0]["ok"], results[0])
        item = self.runtime.memory.get(results[0]["result"]["id"])
        self.assertEqual(item.subject, "friend")
        self.assertEqual(item.about, ["friend"])
        self.assertEqual(item.session_id, FRIEND_SESSION)

    def test_remember_from_owner_is_tagged_owner(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("remember", {"text": "主人哥哥昨天加班到三点"})],
            session_id=OWNER_SESSION,
            reply="记住了",
            trust=TrustLevel.PRIVATE,
        )
        item = self.runtime.memory.get(results[0]["result"]["id"])
        self.assertEqual(item.subject, "owner")
        self.assertEqual(item.about, ["owner"])

    def test_open_loop_action_writes_attribution(self) -> None:
        results = self.runtime.execute_actions(
            [TurnAction("open_loop", {"text": "问问姐姐吉他学得怎么样"})],
            session_id=FRIEND_SESSION,
            reply="好",
            trust=TrustLevel.PRIVATE,
        )
        loop_id = results[0]["result"]["id"]
        row = self.db.query_one(
            "SELECT session_id, subject FROM open_loops WHERE id=?", (loop_id,)
        )
        self.assertEqual(row["session_id"], FRIEND_SESSION)
        self.assertEqual(row["subject"], "friend")

    def test_runtime_routes_friend_role_into_the_prompt(self) -> None:
        from tests.test_owner import fake_runtime

        calls = fake_runtime(self.runtime)

        def system_text(turn: int) -> str:
            # Role hints are runtime context, deliberately kept out of the
            # cacheable prefix message.
            return "\n".join(
                m["content"] for m in calls[turn]["messages"] if m["role"] == "system"
            )

        self.runtime.handle_user_message(FRIEND_SESSION, "在吗")
        friend_prompt = system_text(0)
        self.assertIn("是主人哥哥亲近的朋友，不是主人", friend_prompt)
        self.assertNotIn("已认主的主人哥哥", friend_prompt)

        self.runtime.handle_user_message(OWNER_SESSION, "在吗")
        self.assertIn("已认主的主人哥哥", system_text(1))

        self.runtime.handle_user_message(STRANGER_SESSION, "在吗")
        stranger_prompt = system_text(2)
        self.assertIn("不是主人", stranger_prompt)
        self.assertNotIn("亲近的朋友", stranger_prompt)
        self.assertNotIn("称呼她", stranger_prompt)

    def test_friend_has_the_same_capability_surface(self) -> None:
        # Full capability: nothing below is owner-gated, so the friend slot
        # inherits it unchanged.
        for tool in ("send_voice", "remember", "open_loop", "schedule_wakeup"):
            spec = self.runtime.policy.specs[tool]
            self.assertIsNotNone(spec)
            self.assertEqual(spec.min_trust, TrustLevel.PRIVATE)

    def test_friend_and_owner_prompts_share_the_stable_prefix(self) -> None:
        assembler = ContextAssembler(self.db, self.config, self.runtime.memory)
        owner_bundle = assembler.build(OWNER_SESSION, "在吗", role="owner")
        friend_bundle = assembler.build(FRIEND_SESSION, "在吗", role="friend")
        self.assertEqual(
            owner_bundle.stable_prefix_hash, friend_bundle.stable_prefix_hash
        )
        self.assertNotEqual(owner_bundle.dynamic_hash, friend_bundle.dynamic_hash)


class FriendCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _, self.db, self.config_path = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", str(self.config_path), *argv])
        raw = out.getvalue().strip()
        return code, (json.loads(raw) if raw else {})

    def test_cli_friend_bind_status_unbind(self) -> None:
        self._run("owner", "bind", "--session", OWNER_SESSION)
        code, payload = self._run(
            "owner", "friend", "bind", "--session", FRIEND_SESSION, "--name", "姐姐"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["binding"]["role"], "friend")
        self.assertEqual(payload["binding"]["display_name"], "姐姐")

        code, payload = self._run("owner", "status")
        self.assertTrue(payload["bound"])
        self.assertTrue(payload["friend_bound"])
        self.assertEqual(payload["owner"]["session_id"], OWNER_SESSION)
        self.assertEqual(payload["friend"]["session_id"], FRIEND_SESSION)

        code, payload = self._run("owner", "friend", "unbind")
        self.assertTrue(payload["removed"])
        code, payload = self._run("owner", "status")
        self.assertTrue(payload["bound"])
        self.assertFalse(payload["friend_bound"])

    def test_cli_friend_rejects_group_session(self) -> None:
        code, payload = self._run("owner", "friend", "bind", "--session", GROUP_SESSION)
        self.assertEqual(code, 2)
        self.assertIn("private", payload["error"])

    def test_cli_secret_add_list_remove(self) -> None:
        code, payload = self._run("owner", "secret", "add", "主人哥哥在考虑换工作")
        self.assertEqual(code, 0)
        note_id = payload["note"]["id"]

        code, payload = self._run("owner", "secret", "list")
        self.assertEqual(payload["count"], 1)

        code, payload = self._run("owner", "secret", "remove", "--id", str(note_id))
        self.assertTrue(payload["removed"])
        code, payload = self._run("owner", "secret", "list")
        self.assertEqual(payload["count"], 0)


if __name__ == "__main__":
    unittest.main()
