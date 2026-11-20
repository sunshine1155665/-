from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.context import ContextAssembler
from fatfish.growth import MAX_ENTRIES, append_growth, growth_block, load_growth
from fatfish.memory import MemoryService
from tests.test_owner import make_env


class GrowthLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, _ = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)
        self.core_before = self.db.query_one(
            "SELECT core_json FROM persona WHERE id='default'"
        )["core_json"]

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_growth_is_appended_after_the_original_persona(self) -> None:
        self.assertEqual("", growth_block(self.db))
        result = append_growth(
            self.db,
            ["先听完再回答", "被夸时先把开心收下。"],
            source_date="2026-10-22",
        )
        self.assertEqual(2, result["added"])

        stable = self.assembler.stable_prefix()
        self.assertIn("【成长层】", stable)
        self.assertIn("先听完再回答", stable)
        self.assertIn("被夸时先把开心收下", stable)
        self.assertLess(stable.index("【边界】"), stable.index("【成长层】"))
        self.assertLess(stable.index("【成长层】"), stable.index("【工具】"))
        self.assertEqual(
            self.core_before,
            self.db.query_one("SELECT core_json FROM persona WHERE id='default'")["core_json"],
        )

    def test_private_facts_and_meta_notes_are_rejected(self) -> None:
        result = append_growth(
            self.db,
            [
                "主人哥哥昨晚很累",
                "今天要早点睡",
                "作为AI我不能这样",
                "先听完再回答",
            ],
            source_date="2026-10-22",
        )
        self.assertEqual(1, result["added"])
        self.assertEqual(3, result["rejected"])
        self.assertEqual(["先听完再回答"], [item["text"] for item in load_growth(self.db)])

    def test_growth_is_bounded_and_rerun_is_idempotent(self) -> None:
        for index in range(10):
            append_growth(
                self.db,
                [f"原则{index}：先听完再回答"],
                source_date=f"2026-10-{index + 1:02d}",
            )
        self.assertEqual(MAX_ENTRIES, len(load_growth(self.db)))
        before = len(load_growth(self.db))

        again = append_growth(
            self.db,
            ["原则9：先听完再回答"],
            source_date="2026-10-22",
        )
        self.assertEqual(0, again["added"])
        self.assertEqual(1, again["updated"])
        self.assertEqual(before, len(load_growth(self.db)))


if __name__ == "__main__":
    unittest.main()
