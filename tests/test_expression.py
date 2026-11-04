"""Tests for ``soulx/expression.py`` — breaths and air."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy 只存在于唱歌/引擎 venv
    raise unittest.SkipTest("numpy not installed (singing venv only)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "soulx"))

import expression as ex  # noqa: E402

SR = 24000


class TestInhale(unittest.TestCase):
    def test_shape_and_bounds(self):
        b = ex.inhale(SR)
        self.assertEqual(len(b), int(SR * 0.15))
        self.assertLessEqual(float(np.max(np.abs(b))), 1.0)
        self.assertTrue(np.all(np.isfinite(b)))
        # a breath rises then releases: the middle is louder than the very start
        mid = b[len(b) // 3: 2 * len(b) // 3]
        self.assertGreater(float(np.max(np.abs(mid))), 0.5)

    def test_deterministic_for_a_seed(self):
        np.testing.assert_array_equal(ex.inhale(SR, seed=3), ex.inhale(SR, seed=3))


class TestBreath(unittest.TestCase):
    def test_breath_sits_before_the_phrase(self):
        n = SR * 3
        out = ex.breath(n, SR, [1.0], level=0.3, dur_ms=150, lead_ms=40)
        end = int((1.0 - 0.04) * SR)
        start = end - int(SR * 0.15)
        self.assertGreater(float(np.max(np.abs(out[start:end]))), 0.1)
        # nothing after the phrase start
        self.assertEqual(float(np.max(np.abs(out[end:end + SR // 2]))), 0.0)

    def test_zero_level_is_silent(self):
        self.assertEqual(float(np.max(np.abs(ex.breath(SR * 2, SR, [1.0], 0.0)))), 0.0)

    def test_a_breath_too_close_to_zero_is_skipped(self):
        out = ex.breath(SR, SR, [0.01], level=0.5)
        self.assertEqual(float(np.max(np.abs(out))), 0.0)


class TestAiry(unittest.TestCase):
    def test_adds_a_little_and_stays_finite(self):
        y = np.zeros(SR, dtype=np.float32)
        y[1000:2000] = 0.3
        out = ex.airy(y, SR, amount=0.06)
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertGreater(float(np.max(np.abs(out - y))), 0.0)
        self.assertLess(float(np.max(np.abs(out))), 0.4)

    def test_zero_amount_is_identity(self):
        y = np.zeros(SR, dtype=np.float32)
        np.testing.assert_array_equal(ex.airy(y, SR, amount=0.0), y)


if __name__ == "__main__":
    unittest.main()
