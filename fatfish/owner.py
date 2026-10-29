"""Explicit owner binding (认主) plus the single optional friend slot.

The persona addresses the user as 主人哥哥, but until now there was no explicit
identity behind that word. This registry stores who has been bound, and in what
capacity:

* ``role='owner'`` -- exactly one, the person the persona calls 主人哥哥;
* ``role='friend'`` -- exactly one, an optional close friend the persona
  addresses by their own display name (e.g. 姐姐).

A friend is **not** a second owner. The friend slot only changes how the
runtime hints at the current speaker; it never makes ``is_owner()`` return
true, and owner-only naming stays with the owner.

Binding is deliberately narrow:

* one holder per role -- binding again replaces the previous holder of *that
  same role* and leaves the other role untouched;
* private sessions only -- group sessions can never be bound;
* the two roles may not share a session id (an owner is not their own friend);
* the optional remote claim code lives in ``[relationship].owner_claim_code``
  and is never stored in the database or written to logs.
"""

from __future__ import annotations

import hmac
import re
import time
from typing import Any

from .config import Config
from .db import Database

DEFAULT_DISPLAY_NAME = "主人哥哥"
DEFAULT_FRIEND_NAME = "姐姐"

ROLE_OWNER = "owner"
ROLE_FRIEND = "friend"
# T-0073 (owner decision 2026-10-30): 妈妈 / 老师们 get real roles instead of
# falling into the anonymous-stranger bucket.  A bound non-owner role may chat
# about the owner's non-confidential daily life (black-list policy: everything
# except 作息 / secret_notes / other people's conversations), while the
# conversation isolation between non-owner sessions stays exactly as strict.
ROLE_FAMILY = "family"
ROLE_TEACHER = "teacher"
ROLES: tuple[str, ...] = (ROLE_OWNER, ROLE_FRIEND, ROLE_FAMILY, ROLE_TEACHER)

# The one uin parser for bound sessions.  Only ``<platform>:private:<digits>``
# yields a uin; a web session (or any other shape) is unreachable and must
# read as "no uin", never as a guess.  Runtime, memory and catch-up all go
# through these helpers instead of keeping private copies.
_QQ_PRIVATE_SESSION_RE = re.compile(r"^[^:]+:private:([0-9]+)$")


def uin_from_session_id(session_id: Any) -> str:
    """The numeric uin of a private session id, or ``""``."""
    match = _QQ_PRIVATE_SESSION_RE.match(str(session_id or "").strip())
    return match.group(1) if match else ""


def uin_from_binding(db: Database, role: str) -> str:
    """The uin held by ``role`` in ``owner_bindings``, or ``""``."""
    try:
        row = db.query_one(
            "SELECT session_id FROM owner_bindings WHERE role=? LIMIT 1", (role,)
        )
    except Exception:  # the table may not exist very early
        return ""
    if row is None:
        return ""
    return uin_from_session_id(row["session_id"])


def binding_exists(db: Database, role: str) -> bool:
    """True when the role has a holder, even a non-QQ (web) one.

    Callers that fall back to a person card need this to tell "no binding"
    from "bound to a web session": the latter must never resurrect a stale
    QQ uin.
    """
    try:
        row = db.query_one(
            "SELECT 1 FROM owner_bindings WHERE role=? LIMIT 1", (role,)
        )
    except Exception:  # the table may not exist very early
        return False
    return row is not None


def owner_uin_from_binding(db: Database) -> str | None:
    """The owner slot holder's QQ uin, or ``None`` when unreachable."""
    return uin_from_binding(db, ROLE_OWNER) or None


def friend_uin_from_binding(db: Database) -> str | None:
    """The bound friend slot holder's QQ uin, or ``None`` when unreachable.

    Reads ``owner_bindings`` directly: the binding row is the single consent
    point.  A binding whose session id cannot yield a uin (e.g. a web session)
    counts as unreachable.
    """
    return uin_from_binding(db, ROLE_FRIEND) or None


_OWNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_bindings (
    session_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '主人哥哥',
    bound_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'cli',
    role TEXT NOT NULL DEFAULT 'owner'
)
"""


class OwnerRegistry:
    """Persist and answer the owner binding and the single friend slot."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        configured_name = str(
            config.get("relationship.owner_display_name", DEFAULT_DISPLAY_NAME)
            or DEFAULT_DISPLAY_NAME
        )
        self.default_display_name = _clean_display_name(configured_name)
        configured_friend = str(
            config.get("relationship.friend_display_name", DEFAULT_FRIEND_NAME)
            or DEFAULT_FRIEND_NAME
        )
        self.default_friend_name = _clean_display_name(configured_friend)
        self.claim_code = str(
            config.get("relationship.owner_claim_code", "") or ""
        ).strip()
        self.db.execute(_OWNER_SCHEMA)
        # ``role`` was added after the first release; older databases predate it.
        if "role" not in self.db.columns("owner_bindings"):
            self.db.execute(
                "ALTER TABLE owner_bindings ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'"
            )

    # ------------------------------------------------------------------ reads
    @property
    def claim_enabled(self) -> bool:
        return bool(self.claim_code)

    def get(self, role: str = ROLE_OWNER) -> dict[str, Any] | None:
        """Return the binding for ``role`` (owner by default)."""
        wanted = _normalise_role(role)
        row = self.db.query_one(
            "SELECT session_id, platform, display_name, bound_at, source, role "
            "FROM owner_bindings WHERE role=? LIMIT 1",
            (wanted,),
        )
        return dict(row) if row else None

    def get_friend(self) -> dict[str, Any] | None:
        return self.get(ROLE_FRIEND)

    def is_owner(self, session_id: str) -> bool:
        return _matches_role(self.db, session_id, ROLE_OWNER)

    def is_friend(self, session_id: str) -> bool:
        return _matches_role(self.db, session_id, ROLE_FRIEND)

    def role_of(self, session_id: str) -> str | None:
        """``owner`` / ``friend`` / ``None`` for a session id."""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        try:
            row = self.db.query_one(
                "SELECT role FROM owner_bindings WHERE session_id=? LIMIT 1", (sid,)
            )
        except Exception:
            return None
        if row is None:
            return None
        value = str(row["role"] or "").strip().lower()
        return value if value in ROLES else ROLE_OWNER

    def display_name_for(self, session_id: str) -> str | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        try:
            row = self.db.query_one(
                "SELECT display_name FROM owner_bindings WHERE session_id=? LIMIT 1",
                (sid,),
            )
        except Exception:
            return None
        return str(row["display_name"]) if row else None

    def status(self) -> dict[str, Any]:
        binding = self.get(ROLE_OWNER)
        friend = self.get(ROLE_FRIEND)
        extra: dict[str, list[dict[str, Any]]] = {}
        for role in (ROLE_FAMILY, ROLE_TEACHER):
            try:
                rows = self.db.query(
                    "SELECT session_id, platform, display_name, bound_at, source, role "
                    "FROM owner_bindings WHERE role=? ORDER BY bound_at",
                    (role,),
                )
            except Exception:
                rows = []
            extra[role] = [dict(r) for r in rows]
        return {
            "bound": binding is not None,
            "owner": binding,
            "friend": friend,
            "friend_bound": friend is not None,
            "family": extra[ROLE_FAMILY],
            "teachers": extra[ROLE_TEACHER],
            "claim_code_configured": self.claim_enabled,
            "default_display_name": self.default_display_name,
            "default_friend_name": self.default_friend_name,
        }

    # ----------------------------------------------------------------- writes
    def bind(
        self,
        session_id: str,
        display_name: str = DEFAULT_DISPLAY_NAME,
        source: str = "cli",
        *,
        role: str = ROLE_OWNER,
    ) -> dict[str, Any]:
        wanted = _normalise_role(role)
        sid = _require_private_session(session_id)
        fallback = (
            self.default_display_name
            if wanted == ROLE_OWNER
            else self.default_friend_name
        )
        name = _clean_display_name(display_name) or fallback
        origin = str(source or "cli").strip()[:32] or "cli"
        platform = sid.split(":", 1)[0].strip().lower() if ":" in sid else ""
        bound_at = time.time()

        other = ROLE_FRIEND if wanted == ROLE_OWNER else ROLE_OWNER
        other_row = self.get(other)
        if other_row is not None and str(other_row.get("session_id")) == sid:
            raise ValueError(
                f"session {sid} is already bound as {other}; "
                "the owner is not their own friend"
            )

        with self.db.transaction():
            # Single-slot roles (owner / friend): binding again replaces the
            # previous holder of *that* role.  Multi-slot roles (family /
            # teacher, T-0073): 妈妈 and two teachers coexist, so the upsert
            # is per session -- a session holds at most one role.
            if wanted in (ROLE_OWNER, ROLE_FRIEND):
                self.db.execute("DELETE FROM owner_bindings WHERE role=?", (wanted,))
            else:
                self.db.execute(
                    "DELETE FROM owner_bindings WHERE session_id=?", (sid,)
                )
            self.db.execute(
                "INSERT INTO owner_bindings(session_id, platform, display_name, bound_at, source, role) "
                "VALUES (?,?,?,?,?,?)",
                (sid, platform, name, bound_at, origin, wanted),
            )
        binding = self.get(wanted)
        if binding is None:  # pragma: no cover - defensive
            raise RuntimeError(f"{wanted} binding was not persisted")
        return binding

    def bind_friend(
        self,
        session_id: str,
        display_name: str = DEFAULT_FRIEND_NAME,
        source: str = "cli",
    ) -> dict[str, Any]:
        return self.bind(
            session_id, display_name=display_name, source=source, role=ROLE_FRIEND
        )

    def unbind(
        self, role: str = ROLE_OWNER, *, session_id: str | None = None
    ) -> bool:
        """Remove a binding.

        Single-slot roles drop their one row; multi-slot roles (family /
        teacher) drop only ``session_id`` when given, so unbinding one teacher
        does not evict the other.
        """
        wanted = _normalise_role(role)
        sid = str(session_id or "").strip()
        if sid:
            row = self.db.query_one(
                "SELECT 1 AS hit FROM owner_bindings WHERE session_id=? AND role=?",
                (sid, wanted),
            )
            if row is None:
                return False
            with self.db.transaction():
                self.db.execute(
                    "DELETE FROM owner_bindings WHERE session_id=? AND role=?",
                    (sid, wanted),
                )
            return True
        if self.get(wanted) is None:
            return False
        with self.db.transaction():
            self.db.execute("DELETE FROM owner_bindings WHERE role=?", (wanted,))
        return True

    def unbind_friend(self) -> bool:
        return self.unbind(ROLE_FRIEND)

    # ----------------------------------------------------------------- claims
    def claim_code_matches(self, provided: str) -> bool:
        if not self.claim_code:
            return False
        return hmac.compare_digest(
            str(provided or "").encode("utf-8"),
            self.claim_code.encode("utf-8"),
        )


def _normalise_role(role: Any) -> str:
    value = str(role or "").strip().lower()
    if value not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    return value


def _matches_role(db: Database, session_id: str, role: str) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    try:
        row = db.query_one(
            "SELECT 1 AS hit FROM owner_bindings WHERE session_id=? AND role=? LIMIT 1",
            (sid, role),
        )
    except Exception:
        # The table may not exist yet when context is assembled very early.
        return False
    return row is not None


def _require_private_session(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id must not be empty")
    if _looks_like_group_session(sid):
        raise ValueError("binding requires a private session")
    return sid


def _clean_display_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:64]


def _looks_like_group_session(session_id: str) -> bool:
    value = str(session_id or "").strip().lower()
    return ":group:" in value or value.startswith(("group:", "qq_group:"))


__all__ = [
    "DEFAULT_DISPLAY_NAME",
    "DEFAULT_FRIEND_NAME",
    "ROLE_FRIEND",
    "ROLE_OWNER",
    "ROLES",
    "OwnerRegistry",
]
