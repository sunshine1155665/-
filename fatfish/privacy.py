"""Secret notes: things the owner has explicitly marked as not-for-outsiders.

The friend slot is allowed to know almost everything, so the only real privacy
control left is an explicit deny-list. That is what this module stores.

Two different framings are produced from the same rows, because the reader
differs:

* the **owner's** session gets the plain list -- so the persona knows which
  facts exist and knows they are confidential;
* the **friend's** session gets a hard "never bring this up" framing -- the
  persona cannot dodge a topic it has never heard of, so the list has to be
  present, but it is labelled as forbidden rather than as knowledge.

The list never enters the stable prefix, so prompt caching is unaffected.
"""

from __future__ import annotations

import time
from typing import Any

from .db import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secret_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'cli'
)
"""

MAX_NOTE_CHARS = 300
MAX_PROMPT_NOTES = 10
MAX_PROMPT_CHARS = 700


class SecretNotes:
    def __init__(self, db: Database):
        self.db = db
        self.db.execute(_SCHEMA)

    # ------------------------------------------------------------------ writes
    def add(self, text: str, *, source: str = "cli") -> dict[str, Any]:
        value = " ".join(str(text or "").split())
        if not value:
            raise ValueError("secret note must not be empty")
        value = value[:MAX_NOTE_CHARS]
        existing = self.db.query_one(
            "SELECT id FROM secret_notes WHERE text=? LIMIT 1", (value,)
        )
        now = time.time()
        if existing is not None:
            return self._row(int(existing["id"]))
        cur = self.db.execute(
            "INSERT INTO secret_notes(text, created_at, source) VALUES (?,?,?)",
            (value, now, str(source or "cli")[:32] or "cli"),
        )
        return self._row(int(cur.lastrowid))

    def remove(self, note_id: int) -> bool:
        row = self.db.query_one("SELECT 1 AS hit FROM secret_notes WHERE id=?", (int(note_id),))
        if row is None:
            return False
        self.db.execute("DELETE FROM secret_notes WHERE id=?", (int(note_id),))
        return True

    def clear(self) -> int:
        count = self.db.query_one("SELECT COUNT(*) AS n FROM secret_notes")
        total = int(count["n"]) if count else 0
        self.db.execute("DELETE FROM secret_notes")
        return total

    # ------------------------------------------------------------------ reads
    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, text, created_at, source FROM secret_notes ORDER BY id LIMIT ?",
            (int(limit),),
        )
        return [dict(row) for row in rows]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM secret_notes")
        return int(row["n"]) if row else 0

    def for_prompt(self, *, audience: str = "owner") -> str:
        """Render the deny-list for one audience.

        ``audience='owner'``  -> "these are confidential"
        ``audience='friend'`` -> "never mention these"
        """
        notes = self.list(limit=MAX_PROMPT_NOTES)
        if not notes:
            return ""
        lines = []
        used = 0
        for note in notes:
            line = f"- {note['text']}"
            if used + len(line) > MAX_PROMPT_CHARS:
                lines.append("- （还有更多，此处省略）")
                break
            lines.append(line)
            used += len(line)
        body = "\n".join(lines)
        if audience == "friend":
            return (
                "[保密] 下面这些是主人哥哥明确要求不能外传的内容。"
                "绝对不要主动提起，被问到也不要确认、不要暗示、不要展开，自然会带过去：\n"
                + body
            )
        return (
            "[保密] 下面这些是你知道、但绝对不能对姐姐说的内容"
            "（她问起要自然地带过去，不确认也不否认）：\n" + body
        )

    # ----------------------------------------------------------------- helpers
    def _row(self, note_id: int) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id, text, created_at, source FROM secret_notes WHERE id=?",
            (int(note_id),),
        )
        return dict(row) if row else {}


__all__ = ["SecretNotes"]
