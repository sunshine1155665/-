"""Helpers for keeping non-spoken material out of spoken replies.

Two things get in the way of a clean read-out:

*Transcript metadata.* The transcript carries ``[MM-DD HH:MM]`` next to each
user line so the model can tell how long ago something was said. That marker is
metadata, not a speaking style -- but a model that sees it in its *own* past
messages will happily start every reply with a timestamp, and once a reply looks
like a transcript line it also stops splitting short sentences into separate
bubbles.

*Stage directions.* The persona asks for small actions such as ``（小声）`` or
``（尾巴晃了一下）``. They belong in the chat bubble -- they are part of the
character -- but a synthesiser reads them out loud in the middle of a sentence.

Everything here exists to keep those boundaries: prefixes and asides are
stripped before audio is produced, and timestamps again when the transcript is
rebuilt, because rows written by an older version still carry them.
"""

from __future__ import annotations

import re

# Matches "2026-10-22 21:16", "10-22 21:16", "10/22", "10月22日" inside brackets.
TIMESTAMP_TOKEN = re.compile(
    r"\[\s*(?:\d{4}[-/])?\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*\]"
)

# Same shape, but anchored so it only fires at the start of a line.
LEADING_TIMESTAMP = re.compile(
    r"^\s*[\[(（]\s*(?:\d{4}[-/])?\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*[\])）]\s*"
)

ROLE_LABEL = re.compile(
    r"^\s*(?:用户|你|助手|大肥鱼|鲸鱼娘|user|assistant)\s*[:：]\s*", re.IGNORECASE
)

# One balanced aside on a single line. Nesting is not supported on purpose:
# ``（尾巴晃了一下（真的））`` is not something the persona produces, and the
# budget check below is what decides whether an aside is an action or a clause.
STAGE_DIRECTION = re.compile(r"[（(]([^（）()\n]*)[)）]")

# A trailing aside whose closing bracket never arrived (truncated generation).
UNCLOSED_STAGE_DIRECTION = re.compile(r"[（(]([^（）()\n]*)$")

# An aside that holds a whole clause is content, not a stage direction. Losing
# ``（我真的不知道）`` would change what is being said, so it stays.
_CLAUSE_MARKS = frozenset("。！？!?；;")

# An aside that *names somebody* reads as quoted speech -- "（我真的不知道）",
# "（你回来啦）" -- while a stage direction only describes a movement, "（小声）",
# "（尾巴晃了一下）". This pronoun test is the cheapest signal that separates the
# two; a verb lexicon would leak, because the model invents new actions.
_PERSONAL = frozenset("我你他她它咱俺您")

# Digits are information ("（3.2）", "（2024年）"), never an action.
_DIGITS = re.compile(r"[0-9０-９]")

# An aside with no Chinese in it is not part of the sentence being read:
# "(sighs)", "(beta)", "（♪）".
_CJK = re.compile(r"[\u3400-\u9fff]")

# Left behind once an aside disappears: ``（小声），那就`` -> ``,那就``.
_LEADING_JUNK = re.compile(r"^[\s，,、；;：:]+")
_SPACE_RUN = re.compile(r"[ \t]{2,}")


def strip_stage_directions(text: str, *, max_chars: int = 24) -> str:
    """Drop ``（小声）`` / ``(sighs)`` asides so TTS does not read them aloud.

    An aside is removed only when all of these hold: it is short, it carries no
    sentence punctuation, it names nobody, and it holds no digits. An aside
    without any Chinese in it ("(sighs)") is dropped too -- it cannot be part of
    the sentence being read. Everything else is treated as content and left
    alone: reading a clause slightly wrong is a smaller failure than silently
    deleting it. Text without any aside is returned unchanged, so this is safe
    to call on every segment.
    """
    if not text:
        return ""

    def drop(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if len(inner) > max_chars:
            return match.group(0)
        if _DIGITS.search(inner):
            return match.group(0)
        if _CJK.search(inner) and (
            any(ch in _CLAUSE_MARKS for ch in inner)
            or any(ch in _PERSONAL for ch in inner)
        ):
            return match.group(0)
        return ""

    cleaned = STAGE_DIRECTION.sub(drop, text)
    cleaned = UNCLOSED_STAGE_DIRECTION.sub(drop, cleaned)
    if cleaned == text:
        return text
    cleaned = _SPACE_RUN.sub(" ", cleaned)
    return _LEADING_JUNK.sub("", cleaned).strip()


def strip_timestamps(text: str) -> str:
    """Remove every timestamp-shaped bracket token anywhere in ``text``."""
    if not text:
        return ""
    return TIMESTAMP_TOKEN.sub("", text)


def strip_meta_line(line: str) -> str:
    """Strip a leading timestamp and a leading ``用户:`` style label."""
    cleaned = LEADING_TIMESTAMP.sub("", line)
    cleaned = ROLE_LABEL.sub("", cleaned).strip()
    return strip_timestamps(cleaned).strip()


def normalize_reply_segments(
    segments: list[str],
    *,
    max_segments: int = 4,
) -> list[str]:
    """Drop transcript artifacts and restore one-bubble-per-sentence rhythm."""
    out: list[str] = []
    for segment in segments:
        lines: list[str] = []
        for line in str(segment).splitlines():
            cleaned = strip_meta_line(line)
            if cleaned:
                lines.append(cleaned)
        if not lines:
            continue
        if len(lines) > 1 and all(len(line) <= 80 for line in lines):
            out.extend(lines)
        else:
            out.append("\n".join(lines))
    deduped: list[str] = []
    for text in out:
        if deduped and deduped[-1] == text:
            continue
        deduped.append(text)
    return deduped[: max(1, max_segments)]
