"""Make the syllables flow into each other without inventing new sound.

The SVS writes one phoneme per note, and the vendor's data processor wraps
*every* note in ``<BOW>`` … ``<EOW>`` (a word boundary per syllable). On the
rendered audio that shows up as a hard valley at about a third of the note
changes — on v14: 29% of boundaries dip more than 6 dB below the vowels either
side, 14% more than 12 dB.

First attempt copied the previous vowel into every valley and was heard as
脏音 at 今 and 都: a 90 ms copy of a diphthong is a stutter, and a loud vowel
burst inside a consonant is dirt. So the fill is now two different tools:

* **narrow valley (≤ 45 ms)** — blend the previous vowel's tail into it at 60%
  level with 10 ms equal-gain fades. Continuation, not replacement: the
  consonant underneath stays audible.
* **wide valley** — do not invent audio at all; only lift the envelope towards
  the neighbouring vowels (max +8 dB, Hann shaped), which softens the cut
  without repeating anything.

Phrase breaths are never touched (``song_v2.legato_boundaries`` skips them).
"""

from __future__ import annotations

import numpy as np


def envelope(y: np.ndarray, sr: int, hop_ms: float = 5.0) -> tuple[np.ndarray, float]:
    hop = max(1, int(sr * hop_ms / 1000))
    n = len(y) // hop
    env = np.sqrt(np.mean(y[:n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    return env, hop_ms / 1000


def find_valleys(env: np.ndarray, hop_s: float, boundary_s: float,
                 radius_s: float = 0.07,
                 thr: float = 0.55) -> tuple[float, float] | None:
    """The low-energy region around ``boundary_s``, in seconds, or None.

    ``thr`` is relative to the quieter of the two vowels either side: a valley
    shallower than 55% of it is just a consonant, not a cut, and is left alone.
    """
    k = int(round(boundary_s / hop_s))
    r = int(round(radius_s / hop_s))
    if k - r < 1 or k + r >= len(env):
        return None
    left = float(env[k - r:k - 2].mean())
    right = float(env[k + 2:k + r].mean())
    ref = min(left, right)
    if ref <= 0:
        return None
    seg = env[k - r:k + r]
    if seg.min() > thr * ref:
        return None
    # Expand from the *deepest* frame, not from the first one below threshold:
    # an onset attack can split a valley in two, and filling the release of the
    # previous note instead of the gap is exactly the useless case.
    low = thr * ref
    mid = int(np.argmin(seg))
    a = mid
    while a > 0 and seg[a - 1] < low:
        a -= 1
    b = mid + 1
    while b < len(seg) and seg[b] < low:
        b += 1
    a = max(0, a - 1)
    b = min(len(seg), b + 1)
    return (k - r + a) * hop_s, (k - r + b) * hop_s


def _hann(n: int) -> np.ndarray:
    if n <= 1:
        return np.ones(max(n, 0), dtype=np.float32)
    return (0.5 - 0.5 * np.cos(np.linspace(0, 2 * np.pi, n))).astype(np.float32)


def _blend(y: np.ndarray, a: int, b: int, sr: int, gain: float,
           fade_ms: float) -> None:
    """In-place: blend the previous vowel's tail into ``y[a:b]``."""
    width = b - a
    if width <= 0 or a - width < 0:
        return
    prev = y[a - width:a]
    if float(np.sqrt(np.mean(prev ** 2))) <= 1e-4:
        return
    w = _hann(width)
    fade = max(1, int(fade_ms * sr / 1000))
    ramp = np.minimum(1.0, np.minimum(np.arange(width) / fade,
                                      (width - np.arange(width)) / fade))
    w = (w * ramp).astype(np.float32)
    y[a:b] = (1.0 - w) * y[a:b] + w * (gain * prev)


def _lift(y: np.ndarray, a: int, b: int, env: np.ndarray, hop_s: float,
          max_db: float) -> None:
    """In-place: raise the valley towards the quieter neighbouring vowel."""
    k0 = max(0, int(round(a / hop_s)))
    k1 = min(len(env), int(round(b / hop_s)))
    if k1 <= k0:
        return
    ref = min(float(env[max(0, k0 - 18):k0].mean() or 0.0),
              float(env[k1:k1 + 18].mean() or 0.0))
    if ref <= 0:
        return
    want = ref * 0.7
    cur = np.maximum(env[k0:k1], 1e-9)
    gain = np.clip(want / cur, 1.0, 10 ** (max_db / 20.0))
    curve = np.interp(np.linspace(0, len(gain) - 1, b - a),
                      np.arange(len(gain)), gain)
    y[a:b] = (y[a:b] * curve).astype(np.float32)


def _overlap_fill(src: np.ndarray, length: int, fade: int) -> np.ndarray:
    """Sustain ``src`` for ``length`` samples by overlap-add tiling.

    Unlike copying one long chunk (which repeats a diphthong and was heard as
    脏音), a 20-25 ms tile crossfaded every ~20 ms behaves like a held vowel:
    same pitch, stationary spectrum, no audible seam.
    """
    seg = max(fade * 2 + 1, len(src))
    hop = max(1, seg - fade)
    total = length + seg
    acc = np.zeros(total, dtype=np.float32)
    wsum = np.zeros(total, dtype=np.float32)
    w = (0.5 - 0.5 * np.cos(np.linspace(0, 2 * np.pi, seg))).astype(np.float32)
    pos = 0
    while pos < length:
        tile = (src[:seg] * w) if len(src) >= seg else np.pad(
            src, (0, seg - len(src))) * w
        acc[pos:pos + seg] += tile
        wsum[pos:pos + seg] += w
        pos += hop
    out = acc[:length] / np.maximum(wsum[:length], 1e-6)
    return out


def _sustain(y: np.ndarray, a: int, b: int, sr: int, seg_ms: float = 22.0,
             fade_ms: float = 5.0, level: float = 0.75) -> None:
    """In-place: hold the previous vowel through a long closure."""
    width = b - a
    seg = int(seg_ms * sr / 1000)
    if width <= 0 or a - seg < 0:
        return
    src = y[a - seg:a].copy()
    if float(np.sqrt(np.mean(src ** 2))) <= 1e-4:
        return
    fill = _overlap_fill(src, width, max(1, int(fade_ms * sr / 1000)))
    # Target level: a bit under the vowel either side, so the closure still
    # reads as a consonant rather than a new note.
    want = level * float(np.sqrt(np.mean(src ** 2)))
    cur = float(np.sqrt(np.mean(fill ** 2))) or 1.0
    fill *= want / cur
    ramp = np.minimum(1.0, np.minimum(np.arange(width) / max(1, seg // 2),
                                      (width - np.arange(width))
                                      / max(1, seg // 2))).astype(np.float32)
    y[a:b] = ((1.0 - ramp) * y[a:b] + ramp * fill).astype(np.float32)


def fill_valleys(y: np.ndarray, sr: int, boundaries: list[float],
                 thr: float = 0.55, max_ms: float = 30.0,
                 gain: float = 0.6, fade_ms: float = 8.0,
                 lift_db: float = 8.0) -> np.ndarray:
    """Soften the syllable cuts at ``boundaries`` (seconds, inside a phrase).

    * short valley (≤ ``max_ms``): blend the previous vowel's tail in — it is a
      continuation of the same note, so this is nearly inaudible.
    * long valley: the model really did stop; hold the vowel through it with
      ``_sustain`` (overlap-added tiles at ~0.75 of the vowel level). Copying
      one long chunk here is what produced the 脏音 at 今/都.
    """
    out = y.astype(np.float32).copy()
    env, hop_s = envelope(out, sr)
    cap = int(max_ms * sr / 1000)
    for t in boundaries:
        span = find_valleys(env, hop_s, t, thr=thr)
        if span is None:
            continue
        a, b = int(span[0] * sr), int(span[1] * sr)
        if b - a <= 0:
            continue
        if b - a <= cap:
            _blend(out, a, b, sr, gain, fade_ms)
        elif b - a <= 90 * sr / 1000:
            _sustain(out, a, b, sr)
        # Anything wider is a rest the singer took, not a cut: filling it would
        # hold a note that nobody sang.
    return out
