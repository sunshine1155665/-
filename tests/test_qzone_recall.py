"""Offline tests for the QQ Zone privacy path: exclude_scopes recall, prompt
assembly, keyword intent and the in-conversation review bypass. No network."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ContextAssembler, ensure_default_persona, prompt_excluded_scopes
from fatfish.db import Database
from fatfish.llm import LLMResult
from fatfish.memory import MemoryService
from fatfish.qzone import QzoneCollector
from fatfish.runtime import Runtime, wants_qzone_review
from fatfish.voice import VoiceDecision

OWNER_UIN = "10000"
FRIEND_UIN = "20001"


def make_env(tmp: Path, *, friends_to_llm: str = "false"):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'recall.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[qzone]
enabled = false
owner_uin = "{OWNER_UIN}"
friends_content_to_llm = {friends_to_llm}
"""
    path = tmp / "recall.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


def seed_dynamics(memory: MemoryService, config) -> QzoneCollector:
    collector = QzoneCollector(memory, config)
    items = [
        {
            "external_id": "own1",
            "author_uin": OWNER_UIN,
            "author_name": "主人",
            "content": "主人今天去爬山了",
            "media": [],
            "published_at": time.time() - 3600,
            "url": "",
            "raw": {},
        },
        {
            "external_id": "fr1",
            "author_uin": FRIEND_UIN,
            "author_name": "好友甲",
            "content": "好友甲在 restaurant 吃火锅",
            "media": [],
            "published_at": time.time() - 1800,
            "url": "",
            "raw": {},
        },
    ]
    stored = collector.store_items(items)
    collector.ingest_into_memory(stored)
    return collector


class ExcludeScopesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        seed_dynamics(self.memory, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_default_search_is_unchanged(self) -> None:
        hits = self.memory.search("动态", limit=10)
        origins = {h.item.origin_scope for h in hits}
        self.assertIn("qzone", origins)
        self.assertIn("qzone_friend", origins)

    def test_search_excludes_qzone_friend(self) -> None:
        hits = self.memory.search(
            "动态", limit=10, exclude_scopes=frozenset({"qzone_friend"})
        )
        self.assertTrue(hits)
        for hit in hits:
            self.assertNotEqual(hit.item.origin_scope, "qzone_friend")

    def test_search_rag_excludes_by_metadata_source(self) -> None:
        # Keyword hits make these deterministic under the hash embedder.
        self.assertTrue(any(h.source_id == "fr1" for h in self.memory.search_rag("吃火锅", limit=10)))
        self.assertTrue(any(h.source_id == "own1" for h in self.memory.search_rag("爬山", limit=10)))
        filtered = self.memory.search_rag(
            "吃火锅", limit=10, exclude_scopes=frozenset({"qzone_friend"})
        )
        self.assertFalse(any(h.source_id == "fr1" for h in filtered))
        self.assertTrue(any(h.source_id == "own1" for h in self.memory.search_rag("爬山", limit=10)))

    def test_short_term_digest_and_meta_directory_exclude(self) -> None:
        scopes = frozenset({"qzone_friend"})
        digests = self.memory.short_term_digest(limit=10, exclude_scopes=scopes)
        self.assertTrue(digests)
        for item in digests:
            self.assertNotEqual(item.origin_scope, "qzone_friend")
        without = self.memory.short_term_digest(limit=10)
        self.assertTrue(any(i.origin_scope == "qzone_friend" for i in without))
        meta = self.memory.meta_directory(limit=20, exclude_scopes=scopes)
        names = {m["name"] for m in meta}
        self.assertNotIn("好友甲", names)


class PromptExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bundle_text(self, friends_to_llm: str) -> str:
        config, db = make_env(self.tmp, friends_to_llm=friends_to_llm)
        try:
            memory = MemoryService(db, config)
            seed_dynamics(memory, config)
            assembler = ContextAssembler(db, config, memory)
            bundle = assembler.build("default", "在吗", role="owner", now=time.time())
            return bundle.text()
        finally:
            db.close()

    def test_friend_dynamics_absent_from_prompt_by_default(self) -> None:
        text = self._bundle_text("false")
        self.assertIn("主人今天去爬山了", text)
        self.assertNotIn("吃火锅", text)
        # T-0060 后好友的名字可能经【认识的人】名册出现——名册只带称呼/关系/
        # 备注，不带动态内容。名字若出现，只允许在名册块内；名册之外（摘要、
        # 元记忆目录、检索证据）仍然一个字都不许有。
        roster = ""
        if "【认识的人】" in text:
            start = text.index("【认识的人】")
            end = text.find("\n【", start + 1)
            roster = text[start:] if end == -1 else text[start:end]
        self.assertNotIn("好友甲", text.replace(roster, ""))

    def test_friend_dynamics_allowed_when_opted_in(self) -> None:
        text = self._bundle_text("true")
        self.assertIn("好友甲", text)

    def test_prompt_excluded_scopes_helper(self) -> None:
        config, db = make_env(self.tmp, friends_to_llm="false")
        try:
            self.assertEqual(prompt_excluded_scopes(config), frozenset({"qzone_friend"}))
        finally:
            db.close()
        config2, db2 = make_env(self.tmp, friends_to_llm="true")
        try:
            self.assertEqual(prompt_excluded_scopes(config2), frozenset())
        finally:
            db2.close()


class WantsQzoneReviewTests(unittest.TestCase):
    def test_positive_cases(self) -> None:
        for text in (
            "看看我QQ空间有什么新动态",
            "翻翻我的空间动态",
            "帮我拉一下说说列表",
            "空间里有什么新鲜事",
            "刷一下好友动态",
            "QQ空间",
        ):
            self.assertTrue(wants_qzone_review(text), text)

    def test_negative_cases(self) -> None:
        for text in (
            "",
            "随便说说你今天干了啥",
            "跟你说说我今天面试的事",
            "别看我的空间",
            "不用翻空间了",
            "你倒是说说看啊",
            "今天晚饭吃什么好",
            "我们聊聊别的吧",
        ):
            self.assertFalse(wants_qzone_review(text), text)


class BypassInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        ensure_default_persona(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _make_runtime(self, prompts: list[str]):
        runtime = Runtime(self.db, self.config)
        runtime.llm.api_key = "fake"

        def chat(messages, *args, **kwargs):
            prompts.append(
                "\n".join(str(m.get("content", "")) for m in messages)
            )
            return LLMResult(text='{"messages":["嗯，本鲸鱼娘看到了。"]}', model="fake-model")

        runtime.llm.chat = chat
        runtime.voice.decide = lambda *args, **kwargs: VoiceDecision(
            "text_only", 0.1, ["test"]
        )
        return runtime

    def test_review_injects_local_evidence_wrapped(self) -> None:
        prompts: list[str] = []
        runtime = self._make_runtime(prompts)
        seed_dynamics(runtime.memory, self.config)
        result = runtime.handle_user_message("default", "看看我QQ空间有什么新动态")
        self.assertTrue(prompts)
        prompt = prompts[0]
        self.assertIn("qzone_feed", prompt)
        self.assertIn("主人今天去爬山了", prompt)
        # friends_content_to_llm=false: friend content must not reach the LLM.
        self.assertNotIn("吃火锅", prompt)
        self.assertNotIn("好友甲", prompt)
        # Withheld friend rows surface as a local hint instead.
        self.assertTrue(any("好友动态" in m and "Web" in m for m in result["messages"]))

    def test_review_with_empty_store(self) -> None:
        prompts: list[str] = []
        runtime = self._make_runtime(prompts)
        runtime.handle_user_message("default", "看看我QQ空间有什么新动态")
        self.assertIn("还没有存储的QQ空间动态", prompts[0])

    def test_group_chat_never_injects(self) -> None:
        prompts: list[str] = []
        runtime = self._make_runtime(prompts)
        seed_dynamics(runtime.memory, self.config)
        runtime.handle_user_message("qq_group:1", "看看我QQ空间有什么新动态", is_group=True)
        self.assertNotIn("qzone_feed", prompts[0])

    def test_unrelated_message_does_not_inject(self) -> None:
        prompts: list[str] = []
        runtime = self._make_runtime(prompts)
        seed_dynamics(runtime.memory, self.config)
        runtime.handle_user_message("default", "今天晚饭吃什么好")
        self.assertNotIn("qzone_feed", prompts[0])


if __name__ == "__main__":
    unittest.main()
