"""Run SoulX-Singer SVS (score mode) with our own metadata, and report cost.

Deliberately does not modify the vendored repo: everything is passed in through
``cli.inference``'s public functions, so pulling upstream later stays painless.

Why score mode: ``SoulXSinger.infer`` sets ``gt_f0 = pt_f0 = None`` when
``control == "score"``, i.e. pitch comes only from ``note_pitch``. That is
exactly our situation — we authored the notes in ``text2melody.py``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOULX = ROOT / "vendor" / "SoulX-Singer"
sys.path.insert(0, str(SOULX))


def _patch_audio_io() -> None:
    """torchaudio 2.11 delegates load() to torchcodec, which is not installed.

    ``soulxsinger.utils.data_processor`` does ``from ...audio_utils import
    load_wav`` at module import, so the attribute has to be replaced *before*
    that module is imported — otherwise the old symbol stays bound.
    """
    import numpy as np
    import soundfile as sf
    import torch

    import soulxsinger.utils.audio_utils as audio_utils

    def load_wav(wav_path: str, sample_rate: int):
        data, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
        waveform = torch.from_numpy(np.ascontiguousarray(data.T))
        if sr != sample_rate:
            import torchaudio

            waveform = torchaudio.functional.resample(waveform, sr, sample_rate)
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        return waveform

    audio_utils.load_wav = load_wav


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-wav", required=True)
    ap.add_argument("--prompt-meta", required=True)
    ap.add_argument("--target-meta", required=True)
    ap.add_argument("--save-dir", required=True)
    ap.add_argument("--control", default="score", choices=["score", "melody"])
    ap.add_argument("--pitch-shift", type=int, default=0)
    ap.add_argument("--auto-shift", action="store_true")
    ap.add_argument("--fp16", action="store_true")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    import torch

    _patch_audio_io()

    from cli.inference import build_model, process
    from soulxsinger.utils.file_utils import load_config

    cfg_path = SOULX / "soulxsinger" / "config" / "soulxsinger.yaml"
    model_path = SOULX / "pretrained_models" / "SoulX-Singer" / "model.pt"
    phoneset = SOULX / "soulxsinger" / "utils" / "phoneme" / "phone_set.json"

    if not model_path.is_file():
        raise SystemExit(f"weights missing: {model_path}")

    config = load_config(str(cfg_path))

    t0 = time.perf_counter()
    model = build_model(
        model_path=str(model_path),
        config=config,
        device=args.device,
        use_fp16=args.fp16,
    )
    load_s = time.perf_counter() - t0

    class Args:
        pass

    a = Args()
    a.device = args.device
    a.prompt_wav_path = args.prompt_wav
    a.prompt_metadata_path = args.prompt_meta
    a.target_metadata_path = args.target_meta
    a.phoneset_path = str(phoneset)
    a.save_dir = args.save_dir
    a.control = args.control
    a.auto_shift = args.auto_shift
    a.pitch_shift = args.pitch_shift
    a.use_fp16 = args.fp16

    t1 = time.perf_counter()
    process(a, config, model)
    infer_s = time.perf_counter() - t1

    if torch.cuda.is_available():
        peak = torch.cuda.max_memory_allocated() / 1024 ** 3
        print(f"vram_peak_alloc {peak:.2f} GiB")
    print(f"load {load_s:.1f}s  infer {infer_s:.1f}s")
    out = Path(args.save_dir) / "generated.wav"
    print("out", out, out.stat().st_size if out.is_file() else "MISSING")
    return 0


if __name__ == "__main__":
    sys.exit(main())
