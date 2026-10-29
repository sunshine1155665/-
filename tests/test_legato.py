"""Tests for ``soulx/legato.py``.

The point of the module is narrow: find the deep valleys at note boundaries and
carry the vowel through them, without touching anything else.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "soulx"))

import legato as lg  # noqa: E402

SR = 24000


def two_notes(gap_s: float = 0.04, mark_s: float = 0.5) -> tuple[np.ndarray, float]:
    """0.30 s tone, a silent gap, 0.30 s tone; returns (audio, boundary_s)."""
    t = np.arange(int(0.30 * SR)) / SR
    tone = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    gap = np.zeros(int(gap_s * SR), dtype=np.float32)
    y = np.concatenate([tone, gap, tone])
    return y, 0.30 + gap_s / 2


class TestFindValleys(unittest.TestCase):
    def test_finds_a_deep_gap(self):
        y, b = two_notes()
        env, hop = lg.envelope(y, SR)
        span = lg.find_valleys(env, hop, b)
        self.assertIsNotNone(span)
        a, z = span
        self.assertLess(a, b)
        self.assertGreater(z, b)

    def test_ignores_a_shallow_dip(self):
        t = np.arange(int(0.6 * SR)) / SR
        y = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        env, hop = lg.envelope(y, SR)
        self.assertIsNone(lg.find_valleys(env, hop, 0.3))


class TestFillValleys(unittest.TestCase):
    def test_the_gap_gets_filled(self):
        y, b = two_notes(gap_s=0.03)
        out = lg.fill_valleys(y, SR, [b])
        self.assertEqual(len(out), len(y))
        gap = out[int(0.30 * SR):int(0.33 * SR)]
        self.assertGreater(float(np.sqrt(np.mean(gap ** 2))), 0.02)

    def test_a_wide_closure_is_not_repeated(self):
        # 都 sits behind a 90 ms closure; copying a vowel into that is a
        # stutter (heard as 脏音). Wide valleys only get a level lift.
        y, b = two_notes(gap_s=0.12)
        out = lg.fill_valleys(y, SR, [b])
        orig = float(np.sqrt(np.mean(y[int(0.33 * SR):int(0.40 * SR)] ** 2)))
        new = float(np.sqrt(np.mean(out[int(0.33 * SR):int(0.40 * SR)] ** 2)))
        self.assertGreaterEqual(new, orig - 1e-6)
        # and the copied vowel must not show up as a plateau of previous audio
        self.assertLess(new, 0.25)

    def test_far_from_the_boundary_is_untouched(self):
        y, b = two_notes()
        out = lg.fill_valleys(y, SR, [b])
        np.testing.assert_allclose(out[:int(0.2 * SR)], y[:int(0.2 * SR)])
        np.testing.assert_allclose(out[-int(0.1 * SR):], y[-int(0.1 * SR):])

    def test_a_clean_note_is_not_modified(self):
        t = np.arange(int(0.6 * SR)) / SR
        y = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        out = lg.fill_valleys(y, SR, [0.3])
        np.testing.assert_allclose(out, y, atol=1e-6)

    def test_stays_bounded(self):
        y, b = two_notes(gap_s=0.06)
        out = lg.fill_valleys(y, SR, [b, b], gain=1.0)   # duplicate must be safe
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertLessEqual(float(np.max(np.abs(out))), 0.45)


if __name__ == "__main__":
    unittest.main()
