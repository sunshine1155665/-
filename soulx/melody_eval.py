"""Symbolic checks for a generated melody.

The audio judge is slow, non-deterministic and (at 3-4 s) cannot reliably tell
whether a tune fits the words. These checks are the part that can be trusted in
CI: every claim is a property of the note list, so a regression is a red test,
not a matter of taste.

They are also the inputs the mimo review gets as text, so it can judge the audio
against the actual intervals instead of guessing from a 10-second clip.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "soulx"))
sys.path.insert(0, str(ROOT / "scripts"))

import text2melody as t1  # noqa: E402
from melody_v2 import _tone_ok  # noqa: E402  (single source of truth)


def _pct(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def phrase_metrics(notes: list, tonic: int) -> dict:
    midi = [n.midi for n in notes]
    deg = [m - tonic for m in midi]
    durs = [n.duration for n in notes]
    intervals = [midi[i] - midi[i - 1] for i in range(1, len(midi))]
    tone_pass = sum(1 for i in range(1, len(notes))
                    if _tone_ok(midi[i - 1], midi[i], notes[i].tone))
    steps = sum(1 for x in intervals if abs(x) in (1, 2))
    leaps = sum(1 for x in intervals if abs(x) >= 5)
    repeats = sum(1 for x in intervals if x == 0)
    triples = sum(1 for i in range(2, len(midi))
                  if midi[i] == midi[i - 1] == midi[i - 2])
    return {
        "n": len(notes),
        "range": (max(midi) - min(midi)) if midi else 0,
        "distinct": len(set(midi)),
        "distinct_ratio": _pct(len(set(midi)), len(midi)),
        "steps_pct": _pct(steps, len(intervals)),
        "leaps_pct": _pct(leaps, len(intervals)),
        "max_leap": max((abs(x) for x in intervals), default=0),
        "repeats_pct": _pct(repeats, len(intervals)),
        "triple_repeats": triples,
        "tone_compliance": _pct(tone_pass, len(notes) - 1),
        "tail_drop": (deg[-3] - deg[-1]) if len(deg) >= 3 else 0,
        "tail_descends": bool(len(deg) >= 3
                              and deg[-3] >= deg[-2] >= deg[-1]),
        "end_degree": deg[-1] if deg else None,
        "dur_min": round(min(durs), 3) if durs else 0,
        "dur_max": round(max(durs), 3) if durs else 0,
        "dur_ratio": round(max(durs) / max(1e-6, min(durs)), 2) if durs else 0,
    }


def row_metrics(row: dict, tonic: int) -> dict:
    """Metrics for a report row (degrees + tones + duration multipliers).

    A row without ``tones`` predates tone tracking (v4 and older). Reporting a
    fabricated 78% compliance for it — the old fallback assumed every syllable
    was neutral tone — is worse than reporting "unknown", so the field is None.
    """
    deg = row["degrees"]
    tones = row.get("tones")
    midi = [tonic + d for d in deg]
    beat = row.get("beat", 0)
    mults = row.get("mults") or [1.0] * max(0, len(deg) - 1)
    if len(mults) >= len(deg):
        # v10+ reports every note's multiplier, including the final note, which
        # may be longer than the old fixed 2 beats.
        durs = [round(m * beat, 3) for m in mults[:len(deg)]]
    else:
        durs = [round(m * beat, 3) for m in mults[:-1]] + [round(2 * beat, 3)]
        durs = durs[:len(deg)]
    intervals = [midi[i] - midi[i - 1] for i in range(1, len(midi))]
    tone_pass = (sum(1 for i in range(1, len(deg))
                     if _tone_ok(midi[i - 1], midi[i], tones[i]))
                 if tones else None)
    steps = sum(1 for x in intervals if abs(x) in (1, 2))
    leaps = sum(1 for x in intervals if abs(x) >= 5)
    repeats = sum(1 for x in intervals if x == 0)
    triples = sum(1 for i in range(2, len(midi))
                  if midi[i] == midi[i - 1] == midi[i - 2])
    return {
        "text": row.get("text", ""),
        "pattern": row.get("pattern", ""),
        "n": len(deg),
        "range": max(deg) - min(deg) if deg else 0,
        "distinct": len(set(deg)),
        "distinct_ratio": _pct(len(set(deg)), len(deg)),
        "steps_pct": _pct(steps, len(intervals)),
        "leaps_pct": _pct(leaps, len(intervals)),
        "max_leap": max((abs(x) for x in intervals), default=0),
        "repeats_pct": _pct(repeats, len(intervals)),
        "triple_repeats": triples,
        "tone_compliance": (None if tone_pass is None
                            else _pct(tone_pass, len(deg) - 1)),
        "tail_drop": (deg[-3] - deg[-1]) if len(deg) >= 3 else 0,
        "tail_descends": bool(len(deg) >= 3
                              and deg[-3] >= deg[-2] >= deg[-1]),
        "end_degree": deg[-1] if deg else None,
        "join_in": row.get("join_in"),
        "dur_min": round(min(durs), 3) if durs else 0,
        "dur_max": round(max(durs), 3) if durs else 0,
        "dur_ratio": round(max(durs) / max(1e-6, min(durs)), 2) if durs else 0,
    }


def report_metrics(rep: dict) -> dict:
    tonic = rep.get("tonic", 57)
    phrases = rep.get("phrases", [])
    rows = [row_metrics(r, tonic) for r in phrases]
    # Old reports have no stored join_in; the degrees are enough to recover it.
    for prev, cur, row in zip(phrases, phrases[1:], rows[1:]):
        if row["join_in"] is None:
            row["join_in"] = cur["degrees"][0] - prev["degrees"][-1]
    joins = [r["join_in"] for r in rows if r.get("join_in") is not None]
    scored = [r for r in rows if r["tone_compliance"] is not None]
    return {
        "title": rep.get("title", ""),
        "phrases": rows,
        "joins": joins,
        "joins_ok": all(j != 0 and abs(j) < 7 for j in joins),
        "min_phrase_range": min((r["range"] for r in rows), default=0),
        "tone_compliance": (
            _pct(sum(round(r["tone_compliance"] * (r["n"] - 1)) for r in scored),
                 sum(max(0, r["n"] - 1) for r in scored))
            if scored else None),
        "triple_repeats": sum(r["triple_repeats"] for r in rows),
        "final_tail_drop": rows[-1]["tail_drop"] if rows else 0,
        "final_tail_descends": rows[-1]["tail_descends"] if rows else False,
        "duration_s": rep.get("duration_s"),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: melody_eval.py <report.json>")
        return 2
    rep = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = report_metrics(rep)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
