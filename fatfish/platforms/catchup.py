"""T-0028: online catch-up for missed QQ private messages.

When the QQ adapter (re)connects -- NapCat restarted, dropped login, or the
WebSocket flapped -- messages sent while she was offline may never reach the
turn stream (NTQQ re-delivers most offline messages right after login, but a
drop on our side loses them for good). This service scans the owner first,
then the friend, then every person card ("人物卡") carrying a QQ uin, finds
what was never answered, and runs ONE catch-up turn per contact, in that
order.

Design anchors (task spec + existing precedents):

* Judgment source of truth is the local ``conversation_turns`` stream: a
  session is unanswered when its last permanent ``user`` turn is newer (by
  stream id) than its last ``assistant`` turn. NapCat's
  ``get_friend_msg_history`` extension (verified present in
  ``vendor/napcat/NapCat.Shell/napcat.mjs`` as ``GetFriendMsgHistory``) is an
  OPTIONAL enhancement that recovers messages which never arrived as events;
  any failure degrades to the local stream alone. Never a hard dependency.
* One catch-up turn per contact. The fixed instruction ("你掉线错过了一些
  消息…") is a system-authored prompt marked ephemeral, exactly like the
  self-continue prompt (T-0027); the missed messages ride the turn as
  ``wrap_untrusted`` evidence (same trust surface as the QQ Zone / Bilibili
  bypasses), so hostile-looking message text can never hijack the turn.
* Her reply is a response to existing messages, not a proactive message: the
  adapter delivers it through the normal ``qq_send`` path, untouched by the
  proactive quotas. Quiet hours still apply
  (``qq.catchup_respect_quiet_hours``): a night scan defers itself by
  queueing a ``qq_catchup`` outbox item for the next quiet end (08:00) via
  ``enqueue_retry``; the handler re-scans when due, so by then anything that
  was answered in the meantime reads as ``nothing_new``.
* Every scan writes a per-session meta watermark (``catchup_last_scan_at:``
  in ``schema_meta``) BEFORE judging -- the same crash-safe shape as
  ``maybe_poll_qzone`` -- and reconnect storms are throttled by
  ``qq.catchup_min_interval_seconds``.
* Audit: ``decision_log`` rows with ``kind='catchup'``; one ``scanned`` row
  per full pass (for the morning report) plus one specific row per target
  (``replied`` / ``nothing_new`` / ``skipped_quiet`` / ...).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..clock import human_time, in_quiet_hours as _in_quiet_hours, next_quiet_end as _next_quiet_end
from ..owner import (
    ROLE_FRIEND,
    ROLE_OWNER,
    binding_exists,
    friend_uin_from_binding,
    owner_uin_from_binding,
)
from ..security import TrustLevel, wrap_untrusted

CATCHUP_KIND = "catchup"
CATCHUP_RETRY_KIND = "qq_catchup"
CATCHUP_WATERMARK_PREFIX = "catchup_last_scan_at:"

DEFAULT_MAX_MESSAGES = 20
DEFAULT_MIN_INTERVAL_SECONDS = 300.0
# Person cards with a QQ uin are scanned after owner/friend.  Cards are the
# roster the persona actually knows ("人物卡"), so a bound teacher/family
# member is caught up without hard-coding every role here.  The cap keeps a
# reconnect from fanning out over a long tail of auto-created private cards.
DEFAULT_MAX_CARD_TARGETS = 20
# How closely a history row must match a local user turn (content + time) to
# count as "already in the stream" -- NTQQ re-delivery may lag its original
# send time by a bit.
DEDUPE_WINDOW_SECONDS = 300.0
# How many recent local user turns take part in the content dedupe.
DEDUPE_TURN_LIMIT = 40

DEFAULT_PROMPT = "你掉线错过了一些消息，按顺序看完再回复，不要跳过任何一条。"


@dataclass(slots=True)
class CatchupTarget:
    """One scannable private session."""

    label: str  # "owner" | "friend" | the person card's display name
    uin: str
    session_id: str


@dataclass(slots=True)
class HistoryMessage:
    """One missed history row, including the images behind a ``[图片]``.

    The service keeps the text-only shape working too: ``diff_history`` still
    accepts ``(text, ts)`` tuples (tests and older callers), and equality with
    a 2-tuple is preserved so existing assertions read naturally.
    """

    text: str
    at: float
    images: list[str] = field(default_factory=list)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, tuple) and len(other) == 2:
            return (self.text, self.at) == other
        if isinstance(other, HistoryMessage):
            return (self.text, self.at, self.images) == (
                other.text,
                other.at,
                other.images,
            )
        return NotImplemented


def _history_text(item: Any) -> str:
    return str(getattr(item, "text", item[0] if isinstance(item, tuple) else ""))


def _history_at(item: Any) -> float:
    try:
        return float(getattr(item, "at", item[1] if isinstance(item, tuple) else 0.0))
    except (TypeError, ValueError):
        return 0.0


def _history_images(item: Any) -> list[str]:
    images = getattr(item, "images", None)
    return [str(url) for url in images] if isinstance(images, list) else []


def clean_uin(value: Any) -> str:
    """A numeric QQ uin, or ``""``. Tolerates QZone's ``o123456789`` form."""
    text = str(value or "").strip()
    if text[:1] in {"o", "O"} and text[1:].isdigit():
        return text[1:]
    return text if text.isdigit() else ""


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


