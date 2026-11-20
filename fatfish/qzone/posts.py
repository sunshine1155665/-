"""QQ Zone write path: LLM drafts, desire threshold, owner approval, quota,
outbox delivery.

Nothing here publishes on its own. The pipeline is:

1. ``create_post_draft`` -- called from the ``post_qzone`` action. Gates on
   ``qzone.enabled`` / ``qzone.read_only``, clips the content, screens it for
   credentials and secret notes, then stores a ``draft`` row. With
   ``qzone.require_approval=false`` the persona's own ``desire`` self-rating
   decides: at or above ``qzone.auto_post_desire_threshold`` the draft is
   auto-approved straight into the outbox (still quota-checked, still
   quiet-hours deferred, ``decided_by='desire-threshold'``); below it -- or
   with a missing/invalid rating -- the draft waits for the owner. With
   ``require_approval=true`` desire is recorded but never acts.
2. ``review_post`` -- the owner's path from ``draft`` to the outbox
   (``decided_by='owner'``). An approval *reserves* the day's slot
   (``approved`` counts towards the quota), so two approvals in one day
   cannot overtake ``qzone.max_posts_per_day`` even if the first post is
   still queued. ``max_posts_per_day=0`` means unlimited (boss mandate
   2026-10-25): the quota only bites when the value is positive.
3. ``execute_outbox_item`` -- the ``qzone_post`` outbox handler. It is the
   only writer that touches the network, and its status check is what makes
   outbox redelivery safe: a row that is no longer ``approved`` is skipped
   instead of posted twice.

Every transition lands in ``decision_log`` under kind ``qzone_post`` so the
owner can audit exactly what went out and when; auto-publishes record the
desire value and the threshold that let them through.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from ..clock import day_bounds, in_quiet_hours, next_quiet_end
from ..config import Config
from ..db import Database
from ..outbox import Outbox
from ..privacy import SecretNotes
from ..security import contains_credentials, redact_outbound
from .client import QzoneClient, QzoneError

VALID_POST_STATUSES = ("draft", "approved", "rejected", "posted", "failed", "blocked")
OUTBOX_KIND = "qzone_post"

# Publish retries: 1 first try + 3 retries, mirroring the process_outbox
# attempt accounting (each claim increments ``attempts``).
POST_MAX_ATTEMPTS = 4

DEFAULT_MAX_POSTS_PER_DAY = 1
DEFAULT_REQUIRE_APPROVAL = True
DEFAULT_CONTENT_MAX_CHARS = 2000
DEFAULT_QUIET_START = "23:30"
DEFAULT_QUIET_END = "08:00"
DEFAULT_AUTO_POST_DESIRE_THRESHOLD = 0.7

# decided_by vocabulary: who let a row past the draft stage.
DECIDED_BY_OWNER = "owner"
DECIDED_BY_DESIRE = "desire-threshold"


def normalize_desire(value: Any) -> float | None:
    """Parse the persona's desire self-rating; anything invalid is ``None``.

    ``None`` means "below threshold": the LLM omitting the field, or gaming
    it with garbage, must never widen its own publish rights. Booleans are
    rejected even though Python calls them ints, and out-of-range numbers do
    not clamp -- a 1.5 is a broken rating, not an extra eager one.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(dotted, default)
    if isinstance(config, dict):
        current: Any = config
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


