"""Split text into short, TTS-friendly sentence segments.

The splitter is deliberately conservative: it never breaks protected markup
(``<strong>...</strong>``, ``<em>...</em>`` or ``[breath]``), keeps closing
quotes/brackets with the sentence they close, and only falls back to a hard
split when a segment has no usable punctuation or space near the limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["split_short_sentences", "split_sentences", "visible_len"]

_TERMINALS = frozenset("。！？!?；;")
_CLOSERS = frozenset("”’」』）】")
_COMMAS = frozenset("，,、")
_PAREN_OPEN = frozenset("（(")
_PAREN_CLOSE = frozenset("）)")
_BREATH = "[breath]"
_ELLIPSIS = "……"

# Markup whose rendered text is spoken by TTS but whose tags are control
# tokens. Both the opening/closing markup and [breath] are excluded from the
# character budget.
_MARKUP_RE = re.compile(r"</?(?:strong|em)\b[^>]*>|\[breath\]", re.IGNORECASE)
_PROTECTED_TAG_RE = re.compile(
    r"<(strong|em)\b[^>]*>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL
)
_OPEN_OR_CLOSE_RE = re.compile(r"</?(?:strong|em)\b[^>]*>", re.IGNORECASE)

# Characters that cannot carry a sentence on their own.
_NON_CONTENT = frozenset(
    "。！？!?；;…，,、：:·—－-～~“”‘’\"'「」『』（）()【】[]{}<>《》〈〉/\\|"
)


@dataclass(slots=True)
class _Atom:
    """Smallest unit the splitter will keep together."""

    text: str
    kind: str
    visible: int = 0


def split_short_sentences(
    text: str,
    *,
    max_chars: int = 20,
    min_chars: int = 4,
) -> list[str]:
    """Split *text* into natural chunks of roughly 6-20 spoken characters.

    ``max_chars`` and ``min_chars`` count speakable characters: markup such as
    ``<strong>`` and pause tokens such as ``[breath]`` do not consume budget.
    A protected element is never cut open, even when it alone exceeds the cap.
    """
    max_chars = max(1, int(max_chars))
    min_chars = max(0, int(min_chars))
    raw = text or ""
    if not raw.strip():
        return []

    atoms = _tokenize(raw)
    chunks: list[str] = []
    for piece in _absorb_orphan_controls(_sentence_pieces(atoms)):
        piece = _trim(piece)
        if not piece or not _has_content(_atoms_text(piece)):
            continue
        for part in _split_long(piece, max_chars=max_chars, min_chars=min_chars):
            rendered = _atoms_text(_trim(part))
            if rendered and _has_content(rendered):
                chunks.append(rendered)
    return _repair_parentheses(_merge_short(chunks, min_chars=min_chars))


def split_sentences(text: str) -> list[str]:
    """Split *text* at sentence punctuation without cutting inside a sentence.

    Unlike :func:`split_short_sentences`, this never hard-splits a long
    sentence: a 40-character sentence stays one piece. Callers that need a
    character budget decide how to group these pieces.
    """
    atoms = _tokenize(text or "")
    pieces = _absorb_orphan_controls(_sentence_pieces(atoms))
    result: list[str] = []
    for piece in pieces:
        rendered = _atoms_text(_trim(piece))
        if rendered and _has_content(rendered):
            result.append(rendered)
    return _repair_parentheses(result)


def visible_len(text: str) -> int:
    """Length of speakable text, excluding TTS control markup."""
    return _visible_len(text)


def _tokenize(text: str) -> list[_Atom]:
    atoms: list[_Atom] = []
    i = 0
    size = len(text)
    while i < size:
        ch = text[i]

        if ch == "<":
            match = _PROTECTED_TAG_RE.match(text, i)
            if match:
                raw = match.group(0)
                atoms.append(_Atom(raw, "tag", _visible_len(match.group(2))))
                i = match.end()
                continue
            match = _OPEN_OR_CLOSE_RE.match(text, i)
            if match:
                atoms.append(_Atom(match.group(0), "markup", 0))
                i = match.end()
                continue
            atoms.append(_Atom(ch, "char", 1))
            i += 1
            continue

        if text[i : i + len(_BREATH)].lower() == _BREATH:
            atoms.append(_Atom(text[i : i + len(_BREATH)], "breath", 0))
            i += len(_BREATH)
            continue

        if ch in "\r\n":
            i += 2 if text.startswith("\r\n", i) else 1
            atoms.append(_Atom("\n", "newline", 0))
            continue

        if ch == "…" or (
            ch == "."
            and (
                text.startswith("..", i)
                or (i + 1 < size and text[i + 1] == "…")
            )
        ):
            end = i
            while end < size and text[end] in ".…":
                end += 1
            raw = text[i:end]
            if len(raw) >= 2:
                raw = _ELLIPSIS
            atoms.append(_Atom(raw, "ellipsis", len(raw)))
            i = end
            continue

        if ch in _TERMINALS:
            atoms.append(_Atom(ch, "end", 1))
            i += 1
            continue
        if ch in _CLOSERS:
            atoms.append(_Atom(ch, "closer", 1))
            i += 1
            continue
        if ch in _COMMAS:
            atoms.append(_Atom(ch, "comma", 1))
            i += 1
            continue
        if ch in " \t\u3000":
            atoms.append(_Atom(ch, "space", 1))
            i += 1
            continue

        atoms.append(_Atom(ch, "char", 1))
        i += 1
    return atoms


def _sentence_pieces(atoms: list[_Atom]) -> list[list[_Atom]]:
    """Cut at terminators/newlines, absorbing following closing punctuation."""
    pieces: list[list[_Atom]] = []
    buffer: list[_Atom] = []
    i = 0

    def flush() -> None:
        if buffer:
            pieces.append(list(buffer))
            buffer.clear()

    while i < len(atoms):
        atom = atoms[i]

        if atom.kind == "newline":
            flush()
            i += 1
            continue

        if atom.kind == "end":
            buffer.append(atom)
            i += 1
            while i < len(atoms) and atoms[i].kind in ("end", "closer"):
                buffer.append(atoms[i])
                i += 1
            flush()
            continue

        if atom.kind == "ellipsis":
            # A leading ellipsis belongs to the sentence that follows it.
            leading = not _has_content(_atoms_text(buffer))
            buffer.append(atom)
            i += 1
            if leading:
                continue
            while i < len(atoms) and atoms[i].kind == "closer":
                buffer.append(atoms[i])
                i += 1
            flush()
            continue

        buffer.append(atom)
        i += 1

    flush()
    return pieces


def _absorb_orphan_controls(pieces: list[list[_Atom]]) -> list[list[_Atom]]:
    """Attach a trailing breath/tag-only piece to the sentence before it."""
    merged: list[list[_Atom]] = []
    for piece in pieces:
        has_content = _has_content(_atoms_text(piece))
        has_control = any(
            atom.kind in ("breath", "tag", "markup") for atom in piece
        )
        if not has_content and has_control and merged:
            merged[-1].extend(piece)
            continue
        merged.append(piece)
    return merged


def _split_long(
    atoms: list[_Atom],
    *,
    max_chars: int,
    min_chars: int,
) -> list[list[_Atom]]:
    result: list[list[_Atom]] = []
    remaining = atoms
    while remaining:
        if _atoms_visible_len(remaining) <= max_chars:
            result.append(remaining)
            break
        cut = _choose_cut(remaining, max_chars=max_chars, min_chars=min_chars)
        cut = max(1, min(cut, len(remaining)))
        # Keep a terminal/closing run with its sentence, but also leave a
        # viable tail: a lone punctuation chunk would be dropped later.
        while cut > 1 and not _tail_ok(remaining, cut, min_chars):
            cut -= 1
        if cut == len(remaining):
            result.append(remaining)
            break
        left = _trim(remaining[:cut])
        right = _trim(remaining[cut:])
        if not left:
            # An oversized protected element at the start: keep it intact.
            left, right = remaining[:1], remaining[1:]
        result.append(left)
        remaining = right
    return [part for part in result if part]


def _choose_cut(atoms: list[_Atom], *, max_chars: int, min_chars: int) -> int:
    prefix = [0]
    for atom in atoms:
        prefix.append(prefix[-1] + atom.visible)

    hard = 0
    for i in range(1, len(atoms) + 1):
        if prefix[i] <= max_chars:
            hard = i
        else:
            break
    if hard == 0:
        return 1

    floor = max(min_chars, max_chars // 2)
    candidates: list[int] = []
    for i, atom in enumerate(atoms):
        left_len = prefix[i + 1] if atom.kind in ("comma", "end") else prefix[i]
        if atom.kind in ("comma", "end", "space") and floor <= left_len <= max_chars:
            candidates.append(i + 1 if atom.kind in ("comma", "end") else i)
    if candidates:
        return max(candidates)

    # No punctuation near the limit: hard split, but step back over an ASCII
    # word/number when a sensible boundary exists.
    cut = hard
    while 0 < cut < len(atoms) and _inside_word(atoms[cut - 1], atoms[cut]):
        cut -= 1
    if cut == 0 or prefix[cut] < max(1, max_chars // 2):
        cut = hard
    return cut


def _tail_ok(atoms: list[_Atom], cut: int, min_chars: int) -> bool:
    tail = _trim(atoms[cut:])
    if not tail or not _has_content(_atoms_text(tail)):
        return False
    return _atoms_visible_len(tail) >= min_chars


def _inside_word(left: _Atom, right: _Atom) -> bool:
    if left.kind != "char" or right.kind != "char":
        return False
    return _is_word_char(left.text[-1]) and _is_word_char(right.text[0])


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _merge_short(parts: list[str], *, min_chars: int) -> list[str]:
    if not parts:
        return []
    merged: list[str] = []
    pending = ""
    for part in parts:
        current = _join(pending, part)
        # A short but complete sentence is still a natural TTS utterance.
        # Only merge short fragments that do not end in sentence punctuation.
        if _visible_len(current) < min_chars and not _ends_sentence(current):
            pending = current
            continue
        merged.append(current)
        pending = ""
    if pending:
        if merged and not _ends_sentence(merged[-1]):
            merged[-1] = _join(merged[-1], pending)
        else:
            merged.append(pending)
    return [part for part in merged if part and _has_content(part)]


def _repair_parentheses(parts: list[str]) -> list[str]:
    """Rejoin fragments that a hard cut left inside a parenthetical aside.

    With a 20-character cap, ``……趴着呢（尾巴慢慢晃了一下）`` can be cut into
    ``……晃`` + ``了一下）``.  A slightly longer utterance is much more natural
    than a meaningless tail, so merge until the parentheses balance again.
    Only balanced text is repaired; an unmatched ``（`` keeps the old output.
    """
    if not parts or sum(_paren_balance(part) for part in parts) != 0:
        return parts

    repaired: list[str] = []
    pending = ""
    for part in parts:
        pending = _join(pending, part)
        balance = _paren_balance(pending)
        if balance > 0:
            # Wait for the closing parenthesis in a later fragment.
            continue
        if balance < 0 and repaired:
            repaired[-1] = _join(repaired[-1], pending)
            pending = ""
            continue
        repaired.append(pending)
        pending = ""
    if pending:
        repaired.append(pending)
    return repaired


def _paren_balance(text: str) -> int:
    balance = 0
    for ch in text or "":
        if ch in _PAREN_OPEN:
            balance += 1
        elif ch in _PAREN_CLOSE:
            balance -= 1
    return balance


def _ends_sentence(text: str) -> bool:
    cleaned = (text or "").rstrip().rstrip("”’」』）】")
    if not cleaned:
        return False
    return cleaned[-1] in _TERMINALS or cleaned.endswith(_ELLIPSIS)


def _join(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if (
        left[-1].isascii()
        and right[0].isascii()
        and not left[-1].isspace()
        and not right[0].isspace()
    ):
        return f"{left} {right}"
    return left + right


def _trim(atoms: list[_Atom]) -> list[_Atom]:
    start = 0
    end = len(atoms)
    while start < end and atoms[start].kind == "space":
        start += 1
    while end > start and atoms[end - 1].kind == "space":
        end -= 1
    return atoms[start:end]


def _atoms_text(atoms: list[_Atom]) -> str:
    return "".join(atom.text for atom in atoms).strip()


def _atoms_visible_len(atoms: list[_Atom]) -> int:
    return sum(atom.visible for atom in atoms)


def _visible_len(text: str) -> int:
    """Length of speakable text, excluding TTS control markup."""
    return len(_MARKUP_RE.sub("", text or "").strip())


def _has_content(text: str) -> bool:
    visible = _MARKUP_RE.sub("", text or "")
    return any(ch not in _NON_CONTENT and not ch.isspace() for ch in visible)
