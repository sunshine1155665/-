from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import (
    DEFAULT_PERSONA,
    FACT_RULES,
    OUTPUT_RULES,
    ContextAssembler,
    ensure_default_persona,
)
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.security import kernel_policy_text


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'test.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128
"""
    config_path = tmp / "test.toml"
    config_path.write_text(toml, encoding="utf-8")
    config = load_config("low", config_path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


class FactRulesContentTests(unittest.TestCase):
    def test_fact_rules_cover_required_prohibitions(self) -> None:
        required = (
            "当前用户消息",
            "[最近对话]",
            "[本轮检索证据]",
            "不编造过去",
            "不要推断",
            "我现在在",
            "我记不清",
            "这我没印象",
            "记录里没有",
            "语气示例",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, FACT_RULES)

    def test_fact_rules_are_concise_bullets(self) -> None:
        bullets = [
            line for line in FACT_RULES.splitlines() if line.strip().startswith("- ")
        ]
        self.assertGreaterEqual(len(bullets), 8)
        self.assertLessEqual(len(bullets), 12)

    def test_output_rules_embed_fact_rules(self) -> None:
        self.assertIn("回复规则", OUTPUT_RULES)
        self.assertIn("只输出 JSON", OUTPUT_RULES)
        self.assertIn(FACT_RULES, OUTPUT_RULES)

    def test_fact_rules_name_the_unsupported_examples(self) -> None:
        for phrase in ("你早上没找我", "我一直在等你", "我今天刷了B站"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, FACT_RULES)


class StablePrefixTruthfulnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_stable_prefix_includes_fact_rules(self) -> None:
        stable = self.assembler.stable_prefix()
        self.assertIn("【输出规则】", stable)
        self.assertIn(FACT_RULES, stable)
        self.assertIn(OUTPUT_RULES, stable)

    def test_stable_prefix_keeps_kernel_intact(self) -> None:
        stable = self.assembler.stable_prefix()
        self.assertIn(self.assembler.kernel.text(), stable)
        self.assertIn(kernel_policy_text(), stable)
        self.assertEqual(len(self.assembler.kernel.digest), 64)

    def test_stable_prefix_keeps_persona_intact(self) -> None:
        stable = self.assembler.stable_prefix()
        core = DEFAULT_PERSONA["core"]
        for key in ("identity", "personality", "speech", "relationship"):
            with self.subTest(key=key):
                self.assertIn(core[key], stable)
        for boundary in core["boundaries"]:
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, stable)
        for example in core["examples"]:
            with self.subTest(example=example):
                self.assertIn(example, stable)

    def test_rules_live_in_the_cacheable_stable_section(self) -> None:
        bundle = self.assembler.build("truth-session", "我早上有没有找过你？")
        by_name = {section.name: section for section in bundle.sections}
        stable_section = by_name["stable_prefix"]
        self.assertTrue(stable_section.cacheable)
        self.assertIn(FACT_RULES, stable_section.text)
        self.assertNotIn(FACT_RULES, by_name["history"].text)
        self.assertNotIn(FACT_RULES, by_name["runtime_context"].text)

    def test_stable_prefix_is_repeatable(self) -> None:
        first = self.assembler.build("truth-session", "你好")
        second = self.assembler.build("truth-session", "你好")
        self.assertEqual(first.stable_prefix_hash, second.stable_prefix_hash)
        self.assertIn(FACT_RULES, first.sections[0].text)


if __name__ == "__main__":
    unittest.main()
