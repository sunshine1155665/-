"""Tests for ``soulx/reverb.py``.

The reverb exists to stop the vocal sounding like a MIDI file, so the properties
that matter are: wet=0 is a no-op, the output is the same length, the tail
actually exists past the dry signal, and nothing clips or turns into NaN.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "soulx"))

import reverb as rv  # noqa: E402

SR = 24000


def tone(seconds: float = 0.5, sr: int = SR, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestReverb(unittest.TestCase):
    def test_wet_zero_is_identity(self):
        x = tone()
        np.testing.assert_array_equal(rv.reverb(x, SR, wet=0.0), x)

    def test_length_is_preserved(self):
        x = tone(0.4)
        for wet in (0.1, 0.2, 1.0):
            self.assertEqual(len(rv.reverb(x, SR, wet=wet)), len(x))

    def test_an_impulse_keeps_ringing(self):
        # A click at 0.1 s, silence after. The wet path must still have energy
        # later in the buffer (the dry signal is exactly zero there).
        x = np.zeros(SR // 2, dtype=np.float32)
        x[SR // 10] = 0.5
        y = rv.reverb(x, SR, wet=1.0)
        later = y[SR // 4: SR // 3]
        self.assertGreater(float(np.max(np.abs(later))), 1e-4)

    def test_stays_bounded_and_finite(self):
        x = tone(0.5)
        y = rv.reverb(x, SR, wet=0.3)
        self.assertTrue(np.all(np.isfinite(y)))
        self.assertLessEqual(float(np.max(np.abs(y))), 1.0)

    def test_stereo_in_stereo_out(self):
        x = np.stack([tone(0.3), tone(0.3, freq=330.0)], axis=1)
        y = rv.reverb(x, SR, wet=0.2)
        self.assertEqual(y.shape, x.shape)
        self.assertTrue(np.all(np.isfinite(y)))


if __name__ == "__main__":
    unittest.main()
