"""Give the sung line a human pitch contour: vibrato on held notes + slow drift.

A score-driven SVS sings each note at one exact frequency, and the ear reads
"dead flat" as machine-like even when the melody is fine. Real singing has

* **vibrato** — 5-6 Hz, ~15-30 cents, which fades in after the onset and
  belongs to the *end* of a held note, not its first 100 ms;
* **drift** — a slow ±5 cent wander that no human can switch off.

Both are pitch-only edits on the F0 contour, resynthesised with WORLD (which
keeps the spectral envelope, so the timbre does not move). ``note_envelope`` is
pure and unit-tested; ``humanize`` needs pyworld, which only exists in the
soullx venv.
"""

from __future__ import annotations

import numpy as np


def note_envelope(n_frames: int, frame_period_ms: float,
                  notes: list[tuple[float, float]],
                  ramp_s: float = 0.12) -> np.ndarray:
    """1.0 where vibrato should be at full depth, 0 elsewhere.

    ``notes`` are ``(start_s, duration_s)``. Vibrato ramps in over ``ramp_s``
    and is released over the last ``ramp_s`` of the note — a vibrato that starts
    on the consonant is exactly what makes fake singing sound fake.
    """
    env = np.zeros(n_frames, dtype=np.float32)
    for start, dur in notes:
        if dur < 0.25:               # too short to hold: skip
            continue
        a = int(round((start + ramp_s * 0.5) * 1000 / frame_period_ms))
        b = int(round((start + dur - ramp_s * 0.5) * 1000 / frame_period_ms))
        a = max(0, min(a, n_frames))
        b = max(a, min(b, n_frames))
        if b <= a:
            continue
        ramp = max(1, int(round(ramp_s * 1000 / frame_period_ms)))
        seg = np.ones(b - a, dtype=np.float32)
        up = min(ramp, len(seg))
        down = min(ramp, len(seg))
        seg[:up] = np.linspace(0.0, 1.0, up)
        seg[-down:] = np.minimum(seg[-down:], np.linspace(1.0, 0.0, down))
        env[a:b] = np.maximum(env[a:b], seg)
    return env


def contour(f0: np.ndarray, frame_period_ms: float, env: np.ndarray,
            cents: float = 18.0, hz: float = 5.2, drift_cents: float = 5.0,
            seed: int = 7, scoop_cents: float = 0.0, scoop_ms: float = 45.0,
            note_starts: list[float] | None = None) -> np.ndarray:
    """Add vibrato (scaled by ``env``), a slow drift and note-on scoops.

    ``scoop_cents`` is the pitch a singer approaches a note from: real voices
    slide up into a new note over 30-60 ms instead of switching instantly, and
    a hard switch is one of the "MIDI" tells. Unvoiced frames stay 0.
    """
    rng = np.random.default_rng(seed)
    n = len(f0)
    t = np.arange(n) * frame_period_ms / 1000.0
    # Phase is integrated so the rate is exact even if the frame period drifts.
    vib = np.sin(2 * np.pi * hz * t)
    # Drift: a smooth random walk (sum of two slow sines with random phase).
    ph = rng.uniform(0, 2 * np.pi, 2)
    drift = (np.sin(2 * np.pi * 0.27 * t + ph[0])
             + 0.6 * np.sin(2 * np.pi * 0.11 * t + ph[1])) / 1.6
    shift = (cents * env * vib + drift_cents * drift * (env > 0)) / 1200.0
    if scoop_cents > 0 and note_starts:
        span = max(1, int(round(scoop_ms / frame_period_ms)))
        ramp = np.linspace(-scoop_cents, 0.0, span) / 1200.0
        for s in note_starts:
            k = int(round(s * 1000 / frame_period_ms))
            if k < 0 or k >= n:
                continue
            m = min(span, n - k)
            shift[k:k + m] += ramp[:m]
    out = f0.astype(np.float64).copy()
    voiced = f0 > 0
    out[voiced] = f0[voiced] * np.power(2.0, shift[voiced])
    return out


def humanize(y: np.ndarray, sr: int, notes: list[tuple[float, float]],
             cents: float = 18.0, hz: float = 5.2, drift_cents: float = 5.0,
             seed: int = 7, scoop_cents: float = 0.0,
             scoop_ms: float = 45.0) -> np.ndarray:
    """WORLD re-synthesis of ``y`` with a human pitch contour."""
    import pyworld as pw

    frame_ms = 5.0
    f0, t = pw.harvest(np.ascontiguousarray(y, dtype=np.float64), sr,
                       f0_floor=60, f0_ceil=800, frame_period=frame_ms)
    sp = pw.cheaptrick(np.ascontiguousarray(y, dtype=np.float64), f0, t, sr)
    ap = pw.d4c(np.ascontiguousarray(y, dtype=np.float64), f0, t, sr)
    env = note_envelope(len(f0), frame_ms, notes)
    f0_new = contour(f0, frame_ms, env, cents=cents, hz=hz,
                     drift_cents=drift_cents, seed=seed,
                     scoop_cents=scoop_cents, scoop_ms=scoop_ms,
                     note_starts=[a for a, _ in notes])
    return pw.synthesize(f0_new, sp, ap, sr, frame_period=frame_ms).astype(np.float32)
