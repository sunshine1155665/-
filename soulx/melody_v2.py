"""Melody generator v2: rhythm templates + motif development.

Why a new module instead of editing ``scripts/text2melody.py``: that script is
the working production path (it feeds ``resing_world`` and the SVC/SVS demos).
v2 has to be measured against it before anything replaces it.

The three defects v1 showed, and what v2 does about them:

1. **Every note is one beat.** ``beats = 1.0`` for every syllable but the last,
   so the tune has no rhythm at all — it reads as chanting. v2 takes a rhythm
   template: a repeating cycle of beat multipliers (dotted, swung, short-long,
   pickup...).

2. **Every note is drawn fresh.** With the tone rule weighted at -6.0 and
   "same as previous" only -1.8, the search collapses onto two or three pitches
   and often repeats the same one four times. v2 states a short *motif* up
   front (2-4 notes) and then develops it (repeat, transpose by a scale step),
   so the phrase has a shape a listener can remember.

3. **The contour barely matters.** Its weight is 0.5 against the tone rule's
   6.0, so ``span`` had almost no effect. v2 exposes all weights in one
   ``Weights`` object so the flywheel can tune them against an external judge.

Constraints that stay hard: pentatonic only, inside ``[lo, hi]``, and the
Mandarin tone direction rules (a tone-2 syllable must not fall, tone-4 must not
rise) because violating them produces 倒字 — the syllable stops being
intelligible.

Design decision (2026-10-25): ``Spec.pattern`` is a deterministic tiling path.
``contour`` / ``span`` / ``weights`` only affect the search path; with a pattern
set they are intentionally ignored. Shaping a tiled phrase is done through the
post-generation guards (``min_range`` / ``tail_drop``), not by pretending the
search weights still apply.
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import text2melody as t1  # noqa: E402  (reuse analyze/PENTATONIC/TONE_RANGE)

Note = t1.Note
CONTOURS = t1.CONTOURS
TONE_RANGE = t1.TONE_RANGE

# ---------------------------------------------------------------------------
# rhythm templates: multipliers applied to the base beat, cycled per syllable.
# The last syllable is always stretched separately (a phrase needs a landing).
RHYTHMS: dict[str, list[float]] = {
    "even":    [1.0],
    "dotted":  [1.5, 0.5],
    "swing":   [0.75, 0.5, 1.25],
    "shortlong": [0.5, 1.5],
    "pickup":  [0.5, 1.0, 1.0, 1.5],
    "bounce":  [0.5, 1.0, 0.5, 1.0, 1.0],
    "longshort": [1.5, 1.0, 0.5],
    "lilt":    [1.0, 0.5, 1.0, 1.5],
    # Nursery-rhyme phrasing: short pickup, even quarters, longer cadence.
    # 小星星 is 1155|665-  and 丢手绢 is 丢手绢|丢手绢 with a dotted tail.
    "nursery": [0.5, 1.0, 1.0, 1.5],
}


# Scale-degree offsets over the pentatonic scale (major: 0 2 4 7 9).
# The shapes are lifted from the nursery canon, which is why they are singable
# at first hearing: a wide outline filled with steps, and a cadence on 0.
PATTERNS: dict[str, list[int]] = {
    "star":  [0, 0, 7, 7, 9, 9, 7],        # 小星星 1 1 5 5 6 6 5
    "tiger": [0, 2, 4, 0],                 # 两只老虎 1 2 3 1
    "arc":   [0, 4, 7, 4],                 # do-mi-sol-mi
    "rock":  [0, 7, 0, 7],                 # 五度来回，最容易跟
    "climb": [0, 2, 4, 7, 4, 2],           # 上四音再折回
    "swing": [0, 4, 2, 4],                 # 小摆动
    "call":  [0, 7, 4, 2],                 # 先喊后收
    # Added because a four-phrase song that uses "star" three times reads as one
    # phrase repeated. These give each phrase its own silhouette.
    "step":  [0, 2, 4, 2],                 # 级进往返，最"说话"
    "descend": [9, 7, 4, 2, 0],            # 一路下行，适合收句
    "hover": [4, 2, 4, 2, 4],              # 同区环绕，适合撒娇
    "run":   [0, 2, 4, 7, 9, 7],           # 上行跑句，最"元气"
    "echo":  [0, 4, 0, 2, 0],              # 回主音的呼应
    "swell": [2, 4, 7, 9, 7, 4],           # 拱起再落
}


@dataclass
class Weights:
    """Scoring weights. The flywheel tunes these; defaults are a sane start."""

    tone: float = 6.0        # anti-倒字, dominant
    leap: float = 5.0        # penalty beyond max_leap
    repeat: float = 3.6      # same-as-previous penalty (v1 had 1.8)
    contour: float = 2.2     # contour pull (v1 had 0.5)
    chord: float = 2.2       # chord-tone bonus (nursery melodies sit on do-mi-sol)
    motif: float = 2.6       # pull toward the motif's interval shape
    degree: float = 1.2      # pull toward tonic/third/fifth
    jitter: float = 1.2      # deterministic randomness per seed


@dataclass
class Spec:
    tonic: int = 62
    scale: str = "major"
    lo: int = 62
    hi: int = 76
    beat: float = 0.36
    contour: str = "rise"
    span: float = 8.0
    rhythm: str = "dotted"
    motif_len: int = 3
    max_leap: int = 7
    end_rise: bool = True
    # "tonic" = land the last note on do/mi/sol, descending preferred and never
    # on the top two degrees of the range (that is what made genki's tail feel
    # piercing). "rise" = old behaviour. "free" = whatever scores best.
    end_mode: str = "tonic"
    # Exact motif repeat (两只老虎 is literally 1231|1231). Different from the
    # default, which transposes the motif each cycle and costs memorability.
    motif_repeat: bool = False
    # If set (key of PATTERNS), the melody is tiled from that motif instead of
    # being searched note by note. Deterministic, and it is what actually sounds
    # like a nursery rhyme.
    pattern: str | None = None
    # Rotate the motif by N positions. Every phrase otherwise opens on the tonic
    # (1 1 2 3 ...), which is why four lines in a row sound like four separate
    # starts instead of one tune. Rotation keeps the same notes and key but
    # enters the figure from a different point.
    rotate: int = 0
    # Per-note duration multipliers, overriding the rhythm template. This is how
    # word-level stress gets in: 的/了/呀/我/你 are short, content words are long,
    # reduplicated syllables are two even beats. A fixed template cannot know any
    # of that, which is why every line had the same gait.
    duration_mults: list[float] | None = None
    # 0 disables the "no more than N steps in one direction" rule. Humming and
    # lullabies *are* two-note rocking figures, so they switch it off.
    run_limit: int = 3
    # Multiplier on the same-pitch penalty. <1 tolerates repeats, which is what
    # makes a line easy to hum along to.
    repeat_scale: float = 1.0
    # Break runs of three identical pitches. Off for humming: repetition there
    # is the point, not a defect.
    break_triples: bool = True
    # --- post-generation musical guards -----------------------------------
    # The pattern path is deterministic tiling, so the motif is fixed before any
    # of these run. They are the only knobs that can still shape a phrase after
    # the motif has been chosen.
    # Minimum phrase range in semitones. ``echo`` tiles to 4, which is why the
    # closing line had no memorable lift; a song phrase needs at least 5.
    min_range: int = 0
    # Minimum descent over the last three notes. This is the cadence the mimo
    # review asked for (soft/comfort lines should "put the phrase down", not
    # hover). 0 disables it.
    tail_drop: int = 0
    # --- contour-first ("arc") path ---------------------------------------
    # "pattern" = legacy tiled nursery motif. "arc" = one phrase-level arch
    # solved by DP: stepwise in the middle, leaps only near the edges, cadence
    # forced. This is the answer to "不好听 / 不自然": tiling a 7-note motif
    # over 9 syllables replays the same loop four times, and the ear hears a
    # loop, not a song.
    style: str = "pattern"
    # Final note offset from the tonic: 0 = do (settle), 4 = mi, 2 = re
    # (half cadence). Ending every phrase on 7 (sol) is what made the joins
    # sound like four separate starts.
    cadence: int = 0
    start_deg: int = 0        # first note offset from the tonic
    amp: float = 7.0          # arch height in semitones
    peak_at: float = 0.62     # where the arch peaks, as a fraction of the line
    # Length of the final note, in beats. The cadence is where she holds the
    # note and lets it fade ("句尾气息 / 撒娇的拖音"): a 2-beat ending next to
    # 0.7-beat syllables still read as "收得太干脆". Song-level code raises it
    # for the closing line.
    final_beats: float = 2.0
    # First phrase's interval shape, offered to later phrases as a soft bonus.
    # Motif recall is the cheap way to get a "hook" without tiling.
    head: list[int] | None = None
    # NOTE (2026-10-25): a per-phrase "-1 semitone declination" was tried and
    # rejected. Shifting a pentatonic phrase by a semitone puts every note
    # off-scale (A minor pentatonic 57/59/61/64/66 -> 56/58/60/63/65), and
    # shifting by a scale step changes the cadence's pitch class. Later phrases
    # enter lower through motif rotation and the join rule instead.
    seed: int = 7
    weights: Weights = field(default_factory=Weights)


def _pool(spec: Spec) -> list[int]:
    offsets = t1.PENTATONIC[spec.scale]
    out: list[int] = []
    for octave in range(-2, 3):
        for off in offsets:
            p = spec.tonic + off + 12 * octave
            if spec.lo <= p <= spec.hi:
                out.append(p)
    return sorted(set(out))


def _chord_pcs(tonic: int, bar: int) -> set[int]:
    return t1.chord_pcs(tonic, bar)


def generate(text: str, spec: Spec | None = None) -> tuple[list[Note], dict]:
    spec = spec or Spec()
    rng = random.Random(spec.seed)
    syllables = t1.analyze(text)
    note_syll = [s for s in syllables if s.kind == "note"]
    if not note_syll:
        raise SystemExit("no singable characters")

    pool = _pool(spec)
    if len(pool) < 4:
        raise SystemExit(f"range [{spec.lo},{spec.hi}] too narrow: {pool}")

    curve = CONTOURS[spec.contour]
    total = len(note_syll)
    # Register offset per seed: without it every line starts on the same pitch,
    # because the contour term pins note 0 to base + (curve(0)-0.5)*span.
    centre = len(pool) // 2 + (spec.seed % 3) - 1
    centre = max(1, min(len(pool) - 2, centre))
    base = pool[centre]
    mult = RHYTHMS[spec.rhythm]
    w = spec.weights
    scale_steps = [0, 2, -2, 4, -4, 2, 0, -2]

    def allowed(prev: int | None, tone: int) -> list[int]:
        """Tone direction as a HARD filter, not a weight.

        v1 made 倒字 a -6.0 penalty, which dominated every other term and
        collapsed the melody onto two pitches. Filtering first keeps the
        syllable intelligible while letting shape terms actually matter.
        """
        if prev is None:
            return pool
        lo_d, hi_d = TONE_RANGE.get(tone, (-2, 2))
        # +-1 semitone of slack: the tone rule is about direction, not exact size.
        ok = [p for p in pool if lo_d - 1 <= (p - prev) <= hi_d + 1]
        return ok or pool

    def score(cand: int, prev: int | None, prev2: int | None, prev3: int | None,
              target: float, bar: int, motif_delta: int | None, low_bias: bool,
              leapt_recently: bool, run_dir: int = 0, run_len: int = 0,
              tone: int = 0) -> float:
        s = 0.0
        delta = 0 if prev is None else cand - prev
        ad = abs(delta)
        if prev is not None:
            if ad > spec.max_leap:
                s -= w.leap + 2.0 * (ad - spec.max_leap)
            elif 3 <= ad <= spec.max_leap:
                # Jumps are what makes a line "灵动"; reward one, then stop.
                bonus = 1.8 if not leapt_recently else 0.2
                # Mandarin only *permits* a big interval on a rising (tone 2) or
                # falling (tone 4) syllable, so those are where leaps must be
                # placed. Without this bias every line comes out as a scale run —
                # which is exactly what the reviewer kept complaining about.
                if tone == 2 and delta > 0:
                    bonus += 2.4
                elif tone == 4 and delta < 0:
                    bonus += 2.4
                s += bonus
            elif 1 <= ad <= 2:
                s += 0.3
            if cand == prev:
                s -= w.repeat * spec.repeat_scale
                if cand == prev2:
                    s -= w.repeat * 1.2 * spec.repeat_scale
                    if cand == prev3 and spec.break_triples:
                        s -= 12.0          # three in a row is a bug, not a choice
            # A run of steps in one direction is a scale exercise, not a tune.
            # The reviewer put it best: "像在按音阶往上爬 / 像升级音效".
            if (spec.run_limit > 0 and delta != 0
                    and (delta > 0) == (run_dir > 0) and run_dir != 0):
                nxt = run_len + 1
                if nxt >= spec.run_limit:
                    s -= 3.2 * (nxt - spec.run_limit + 1)
        if cand % 12 in _chord_pcs(spec.tonic, bar):
            s += w.chord
        s -= w.contour * abs((cand - base) - target)
        if motif_delta is not None and prev is not None:
            s -= w.motif * abs(delta - motif_delta)
        tonic_pc = spec.tonic % 12
        if cand % 12 == tonic_pc:
            s += w.degree
        elif (cand - spec.tonic) % 12 in (4, 7):
            s += w.degree * 0.5
        if low_bias:
            s -= 0.25 * (cand - pool[0])
        s += rng.uniform(0.0, w.jitter)
        return s

    def snap(desired: int, prev: int | None, tone: int) -> int:
        cands = allowed(prev, tone)
        # A sequence must stay singable: never snap beyond max_leap mid-phrase.
        near = [p for p in cands
                if prev is None or abs(p - prev) <= spec.max_leap] or cands
        return min(near, key=lambda p: abs(p - desired))

    if spec.style == "arc":
        arc = _generate_arc(text, spec, pool, note_syll)
        if arc is not None:
            return arc
        # Tone rules made the forced cadence unreachable: fall through to the
        # tiled path rather than emit a phrase with a broken ending.

    if spec.pattern:
        return _generate_pattern(text, spec, pool, note_syll)

    notes: list[Note] = []
    cursor = 0.0
    bar = 0
    prev: int | None = None
    prev2: int | None = None
    prev3: int | None = None
    last_leap_at = -9
    run_dir = 0
    run_len = 0
    motif: list[int] = []
    note_i = 0

    for syl in syllables:
        if syl.kind == "rest":
            cursor += spec.beat
            continue

        is_last = note_i == total - 1
        beats = 2.0 if is_last else mult[note_i % len(mult)]
        x = note_i / max(1, total - 1)
        target = (curve(x) - 0.5) * spec.span
        low_bias = syl.tone in t1.TONE_LOW_BIAS

        if note_i < spec.motif_len:
            best = max(allowed(prev, syl.tone),
                       key=lambda c: score(c, prev, prev2, prev3, target, bar,
                                           None, low_bias,
                                           note_i - last_leap_at <= 1,
                                           run_dir, run_len, syl.tone))
            midi = best
            motif.append(midi)
        elif is_last:
            prev_p = prev if prev is not None else base
            cands = allowed(prev, syl.tone)
            triad = [p for p in cands
                     if p % 12 in {spec.tonic % 12, (spec.tonic + 4) % 12,
                                   (spec.tonic + 7) % 12}] or cands
            if spec.end_mode == "tonic":
                # Nursery rhymes cadence *down* onto the tonic: 小星星 ends 221-,
                # 两只老虎 ends 151-. Stay clear of the top two scale degrees —
                # a long high final note is exactly the "尾音太尖" the user heard.
                ceiling = pool[-2] if len(pool) >= 2 else pool[-1]
                safe = [p for p in triad if p <= ceiling] or triad
                near = [p for p in safe if abs(p - prev_p) <= 7] or safe

                def end_cost(p: int) -> tuple:
                    return (0 if p <= prev_p else 1,     # descending preferred
                            0 if p % 12 == spec.tonic % 12 else 1,
                            abs(p - prev_p))

            else:
                near = [p for p in triad if abs(p - prev_p) <= 7] or triad

                def end_cost(p: int) -> tuple:
                    up = p > prev_p
                    wrong_dir = 0 if up == spec.end_rise else 1
                    return (wrong_dir, abs(p - prev_p), abs(p - (spec.tonic + 7)))

            midi = min(near, key=end_cost)
            motif.append(midi)
        else:
            # Develop the motif by sequence: repeat its interval shape, shifted.
            k = (note_i - spec.motif_len)
            idx = k % len(motif)
            cycle = k // len(motif)
            if spec.motif_repeat:
                # first pass identical, second pass lifts a tone (小星星 style)
                shift = 0 if cycle % 2 == 0 else 2
            else:
                shift = scale_steps[(cycle + spec.seed) % len(scale_steps)]
            desired = motif[idx] + shift
            if idx > 0:
                motif_delta = motif[idx] - motif[idx - 1]
            else:
                motif_delta = None
            # 70% sequence, 30% free choice, so the phrase does not become a loop
            if rng.random() < 0.7:
                midi = snap(desired, prev, syl.tone)
            else:
                midi = max(allowed(prev, syl.tone),
                           key=lambda c: score(c, prev, prev2, prev3, target,
                                               bar, motif_delta, low_bias,
                                               note_i - last_leap_at <= 1,
                                               run_dir, run_len, syl.tone))

        if prev is not None:
            d = midi - prev
            if abs(d) >= 3:
                last_leap_at = note_i
            if d != 0 and (d > 0) == (run_dir > 0) and run_dir != 0:
                run_len += 1
            elif d != 0:
                run_dir, run_len = (1 if d > 0 else -1), 1
            else:
                run_dir, run_len = 0, 0
        notes.append(Note(index=note_i, char=syl.char, pinyin=syl.pinyin,
                          tone=syl.tone, midi=midi, start=round(cursor, 4),
                          duration=round(beats * spec.beat, 4), beats=beats))
        prev3, prev2, prev = prev2, prev, midi
        cursor += beats * spec.beat
        note_i += 1
        if beats >= 2.0:
            bar += 1
        elif note_i % 4 == 0:
            bar += 1

    # Post-pass: the motif sequence path writes pitches via snap() and so never
    # sees score()'s triple-repeat penalty. A run of three identical notes reads
    # as a stuck machine ("像卡住的复读机"), so break it here unconditionally.
    for i in range(2 if spec.break_triples else len(notes), len(notes)):
        if notes[i].midi == notes[i - 1].midi == notes[i - 2].midi:
            alt = [p for p in pool if p != notes[i].midi]
            alt.sort(key=lambda p: (abs(p - notes[i].midi), p))
            for cand in alt:
                lo_d, hi_d = TONE_RANGE.get(notes[i].tone, (-2, 2))
                d = cand - notes[i - 1].midi
                if lo_d - 1 <= d <= hi_d + 1:
                    notes[i].midi = cand
                    break
            else:
                notes[i].midi = alt[0]

    # Guards are shared with the pattern path: a phrase may have been generated
    # note by note, but it still has to close and sit in a singable range.
    pitches = [n.midi for n in notes]
    tones = [n.tone for n in notes]
    _post_guards(pitches, tones, spec, pool)
    for n, p in zip(notes, pitches):
        n.midi = p

    meta = {
        "text": text, "tonic": spec.tonic, "scale": spec.scale,
        "range": [spec.lo, spec.hi], "beat_seconds": spec.beat,
        "contour": spec.contour, "rhythm": spec.rhythm, "span": spec.span,
        "motif_len": spec.motif_len, "seed": spec.seed, "notes": len(notes),
        "duration": round(notes[-1].start + notes[-1].duration, 3),
        "generator": "v5",
    }
    return notes, meta


def _tone_ok(prev: int | None, cand: int, tone: int) -> bool:
    """Direction check with a deliberate bias: same-pitch is always allowed.

    Tone 2 must rise and tone 4 must fall, but sustaining the previous pitch is
    how people actually sing a syllable that the melody repeats — it reads as
    "flat tone", not as a wrong word. Reversing direction is what makes a lyric
    unintelligible, so that stays forbidden.
    """
    if prev is None:
        return True
    d = cand - prev
    if tone == 2:
        return d >= -1
    if tone == 4:
        return d <= 1
    lo_d, hi_d = TONE_RANGE.get(tone, (-2, 2))
    return lo_d - 1 <= d <= hi_d + 1


# ---------------------------------------------------------------------------
# Post-generation guards. These run *after* a motif has been tiled and its
# 倒字 repaired. They never rewrite the motif wholesale; they nudge the few
# notes needed to satisfy a musical constraint, and leave everything else.
def _repair_leaps(pitches: list[int], tones: list[int], spec: "Spec",
                  pool: list[int]) -> None:
    """Keep every interval inside ``max_leap`` after tone repair.

    The pattern path tiles a motif and then repairs 倒字 by moving single notes.
    That repair is tone-correct but can leave a ninth-sized hole (a tone-4
    syllable is allowed to fall any distance), which sounds like a tear. The
    search path already scores leaps; this is the equivalent guard for tiling.
    """
    if spec.max_leap <= 0 or len(pitches) < 2:
        return
    for i in range(1, len(pitches)):
        prev = pitches[i - 1]
        if abs(pitches[i] - prev) <= spec.max_leap:
            continue
        if i == len(pitches) - 1:
            # The final interval is the cadence; shortening it by moving the
            # last note would change the phrase's landing. Leave it.
            continue
        alts = sorted((p for p in pool if p != pitches[i] and p != prev),
                      key=lambda p: (abs(p - pitches[i]), p))
        for cand in alts:
            if abs(cand - prev) > spec.max_leap:
                continue
            if not _tone_ok(prev, cand, tones[i]):
                continue
            if i + 1 < len(pitches) and not _tone_ok(cand, pitches[i + 1],
                                                     tones[i + 1]):
                continue
            pitches[i] = cand
            break


def _repair_tones(pitches: list[int], tones: list[int], spec: "Spec",
                  pool: list[int]) -> None:
    """Final 倒字 sweep after every other guard has moved notes.

    The first repair pass runs sequentially, so a later guard (cadence, leap
    repair) can change the note *before* an already-checked syllable and
    invalidate it. This pass looks at both neighbours and is cheap enough to run
    twice.
    """
    for i in range(1, len(pitches)):
        prev = pitches[i - 1]
        if _tone_ok(prev, pitches[i], tones[i]):
            continue
        if i == len(pitches) - 1 and i >= 2:
            # The last note owns the cadence, so try to fix the approach note
            # first. Moving the cadence to satisfy a tone rule is what turned
            # "2 2 0" endings into "2 2 4" and lost the resolution.
            moved = False
            for cand in sorted(pool, key=lambda p: abs(p - pitches[i - 1])):
                if cand in (pitches[i - 1], pitches[i]):
                    continue
                if not _tone_ok(pitches[i - 2], cand, tones[i - 1]):
                    continue
                if not _tone_ok(cand, pitches[i], tones[i]):
                    continue
                pitches[i - 1] = cand
                moved = True
                break
            if moved:
                continue
        # If the offending note is a local peak, lifting the note before it is
        # usually better than flattening the peak: it keeps the phrase's high
        # point (and therefore its range) instead of turning the line into a
        # narrow wobble.
        if i >= 2 and pitches[i] > prev and (
                i + 1 >= len(pitches) or pitches[i] >= pitches[i + 1]):
            best: tuple[tuple, int] | None = None
            for cand in pool:
                if cand == prev:
                    continue
                if not _tone_ok(pitches[i - 2], cand, tones[i - 1]):
                    continue
                if not _tone_ok(cand, pitches[i], tones[i]):
                    continue
                cost = (abs(cand - prev), abs(cand - pitches[i]))
                if best is None or cost < best[0]:
                    best = (cost, cand)
            if best is not None:
                pitches[i - 1] = best[1]
                continue
        alts = sorted((p for p in pool if p != pitches[i]),
                      key=lambda p: (abs(p - pitches[i]), p))
        for cand in alts:
            if not _tone_ok(prev, cand, tones[i]):
                continue
            if i + 1 < len(pitches) and not _tone_ok(cand, pitches[i + 1],
                                                     tones[i + 1]):
                continue
            pitches[i] = cand
            break


def _break_triples(pitches: list[int], tones: list[int], spec: "Spec",
                   pool: list[int], lock_tail: bool = False) -> None:
    """Break a run of three identical pitches without creating 倒字.

    The third note is tried first (it changes the motif least), then the middle,
    then the first. Every candidate is checked against both neighbours and
    against making a new run — the earlier version only moved the third note,
    and when that was illegal it left the run or (worse) moved it into a note
    that flattened the next interval.
    """
    if not spec.break_triples:
        return
    for i in range(2, len(pitches)):
        if not (pitches[i] == pitches[i - 1] == pitches[i - 2]):
            continue
        # Try the first note of the run, then the middle one, then the last:
        # the cadence is the least desirable thing to move, so it goes last.
        # The arc path locks it entirely: its ending is a promise.
        for j in (i - 2, i - 1, i):
            if lock_tail and j == len(pitches) - 1:
                continue
            alts = sorted((p for p in pool if p != pitches[j]),
                          key=lambda p: (abs(p - pitches[j]), p))
            placed = False
            for cand in alts:
                if not _tone_ok(pitches[j - 1], cand, tones[j]):
                    continue
                if j + 1 < len(pitches) and not _tone_ok(cand, pitches[j + 1],
                                                         tones[j + 1]):
                    continue
                if j >= 2 and cand == pitches[j - 1] == pitches[j - 2]:
                    continue
                if (j + 2 < len(pitches) and cand == pitches[j + 1]
                        and pitches[j + 1] == pitches[j + 2]):
                    continue
                pitches[j] = cand
                placed = True
                break
            if placed:
                break


def _ensure_range(pitches: list[int], tones: list[int], spec: "Spec",
                  pool: list[int]) -> None:
    """Widen a phrase that is too narrow to carry a tune.

    Only interior notes are moved: the cadence (last two notes) is owned by
    ``_repair_tail`` and the first note by the cross-phrase join. A candidate is
    accepted only if it becomes the phrase's new peak (or trough) — moving a
    middle note *within* the existing range does not widen anything, which is
    what the first version of this guard did.
    """
    if spec.min_range <= 0 or len(pitches) < 4:
        return
    for _ in range(2):
        cur_lo, cur_hi = min(pitches), max(pitches)
        if cur_hi - cur_lo >= spec.min_range:
            return
        best: tuple[tuple, int, int] | None = None
        # Raise a new peak.
        for i in range(1, len(pitches) - 2):
            for p in pool:
                if p <= cur_hi or p - cur_lo < spec.min_range:
                    continue
                if not _tone_ok(pitches[i - 1], p, tones[i]):
                    continue
                if not _tone_ok(p, pitches[i + 1], tones[i + 1]):
                    continue
                cost = (p - cur_hi, abs(p - pitches[i]), i)
                if best is None or cost < best[0]:
                    best = (cost, i, p)
        if best is None:
            # Otherwise lower a new trough.
            for i in range(1, len(pitches) - 2):
                for p in pool:
                    if p >= cur_lo or cur_hi - p < spec.min_range:
                        continue
                    if not _tone_ok(pitches[i - 1], p, tones[i]):
                        continue
                    if not _tone_ok(p, pitches[i + 1], tones[i + 1]):
                        continue
                    cost = (cur_lo - p, abs(p - pitches[i]), i)
                    if best is None or cost < best[0]:
                        best = (cost, i, p)
        if best is None:
            # No legal widening exists without creating 倒字. Leaving the phrase
            # narrow is the correct failure mode: intelligibility beats width.
            return
        pitches[best[1]] = best[2]


def _repair_tail(pitches: list[int], tones: list[int], spec: "Spec",
                 pool: list[int]) -> None:
    """Make the last three notes descend by at least ``tail_drop`` semitones.

    The final note is kept where the cadence put it; the two notes before it are
    re-chosen. This is what turns a hovering ``1 3 2 2 1 1 2 2 1`` into a
    closing gesture. Questions (``end_mode="rise"``) are exempt: they are
    supposed to hang, not settle.
    """
    if spec.tail_drop <= 0 or spec.end_mode == "rise" or len(pitches) < 3:
        return
    c = pitches[-1]
    best: tuple[tuple, int, int] | None = None
    for a in pool:
        if a - c < spec.tail_drop:
            continue
        if not _tone_ok(pitches[-4] if len(pitches) > 3 else None,
                        a, tones[-3]):
            continue
        for b in pool:
            if not (a >= b >= c) or a - c < spec.tail_drop:
                continue
            if not _tone_ok(a, b, tones[-2]):
                continue
            if not _tone_ok(b, c, tones[-1]):
                continue
            cost = (abs(a - pitches[-3]) + abs(b - pitches[-2]),
                    abs(a - pitches[-3]), a)
            if best is None or cost < best[0]:
                best = (cost, a, b)
    if best is not None:
        pitches[-3], pitches[-2] = best[1], best[2]


def _post_guards(pitches: list[int], tones: list[int], spec: "Spec",
                 pool: list[int], lock_tail: bool = False) -> None:
    _ensure_range(pitches, tones, spec, pool)
    _repair_leaps(pitches, tones, spec, pool)
    _repair_tail(pitches, tones, spec, pool)
    for _ in range(3):
        _break_triples(pitches, tones, spec, pool, lock_tail=lock_tail)
        _repair_tones(pitches, tones, spec, pool)
        _repair_tail(pitches, tones, spec, pool)


def retune_join(notes: list[Note], prev_midi: int | None,
                spec: "Spec") -> bool:
    """Fix a dead or leapy phrase join by moving the new phrase's first note.

    Two joins are musically dead: the same pitch (nothing moved) and a leap of a
    seventh or more (the line tears). Prefer a step of 2-4 semitones. Tone
    direction is still checked against both neighbours, so this can never fix a
    join by creating 倒字.
    """
    if prev_midi is None or len(notes) < 2:
        return False
    delta = notes[0].midi - prev_midi
    if delta != 0 and abs(delta) < 7:
        return False
    pool = _pool(spec)

    def legal(p: int) -> bool:
        if not (spec.lo <= p <= spec.hi) or p == notes[0].midi:
            return False
        if not _tone_ok(prev_midi, p, notes[0].tone):
            return False
        return _tone_ok(p, notes[1].midi, notes[1].tone)

    cands = [p for p in pool if legal(p)]
    if cands:
        def cost(p: int) -> tuple:
            d = abs(p - prev_midi)
            # A step is the ideal join, but never at the price of landing on the
            # next note's pitch: "2 2 4" at the start of a phrase is the
            # "敲门" opening the arc path exists to avoid.
            return (0 if p != notes[1].midi else 1,
                    0 if 2 <= d <= 4 else 1, 0 if d < 7 else 1, d)

        notes[0].midi = min(cands, key=cost)
        return True

    # A single-note fix is not always possible: the first syllable's tone may
    # forbid the step that the second syllable's tone then demands. Moving the
    # first two notes together keeps the entry singable and still fixes the join.
    if len(notes) < 3:
        return False
    best: tuple[tuple, int, int] | None = None
    for p1 in pool:
        d = abs(p1 - prev_midi)
        if p1 == prev_midi or d >= 7:
            continue
        if not _tone_ok(prev_midi, p1, notes[0].tone):
            continue
        for p2 in pool:
            if not _tone_ok(p1, p2, notes[1].tone):
                continue
            if not _tone_ok(p2, notes[2].midi, notes[2].tone):
                continue
            triple = 1 if p1 == p2 == notes[2].midi else 0
            cost = (0 if 2 <= d <= 4 else 1, triple,
                    abs(p1 - notes[0].midi) + abs(p2 - notes[1].midi), d)
            if best is None or cost < best[0]:
                best = (cost, p1, p2)
    if best is None:
        return False
    notes[0].midi, notes[1].midi = best[1], best[2]
    return True


def open_with_vocative(notes: list[Note], spec: "Spec") -> bool:
    """Start a line that opens with a vocative on a call-like entry.

    「主人哥哥今天辛苦了」should feel like someone being addressed: enter from a
    step below the second syllable. The earlier version also allowed *holding*
    the first pitch into the second, which is exactly the ``7 7`` chant the
    reviewer keeps flagging — a vocative wants a lift, not a stutter.
    """
    if len(notes) < 2:
        return False
    pool = _pool(spec)
    second = notes[1].midi
    for cand in (second - 2, second - 4):
        if cand not in pool or cand == notes[0].midi or cand == second:
            continue
        if not _tone_ok(cand, second, notes[1].tone):
            continue
        notes[0].midi = cand
        return True
    return False



def _quantise(target: float, pool: list[int]) -> int:
    """Snap a desired pitch onto the nearest pentatonic pitch."""
    return min(pool, key=lambda p: (abs(p - target), p))


def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


# Speech-melody bias per tone: a rising tone wants to sit a little higher, a
# falling tone a little lower. The hard tone rule already forbids the wrong
# direction; this only shapes the target curve so the line follows the words
# instead of sitting on a grid.
_TONE_BIAS = {1: 0.5, 2: 1.0, 3: -0.5, 4: -1.0, 5: 0.0}

# Cadence degrees tried in order after the one the caller asked for. Landing on
# the tonic is a full close, so it is kept for the last phrase: a verse that
# resolves to do on every line sounds finished four times over. mi and re are
# the half cadences, sol is the "still going" end that v5 used everywhere
# (which is why its joins sounded like four separate starts).
_CADENCE_PREFS = (4, 2, 7, 0, 9, 12)

# 偏音 (a passing tone outside the pentatonic scale). Sol (7) and mi (4) are a
# minor third apart, and a level tone may not fall that far, so a run of level
# tones has no legal way down except the 7-9-7-9 wobble the reviewer keeps
# hearing. One passing tone turns that into a real descent (7-5-4). It is
# expensive on purpose: the DP only buys it when it saves more than it costs,
# i.e. when it is actually a passing note and not a new home note.
_PASSING_OFFSET = 5
_PASSING_COST = 3.4

# Function words (的/了/着/和/都...) should not carry the phrase's biggest leap:
# a jump on 的 sounds like a mistake, the same jump on a noun sounds like
# emphasis. The reviewer asked for this twice.
_FUNC_CHARS = set("的了着和都吗呢吧呀嘛哦哈很就还又也个们得地么")


def _passing_tones(spec: "Spec") -> set[int]:
    return {spec.tonic + _PASSING_OFFSET + 12 * o for o in range(-2, 3)}


def _cadence_pairs(spec: "Spec", tones: list[int],
                   pool: list[int]) -> list[tuple[int, int]]:
    """Candidate (last, penultimate) pairs, cheapest first.

    A tone-2 syllable has to rise (or hold) and a tone-4 syllable has to fall,
    so the last two notes cannot be chosen independently. The first arc version
    fixed the cadence degree and then looked for an approach note, so p1 (last
    word 来, tone 2) could only reach re by repeating it — ``2 2 2`` — which the
    triple guard then "repaired" into ``2 2 4``, destroying the cadence.

    A pair is dropped when the penultimate note has no legal *moving* approach
    of its own: ``0 0 2`` is not an ending, it is a stutter. The caller runs the
    DP for the first few pairs and keeps the one with the lowest total cost, so
    the requested degree wins unless it forces something ugly.
    """
    last_tone = tones[-1]
    pen_tone = tones[-2] if len(tones) > 1 else 5
    tonic = _quantise(spec.tonic, pool)
    order: list[int] = []
    for deg in (spec.cadence,) + _CADENCE_PREFS:
        p = _quantise(spec.tonic + deg, pool)
        if p not in order:
            order.append(p)
    scored: list[tuple[float, int, int]] = []
    for rank, last in enumerate(order):
        # The final phrase has to close on the tonic; no substitute is allowed.
        if spec.cadence == 0 and last != tonic:
            continue
        for pen in pool:
            if pen == last:
                kind = 3.0        # repeated final note: last resort
            elif not _tone_ok(pen, last, last_tone):
                continue
            else:
                d = last - pen
                if -4 <= d <= -1:
                    kind = 0.0    # step down into the cadence
                elif 1 <= d <= 4:
                    kind = 0.6    # rising approach (tone-2 endings)
                else:
                    continue
            if not any(q != pen and abs(q - pen) <= spec.max_leap
                       and _tone_ok(q, pen, pen_tone) for q in pool):
                continue
            cost = kind + 0.5 * rank + 0.1 * abs(abs(last - pen) - 2)
            if spec.cadence != 0 and last == tonic:
                # Resolving to do in the middle of a verse closes the song too
                # early; keep it for the last phrase.
                cost += 0.8
            scored.append((cost, last, pen))
    if not scored:
        fallback = _quantise(spec.tonic + spec.cadence, pool)
        return [(fallback, fallback)]
    scored.sort()
    return [(last, pen) for _, last, pen in scored]


def _arc_targets(spec: "Spec", tones: list[int], n: int, start: int,
                 last: int, pen: int) -> list[float]:
    """The pitch curve the DP is pulled toward, one value per syllable.

    A single smooth arch: rise to ``peak_at``, fall into the penultimate note,
    land on the cadence. Two corrections make it singable rather than
    mechanical: per-tone bias (a rising tone sits a little higher) and a
    direction clamp (a tone-4 syllable may not sit above the note before it, a
    tone-2 syllable may not sit below). Without the clamp the curve asks for a
    rise on a falling word, the tone filter refuses, and the DP's only legal
    move is to repeat the previous pitch — which is where the "念经" runs came
    from.
    """
    peak_i = max(1, min(n - 3, round(spec.peak_at * (n - 3)))) if n >= 4 else 0
    targets: list[float] = []
    for i in range(n):
        if i == n - 1:
            targets.append(last)
        elif i == n - 2:
            targets.append(pen)
        elif n < 4:
            targets.append(start + spec.amp * (i / max(1, n - 2)))
        elif i <= peak_i:
            targets.append(start + spec.amp * _smoothstep(i / peak_i))
        else:
            fall = (i - peak_i) / max(1, (n - 3) - peak_i)
            targets.append(start + spec.amp
                           - (start + spec.amp - pen) * _smoothstep(fall))
    targets = [t + _TONE_BIAS.get(tones[i], 0.0)
               for i, t in enumerate(targets)]
    for i in range(1, max(1, n - 2)):
        if tones[i] == 4:
            targets[i] = min(targets[i], targets[i - 1] - 1.0)
        elif tones[i] == 2:
            targets[i] = max(targets[i], targets[i - 1] + 1.0)
    return targets


def _arc_dp(spec: "Spec", pool: list[int], tones: list[int],
            targets: list[float], start: int, last: int, pen: int,
            force_pen: bool, relax: int = 0,
            light: list[bool] | None = None) -> tuple[list[int], float] | None:
    """Shortest-path solve for one arch phrase.

    State is ``(pitch, interval that arrived, length of the alternating run)``.
    The real interval lets the cost answer a leap with a step in the opposite
    direction (counter-leap); the run length lets it charge the second and third
    flip of the same interval more than the first, which is the difference
    between a lilt (7 9 7) and chanting (7 9 7 9 7). Tone direction is a hard
    filter, never a cost: a 倒字 cannot be bought back.

    ``pool`` may contain a passing tone outside the pentatonic scale (see
    ``_passing_tones``); it is only reachable by step, and it is never allowed
    on the cadence.

    ``relax`` loosens the anti-repeat rules only when nothing else is reachable
    (0 = strict, 1 = a repeat before the cadence is allowed, 2 = any repeat).
    A tone-2 word on the tonic, for instance, can only be approached from do
    itself, and a phrase that refuses to repeat would have no legal ending at
    all — a plain ``do do`` close beats dropping the cadence.
    """
    n = len(tones)
    passing = _passing_tones(spec)
    # State: (pitch, interval that arrived, length of the alternating run).
    # The run length is what separates "a lilt" (7 9 7) from "chanting"
    # (7 9 7 9 7): each further flip of the same interval costs more, so the DP
    # is pushed to leave the pair instead of ping-ponging to the end.
    State = tuple[int, int, int]
    dp: list[dict[State, tuple[float, State | None]]] = [
        {(start, 0, 0): (0.0, None)}]
    for i in range(1, n):
        cur: dict[State, tuple[float, State | None]] = {}
        for (q, d_prev, run_prev), (c_prev, _) in dp[i - 1].items():
            for p in pool:
                if i == n - 1 and p != last:
                    continue
                if force_pen and i == n - 2 and p != pen:
                    continue
                d = p - q
                ad = abs(d)
                if ad > spec.max_leap:
                    continue
                if not _tone_ok(q, p, tones[i]):
                    continue
                # No "knock knock" opening: the second syllable has to move.
                if i == 1 and d == 0 and relax < 2:
                    continue
                # ...and neither may the note just before the cadence: a repeat
                # there turns the ending into "0 0 2" instead of an approach.
                if i == n - 2 and d == 0 and n >= 5 and relax < 1:
                    continue
                # A repeated pitch on a toned syllable is the "念经" sound: the
                # word does not move at all. Only a neutral tone may repeat.
                # (Letting 一声 repeat was tried: it fixes the 7-9-7-9 wobble in
                # a run of level tones but pushes repeats back to 25%, which is
                # the exact complaint v5 got. The wobble is instead left for the
                # next round — it needs a rhythm/melisma answer, not a pitch one.)
                if ad == 0 and tones[i] != 5 and relax < 2:
                    continue
                # ...and the final note may not repeat the penultimate one
                # either: "0 0" is a stutter, not a cadence. Neutral-tone
                # endings (了) would otherwise slip through the rule above.
                if i == n - 1 and d == 0 and relax < 2:
                    continue
                if ad == 0:
                    # Repeats are legal but must be paid for; a neutral-tone
                    # syllable is the natural place to allow one. The cost is
                    # deliberately above a step: "7 7" inside a descending arch
                    # is the single strongest "念经" cue the reviewer flagged.
                    step = 2.0 if tones[i] == 5 else 3.6
                elif ad <= 2:
                    step = 0.0
                elif ad <= 4:
                    step = 0.7
                elif ad <= 5:
                    step = 1.4
                else:
                    step = 3.0
                # Leaps belong at the edges of a phrase, not in its middle.
                if ad >= 5 and 1 < i < n - 2:
                    step += 1.6
                # Counter-leap: a leap is answered by a step back.
                if abs(d_prev) >= 5 and d_prev * d < 0 and ad <= 2:
                    step -= 0.5
                if abs(d_prev) >= 5 and d_prev * d > 0 and ad >= 5:
                    step += 2.0
                # A-B-A-B: the same interval repeated with the sign flipped is
                # "原地踏步" (7 9 7 9 7), the strongest "念经" cue left once
                # repeats are banned. The third and later flips cost much more
                # than the first, because 7 9 7 is a lilt and 7 9 7 9 7 is not.
                run = 0
                if d and d_prev and d * d_prev < 0:
                    if abs(d) == abs(d_prev):
                        run = min(3, run_prev + 1)
                        step += 1.1 * run
                    else:
                        step += 0.25
                # A passing tone outside the pentatonic scale is a spice, not a
                # home note: it must be entered and left by step (never used as
                # a leap target), and it costs enough that the DP only reaches
                # for it when it buys a real line instead of the 7-9 wobble.
                if (p in passing or q in passing) and ad > 2:
                    continue
                if p in passing:
                    step += _PASSING_COST
                # A big leap belongs on a content word. On 的/了/和 it reads as
                # a mistake, not as emphasis, so this is a hard filter instead
                # of a price the DP can pay off somewhere else.
                if light and light[i] and ad >= 4:
                    continue
                cost = c_prev + step + abs(p - targets[i])
                # Motif recall: matching the first phrase's head is a bonus,
                # never a requirement (tone rules win).
                if spec.head and 1 <= i < len(spec.head):
                    if p - start == spec.head[i]:
                        cost -= 0.9
                # State carries the real interval, not just its sign: the
                # counter-leap and A-B-A-B rules above need the magnitude, and a
                # sign-only state silently made them dead code.
                key = (p, d, run)
                if key not in cur or cost < cur[key][0]:
                    cur[key] = (cost, (q, d_prev, run_prev))
        if not cur:
            return None
        dp.append(cur)

    state = min(dp[-1].items(), key=lambda kv: kv[1][0])[0]
    total = dp[-1][state][0]
    pitches = [0] * n
    for i in range(n - 1, -1, -1):
        pitches[i] = state[0]
        prev_state = dp[i][state][1]
        if prev_state is None:
            break
        state = prev_state
    return pitches, total


def _generate_arc(text: str, spec: "Spec", pool: list[int],
                  note_syll: list) -> tuple[list[Note], dict] | None:
    """One phrase = one arch, solved by DP.

    Why not keep tiling motifs: a 7-note motif over 9 syllables repeats four
    times in four lines, and the listener hears a loop. An arch with a single
    peak, stepwise motion around it, a leap only at the edges, and a forced
    cadence is what makes a phrase sound *sung* rather than assembled.

    The DP state is ``(pitch, interval that arrived)`` so the cost can resolve a
    leap by step in the opposite direction — the standard counter-leap rule — and
    can charge for A-B-A-B wobble. Tone direction stays a hard filter.

    Returns ``None`` when the cadence is unreachable under the tone rules, so
    the caller can fall back to the tiled path instead of emitting a broken
    phrase.
    """
    n = len(note_syll)
    tones = [s.tone for s in note_syll]
    if n < 2:
        return None

    # The DP may use a 偏音 as a passing tone; the cadence and the guards stay
    # strictly pentatonic, so the ending never lands on a spice note.
    passing = {p for p in _passing_tones(spec) if spec.lo <= p <= spec.hi}
    arc_pool = sorted(set(pool) | passing)
    light = [s.tone == 5 or s.char in _FUNC_CHARS for s in note_syll]

    pairs = _cadence_pairs(spec, tones, pool)
    start = _quantise(spec.tonic + spec.start_deg, pool)
    pitches: list[int] | None = None
    best_cost = 0.0
    for rank, (last, pen) in enumerate(pairs[:3]):
        targets = _arc_targets(spec, tones, n, start, last, pen)
        got = _arc_dp(spec, arc_pool, tones, targets, start, last, pen, True,
                      light=light)
        if got is None:
            continue
        cand, cost = got
        cost += 0.25 * rank      # tie-break toward the requested cadence
        if pitches is None or cost < best_cost:
            pitches, best_cost = cand, cost
    if pitches is None:
        # Every forced approach failed: keep the cadence and let the note before
        # it float, then allow a repeat as a last resort. A tone-2 word on the
        # tonic has no other legal approach than do itself, and "do do" is a far
        # better ending than a cadence that never lands.
        last, pen = pairs[0]
        targets = _arc_targets(spec, tones, n, start, last, pen)
        for relax in (0, 1, 2):
            got = _arc_dp(spec, arc_pool, tones, targets, start, last, pen,
                          False, relax, light=light)
            if got is not None:
                pitches = got[0]
                break
        if pitches is None:
            return None

    _post_guards(pitches, tones, spec, pool, lock_tail=True)

    mult = RHYTHMS[spec.rhythm]
    dm = spec.duration_mults
    beats_list: list[float] = []
    for i in range(n):
        if i == n - 1:
            beats_list.append(float(spec.final_beats))
        elif dm:
            beats_list.append(float(dm[i % len(dm)]))
        else:
            beats_list.append(float(mult[i % len(mult)]))
    # Breath: hold the phrase's peak. A line of nine equal-length syllables is
    # the "匀速一字一音" the reviewer kept hearing; one long note per phrase is
    # the cheapest cure that does not touch the pitch rules. The cadence is
    # never stretched past two beats — the ending must stay the longest note.
    peak_i = max(range(n), key=lambda j: (pitches[j], -j))
    if 0 < peak_i < n - 2:
        beats_list[peak_i] = min(2.0, round(beats_list[peak_i] * 1.6, 3))

    notes: list[Note] = []
    cursor = 0.0
    for i, syl in enumerate(note_syll):
        beats = beats_list[i]
        notes.append(Note(index=i, char=syl.char, pinyin=syl.pinyin,
                          tone=syl.tone, midi=pitches[i],
                          start=round(cursor, 4),
                          duration=round(beats * spec.beat, 4), beats=beats))
        cursor += beats * spec.beat

    meta = {
        "text": text, "tonic": spec.tonic, "scale": spec.scale,
        "range": [spec.lo, spec.hi], "beat_seconds": spec.beat,
        "contour": "arc", "rhythm": spec.rhythm, "span": spec.amp,
        "pattern": None, "seed": spec.seed, "notes": len(notes),
        "duration": round(notes[-1].start + notes[-1].duration, 3),
        "generator": "v7-arc",
        # The row reports these as ``mults`` so melody_eval derives the same
        # durations the singer actually gets (including the held peak).
        "beats": beats_list,
    }
    return notes, meta


def _generate_pattern(text: str, spec: "Spec", pool: list[int],
                      note_syll: list) -> tuple[list[Note], dict]:
    """Tile a nursery motif over the syllables, then repair 倒字.

    The repair is deliberate: the motif comes first (that is what makes the tune
    singable), and any syllable whose tone direction would be violated is nudged
    to the neighbouring scale degree rather than rewriting the motif.
    """
    n = len(note_syll)
    # Short lines want a compact rocking motif (两只老虎 fits 4 syllables);
    # longer lines want the wide outline (小星星 fits 7). Tiling 两只老虎 over 8
    # syllables collapses to a two-semitone wobble, which is the "太平" the user
    # complained about.
    pat_name = spec.pattern
    if pat_name == "auto":
        pat_name = "tiger" if n <= 5 else ("climb" if n <= 7 else "star")
    motif = PATTERNS[pat_name]
    if spec.rotate:
        k = spec.rotate % len(motif)
        motif = motif[k:] + motif[:k]
    base = pool[len(pool) // 2]
    degrees: list[int] = []
    while len(degrees) < n:
        degrees.extend(motif)
    degrees = degrees[:n]
    # Cadence: the last note must be the tonic (小星星 221-, 两只老虎 151-).
    degrees[-1] = 0
    if n >= 3:
        degrees[-2] = 0 if degrees[-3] != 0 else 2

    def to_midi(deg: int) -> int:
        pcs = t1.PENTATONIC[spec.scale]
        octave, idx = divmod(deg, 12) if False else (0, 0)
        # map scale-degree offset to a pool pitch near the centre
        target = spec.tonic + deg
        octave_shift = 0
        while target + 12 * octave_shift < spec.lo:
            octave_shift += 1
        while target + 12 * octave_shift > spec.hi:
            octave_shift -= 1
        cand = target + 12 * octave_shift
        return min(pool, key=lambda p: (abs(p - cand), p))

    pitches = [to_midi(d) for d in degrees]

    # repair tone direction, cheapest neighbour first (skip the cadence for now)
    for i in range(1, n - 1):
        if _tone_ok(pitches[i - 1], pitches[i], note_syll[i].tone):
            continue
        alts = sorted((p for p in pool if p != pitches[i]),
                      key=lambda p: (abs(p - pitches[i]), p))
        for cand in alts:
            if _tone_ok(pitches[i - 1], cand, note_syll[i].tone):
                pitches[i] = cand
                break

    # Break runs of three identical pitches. The pattern path returns early from
    # generate(), so the post-pass that lives there never ran here — which is why
    # tiled motifs came out as "3 3 3".
    if spec.break_triples:
        for i in range(2, n):
            if pitches[i] == pitches[i - 1] == pitches[i - 2]:
                alts = sorted((p for p in pool if p != pitches[i]),
                              key=lambda p: (abs(p - pitches[i]), p))
                for cand in alts:
                    if _tone_ok(pitches[i - 1], cand, note_syll[i].tone):
                        pitches[i] = cand
                        break

    # Cadence last, and forced onto the tonic. A single short line that ends on
    # the fifth sounds unresolved; 小星星 gets away with it only because a second
    # phrase answers it. If the tone of the last syllable forbids the drop, the
    # penultimate note is moved as well so the approach stays a step, not a leap.
    if n >= 2 and spec.end_mode == "rise":
        # A question or an invitation has to *hang*: lift the last note instead
        # of settling. Keep it inside the range so it does not turn shrill.
        prev = pitches[-2]
        up = [p for p in pool if prev < p <= min(pool[-1], prev + 5)]
        asked = [p for p in up if _tone_ok(prev, p, note_syll[-1].tone)]
        if asked:
            pitches[-1] = min(asked, key=lambda p: abs(p - (prev + 2)))
    elif n >= 2:
        ceiling = pool[-2] if len(pool) >= 2 else pool[-1]
        # A song needs two kinds of ending: the last phrase settles on the
        # tonic, the ones before it hang on the fifth or third so the next
        # phrase has somewhere to go. 小星星 does exactly this (665- then 221-).
        want_pc = spec.tonic % 12
        if spec.end_mode == "fifth":
            want_pc = (spec.tonic + 7) % 12
        elif spec.end_mode == "third":
            want_pc = (spec.tonic + 4) % 12
        targets = [p for p in pool if p % 12 == want_pc and p <= ceiling]
        if targets:
            # Prefer a cadence at or below the middle of the range. Picking the
            # nearest octave-equivalent put the final note on 1' (a high tonic)
            # and the tail came out shrill again.
            target = min(targets, key=lambda p: (0 if p <= base else 1,
                                                 abs(p - base)))
            # Try to approach the target by step. If no single penultimate note
            # satisfies both tone checks, walk the candidates from nearest to
            # furthest instead of giving up and dropping back to the tonic —
            # that silent fallback is what flattened phrase 3 of the song.
            mids = [p for p in pool if abs(p - target) <= 5 and p != target]
            mids.sort(key=lambda p: abs(p - target))
            placed = False
            for pen in mids:
                if _tone_ok(pitches[-3] if n > 2 else None, pen,
                            note_syll[-2].tone) and \
                        _tone_ok(pen, target, note_syll[-1].tone):
                    pitches[-2] = pen
                    pitches[-1] = target
                    placed = True
                    break
            if not placed and _tone_ok(pitches[-2], target, note_syll[-1].tone):
                pitches[-1] = target
                placed = True
            if not placed:
                # Last resort: keep the target and accept a one-step 倒字 on the
                # penultimate, which is far less audible than losing the cadence.
                pitches[-1] = target

        # The cadence runs after the triple-breaker and can itself create a run
        # of three (it sets the last two notes). Fix it by moving the third-from-
        # last note, so the cadence stays intact.
        if spec.break_triples and n >= 4 and \
                pitches[-1] == pitches[-2] == pitches[-3]:
            alts = sorted((q for q in pool if q != pitches[-2]),
                          key=lambda q: (abs(q - pitches[-3]), q))
            for cand in alts:
                if _tone_ok(pitches[-4], cand, note_syll[-3].tone) and \
                        _tone_ok(cand, pitches[-2], note_syll[-2].tone):
                    pitches[-3] = cand
                    break

    tones = [s.tone for s in note_syll]
    _post_guards(pitches, tones, spec, pool)

    mult = RHYTHMS[spec.rhythm]
    dm = spec.duration_mults
    notes: list[Note] = []
    cursor = 0.0
    note_i = 0
    for syl in note_syll:
        is_last = note_i == n - 1
        if is_last:
            beats = 2.0
        elif dm:
            beats = dm[note_i % len(dm)]
        else:
            beats = mult[note_i % len(mult)]
        notes.append(Note(index=note_i, char=syl.char, pinyin=syl.pinyin,
                          tone=syl.tone, midi=pitches[note_i],
                          start=round(cursor, 4),
                          duration=round(beats * spec.beat, 4), beats=beats))
        cursor += beats * spec.beat
        note_i += 1

    meta = {
        "text": text, "tonic": spec.tonic, "scale": spec.scale,
        "range": [spec.lo, spec.hi], "beat_seconds": spec.beat,
        "contour": "pattern", "rhythm": spec.rhythm, "span": spec.span,
        "pattern": pat_name, "seed": spec.seed, "notes": len(notes),
        "duration": round(notes[-1].start + notes[-1].duration, 3),
        "generator": "v5-pattern",
    }
    return notes, meta


# ---------------------------------------------------------------------------
# Cheap symbolic features. These are the *selector*'s inputs: fast and
# deterministic, so we can scan hundreds of candidates per lyric without paying
# for a GPU pass or an API call.
def features(notes: list[Note]) -> dict:
    p = [n.midi for n in notes]
    d = [n.duration for n in notes]
    if len(p) < 2:
        return {}
    intervals = [p[i] - p[i - 1] for i in range(1, len(p))]
    steps = sum(1 for x in intervals if abs(x) in (1, 2))
    leaps = sum(1 for x in intervals if abs(x) >= 5)
    repeats = sum(1 for x in intervals if x == 0)
    triples = sum(1 for i in range(2, len(p)) if p[i] == p[i - 1] == p[i - 2])
    up = sum(1 for x in intervals if x > 0)
    return {
        "n": len(p),
        "distinct": len(set(p)),
        "distinct_ratio": len(set(p)) / len(p),
        "range": max(p) - min(p),
        "steps_pct": steps / len(intervals),
        "leaps_pct": leaps / len(intervals),
        "repeats_pct": repeats / len(intervals),
        "triple_repeats": triples,
        "up_pct": up / len(intervals),
        "dur_std": (sum((x - sum(d) / len(d)) ** 2 for x in d) / len(d)) ** 0.5,
        "dur_ratio": max(d) / max(1e-6, min(d)),
        "end_dir": p[-1] - p[-2],
        "span_used": (max(p) - min(p)) / max(1, len(p) - 1),
    }


def selector_score(f: dict, theta: dict | None = None) -> float:
    """Map features to one number. ``theta`` is what the flywheel learns."""
    th = theta or {
        "distinct": 1.0, "steps": 1.0, "repeats": -2.0, "triples": -1.5,
        "range": 0.25, "leaps": 0.4, "dur": 0.8, "end": 0.5,
    }
    if not f:
        return -1e9
    return (
        th["distinct"] * f["distinct_ratio"] * 4
        + th["steps"] * f["steps_pct"] * 3
        + th["repeats"] * f["repeats_pct"] * 3
        + th["triples"] * f["triple_repeats"]
        + th["range"] * min(f["range"], 14)
        + th["leaps"] * f["leaps_pct"] * 2
        + th["dur"] * (1.0 if f["dur_ratio"] > 1.4 else 0.0)
        + th["end"] * (1.0 if f["end_dir"] > 0 else 0.0)
    )


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rhythm", default="dotted")
    ap.add_argument("--contour", default="rise")
    ap.add_argument("--beat", type=float, default=0.36)
    ap.add_argument("--span", type=float, default=8.0)
    ap.add_argument("--motif", type=int, default=3)
    args = ap.parse_args()
    spec = Spec(seed=args.seed, rhythm=args.rhythm, contour=args.contour,
                beat=args.beat, span=args.span, motif_len=args.motif)
    notes, meta = generate(args.text, spec)
    f = features(notes)
    print("pitches:", [n.midi for n in notes])
    print("durs   :", [n.duration for n in notes])
    print(f"dur {meta['duration']}s  score {selector_score(f):.2f}")
    for k, v in f.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
