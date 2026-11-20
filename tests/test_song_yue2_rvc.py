"""Pure-helper tests for the YuE2 -> RVC SingService renderer.

No model, no GPU, no audio deps: importing the module must work in the
stdlib test env and in CI.  The render itself is covered by the manual E2E
report (<MUSIC_ROOT>/work/longform/e2e_sing/E2E_REPORT.md).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from soulx.song_yue2_rvc import (
    STYLES,
    build_request,
    parse_args,
    read_lyrics,
    song_id_for,
)

LINES = ["天亮了窗帘慢慢拉开", "牛奶热好放在桌子上", "今天也要一起加油呀", "早安呀我的主人哥哥"]


class SongYue2RvcHelperTests(unittest.TestCase):
    def test_read_lyrics_returns_title_and_four_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "lyrics.json"
            src.write_text(
                json.dumps({"title": "早安", "phrases": [{"text": x} for x in LINES]},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            title, lines = read_lyrics(src)
        self.assertEqual(title, "早安")
        self.assertEqual(lines, LINES)

    def test_read_lyrics_defaults_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "lyrics.json"
            src.write_text(json.dumps({"phrases": [{"text": x} for x in LINES]},
                                      ensure_ascii=False), encoding="utf-8")
            title, _ = read_lyrics(src)
        self.assertEqual(title, "点歌")

    def test_read_lyrics_rejects_wrong_line_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "lyrics.json"
            src.write_text(json.dumps({"phrases": [{"text": x} for x in LINES[:3]]},
                                      ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(SystemExit):
                read_lyrics(src)

    def test_read_lyrics_prefers_lyrics_key_and_keeps_tags(self) -> None:
        full = ["[Verse]", LINES[0], LINES[1], "[Chorus]", LINES[2], LINES[3]]
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "lyrics.json"
            src.write_text(
                json.dumps({"title": "点歌", "lyrics": full}, ensure_ascii=False),
                encoding="utf-8",
            )
            title, lines = read_lyrics(src)
        self.assertEqual(title, "点歌")
        self.assertEqual(lines, full)

    def test_read_lyrics_rejects_too_many_sung_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "lyrics.json"
            src.write_text(
                json.dumps({"lyrics": [f"第{n}句在唱歌呀" for n in range(17)]},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                read_lyrics(src)

    def test_read_lyrics_does_not_count_section_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "lyrics.json"
            src.write_text(
                json.dumps({"lyrics": ["[Verse]", "[Chorus]", "[Bridge]"]},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                read_lyrics(src)

    def test_build_request_uses_emotion_style_and_verse(self) -> None:
        req = build_request("genki", LINES, song_id="job1", seed=7)
        self.assertEqual(req["id"], "job1")
        self.assertEqual(req["style"], STYLES["genki"])
        self.assertEqual(req["cot"], "full")
        self.assertEqual(req["seed"], 7)
        self.assertEqual(req["lyrics"], "[Verse]\n" + "\n".join(LINES))

    def test_build_request_keeps_existing_section_tags(self) -> None:
        full = ["[Verse]", LINES[0], LINES[1], "[Chorus]", LINES[2], LINES[3]]
        req = build_request("soft", full, song_id="job2", seed=1)
        self.assertEqual(req["lyrics"], "\n".join(full))

    def test_all_emotions_have_a_style(self) -> None:
        for emotion in ("soft", "genki", "sad"):
            self.assertIn("young female vocal", STYLES[emotion])

    def test_parse_args_accepts_sing_service_flags(self) -> None:
        args = parse_args([
            "--emotion", "soft", "--out", "job", "--src", "lyrics.json",
            "--semitones", "0", "--prompt-wav", "p.wav", "--prompt-meta", "p.json",
        ])
        self.assertEqual(args.emotion, "soft")
        self.assertEqual(args.semitones, 0.0)
        self.assertEqual(args.index_rate, 0.60)
        self.assertEqual(args.protect, 0.50)
        self.assertEqual(args.prompt_wav, "p.wav")

    def test_song_id_is_sanitized(self) -> None:
        self.assertEqual(song_id_for(Path("a b/c!d")), "cd")
        self.assertEqual(song_id_for(Path("...")), "sing")


if __name__ == "__main__":
    unittest.main()
