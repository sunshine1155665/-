"""Accompaniment: a plucked/bell voice instead of a sustained sine chord.

The first version held a triad as pure sines for the whole phrase. That is fine
as a tuning reference but it sounds like a test tone, and the user rightly asked
what else is possible. Nothing here needs a download or a soundfont — the
difference between "sine pad" and "music box" is:

  * harmonics (1, 2, 3, 4 at 1, .45, .22, .12) — a pure sine has only one, which
    is why it sounds synthetic;
  * a fast attack and exponential decay — struck, not bowed;
  * an arpeggio instead of a block chord, so the ear gets rhythm;
  * a bass note on the strong beats to mark the bar.

Later options, in order of cost: FluidSynth + a GM soundfont (~140 MB, real
instruments), or ACE-Step for a full arrangement. Both are beyond what a
"she hums you a little song" feature needs.
"""

from __future__ import annotations

import numpy as np

SR = 24000


def pluck(freq: float, dur: float, *, amp: float = 1.0,
          tau: float = 0.22, harmonics=(1.0, 0.45, 0.22, 0.12)) -> np.ndarray:
    n = max(1, int(dur * SR))
    t = np.arange(n) / SR
    wave = np.zeros(n, dtype=np.float64)
    for k, weight in enumerate(harmonics, start=1):
        wave += weight * np.sin(2 * np.pi * freq * k * t)
    env = np.exp(-t / tau) * np.minimum(1.0, t / 0.004)
    # short fade-out so a cut note never clicks
    fade = min(n, int(0.01 * SR))
    if fade:
        env[-fade:] *= np.linspace(1.0, 0.0, fade)
    return (wave * env * amp).astype(np.float32)


def midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def render(spans: list[tuple[float, float]], tonic: int,
           progression: list[tuple[int, str]],
           triad: dict[str, list[int]], total_s: float) -> np.ndarray:
    """One chord per phrase; arpeggio in eighths, bass on beats 1 and 3."""
    n = int(total_s * SR) + SR // 4
    out = np.zeros(n, dtype=np.float32)

    def add(buf: np.ndarray, at_s: float) -> None:
        start = int(at_s * SR)
        end = min(n, start + len(buf))
        if end <= start:
            return
        out[start:end] += buf[: end - start]

    step = 0.44                       # quarter notes: half as busy as before
    for (start, length), (root, quality) in zip(spans, progression):
        tones = [tonic + 12 + root + i for i in triad[quality]]
        # arpeggio: root, fifth, third, fifth -- keeps the figure moving
        order = [tones[0], tones[2], tones[1], tones[2]]
        k = 0
        t = start
        while t < start + length - 0.05:
            add(pluck(midi_hz(order[k % len(order)]), min(step * 2.2, 0.9),
                      amp=0.045), t)
            k += 1
            t += step
        # bass on the strong beats only
        beat = step * 2
        t = start
        while t < start + length - 0.05:
            add(pluck(midi_hz(tonic - 12 + root), min(beat * 2.4, 1.2),
                      amp=0.035, tau=0.45, harmonics=(1.0, 0.3, 0.1)), t)
            t += beat
    return out
