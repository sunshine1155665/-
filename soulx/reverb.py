"""A very small Schroeder reverb, so the vocal sits in a room instead of on top
of the track.

The renders are bone dry: the vocal, the plucks and the noise floor share one
flat space, which is a large part of why they read as "demo / 不自然". A real
mix has a tail behind the voice; 10–18% of a short plate is enough to glue the
two together without turning it into a church.

Four parallel feedback combs (different delays) into two series allpasses — the
classic Schroeder topology. Both are single-tap recursions, so they are written
as block updates (one numpy op per delay-length block) instead of a per-sample
Python loop: a 13 s vocal takes milliseconds.

Only numpy (the staging env already has it).
"""

from __future__ import annotations

import numpy as np

# Delay times in seconds. Primes-ish so the combs do not line up and ring at one
# pitch; 20-45 ms is a small room, which is what a close-mic'ed song wants.
_COMB_S = (0.0297, 0.0371, 0.0411, 0.0437)
_ALLPASS_S = (0.0050, 0.0017)


def _comb(x: np.ndarray, delay: int, feedback: float) -> np.ndarray:
    """``y[i] = x[i] + feedback * y[i - delay]``, block by block."""
    y = np.zeros_like(x)
    for start in range(0, len(x), delay):
        chunk = x[start:start + delay]
        n = len(chunk)
        prev = y[start - delay:start - delay + n] if start >= delay else 0.0
        y[start:start + n] = chunk + feedback * prev
    return y


def _allpass(x: np.ndarray, delay: int, g: float = 0.5) -> np.ndarray:
    """``y[i] = -g*x[i] + x[i-delay] + g*y[i-delay]``."""
    y = np.zeros_like(x)
    for start in range(0, len(x), delay):
        chunk = x[start:start + delay]
        n = len(chunk)
        if start >= delay:
            x_prev = x[start - delay:start - delay + n]
            y_prev = y[start - delay:start - delay + n]
        else:
            x_prev = y_prev = 0.0
        y[start:start + n] = -g * chunk + x_prev + g * y_prev
    return y


def reverb(x: np.ndarray, sr: int, feedback: float = 0.74,
           wet: float = 0.15) -> np.ndarray:
    """Return ``x`` with a short tail mixed in. ``wet`` 0 = unchanged."""
    if wet <= 0:
        return x
    mono = x if x.ndim == 1 else x.mean(axis=1)
    acc = np.zeros_like(mono)
    for s in _COMB_S:
        acc += _comb(mono, max(1, int(s * sr)), feedback)
    acc /= len(_COMB_S)
    for s in _ALLPASS_S:
        acc = _allpass(acc, max(1, int(s * sr)))
    # A real room does not answer before the sound; fade the first 50 ms so the
    # comb's pre-echo does not smear the consonant.
    pre = min(len(acc), int(0.05 * sr))
    if pre:
        acc[:pre] *= np.linspace(0.0, 1.0, pre)
    peak = float(np.max(np.abs(acc))) or 1.0
    acc = acc / peak * (float(np.max(np.abs(mono))) or 1.0)
    if x.ndim == 1:
        return (1.0 - wet) * x + wet * acc
    return (1.0 - wet) * x + wet * acc[:, None]


def main() -> int:
    import argparse

    import soundfile as sf

    ap = argparse.ArgumentParser(description="给一段人声加一点点房间感")
    ap.add_argument("path")
    ap.add_argument("--out", default=None)
    ap.add_argument("--wet", type=float, default=0.15)
    args = ap.parse_args()
    y, sr = sf.read(args.path, dtype="float32", always_2d=False)
    out = reverb(y, sr, wet=args.wet)
    dst = args.out or args.path.replace(".wav", "_verb.wav")
    sf.write(dst, out, sr, subtype="PCM_16")
    print("wrote", dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
