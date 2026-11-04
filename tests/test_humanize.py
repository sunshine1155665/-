"""Tests for ``soulx/humanize.py`` — the pitch-contour maths.

``humanize()`` itself needs pyworld (only installed in the soullx venv), so the
pure parts are pinned here: where the vibrato is allowed to start and stop, and
that the contour only touches voiced frames.
"""

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

import humanize as hu  # noqa: E402


class TestNoteEnvelope(unittest.TestCase):
    def test_vibrato_ramps_in_and_out(self):
        # One 1.0 s note starting at 0.5 s, 5 ms frames (400 frames = 2 s).
        env = hu.note_envelope(400, 5.0, [(0.5, 1.0)], ramp_s=0.12)
        self.assertEqual(float(env[0]), 0.0)         # before the note
        self.assertLess(float(env[105]), 0.5)        # ramping in at 0.525 s
        self.assertAlmostEqual(float(env[200]), 1.0, places=2)   # 1.0 s: middle
        self.assertLess(float(env[295]), 1.0)        # 1.475 s: releasing
        self.assertEqual(float(env[-1]), 0.0)

    def test_short_notes_get_no_vibrato(self):
        # A 0.2 s note cannot hold a vibrato; adding one makes it warble.
        env = hu.note_envelope(400, 5.0, [(0.5, 0.2)])
        self.assertEqual(float(env.max()), 0.0)

    def test_release_does_not_eat_the_whole_note(self):
        env = hu.note_envelope(400, 5.0, [(0.2, 0.3)], ramp_s=0.12)
        self.assertGreaterEqual(float(env.max()), 0.5)


class TestContour(unittest.TestCase):
    def f0(self, n=400, hz=220.0):
        return np.full(n, hz, dtype=np.float64)

    def test_unvoiced_frames_stay_zero(self):
        f0 = self.f0()
        f0[100:150] = 0.0
        env = np.ones(len(f0), dtype=np.float32)
        out = hu.contour(f0, 5.0, env, cents=20.0)
        self.assertTrue(np.all(out[100:150] == 0.0))
        self.assertTrue(np.all(out[:100] > 0))

    def test_depth_scales_the_movement(self):
        env = np.ones(400, dtype=np.float32)
        small = hu.contour(self.f0(), 5.0, env, cents=5.0, drift_cents=0.0)
        big = hu.contour(self.f0(), 5.0, env, cents=25.0, drift_cents=0.0)
        self.assertLess(small.std(), big.std())
        # ~25 cents peak = 220 * (2**(25/1200) - 1) ≈ 3.2 Hz
        self.assertAlmostEqual(float(big.max() - big.min()), 6.4, delta=0.8)

    def test_zero_envelope_means_no_vibrato(self):
        env = np.zeros(400, dtype=np.float32)
        out = hu.contour(self.f0(), 5.0, env, cents=25.0, drift_cents=0.0)
        np.testing.assert_allclose(out, self.f0())


if __name__ == "__main__":
    unittest.main()
