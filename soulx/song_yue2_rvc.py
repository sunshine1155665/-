#!/usr/bin/env python3
"""YuE2 full song -> Demucs -> RVC whale voice, as a SingService renderer.

Drop-in for ``fatfish.sing.SingService``: accepts the same ``--emotion``,
``--out``, ``--src``, ``--semitones``, ``--prompt-wav`` and ``--prompt-meta``
flags, and writes ``<out>/full.wav``.

This script is deliberately outside ``fatfish/`` (the app stays stdlib-only):
it runs in ``.venv-soulx`` and shells out to the YuE2 and RVC environments.
The caller owns the GPU lease; this script does not take it again.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:  # optional at import time so the pure CLI helpers are testable in CI
    import librosa
    import numpy as np
    import soundfile as sf
except ImportError:  # pragma: no cover - the renderer itself needs these
    librosa = None
    np = None
    sf = None

ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


# All external engine paths come from the environment; no local absolute paths
# are committed.  ``_require_paths`` fails with a readable message if missing.
YUE2_ROOT = _env_path("YUE2_ROOT")
YUE2_EXE = YUE2_ROOT / ".venv" / "Scripts" / "yue2.exe" if YUE2_ROOT else None
YUE2_MODEL = YUE2_ROOT / "models" / "YuE2-3B" if YUE2_ROOT else None
YUE2_VAE = YUE2_ROOT / "models" / "YuE2-Vae" if YUE2_ROOT else None
RVC_ROOT = _env_path("RVC_ROOT")
RVC_PY = RVC_ROOT / ".venv-train" / "Scripts" / "python.exe" if RVC_ROOT else None
RVC_CONV = RVC_ROOT / "convert_audio.py" if RVC_ROOT else None
RVC_PTH = _env_path("RVC_PTH")
RVC_INDEX = _env_path("RVC_INDEX")
SR = 48000

STYLES = {
    # 2026-11-12: 三个风格统一以"甜"为底色。此前 soft 是 warm ballad，和 TTS 的
    # soft（happy .45 / calm .25，本身带甜）语义不对齐；实测甜系 prompt 换音后
    # sim 0.890→0.924、质心 2193→2526。旧描述见 .backup_tts_tones 的同名备份。
    "soft": ("Mandarin, sweet honeyed pop ballad, soft piano and light strings, 72 BPM, "
             "young female vocal, sweet smiling tone, breathy, playful, gentle vibrato, "
             "close-mic, clear diction, narrow range, no high notes"),
    "genki": ("Mandarin, bright upbeat pop, acoustic guitar and light percussion, 108 BPM, "
              "young female vocal, sweet smiling tone, honeyed, playful, energetic, "
              "clear diction, narrow range, no high notes, open"),
    "sad": ("Mandarin, slow melancholic ballad, piano and soft strings, 60 BPM, "
            "young female vocal, sweet fragile tone, breathy, gentle, intimate, "
            "close-mic, clear diction, narrow range, no high notes"),
}


def _require_paths() -> None:
    missing = [
        name for name, value in (
            ("YUE2_ROOT", YUE2_ROOT), ("YUE2_EXE", YUE2_EXE),
            ("YUE2_MODEL", YUE2_MODEL), ("YUE2_VAE", YUE2_VAE),
            ("RVC_ROOT", RVC_ROOT), ("RVC_PY", RVC_PY), ("RVC_CONV", RVC_CONV),
            ("RVC_PTH", RVC_PTH), ("RVC_INDEX", RVC_INDEX),
        ) if value is None or not value.exists()
    ]
    if missing:
        raise SystemExit(
            "renderer paths missing: set "
            + ", ".join(missing)
            + " (see config/low.toml [sing] and README)"
        )


def _require_audio() -> None:
    if librosa is None or np is None or sf is None:
        raise SystemExit(
            "audio dependencies missing: run this script with .venv-soulx "
            "(librosa/numpy/soundfile)"
        )


#: Section tags a complete song may carry (mirror of fatfish.sing.service).
_SECTION_TAGS = frozenset(
    {"verse", "chorus", "bridge", "intro", "outro", "hook", "refrain",
     "pre-chorus", "prechorus"}
)


def _is_section_tag(line: str) -> bool:
    text = line.strip()
    if not (text.startswith("[") and text.endswith("]") and len(text) > 2):
        return False
    return text[1:-1].strip().lower().replace(" ", "") in _SECTION_TAGS


def read_lyrics(src: Path) -> tuple[str, list[str]]:
    """Read the lyrics JSON; no audio deps.

    ADR-0018: custom songs carry ``lyrics`` — a *complete* song, 4–16 sung
    lines (section tags kept, not counted).  The 4-line default song only has
    ``phrases``.  Both end up as one lyrics block for YuE2.
    """
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    if isinstance(data.get("lyrics"), list):
        lines = [str(x).strip() for x in data["lyrics"]]
    else:
        lines = [str(p.get("text", "")).strip() for p in data.get("phrases", [])]
    lines = [x for x in lines if x]
    sung = [x for x in lines if not _is_section_tag(x)]
    if not 4 <= len(sung) <= 16:
        raise SystemExit(f"need 4-16 sung lyric lines, got {len(sung)}")
    return str(data.get("title") or "点歌"), lines


def build_request(emotion: str, lines: list[str], *, song_id: str, seed: int) -> dict:
    """The exact YuE2 request; kept pure so tests never load a model."""
    body = "\n".join(lines)
    if not any(_is_section_tag(line) for line in lines):
        body = "[Verse]\n" + body
    return {
        "id": song_id,
        "style": STYLES[emotion],
        "lyrics": body,
        "cot": "full",
        "seed": int(seed),
    }


def song_id_for(out: Path) -> str:
    cleaned = "".join(ch for ch in (out.name or "sing") if ch.isalnum() or ch in "-_")[:40]
    return cleaned or "sing"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emotion", default="soft", choices=sorted(STYLES))
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--semitones", type=float, default=0.0)
    ap.add_argument("--prompt-wav", default="")
    ap.add_argument("--prompt-meta", default="")
    ap.add_argument("--index-rate", type=float, default=0.60)
    ap.add_argument("--protect", type=float, default=0.50)
    ap.add_argument("--seed", type=int, default=None)
    return ap.parse_args(argv)


def run(cmd: list[str], *, timeout: float, env: dict[str, str] | None = None) -> str:
    print("+ " + " ".join(str(x) for x in cmd), flush=True)
    t = time.time()
    proc = subprocess.run(
        [str(x) for x in cmd], capture_output=True, text=True,
        timeout=timeout, env=env, cwd=str(ROOT),
    )
    print(f"  exit={proc.returncode} {time.time() - t:.1f}s", flush=True)
    if proc.returncode != 0:
        sys.stderr.write((proc.stdout or "")[-2000:])
        sys.stderr.write((proc.stderr or "")[-4000:])
        raise SystemExit(f"step failed: {Path(str(cmd[0])).name} exit={proc.returncode}")
    return proc.stdout or ""


def f0_median(path: Path) -> float:
    y, sr = librosa.load(str(path), sr=22050, mono=True)
    f0, _, _ = librosa.pyin(y, fmin=65, fmax=1000, sr=sr, frame_length=2048)
    f0 = f0[np.isfinite(f0)]
    return float(np.median(f0)) if len(f0) else 0.0


def gate_vocal(src: Path, conv: Path, out: Path) -> None:
    """Zero the converted vocal outside the source's voiced span."""
    s, sr = librosa.load(str(src), sr=SR, mono=True)
    c, _ = librosa.load(str(conv), sr=SR, mono=True)
    n = min(len(s), len(c))
    s, c = s[:n], c[:n]
    hop = int(0.02 * sr)
    rms = librosa.feature.rms(y=s, frame_length=4 * hop, hop_length=hop).ravel()
    thr = max(1e-5, float(np.max(rms)) * 0.004)
    idx = np.where(rms > thr)[0]
    if len(idx) == 0:
        sf.write(str(out), c, SR, subtype="PCM_16")
        return
    a = max(0, idx[0] - 4)
    b = min(len(rms) - 1, idx[-1] + 4)
    start, end = a * hop, min(n, (b + 1) * hop)
    g = np.zeros(n, dtype=np.float32)
    g[start:end] = 1.0
    fade = int(0.02 * sr)
    if start > 0:
        k = min(fade, start)
        g[start - k:start] = np.linspace(0, 1, k)
    if end < n:
        k = min(fade, n - end)
        g[end:end + k] = np.linspace(1, 0, k)
    sf.write(str(out), c * g, SR, subtype="PCM_16")