class CatchUpService:
    """Scan owner + friend sessions after (re)connect and catch up once.

    The service owns judgment, ordering, watermarks and audit. Platform
    mechanics (history fetch, turn execution, delivery, outbox retry items)
    are injected as callables by the QQ adapter, so the whole judgment layer
    stays offline-testable.
    """

    def __init__(self, runtime: Any, config: Any):
        self.runtime = runtime
        self.config = config
        self.db = runtime.db
        self.tz_name = config.timezone_name
        self.max_messages = max(1, int(config.get("qq.catchup_max_messages", DEFAULT_MAX_MESSAGES)))
        self.min_interval = max(0.0, float(config.get("qq.catchup_min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)))
        self.respect_quiet = bool(config.get("qq.catchup_respect_quiet_hours", True))
        self.fetch_history_enabled = bool(config.get("qq.catchup_fetch_history", True))
        # "接口改为人物卡里的" (boss, 2026-10-31): the roster is the set of
        # live person cards, not the owner/friend binding table alone.
        self.from_cards = _as_bool(config.get("qq.catchup_from_cards", True), True)
        self.max_card_targets = max(
            0, int(config.get("qq.catchup_max_people", DEFAULT_MAX_CARD_TARGETS))
        )
        self.include_unknown_cards = _as_bool(
            config.get("qq.catchup_include_unknown_cards", False), False
        )
        self.self_uin = clean_uin(config.get("qq.self_id", ""))
        self.quiet_start = str(config.get("memory.quiet_start", "23:30") or "23:30")
        self.quiet_end = str(config.get("memory.quiet_end", "08:00") or "08:00")
        self.prompt = str(config.get("qq.catchup_prompt", "") or "").strip() or DEFAULT_PROMPT

    # ------------------------------------------------------------- targets
    def resolve_targets(self) -> list[CatchupTarget]:
        """Owner first, friend second, then the person cards with a QQ uin.

        A session shared by two roles scans once (owner wins).  The card list
        is the same roster the persona sees in "人物卡": live ``kind='person'``
        cards with a numeric uin.  ``list_people`` already folds merged-away
        cards; self is excluded explicitly.
        """
        targets: list[CatchupTarget] = []
        seen: set[str] = set()
        owner = self._owner_uin()
        if owner:
            session_id = f"qq:private:{owner}"
            targets.append(CatchupTarget(label="owner", uin=owner, session_id=session_id))
            seen.add(session_id)
        friend = self._friend_uin()
        if friend:
            session_id = f"qq:private:{friend}"
            if session_id not in seen:
                targets.append(CatchupTarget(label="friend", uin=friend, session_id=session_id))
                seen.add(session_id)
        if self.from_cards and self.max_card_targets:
            targets.extend(self._card_targets(seen))
        return targets

    def _card_targets(self, seen: set[str]) -> list[CatchupTarget]:
        """Person cards with a QQ uin, in roster order, capped and deduped."""
        from ..memory.entities import list_people  # lazy: platforms -> memory

        try:
            cards = list_people(self.db, limit=max(self.max_card_targets * 4, 50))
        except Exception:
            return []
        out: list[CatchupTarget] = []
        for card in cards:
            if len(out) >= self.max_card_targets:
                break
            # Slot cards are handled by the owner/friend resolution above;
            # including them here would resurrect a stale QQ uin after a
            # binding moved to a web session.
            if str(card.get("id") or "") in {"person:self", "person:owner", "person:friend"}:
                continue
            uin = clean_uin(card.get("uin"))
            if not uin or (self.self_uin and uin == self.self_uin):
                continue
            session_id = f"qq:private:{uin}"
            if session_id in seen:
                continue
            attrs = card.get("attrs") if isinstance(card.get("attrs"), dict) else {}
            source = str(attrs.get("source") or "").strip().lower()
            relation = str(card.get("relation") or "").strip()
            # Auto-created private cards (source='chat', no relation) are
            # acquaintances, not the curated roster: do not read/reply to
            # their history after an outage unless explicitly opted in.
            if not self.include_unknown_cards and not (
                relation or source in {"cli", "binding", "manual"}
            ):
                continue
            label = str(card.get("name") or "").strip() or "person"
            out.append(CatchupTarget(label=label, uin=uin, session_id=session_id))
            seen.add(session_id)
        return out

    def _owner_uin(self) -> str:
        """Owner uin: the consent binding first, then its card mirror.

        ``owner_bindings`` is the single consent point; ``person:owner`` is
        its card mirror (CLI ``person sync``), usable only when no binding
        row exists at all.  The QZone cookie is the BOT's
        identity and ``qzone.owner_uin`` is the QZone posting account, so
        neither is a valid owner source here (2026-10-31 incident).
        """
        if binding_exists(self.db, ROLE_OWNER):
            # A web binding means "no QQ owner target"; never resurrect a
            # stale QQ uin from the card in that case.
            return owner_uin_from_binding(self.db) or ""
        return self._card_uin("person:owner")

    def _friend_uin(self) -> str:
        if binding_exists(self.db, ROLE_FRIEND):
            return friend_uin_from_binding(self.db) or ""
        return self._card_uin("person:friend")

    def _card_uin(self, entity_id: str) -> str:
        from ..memory.entities import entity_card  # lazy: platforms -> memory

        try:
            card = entity_card(self.db, entity_id)
        except Exception:
            return ""
        if not card:
            return ""
        return clean_uin(card.get("uin"))

    def display_name(self, target: CatchupTarget) -> str:
        """The persona's name for this contact, for the evidence header only."""
        getter = getattr(getattr(self.runtime, "owner", None), "display_name_for", None)
        if callable(getter):
            try:
                found = getter(target.session_id)
            except Exception:
                found = None
            if found:
                return str(found)
        if target.label not in {"owner", "friend"}:
            # Person-card contact: the card's canonical name is the safest
            # header (the uin never enters the evidence text).
            return target.label or "联系人"
        key = (
            "relationship.owner_display_name"
            if target.label == "owner"
            else "relationship.friend_display_name"
        )
        fallback = "主人哥哥" if target.label == "owner" else "姐姐"
        return str(self.config.get(key, fallback) or fallback) or fallback

    # ------------------------------------------------------------ judgment
    def unanswered_state(self, session_id: str) -> dict[str, Any]:
        """Compare the last permanent user turn against the last assistant turn.

        Stream ids, not timestamps: the turn stream is append-only, so
        ``last_user.id > last_assistant.id`` is exactly "she has not spoken
        since they did", immune to clock skew between event delivery and the
        reply.
        """
        state: dict[str, Any] = {"last_user": None, "last_assistant": None, "unanswered": False}
        try:
            user_row = self.db.query_one(
                "SELECT id, content, created_at FROM conversation_turns "
                "WHERE session_id=? AND role='user' AND ephemeral=0 ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            assistant_row = self.db.query_one(
                "SELECT id, content, created_at FROM conversation_turns "
                "WHERE session_id=? AND role='assistant' AND ephemeral=0 ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
        except Exception:  # pragma: no cover - DB trouble reads as "no data"
            return state
        state["last_user"] = dict(user_row) if user_row else None
        state["last_assistant"] = dict(assistant_row) if assistant_row else None
        state["unanswered"] = bool(
            user_row
            and (
                assistant_row is None
                or int(user_row["id"]) > int(assistant_row["id"])
            )
        )
        return state

    def _local_user_turns(self, session_id: str) -> list[tuple[str, float]]:
        try:
            rows = self.db.query(
                "SELECT content, created_at FROM conversation_turns "
                "WHERE session_id=? AND role='user' AND ephemeral=0 ORDER BY id DESC LIMIT ?",
                (session_id, DEDUPE_TURN_LIMIT),
            )
        except Exception:  # pragma: no cover - defensive
            return []
        return [(str(row["content"] or "").strip(), float(row["created_at"])) for row in rows]

    def diff_history(self, session_id: str, history: list[Any]) -> list[HistoryMessage]:
        """History rows that never reached the local turn stream.

        A row counts as missed when it is newer than her last reply (older
        ones were answered), carries a usable timestamp, and does not match a
        recent local user turn (content equal within the re-delivery window) --
        that last filter is what keeps NTQQ's own offline re-delivery from
        being answered twice (once here, once via the normal event path).

        ``history`` rows may be ``(text, ts)`` tuples (legacy/tests) or
        :class:`HistoryMessage` with image URLs; missed rows always come back
        as ``HistoryMessage`` so the vision path can see the images.
        """
        state = self.unanswered_state(session_id)
        last_assistant = state["last_assistant"]
        cutoff = float(last_assistant["created_at"]) if last_assistant else 0.0
        local_turns = self._local_user_turns(session_id)
        missed: list[HistoryMessage] = []
        for item in history:
            text = _history_text(item)
            ts = _history_at(item)
            clean = str(text or "").strip()
            if not clean or ts <= 0:
                continue
            if ts <= cutoff:
                continue
            if any(clean == content and abs(ts - at) <= DEDUPE_WINDOW_SECONDS for content, at in local_turns):
                continue
            missed.append(
                HistoryMessage(
                    text=clean[:500],
                    at=ts,
                    images=_history_images(item),
                )
            )
        missed.sort(key=lambda item: item.at)
        return missed

    def build_evidence(self, target: CatchupTarget, missed: list[Any]) -> list[str]:
        """The missed messages as one wrapped untrusted block (or nothing)."""
        if not missed:
            return []
        name = self.display_name(target)
        lines = [f"掉线期间错过的消息（{name}，共 {len(missed)} 条，按时间顺序）："]
        for item in missed:
            text = _history_text(item)
            ts = _history_at(item)
            stamp = human_time(ts, self.tz_name) if ts > 0 else "时间未知"
            lines.append(f"- [{stamp}] {text}")
        block = "\n".join(lines)
        return [
            wrap_untrusted(
                block,
                kind="catchup_messages",
                source="qq:offline_history",
                trust=TrustLevel.EXTERNAL,
            )
        ]

    # ---------------------------------------------------------- watermarks
    def _watermark_key(self, session_id: str) -> str:
        return f"{CATCHUP_WATERMARK_PREFIX}{session_id}"

    def _meta_float(self, key: str) -> float | None:
        raw = self.db.get_meta(key)
        try:
            return float(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def scan_due(self, session_id: str, *, now: float) -> bool:
        """True when the last scan of this session is older than the interval.

        Connect storms (NapCat flapping) hit this throttle instead of
        re-scanning on every single reconnect.
        """
        last = self._meta_float(self._watermark_key(session_id))
        return last is None or now - last >= self.min_interval

    def mark_scanned(self, session_id: str, *, now: float) -> None:
        # Written BEFORE the scan (maybe_poll_qzone's shape): a crash cannot
        # turn the next reconnect into an immediate identical re-scan.
        self.db.set_meta(self._watermark_key(session_id), f"{float(now):.6f}")

    # -------------------------------------------------------- quiet hours
    def in_quiet_hours(self, *, now: float) -> bool:
        return _in_quiet_hours(float(now), self.quiet_start, self.quiet_end, self.tz_name)

    def next_quiet_end(self, *, now: float) -> float:
        return _next_quiet_end(float(now), self.quiet_end, self.tz_name)

    # ---------------------------------------------------------------- audit
    def log(
        self,
        input_data: dict[str, Any],
        *,
        action: str,
        outcome: str,
        session_id: str | None = None,
        now: float | None = None,
    ) -> None:
        try:
            self.runtime.memory.log_decision(
                CATCHUP_KIND,
                input_data,
                action=action,
                outcome=str(outcome),
                model="runtime",
                session_id=session_id,
                at=now,
            )
        except Exception:  # audit must never break the scan
            pass

    # ----------------------------------------------------------------- scan
    def scan_all(
        self,
        *,
        now: float | None = None,
        trigger: str = "connect",
        connected: bool = True,
        fetch_history: Callable[[str, int], list[Any]] | None = None,
        run_turn: Callable[..., dict[str, Any]] | None = None,
        enqueue_retry: Callable[[CatchupTarget, float], bool] | None = None,
    ) -> dict[str, Any]:
        """One ordered pass over owner + friend. Never raises."""
        now = time.time() if now is None else float(now)
        results: list[dict[str, Any]] = []
        try:
            targets = self.resolve_targets()
        except Exception as exc:  # pragma: no cover - defensive
            targets = []
            results.append({"label": "-", "outcome": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
        for target in targets:
            try:
                results.append(
                    self.scan_target(
                        target,
                        now=now,
                        connected=connected,
                        fetch_history=fetch_history,
                        run_turn=run_turn,
                        enqueue_retry=enqueue_retry,
                    )
                )
            except Exception as exc:  # one broken target must not stop the other
                results.append(
                    {
                        "label": target.label,
                        "session_id": target.session_id,
                        "outcome": "error",
                        "error": f"{type(exc).__name__}: {exc}"[:200],
                    }
                )
        missed_total = sum(int(r.get("missed") or 0) for r in results)
        replied = [str(r.get("session_id")) for r in results if r.get("outcome") == "replied"]
        self.log(
            {
                "trigger": trigger,
                "targets": [t.label for t in targets],
                "outcomes": [r.get("outcome") for r in results],
                "missed_total": missed_total,
                "replied": replied,
            },
            action="scanned",
            outcome="ok" if targets else "no_targets",
            now=now,
        )
        return {"trigger": trigger, "results": results, "missed_total": missed_total}

    def scan_target(
        self,
        target: CatchupTarget,
        *,
        now: float | None = None,
        force: bool = False,
        connected: bool = True,
        fetch_history: Callable[[str, int], list[Any]] | None = None,
        run_turn: Callable[..., dict[str, Any]] | None = None,
        enqueue_retry: Callable[[CatchupTarget, float], bool] | None = None,
    ) -> dict[str, Any]:
        """Judge one session and, if needed, run its single catch-up turn."""
        now = time.time() if now is None else float(now)
        base = {"label": target.label, "uin": target.uin, "session_id": target.session_id}
        if not force and not self.scan_due(target.session_id, now=now):
            return {**base, "outcome": "skipped_interval"}
        self.mark_scanned(target.session_id, now=now)

        if not connected:
            self.log(
                {**base, "reason": "not_connected"},
                action="skipped_offline",
                outcome="no_login",
                session_id=target.session_id,
                now=now,
            )
            return {**base, "outcome": "skipped_offline"}

        if self.respect_quiet and self.in_quiet_hours(now=now):
            fire_at = self.next_quiet_end(now=now)
            queued = bool(enqueue_retry(target, fire_at)) if enqueue_retry is not None else False
            self.log(
                {**base, "reason": "quiet_hours", "fire_at": fire_at, "retry_queued": queued},
                action="skipped_quiet",
                outcome="deferred" if queued else "no_queue",
                session_id=target.session_id,
                now=now,
            )
            return {**base, "outcome": "skipped_quiet", "fire_at": fire_at, "retry_queued": queued}

        missed: list[HistoryMessage] = []
        history_ok: bool | None = None
        if self.fetch_history_enabled and fetch_history is not None:
            try:
                history = fetch_history(target.uin, self.max_messages) or []
                missed = self.diff_history(target.session_id, history)
                history_ok = True
            except Exception as exc:
                # Degraded mode: judge from the local stream alone. Never a
                # hard dependency on the NapCat extension action.
                history_ok = False
                self.log(
                    {**base, "reason": "history_fetch_failed", "error": f"{type(exc).__name__}: {exc}"[:160]},
                    action="scanned",
                    outcome="history_degraded",
                    session_id=target.session_id,
                    now=now,
                )

        state = self.unanswered_state(target.session_id)
        if not state["unanswered"] and not missed:
            self.log(
                {**base, "history_ok": history_ok},
                action="nothing_new",
                outcome="ok",
                session_id=target.session_id,
                now=now,
            )
            return {**base, "outcome": "nothing_new", "missed": 0, "history_ok": history_ok}

        if run_turn is None:  # pragma: no cover - adapter always provides one
            return {**base, "outcome": "error", "error": "no_turn_runner", "missed": len(missed)}
        try:
            result = run_turn(target, missed, now=now)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            self.log(
                {**base, "missed": len(missed), "history_ok": history_ok, "error": error},
                action="replied",
                outcome="error",
                session_id=target.session_id,
                now=now,
            )
            return {**base, "outcome": "error", "error": error, "missed": len(missed)}
        outcome = str((result or {}).get("outcome") or ("replied" if (result or {}).get("ok") else "error"))
        self.log(
            {
                **base,
                "missed": len(missed),
                "history_ok": history_ok,
                "queued": (result or {}).get("queued"),
            },
            action="replied",
            outcome=outcome,
            session_id=target.session_id,
            now=now,
        )
        return {
            **base,
            "outcome": outcome,
            "missed": len(missed),
            "history_ok": history_ok,
            "result": result,
        }
