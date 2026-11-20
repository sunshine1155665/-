"""Tests for ``scripts/serve_audio.py`` — the Range parser only.

Mobile Safari sends ``Range`` before it plays, and answering 200 with the whole
file is what makes the audio element stall, so the parser is pinned here. The
handler itself is exercised by hand (curl, see docs/branches/ops.md).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import serve_audio as sa  # noqa: E402


class TestParseRange(unittest.TestCase):
    def test_no_header_means_whole_file(self):
        self.assertIsNone(sa.parse_range(None, 1000))
        self.assertIsNone(sa.parse_range("", 1000))
        self.assertIsNone(sa.parse_range("items=0-1", 1000))

    def test_open_ended(self):
        self.assertEqual(sa.parse_range("bytes=100-", 1000), (100, 999))

    def test_closed(self):
        self.assertEqual(sa.parse_range("bytes=0-1023", 5000), (0, 1023))

    def test_suffix_range(self):
        self.assertEqual(sa.parse_range("bytes=-500", 5000), (4500, 4999))

    def test_end_is_clamped(self):
        self.assertEqual(sa.parse_range("bytes=990-2000", 1000), (990, 999))

    def test_only_first_range_is_used(self):
        self.assertEqual(sa.parse_range("bytes=10-20,30-40", 1000), (10, 20))

    def test_nonsense_falls_back_to_whole_file(self):
        for bad in ("bytes=abc-def", "bytes=1000-", "bytes=20-10",
                    "bytes=-0", "bytes="):
            self.assertIsNone(sa.parse_range(bad, 1000), bad)


if __name__ == "__main__":
    unittest.main()
