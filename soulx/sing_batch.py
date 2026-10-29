"""Batch SVS: sing many note lists in one process.

Loading the 704 M model costs ~9 s, so the flywheel must not pay that per clip.
This module loads once and then renders a queue, reusing the exact inference path
proven in ``run_svs.py`` (score mode, fp16, her TTS voice as the prompt).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Overridable so a task worktree (which has no vendor/) can render with the
# main checkout's model without copying 700 MB around.
SOULX = Path(os.environ.get("SOULX_ROOT",
                            str(ROOT / "vendor" / "SoulX-Singer")))
sys.path.insert(0, str(SOULX))
sys.path.insert(0, str(ROOT / "soulx"))

from build_target import _phonemes  # noqa: E402


def notes_to_target(notes, text: str, index: str, start_s: float = 0.0) -> dict:
    """v2 notes -> SoulX target metadata (score mode, so no f0 field).

    ``start_s`` places the segment on the output timeline. ``cli/inference.py``
    renders each metadata entry separately and adds it into one buffer at
    ``time``, so a list of these is how a long song gets built: one short
    reference clip, many segments, single continuous audio.
    """
    phones = _phonemes(text)
    if len(phones) != len(notes):
        raise SystemExit(f"{index}: {len(notes)} notes vs {len(phones)} phonemes")
    tokens, durs, pitches, types = ["<SP>"], ["0.24"], ["0"], ["1"]
    for n, ph in zip(notes, phones):
        tokens.append(ph)
        durs.append(f"{n.duration:.4f}")
        pitches.append(str(int(n.midi)))
        types.append("2")
    total = sum(float(d) for d in durs)
    start_ms = int(round(start_s * 1000))
    return {
        "index": index, "language": "Mandarin",
        "time": [start_ms, start_ms + int(round(total * 1000))],
        "duration": " ".join(durs),
        "text": " ".join(["<SP>"] + [c for c in text if c.strip()]),
        "phoneme": " ".join(tokens),
        "note_pitch": " ".join(pitches),
        "note_type": " ".join(types),
    }


class Singer:
    def __init__(self, prompt_wav: str, prompt_meta: str, *, fp16: bool = True,
                 device: str = "cuda", pitch_shift: int = 0,
                 n_steps: int | None = None, cfg: float | None = None):
        from run_svs import _patch_audio_io

        _patch_audio_io()
        import torch

        from cli.inference import build_model
        from soulxsinger.utils.file_utils import load_config

        self.torch = torch
        self.prompt_wav = str(ROOT / prompt_wav) if not Path(prompt_wav).is_absolute() else prompt_wav
        self.prompt_meta = str(ROOT / prompt_meta) if not Path(prompt_meta).is_absolute() else prompt_meta
        self.fp16 = fp16
        self.device = device
        # ``pitch_shift`` transposes the score inside the model, so the voice
        # keeps its formants. The old post-hoc resample (``transpose_post``)
        # moves the formants with the pitch, which is what made the +3 renders
        # sound younger than the reference.
        self.pitch_shift = int(pitch_shift)
        self.config = load_config(str(SOULX / "soulxsinger" / "config" / "soulxsinger.yaml"))
        # Flow-matching knobs: more steps = cleaner, cfg = how hard the model is
        # pushed to follow the score (lower is looser / more expressive).
        if n_steps is not None:
            self.config.infer.n_steps = int(n_steps)
        if cfg is not None:
            self.config.infer.cfg = float(cfg)
        self.n_steps = int(self.config.infer.n_steps)
        self.cfg = float(self.config.infer.cfg)
        t0 = time.perf_counter()
        self.model = build_model(
            model_path=str(SOULX / "pretrained_models" / "SoulX-Singer" / "model.pt"),
            config=self.config, device=device, use_fp16=fp16)
        self.load_s = time.perf_counter() - t0
        self.n = 0

    def sing(self, seg: dict, out_wav: Path) -> Path:
        return self.sing_multi([seg], out_wav)

    def sing_multi(self, segs: list[dict], out_wav: Path) -> Path:
        """Render N segments into one continuous wav (long-form support)."""
        from cli.inference import process

        out_wav = Path(out_wav)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        meta_path = out_wav.with_suffix(".json")
        meta_path.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")

        class A:
            pass

        a = A()
        a.device = self.device
        a.prompt_wav_path = self.prompt_wav
        a.prompt_metadata_path = self.prompt_meta
        a.target_metadata_path = str(meta_path)
        a.phoneset_path = str(SOULX / "soulxsinger" / "utils" / "phoneme" / "phone_set.json")
        a.save_dir = str(out_wav.parent)
        a.control = "score"
        a.auto_shift = False
        a.pitch_shift = self.pitch_shift
        a.use_fp16 = self.fp16

        with self.torch.no_grad():
            process(a, self.config, self.model)
        produced = out_wav.parent / "generated.wav"
        if produced != out_wav:
            produced.replace(out_wav)
        self.n += len(segs)
        return out_wav
