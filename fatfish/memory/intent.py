"""Cheap, deterministic detection of "ask her to recall something" turns.

The prompt needs to know whether the current message is a memory question: a
normal chat turn must not be told to answer "specifically" or to use the
missing-memory cute line.  This is a keyword fast path, not an NLU model --
the cost of a false positive is one extra instruction block, and a false
negative leaves the old behaviour untouched.
"""

from __future__ import annotations

# Phrases that only make sense when the speaker is pointing at a past shared
# event.  A bare "叫什么/多大" is deliberately absent: "你今年多大" is an
# identity question, not a memory lookup.  "答应/约定" are soft markers: they
# also appear in present requests ("你答应给我唱歌"), so they only count with
# a question marker.
_MEMORY_MARKERS = (
    "记得",
    "记不记得",
    "还记得",
    "记着",
    "上次",
    "上回",
    "那次",
    "那天",
    "当时",
    "之前",
    "以前",
    "昨天",
    "昨晚",
    "昨天晚上",
    "前天",
    "去年",
    "前年",
    "上个月",
    "上周",
    "前几天",
    "我说过",
    "你说过",
    "我跟你说",
    "你跟我说",
    "我问你",
    "你问我",
    "问你",
    "问我",
    "我们说好",
    "原话",
)

_SOFT_MARKERS = ("答应", "约定")

_QUESTION_MARKERS = (
    "吗",
    "呢",
    "么",
    "什么",
    "啥",
    "谁",
    "哪",
    "多少",
    "几",
    "是不是",
    "有没有",
    "怎么",
    "怎样",
    "如何",
    "为什么",
)

# A reminder ("记得吃饭", "别忘了带伞") starts with these and usually is not a
# question.  If a question marker is present too ("记得我说过什么吗"), the
# message is treated as a memory question.
_REMINDER_PREFIXES = ("记得", "别忘了", "别忘记", "记住")


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def wants_memory_check(text: str) -> bool:
    """True for turns that point at a past shared event."""
    message = _normalize(text)
    if not message:
        return False
    if message.startswith(_REMINDER_PREFIXES) and not any(
        marker in message for marker in _QUESTION_MARKERS
    ):
        return False
    if any(marker in message for marker in _MEMORY_MARKERS):
        return True
    if any(marker in message for marker in _SOFT_MARKERS) and any(
        marker in message for marker in _QUESTION_MARKERS
    ):
        return True
    return False


def wants_verbatim(text: str) -> bool:
    """True when the ask is about the exact words, not just the gist."""
    message = _normalize(text)
    return any(
        marker in message
        for marker in ("原话", "怎么说的", "说了什么", "说了啥", "怎么回答", "怎么答")
    )
