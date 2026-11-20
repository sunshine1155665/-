"""Deterministic trip summaries (ADR-0014 §7.3).

The preferred path is one LLM JSON summary with a fixed schema.  This module
is the no-LLM fallback: it must always produce a usable summary and at least
one structured key fact, so a failed summarizer never loses the trip.

Privacy: the summary is written to owner-side memory (``origin_scope='roam'``)
and is *not* shown to the target room.  ``include_quotes`` stays off by
default; the fallback clips fragments instead of copying whole messages.
"""

from __future__ import annotations

from typing import Any

from .model import SummaryPayload


def _clean(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").replace("[图片]", " ").split())
    if limit > 0 and len(value) > limit:
        value = value[: max(1, limit - 1)] + "…"
    return value


def deterministic_summary(
    turns: list[dict[str, Any]],
    *,
    purpose: str = "",
    display_name: str = "",
    max_chars: int = 600,
    key_fact_limit: int = 3,
    include_quotes: bool = False,
) -> SummaryPayload:
    """Build a summary from the trip's own turns only.

    ``turns`` are rows from ``conversation_turns`` tagged with this trip.  User
    turns are the other side; assistant turns are her own words.  When the
    other side never replied, the summary says so instead of inventing content.
    """
    user_lines: list[str] = []
    assistant_lines: list[str] = []
    for turn in turns or []:
        role = str(turn.get("role") or "")
        text = _clean(turn.get("content"), 120)
        if not text:
            continue
        if role == "user":
            user_lines.append(text)
        elif role == "assistant":
            assistant_lines.append(text)

    who = display_name or "对方"
    parts: list[str] = []
    if purpose:
        parts.append(f"这趟的目的是：{_clean(purpose, 80)}")
    if user_lines:
        latest = user_lines[-1]
        parts.append(f"聊到：{_clean(latest, 90)}")
    elif assistant_lines:
        parts.append(f"已经和{who}打过招呼，对方还没回话")
    else:
        parts.append(f"去了{who}那里，没有聊到新内容")
    summary = "；".join(parts).rstrip("。") + "。"
    summary = _clean(summary, max_chars)

    key_facts: list[str] = []
    for line in user_lines[-key_fact_limit:]:
        # Structured paraphrase, not a verbatim transcript: the owner gets
        # "what was said about", the raw words stay in the room.
        fact = _clean(line if include_quotes else f"{who}提到：{line}", 90)
        if fact and fact not in key_facts:
            key_facts.append(fact)

    topics: list[str] = []
    for line in user_lines[-3:]:
        topic = _clean(line, 24)
        if topic and topic not in topics:
            topics.append(topic)

    return SummaryPayload(
        summary=summary,
        topics=topics,
        key_facts=key_facts,
        mood="",
        report_to_owner=summary,
        degraded=True,
    )


__all__ = ["deterministic_summary"]
