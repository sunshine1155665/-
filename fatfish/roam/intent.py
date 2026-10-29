"""Owner-command parsing for roaming (ADR-0014 P0).

The owner's explicit instruction is the only way a trip starts in P0
(``autonomy='off'`` semantics; the P1 scheduler adds proposals).  This module
is deliberately deterministic: "去看看妈妈，问她身体" must work even when the
model forgets to emit the ``roam_visit`` action.

Only a bound room name is accepted.  A missing/unknown name returns ``None`` --
never a guess at a recipient.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .model import Room

# Verbs that make a sentence a departure order rather than narration.  Keep
# the list short: a false positive sends a message to someone.
_VISIT_MARKERS = (
    "去看看",
    "去看一下",
    "去看",
    "去找",
    "去问",
    "去陪",
    "去瞧瞧",
    "去和",
    "去跟",
    "去聊聊",
    "去聊",
    "帮我看看",
    "替我去看看",
)

_RETURN_MARKERS = (
    "回来",
    "先回来",
    "快回来",
    "别去了",
    "不用去了",
    "不要去了",
    "回家",
)

# Leading filler between the room name and the actual purpose.
_PURPOSE_PREFIXES = (
    "顺便",
    "帮我",
    "替我",
    "问一下她",
    "问一下他",
    "问一下",
    "问问",
    "问她",
    "问他",
    "问",
    "看一下她",
    "看一下他",
    "看一下",
    "看看她",
    "看看他",
    "看看",
    "聊聊",
    "聊",
    "说",
)

_PUNCT = "，,。.；;：:！!？?、 \t\n"

_NEGATIONS = ("别去", "不要去", "不用去", "先别", "别找", "不用找")


def _clean_purpose(raw: str) -> str:
    text = raw.strip(_PUNCT)
    changed = True
    while changed and text:
        changed = False
        for prefix in _PURPOSE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip(_PUNCT)
                changed = True
                break
    return text[:120]


def parse_visit_command(
    text: str, rooms: Iterable[Room]
) -> dict[str, Any] | None:
    """Return ``{"room": Room, "purpose": str}`` for an owner departure order.

    The longest matching room name wins ("妈妈" before "妈").  The name must
    appear after a visit marker, so "妈妈今天说什么了" is not a departure.
    """
    value = str(text or "").strip()
    if not value:
        return None
    # "别去看看妈妈" / "先别去找她" is a refusal, not an order.  Check the
    # whole sentence: a negation after the verb ("去看看妈妈？不用了") is less
    # likely than a prefix, and refusing on ambiguity is the safe side.
    if any(negation in value for negation in _NEGATIONS):
        return None
    room_list = [room for room in rooms if room.names()]
    if not room_list:
        return None
    candidates = [
        (room, name)
        for room in room_list
        for name in room.names()
    ]
    for room, name in sorted(candidates, key=lambda item: len(item[1]), reverse=True):
        index = value.find(name)
        if index < 0:
            continue
        head = value[:index]
        if not any(marker in head for marker in _VISIT_MARKERS):
            continue
        tail = value[index + len(name):]
        return {"room": room, "purpose": _clean_purpose(tail)}
    return None


def parse_return_command(text: str) -> bool:
    """True when the owner tells her to come back / not to go."""
    value = str(text or "").strip()
    if not value:
        return False
    return any(marker in value for marker in _RETURN_MARKERS)


__all__ = ["parse_return_command", "parse_visit_command"]
