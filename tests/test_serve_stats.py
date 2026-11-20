"""Tests for ``scripts/serve_stats.py`` — the pure parsers only.

``nvidia-smi`` output formats and the CPU tick math are what actually break
across driver updates, so those are pinned here. The HTTP layer is exercised
by hand (curl, same as serve_audio.py).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import serve_stats as ss  # noqa: E402


def typeperf_output(header: str, data: str) -> str:
    """A realistic two-line typeperf capture plus its trailing chatter."""
    return header + "\n" + data + "\n\nExiting, please wait...\n"


class TestParseGpuLine(unittest.TestCase):
    def test_normal_row(self):
        row = ss.parse_gpu_line("51, 9, 26.51, 80, 1924, 8151")
        self.assertEqual(row, {
            "temp_c": 51, "util": 9, "power_w": 26.51,
            "power_limit_w": 80, "vram_used_mb": 1924, "vram_total_mb": 8151,
        })

    def test_power_draw_not_available(self):
        # Some drivers report [N/A] while the GPU idles.
        row = ss.parse_gpu_line("48, 0, [N/A], 80, 700, 8151")
        self.assertEqual(row["power_w"], None)
        self.assertEqual(row["temp_c"], 48)
        self.assertEqual(row["vram_used_mb"], 700)

    def test_garbage_row_returns_none(self):
        self.assertIsNone(ss.parse_gpu_line("only, three, fields"))
        self.assertIsNone(ss.parse_gpu_line(""))


class TestParseTypeperf(unittest.TestCase):
    HEADER = ('"(PDH-CSV 4.0)",'
              '"\\\\HOST\\GPU Process Memory(pid_11836_luid_0x0_phys_0)\\Dedicated Usage",'
              '"\\\\HOST\\GPU Process Memory(pid_11836_luid_0x1_phys_0)\\Dedicated Usage",'
              '"\\\\HOST\\GPU Process Memory(pid_99_luid_0x0_phys_0#1)\\Dedicated Usage",'
              '"\\\\HOST\\GPU Process Memory(pid_7_luid_0x0_phys_0)\\Dedicated Usage"')
    DATA = ('"11/15/2026 13:22:43.708","315994112.000000","0.000000",'
            '"1048576.000000","oops"')

    def test_sums_luid_instances_and_drops_zero_or_bad(self):
        out = typeperf_output(self.HEADER, self.DATA)
        self.assertEqual(ss.parse_typeperf(out), {
            11836: 315994112.0,   # two adapter instances summed
            99: 1048576.0,        # the #1 instance suffix is still the pid
        })                        # pid 7 dropped: value failed to parse

    def test_truncated_output_returns_empty(self):
        self.assertEqual(ss.parse_typeperf(""), {})
        self.assertEqual(ss.parse_typeperf(self.HEADER), {})


class TestParseTasklist(unittest.TestCase):
    def test_pid_to_name(self):
        out = ('"python.exe","1234","Console","1","100 K"\n'
               '"explorer.exe","5678","Console","1","50,000 K"\n'
               '"Image Name","PID","Session Name","Session#","Mem Usage"\n')
        self.assertEqual(ss.parse_tasklist(out),
                         {1234: "python.exe", 5678: "explorer.exe"})


class TestCpuPercent(unittest.TestCase):
    def test_known_delta(self):
        # idle +10, kernel +50, user +50 -> busy = 100-10 = 90%
        self.assertAlmostEqual(
            ss.cpu_percent_from_samples((10, 50, 50), (20, 100, 100)), 90.0)

    def test_zero_delta_returns_none(self):
        self.assertIsNone(
            ss.cpu_percent_from_samples((10, 50, 50), (10, 50, 50)))

    def test_clamped_to_0_100(self):
        # idle delta of 0 with growing totals = 100% busy;
        # a negative user delta (clock quirks) must not go below 0.
        self.assertEqual(
            ss.cpu_percent_from_samples((10, 50, 50), (10, 100, 100)), 100.0)
        self.assertEqual(
            ss.cpu_percent_from_samples((0, 100, 100), (150, 200, 100)), 0.0)


class TestLeaseSnapshot(unittest.TestCase):
    def test_json_lock_is_summarized(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "vram.lock"
            lock.write_text('{"agent": "song", "pid": 123}', encoding="utf-8")
            ss.LOCK_PATH = lock
            self.assertEqual(ss.lease_snapshot(), "agent=song pid=123")

    def test_raw_lock_falls_back_to_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "vram.lock"
            lock.write_text("held by worker-1 since 10:00", encoding="utf-8")
            ss.LOCK_PATH = lock
            self.assertEqual(ss.lease_snapshot(), "held by worker-1 since 10:00")

    def test_missing_lock_is_none(self):
        ss.LOCK_PATH = Path(tempfile.gettempdir()) / "no-such-vram.lock"
        self.assertIsNone(ss.lease_snapshot())


if __name__ == "__main__":
    unittest.main()
