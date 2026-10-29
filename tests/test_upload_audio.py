"""Tests for ``scripts/upload_audio.py`` — the multipart encoder only.

No network: the encoder is the part that can silently produce a 400 from the
host, so it is pinned here; ``upload()`` is a thin urllib wrapper.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import upload_audio as up  # noqa: E402


class TestMultipart(unittest.TestCase):
    def test_body_has_fields_and_file(self):
        body, ctype = up.multipart({"reqtype": "fileupload", "time": "72h"},
                                   "fileToUpload", "a.wav", b"RIFFdata",
                                   "audio/wav")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=", 1)[1].encode()
        self.assertIn(b"name=\"reqtype\"", body)
        self.assertIn(b"fileupload", body)
        self.assertIn(b"name=\"time\"", body)
        self.assertIn(b"filename=\"a.wav\"", body)
        self.assertIn(b"Content-Type: audio/wav", body)
        self.assertIn(b"RIFFdata", body)
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))

    def test_boundary_is_unique_per_call(self):
        _, a = up.multipart({}, "f", "x", b"", "text/plain")
        _, b = up.multipart({}, "f", "x", b"", "text/plain")
        self.assertNotEqual(a, b)

    def test_binary_payload_is_not_mangled(self):
        payload = bytes(range(256))
        body, _ = up.multipart({}, "f", "x.bin", payload, "application/octet-stream")
        self.assertIn(payload, body)


if __name__ == "__main__":
    unittest.main()
