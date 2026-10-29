"""Tests for ``scripts/finalize_song.py`` — freezing a take.

The point of the script is that the *settings* travel with the audio (the render
is stochastic, so a take cannot be regenerated). These tests build a fake render
in a temp dir and check the files, the manifest and the reconstructed command.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import finalize_song as fz  # noqa: E402

REPORT = {
    "title": "等你回家", "tonic": 60, "semitones": 3.0, "native_key": True,
    "shift_mode": "resample", "n_steps": 64, "cfg": 3.0, "reverb": 0.14,
    "vibrato": 18.0, "vibrato_hz": 5.2, "drift_cents": 5.0,
    "gap_beats": [1.1, 1.0, 1.4, 0.0], "duration_s": 16.44,
    "emotion": "soft", "rate_scale": 0.78, "legato": 0.55, "scoop": 25.0,
    "breath": 0.08, "air": 0.05, "backing_db": -7.0,
    "phrases": [{"text": "本鲸鱼娘今天好想你", "jianpu": "1 2 3 5 6 5 4 3 2",
                 "degrees": [0, 2, 4, 7, 9, 7, 5, 4, 2], "beat": 0.32,
                 "mults": [1.0] * 9}],
}


class TestFreeze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "melody_v12"
        self.src.mkdir()
        (self.src / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False), encoding="utf-8")
        (self.src / "full.wav").write_bytes(b"RIFFfull")
        (self.src / "vocal.wav").write_bytes(b"RIFFvocal")
        (self.src / "raw.wav").write_bytes(b"RIFFraw")

    def test_copies_only_the_keep_list(self):
        out = Path(self.tmp.name) / "song_final"
        meta = fz.freeze(self.src, out, note="听起来最松")
        self.assertTrue((out / "full.wav").is_file())
        self.assertTrue((out / "vocal.wav").is_file())
        self.assertTrue((out / "report.json").is_file())
        self.assertFalse((out / "raw.wav").exists())
        self.assertEqual(set(meta["files"]), {"full.wav", "vocal.wav",
                                              "report.json"})

    def test_manifest_keeps_settings_and_hash(self):
        out = Path(self.tmp.name) / "song_final"
        meta = fz.freeze(self.src, out)
        self.assertEqual(meta["settings"]["tonic"], 60)
        self.assertEqual(meta["settings"]["vibrato"], 18.0)
        self.assertEqual(len(meta["files"]["full.wav"]["sha256_16"]), 16)
        saved = json.loads((out / "SETTINGS.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], "melody_v12")

    def test_command_is_rebuildable_from_the_report(self):
        meta = fz.freeze(self.src, Path(self.tmp.name) / "out")
        cmd = meta["command"]
        for bit in ("--native-key", "--n-steps 64", "--reverb 0.14",
                    "--vibrato 18.0", "--rate-scale 0.78", "--emotion soft",
                    "--legato 0.55", "--scoop 25.0", "--breath 0.08",
                    "--air 0.05", "--backing-db -7.0"):
            self.assertIn(bit, cmd)

    def test_readme_mentions_the_melody(self):
        out = Path(self.tmp.name) / "song_final"
        fz.freeze(self.src, out, note="听起来最松")
        text = (out / "README.md").read_text(encoding="utf-8")
        self.assertIn("等你回家", text)
        self.assertIn("1 2 3 5 6 5 4 3 2", text)
        self.assertIn("听起来最松", text)

    def test_refuses_a_dir_without_report(self):
        bad = Path(self.tmp.name) / "empty"
        bad.mkdir()
        with self.assertRaises(SystemExit):
            fz.freeze(bad, Path(self.tmp.name) / "x")


if __name__ == "__main__":
    unittest.main()