def mix(vocal: Path, inst: Path, out: Path, vg: float = 0.85, ig: float = 0.55) -> None:
    v, _ = librosa.load(str(vocal), sr=SR, mono=True)
    i, _ = librosa.load(str(inst), sr=SR, mono=False)
    if i.ndim == 1:
        i = np.stack([i, i])
    n = min(len(v), i.shape[1])
    m = v[:n] * vg + i[:, :n] * ig
    peak = float(np.max(np.abs(m))) if n else 0.0
    if peak > 0.99:
        m = m / peak * 0.98
    sf.write(str(out), m.T, SR, subtype="PCM_16")


def main(argv: list[str] | None = None) -> int:
    _require_audio()
    _require_paths()
    args = parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    title, lines = read_lyrics(Path(args.src))
    song_id = song_id_for(out)

    timings: dict[str, float] = {}
    t0 = time.time()
    seed = args.seed if args.seed is not None else int(time.time()) % 2_000_000_000
    req = build_request(args.emotion, lines, song_id=song_id, seed=seed)
    (out / "request.json").write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

    t = time.time()
    run([YUE2_EXE, "generate", "--request", out / "request.json",
         "--model", YUE2_MODEL, "--vae", YUE2_VAE, "--device", "cuda",
         "--budget", "8", "--low-vram", "--quantization", "fp8",
         "--output", out / "yue2", "--no-progress"], timeout=600)
    timings["yue2_s"] = round(time.time() - t, 1)
    audio = out / "yue2" / song_id / "audio.flac"
    if not audio.is_file():
        raise SystemExit(f"yue2 produced no audio: {audio}")

    t = time.time()
    env = os.environ.copy()
    env.setdefault("HF_HUB_OFFLINE", "1")
    run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
         "--shifts", "2", "--overlap", "0.5", "-o", out / "demucs", audio],
        timeout=300, env=env)
    timings["demucs_s"] = round(time.time() - t, 1)
    src_v = out / "demucs" / "htdemucs" / "audio" / "vocals.wav"
    inst = out / "demucs" / "htdemucs" / "audio" / "no_vocals.wav"
    if not (src_v.is_file() and inst.is_file()):
        raise SystemExit("demucs produced no stems")

    src_f0 = f0_median(src_v)
    pitch = int(round(args.semitones))
    t = time.time()
    run([RVC_PY, RVC_CONV, "--pth", RVC_PTH, "--index", RVC_INDEX,
         "--src", src_v, "--out", out / "whale_raw.wav",
         "--pitch", str(pitch), "--index-rate", str(args.index_rate),
         "--protect", str(args.protect), "--f0-method", "rmvpe"], timeout=180)
    timings["rvc_s"] = round(time.time() - t, 1)

    gate_vocal(src_v, out / "whale_raw.wav", out / "whale_gated.wav")
    mix(out / "whale_gated.wav", inst, out / "full.wav")
    timings["total_s"] = round(time.time() - t0, 1)

    report = {
        "title": title,
        "lyrics": lines,
        "emotion": args.emotion,
        "song_id": song_id,
        "pitch": pitch,
        "source_f0": round(src_f0, 1),
        "index_rate": args.index_rate,
        "protect": args.protect,
        "timings": timings,
        "audio": str(out / "full.wav"),
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
