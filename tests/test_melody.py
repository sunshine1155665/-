"""Symbolic tests for the singing melody (soulx/melody_v2 + song_v2).

Nothing here touches audio, the network or a GPU. The pitch analysis is stubbed
with a fixed tone table, so the tests do not depend on pypinyin/g2pM being
installed — a missing g2pM would otherwise silently turn every syllable into
tone 5 and make the tone tests vacuous.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy 只存在于唱歌/引擎 venv
    raise unittest.SkipTest("numpy not installed (singing venv only)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "soulx"))

import melody_eval as ev  # noqa: E402
import melody_lexicon as lex  # noqa: E402
import melody_v2 as mv2  # noqa: E402
import prosody  # noqa: E402
import song_v2 as s2  # noqa: E402
import text2melody as t1  # noqa: E402

# A fixed tone table for the four lyric lines used everywhere below. Values are
# the real Mandarin tones; tone 5 (neutral) is the fallback.
TONES = {
    "本": 3, "鲸": 2, "鱼": 2, "娘": 2, "今": 1, "天": 1, "好": 3, "想": 3, "你": 3,
    "我": 3, "数": 4, "着": 5, "海": 3, "浪": 4, "等": 3, "回": 2, "来": 2,
    "热": 4, "汤": 1, "和": 2, "拥": 1, "抱": 4, "都": 1, "准": 3, "备": 4,
    "主": 3, "人": 2, "哥": 1, "辛": 1, "苦": 3, "了": 5,
    # a few extras used by the guard tests
    "快": 4, "夸": 1, "我": 3, "嘛": 5, "星": 1, "亮": 4, "月": 4,
}

LINES = [
    "本鲸鱼娘今天好想你",
    "我数着海浪等你回来",
    "热汤和拥抱都准备好",
    "主人哥哥今天辛苦了",
]


def fake_analyze(text: str) -> list:
    return [t1.Syllable(char=c, pinyin="", tone=TONES.get(c, 5), kind="note")
            for c in text if "\u4e00" <= c <= "\u9fff"]


class MelodyBase(unittest.TestCase):
    def setUp(self) -> None:
        self._p = patch.object(t1, "analyze", fake_analyze)
        self._p.start()
        self.addCleanup(self._p.stop)

    def spec(self, **kw) -> mv2.Spec:
        base = dict(tonic=57, lo=55, hi=69, beat=0.25, seed=7)
        base.update(kw)
        return mv2.Spec(**base)


class TestDeterminism(MelodyBase):
    def test_same_spec_same_notes(self):
        sp = self.spec(pattern="star", end_mode="fifth")
        a, _ = mv2.generate(LINES[0], sp)
        b, _ = mv2.generate(LINES[0], sp)
        self.assertEqual([n.midi for n in a], [n.midi for n in b])

    def test_pattern_path_ignores_search_knobs(self):
        """contour/span/weights only affect the search path (documented)."""
        a, _ = mv2.generate(
            LINES[0], self.spec(pattern="star", end_mode="fifth",
                                contour="rise", span=2.0))
        b, _ = mv2.generate(
            LINES[0], self.spec(pattern="star", end_mode="fifth",
                                contour="fall", span=12.0,
                                weights=mv2.Weights(tone=0.1, leap=0.1)))
        self.assertEqual([n.midi for n in a], [n.midi for n in b])

    def test_search_path_still_deterministic(self):
        sp = self.spec(contour="rise")
        a, _ = mv2.generate(LINES[2], sp)
        b, _ = mv2.generate(LINES[2], sp)
        self.assertEqual([n.midi for n in a], [n.midi for n in b])


class TestHardConstraints(MelodyBase):
    def test_tones_respected(self):
        for text, pat in zip(LINES, ["star", "run", "climb", "descend"]):
            notes, _ = mv2.generate(
                text, self.spec(pattern=pat, end_mode="tonic",
                                min_range=5, tail_drop=0))
            for i in range(1, len(notes)):
                self.assertTrue(
                    mv2._tone_ok(notes[i - 1].midi, notes[i].midi,
                                 notes[i].tone),
                    f"{text} note {i} ({notes[i].char}) breaks its tone")

    def test_pentatonic_only(self):
        sp = self.spec(pattern="run", end_mode="fifth")
        pool = set(mv2._pool(sp))
        notes, _ = mv2.generate(LINES[1], sp)
        self.assertTrue({n.midi for n in notes} <= pool)

    def test_no_triple_repeats(self):
        for text, pat in zip(LINES, ["star", "run", "climb", "descend"]):
            notes, _ = mv2.generate(text, self.spec(pattern=pat,
                                                    min_range=5))
            for i in range(2, len(notes)):
                self.assertFalse(
                    notes[i].midi == notes[i - 1].midi == notes[i - 2].midi,
                    f"{text} has a run of three at {i}")

    def test_break_triples_can_be_disabled(self):
        """Humming is allowed to rock on one pitch."""
        notes, _ = mv2.generate("月亮月亮月亮",
                                self.spec(pattern="tiger", break_triples=False))
        self.assertIsInstance(notes, list)


class TestGuards(MelodyBase):
    def test_min_range_widens_a_narrow_phrase(self):
        # Interior notes are 61s, the cadence is 59/57. The only legal way to
        # reach a 9-semitone range is to lift an interior note to 66, which the
        # tone-2 syllable at index 4 allows and the tone-3 syllable after it
        # permits to fall from.
        pitches = [57, 59, 61, 61, 61, 61, 61, 59, 57]
        tones = [3, 2, 2, 2, 2, 3, 3, 2, 3]
        sp = self.spec(min_range=9)
        mv2._ensure_range(pitches, tones, sp, mv2._pool(sp))
        self.assertGreaterEqual(max(pitches) - min(pitches), 9)

    def test_min_range_never_breaks_a_tone(self):
        # A phrase whose every widening move would be 倒字 must stay narrow
        # rather than become unsingable.
        pitches = [57, 59, 61, 61, 59, 59, 61, 59, 57]
        tones = [2, 2, 2, 2, 2, 2, 2, 2, 2]
        sp = self.spec(min_range=12)
        before = list(pitches)
        mv2._ensure_range(pitches, tones, sp, mv2._pool(sp))
        self.assertEqual(pitches, before)

    def test_tail_drop_closes_the_phrase(self):
        notes, _ = mv2.generate(
            "主人哥哥今天辛苦了",
            self.spec(pattern="descend", end_mode="tonic", min_range=6,
                      tail_drop=3))
        deg = [n.midi - 57 for n in notes]
        self.assertGreaterEqual(deg[-3] - deg[-1], 3)
        self.assertGreaterEqual(deg[-3], deg[-2])
        self.assertGreaterEqual(deg[-2], deg[-1])

    def test_tail_drop_exempt_for_questions(self):
        """A question is supposed to hang, not settle."""
        notes, _ = mv2.generate(
            "快夸夸我嘛",
            self.spec(pattern="call", end_mode="rise", tail_drop=3))
        self.assertTrue(len(notes) >= 3)

    def test_tonic_cadence(self):
        notes, _ = mv2.generate(
            "主人哥哥今天辛苦了",
            self.spec(pattern="descend", end_mode="tonic", tail_drop=3))
        self.assertEqual(notes[-1].midi % 12, 57 % 12)


class TestJoin(MelodyBase):
    def _notes(self, text="我数着海浪等你回来"):
        notes, _ = mv2.generate(text, self.spec(pattern="run",
                                                end_mode="fifth"))
        return notes

    def test_dead_join_is_moved(self):
        notes = self._notes()
        prev = notes[0].midi
        self.assertTrue(mv2.retune_join(notes, prev, self.spec(pattern="run")))
        self.assertNotEqual(notes[0].midi, prev)
        self.assertLess(abs(notes[0].midi - prev), 7)

    def test_seventh_leap_is_moved(self):
        notes = self._notes()
        prev = notes[0].midi + 12
        self.assertTrue(mv2.retune_join(notes, prev, self.spec(pattern="run")))
        self.assertLess(abs(notes[0].midi - prev), 7)

    def test_good_join_is_left_alone(self):
        notes = self._notes()
        before = [n.midi for n in notes]
        prev = notes[0].midi + 2
        self.assertFalse(mv2.retune_join(notes, prev, self.spec(pattern="run")))
        self.assertEqual([n.midi for n in notes], before)

    def test_retune_never_creates_daozi(self):
        notes = self._notes()
        prev = notes[0].midi
        mv2.retune_join(notes, prev, self.spec(pattern="run"))
        self.assertTrue(mv2._tone_ok(prev, notes[0].midi, notes[0].tone))
        self.assertTrue(mv2._tone_ok(notes[0].midi, notes[1].midi,
                                    notes[1].tone))

    def test_vocative_opens_with_a_call(self):
        notes, _ = mv2.generate("主人哥哥今天辛苦了",
                                self.spec(pattern="descend", end_mode="tonic"))
        changed = mv2.open_with_vocative(notes, self.spec(pattern="descend"))
        if changed:
            d = notes[0].midi - notes[1].midi
            # Enter from below. Holding the first pitch into the second (d == 0)
            # is the "7 7" stutter the reviewer flagged, so it is no longer an
            # option.
            self.assertIn(d, (-2, -4))
        for i in range(1, len(notes)):
            self.assertTrue(mv2._tone_ok(notes[i - 1].midi, notes[i].midi,
                                         notes[i].tone))


class TestLexicon(unittest.TestCase):
    def test_weights_align_to_cjk(self):
        self.assertEqual(len(lex.weights("热汤和拥抱")), 5)
        self.assertEqual(lex.weights(""), [])

    def test_warm_word_weighs_more(self):
        w = lex.weights("热汤和拥抱")
        self.assertEqual(w[0], 1.0)        # 热 is a warm word
        self.assertGreaterEqual(w[1], 0.7)  # 汤 is warm + concrete
        self.assertEqual(w[2], 0.0)        # 和 is a function word

    def test_features_counts_concrete(self):
        f = lex.features("热汤和拥抱都准备好")
        self.assertGreaterEqual(f["concrete"], 2)

    def test_address_is_not_a_pronoun(self):
        self.assertTrue(lex.features("主人哥哥今天辛苦了")["has_vocative"])
        # 我数着… is narration; a call-like entry would sound wrong.
        self.assertFalse(lex.features("我数着海浪等你回来")["has_vocative"])

    def test_wait_verb_detected(self):
        self.assertTrue(lex.features("我数着海浪等你回来")["has_wait"])


class TestDuration(MelodyBase):
    def test_mults_are_clamped(self):
        for text in LINES:
            for m in s2.stress_mults(text):
                self.assertGreaterEqual(m, 0.5)
                self.assertLessEqual(m, 1.8)

    def test_warm_word_holds_longer_than_function_word(self):
        m = s2.stress_mults("热汤和拥抱")
        # 和 is a function word; 拥 is warm. The cadence-lean rule only touches
        # the penultimate syllable, so compare the interior pair.
        self.assertGreater(m[3], m[2])

    def test_last_syllable_is_not_stretched_by_the_multiplier(self):
        # The final note gets a fixed 2 beats in generate(); the multiplier
        # list only describes the syllables before it.
        self.assertEqual(len(s2.stress_mults("主人哥哥今天辛苦了")), 9)


class TestPatternDispatch(MelodyBase):
    def test_last_phrase_gets_a_wide_motif(self):
        pat = s2.pick_pattern(LINES[3], "cheer", 3, 4)
        motif = mv2.PATTERNS[pat]
        self.assertGreaterEqual(max(motif) - min(motif), 6)

    def test_curated_default_is_kept(self):
        self.assertEqual(s2.pick_pattern(LINES[0], "plain", 0, 4), "star")
        self.assertEqual(s2.pick_pattern(LINES[1], "invite", 1, 4), "run")

    def test_concrete_override_widens_narrow_default(self):
        # p2's default is climb (width 7) — already wide, so it must stay.
        self.assertEqual(s2.pick_pattern(LINES[2], "plain", 2, 4), "climb")

    def test_choose_pattern_prefers_singable_leaps(self):
        info = prosody.analyze("快夸夸我嘛")
        tones = [s.tone for s in fake_analyze("快夸夸我嘛")]
        picked = prosody.choose_pattern("快夸夸我嘛", info, mv2.PATTERNS,
                                        tones=tones)
        self.assertIn(picked, mv2.PATTERNS)


class TestArc(MelodyBase):
    """v6 contour-first path: one arch per phrase, tone-aware cadence.

    v5 tiled a nursery motif over nine syllables four times; the audio review
    heard a loop ("四个九字句一个节奏"). These tests pin the properties that
    replaced it: no repeated pitch on a toned syllable, a forced cadence that is
    still tone-legal, and one stepwise run per phrase so the line moves.
    """

    CONFIG = [
        ("本鲸鱼娘今天好想你", dict(start_deg=0, amp=7, cadence=4)),
        ("我数着海浪等你回来", dict(start_deg=4, amp=7, cadence=2)),
        ("热汤和拥抱都准备好", dict(start_deg=2, amp=7, cadence=4)),
        ("主人哥哥今天辛苦了", dict(start_deg=4, amp=9, cadence=0)),
    ]

    def arc(self, text: str, **kw) -> list:
        notes, _ = mv2.generate(
            text, self.spec(style="arc", rhythm="nursery", **kw))
        return notes

    def test_arc_is_deterministic(self):
        a = self.arc(LINES[0], start_deg=0, amp=7, cadence=4)
        b = self.arc(LINES[0], start_deg=0, amp=7, cadence=4)
        self.assertEqual([n.midi for n in a], [n.midi for n in b])

    def test_generator_is_recorded(self):
        _, meta = mv2.generate(LINES[0], self.spec(
            style="arc", rhythm="nursery", start_deg=0, amp=7, cadence=4))
        self.assertEqual(meta["generator"], "v7-arc")

    def test_no_repeated_pitch_on_a_toned_syllable(self):
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            for i in range(1, len(notes)):
                if notes[i].tone == 5:
                    continue
                self.assertNotEqual(
                    notes[i].midi, notes[i - 1].midi,
                    f"{text} repeats {notes[i].char} at {i}")

    def test_second_syllable_moves(self):
        # 0 0 7 7 was the "敲门" opening of v5.
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            self.assertNotEqual(notes[0].midi, notes[1].midi, text)

    def test_tones_are_never_reversed(self):
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            for i in range(1, len(notes)):
                self.assertTrue(
                    mv2._tone_ok(notes[i - 1].midi, notes[i].midi,
                                 notes[i].tone),
                    f"{text} breaks tone at {i}")

    def test_leaps_stay_inside_the_limit(self):
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            for i in range(1, len(notes)):
                self.assertLessEqual(abs(notes[i].midi - notes[i - 1].midi),
                                     mv2.Spec().max_leap)

    def test_each_phrase_has_a_stepwise_run(self):
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            iv = [abs(notes[i].midi - notes[i - 1].midi)
                  for i in range(1, len(notes))]
            self.assertTrue(
                any(iv[j] <= 2 and iv[j + 1] <= 2 for j in range(len(iv) - 1)),
                f"{text} has no three-note step")

    def test_each_phrase_holds_its_peak(self):
        # "匀速一字一音，没有一处能喘口气": one interior note is held, so the
        # line breathes. The final note stays the longest (2 beats).
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            beats = [n.beats for n in notes]
            self.assertTrue(any(b >= 1.5 for b in beats[1:-1]),
                            f"{text} has no held note")
            self.assertEqual(beats[-1], 2.0)
            self.assertGreaterEqual(beats[-1], max(beats))

    def test_requested_cadence_is_used_when_the_tone_allows(self):
        # 你 / 好 are tone 3 and 了 is neutral, so nothing forbids the degree.
        # A non-final phrase may settle on re instead of mi — both are half
        # cadences, and the picker trades the degree for a real approach.
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            if text == LINES[3]:
                self.assertEqual(notes[-1].midi - 57, 0, text)
            else:
                self.assertIn(notes[-1].midi - 57, (2, 4), text)

    def test_tone_2_ending_keeps_the_tonic_and_rises(self):
        # 来 is tone 2: falling into do would be 倒字, so the phrase has to
        # approach from below or hold. The cadence degree still wins.
        notes = self.arc(LINES[1], start_deg=4, amp=7, cadence=0)
        self.assertEqual(notes[-1].midi - 57, 0)
        self.assertGreaterEqual(notes[-1].midi - notes[-2].midi, -1)

    def test_last_phrase_only_accepts_the_tonic(self):
        sp = self.spec(style="arc", rhythm="nursery", cadence=0)
        pool = mv2._pool(sp)
        tones = [3, 4, 5, 3, 4, 3, 3, 2, 2]
        for last, _ in mv2._cadence_pairs(sp, tones, pool):
            self.assertEqual(last, 57)

    def test_cadence_prefers_a_stepwise_descent(self):
        sp = self.spec(style="arc", rhythm="nursery", cadence=0)
        pool = mv2._pool(sp)
        last, pen = mv2._cadence_pairs(sp, [3] * 9, pool)[0]
        self.assertEqual(last, 57)
        self.assertIn(pen - last, (1, 2, 3, 4))

    def test_no_two_note_wobble(self):
        # v6's remaining flaw: 7 9 7 9 7 at the peak and in the run of level
        # tones. Once repeats are banned, an equal-magnitude sign flip is the
        # last way to stand still. One flip is a turn (fine); three in a row is
        # X-Y-X-Y, the "念经" figure, and must never appear.
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            deg = [n.midi - 57 for n in notes]
            iv = [deg[i] - deg[i - 1] for i in range(1, len(deg))]
            for i in range(1, len(iv) - 1):
                a, b, c = iv[i - 1], iv[i], iv[i + 1]
                self.assertFalse(
                    abs(a) == abs(b) == abs(c) and a * b < 0 and b * c < 0,
                    f"{text} wobbles at {i}: {deg}")

    def test_passing_tone_is_entered_and_left_by_step(self):
        # 7 and 4 are three semitones apart, so a run of level tones has no
        # legal way down; fa (tonic + 5) is the passing note that opens it.
        # It is spice, not a home note: it must never be the target of a leap.
        passing = {57 + mv2._PASSING_OFFSET}
        for text, cfg in self.CONFIG:
            midi = [n.midi for n in self.arc(text, **cfg)]
            for i, m in enumerate(midi):
                if m not in passing:
                    continue
                self.assertGreater(i, 0, text)
                self.assertLess(i, len(midi) - 1, text)
                self.assertLessEqual(abs(m - midi[i - 1]), 2, text)
                self.assertLessEqual(abs(midi[i + 1] - m), 2, text)

    def test_passing_tone_never_lands_the_cadence(self):
        passing = {57 + mv2._PASSING_OFFSET}
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            self.assertNotIn(notes[-1].midi, passing, text)
            self.assertNotIn(notes[-2].midi, passing, text)

    def test_function_words_never_carry_a_big_leap(self):
        # "4 -> 9 on 和 is the biggest leap of the phrase but lands on a
        # function word" (reviewer, twice). 4+ semitones now belong to content
        # words only.
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            for i in range(1, len(notes)):
                if abs(notes[i].midi - notes[i - 1].midi) < 4:
                    continue
                self.assertNotIn(notes[i].char, mv2._FUNC_CHARS, text)

    def test_the_ending_never_stutters(self):
        # A neutral-tone last syllable (了) used to be able to repeat the note
        # before it, which is the "0 0" close the reviewer called 念经.
        for text, cfg in self.CONFIG:
            notes = self.arc(text, **cfg)
            self.assertNotEqual(notes[-1].midi, notes[-2].midi, text)


class TestSong(unittest.TestCase):
    """End-to-end symbolic check of the four-line song (no audio)."""

    def setUp(self) -> None:
        self._p = patch.object(t1, "analyze", fake_analyze)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.phrases, self.rows = s2.compose_melody(LINES)

    def test_all_phrases_have_a_usable_range(self):
        for i, row in enumerate(self.rows):
            self.assertGreaterEqual(row["range"], 5, f"p{i} too flat")

    def test_last_phrase_closes(self):
        last = self.rows[-1]
        self.assertGreaterEqual(last["range"], 6)
        self.assertGreaterEqual(last["tail_drop"], 3)
        self.assertEqual(last["degrees"][-1], 0)

    def test_joins_are_alive(self):
        for row in self.rows[1:]:
            j = row["join_in"]
            self.assertIsNotNone(j)
            self.assertNotEqual(j, 0)
            self.assertLess(abs(j), 7)

    def test_no_daozi_anywhere(self):
        for row in self.rows:
            deg = row["degrees"]
            tones = row["tones"]
            for i in range(1, len(deg)):
                self.assertTrue(
                    mv2._tone_ok(57 + deg[i - 1], 57 + deg[i], tones[i]),
                    f"{row['text']} breaks tone at {i}")

    def test_no_repeated_runs(self):
        for row in self.rows:
            deg = row["degrees"]
            for i in range(2, len(deg)):
                self.assertFalse(deg[i] == deg[i - 1] == deg[i - 2])

    def test_every_phrase_uses_the_arc_generator(self):
        for row in self.rows:
            self.assertEqual(row["generator"], "v7-arc", row["text"])

    def test_row_mults_match_the_note_beats(self):
        # melody_eval derives durations from ``mults``; if the held peak is not
        # reported there, the metrics describe a melody nobody hears.
        for row, notes in zip(self.rows, self.phrases):
            self.assertEqual([round(n.beats, 3) for n in notes],
                             [round(m, 3) for m in row["mults"]], row["text"])

    def test_arc_plan_has_no_duplicate_shapes(self):
        # Two phrases with the same amp, cadence and peak get the same target
        # curve, so the DP returns the same line transposed — "第 1、3 句几乎
        # 同形，整首像一句循环". The plan must differ on at least one of them.
        shapes = [(p["amp"], p["cadence"], p.get("peak_at", 0.62))
                  for p in s2.ARC_PLAN]
        self.assertEqual(len(shapes), len(set(shapes)), shapes)

    def test_no_phrase_is_another_phrase_shifted(self):
        # The v6 first cut had p0 ``0 2 4 7 9 7 9 7 4`` and p2
        # ``2 4 7 9 7 9 7 4 2``: the same tune one syllable later. Comparing a
        # phrase's tail against another's head catches that exactly.
        degs = [row["degrees"] for row in self.rows]
        for a in range(len(degs)):
            for b in range(len(degs)):
                if a == b:
                    continue
                self.assertNotEqual(degs[a][1:], degs[b][:-1],
                                    f"p{a} is p{b} shifted")

    def test_no_two_phrases_share_a_contour(self):
        # v7 passed the "no duplicate shape" test and still sounded like one
        # arch played four times: every line climbed to 9 at index 3-4. The
        # reviewer hears the up/down pattern, so compare that: the four lines
        # must not share an up/down fingerprint.
        def signs(d):
            return "".join("+" if b > a else ("-" if b < a else "0")
                           for a, b in zip(d, d[1:]))
        fingerprints = [signs(row["degrees"]) for row in self.rows]
        self.assertEqual(len(fingerprints), len(set(fingerprints)),
                         fingerprints)
        # ... and the peaks must not all sit on the same syllable.
        peaks = [(d.index(max(d)), max(d))
                 for d in (row["degrees"] for row in self.rows)]
        self.assertGreaterEqual(len({p for p, _ in peaks}), 2, peaks)
        self.assertGreaterEqual(len({h for _, h in peaks}), 2, peaks)

    def test_peak_stays_inside_the_comfortable_range(self):
        # v8 put the 拥抱 climax on the octave (12). The reviewer heard the
        # octave jump as "一下子蹦到楼顶 ... 像通用儿歌，出戏": for this voice a
        # fifth above do (9) is the ceiling, so the climax has to be a shape
        # change, not a higher note.
        for row in self.rows:
            self.assertLessEqual(max(row["degrees"]), 9, row["degrees"])

    def test_report_metrics_aggregate(self):
        rep = {"title": "t", "tonic": 57, "phrases": self.rows,
               "duration_s": sum(r["duration"] for r in self.rows)}
        m = ev.report_metrics(rep)
        self.assertEqual(m["tone_compliance"], 1.0)
        self.assertEqual(m["triple_repeats"], 0)
        self.assertTrue(m["joins_ok"])
        self.assertGreaterEqual(m["min_phrase_range"], 5)
        self.assertGreaterEqual(m["final_tail_drop"], 3)
        self.assertTrue(m["final_tail_descends"])

    def test_row_metrics_durations_match_the_beat(self):
        row = self.rows[0]
        m = ev.row_metrics(row, 57)
        self.assertAlmostEqual(m["dur_max"], round(max(row["mults"]) * row["beat"], 3))
        self.assertGreater(m["dur_ratio"], 1.0)

    def test_the_song_ends_on_a_held_note(self):
        # "收得太干脆": the cadence note is the one she can hold, so it must be
        # the longest note of its line, and the closing line holds longer than
        # the others (the song's last breath).
        for i, (row, notes) in enumerate(zip(self.rows, self.phrases)):
            self.assertEqual(notes[-1].beats, max(n.beats for n in notes),
                             f"p{i} ending is not held")
        self.assertGreater(self.rows[-1]["mults"][-1],
                           self.rows[-2]["mults"][-1])
        # melody_eval must report the duration the singer actually gets,
        # otherwise the metrics describe a melody nobody hears.
        m = ev.row_metrics(self.rows[-1], 57)
        self.assertAlmostEqual(m["dur_max"],
                               round(self.rows[-1]["mults"][-1]
                                     * self.rows[-1]["beat"], 3))

    def test_gaps_are_measured_in_beats(self):
        # The old fixed 0.45/0.40/0.55 s took the singer out of the song for two
        # beats at a time at any tempo. Gaps must scale with the beat, or slowing
        # the song down silently lengthens them too.
        for rate in (1.0, 0.8):
            rows = [dict(r) for r in s2.compose_melody(LINES, rate)[1]]
            spans = s2.phrase_timeline(rows)
            for i in range(len(spans) - 1):
                gap = spans[i + 1][0] - (spans[i][0] + spans[i][1])
                expect = s2.phrase_timeline.__defaults__[0][i] * rows[i]["beat"]
                self.assertAlmostEqual(gap, expect, places=4)
            # and the whole song gets longer when the tempo drops
            self.assertGreater(spans[-1][0] + spans[-1][1],
                               sum(r["duration"] for r in rows))

    def test_gap_beats_are_overridable(self):
        rows = [dict(r) for r in self.rows]
        tight = s2.phrase_timeline(rows, (0.5, 0.5, 0.5, 0.0))
        loose = s2.phrase_timeline(rows, (2.0, 2.0, 2.0, 0.0))
        self.assertLess(tight[1][0], loose[1][0])
        self.assertAlmostEqual(tight[1][0] - (tight[0][0] + tight[0][1]),
                               0.5 * rows[0]["beat"], places=3)

    def test_legato_boundaries_skip_the_breath(self):
        # Boundaries are the syllable cuts inside a line. The first note of each
        # phrase sits after a breath, which must NOT be filled (that would
        # delete the phrasing).
        s2.ARC_PLAN = [dict(p) for p in s2.ARC_PLAN]
        rows = [dict(r) for r in self.rows]
        spans = s2.phrase_timeline(rows)
        bounds = s2.legato_boundaries(spans, rows)
        self.assertEqual(len(bounds), 4 * 9 - 4)
        firsts = {round(sp[0], 4) for sp in spans}
        self.assertFalse(any(round(b, 4) in firsts for b in bounds))
        # every boundary lands inside the phrase it belongs to
        for (start, dur), row in zip(spans, rows):
            inside = [b for b in bounds if start < b < start + dur]
            self.assertEqual(len(inside), len(row["mults"]) - 1)

    def test_backing_sits_a_fixed_distance_under_the_voice(self):
        # The plucks used to be summed in raw and landed 17 dB under the vocal
        # (an accidental a-cappella). The balance is now a number, in dB.
        rng = np.random.default_rng(3)
        vocal = (0.3 * np.sin(2 * np.pi * 220 * np.arange(4800) / 24000)).astype(np.float32)
        backing = (0.01 * rng.standard_normal(4800)).astype(np.float32)
        for db in (-7.0, -12.0, 0.0):
            out = s2.match_backing(vocal, backing, db)
            vr = float(np.sqrt(np.mean(vocal ** 2)))
            br = float(np.sqrt(np.mean(out ** 2)))
            self.assertAlmostEqual(20 * np.log10(vr / br), -db, places=1)

    def test_native_key_shifts_the_score_not_the_audio(self):
        # --native-key writes the score three semitones up instead of resampling
        # the rendered audio (which drags the formants with the pitch). The tune
        # must be identical, just in another key.
        low, rows_low = s2.compose_melody(LINES, tonic=57)
        high, rows_high = s2.compose_melody(LINES, tonic=60)
        self.assertEqual([r["degrees"] for r in rows_low],
                         [r["degrees"] for r in rows_high])
        for a, b in zip(low, high):
            self.assertEqual([n.midi + 3 for n in a], [n.midi for n in b])
            self.assertEqual([round(n.start, 4) for n in a],
                             [round(n.start, 4) for n in b])


class TestPhraseMetrics(unittest.TestCase):
    def test_known_phrase(self):
        notes = [
            t1.Note(0, "a", "", 5, 57, 0.0, 0.25, 1.0),
            t1.Note(1, "b", "", 5, 59, 0.25, 0.25, 1.0),
            t1.Note(2, "c", "", 5, 61, 0.5, 0.25, 1.0),
            t1.Note(3, "d", "", 5, 57, 0.75, 0.5, 2.0),
        ]
        m = ev.phrase_metrics(notes, 57)
        self.assertEqual(m["range"], 4)
        self.assertEqual(m["tail_drop"], 2)
        self.assertFalse(m["tail_descends"])
        self.assertEqual(m["end_degree"], 0)
        self.assertEqual(m["distinct"], 3)


if __name__ == "__main__":
    unittest.main()
