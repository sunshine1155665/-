"""Build SoulX-Singer *target* metadata from our own lyrics + melody.

Why hand-build instead of running their preprocess pipeline:

* ``cli/inference.py`` reads a target JSON list of segments; in ``score`` mode
  the model takes pitch purely from ``note_pitch`` (see ``SoulXSinger.infer``:
  ``control == "score"`` sets ``gt_f0 = pt_f0 = None``), so the ``f0`` field is
  never needed on the target side.
* Their preprocess exists to *recover* lyrics and notes from an existing
  recording. We already know both, exactly, because ``scripts/text2melody.py``
  generates them. Feeding our own numbers removes the "automatic alignment is
  imperfect, fix it in the MIDI editor" failure mode entirely.

So the whole preprocess package is only needed for the **prompt** (her
reference clip), never for the target.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOULX = ROOT / "vendor" / "SoulX-Singer"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SOULX))

import text2melody  # noqa: E402

NOTE_TYPE_REST = 1
NOTE_TYPE_NOTE = 2
NOTE_TYPE_TIE = 3


def _phonemes(lyrics: str) -> list[str]:
    """Per-character pinyin-with-tone using the repo's own g2pM.

    Using g2pM (not pypinyin) matters: the model indexes phonemes through
    ``phone_set.json``, which is exactly g2pM's inventory. A pypinyin variant
    like ``zh_nue4`` vs ``zh_nve4`` would raise a KeyError at load time.
    """
    from g2pM import G2pM

    g2p = G2pM()
    chars = [c for c in lyrics if c.strip()]
    sylls = g2p("".join(chars), tone=True, char_split=False)
    if len(sylls) != len(chars):
        raise SystemExit(f"g2p mismatch: {len(chars)} chars vs {len(sylls)} syllables")
    return [f"zh_{s}" for s in sylls]


def build(
    lyrics: str,
    *,
    tonic: int = 60,
    scale: str = "major",
    lo: int = 60,
    hi: int = 72,
    beat: float = 0.42,
    seed: int = 7,
    contour: str = "auto",
    index: str = "whale_target",
) -> dict:
    notes, _syllables, meta = text2melody.generate(
        lyrics, tonic=tonic, scale=scale, lo=lo, hi=hi, beat=beat,
        seed=seed, contour=contour,
    )
    phones = _phonemes(lyrics)
    if len(phones) != len(notes):
        raise SystemExit(f"note/phoneme mismatch: {len(notes)} notes vs {len(phones)} phones")

    # Leading rest: the example files all start with "<SP>" and note_type 1.
    # It gives the model a breath before the first syllable.
    tokens = ["<SP>"]
    durations = ["0.24"]
    pitches = ["0"]
    types = [str(NOTE_TYPE_REST)]

    for note, phone in zip(notes, phones):
        tokens.append(phone)
        durations.append(f"{note.duration:.4f}")
        pitches.append(str(int(note.midi)))
        types.append(str(NOTE_TYPE_NOTE))

    total_s = sum(float(d) for d in durations)

    segment = {
        "index": index,
        "language": "Mandarin",
        "time": [0, int(round(total_s * 1000))],
        "duration": " ".join(durations),
        # `text` keeps the human-readable characters; the model only indexes
        # `phoneme`, but keeping text makes the JSON debuggable by eye.
        "text": " ".join([tokens[0]] + [c for c in lyrics if c.strip()]),
        "phoneme": " ".join(tokens),
        "note_pitch": " ".join(pitches),
        "note_type": " ".join(types),
    }
    meta_out = {
        "segment": segment,
        "generated": meta,
        "notes": [
            {
                "char": n.char,
                "midi": n.midi,
                "start": n.start,
                "duration": n.duration,
                "pinyin": n.pinyin,
                "tone": n.tone,
            }
            for n in notes
        ],
    }
    return meta_out


def validate(segment: dict) -> dict:
    """Run the repo's own DataProcessor so a bad field fails here, not on GPU."""
    from omegaconf import OmegaConf

    from soulxsinger.utils.data_processor import DataProcessor

    cfg = OmegaConf.load(SOULX / "soulxsinger" / "config" / "soulxsinger.yaml")
    proc = DataProcessor(
        hop_size=cfg.audio.hop_size,
        sample_rate=cfg.audio.sample_rate,
        phoneset_path=str(SOULX / "soulxsinger" / "utils" / "phoneme" / "phone_set.json"),
        device="cpu",
    )
    item = proc.process(dict(segment), None)
    return {k: (tuple(v.shape) if hasattr(v, "shape") else v) for k, v in item.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lyrics")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tonic", type=int, default=60)
    ap.add_argument("--scale", default="major")
    ap.add_argument("--lo", type=int, default=60)
    ap.add_argument("--hi", type=int, default=72)
    ap.add_argument("--beat", type=float, default=0.42)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--index", default="whale_target")
    args = ap.parse_args()

    built = build(
        args.lyrics, tonic=args.tonic, scale=args.scale, lo=args.lo, hi=args.hi,
        beat=args.beat, seed=args.seed, index=args.index,
    )
    seg = built["segment"]
    shapes = validate(seg)
    print("DataProcessor OK:", shapes)
    print("tokens:", len(seg["phoneme"].split()), "duration_s:", seg["time"][1] / 1000)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([seg], ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