class QzonePostService:
    """Draft / review / publish lifecycle for QQ Zone shuoshuo."""

    def __init__(
        self,
        db: Database,
        config: Config,
        outbox: Outbox | None = None,
        client: QzoneClient | None = None,
        secrets: SecretNotes | None = None,
    ):
        self.db = db
        self.config = config
        self._outbox = outbox
        self._client = client
        self._secrets = secrets

    # ---------------------------------------------------------------- config
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("qzone.enabled", False))

    @property
    def read_only(self) -> bool:
        return bool(self.config.get("qzone.read_only", True))

    @property
    def require_approval(self) -> bool:
        return bool(self.config.get("qzone.require_approval", DEFAULT_REQUIRE_APPROVAL))

    @property
    def max_posts_per_day(self) -> int:
        return max(0, int(self.config.get("qzone.max_posts_per_day", DEFAULT_MAX_POSTS_PER_DAY)))

    @property
    def content_max_chars(self) -> int:
        return max(1, int(self.config.get("qzone.content_max_chars", DEFAULT_CONTENT_MAX_CHARS)))

    @property
    def auto_post_desire_threshold(self) -> float:
        """Desire at/above which a draft skips the owner (require_approval=false)."""
        try:
            threshold = float(
                self.config.get("qzone.auto_post_desire_threshold", DEFAULT_AUTO_POST_DESIRE_THRESHOLD)
            )
        except (TypeError, ValueError):
            return DEFAULT_AUTO_POST_DESIRE_THRESHOLD
        if threshold != threshold:  # NaN
            return DEFAULT_AUTO_POST_DESIRE_THRESHOLD
        return min(1.0, max(0.0, threshold))

    def quiet_hours(self) -> tuple[str, str]:
        start = str(
            self.config.get("qzone.quiet_hours_start", DEFAULT_QUIET_START)
        ).strip() or DEFAULT_QUIET_START
        end = str(
            self.config.get("qzone.quiet_hours_end", DEFAULT_QUIET_END)
        ).strip() or DEFAULT_QUIET_END
        return start, end

    def _resolve_outbox(self) -> Outbox:
        if self._outbox is None:
            raise RuntimeError("qzone post service requires an outbox")
        return self._outbox

    def attach_outbox(self, outbox: Outbox) -> None:
        """Give the runtime a way to hand its outbox over after construction."""
        self._outbox = outbox

    def resolve_client(self) -> QzoneClient:
        if self._client is None:
            self._client = QzoneClient.from_config(self.config)
        return self._client

    # ----------------------------------------------------------------- draft
    def create_post_draft(
        self,
        content: str,
        *,
        desire: float | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        created_by: str = "llm",
        now: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Gate, screen and store one draft. Never touches the network.

        ``desire`` is the persona's own 0-1 eagerness self-rating. It is
        always recorded on the row; it only *acts* when
        ``require_approval=false`` and it reaches
        ``auto_post_desire_threshold`` -- anything missing or invalid counts
        as below threshold, so the review path stays the default.
        """
        now = time.time() if now is None else float(now)
        if not self.enabled:
            return {"ok": False, "error": "qzone_disabled"}
        if self.read_only:
            return {"ok": False, "error": "qzone_read_only"}
        text = " ".join(str(content or "").split())
        if not text:
            return {"ok": False, "error": "empty_content"}
        limit = self.content_max_chars
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        desire = normalize_desire(desire)

        status = "draft"
        error: str | None = None
        if self._content_is_sensitive(text):
            # Store the *redacted* text: the draft row is an audit trail, not
            # a second copy of the cookie jar.
            text, _hits = redact_outbound(text)
            status = "blocked"
            error = "sensitive_content"

        key = idempotency_key or f"qzone_post_draft:{uuid.uuid4().hex}"
        cur = self.db.execute(
            "INSERT OR IGNORE INTO qzone_posts("
            "content, media_json, status, created_by, session_id, trace_id, "
            "idempotency_key, error, created_at, decided_at, desire) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                text,
                "[]",
                status,
                "llm" if created_by not in ("llm", "owner") else created_by,
                str(session_id)[:128] if session_id else None,
                str(trace_id)[:128] if trace_id else None,
                key,
                error,
                now,
                now if status == "blocked" else None,
                desire,
            ),
        )
        row = self.db.query_one(
            "SELECT id, status FROM qzone_posts WHERE idempotency_key=?", (key,)
        )
        if row is None:  # pragma: no cover - sqlite should have the row
            return {"ok": False, "error": "draft_insert_failed"}
        post_id = int(row["id"])
        if cur.rowcount == 0:
            # Explicit key already stored: report the existing row, create
            # nothing (the INSERT was ignored, so there is no new row).
            return {
                "ok": True,
                "duplicate": True,
                "result": {"post_id": post_id, "status": str(row["status"])},
            }

        if status == "blocked":
            self._log(
                {
                    "post_id": post_id,
                    "stage": "draft",
                    "reason": "sensitive_content",
                    "session_id": session_id,
                },
                action="blocked",
                outcome="denied",
                session_id=session_id,
            )
            return {"ok": False, "error": "sensitive_content", "result": {"post_id": post_id, "status": "blocked"}}

        self._log(
            {
                "post_id": post_id,
                "stage": "draft",
                "chars": len(text),
                "desire": desire,
                "session_id": session_id,
            },
            action="draft_created",
            outcome="ok",
            session_id=session_id,
        )

        if self.require_approval:
            # Review stays mandatory; the rating is recorded but never acts.
            return {"ok": True, "result": {"post_id": post_id, "status": "pending_approval"}}

        # Explicit opt-out of review: her own desire rating decides. At or
        # above the threshold the draft goes straight to the outbox (still
        # behind the daily quota and the quiet window); below it -- or with
        # no rating at all -- she waits for the owner like everyone else.
        threshold = self.auto_post_desire_threshold
        if desire is None or desire < threshold:
            return {"ok": True, "result": {"post_id": post_id, "status": "pending_approval"}}

        quota = self.quota_status(now=now)
        # max_posts_per_day=0 means unlimited (boss mandate 2026-10-25):
        # the cap only exists when the configured value is positive.
        if 0 < quota["max_posts_per_day"] <= quota["posted_today"]:
            self.db.execute(
                "UPDATE qzone_posts SET status='rejected', decided_at=?, decided_by=?, error=? WHERE id=?",
                (now, DECIDED_BY_DESIRE, "quota_exceeded", post_id),
            )
            self._log(
                {"post_id": post_id, "stage": "auto_publish", "quota": quota, "desire": desire},
                action="rejected",
                outcome="quota_exceeded",
                session_id=session_id,
            )
            return {
                "ok": False,
                "error": "quota_exceeded",
                "result": {"post_id": post_id, "status": "rejected"},
            }
        self._enqueue(post_id, now=now, session_id=session_id)
        self.db.execute(
            "UPDATE qzone_posts SET status='approved', decided_at=?, decided_by=? WHERE id=? AND status='draft'",
            (now, DECIDED_BY_DESIRE, post_id),
        )
        self._log(
            {
                "post_id": post_id,
                "stage": "auto_publish",
                "desire": desire,
                "threshold": threshold,
                "require_approval": False,
            },
            action="approved",
            outcome="desire_threshold",
            session_id=session_id,
        )
        return {"ok": True, "result": {"post_id": post_id, "status": "queued"}}

    def _content_is_sensitive(self, text: str) -> bool:
        """Credential shapes or a verbatim secret note must not leave the box."""
        if contains_credentials(text):
            return True
        try:
            notes = self._secrets if self._secrets is not None else SecretNotes(self.db)
        except Exception:  # a broken notes table must not block drafts
            return False
        self._secrets = notes
        for note in notes.list(limit=100):
            value = " ".join(str(note.get("text") or "").split())
            if value and value in text:
                return True
        return False

    # ---------------------------------------------------------------- review
    def review_post(
        self,
        post_id: int,
        decision: str,
        *,
        now: float | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Owner verdict on a draft. ``None`` means the row does not exist."""
        decision = str(decision or "").strip().lower()
        if decision not in ("approved", "rejected"):
            raise ValueError(f"invalid decision: {decision}")
        now = time.time() if now is None else float(now)
        row = self.db.query_one("SELECT * FROM qzone_posts WHERE id=?", (int(post_id),))
        if row is None:
            return None
        if str(row["status"]) != "draft":
            return {"ok": False, "error": "not_draft", "post": self.post_to_dict(row)}

        if decision == "rejected":
            self.db.execute(
                "UPDATE qzone_posts SET status='rejected', decided_at=?, decided_by=? WHERE id=? AND status='draft'",
                (now, DECIDED_BY_OWNER, int(post_id)),
            )
            self._log(
                {"post_id": int(post_id), "stage": "review"},
                action="rejected",
                outcome="ok",
                session_id=session_id,
            )
            return {"ok": True, "post": self.get_post(int(post_id))}

        quota = self.quota_status(now=now)
        # max_posts_per_day=0 means unlimited (boss mandate 2026-10-25).
        if 0 < quota["max_posts_per_day"] <= quota["posted_today"]:
            # The draft stays pending: rejecting it here would silently delete
            # the owner's approved intent, and they may want to push it tomorrow.
            self._log(
                {"post_id": int(post_id), "stage": "review", "quota": quota},
                action="approved",
                outcome="quota_exceeded",
                session_id=session_id,
            )
            return {"ok": False, "error": "quota_exceeded", "quota": quota}

        self._enqueue(int(post_id), now=now, session_id=session_id)
        self.db.execute(
            "UPDATE qzone_posts SET status='approved', decided_at=?, decided_by=? WHERE id=? AND status='draft'",
            (now, DECIDED_BY_OWNER, int(post_id)),
        )
        self._log(
            {"post_id": int(post_id), "stage": "review"},
            action="approved",
            outcome="ok",
            session_id=session_id,
        )
        return {"ok": True, "post": self.get_post(int(post_id))}

    def _enqueue(self, post_id: int, *, now: float, session_id: str | None = None) -> int:
        outbox = self._resolve_outbox()
        start, end = self.quiet_hours()
        available = now
        if in_quiet_hours(now, start, end, self.config.timezone_name):
            # Quiet window: hold the post until it ends (23:30 -> next 08:00).
            available = next_quiet_end(now, end, self.config.timezone_name)
        item_id, _created = outbox.enqueue(
            OUTBOX_KIND,
            {"post_id": int(post_id)},
            idempotency_key=f"{OUTBOX_KIND}:{int(post_id)}",
            available_at=available,
            max_attempts=POST_MAX_ATTEMPTS,
        )
        if available > now:
            self._log(
                {"post_id": int(post_id), "stage": "enqueue", "available_at": available},
                action="deferred",
                outcome="quiet_hours",
                session_id=session_id,
            )
        return int(item_id)

    # --------------------------------------------------------------- quota
    def posts_today(self, *, now: float | None = None) -> int:
        """Rows that consumed (or reserved) today's slot.

        ``approved`` counts alongside ``posted``: approval reserves the slot,
        so approving a second draft the same day is a quota error even before
        the first one actually posts. With ``max_posts_per_day=0`` the count
        is informational only -- the quota is disabled.
        """
        now = time.time() if now is None else float(now)
        start, end = day_bounds(now, self.config.timezone_name)
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM qzone_posts "
            "WHERE status IN ('approved','posted') AND decided_at>=? AND decided_at<?",
            (start, end),
        )
        return int(row["n"]) if row else 0

    def quota_status(self, *, now: float | None = None) -> dict[str, Any]:
        """Today's slot usage. ``max_posts_per_day=0`` means unlimited."""
        now = time.time() if now is None else float(now)
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM qzone_posts WHERE status='approved'"
        )
        max_posts = self.max_posts_per_day
        return {
            "posted_today": self.posts_today(now=now),
            "approved_pending": int(row["n"]) if row else 0,
            "max_posts_per_day": max_posts,
            "unlimited": max_posts <= 0,
        }

    # --------------------------------------------------------------- outbox
    def execute_outbox_item(self, item: Any) -> dict[str, Any]:
        """``qzone_post`` handler. Contract: ``{"ok": bool, "error"?: str}``.

        The status check is the redelivery guard: only an ``approved`` row may
        hit the network, so a replayed outbox item after a successful post (or
        a lease recovery) is a no-op instead of a duplicate shuoshuo.
        """
        payload = getattr(item, "payload", None) or {}
        raw_id = payload.get("post_id")
        try:
            post_id = int(raw_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"invalid_post_id: {raw_id!r}"}
        row = self.db.query_one("SELECT * FROM qzone_posts WHERE id=?", (post_id,))
        if row is None:
            return {"ok": False, "error": "post_missing"}
        status = str(row["status"])
        if status != "approved":
            self._log(
                {"post_id": post_id, "stage": "handler", "row_status": status},
                action="skipped",
                outcome="not_approved",
            )
            return {"ok": True, "skipped": f"status={status}"}

        self._log(
            {"post_id": post_id, "stage": "handler", "attempt": int(getattr(item, "attempts", 0))},
            action="publishing",
            outcome="start",
        )
        try:
            result = self.resolve_client().publish_text(str(row["content"]))
        except QzoneError as exc:
            error = str(exc)[:500]
            attempts = int(getattr(item, "attempts", 0) or 0)
            max_attempts = int(getattr(item, "max_attempts", POST_MAX_ATTEMPTS) or POST_MAX_ATTEMPTS)
            if attempts >= max_attempts:
                self.db.execute(
                    "UPDATE qzone_posts SET status='failed', error=? WHERE id=? AND status='approved'",
                    (error, post_id),
                )
                self._log(
                    {"post_id": post_id, "stage": "handler", "error": error},
                    action="failed",
                    outcome="terminal",
                )
            else:
                self._log(
                    {"post_id": post_id, "stage": "handler", "error": error, "attempt": attempts},
                    action="failed",
                    outcome="retry_scheduled",
                )
            return {"ok": False, "error": error}
        except Exception as exc:  # a crash must not strand the outbox lease
            self._log(
                {"post_id": post_id, "stage": "handler", "error": f"{type(exc).__name__}: {exc}"},
                action="failed",
                outcome="error",
            )
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}

        self.db.execute(
            "UPDATE qzone_posts SET status='posted', posted_at=?, post_result_json=?, error=NULL "
            "WHERE id=? AND status='approved'",
            (time.time(), json.dumps(result, ensure_ascii=False, default=str)[:4000], post_id),
        )
        self._log(
            {"post_id": post_id, "stage": "handler", "tid": result.get("tid"), "url": result.get("url")},
            action="posted",
            outcome="ok",
        )
        return {"ok": True, "result": result}

    # -------------------------------------------------------------- queries
    def get_post(self, post_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM qzone_posts WHERE id=?", (int(post_id),))
        return self.post_to_dict(row) if row else None

    def list_posts(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        params: list[Any] = []
        where = "1=1"
        if status:
            if status not in VALID_POST_STATUSES:
                raise ValueError(f"invalid status: {status}")
            where = "status=?"
            params.append(status)
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM qzone_posts WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        )
        return [self.post_to_dict(row) for row in rows]

    @staticmethod
    def post_to_dict(row: Any) -> dict[str, Any]:
        try:
            result = json.loads(row["post_result_json"] or "null")
        except (TypeError, ValueError, json.JSONDecodeError):
            result = None
        try:
            media = json.loads(row["media_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            media = []
        if not isinstance(media, list):
            media = []
        return {
            "id": int(row["id"]),
            "content": str(row["content"]),
            "media": media,
            "status": str(row["status"]),
            "created_by": str(row["created_by"]),
            "session_id": row["session_id"],
            "trace_id": row["trace_id"],
            "error": row["error"],
            "result": result,
            "created_at": float(row["created_at"]),
            "decided_at": row["decided_at"],
            "posted_at": row["posted_at"],
            "desire": row["desire"],
            "decided_by": row["decided_by"],
        }

    def status(self) -> dict[str, Any]:
        """Write-path health for the Web panel / CLI ``qzone status``."""

        def _count(where: str) -> int:
            row = self.db.query_one(f"SELECT COUNT(*) AS n FROM qzone_posts WHERE {where}")
            return int(row["n"]) if row else 0

        return {
            "write_enabled": self.enabled and not self.read_only,
            "require_approval": self.require_approval,
            "max_posts_per_day": self.max_posts_per_day,
            "quota_unlimited": self.max_posts_per_day <= 0,
            "auto_post_desire_threshold": self.auto_post_desire_threshold,
            "content_max_chars": self.content_max_chars,
            "quiet_hours": list(self.quiet_hours()),
            "quota": self.quota_status(),
            "drafts": _count("status='draft'"),
            "posted": _count("status='posted'"),
            "failed": _count("status='failed'"),
            "blocked": _count("status='blocked'"),
            "rejected": _count("status='rejected'"),
        }

    # -------------------------------------------------------------- internal
    def _log(
        self,
        input_data: dict[str, Any],
        *,
        action: str,
        outcome: str,
        session_id: str | None = None,
    ) -> None:
        try:
            from ..memory import MemoryService

            memory = MemoryService(self.db, self.config)
            memory.log_decision(
                "qzone_post",
                input_data,
                action=action,
                outcome=outcome,
                model="qzone_posts",
                session_id=session_id,
            )
        except Exception:  # audit must never break the pipeline it audits
            pass


def install_outbox_handler(
    handlers: dict[str, Callable[[Any], Any]], service: QzonePostService
) -> dict[str, Callable[[Any], Any]]:
    """Register ``qzone_post`` on a handler mapping (returns the same dict)."""
    handlers[OUTBOX_KIND] = service.execute_outbox_item
    return handlers
