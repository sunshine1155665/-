"""Emotion presets (``song_v2.EMOTIONS``) and the CLI resolution around them.

There is no emotion embedding in this SVS checkpoint, so an emotion is a
*setting* of the performance knobs. These tests pin the mapping (so a preset
cannot silently lose a knob) and pin the precedence: preset first, an explicit
flag wins.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "soulx"))

import song_v2 as s2  # noqa: E402

KNOBS = ("rate_scale", "vibrato", "vibrato_hz", "drift_cents", "scoop",
         "breath", "air", "reverb", "backing_db", "legato")


class TestPresets(unittest.TestCase):
    def test_every_preset_is_complete(self):
        for name in s2.EMOTIONS:
            p = s2.emotion_params(name)
            for k in KNOBS:
                self.assertIn(k, p, f"{name} missing {k}")
                self.assertIsInstance(p[k], float, f"{name}.{k} not a number")

    def test_unknown_emotion_raises(self):
        with self.assertRaises(KeyError):
            s2.emotion_params("angry")

    def test_genki_is_quicker_and_cleaner_than_sad(self):
        g, s = s2.emotion_params("genki"), s2.emotion_params("sad")
        self.assertGreater(g["rate_scale"], s["rate_scale"])     # faster
        self.assertLess(g["vibrato"], s["vibrato"])              # less wavy
        self.assertLess(g["breath"], s["breath"])                # less air
        self.assertGreater(s["reverb"], g["reverb"])             # darker room

    def test_soft_is_the_middle_ground(self):
        soft, genki, sad = (s2.emotion_params(n) for n in ("soft", "genki", "sad"))
        self.assertLess(genki["rate_scale"], 1.0)
        self.assertGreater(genki["rate_scale"], sad["rate_scale"])
        self.assertLess(genki["rate_scale"], soft["rate_scale"] + 0.001 + 0.2)
        self.assertGreater(soft["breath"], genki["breath"])
        self.assertLess(soft["breath"], sad["breath"])

    def test_legato_stays_in_a_sane_range(self):
        for name in s2.EMOTIONS:
            thr = s2.emotion_params(name)["legato"]
            self.assertGreater(thr, 0.3)
            self.assertLess(thr, 0.8)


class TestCli(unittest.TestCase):
    def test_preset_fills_the_defaults(self):
        a = s2.parse_args(["--emotion", "genki"])
        self.assertEqual(a.rate_scale, s2.EMOTIONS["genki"]["rate_scale"])
        self.assertEqual(a.vibrato, s2.EMOTIONS["genki"]["vibrato"])

    def test_explicit_flag_beats_the_preset(self):
        a = s2.parse_args(["--emotion", "sad", "--vibrato", "9",
                           "--rate-scale", "0.9"])
        self.assertEqual(a.vibrato, 9.0)
        self.assertEqual(a.rate_scale, 0.9)
        # the rest still comes from the preset
        self.assertEqual(a.scoop, s2.EMOTIONS["sad"]["scoop"])

    def test_default_emotion_is_soft(self):
        a = s2.parse_args([])
        self.assertEqual(a.emotion, "soft")
        self.assertEqual(a.rate_scale, s2.EMOTIONS["soft"]["rate_scale"])

    def test_a_bare_run_reproduces_the_shipped_settings(self):
        # "默认最好" only holds if the defaults are the take that shipped:
        # native key (no resample) and 64 flow-matching steps.
        a = s2.parse_args([])
        self.assertTrue(a.native_key)
        self.assertEqual(a.n_steps, 64)
        self.assertGreater(a.vibrato, 0)
        self.assertAlmostEqual(a.backing_db, -7.0)
        self.assertGreater(a.legato, 0)

    def test_old_paths_are_still_reachable(self):
        a = s2.parse_args(["--no-native-key"])
        self.assertFalse(a.native_key)


if __name__ == "__main__":
    unittest.main()
