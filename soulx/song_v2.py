"""Standard singing chain + melody v2 (rotation and per-phrase rhythm).

This file is the settled configuration after all the A/B work:

    reference  cand_home_up1.wav   (production v6 reference, real recording)
    prompt     data/soulx/prompt_v6.json  (aligned from the 01_home line)
    key        A3 tonic, range 55-69      (low enough for a daily/casual song)
    melody     nursery motif library, per-phrase motif + rotation + rhythm
    post       +2 semitones via resample+time-stretch (same as production TTS)
    backing    plucked arpeggio + bass, quiet

Two melody changes against the previous take:
  * **rotation** — phrases no longer all open on the tonic. Before, all four
    lines started on 1, which reads as four separate starts rather than a tune.
  * **per-phrase rhythm** — the same eighth-note grid for 19 seconds is why it
    felt stiff; phrases 3-4 now use a dotted and a short-long figure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "soulx"))

import accomp  # noqa: E402
import expression  # noqa: E402
import humanize  # noqa: E402
import legato  # noqa: E402
import melody_lexicon as lex  # noqa: E402
import melody_v2 as mv2  # noqa: E402
import modes  # noqa: E402
import prosody  # noqa: E402
import reverb  # noqa: E402
import text2melody as t1  # noqa: E402
from sing_batch import Singer, notes_to_target  # noqa: E402
from transpose_post import transpose  # noqa: E402

SR = 24000
RATE_SCALE = 1.0
TONIC = 57
LO, HI = 55, 69
PROMPT_WAV = "data/whale_voice_demo_v2/refs/cand_home_up1.wav"
PROMPT_META = "data/soulx/prompt_v6.json"

# pattern / cadence / rotation per phrase. A A' B A''.
# p3 used to be ``echo``, which tiles to a 4-semitone wobble — the closing line
# had no lift and the last join dropped a fifth. ``descend`` gives it a real
# arch and lands it on the tonic.
PLAN = [
    dict(pat="star",    end="fifth", rotate=0),
    dict(pat="run",     end="third", rotate=2),
    dict(pat="climb",   end="fifth", rotate=2),
    dict(pat="descend", end="tonic", rotate=0),
]

# v6: one arch per phrase instead of a tiled motif. v5 tiled a 7-note nursery
# figure over nine syllables four times, and the reviewer heard a loop ("四个
# 九字句一个节奏，字字钉在同两个音上"). The cadence degree is a request, not a
# promise — ``melody_v2`` moves it when the last word's tone forbids it.
#
# v8 (rejected by the reviewer): four different peak positions, but p2 climbed
# to the octave (12) for the 拥抱 climax. Heard as "第三句一下子蹦到楼顶... 像通用
# 儿歌，出戏", and p3's 主人 came out as two identical notes ("像在敲门").
#
# v10 (rejected): tried to break the "same句型" with an early peak on line 3
# (2 4 9 7 ...). The 2-4-9 leap plus its flatter neighbours dropped natural to
# 2 and singable to 4 — "音符时值均匀无呼吸，像摆音盒". Reverted below.
#
# v9 is the current shape set: nothing above 9 (a fifth above do), every
# non-final line lands instead of hanging.
#   p0  0 2 4 7 9 7 5 4 2   arch, peak on 天 (9) at 4/8, rolls back down
#   p1  4 0 2 4 5 4 0 2 4   quiet answer: dips on 数, only reaches 5, lands on 4
#   p2  2 4 7 9 7 5 4 0 2   the one high point (拥抱 on 9) then a stepwise
#                           9 7 5 4 0 2 fall, ending on 2 to lead into p3
#   p3  0 4 7 9 7 5 4 2 0   lifts on 主人哥哥, then six notes down to do
# Fingerprints ++++---- / -+++--++ / +++----+ / +++----- are all different.
# p1's low profile is deliberate: four lines that all climb to 9 is what made
# v7 sound like one shape repeated.
ARC_PLAN = [
    dict(start=0, amp=7, cadence=4, peak_at=0.62),
    dict(start=2, amp=9, cadence=4, peak_at=0.55),
    dict(start=2, amp=9, cadence=2, peak_at=0.75),
    dict(start=2, amp=9, cadence=0, peak_at=0.85),
]

# How long she holds the last note of each line, in beats. v9's endings were
# reviewed as "句尾气息和撒娇的拖音没有跟上 / 收得太干脆"; the cadence is the
# only note she can hold without breaking the speech-like rhythm before it, so
# the ending gets 2.5 beats and the closing line 3.
FINAL_BEATS = (2.5, 2.5, 2.5, 3.0)

# Tempo is now derived from the words, not from a fixed "one note per beat".
# Speaking rate for a bright young voice is roughly 4 characters/second; the old
# fixed beat worked out to about 2.4 chars/s, which is why every line dragged.
# Each gesture gets its own rate: boasting is faster than coaxing.
# Characters per second. 3.9 measured out too brisk for this kind of song
# ("速度太快了"); 3.2-3.5 is closer to a relaxed spoken-sung delivery.
RATE = {"boast": 3.7, "cheer": 3.5, "invite": 3.4, "plain": 3.4, "soft": 3.0,
        "ask": 3.5}

# Rhythm figure follows the sentence mood rather than a fixed plan.
GESTURE_RHYTHM = {
    "boast": "bounce",      # short-short-long, pushy
    "cheer": "nursery",     # pickup + dotted cadence
    "invite": "dotted",     # long-short, questioning lilt
    "plain": "nursery",
    "soft": "even",         # no lilt, just gentle
    "ask": "dotted",
}
NUM = {0: "1", 2: "2", 4: "3", 5: "4", 7: "5", 9: "6", 11: "7",
       12: "1'", 14: "2'", 16: "3'", -2: ",6", -5: ",3", -3: ",2"}


def jianpu(deg: list[int]) -> str:
    return " ".join(NUM.get(d, str(d)) for d in deg)


# Function words are spoken quickly — they carry grammar, not weight. Content
# words hold. This is the difference between a sentence having a gait and every
# sentence having the same gait.
FUNCTION_WORDS = set("的了呀呢吧嘛哦啦吗么我你他她它着过得很就都也还再把被给跟在")


def stress_mults(text: str) -> list[float]:
    """Per-syllable duration multipliers from the words themselves.

    Two layers, multiplied once (not twice):
    * word class — function words are spoken quickly, content words hold;
    * semantic weight — warm/comfort words hold a little longer, clamped.
    The syllable before the cadence still gets the largest lean.
    """
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    n = len(chars)
    w = lex.weights(text)
    mults = []
    for i, c in enumerate(chars):
        base = 0.62 if c in FUNCTION_WORDS else 1.0
        weight = w[i] if i < len(w) else 0.0
        mults.append(base * (1.0 + 0.15 * weight))
    # reduplication (开开心心, 夸夸) wants two even short beats
    for i in range(1, n):
        if chars[i] == chars[i - 1]:
            mults[i] = mults[i - 1] = 0.78
    for i in range(2, n):
        if chars[i] == chars[i - 2] and chars[i] != chars[i - 1]:
            mults[i] = mults[i - 2] = 0.78
    feats = lex.features(text)
    # the syllable before the cadence leans into it; a waiting/caring verb
    # leans harder, because that is the emotional verb of the line
    if n >= 2:
        mults[-2] = max(mults[-2], 1.5 if feats["has_wait"] else 1.35)
    return [round(min(1.8, max(0.5, m)), 3) for m in mults]


def pick_pattern(text: str, gesture: str, index: int, total: int) -> str:
    """Choose the motif from the line's words, not from its syllable count.

    The curated plan stays the default — it is the result of the earlier A/B
    rounds, and replacing it wholesale regressed the first two lines in testing.
    Semantics only *override* it in two cases that the plan cannot see:

    * a line with two or more concrete nouns and a narrow default motif gets a
      wider one (「热汤和拥抱」should not use the narrowest figure);
    * a waiting/caring verb on a hovering default motif gets a stepwise figure
      (the verb carries the line, so the tune should move with it).

    The closing line is the exception: it must be wide enough to fall from, and
    a descent is preferred.
    """
    default = PLAN[index]["pat"]
    info = prosody.analyze(text)
    tones = [s.tone for s in t1.analyze(text) if s.kind == "note"]

    if index == total - 1:
        wide = {k: v for k, v in mv2.PATTERNS.items()
                if max(v) - min(v) >= 6}
        if default in wide:
            return default
        return prosody.choose_pattern(text, info, wide or mv2.PATTERNS,
                                      tones=tones)

    width = max(mv2.PATTERNS[default]) - min(mv2.PATTERNS[default])
    feats = lex.features(text)
    if feats["concrete"] >= 2 and width < 5:
        wider = {k: v for k, v in mv2.PATTERNS.items()
                 if k != "descend" and max(v) - min(v) >= 5}
        return prosody.choose_pattern(text, info, wider or mv2.PATTERNS,
                                      tones=tones)
    if feats["has_wait"] and default in ("hover", "echo", "tiger"):
        moving = {k: mv2.PATTERNS[k] for k in ("step", "run", "arc", "swell")}
        return prosody.choose_pattern(text, info, moving, tones=tones)
    return default


def phrase_spec(text: str, item: dict, rate_scale: float | None = None,
                tonic: int = TONIC) -> dict:
    base = dict(modes.spec_for("hum"))
    info = prosody.analyze(text)
    gesture = info["gesture"]
    g = prosody.GESTURES[gesture]

    rhythm = GESTURE_RHYTHM.get(gesture, "nursery")
    scale = RATE_SCALE if rate_scale is None else rate_scale
    rate = RATE.get(gesture, 3.4) * scale
    mults = stress_mults(text)
    # Target duration from the speaking rate, then solve for the beat so the
    # stress pattern fits exactly inside it (the last note is a fixed 2 beats).
    target = len(mults) / rate
    total_beats = sum(mults[:-1]) + 2.0
    beat = target / total_beats

    spec = dict(base)
    spec.update(tonic=tonic, lo=tonic - 2, hi=tonic + 12,
                pattern=item["pat"], end_mode=item["end"],
                rotate=item["rotate"], rhythm=rhythm,
                duration_mults=mults)
    spec["beat"] = round(beat, 3)
    spec["_mults"] = mults
    spec["_g"] = gesture
    spec["_feats"] = lex.features(text)
    spec["span"] = round(base.get("span", 6.5) * g["span_mul"], 2)
    spec["break_triples"] = True
    return spec


def compose_melody(lines: list[str], rate_scale: float | None = None,
                   verbose: bool = False,
                   tonic: int = TONIC) -> tuple[list[list], list[dict]]:
    """Turn lyric lines into per-phrase notes and report rows.

    Split out of ``main`` so the symbolic part can be tested (and reviewed)
    without a GPU, a prompt wav, g2pM or the singer. Everything here is
    deterministic: same lines in, same notes out.
    """
    phrases: list[list] = []
    rows: list[dict] = []
    prev_end: int | None = None
    head: list[int] | None = None
    for i, text in enumerate(lines):
        gesture = prosody.analyze(text)["gesture"]
        pat = pick_pattern(text, gesture, i, len(lines))
        item = dict(PLAN[i], pat=pat)
        spec = phrase_spec(text, item, rate_scale, tonic=tonic)
        mults = spec.pop("_mults", None)
        feats = spec.pop("_feats", {})
        gesture = spec.pop("_g", "")
        # Musical guards: every phrase needs a usable range; the last one has to
        # close (descending cadence). Later phrases stay in the same key — a
        # semitone drift would leave the pentatonic scale (see melody_v2.Spec).
        spec["min_range"] = 6 if i == len(lines) - 1 else 5
        # The arc path owns its own ending (tone-aware cadence pair), so the
        # tail-drop guard would only fight it.
        spec["tail_drop"] = 0
        arc = ARC_PLAN[i] if i < len(ARC_PLAN) else ARC_PLAN[-1]
        spec["style"] = "arc"
        spec["start_deg"] = arc["start"]
        spec["amp"] = arc["amp"]
        spec["cadence"] = arc["cadence"]
        spec["peak_at"] = arc.get("peak_at", 0.62)
        spec["final_beats"] = (FINAL_BEATS[i] if i < len(FINAL_BEATS)
                               else FINAL_BEATS[-1])
        # "even" is the rhythm that made v5 read as chanting: every syllable the
        # same length. A soft line still gets a lilt, just a gentle one.
        if spec["rhythm"] == "even":
            spec["rhythm"] = "nursery"
        # Motif recall is offered to later phrases as a soft bonus. Measured
        # 2026-10-25: on the arc path it never changes the optimum (the contour
        # target and the tone rules dominate), so opening variety has to come
        # from ARC_PLAN's start/amp/peak_at, not from here.
        spec["head"] = head
        sp = mv2.Spec(seed=7, **spec)
        notes, meta = mv2.generate(text, sp)
        if i == 0:
            # Later phrases are nudged (never forced) toward this shape: motif
            # recall is the cheap way to get a hook without tiling.
            head = [n.midi - notes[0].midi for n in notes[1:4]]
        if feats.get("has_vocative"):
            mv2.open_with_vocative(notes, sp)
        join_pre = None if prev_end is None else notes[0].midi - prev_end
        joined = mv2.retune_join(notes, prev_end, sp)
        # Record the join the listener actually hears (after retune), and keep
        # the raw one for diagnosing why a retune was needed.
        join_in = None if prev_end is None else notes[0].midi - prev_end
        dur = meta["duration"]
        deg = [n.midi - tonic for n in notes]
        prev_end = notes[-1].midi
        phrases.append(notes)
        rows.append({"text": text, "pattern": pat,
                     "end": PLAN[i]["end"], "rotate": PLAN[i]["rotate"],
                     "generator": meta.get("generator"),
                     "cadence": ARC_PLAN[min(i, len(ARC_PLAN) - 1)]["cadence"],
                     "rhythm": spec["rhythm"], "gesture": gesture,
                     "beat": spec["beat"], "mults": meta.get("beats") or mults, "degrees": deg,
                     "tones": [n.tone for n in notes],
                     "range": max(n.midi for n in notes) - min(n.midi for n in notes),
                     "tail_drop": (deg[-3] - deg[-1]) if len(deg) >= 3 else 0,
                     "join_in": join_in, "join_pre": join_pre,
                     "join_retuned": joined,
                     "concrete": feats.get("concrete", 0),
                     "wait": feats.get("wait", 0),
                     "cps": round(len(text) / dur, 2),
                     "jianpu": jianpu(deg), "duration": round(dur, 2)})
        if verbose:
            print(f"p{i} {text:10} {pat:6} rot{PLAN[i]['rotate']} "
                  f"{spec['rhythm']:10} beat{spec['beat']:.3f} "
                  f"{rows[-1]['cps']:.1f}字/秒 {rows[-1]['jianpu']}")
    return phrases, rows


def phrase_timeline(rows: list[dict],
                    gap_beats: tuple[float, ...] = (1.1, 1.0, 1.4, 0.0),
                    ) -> list[tuple[float, float]]:
    """(start, duration) for each phrase, with the breath *in beats*.

    Gaps used to be fixed seconds (0.45/0.40/0.55), which is two beats of dead
    air at a slow tempo and a hiccup at a fast one; counting them in the
    phrase's own beat keeps the song moving when the tempo changes.
    """
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for i, row in enumerate(rows):
        spans.append((cursor, row["duration"]))
        gap = gap_beats[i] * row["beat"] if i < len(gap_beats) else 0.0
        cursor += row["duration"] + gap
    return spans


def legato_boundaries(spans: list[tuple[float, float]],
                      rows: list[dict]) -> list[float]:
    """Note starts *inside* phrases: the places a syllable can be cut off.

    The first note of a phrase is excluded on purpose — that boundary is the
    breath between lines, and filling it would delete the phrasing.
    """
    out: list[float] = []
    for (start, _dur), row in zip(spans, rows):
        cursor = start
        for j, m in enumerate(row["mults"]):
            if j:
                out.append(cursor)
            cursor += m * row["beat"]
    return out


def match_backing(vocal: np.ndarray, backing: np.ndarray,
                  db: float) -> np.ndarray:
    """Scale ``backing`` so its RMS sits ``db`` below the vocal's.

    The plucked accompaniment used to be summed in raw, which landed it 17 dB
    under the voice: the track was effectively a cappella, and a bare voice on
    a plucked chord reads as "念" instead of a song. The balance belongs in the
    code, in dB, not in the synth's amplitude.
    """
    vy = float(np.sqrt(np.mean(vocal ** 2))) or 1.0
    bp = float(np.sqrt(np.mean(backing ** 2))) or 1.0
    return (backing * (vy / bp) * (10.0 ** (db / 20.0))).astype(np.float32)


def note_times(spans: list[tuple[float, float]],
               rows: list[dict]) -> list[tuple[float, float]]:
    """(absolute start, duration) of every note, for pitch edits."""
    out: list[tuple[float, float]] = []
    for (start, _dur), row in zip(spans, rows):
        cursor = start
        for m in row["mults"]:
            d = m * row["beat"]
            out.append((cursor, d))
            cursor += d
    return out


def compose(lines: list[str], rate_scale: float | None = None,
            verbose: bool = False, tonic: int = TONIC,
            gap_beats: tuple[float, ...] = (1.1, 1.0, 1.4, 0.0),
            ) -> tuple[list, list[dict], list[tuple]]:
    """Full singing input: (singer segments, report rows, phrase spans)."""
    phrases, rows = compose_melody(lines, rate_scale, verbose, tonic=tonic)
    segs: list = []
    # The closing line gets the longest pause so the last cadence is heard as
    # the end of the song, not as another line.
    spans = phrase_timeline(rows, gap_beats)
    for i, (text, notes) in enumerate(zip(lines, phrases)):
        segs.append(notes_to_target(notes, text, f"p{i}", start_s=spans[i][0]))
    return segs, rows, spans


# Emotion presets. There is no emotion embedding in this SVS checkpoint, so
# "emotion" here means: which of the performance knobs you turn, and how far.
# Every preset is a complete, honest setting — pass any individual flag on the
# command line to override one value.
#
#   soft   ╱ 撒娇·黏人 —— slow, airy, deep vibrato, slides into every note
#   genki  ╱ 元气 —— quicker, brighter, less vibrato, tighter room
#   sad    ╱ 委屈 —— slowest, longest slides, most breath, darker room
#
# The numbers are perceptual, not measured: raising vibrato and scoop makes a
# line sound "sung at" rather than "read out", raising tempo makes it chirpy.
EMOTIONS = {
    "soft": dict(rate_scale=0.78, vibrato=18.0, vibrato_hz=5.2,
                 drift_cents=5.0, scoop=25.0, breath=0.08, air=0.05,
                 reverb=0.14, backing_db=-7.0, legato=0.55),
    "genki": dict(rate_scale=0.88, vibrato=11.0, vibrato_hz=5.6,
                  drift_cents=4.0, scoop=15.0, breath=0.05, air=0.03,
                  reverb=0.10, backing_db=-6.0, legato=0.45),
    "sad": dict(rate_scale=0.70, vibrato=24.0, vibrato_hz=4.7,
                drift_cents=7.0, scoop=38.0, breath=0.12, air=0.07,
                reverb=0.19, backing_db=-9.0, legato=0.65),
}


def emotion_params(name: str) -> dict:
    """Complete parameter set for an emotion (KeyError on an unknown name)."""
    return dict(EMOTIONS[name])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI only: preset first, explicit flags second. No model, no GPU."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--emotion", default="soft", choices=sorted(EMOTIONS))
    known, _ = pre.parse_known_args(argv)

    ap = argparse.ArgumentParser()
    ap.add_argument("--emotion", default=known.emotion, choices=sorted(EMOTIONS),
                    help="情绪预设：soft 撒娇 / genki 元气 / sad 委屈；"
                         "单独传别的参数可以覆盖预设里的某一项")
    ap.add_argument("--out", default="data/soulx/song_v2")
    ap.add_argument("--semitones", type=float, default=3.0,
                    help="key of the sung take (3 = the shipped one; with "
                         "--native-key this shifts the score, not the audio)")
    ap.add_argument("--rate-scale", type=float, default=1.0,
                    help="multiply every RATE; <1 = slower")
    ap.add_argument("--src", default="data/soulx/persona_song/report.json",
                    help="lyrics report to sing")
    ap.add_argument("--prompt-wav", default=PROMPT_WAV)
    ap.add_argument("--prompt-meta", default=PROMPT_META)
    ap.add_argument("--shift-mode", choices=("resample", "model"),
                    default="resample",
                    help="how to transpose: resample the rendered audio (moves "
                         "the formants, sounds younger) or shift the score "
                         "inside the model (keeps her timbre)")
    # The frozen take (song_final) was rendered with these two; a bare
    # ``song_v2.py`` run has to produce the same *settings*, or "默认最好" only
    # holds for the command line we happened to type.
    ap.add_argument("--n-steps", type=int, default=64,
                    help="flow-matching steps (64 was the take that shipped)")
    ap.add_argument("--cfg", type=float, default=None,
                    help="guidance: higher hugs the score, lower is looser")
    ap.add_argument("--native-key", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="sing the score in the transposed key instead of "
                         "resampling the audio (no time-stretch artefacts); "
                         "--no-native-key restores the old resample path")
    ap.add_argument("--reverb", type=float, default=0.0,
                    help="wet amount of a short room on the vocal (0.12-0.18)")
    ap.add_argument("--vibrato", type=float, default=0.0,
                    help="vibrato depth in cents on held notes (15-25)")
    ap.add_argument("--vibrato-hz", type=float, default=5.2)
    ap.add_argument("--drift-cents", type=float, default=5.0,
                    help="slow pitch wander; 0 makes it sound like a synth")
    ap.add_argument("--legato", type=float, default=0.0,
                    help="fill syllable-boundary valleys deeper than this "
                         "fraction of the vowel level (0.55 ≈ -5 dB); 0 = off")
    ap.add_argument("--scoop", type=float, default=0.0,
                    help="cents the voice slides up into each note (20-30)")
    ap.add_argument("--breath", type=float, default=0.0,
                    help="level of the inhale before each phrase, relative to "
                         "the vocal peak (0.05-0.12)")
    ap.add_argument("--air", type=float, default=0.0,
                    help="band-limited noise mixed into the vocal, relative to "
                         "its RMS (0.04-0.08): takes the synth sheen off")
    ap.add_argument("--backing-db", type=float, default=-7.0,
                    help="RMS of the backing relative to the vocal, in dB "
                         "(0 = equal, -7 = voice clearly on top); the "
                         "accompaniment used to land at -17 by accident")
    ap.add_argument("--gap-beats", default="1.1,1.0,1.4,0.0",
                    help="breath between phrases, in beats")
    # Override the per-argument defaults with the preset, but only now: a
    # set_defaults() before add_argument() is undone by the argument's own
    # default=.
    ap.set_defaults(**emotion_params(known.emotion))
    args = ap.parse_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    src = Path(args.src)
    title = json.loads(src.read_text(encoding="utf-8")).get("title", "song")
    lines = [p["text"] for p in
             json.loads(src.read_text(encoding="utf-8"))["phrases"]]

    shift = int(round(args.semitones))
    # Native key: the score itself is written three semitones higher, so the
    # singer never gets resampled. ``transpose_post`` raises the pitch by
    # resampling and then time-stretching the result, which smears consonants
    # and drags the formants up (heard as "童声/不自然").
    tonic = TONIC + shift if args.native_key else TONIC
    gap_beats = tuple(float(x) for x in args.gap_beats.split(",") if x.strip())
    segs, rows, spans = compose(lines, args.rate_scale, verbose=True,
                                tonic=tonic, gap_beats=gap_beats)

    singer = Singer(
        args.prompt_wav, args.prompt_meta,
        # "model" lets the model transpose the score; it was measured to be a
        # no-op on this checkpoint (median F0 327 -> 320 Hz) and is kept only
        # for future models. --native-key is the supported way to skip the
        # resample.
        pitch_shift=shift if args.shift_mode == "model" else 0,
        n_steps=args.n_steps, cfg=args.cfg)
    raw = out / "raw.wav"
    singer.sing_multi(segs, raw)

    vocal = out / "vocal.wav"
    if args.native_key or args.shift_mode == "model":
        # Already at the target pitch; no resample, so keep the file as is.
        shutil.copyfile(raw, vocal)
    else:
        transpose(raw, vocal, args.semitones)

    y, _ = sf.read(str(vocal), dtype="float32", always_2d=False)
    if args.vibrato > 0 or args.scoop > 0:
        y = humanize.humanize(y, SR, note_times(spans, rows),
                              cents=args.vibrato, hz=args.vibrato_hz,
                              drift_cents=args.drift_cents,
                              scoop_cents=args.scoop)
    if args.legato > 0:
        y = legato.fill_valleys(y, SR, legato_boundaries(spans, rows),
                                thr=args.legato)
    if args.air > 0:
        y = expression.airy(y, SR, amount=args.air)
    if args.breath > 0:
        peak = float(np.max(np.abs(y))) or 1.0
        y = y + expression.breath(len(y), SR, [s for s, _ in spans],
                                  level=args.breath * peak)
    if args.reverb > 0:
        y = reverb.reverb(y, SR, wet=args.reverb).astype(np.float32)
    if (args.vibrato > 0 or args.scoop > 0 or args.legato > 0
            or args.air > 0 or args.breath > 0 or args.reverb > 0):
        peak = float(np.max(np.abs(y))) or 1.0
        if peak > 0.99:
            y = y / peak * 0.99
        sf.write(str(vocal), y, SR, subtype="PCM_16")
    pad = accomp.render(spans, tonic,
                        t1.PROGRESSION, t1.TRIAD, len(y) / SR)
    n = min(len(y), len(pad))
    y, pad = y[:n], pad[:n]
    if args.backing_db is not None:
        pad = match_backing(y, pad, args.backing_db)
    mixed = y + pad
    peak = float(np.max(np.abs(mixed))) or 1.0
    sf.write(str(out / "full.wav"), (mixed / peak * 0.92).astype(np.float32),
             SR, subtype="PCM_16")

    joins = [{"interval": b["degrees"][0] - a["degrees"][-1]}
             for a, b in zip(rows, rows[1:])]
    print("joins", [j["interval"] for j in joins])
    print(f"total {len(mixed)/SR:.2f}s")

    (out / "report.json").write_text(json.dumps({
        "title": title, "tonic": tonic, "range": [tonic - 2, tonic + 12],
        "prompt": args.prompt_wav, "semitones": args.semitones,
        "emotion": args.emotion, "rate_scale": args.rate_scale,
        "shift_mode": args.shift_mode, "n_steps": singer.n_steps,
        "cfg": singer.cfg, "native_key": args.native_key,
        "reverb": args.reverb, "gap_beats": list(gap_beats),
        "vibrato": args.vibrato, "vibrato_hz": args.vibrato_hz,
        "drift_cents": args.drift_cents, "backing_db": args.backing_db,
        "legato": args.legato, "scoop": args.scoop,
        "breath": args.breath, "air": args.air,
        "phrases": rows, "joins": joins,
        "duration_s": round(len(mixed) / SR, 2),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out / "report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
