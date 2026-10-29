"""Match the production voice: transpose the SVS output up 2 semitones.

Two things clicked when the reference clip was finally the right one:

1. ``fatfish/tts/indextts.py`` sets ``PITCH_SEMITONES = 2`` and applies
   ``asetrate=22050*1.122, aresample, atempo=1/1.122`` to every TTS output. So the
   voice the user actually knows is ``cand_home_up1`` **plus two semitones**.
   Our SVS was serving the unshifted version.

2. That same operation is how you get a child-ish timbre without artefacts:
   resample first (pitch *and* formants move together, like a real transposition),
   then stretch the duration back. No WORLD analysis, no phase-vocoder formant
   warp — which is where the "电音" came from.

So: drop ``childify`` for this route and use varispeed +11.22%, then time-stretch
back to the original length.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

SR = 24000
FACTOR = 2 ** (2 / 12)      # +2 semitones == 1.122, same as production


def transpose(path_in, path_out, semitones: float = 2.0) -> None:
    import librosa

    y, sr = librosa.load(str(path_in), sr=SR, mono=True)
    factor = 2 ** (semitones / 12)

    # 1) resample: this raises pitch and shifts the formants with it
    n_out = int(len(y) / factor)
    idx = np.arange(n_out) * factor
    i0 = np.floor(idx).astype(int)
    i1 = np.minimum(i0 + 1, len(y) - 1)
    frac = idx - i0
    shifted = (y[i0] * (1 - frac) + y[i1] * frac).astype(np.float32)

    # 2) stretch back to the original duration (formants stay where they moved)
    import librosa as _lb
    restored = _lb.effects.time_stretch(shifted, rate=1.0 / factor)

    peak = float(np.max(np.abs(restored))) or 1.0
    sf.write(str(path_out), (restored / peak * 0.92).astype(np.float32), SR,
             subtype="PCM_16")


def report(path_in, path_out) -> str:
    import librosa
    import pyworld as pw

    def med(p):
        y, _ = librosa.load(str(p), sr=SR, mono=True)
        f0, t = pw.harvest(y.astype(np.float64), SR, f0_floor=60, f0_ceil=1000,
                           frame_period=20.0)
        v = f0[f0 > 0]
        return float(np.median(v)) if v.size else 0.0

    return (f"{med(path_in):.0f} -> {med(path_out):.0f} Hz  "
            f"({len(librosa.load(str(path_in), sr=SR)[0])/SR:.2f}s -> "
            f"{len(librosa.load(str(path_out), sr=SR)[0])/SR:.2f}s)")


if __name__ == "__main__":
    import sys

    src, dst = sys.argv[1], sys.argv[2]
    st = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    transpose(src, dst, st)
    print("wrote", dst, "|", report(src, dst))
