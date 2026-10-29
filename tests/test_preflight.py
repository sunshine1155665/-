"""Tests for ``scripts/preflight.py`` — parsers and the go/no-go decision.

No GPU, no network, no model: the guards are pinned with fixed strings, which is
the whole point (a pre-flight check that only works when the machine is already
healthy is not a check).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import preflight as pf  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_nvidia_smi_csv(self):
        g = pf.parse_nvidia_smi("6784, 8151\n")
        self.assertIsNotNone(g)
        self.assertAlmostEqual(g.used, 6784 / 1024, places=3)
        self.assertAlmostEqual(g.total, 8151 / 1024, places=3)
        self.assertAlmostEqual(g.free, (8151 - 6784) / 1024, places=3)

    def test_nvidia_smi_ignores_junk_and_handles_units(self):
        g = pf.parse_nvidia_smi("no devices\n4096 MiB, 8192 MiB\n")
        self.assertIsNotNone(g)
        self.assertAlmostEqual(g.used, 4.0)
        self.assertAlmostEqual(g.free, 4.0)
        self.assertIsNone(pf.parse_nvidia_smi(""))

    def test_meminfo(self):
        text = ("MemTotal:       33234512 kB\n"
                "MemFree:         1048576 kB\n"
                "MemAvailable:    6291456 kB\n"
                "Buffers:          100000 kB\n")
        avail, total = pf.parse_meminfo(text)
        self.assertAlmostEqual(avail, 6.0, places=2)
        self.assertAlmostEqual(total, 33234512 / 1024 ** 2, places=2)
        self.assertIsNone(pf.parse_meminfo("nonsense"))


class TestVerdict(unittest.TestCase):
    def gpu(self, used: float) -> pf.Gpu:
        return pf.Gpu(used=used, total=8.0)

    def test_room_is_green(self):
        ok, notes = pf.verdict(5.5, 6.0, self.gpu(1.0), (8.0, 32.0), {9880: False})
        self.assertTrue(ok, notes)
        self.assertTrue(any("RAM" in n and "够" in n for n in notes))

    def test_busy_gpu_is_red(self):
        # The 99% case: TTS resident and two other jobs on the card.
        ok, notes = pf.verdict(5.5, 6.0, self.gpu(6.8), (8.0, 32.0),
                               {9880: True})
        self.assertFalse(ok)
        self.assertTrue(any("只剩" in n for n in notes))
        self.assertTrue(any("indextts:9880" in n for n in notes))

    def test_low_ram_is_red_even_with_gpu_room(self):
        ok, notes = pf.verdict(5.5, 6.0, self.gpu(1.0), (2.0, 32.0), {})
        self.assertFalse(ok)
        self.assertTrue(any(n.startswith("RAM:") and "只剩" in n for n in notes))

    def test_missing_probes_are_red(self):
        ok, notes = pf.verdict(5.5, 6.0, None, None, {})
        self.assertFalse(ok)
        self.assertEqual(len(notes), 3, notes)


if __name__ == "__main__":
    unittest.main()
