"""Sentence-aware segmentation for streaming TTS.

The old splitter (``voice.segments.split_short_sentences``) hard-splits at the
character budget, which is right for a single-shot synthesis but wrong for
sentence streaming: cutting a grammatical sentence in half makes the prosody
fall apart, and the listener hears a mid-clause pause.

This module implements the three-case policy used by the live test:

1. A reply that is **one short sentence** (≤ ``max_chars``) is one segment.
   It is synthesized once; the output layer may still chunk the audio.
2. A reply that is **one long sentence** (> ``max_chars``) is also one segment.
   We never cut inside a sentence; a slightly longer utterance is better than
   a broken one.
3. A **multi-sentence reply** is packed greedily into segments of at most
   ``max_chars``. A sentence that alone exceeds the budget becomes its own
   segment (``long_sentence``) instead of being split.

The result is a deterministic plan: same text + same budget -> same segments.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..voice.segments import split_sentences, visible_len

__all__ = ["PlannedSegment", "plan_segments"]


@dataclass(frozen=True, slots=True)
class PlannedSegment:
    text: str
    chars: int
    # single_short | single_long | packed | long_sentence
    mode: str


def plan_segments(text: str, *, max_chars: int = 20) -> list[PlannedSegment]:
    """Return the generation-side segmentation plan for *text*."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    max_chars = max(1, int(max_chars))

    sentences = split_sentences(cleaned)
    if not sentences:
        return []

    if len(sentences) == 1:
        sentence = sentences[0]
        size = visible_len(sentence)
        mode = "single_short" if size <= max_chars else "single_long"
        return [PlannedSegment(sentence, size, mode)]

    segments: list[PlannedSegment] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        joined = "".join(buffer)
        mode = "packed" if len(buffer) > 1 else "single_short"
        segments.append(PlannedSegment(joined, visible_len(joined), mode))
        buffer.clear()

    for sentence in sentences:
        size = visible_len(sentence)
        if size > max_chars:
            flush()
            segments.append(PlannedSegment(sentence, size, "long_sentence"))
            continue
        if buffer and visible_len("".join(buffer + [sentence])) > max_chars:
            flush()
        buffer.append(sentence)
    flush()
    return segments
