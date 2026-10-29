#!/usr/bin/env python
"""Rule-based lyric -> melody generator (v0).

Mandarin is a tonal language: every syllable carries a tone contour that a
listener expects the melody to follow. Violating it produces 倒字 -- the
listener hears a different word. This module turns that expectation into a
scoring constraint, then keeps every note inside a pentatonic scale so the
result cannot sound "wrong" even when it is not beautiful.

Pipeline position::

    lyrics --[text2melody]--> notes.json / melody.mid / melody.wav
           --[resing_world]--> sung wav

No model, no GPU, no network. The only optional dependency is ``pypinyin``
(for tone lookup); it is installed into ``vendor/singdeps`` so that
``.venv-cosyvoice`` stays clean. Without it the script still runs, but every
syllable degrades to neutral tone and the contour constraint is lost.

Examples::

    .venv-cosyvoice\\Scripts\\python.exe scripts\\text2melody.py "主人哥哥生日快乐"
    ... --tonic 60 --lo 60 --hi 72 --beat 0.32 --seed 7 --render --midi

Measured range of the canonical reference (``ref_base_x2.wav``, 2026-10-22):
P5 = C4 (261 Hz), P50 = G#4 (416 Hz), P95 = C5 (534 Hz). The defaults below
bracket that range, so generated melodies stay inside her comfortable register.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SINGDEPS = ROOT / "vendor" / "singdeps"
if SINGDEPS.is_dir():
    sys.path.insert(0, str(SINGDEPS))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from pypinyin import Style, pinyin

    HAVE_PYPINYIN = True
except ImportError:  # degraded mode
    pinyin = None
    Style = None
    HAVE_PYPINYIN = False

import random

# --- music theory tables -------------------------------------------------

# 宫 商 角 徵 羽 -- the pentatonic scale, as semitone offsets from the tonic.
PENTATONIC = {
    "major": [0, 2, 4, 7, 9],
    "minor": [0, 3, 5, 7, 10],
}

# Chord skeleton: (root offset from tonic, quality). I - V - vi - IV loop.
PROGRESSION = [(0, "maj"), (7, "maj"), (9, "min"), (5, "maj")]

TRIAD = {"maj": [0, 4, 7], "min": [0, 3, 7]}

# Mandarin tone -> allowed melodic interval relative to the previous note.
# tone 1 (55 high-flat): hold or micro-rise
# tone 2 (35 rising):    must rise at least a major second
# tone 3 (214 dipping):  stays low, small moves only
# tone 4 (51 falling):   must fall at least a major second
# tone 5 (neutral):      leans on the previous note
TONE_RANGE = {
    1: (-1, 2),
    2: (2, 7),
    3: (-4, 2),
    4: (-7, -2),
    5: (-2, 1),
}
TONE_LOW_BIAS = {3: 2.2}  # 上声 prefers the lower half of the range

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Phrase-level pitch contours, normalised to 0..1 over the phrase. A melody
# needs a shape; without one every syllable gravitates to the same pitch and
# the result reads as chanting rather than singing.
CONTOURS = {
    "arch": lambda x: math.sin(math.pi * x),
    "rise": lambda x: x ** 0.85,
    "fall": lambda x: 1.0 - x ** 0.85,
    "wave": lambda x: 0.5 + 0.5 * math.sin(2 * math.pi * x - math.pi / 2),
    "valley": lambda x: 1.0 - abs(2 * x - 1),
}


def midi_name(m: int) -> str:
    return f"{NOTE_NAMES[m % 12]}{m // 12 - 1}"


def midi_to_hz(m: float) -> float:
    return 440.0 * (2.0 ** ((m - 69) / 12.0))


# --- text analysis -------------------------------------------------------


@dataclass
class Syllable:
    char: str
    pinyin: str
    tone: int
    kind: str  # "note" or "rest"


@dataclass
class Note:
    index: int
    char: str
    pinyin: str
    tone: int
    midi: int
    start: float
    duration: float
    beats: float

    @property
    def hz(self) -> float:
        return round(midi_to_hz(self.midi), 2)


def analyze(text: str) -> list[Syllable]:
    """Split text into syllables. Chinese: one char == one syllable."""
    out: list[Syllable] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            py, tone = "", 5
            if HAVE_PYPINYIN:
                res = pinyin(
                    ch,
                    style=Style.TONE3,
                    neutral_tone_with_five=True,
                    errors="ignore",
                )
                if res and res[0]:
                    py = res[0][0]
                    if py and py[-1].isdigit():
                        tone = int(py[-1])
                    py = py.rstrip("0123456789")
            out.append(Syllable(char=ch, pinyin=py, tone=tone, kind="note"))
        elif ch.strip() and ch in "，,。.！!？?；;、…~—":
            out.append(Syllable(char=ch, pinyin="", tone=0, kind="rest"))
        # spaces and anything else are dropped
    return out


# --- melody generation ---------------------------------------------------


def scale_pitches(tonic: int, kind: str, lo: int, hi: int) -> list[int]:
    offs = PENTATONIC[kind]
    out = set()
    for octave in range(-2, 3):
        for off in offs:
            m = tonic + off + 12 * octave
            if lo <= m <= hi:
                out.add(m)
    return sorted(out)


def chord_pcs(tonic: int, bar: int) -> set[int]:
    root_off, quality = PROGRESSION[bar % len(PROGRESSION)]
    root = tonic + root_off
    return {(root + iv) % 12 for iv in TRIAD[quality]}


def generate(
    text: str,
    tonic: int = 60,
    scale: str = "major",
    lo: int = 60,
    hi: int = 72,
    beat: float = 0.45,
    max_leap: int = 7,
    seed: int = 7,
    contour: str = "auto",
    span: float = 7.0,
) -> tuple[list[Note], list[Syllable], dict]:
    rng = random.Random(seed)
    syllables = analyze(text)
    note_syllables = [s for s in syllables if s.kind == "note"]
    if not note_syllables:
        raise SystemExit("no singable characters in input")

    pitches = scale_pitches(tonic, scale, lo, hi)
    if len(pitches) < 3:
        raise SystemExit(f"range [{lo},{hi}] too narrow for {scale} pentatonic")

    if contour == "auto":
        contour = rng.choice(sorted(CONTOURS))
    if contour not in CONTOURS:
        raise SystemExit(f"unknown contour {contour!r}")
    curve = CONTOURS[contour]

    total = len(note_syllables)
    base = pitches[len(pitches) // 2]
    notes: list[Note] = []
    prev: int | None = None
    prev2: int | None = None
    cursor = 0.0  # seconds
    bar = 0
    note_i = 0

    for syl in syllables:
        if syl.kind == "rest":
            cursor += beat
            continue

        is_last = note_i == total - 1
        beats = 2.0 if is_last else 1.0
        tonality = chord_pcs(tonic, bar)
        lo_d, hi_d = TONE_RANGE.get(syl.tone, (-2, 2))

        # Height the phrase contour asks for, in semitones relative to base.
        x = note_i / max(1, total - 1)
        target = (curve(x) - 0.5) * span

        if is_last:
            pool = [p for p in (tonic, tonic + 4, tonic + 12, tonic + 16) if lo <= p <= hi]
            if not pool:
                pool = [pitches[0]]
            # The final syllable still has a tone: resolve without 倒字 if possible.
            ok = [c for c in pool if prev is None or lo_d - 1 <= c - prev <= hi_d + 1]
            midi = min(ok or pool, key=lambda c: (c != tonic, abs(c - (prev if prev is not None else base))))
        else:
            best: tuple[float, int] | None = None
            for cand in pitches:
                delta = 0 if prev is None else cand - prev
                score = 0.0
                # 1) tone direction -- the anti-倒字 constraint, deliberately dominant
                if delta < lo_d:
                    score -= 6.0 * min(3, lo_d - delta)
                if delta > hi_d:
                    score -= 6.0 * min(3, delta - hi_d)
                # 2) interval size: stepwise is natural, leaps are not free
                ad = abs(delta)
                if ad > max_leap:
                    score -= 5.0 + 2.0 * (ad - max_leap)
                elif ad > 4:
                    score -= 1.4
                elif 1 <= ad <= 2:
                    score += 0.6
                # 3) repeats are the main reason a generated melody sounds like chanting
                if prev is not None and cand == prev:
                    score -= 1.8
                    if prev2 is not None and cand == prev2:
                        score -= 3.0
                # 4) chord tones, weighted more on the downbeat
                if cand % 12 in tonality:
                    score += 1.4 if note_i % 4 else 2.0
                elif note_i % 4 == 0:
                    score -= 1.1
                # 5) shape: follow the phrase contour
                score -= 0.5 * abs((cand - base) - target)
                # 6) 上声 sits low
                if syl.tone in TONE_LOW_BIAS:
                    score -= 0.3 * (cand - pitches[0])
                # 7) deterministic jitter so different seeds give different tunes
                score += rng.uniform(0.0, 1.2)
                if best is None or score > best[0]:
                    best = (score, cand)
            midi = best[1] if best else base

        notes.append(
            Note(
                index=note_i,
                char=syl.char,
                pinyin=syl.pinyin,
                tone=syl.tone,
                midi=midi,
                start=round(cursor, 4),
                duration=round(beats * beat, 4),
                beats=beats,
            )
        )
        prev2, prev = prev, midi
        cursor += beats * beat
        note_i += 1
        if beats >= 2.0:
            bar += 1
        elif note_i % 4 == 0:
            bar += 1

    meta = {
        "text": text,
        "tonic": tonic,
        "tonic_name": midi_name(tonic),
        "scale": scale,
        "range": [lo, hi],
        "beat_seconds": beat,
        "seed": seed,
        "contour": contour,
        "span": span,
        "pypinyin": HAVE_PYPINYIN,
        "notes": len(notes),
        "duration": round(notes[-1].start + notes[-1].duration, 3),
        "pitch_set": pitches,
    }
    return notes, syllables, meta


# --- validation ----------------------------------------------------------


def validate(notes: list[Note], meta: dict, syllables: list[Syllable]) -> list[tuple[str, bool, str]]:
    lo, hi = meta["range"]
    tonic = meta["tonic"]
    pitches = set(meta["pitch_set"])
    singable = [s for s in syllables if s.kind == "note"]
    checks: list[tuple[str, bool, str]] = []

    out_of_range = [n.midi for n in notes if not (lo <= n.midi <= hi)]
    checks.append(("音域", not out_of_range, f"越界 {out_of_range}" if out_of_range else f"{lo}-{hi} 内"))

    checks.append(
        (
            "音节对齐",
            len(notes) == len(singable),
            f"{len(notes)} 音符 / {len(singable)} 字",
        )
    )

    off_scale = [n.midi for n in notes if n.midi not in pitches]
    checks.append(("五声音阶", not off_scale, f"调外音 {off_scale}" if off_scale else "全部在阶内"))

    big = [(a.index, b.index, b.midi - a.midi) for a, b in zip(notes, notes[1:]) if abs(b.midi - a.midi) > 7]
    checks.append(("跳进", not big, f"大跳 {big}" if big else "无超过纯五度的跳进"))

    last = notes[-1].midi
    resolved = (last - tonic) % 12 in (0, 4)
    checks.append(("句末收束", resolved, f"{midi_name(last)}" + ("（主音/三音）" if resolved else "（未解决）")))

    conflicts = []
    for a, b in zip(notes, notes[1:]):
        d = b.midi - a.midi
        lo_d, hi_d = TONE_RANGE.get(b.tone, (-2, 2))
        if d < lo_d - 1 or d > hi_d + 1:
            conflicts.append((b.char, b.tone, d))
    checks.append(("倒字风险", not conflicts, f"{conflicts}" if conflicts else "声调走向全部满足"))

    return checks


# --- output writers ------------------------------------------------------


def write_midi(notes: list[Note], meta: dict, path: Path, program: int = 52) -> None:
    """Minimal SMF format 0 writer (no external dependency)."""
    division = 480  # ticks per quarter
    ticks_per_second = division / meta["beat_seconds"]

    events: list[tuple[int, bytes]] = []

    def meta_event(delta: int, payload: bytes) -> None:
        events.append((delta, b"\xff" + payload))

    # tempo: microseconds per quarter note
    us_per_qn = int(round(meta["beat_seconds"] * 1_000_000))
    meta_event(0, b"\x51\x03" + us_per_qn.to_bytes(3, "big"))
    # program change
    events.append((0, bytes([0xC0, program & 0x7F])))
    # track name
    name = "fatfish text2melody".encode("ascii")
    meta_event(0, b"\x03" + bytes([len(name)]) + name)

    abs_events: list[tuple[int, int, bytes]] = []
    for n in notes:
        on = int(round(n.start * ticks_per_second))
        off = int(round((n.start + n.duration) * ticks_per_second))
        abs_events.append((on, 1, bytes([0x90, n.midi & 0x7F, 90])))
        abs_events.append((off, 0, bytes([0x80, n.midi & 0x7F, 0])))
    abs_events.sort(key=lambda e: (e[0], e[1]))

    prev_tick = 0
    for tick, _order, payload in abs_events:
        events.append((tick - prev_tick, payload))
        prev_tick = tick
    events.append((0, b"\xff\x2f\x00"))

    track = bytearray()
    for delta, payload in events:
        track += _varint(delta)
        track += payload

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, division)
    body = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    path.write_bytes(header + body)


def _varint(value: int) -> bytes:
    if value < 0:
        value = 0
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def render_melody(notes: list[Note], path: Path, sr: int = 24000, gain: float = 0.22) -> None:
    """Render the melody with a soft additive synth so the tune is audible."""
    import numpy as np
    import soundfile as sf

    total = int(round((notes[-1].start + notes[-1].duration + 0.25) * sr))
    buf = np.zeros(total, dtype=np.float64)
    harmonics = [(1, 1.0), (2, 0.42), (3, 0.18), (4, 0.08)]

    for n in notes:
        start = int(round(n.start * sr))
        length = int(round(n.duration * sr))
        t = np.arange(length) / sr
        f = midi_to_hz(n.midi)
        wave = np.zeros(length)
        for mult, amp in harmonics:
            wave += amp * np.sin(2 * np.pi * f * mult * t)
        # simple ADSR: 15ms attack, 40ms release, gentle decay
        env = np.ones(length)
        a = min(length, int(0.015 * sr))
        if a:
            env[:a] = np.linspace(0, 1, a)
        r = min(length, int(0.04 * sr))
        if r:
            env[-r:] = np.linspace(1, 0, r)
        env *= np.exp(-0.9 * t / max(0.15, n.duration))
        seg = wave * env * gain
        end = min(total, start + length)
        buf[start:end] += seg[: end - start]

    peak = float(np.max(np.abs(buf))) or 1.0
    buf = buf / peak * 0.85
    sf.write(str(path), buf, sr, subtype="PCM_16")


def print_notes(notes: list[Note]) -> None:
    print(f"{'#':>3} {'字':<2} {'拼音':<8} {'调':>2} {'音':<5} {'MIDI':>4} {'Hz':>7} {'拍':>4} {'起':>6}")
    for n in notes:
        print(
            f"{n.index:>3} {n.char:<2} {n.pinyin or '-':<8} {n.tone:>2} "
            f"{midi_name(n.midi):<5} {n.midi:>4} {n.hz:>7.1f} {n.beats:>4.1f} {n.start:>6.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rule-based lyric to melody generator")
    ap.add_argument("text", help="lyrics; punctuation becomes a rest")
    ap.add_argument("--tonic", type=int, default=60, help="tonic MIDI (default 60 = C4)")
    ap.add_argument("--scale", choices=sorted(PENTATONIC), default="major")
    ap.add_argument("--lo", type=int, default=60)
    ap.add_argument("--hi", type=int, default=72)
    ap.add_argument("--beat", type=float, default=0.45, help="base note length in seconds")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--contour",
        default="auto",
        choices=["auto", *sorted(CONTOURS)],
        help="phrase pitch contour; auto picks one from the seed",
    )
    ap.add_argument("--span", type=float, default=7.0, help="contour height in semitones")
    ap.add_argument("--out", default="data/sing", help="output directory")
    ap.add_argument("--name", default="melody", help="output basename")
    ap.add_argument("--midi", action="store_true", help="also write a .mid file")
    ap.add_argument("--render", action="store_true", help="also render melody.wav")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    outdir = (ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    notes, syllables, meta = generate(
        args.text,
        tonic=args.tonic,
        scale=args.scale,
        lo=args.lo,
        hi=args.hi,
        beat=args.beat,
        seed=args.seed,
        contour=args.contour,
        span=args.span,
    )
    checks = validate(notes, meta, syllables)

    payload = {
        "meta": meta,
        "notes": [asdict(n) | {"hz": n.hz, "name": midi_name(n.midi)} for n in notes],
        "checks": [{"name": c[0], "ok": c[1], "detail": c[2]} for c in checks],
    }
    json_path = outdir / f"{args.name}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"text: {args.text}")
    print(
        f"tonic: {meta['tonic_name']}  scale: {args.scale}  range: {args.lo}-{args.hi} "
        f"({midi_name(args.lo)}-{midi_name(args.hi)})  pypinyin: {HAVE_PYPINYIN}"
    )
    print()
    print_notes(notes)
    print()
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print()
    print(f"wrote {json_path.relative_to(ROOT) if json_path.is_relative_to(ROOT) else json_path}")

    if args.midi:
        midi_path = outdir / f"{args.name}.mid"
        write_midi(notes, meta, midi_path)
        print(f"wrote {midi_path}")
    if args.render:
        wav_path = outdir / f"{args.name}.wav"
        render_melody(notes, wav_path)
        print(f"wrote {wav_path}")

    print()
    print("RESULT:", "ALL CHECKS PASS" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
