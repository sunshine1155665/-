"""IndexTTS-2.5 tone -> emotion vector / reference mapping.

IndexTTS-2.5 separates *who is speaking* (the reference clip) from *how it is
spoken* (an 8-D emotion vector).  This module is the single source of truth for
both halves, so the server and any offline evaluation cannot drift apart.

The vectors below are the recipes that were validated on 2.5 with the eight
scene lines (``data/tts25_emotion_test``), not the old 2.0 table.  2.5 applies a
per-axis bias (``calm`` is multiplied by 0.5625), so copying the 2.0 numbers
would make ``soft`` nearly disappear.

Invariants kept from the v6 work:

* ``neutral`` uses no vector at all.  The reference clip's own prosody is the
  most natural neutral.
* ``annoyed`` is the API label for 吃醋/委屈/嘴硬, not real anger.  Sad + happy
  + calm keeps the "哼" a cute pout instead of an adult scold.
* The total stays at or below 0.8; the model would scale it down anyway.

The v6 identity anchor is ``cand_home_up1.wav`` for every tone.  That clip was
produced offline from the original recording **already +2 semitones up**, so
the pipeline must *not* add another shift on top: ``tts.pitch_semitones``
defaults to 0 and the server only re-enables the asetrate+atempo overlay for a
reference recorded at the original pitch (2026-10-25: stacking +2 on the
pre-shifted reference produced +4 and the user rejected the squeaky pitch).
"""

from __future__ import annotations

from .instruct import normalize_tone

# IndexTTS expects this order.  Keep it next to the vectors so a future order
# change is a one-line edit, not a silent channel swap.
EMOTION_ORDER: tuple[str, ...] = (
    "happy",
    "angry",
    "sad",
    "afraid",
    "disgusted",
    "melancholic",
    "surprised",
    "calm",
)

# Sparse weights; missing emotions are zero.  Empty = let the model speak with
# the reference's natural prosody.
TONE_VECTORS: dict[str, dict[str, float]] = {
    "neutral": {},
    "happy": {"happy": 0.55, "surprised": 0.12, "calm": 0.08},
    "soft": {"happy": 0.45, "melancholic": 0.05, "calm": 0.25},
    "sad": {"happy": 0.12, "sad": 0.38, "melancholic": 0.07, "calm": 0.18},
    # 吃醋/嘴硬，不是真生气。angry 永远是 0。
    "annoyed": {"happy": 0.22, "sad": 0.30, "surprised": 0.03, "calm": 0.12},
    "lively": {"happy": 0.58, "surprised": 0.25, "calm": 0.05},
}

# The v6 identity anchor.  Kept as a dict so callers that ask per tone keep the
# same shape, but every tone resolves to the same file now.
V6_REFERENCE = "cand_home_up1.wav"
TONE_REFS: dict[str, str] = {tone: V6_REFERENCE for tone in TONE_VECTORS}

INTERVAL_SILENCE_MS = 120
# 参考音 cand_home_up1.wav 离线制作时已经 +2 半音（v6 身份锚点）。管线再叠一次
# 就是 +4，音高过头 —— 所以叠加升调改成配置 ``tts.pitch_semitones``，默认 0 =
# 不叠加。只有换回原调参考音时才把它设回 2，机制仍是 asetrate 提频 + atempo
# 时长还原（见 post_filter_for 与 docs/branches/voice.md）。
DEFAULT_PITCH_SEMITONES = 0


def post_filter_for(pitch_semitones: int, sample_rate: int = 22050) -> str | None:
    """FFmpeg filter for a +N semitone shift, or ``None`` when disabled.

    ``+N`` semitones: speed the samples up by ``2**(N/12)`` (``asetrate``), then
    time-stretch back with ``atempo`` so only the pitch moves.  ``0`` (and any
    non-positive value) means no overlay — the production reference
    ``cand_home_up1.wav`` is already +2 up, so production runs at 0.
    """
    semitones = int(pitch_semitones)
    if semitones <= 0:
        return None
    factor = f"{2.0 ** (semitones / 12.0):.3f}"
    rate = int(sample_rate)
    return f"asetrate={rate}*{factor},aresample={rate},atempo=1/{factor}"


# Cache namespace.  Bumped whenever the engine/reference changes so a 2.0 WAV
# is never replayed as if it were 2.5.  The pitch overlay is NOT baked in here:
# the client cache key carries ``tts.pitch_semitones`` directly, so changing it
# invalidates cached audio without a manual version bump.
CACHE_VERSION = "v6-25"


def vector_for(tone: object) -> list[float] | None:
    """Return the 8-D vector for ``tone``, or ``None`` for neutral.

    Unknown tones are normalized to neutral by :func:`normalize_tone`; the
    server must never reject a request just because the chat model invented a
    new emotion name.
    """
    weights = TONE_VECTORS[normalize_tone(tone)]
    if not weights:
        return None
    return [float(weights.get(name, 0.0)) for name in EMOTION_ORDER]


def reference_name_for(tone: object) -> str:
    """Reference filename (inside the refs directory) for ``tone``."""
    return TONE_REFS[normalize_tone(tone)]


def interval_silence_for(tone: object) -> int:
    """Inter-segment silence in milliseconds (currently tone-independent)."""
    normalize_tone(tone)  # validate/normalize for a stable API
    return INTERVAL_SILENCE_MS
