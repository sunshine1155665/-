"""Long-term / short-term memory with timelines, tiers and RAG pointers."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from ..clock import day_bounds, day_phase, human_date, human_time, parse_when, to_local
from ..config import Config
from ..db import Database
from .embedder import Embedder, build_embedder, cosine, dumps, loads, pack_embedding
from .entities import entities_for_query, entity_family_ids
from .events import EventBuilder
from .querytime import parse_time_window
from .sessions import is_test_bvid, is_test_session

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMELINE_TURN_CHARS = 100
_TIMELINE_SUMMARY_CHARS = 120

# ---------------------------------------------------------------------------
# T-0073 memory isolation (owner decision 2026-10-30)
#
# Visibility rules, in one place:
#   * ``audience='owner'``  -- owner sessions only (fail-closed default);
#   * ``audience='open'``   -- owner-origin non-confidential fact, any session
#     may be fed it (black-list policy: everything except 作息 / secret notes /
#     other people's conversations);
#   * a row originating in someone's private session is *always* restricted to
#     that session (and the owner), whatever its audience flag.
#
# The owner's own schedule is the one category the owner called out by name
# ("作息之外的东西"), so the write side pattern-matches it deterministically.
# Over-matching is the safe direction; the owner can flip a row via CLI.
#
# Scope correction (owner, 2026-10-30 15:00): only 主人哥哥's schedule is
# sealed. 鲸鱼娘's own routine stays shareable, and reminding people to sleep
# early is wanted behaviour -- so a schedule word only marks the row secret
# when the text does NOT clearly talk about her (self-markers win).
SCHEDULE_SECRET_RE = re.compile(
    r"几点(起|睡|回|下|吃)|睡(了|觉|点|醒|不着)|失眠|起床|作息|熬夜|通宵|"
    r"到(家|宿舍)|回(家|宿舍)|出门|外出|在(哪|哪儿|哪里)|下班|上班"
)
SELF_MARKER_RE = re.compile(r"鲸鲸|大肥鱼|本小姐|本鲸鱼娘")

AUDIENCE_OWNER = "owner"
AUDIENCE_OPEN = "open"
AUDIENCES = (AUDIENCE_OWNER, AUDIENCE_OPEN)

# Sentinel: "no viewer supplied" (owner's CLI / web console) vs an explicit
# ``viewer_role=None`` (an unbound stranger in the runtime turn path).  Only
# explicit viewers get gated; the console keeps its full view.
_UNSET = object()


@dataclass(slots=True)
class MemoryItem:
    id: str
    type: str
    scope: str
    session_id: str | None
    event_id: str | None
    episode_id: str | None
    gist: str
    summary: str
    event_time_start: float | None
    event_time_end: float | None
    time_granularity: str
    time_confidence: float
    observed_at: float
    valid_from: float | None
    valid_to: float | None
    importance: float
    confidence: float
    stability: float
    tier: int
    status: str
    trust_level: str
    origin_scope: str
    source_type: str | None
    source_id: str | None
    evidence: str | None
    entities: list[str]
    tags: list[str]
    embedding: list[float] | None
    access_count: int
    last_accessed_at: float | None
    supersedes: str | None
    superseded_by: str | None
    created_at: float
    updated_at: float
    # Attribution. ``subject`` is who this memory belongs to in normalised form
    # (``owner`` / ``friend`` / ``group`` / ``external`` / ``unknown``);
    # ``about`` lists the parties the content concerns, which may differ from
    # the speaker -- 姐姐 can report a fact about 主人哥哥.
    subject: str = ""
    about: list[str] = field(default_factory=list)

    @property
    def event_date(self) -> str | None:
        if self.event_time_start is None:
            return None
        return human_date(self.event_time_start, "Asia/Shanghai")


@dataclass(slots=True)
class MemoryHit:
    item: MemoryItem
    score: float
    similarity: float = 0.0
    keyword: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class RagHit:
    id: str
    source_type: str
    source_id: str
    text: str
    metadata: dict[str, Any]
    score: float
    similarity: float


@dataclass(slots=True)
class RecallResult:
    query: str
    memory_hits: list[MemoryHit] = field(default_factory=list)
    rag_hits: list[RagHit] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    ephemeral_text: str = ""
    meta_directory: list[dict[str, Any]] = field(default_factory=list)
    ephemeral: bool = True


class MemoryService:
    def __init__(self, db: Database, config: Config, embedder: Embedder | None = None):
        self.db = db
        self.config = config
        self.tz_name = config.timezone_name
        self.embedder = embedder or build_embedder(
            str(config.get("memory.embedding_backend", "hash")),
            int(config.get("memory.embedding_dim", 256)),
        )
        self.max_items = int(config.get("memory.max_items_per_search", 5000))
        # Cosine runs in Python, so the full 5k scan costs 260-600 ms.  Bound
        # the scan to a lexical + important shortlist first; 0 disables it and
        # restores the old behaviour exactly.
        self.candidate_limit = max(
            0, int(config.get("memory.candidate_limit", 512))
        )
        # The meta directory is a "what do I know about" overview.  Scanning
        # the newest few hundred active memories is enough to build it; reading
        # every row turned a cheap prompt section into an O(all memories) scan.
        self.meta_scan_limit = max(
            50,
            min(
                self.max_items,
                int(config.get("memory.meta_scan_limit", 400)),
            ),
        )
        self.short_term_ttl_days = int(config.get("memory.short_term_ttl_days", 7))
        self.today_timeline_chars = int(config.get("memory.today_timeline_chars", 1200))
        # 2026-11-18: forgetting-curve ranking.  Recency is a ranking signal,
        # never a deletion rule: archived rows stay searchable with a penalty,
        # and a genuinely exact hit gets a floor so old-but-right memories are
        # never pushed out by fresh-but-irrelevant ones.
        self.recency_weight = max(
            0.0, float(config.get("memory.recency_weight", 0.15))
        )
        self.archived_score_multiplier = min(
            1.0, max(0.1, float(config.get("memory.archived_score_multiplier", 0.8)))
        )
        self.exact_match_similarity_floor = min(
            1.0, max(0.0, float(config.get("memory.exact_match_similarity_floor", 0.75)))
        )
        self.exact_match_recency_floor = min(
            1.0, max(0.0, float(config.get("memory.exact_match_recency_floor", 0.6)))
        )
        self.archive_min_access_count = max(
            0, int(config.get("memory.archive_min_access_count", 3))
        )
        self.archive_recent_access_days = max(
            0.0, float(config.get("memory.archive_recent_access_days", 60))
        )
        self.events = EventBuilder(db, config)

    @property
    def party_names(self) -> dict[str, str]:
        """Live display names for attribution labels (``owner`` -> 主人哥哥)."""
        names: dict[str, str] = {}
        try:
            rows = self.db.query("SELECT role, display_name FROM owner_bindings")
        except Exception:
            return names
        for row in rows:
            role = str(row["role"] or "").strip().lower()
            name = str(row["display_name"] or "").strip()
            if role and name:
                names[role] = name
        return names

    # ------------------------------------------------- isolation (T-0073)
    def _bound_sessions(self) -> dict[str, str]:
        """session_id -> role for every bound private session."""
        try:
            rows = self.db.query("SELECT session_id, role FROM owner_bindings")
        except Exception:
            return {}
        return {
            str(r["session_id"]): str(r["role"] or "").strip().lower()
            for r in rows
        }

    def owner_session(self) -> str | None:
        for sid, role in self._bound_sessions().items():
            if role == "owner":
                return sid
        return None

    def non_owner_private_sessions(self) -> set[str]:
        """Bound private sessions that belong to someone other than the owner.

        ADR-0014 privacy boundary: the owner may recall summaries and key
        facts from a roam visit, but never another person's raw chat lines.
        Verbatim detail recall filters these sessions out for the owner.
        """
        return {
            sid
            for sid, role in self._bound_sessions().items()
            if role and role != "owner" and ":private:" in sid
        }

    def audience_for(self, text: str, session_id: str | None) -> str:
        """Write-side default for ``memory_items.audience``.

        Owner-session rows follow the owner's black-list policy: anything the
        作息 pattern touches stays owner-only, the rest is open.  Rows from
        every other session (and rows with no session) default to owner-only
        -- their own session can still recall them via the session filter, so
        fail-closed here costs nothing.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return AUDIENCE_OWNER
        if self._bound_sessions().get(sid) != "owner":
            return AUDIENCE_OWNER
        text = str(text or "")
        if (
            SCHEDULE_SECRET_RE.search(text)
            and not SELF_MARKER_RE.search(text)
        ):
            return AUDIENCE_OWNER
        return AUDIENCE_OPEN

    def _viewer_gate_sql(self, viewer_session: str | None) -> tuple[str, list[Any]]:
        """SQL predicate for a non-owner viewer: what this session may recall.

        ``session_id = viewer`` covers the viewer's own rows; the owner-origin
        ``open`` rows are the black-list-approved share; the last branch keeps
        鲸鱼娘's own life / group-common memories visible while closing the
        "another stranger's private chat" hole (unbound private sessions are
        caught by the ``:private:`` shape, bound ones by the NOT IN list).
        """
        bound = self._bound_sessions()
        private_sids = [sid for sid in bound if ":private:" in sid]
        clauses = ["session_id = ?"]
        params: list[Any] = [str(viewer_session or "").strip()]
        owner_sid = next(
            (sid for sid, role in bound.items() if role == "owner"), None
        )
        if owner_sid is not None:
            clauses.append("(audience = 'open' AND session_id = ?)")
            params.append(owner_sid)
        if private_sids:
            marks = ",".join("?" for _ in private_sids)
            clauses.append(
                "(scope = 'global' AND (session_id IS NULL OR session_id = ''"
                f" OR (session_id NOT IN ({marks}) AND session_id NOT LIKE '%:private:%')))"
            )
            params.extend(private_sids)
        else:
            clauses.append(
                "(scope = 'global' AND (session_id IS NULL OR session_id = ''"
                " OR session_id NOT LIKE '%:private:%'))"
            )
        return "(" + " OR ".join(clauses) + ")", params

    def _viewer_allows_session(self, viewer_session: str | None) -> Any:
        """Row-level predicate for tables without an ``audience`` column.

        Used for verbatim quotes (event_details): the black-list policy shares
        *facts*, never verbatim words, so non-owner viewers only see rows from
        their own session.  The owner bypasses the check entirely.
        """
        allowed = {str(viewer_session or "").strip()}
        return lambda sid: str(sid or "").strip() in allowed

    # ------------------------------------------------------------------ items
    def add_item(
        self,
        summary: str,
        *,
        type: str = "episodic",
        gist: str | None = None,
        scope: str = "global",
        session_id: str | None = None,
        event_id: str | None = None,
        episode_id: str | None = None,
        event_time: str | float | None = None,
        event_time_start: float | None = None,
        event_time_end: float | None = None,
        time_granularity: str | None = None,
        time_confidence: float | None = None,
        observed_at: float | None = None,
        valid_from: float | None = None,
        valid_to: float | None = None,
        importance: float | None = None,
        confidence: float = 0.7,
        source_type: str | None = None,
        source_id: str | None = None,
        evidence: str | None = None,
        entities: Iterable[str] = (),
        tags: Iterable[str] = (),
        subject: str = "",
        about: Iterable[str] = (),
        tier: int = 1,
        status: str = "active",
        trust_level: str = "internal",
        origin_scope: str = "self",
        audience: str | None = None,
    ) -> MemoryItem:
        observed = time.time() if observed_at is None else float(observed_at)
        start, end, granularity, tconf = self._normalize_event_time(
            event_time, event_time_start, event_time_end, time_granularity, time_confidence
        )
        importance = self._importance(summary, type) if importance is None else _clamp(importance)
        gist = (gist or _make_gist(summary)).strip()
        if audience is None:
            audience = self.audience_for(f"{gist}\n{summary}", session_id)
        audience = str(audience).strip().lower()
        if audience not in AUDIENCES:
            raise ValueError(f"unknown audience: {audience!r}")
        existing = self._find_duplicate(summary, source_type, source_id, observed)
        if existing:
            return existing
        entities = _unique(entities)
        tags = _unique(tags)
        about_parties = [_normalise_party(a) for a in _unique(about) if str(a or "").strip()]
        party = _normalise_party(subject)
        text_for_embedding = " ".join([gist, summary, " ".join(tags), " ".join(entities)])
        embedding = self.embedder.embed(text_for_embedding)
        now = time.time()
        item_id = uuid.uuid4().hex
        self.db.execute(
            """
            INSERT INTO memory_items(
                id, type, scope, session_id, event_id, episode_id, gist, summary,
                event_time_start, event_time_end, time_granularity, time_confidence,
                observed_at, valid_from, valid_to, importance, confidence, stability,
                tier, status, trust_level, origin_scope, source_type, source_id, evidence,
                subject, about_json, entities_json, tags_json, embedding, audience,
                access_count, last_accessed_at, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id, type, scope, session_id, event_id, episode_id, gist, summary,
                start, end, granularity, tconf,
                observed, valid_from, valid_to, importance, _clamp(confidence), 1.0,
                int(tier), status, str(trust_level), str(origin_scope), source_type, source_id, evidence,
                party, json.dumps(about_parties, ensure_ascii=False),
                json.dumps(entities, ensure_ascii=False), json.dumps(tags, ensure_ascii=False),
                pack_embedding(embedding), audience, 0, None, now, now,
            ),
        )
        return self.get(item_id)  # type: ignore[return-value]

    def _find_duplicate(
        self,
        summary: str,
        source_type: str | None,
        source_id: str | None,
        observed_at: float,
    ) -> MemoryItem | None:
        if source_type and source_id:
            row = self.db.query_one(
                "SELECT id FROM memory_items WHERE source_type=? AND source_id=? AND status='active' "
                "ORDER BY observed_at DESC LIMIT 1",
                (source_type, source_id),
            )
            if row:
                return self.get(row["id"])
            # An explicit source identity is authoritative: do not fall back
            # to summary matching, or two days with an identical line would
            # collapse into one archived memory during backfill.
            return None
        row = self.db.query_one(
            "SELECT id FROM memory_items WHERE summary=? AND status='active' AND observed_at>=? "
            "ORDER BY observed_at DESC LIMIT 1",
            (summary, observed_at - 86400),
        )
        return self.get(row["id"]) if row else None

    def get(self, item_id: str) -> MemoryItem | None:
        row = self.db.query_one("SELECT * FROM memory_items WHERE id=?", (item_id,))
        return self._row_to_item(row) if row else None

    def update_item(self, item_id: str, **fields: Any) -> MemoryItem | None:
        allowed = {
            "gist", "summary", "importance", "confidence", "tier", "status",
            "valid_from", "valid_to", "superseded_by", "supersedes", "tags_json",
            "entities_json", "stability", "event_id", "episode_id", "source_type",
            "source_id", "session_id", "importance", "observed_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get(item_id)
        updates["updated_at"] = time.time()
        sets = ", ".join(f"{key}=?" for key in updates)
        self.db.execute(
            f"UPDATE memory_items SET {sets} WHERE id=?",
            (*updates.values(), item_id),
        )
        return self.get(item_id)

    def update_item_fields(self, item_id: str, fields: dict[str, Any]) -> MemoryItem | None:
        """Edit user-visible fields while keeping trust/metrics untouched.

        Only ``gist``, ``summary``, ``type``, ``importance``, ``tags`` and
        ``entities`` are writable here.  Unknown keys (including
        ``trust_level``/``origin_scope``) are ignored.  Raises ``ValueError``
        for invalid values; returns ``None`` when the item does not exist.
        """
        item = self.get(item_id)
        if item is None:
            return None
        updates: dict[str, Any] = {}
        for name in ("gist", "summary"):
            if name in fields:
                value = fields[name]
                if not isinstance(value, str):
                    raise ValueError(f"{name} must be a string")
                updates[name] = value.strip()
        if "type" in fields:
            value = fields["type"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("type must be a non-empty string")
            updates["type"] = value.strip()
        if "importance" in fields:
            value = fields["importance"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("importance must be a number between 0 and 1")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError("importance must be between 0 and 1")
            updates["importance"] = float(value)
        for name, column in (("tags", "tags_json"), ("entities", "entities_json")):
            if name in fields:
                value = fields[name]
                if not isinstance(value, (list, tuple)) or not all(
                    isinstance(entry, str) for entry in value
                ):
                    raise ValueError(f"{name} must be a list of strings")
                updates[column] = json.dumps(_unique(value), ensure_ascii=False)
        if not updates:
            return item

        gist = str(updates.get("gist", item.gist))
        summary = str(updates.get("summary", item.summary))
        tags = _json_list(updates["tags_json"]) if "tags_json" in updates else item.tags
        entities = (
            _json_list(updates["entities_json"])
            if "entities_json" in updates
            else item.entities
        )
        text_for_embedding = " ".join([gist, summary, " ".join(tags), " ".join(entities)])
        updates["embedding"] = pack_embedding(self.embedder.embed(text_for_embedding))
        updates["updated_at"] = time.time()
        sets = ", ".join(f"{key}=?" for key in updates)
        self.db.execute(
            f"UPDATE memory_items SET {sets} WHERE id=?",
            (*updates.values(), item_id),
        )
        return self.get(item_id)

    def delete_item(self, item_id: str, *, hard: bool = False) -> MemoryItem | None:
        """Soft-delete by default; ``hard=True`` removes the row and FTS entry."""
        item = self.get(item_id)
        if item is None:
            return None
        if hard:
            self.db.execute("DELETE FROM memory_fts WHERE id=?", (item_id,))
            self.db.execute("DELETE FROM memory_items WHERE id=?", (item_id,))
            return item
        return self.update_item(item_id, status="deleted")

    def restore_item(self, item_id: str) -> MemoryItem | None:
        """Bring a soft-deleted item back to ``active``."""
        if self.get(item_id) is None:
            return None
        return self.update_item(item_id, status="active")

    def list_items(self, limit: int = 50, *, status: str | None = "active") -> list[MemoryItem]:
        if status:
            rows = self.db.query(
                "SELECT * FROM memory_items WHERE status=? ORDER BY observed_at DESC LIMIT ?",
                (status, int(limit)),
            )
        else:
            # "all" means every status the caller may see, never deleted rows.
            rows = self.db.query(
                "SELECT * FROM memory_items WHERE status<>'deleted' "
                "ORDER BY observed_at DESC LIMIT ?",
                (int(limit),),
            )
        return [self._row_to_item(row) for row in rows]

    def search_fts(self, query: str, limit: int = 20) -> list[MemoryItem]:
        """Full-text search over gist/summary, excluding soft-deleted items.

        ``unicode61`` keeps CJK runs as single tokens, so a plain FTS phrase
        misses substrings inside a run.  The LIKE clauses complement MATCH for
        those cases; either way the join keeps ``status='deleted'`` rows out.
        """
        tokens = re.findall(r"\w+", query or "")
        if not tokens:
            return []
        expression = " ".join(f'"{token}"' for token in tokens)
        like_clause = " OR ".join(
            "(m.gist LIKE ? OR m.summary LIKE ?)" for _ in tokens
        )
        like_params = [f"%{token}%" for token in tokens for _ in range(2)]
        rows = self.db.query(
            "SELECT m.* FROM memory_items AS m "
            "WHERE m.status<>'deleted' AND ("
            "m.id IN (SELECT id FROM memory_fts WHERE memory_fts MATCH ?) "
            f"OR ({like_clause})) "
            "ORDER BY m.importance DESC, m.observed_at DESC LIMIT ?",
            (expression, *like_params, max(1, int(limit))),
        )
        return [self._row_to_item(row) for row in rows]

    # ----------------------------------------------------------------- search
    def _scope_exclusion(
        self, exclude_scopes: frozenset[str] | set[str] | None
    ) -> tuple[str, list[str]]:
        """SQL fragment keeping memory rows with the given origin scopes out.

        Empty input yields an empty fragment: every existing caller keeps its
        exact previous behaviour. This is the privacy boundary for external
        content (e.g. ``qzone_friend`` QQ Zone dynamics) that must never reach
        the LLM prompt by default.
        """
        scopes = sorted({str(s) for s in (exclude_scopes or ()) if s})
        if not scopes:
            return "", []
        placeholders = ", ".join("?" for _ in scopes)
        return f"origin_scope NOT IN ({placeholders})", scopes

    def _shortlist_ids(
        self,
        qlower: str,
        clauses: list[str],
        params: list[Any],
        limit: int,
        *,
        exclude_scopes: frozenset[str] | set[str] | None = None,
    ) -> list[str]:
        """Cheap prefilter before the Python cosine scan.

        Lexical hits come first (FTS5 + LIKE for CJK substrings), then the
        highest-importance rows fill the rest.  Without the fill, a query with
        no literal overlap would lose every semantic match; without the bound,
        every search parses up to ``max_items`` embeddings in pure Python.
        """
        if limit <= 0:
            return []
        scope_clause, scope_params = self._scope_exclusion(exclude_scopes)
        if scope_clause:
            clauses = [*clauses, scope_clause]
            params = [*params, *scope_params]
        where = " AND ".join(clauses)
        ids: list[str] = []
        tokens = re.findall(r"\w+", qlower or "")
        if tokens:
            expression = " ".join(f'"{token}"' for token in tokens)
            rows = self.db.query(
                "SELECT id FROM memory_items WHERE "
                + where
                + " AND id IN (SELECT id FROM memory_fts WHERE memory_fts MATCH ?) "
                "ORDER BY importance DESC, observed_at DESC LIMIT ?",
                (*params, expression, limit),
            )
            ids = [str(row["id"]) for row in rows]
            if not ids:
                # FTS5/unicode61 treats a CJK run as one token, so a substring
                # query ("鲸鲸" inside "鲸鲸今年多大了") misses. Rank LIKE hits
                # by how many query tokens they contain before the importance
                # fill, otherwise a broad token can crowd out the exact match.
                match_expr = " + ".join(
                    "(gist LIKE ? OR summary LIKE ?)" for _ in tokens
                )
                like_params = [f"%{token}%" for token in tokens for _ in range(2)]
                rows = self.db.query(
                    "SELECT id FROM memory_items WHERE "
                    + where
                    + f" AND ({match_expr}) > 0 "
                    "ORDER BY (" + match_expr + ") DESC, importance DESC, "
                    "observed_at DESC LIMIT ?",
                    (*params, *like_params, *like_params, limit),
                )
                ids = [str(row["id"]) for row in rows]
        if len(ids) < limit:
            rows = self.db.query(
                "SELECT id FROM memory_items WHERE "
                + where
                + " ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, "
                "importance DESC, observed_at DESC LIMIT ?",
                (*params, limit),
            )
            seen = set(ids)
            for row in rows:
                item_id = str(row["id"])
                if item_id not in seen:
                    ids.append(item_id)
                    seen.add(item_id)
                if len(ids) >= limit:
                    break
        return ids

    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        session_id: str | None = None,
        types: Iterable[str] | None = None,
        time_start: float | None = None,
        time_end: float | None = None,
        include_forgotten: bool = False,
        exclude_scopes: frozenset[str] | set[str] | None = None,
        viewer_session: str | None = None,
        viewer_role: Any = _UNSET,
    ) -> list[MemoryHit]:
        qvec = self.embedder.embed(query)
        qlower = query.lower().strip()
        clauses = ["1=1"]
        params: list[Any] = []
        if include_forgotten:
            clauses.append("status<>'deleted'")
        else:
            # Archived rows are old, not gone.  Keep them in the candidate
            # pool and apply a score penalty below; a hard active-only filter
            # used to make a true old match unreachable after compaction.
            clauses.append("status IN ('active','archived')")
        if types:
            type_list = list(types)
            clauses.append("type IN (%s)" % ",".join("?" for _ in type_list))
            params.extend(type_list)
        if session_id:
            clauses.append("(scope='global' OR session_id=?)")
            params.append(session_id)
        if time_start is not None:
            clauses.append("(event_time_end IS NULL OR event_time_end >= ?)")
            params.append(float(time_start))
        if time_end is not None:
            clauses.append("(event_time_start IS NULL OR event_time_start <= ?)")
            params.append(float(time_end))
        scope_clause, scope_params = self._scope_exclusion(exclude_scopes)
        if scope_clause:
            clauses.append(scope_clause)
            params.extend(scope_params)
        # T-0073: a non-owner viewer never sees another person's rows.  The
        # gate must be applied BEFORE the shortlist query too, or a hidden row
        # could still enter via ``id IN (shortlist)``.  The owner keeps the
        # old, unrestricted behaviour (he may know everything); callers that
        # pass no viewer at all (owner's CLI / web console) are also ungated.
        if viewer_role is not _UNSET and str(viewer_role or "").strip().lower() != "owner":
            gate_clause, gate_params = self._viewer_gate_sql(viewer_session)
            clauses.append(gate_clause)
            params.extend(gate_params)
        scan_limit = self.max_items
        shortlist = self._shortlist_ids(
            qlower, clauses, params, self.candidate_limit, exclude_scopes=exclude_scopes
        )
        if shortlist:
            clauses.append("id IN (%s)" % ",".join("?" for _ in shortlist))
            params.extend(shortlist)
            scan_limit = len(shortlist)
        sql = (
            "SELECT * FROM memory_items WHERE "
            + " AND ".join(clauses)
            + " ORDER BY importance DESC, observed_at DESC LIMIT ?"
        )
        params.append(scan_limit)
        now = time.time()
        explicit_time = time_start is not None or time_end is not None

        def scored() -> Iterator[MemoryHit]:
            # Iterate the cursor one row at a time.  ``fetchall`` plus a list of
            # hits used to hold two full copies of up to ``max_items`` rows,
            # each carrying a 768-float embedding (~24 KB) -- ~120 MB peak per
            # search, twice per turn.  ``nlargest`` keeps only ``limit`` hits.
            for row in self.db.execute(sql, params):
                item = self._row_to_item(row)
                if is_test_session(item.session_id):
                    continue
                if item.source_type == "bilibili" and is_test_bvid(item.source_id):
                    continue
                if item.valid_to is not None and item.valid_to <= now:
                    continue
                sim = cosine(qvec, item.embedding)
                kw = self._keyword_score(qlower, item)
                days = max(0.0, now - (item.last_accessed_at or item.observed_at)) / 86400.0
                # Forgetting curve: type/importance/access-scaled half-life.
                # An explicit time window already ordered the candidates in
                # time, so recency must not double-penalise that query.
                if explicit_time:
                    recency = 1.0
                else:
                    half_life = max(0.1, self._half_life_days(item))
                    recency = math.exp(-math.log(2) * days / half_life)
                exact = (
                    sim >= self.exact_match_similarity_floor or kw >= 1.0
                )
                if exact:
                    # A real hit must survive age and archival: floor only the
                    # recency term, not the semantic/keyword strength.
                    recency = max(recency, self.exact_match_recency_floor)
                score = (
                    0.55 * sim
                    + 0.25 * item.importance
                    + self.recency_weight * recency
                    + 0.10 * item.confidence
                    + 0.10 * kw
                )
                if kw > 0:
                    score += 0.05
                if item.status != "active" and not exact:
                    score *= self.archived_score_multiplier
                if score <= 0.05:
                    continue
                yield MemoryHit(
                    item=item,
                    score=round(score, 6),
                    similarity=round(sim, 6),
                    keyword=round(kw, 6),
                    reason="semantic+importance+recency+archive",
                )

        hits = heapq.nlargest(max(1, int(limit)), scored(), key=lambda h: h.score)
        self._touch([h.item.id for h in hits])
        return hits

    def _keyword_score(self, qlower: str, item: MemoryItem) -> float:
        if not qlower:
            return 0.0
        hay = f"{item.gist} {item.summary} {' '.join(item.tags)} {' '.join(item.entities)}".lower()
        if qlower in hay:
            return 1.0
        tokens = [t for t in re.split(r"\s+", qlower) if t]
        if not tokens:
            return 0.0
        return sum(1 for token in tokens if token in hay) / len(tokens)

    def _touch(self, ids: list[str]) -> None:
        if not ids:
            return
        now = time.time()
        for item_id in ids:
            self.db.execute(
                "UPDATE memory_items SET access_count=access_count+1, last_accessed_at=? WHERE id=?",
                (now, item_id),
            )

    # ------------------------------------------------------------------- RAG
    def add_rag_chunk(
        self,
        source_type: str,
        source_id: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        chunk_index: int = 0,
        trust_level: str = "internal",
        origin_scope: str = "self",
    ) -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty RAG text")
        meta = dict(metadata or {})
        meta.setdefault("trust_level", str(trust_level))
        meta.setdefault("source", str(origin_scope))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self.db.query_one(
            "SELECT id FROM rag_chunks WHERE source_type=? AND source_id=? AND chunk_index=? AND hash=?",
            (source_type, source_id, int(chunk_index), digest),
        )
        if existing:
            return str(existing["id"])
        chunk_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO rag_chunks(id, source_type, source_id, chunk_index, text, embedding, metadata_json, hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                chunk_id, source_type, source_id, int(chunk_index), text,
                pack_embedding(self.embedder.embed(text)),
                json.dumps(meta, ensure_ascii=False), digest, time.time(),
            ),
        )
        return chunk_id

    def search_rag(
        self,
        query: str,
        limit: int = 5,
        *,
        source_type: str | None = None,
        exclude_scopes: frozenset[str] | set[str] | None = None,
        viewer_session: str | None = None,
        viewer_role: Any = _UNSET,
    ) -> list[RagHit]:
        qvec = self.embedder.embed(query)
        qlower = query.lower().strip()
        if source_type:
            sql = (
                "SELECT * FROM rag_chunks WHERE source_type=? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (source_type, self.max_items)
        else:
            sql = "SELECT * FROM rag_chunks ORDER BY created_at DESC LIMIT ?"
            params = (self.max_items,)
        excluded = {str(s) for s in (exclude_scopes or ()) if s}
        viewer_is_owner = viewer_role is _UNSET or (
            str(viewer_role or "").strip().lower() == "owner"
        )
        item_gate = None if viewer_is_owner else self._viewer_gate_sql(viewer_session)

        def scored() -> Iterator[RagHit]:
            for row in self.db.execute(sql, params):
                if row["source_type"] == "bilibili" and is_test_bvid(row["source_id"]):
                    continue
                text = row["text"]
                sim = cosine(qvec, loads(row["embedding"]))
                kw = 1.0 if qlower and qlower in text.lower() else 0.0
                score = 0.75 * sim + 0.25 * kw
                if score <= 0.03:
                    continue
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                # rag_chunks has no origin_scope column; the scope rides in
                # metadata_json ($.source, written by add_rag_chunk). The
                # filter runs before any hit can escape this generator, so an
                # excluded chunk can only cost a candidate slot, never leak.
                if excluded and str(metadata.get("source", "")) in excluded:
                    continue
                # T-0073: a chunk cut from a memory item inherits that item's
                # visibility.  Chunks without a memory origin (B站, 生活事件)
                # are 鲜鱼娘's own content and stay visible.
                if item_gate is not None and row["source_type"] == "memory":
                    if self.db.query_one(
                        "SELECT 1 AS hit FROM memory_items WHERE id=? LIMIT 1",
                        (str(row["source_id"] or ""),),
                    ) is not None:
                        gate_sql, gate_params = item_gate
                        ok = self.db.query_one(
                            "SELECT 1 AS hit FROM memory_items WHERE id=? AND "
                            + gate_sql,
                            (str(row["source_id"] or ""), *gate_params),
                        )
                        if ok is None:
                            continue
                yield RagHit(
                    id=row["id"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    text=text,
                    metadata=metadata,
                    score=round(score, 6),
                    similarity=round(sim, 6),
                )

        return heapq.nlargest(max(1, int(limit)), scored(), key=lambda h: h.score)

    def _entity_event_ids(self, query: str) -> list[str] | None:
        """Event ids carrying a mention of any entity matched by ``query``.

        ``entities_for_query`` finds the known entities (canonical name or
        alias as a substring of the query); the DISTINCT ``source_id``s of
        their ``entity_mentions`` rows with ``source_type='event'`` are the
        events worth filtering details by.  ``None`` means "no entity signal,
        do not filter" -- both when nothing matched and when matched entities
        have no event-level mentions yet.
        """
        hits = entities_for_query(self.db, query)
        if not hits:
            return None
        # A merged card keeps its mentions (evidence is never moved), so the
        # query must cover the whole family: the live card plus every card
        # merged into it.
        entity_ids: list[str] = []
        for hit in hits:
            for entity_id in entity_family_ids(self.db, str(hit["id"])):
                if entity_id not in entity_ids:
                    entity_ids.append(entity_id)
        if not entity_ids:
            return None
        placeholders = ",".join("?" for _ in entity_ids)
        rows = self.db.query(
            "SELECT DISTINCT source_id FROM entity_mentions "
            f"WHERE source_type='event' AND entity_id IN ({placeholders})",
            tuple(entity_ids),
        )
        ids = [str(row["source_id"]) for row in rows if row["source_id"]]
        return ids or None

    def recall_details(
        self,
        query: str,
        *,
        limit: int = 4,
        time_start: float | None = None,
        time_end: float | None = None,
        viewer_session: str | None = None,
        viewer_role: Any = _UNSET,
    ) -> list[dict[str, Any]]:
        """Detail rows for recall: auto time window + entity pre-filter.

        When both time bounds are missing, the query's time words are parsed
        into a hard window first (ADR-0003 §4.5.3 step 1).  Entity hits
        narrow the search to events that actually mention them; when that
        over-filters to nothing, one unfiltered retry keeps recall generous
        (宁多勿漏) -- an empty result must not hide a real match just because
        the entity layer lags behind the archive.  The detail relevance gate
        (``memory.detail_min_score``, default 0.2) is applied here so both the
        direct call and the retry share one threshold.  Matched details are
        then widened with their nearest same-event siblings
        (``expand_event_context``): a question and its answer are often two
        separate clauses, and matching only the question used to make the
        model answer "记不清" while the answer sat one row away.  A third
        widening follows 1-hop ``entity_edges`` from the matched details'
        entities (``expand_entity_neighbors``): related facts share people,
        not query words, so a "后来呢" answer that shares zero bigrams with
        the question rides in flagged ``via_graph`` — never as an independent
        hit.
        """
        from .details import expand_event_context, search_details
        from .edges import GRAPH_MIN_CONFIDENCE, expand_entity_neighbors

        if time_start is None and time_end is None:
            time_start, time_end = parse_time_window(query, self.tz_name)
        min_score = float(self.config.get("memory.detail_min_score", 0.2))
        event_ids = self._entity_event_ids(query)
        direct = search_details(
            self.db, query, limit=limit,
            time_start=time_start, time_end=time_end,
            event_ids=event_ids, min_score=min_score,
        )
        if not direct and event_ids:
            direct = search_details(
                self.db, query, limit=limit,
                time_start=time_start, time_end=time_end,
                min_score=min_score,
            )
        extras: list[dict[str, Any]] = []
        graph: list[dict[str, Any]] = []
        if direct:
            extras = expand_event_context(
                self.db,
                direct,
                per_event=max(
                    0, int(self.config.get("memory.detail_context_per_event", 2))
                ),
                max_extra=max(
                    0, int(self.config.get("memory.detail_context_max", 4))
                ),
            )
            if self.config.get("memory.graph_expand", 1):
                graph = expand_entity_neighbors(
                    self.db,
                    direct,
                    [*direct, *extras],
                    time_start=time_start,
                    time_end=time_end,
                    max_extra=max(
                        0, int(self.config.get("memory.graph_expand_max", 3))
                    ),
                    per_neighbor=max(
                        0,
                        int(self.config.get("memory.graph_expand_per_neighbor", 2)),
                    ),
                    min_confidence=float(
                        self.config.get(
                            "memory.graph_expand_min_confidence",
                            GRAPH_MIN_CONFIDENCE,
                        )
                    ),
                )
        rows = [*direct, *extras, *graph]
        # T-0073: details are verbatim quotes, and the black-list policy shares
        # facts, never other people's words.  A non-owner viewer only gets
        # rows from their own session; the owner skips the filter -- except
        # for bound private rooms (ADR-0014): the owner sees their summaries,
        # never their raw chat transcript.
        role_text = str(viewer_role or "").strip().lower() if viewer_role is not _UNSET else ""
        if rows and role_text == "owner":
            blocked = self.non_owner_private_sessions()
            if blocked:
                rows = [
                    r
                    for r in rows
                    if str(r.get("session_id") or "") not in blocked
                ]
        elif rows and viewer_role is not _UNSET:
            allows = self._viewer_allows_session(viewer_session)
            rows = [r for r in rows if allows(r.get("session_id"))]
        return rows

    def recall(
        self,
        query: str,
        *,
        limit_memory: int = 6,
        limit_rag: int = 4,
        limit_details: int = 6,
        include_quotes: bool = False,
        time_start: float | None = None,
        time_end: float | None = None,
    ) -> RecallResult:
        if time_start is None and time_end is None:
            time_start, time_end = parse_time_window(query, self.tz_name)
        mem_hits = self.search(
            query, limit=limit_memory, time_start=time_start, time_end=time_end
        )
        rag_hits = self.search_rag(query, limit=limit_rag)
        details = self.recall_details(
            query, limit=limit_details,
            time_start=time_start, time_end=time_end,
        )
        return self.format_recall(
            query, mem_hits, rag_hits,
            details=details, include_quotes=include_quotes,
        )

    @staticmethod
    def _prompt_memory_hits(hits: list[MemoryHit]) -> list[MemoryHit]:
        """Drop raw archived lines when an explicit private memory exists.

        ``event_archive`` rows are the user's own sentence fragments ("鲸鲸",
        "跟我说声晚安，可以吗？"); they are useful when nothing better exists,
        but next to a ``model_action``/private memory they are pure noise and
        can push the actual fact out of the model's attention.  Only this
        prompt view is filtered -- search/API callers still see every hit.
        """
        if not hits:
            return []
        has_private = any(
            str(hit.item.trust_level or "").lower() == "private" for hit in hits
        )
        if not has_private:
            return hits
        return [
            hit
            for hit in hits
            if str(hit.item.trust_level or "").lower() == "private"
            or str(hit.item.source_type or "") != "event_archive"
        ]

    def format_recall(
        self,
        query: str,
        mem_hits: list[MemoryHit],
        rag_hits: list[RagHit],
        *,
        details: list[dict[str, Any]] | None = None,
        include_quotes: bool = False,
        memory_first: bool = False,
    ) -> RecallResult:
        details = list(details or [])
        mem_hits = self._prompt_memory_hits(list(mem_hits))
        refs = (
            [f"detail:{row['id']}" for row in details]
            + [f"memory:{h.item.id}" for h in mem_hits]
            + [f"rag:{h.id}" for h in rag_hits]
        )
        lines: list[str] = []
        names = self.party_names
        detail_lines: list[str] = []
        memory_lines: list[str] = []
        rag_lines: list[str] = []
        matched = [
            row for row in details
            if not row.get("context") and not row.get("via_graph")
        ]
        sibling_context = [row for row in details if row.get("context")]
        graph_context = [row for row in details if row.get("via_graph")]
        if matched:
            detail_lines.append("[细节召回]")
            for row in matched:
                when = (
                    human_time(row["occurred_at"], self.tz_name)
                    if row.get("occurred_at") else "—"
                )
                title = row.get("event_title") or "未命名事件"
                detail_lines.append(f"- ({when}) [{title}] {row['text']}")
                if include_quotes and row.get("quote_text"):
                    detail_lines.append(f"  原话：「{row['quote_text']}」")
        if sibling_context:
            # Same-event context, not an independent hit: the question and its
            # answer are often separate clauses.  Keep it visually separate so
            # the model uses it while still ranking the real match first.
            detail_lines.append("[同事件补充 · 同一次对话的上下文，不是独立命中]")
            for row in sibling_context:
                when = (
                    human_time(row["occurred_at"], self.tz_name)
                    if row.get("occurred_at") else "—"
                )
                title = row.get("event_title") or "未命名事件"
                detail_lines.append(f"- ({when}) [{title}] {row['text']}")
                if include_quotes and row.get("quote_text"):
                    detail_lines.append(f"  原话：「{row['quote_text']}」")
        if graph_context:
            # Graph-backed context (1-hop entity edges): shares people with the
            # match, not query words.  Keep it visually separate, same idea as
            # the same-event block above — it is supporting evidence, not a hit.
            detail_lines.append("[关联补充 · 经人物关系带出的上下文，不是独立命中]")
            for row in graph_context:
                when = (
                    human_time(row["occurred_at"], self.tz_name)
                    if row.get("occurred_at") else "—"
                )
                title = row.get("event_title") or "未命名事件"
                via = str(row.get("graph_via") or "").strip()
                suffix = f"  （{via}）" if via else ""
                detail_lines.append(f"- ({when}) [{title}] {row['text']}{suffix}")
                if include_quotes and row.get("quote_text"):
                    detail_lines.append(f"  原话：「{row['quote_text']}」")
        if mem_hits:
            memory_lines.append("[记忆召回]")
            for hit in mem_hits:
                when = (
                    human_time(hit.item.event_time_start, self.tz_name)
                    if hit.item.event_time_start
                    else human_date(hit.item.observed_at, self.tz_name)
                )
                source = hit.item.source_type or "self"
                who = attribution_line(hit.item, names)
                who_part = f"{who} " if who else ""
                memory_lines.append(
                    f"- ({when}) {who_part}{hit.item.gist} "
                    f"[score={hit.score:.2f}; trust={hit.item.trust_level}; source={source}]"
                )
        if rag_hits:
            rag_lines.append("[外部证据召回]")
            for hit in rag_hits:
                snippet = hit.text.replace("\n", " ")[:180]
                trust = str(hit.metadata.get("trust_level", "external"))
                source = str(hit.metadata.get("source", hit.source_type))
                # Bilibili chunks carry the video title/up in metadata; the
                # body starts with the same title, but a 180-char snippet can
                # lose it.  Put it in front so "which video" is answerable.
                title = str(hit.metadata.get("title") or "").strip()
                up = str(hit.metadata.get("up") or "").strip()
                head = ""
                if title:
                    head = f"《{title[:80]}》" + (f" @{up[:40]}" if up else "") + "："
                rag_lines.append(
                    f"- {head}{snippet} [score={hit.score:.2f}; ref={hit.id[:8]}; "
                    f"trust={trust}; source={source}]"
                )
        # Memory questions put the fact summaries first: the raw detail window
        # can be dominated by a neighbouring conversation (晚安/照片), and the
        # model used to answer "翻不出来" while the answering gist sat below it.
        if memory_first:
            lines.extend(memory_lines)
        lines.extend(detail_lines)
        if not memory_first:
            lines.extend(memory_lines)
        lines.extend(rag_lines)
        text = "\n".join(lines)
        result = RecallResult(
            query=query,
            memory_hits=mem_hits,
            rag_hits=rag_hits,
            details=details,
            refs=refs,
            ephemeral_text=text,
            meta_directory=self.meta_directory(limit=8),
            ephemeral=True,
        )
        self.log_decision(
            "recall",
            {"query": query, "memory": len(mem_hits), "rag": len(rag_hits),
             "details": len(details)},
            action="ephemeral_context",
            outcome=f"refs={len(refs)}",
            model="memory",
        )
        return result

    # --------------------------------------------------------- meta memory
    def meta_directory(
        self,
        limit: int = 12,
        *,
        exclude_scopes: frozenset[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """A compact 'what do I know about' directory for gist-level awareness.

        Only the newest ``meta_scan_limit`` active rows are inspected.  The
        directory is a summary, not an exhaustive index; scanning every memory
        made this cost grow with the whole database on each turn.
        ``exclude_scopes`` keeps external-origin rows out of the buckets (the
        directory text goes straight into the prompt).
        """
        scope_clause, scope_params = self._scope_exclusion(exclude_scopes)
        where = "status='active'"
        if scope_clause:
            where += f" AND {scope_clause}"
        rows = self.db.query(
            "SELECT gist, summary, tags_json, entities_json, importance, observed_at, "
            "access_count, session_id, source_type, source_id "
            f"FROM memory_items WHERE {where} ORDER BY observed_at DESC LIMIT ?",
            (*scope_params, self.meta_scan_limit),
        )
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            if is_test_session(row["session_id"]):
                continue
            if row["source_type"] == "bilibili" and is_test_bvid(row["source_id"]):
                continue
            labels: list[str] = []
            try:
                labels.extend(json.loads(row["tags_json"] or "[]"))
                labels.extend(json.loads(row["entities_json"] or "[]"))
            except json.JSONDecodeError:
                pass
            if not labels:
                labels = [str(row["gist"])[:12]]
            for label in _unique(labels)[:4]:
                key = str(label).strip()
                if not key:
                    continue
                bucket = buckets.setdefault(
                    key, {"name": key, "count": 0, "last_seen": 0.0, "importance": 0.0}
                )
                bucket["count"] += 1
                bucket["last_seen"] = max(bucket["last_seen"], float(row["observed_at"]))
                bucket["importance"] = max(bucket["importance"], float(row["importance"]))
        items = sorted(
            buckets.values(),
            key=lambda b: (b["importance"], b["last_seen"], b["count"]),
            reverse=True,
        )
        for item in items:
            item["last_seen_text"] = human_date(item["last_seen"], self.tz_name)
            item["importance"] = round(item["importance"], 2)
        return items[: max(1, int(limit))]

    # ------------------------------------------------------------- compaction
    def compact(self, *, dry_run: bool = True, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        rows = self.db.query(
            "SELECT * FROM memory_items WHERE status='active' ORDER BY observed_at ASC"
        )
        stats = {
            "checked": 0,
            "kept": 0,
            "compressed": 0,
            "archived": 0,
            "protected": 0,
            "dry_run": dry_run,
            "changes": [],
        }
        for row in rows:
            item = self._row_to_item(row)
            stats["checked"] += 1
            if self._is_protected(item, now):
                stats["protected"] += 1
                continue
            half_life = self._half_life_days(item)
            days = max(0.0, now - (item.last_accessed_at or item.observed_at)) / 86400.0
            retention = math.exp(-math.log(2) * days / max(0.1, half_life))
            target = self._target_tier(item, retention, now)
            new_tier = max(item.tier, target)
            new_status = "archived" if new_tier >= 4 else "active"
            if new_tier == item.tier and new_status == item.status:
                stats["kept"] += 1
                continue
            change = {
                "id": item.id,
                "gist": item.gist[:60],
                "from_tier": item.tier,
                "to_tier": new_tier,
                "from_status": item.status,
                "to_status": new_status,
                "retention": round(retention, 4),
                "half_life_days": round(half_life, 2),
            }
            stats["changes"].append(change)
            if new_status == "archived":
                stats["archived"] += 1
            else:
                stats["compressed"] += 1
            if not dry_run:
                self.db.execute(
                    "UPDATE memory_items SET tier=?, status=?, stability=?, updated_at=? WHERE id=?",
                    (new_tier, new_status, half_life, now, item.id),
                )
        if not dry_run:
            self.log_decision(
                "compaction",
                {"checked": stats["checked"], "compressed": stats["compressed"], "archived": stats["archived"]},
                action="apply",
                outcome="ok",
                model="rules",
            )
        return stats

    def _is_protected(self, item: MemoryItem, now: float) -> bool:
        if item.type in {"persona", "prospective"}:
            return True
        if now - item.observed_at < 48 * 3600:
            return True
        # Spaced-repetition protection: memories that are actually recalled
        # must not be archived just because they are old.  access_count and
        # last_accessed_at are updated by search()'s _touch().
        if item.access_count >= self.archive_min_access_count > 0:
            return True
        last_access = item.last_accessed_at or item.observed_at
        if (
            self.archive_recent_access_days > 0
            and now - last_access < self.archive_recent_access_days * 86400.0
        ):
            return True
        text = f"{item.gist} {item.summary} {' '.join(item.tags)}"
        if any(word in text for word in ("记住", "别忘", "重要", "承诺", "答应", "生日")):
            return True
        if "pin" in item.tags or "protected" in item.tags:
            return True
        if item.valid_to is not None and item.valid_to > now:
            return True
        return False

    @staticmethod
    def _half_life_days(item: MemoryItem) -> float:
        base = {
            "preference": 180.0,
            "relationship": 365.0,
            "semantic": 120.0,
            "episodic": 30.0,
            "prospective": 365.0,
            "emotional": 90.0,
            "persona": 3650.0,
        }.get(item.type, 60.0)
        return base * (1.0 + 2.0 * item.importance) * (1.0 + math.log1p(item.access_count))

    @staticmethod
    def _target_tier(item: MemoryItem, retention: float, now: float) -> int:
        if item.importance >= 0.8:
            return 1
        if retention < 0.08:
            return 4
        if retention < 0.20 and item.importance < 0.6:
            return 3
        if retention < 0.45:
            return 2
        return 1

    # ------------------------------------------------------------- open loops
    def add_open_loop(
        self,
        text: str,
        *,
        due_at: float | None = None,
        kind: str = "follow_up",
        source_ref: str | None = None,
        session_id: str | None = None,
        subject: str = "",
    ) -> str:
        loop_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO open_loops(id, created_at, due_at, kind, text, status, source_ref, "
            "session_id, subject) VALUES (?,?,?,?,?, 'open', ?,?,?)",
            (
                loop_id,
                time.time(),
                due_at,
                kind,
                text,
                source_ref,
                session_id,
                _normalise_party(subject),
            ),
        )
        return loop_id

    def list_open_loops(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM open_loops WHERE status='open' ORDER BY COALESCE(due_at, created_at) LIMIT ?",
            (int(limit),),
        )
        return [dict(row) for row in rows]

    def close_open_loop(self, loop_id: str) -> None:
        self.db.execute("UPDATE open_loops SET status='done' WHERE id=?", (loop_id,))

    # ----------------------------------------------------------- conversation
    def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        used_refs: Iterable[str] = (),
        ephemeral: bool = False,
        sender_id: str | None = None,
        sender_name: str = "",
        trip_id: str | None = None,
    ) -> int:
        """Append one turn.

        ``sender_id`` / ``sender_name`` are the platform account id and the
        display name at send time (v13).  They are only meaningful for user
        turns in a shared session: a group id identifies the group, not the
        member who spoke.  Assistant turns leave both empty.

        ``trip_id`` (ADR-0014) tags the turn as part of one roam trip.  Only
        that trip's summary reads these rows; ordinary turns stay NULL.
        """
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at, "
            "token_count, used_refs_json, ephemeral, sender_id, sender_name, trip_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, role, content, now, _estimate_tokens(content),
                json.dumps(list(used_refs), ensure_ascii=False), 1 if ephemeral else 0,
                None if sender_id is None else str(sender_id),
                str(sender_name or "")[:64],
                None if trip_id is None else str(trip_id),
            ),
        )
        turn_id = int(cur.lastrowid)
        self.events.observe_turn(turn_id, session_id, role, content, now)
        return turn_id

    def recent_turns(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM conversation_turns WHERE session_id=? AND ephemeral=0 "
            "ORDER BY id DESC LIMIT ?",
            (session_id, int(limit)),
        )
        return [dict(row) for row in reversed(rows)]

    def recent_episodes(
        self,
        session_id: str,
        *,
        before: float,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Whole-day archives that already fell out of the transcript window.

        ``before`` is the timestamp of the oldest turn ``[最近对话]`` still
        carries: anything ending later is already visible there as raw lines,
        so returning it would replay the same hours twice.  ``limit`` counts
        *days*, not rows -- a quiet day leaves a gap instead of pushing a
        different day out of view.

        Rows come back oldest-first.  Both ``summary`` (the factual timeline,
        with ``[HH:MM] title`` headers) and ``summary_voice`` (the same day in
        her own voice) are returned so the caller can compose them; neither is
        parsed here.
        """
        rows = self.db.query(
            "SELECT id, title, start_at, end_at, summary, summary_voice, turn_count "
            "FROM episodes WHERE session_id=? AND end_at IS NOT NULL AND end_at<? "
            "ORDER BY end_at DESC LIMIT ?",
            (str(session_id), float(before), max(1, int(limit))),
        )
        return [dict(row) for row in reversed(rows)]

    def today_timeline(
        self,
        session_id: str,
        *,
        now: float | None = None,
        max_chars: int | None = None,
        before: float | None = None,
    ) -> str:
        """Compact local-day timeline for the current session.

        Turns are chronological; each line is ``HH:MM 主人/你: content``. When
        the full day exceeds the character cap, recent turns are kept and the
        earlier part is represented by closed-event summaries from today.
        Returns an empty string when there is nothing today.

        ``before`` trims the range to turns older than that timestamp. The
        prompt builder passes the timestamp of the oldest turn already carried
        by ``[最近对话]``, so the two blocks complement each other instead of
        replaying the same transcript twice.
        """
        now = time.time() if now is None else float(now)
        cap = int(self.today_timeline_chars if max_chars is None else max_chars)
        cap = max(1, cap)
        start, _ = day_bounds(now, self.tz_name)
        upper = (now + 1e-6) if before is None else min(now, float(before))
        rows = self.db.query(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE session_id=? AND ephemeral=0 AND created_at>=? AND created_at<? "
            "ORDER BY created_at, id",
            (str(session_id), start, upper),
        )
        if not rows:
            return ""

        header = f"[今日时间线 {human_date(now, self.tz_name)}]"
        full = "\n".join([header, *(self._timeline_line(row) for row in rows)])
        if len(full) <= cap:
            return full

        summaries = [
            (float(row["end_at"]), self._event_summary_line(row))
            for row in self.db.query(
                "SELECT * FROM conversation_events WHERE session_id=? "
                "AND status IN ('closed','archived') AND start_at>=? "
                "AND end_at IS NOT NULL AND end_at<=? ORDER BY start_at, created_at",
                (str(session_id), start, now),
            )
        ]

        # Walk backwards from the newest turn; keep the longest suffix that
        # fits together with summaries of events ending before that suffix.
        recent: list[str] = []
        accepted_summaries: list[str] = []
        for row in reversed(rows):
            candidate = [self._timeline_line(row), *recent]
            cutoff = float(row["created_at"])
            summary_lines = [line for end_at, line in summaries if end_at < cutoff]
            candidate_text = "\n".join([header, *summary_lines, *candidate])
            if len(candidate_text) <= cap:
                recent = candidate
                accepted_summaries = summary_lines
            else:
                break

        if not recent:
            # Even one turn plus a summary is too much: keep the newest turn,
            # trimmed to the remaining budget.
            line = self._timeline_line(rows[-1])
            budget = max(0, cap - len(header) - 1)
            if len(line) > budget:
                line = (line[: max(0, budget - 1)] + "…") if budget > 0 else ""
            return "\n".join(part for part in (header, line) if part)[:cap]

        return "\n".join([header, *accepted_summaries, *recent])

    def _timeline_line(self, row: Any) -> str:
        stamp = to_local(float(row["created_at"]), self.tz_name).strftime("%H:%M")
        speaker = "主人" if row["role"] == "user" else "你"
        content = " ".join(str(row["content"]).split())
        if len(content) > _TIMELINE_TURN_CHARS:
            content = content[: _TIMELINE_TURN_CHARS - 1] + "…"
        return f"{stamp} {speaker}: {content}"

    def _event_summary_line(self, row: Any) -> str:
        stamp = to_local(float(row["start_at"]), self.tz_name).strftime("%H:%M")
        summary = " ".join(str(row["summary"] or row["title"] or "").split())
        if len(summary) > _TIMELINE_SUMMARY_CHARS:
            summary = summary[: _TIMELINE_SUMMARY_CHARS - 1] + "…"
        return f"{stamp} 事件: {summary}" if summary else f"{stamp} 事件"

    def short_term_digest(
        self,
        limit: int = 5,
        now: float | None = None,
        *,
        exclude_scopes: frozenset[str] | set[str] | None = None,
    ) -> list[MemoryItem]:
        now = time.time() if now is None else now
        cutoff = now - self.short_term_ttl_days * 86400
        scope_clause, scope_params = self._scope_exclusion(exclude_scopes)
        where = "status='active' AND observed_at>=? AND tier<=2"
        if scope_clause:
            where += f" AND {scope_clause}"
        rows = self.db.query(
            f"SELECT * FROM memory_items WHERE {where} "
            "ORDER BY importance DESC, observed_at DESC LIMIT ?",
            (cutoff, *scope_params, int(limit)),
        )
        return [
            item
            for item in (self._row_to_item(row) for row in rows)
            if not is_test_session(item.session_id)
            and not (
                item.source_type == "bilibili" and is_test_bvid(item.source_id)
            )
        ]

    # --------------------------------------------------------------- logging
    def log_decision(
        self,
        kind: str,
        input_data: Any,
        *,
        action: str,
        outcome: str,
        model: str = "",
        latency_ms: float = 0.0,
        session_id: str | None = None,
        at: float | None = None,
    ) -> None:
        raw = json.dumps(input_data, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        # ``at`` lets simulated-time callers (runtime tests, day-bucketed
        # counters) stamp the row with the turn's clock; production paths
        # leave it unset and get the wall clock, as before.
        at_value = time.time() if at is None else float(at)
        self.db.execute(
            "INSERT INTO decision_log(at, kind, input_hash, output_json, action, outcome, latency_ms, model, session_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                at_value, kind, digest, raw[:8000], action, outcome,
                float(latency_ms), model, session_id,
            ),
        )

    def metric(self, key: str, value: float, tags: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (time.time(), key, float(value), json.dumps(tags or {}, ensure_ascii=False)),
        )

    # ---------------------------------------------------------------- helpers
    def _normalize_event_time(
        self,
        event_time: str | float | None,
        start: float | None,
        end: float | None,
        granularity: str | None,
        confidence: float | None,
    ) -> tuple[float | None, float | None, str, float]:
        if start is not None or end is not None:
            return (
                float(start) if start is not None else (float(end) if end is not None else None),
                float(end) if end is not None else (float(start) if start is not None else None),
                granularity or "minute",
                1.0 if confidence is None else _clamp(confidence),
            )
        if event_time is None or (isinstance(event_time, str) and not event_time.strip()):
            return None, None, "unknown", 0.0
        if isinstance(event_time, (int, float)):
            ts = float(event_time)
            return ts, ts, granularity or "second", 1.0 if confidence is None else _clamp(confidence)
        raw = str(event_time).strip()
        if _DATE_ONLY.match(raw):
            ts = parse_when(raw, self.tz_name)
            start_ts, end_ts = day_bounds(ts, self.tz_name)
            return start_ts, end_ts, "day", 0.9 if confidence is None else _clamp(confidence)
        ts = parse_when(raw, self.tz_name)
        return ts, ts, granularity or "minute", 0.8 if confidence is None else _clamp(confidence)

    @staticmethod
    def _importance(text: str, type: str) -> float:
        base = {
            "preference": 0.75,
            "relationship": 0.80,
            "prospective": 0.90,
            "semantic": 0.60,
            "emotional": 0.70,
            "episodic": 0.40,
            "persona": 1.0,
        }.get(type, 0.45)
        text = text or ""
        if any(word in text for word in ("记住", "别忘", "重要", "承诺", "答应", "约定")):
            return 0.95
        if any(word in text for word in ("生日", "真名", "住址", "电话", "喜欢", "讨厌", "害怕")):
            return max(base, 0.75)
        if any(word in text for word in ("今天", "刚才", "刚刚")):
            return max(base, 0.45)
        return base

    def _row_to_item(self, row: Any) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            type=row["type"],
            scope=row["scope"],
            session_id=row["session_id"],
            event_id=row["event_id"],
            episode_id=row["episode_id"],
            gist=row["gist"],
            summary=row["summary"],
            event_time_start=row["event_time_start"],
            event_time_end=row["event_time_end"],
            time_granularity=row["time_granularity"],
            time_confidence=float(row["time_confidence"]),
            observed_at=float(row["observed_at"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            stability=float(row["stability"]),
            tier=int(row["tier"]),
            status=row["status"],
            trust_level=str(row["trust_level"] or "internal"),
            origin_scope=str(row["origin_scope"] or "self"),
            source_type=row["source_type"],
            source_id=row["source_id"],
            evidence=row["evidence"],
            entities=_json_list(row["entities_json"]),
            tags=_json_list(row["tags_json"]),
            embedding=loads(row["embedding"]),
            access_count=int(row["access_count"]),
            last_accessed_at=row["last_accessed_at"],
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            subject=str(_row_get(row, "subject", "") or ""),
            about=_json_list(_row_get(row, "about_json", "[]")),
        )


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that a not-yet-migrated database may still be missing."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None and default is not None else value


_PARTY_ALIASES = {
    "owner": "owner",
    "主人": "owner",
    "主人哥哥": "owner",
    "用户": "owner",
    "friend": "friend",
    "姐姐": "friend",
    "group": "group",
    "群聊": "group",
    "external": "external",
    "bilibili": "external",
    "b站": "external",
    "unknown": "unknown",
    "": "unknown",
}


_PARTY_LABELS = {
    "owner": "主人哥哥",
    "friend": "姐姐",
    "group": "群聊",
    "external": "外部来源",
    "unknown": "来源不明",
}


def _normalise_party(value: Any) -> str:
    """Map a free-form attribution to a stable tag.

    Unknown values are kept verbatim (short) so a future role does not need a
    code change to start being labelled correctly.
    """
    key = str(value or "").strip().lower()
    if key in _PARTY_ALIASES:
        return _PARTY_ALIASES[key]
    return key[:24]


def party_label(value: Any, names: dict[str, str] | None = None) -> str:
    """Human-readable attribution for a prompt line.

    ``names`` overrides the defaults with live display names from
    ``owner_bindings`` so a renamed friend still renders correctly.
    """
    key = _normalise_party(value)
    if names and key in names and str(names[key]).strip():
        return str(names[key]).strip()
    return _PARTY_LABELS.get(key, key or _PARTY_LABELS["unknown"])


def attribution_line(item: "MemoryItem", names: dict[str, str] | None = None) -> str:
    """``[姐姐说的·关于主人哥哥]`` style prefix; empty when nothing is known."""
    speaker = party_label(item.subject, names) if item.subject else ""
    about = [party_label(a, names) for a in item.about if str(a or "").strip()]
    about = [a for a in about if a and a != speaker]
    if speaker and about:
        return f"[{speaker}说的·关于{'、'.join(about)}]"
    if speaker:
        return f"[{speaker}]"
    if about:
        return f"[关于{'、'.join(about)}]"
    return ""


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _make_gist(text: str, limit: int = 80) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _estimate_tokens(text: str) -> int:
    # Rough mixed Chinese/Latin estimate; replace with a tokenizer when available.
    return max(1, int(len(text) / 2.2))
