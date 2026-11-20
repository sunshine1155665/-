"""Unit tests for scripts/pet_sticker_gen.py pure helpers (no network, no PIL)."""

import importlib.util
import os
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "pet_sticker_gen",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "pet_sticker_gen.py"),
)
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


class ParseGridTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(gen.parse_grid("2x2"), (2, 2))
        self.assertEqual(gen.parse_grid("3X1"), (3, 1))
        self.assertEqual(gen.parse_grid("2"), (2, 1))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            gen.parse_grid("0x2")
        with self.assertRaises(ValueError):
            gen.parse_grid("4x4")  # 16 cells > 12 cap


class CellBoxesTest(unittest.TestCase):
    def test_row_major_and_pixel_math(self):
        boxes = gen.cell_boxes(100, 200, 2, 2)
        self.assertEqual(boxes, [(0, 0, 50, 100), (50, 0, 100, 100), (0, 100, 50, 200), (50, 100, 100, 200)])

    def test_single_row(self):
        self.assertEqual(gen.cell_boxes(90, 10, 3, 1), [(0, 0, 30, 10), (30, 0, 60, 10), (60, 0, 90, 10)])


class PresetsTest(unittest.TestCase):
    def test_cells_fit_grid_and_are_unique(self):
        for name, spec in gen.PRESETS.items():
            cols, rows = spec["grid"]
            self.assertLessEqual(
                len(spec["cells"]), cols * rows, f"preset {name} cells exceed grid"
            )
            self.assertEqual(
                len(spec["cells"]), len(set(spec["cells"])), f"preset {name} has duplicate cells"
            )
            for cell in spec["cells"]:
                self.assertRegex(cell, r"^[a-z0-9]+$")

    def test_scene_dirs_safe(self):
        for spec in gen.PRESETS.values():
            self.assertRegex(spec["scene"], r"^[a-z]+$")


class LoadEnvTest(unittest.TestCase):
    def test_loads_key_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comment\nPET_TEST_KEY=abc\n\nBAD_LINE\nPET_TEST_EMPTY=\n")
            gen.load_env_keys(path)
            self.assertEqual(os.environ.get("PET_TEST_KEY"), "abc")
            self.assertEqual(os.environ.get("PET_TEST_EMPTY"), "")
            # pre-set env must win over the file
            os.environ["PET_TEST_KEY2"] = "keep"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("PET_TEST_KEY2=overwritten\n")
            gen.load_env_keys(path)
            self.assertEqual(os.environ.get("PET_TEST_KEY2"), "keep")
            os.environ.pop("PET_TEST_KEY", None)
            os.environ.pop("PET_TEST_EMPTY", None)
            os.environ.pop("PET_TEST_KEY2", None)


if __name__ == "__main__":
    unittest.main()
