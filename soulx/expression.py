"""Performance cues that the score cannot express: breaths and a breathy onset.

The SVS sings each line starting exactly on the beat with no inhale, which reads
as "she is reading a chart". Real singing has an audible breath before a phrase
and a little air on the note after it. ``breath`` synthesises a soft inhale just
before each phrase start; :func:`airy` mixes a hint of band-limited noise into
the vocal so the tone is less "clean synth" and more close-mic'ed.

Everything is numpy-only and unit-tested on synthetic buffers.
"""

from __future__ import annotations

import numpy as np


def inhale(sr: int, dur_ms: float = 150.0, seed: int = 7) -> np.ndarray:
    """One breath: band-limited noise with a slow rise and a quick release."""
    n = max(16, int(sr * dur_ms / 1000))
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n).astype(np.float32)
    # Crude band-pass: difference (high-pass ~300 Hz) then a 3-tap smoother
    # (low-pass ~4 kHz). Good enough for noise that sits under the mix.
    hp = np.diff(noise, prepend=0.0)
    lp = np.convolve(hp, np.ones(3, dtype=np.float32) / 3.0, mode="same")
    t = np.linspace(0.0, 1.0, n)
    env = np.sin(np.pi * np.clip(t / 0.75, 0.0, 1.0)) ** 1.4
    env[int(0.75 * n):] *= np.linspace(1.0, 0.0, n - int(0.75 * n))
    out = (lp * env).astype(np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return out / peak


def breath(n_samples: int, sr: int, starts: list[float], level: float = 0.1,
           dur_ms: float = 150.0, lead_ms: float = 40.0,
           seed: int = 7) -> np.ndarray:
    """A breath that ends ``lead_ms`` before each phrase start."""
    out = np.zeros(n_samples, dtype=np.float32)
    if level <= 0:
        return out
    for i, s in enumerate(starts):
        b = inhale(sr, dur_ms=dur_ms, seed=seed + i)
        end = int((s - lead_ms / 1000.0) * sr)
        a = end - len(b)
        if a < 0 or end > n_samples:
            continue
        out[a:end] += b * level
    return out


def airy(y: np.ndarray, sr: int, amount: float = 0.06,
         seed: int = 11) -> np.ndarray:
    """Add a little high-band noise, gated by the vocal's own envelope.

    Kept low (6%) on purpose: enough to take the sheen off the synth, not enough
    to hiss. The noise follows the vocal envelope, so it sits on phrases and
    disappears in the gaps.
    """
    if amount <= 0:
        return y
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(len(y)).astype(np.float32)
    noise = np.diff(noise, prepend=0.0)          # brighten
    hop = max(1, int(sr * 0.01))
    n = len(y) // hop
    env = np.sqrt(np.mean(y[:n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    env = np.interp(np.arange(len(y)), np.arange(n) * hop + hop // 2, env)
    env = np.clip(env / (float(env.max()) or 1.0), 0.0, 1.0)
    # ``amount`` is a fraction of the vocal's own RMS, so the number means the
    # same thing on a loud and a quiet take.
    rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2))) or 1.0
    return (y + amount * rms * noise * env).astype(np.float32)
