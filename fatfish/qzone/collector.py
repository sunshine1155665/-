"""Read-only QQ Zone ingestion: feed rows -> episodic memories -> privacy scope.

This module only reads. Cookies/login live in ``QzoneClient``; nothing here
posts, likes, comments or deletes remote content.

Privacy contract
----------------
``origin_scope`` is the leak boundary between the feed store and the LLM
prompt (see ``memory/service.py::exclude_scopes``):

* author is the owner (``qzone.owner_uin``, falling back to the cookie ``uin``
  -- the cookie is by definition the owner's own credential) →
  ``origin_scope='qzone'``, ``author_scope='self'``.
* anyone else (friend circle or stranger) → ``origin_scope='qzone_friend'``,
  kept out of every prompt-facing recall by default
  (``qzone.friends_content_to_llm=false``).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from ..clock import to_local
from ..config import Config
from ..memory import MemoryService
from ..memory.entities import sync_qzone_author
from ..memory.tags import sync_tags
from .client import MAX_FEED_ITEMS, QzoneClient, QzoneError
from .posts import QzonePostService

CIRCUIT_BREAK_THRESHOLD = 3
MEMORY_SOURCE_TYPE = "qzone"
PLATFORM = "qzone"

_META_FAILURES = "qzone_consecutive_failures"
_META_BREAKER = "qzone_breaker_open"
_META_DIGEST_DATE = "last_qzone_digest_date"


def _clean_comments(raw: Any) -> list[dict[str, str]]:
    """Normalize an embedded-comment list; tolerate parser/shape drift."""
    if not isinstance(raw, (list, tuple)):
        return []
    comments: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        comments.append(
            {
                "author": str(entry.get("author") or "").strip(),
                "text": text,
            }
        )
    return comments


class QzoneCollector:
    def __init__(self, memory: MemoryService, config: Config, client: QzoneClient | None = None):
        self.memory = memory
        self.config = config
        self._client = client
        self.breaker_threshold = max(
            1, int(config.get("qzone.circuit_break_failures", CIRCUIT_BREAK_THRESHOLD))
        )
        # The write path shares the collector's config/client resolution but
        # lives in its own service (drafts/review/quota/outbox handler).
        self.posts = QzonePostService(memory.db, config, client=client)

    # ------------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        """Consumed like ``platforms/qq.py``: read from config, actually
        checked before every background poll (never a dead field)."""
        return bool(self.config.get("qzone.enabled", False))

    @property
    def read_only(self) -> bool:
        """True while the write path is locked shut (config default)."""
        return bool(self.config.get("qzone.read_only", True))

    @property
    def friends_content_to_llm(self) -> bool:
        return bool(self.config.get("qzone.friends_content_to_llm", False))

    @property
    def feeds_optional(self) -> bool:
        """True when a feeds-only outage must not trip the poll breaker.

        The friend-feed endpoint is the flakiest read surface (endpoint
        churn/risk control); profiles may mark it optional so a poll that
        still got the own-shuoshuo list counts as ``partial`` instead of a
        consecutive failure. Default false keeps the strict breaker.
        """
        return bool(self.config.get("qzone.feeds_optional", False))

    @property
    def friend_fallback(self) -> bool:
        """聚合 feed 挂掉时，改为逐个已知好友拉 ``emotion_cgi``。

        2026-10-29 实测：``feeds3_html_more`` 对 ic2 模块被判未登录
        （``-3000/-4001``），但同一个 cookie 拉**单个好友**的说说正常。
        代价是每人 1 个请求，所以只覆盖显式配置的 ``qzone.friend_uins``。
        """
        return bool(self.config.get("qzone.friend_fallback", True))

    @property
    def friend_uins(self) -> list[str]:
        """回退路径要拉的好友 uin。

        优先读 ``qzone.friend_uins``；为空时读环境变量
        ``QZONE_FRIEND_UINS``（逗号分隔）。走环境变量是为了**不把真实 QQ 号
        写进受版本管理的 ``config/*.toml``**（与 ``QZONE_UIN``/``QZONE_SKEY``
        同一套约定）。
        """
        raw: Any = self.config.get("qzone.friend_uins", []) or []
        if not raw:
            env_raw = (os.environ.get("QZONE_FRIEND_UINS") or "").strip()
            raw = [part for part in env_raw.replace(";", ",").split(",")]
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[str] = []
        for item in raw:
            value = str(item).strip()
            if value.isdigit() and value not in out:
                out.append(value)
        return out

    @property
    def friend_fallback_count(self) -> int:
        raw = int(self.config.get("qzone.friend_fallback_count", 5) or 5)
        return max(1, min(raw, MAX_FEED_ITEMS))

    # ------------------------------------------------------------- write path
    def create_post_draft(
        self,
        content: str,
        *,
        desire: float | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Start a shuoshuo draft (see ``QzonePostService.create_post_draft``).

        Never publishes on its own: with ``require_approval=false`` the
        persona's ``desire`` self-rating may send it straight to the outbox,
        otherwise it waits for the owner's approval in Web/CLI. Only the
        outbox handler touches the network afterwards.
        """
        return self.posts.create_post_draft(
            content, desire=desire, session_id=session_id, trace_id=trace_id, now=now
        )

    def _get_meta_int(self, key: str) -> int:
        raw = self.memory.db.get_meta(key, "")
        try:
            return int(float(raw)) if raw else 0
        except (TypeError, ValueError):
            return 0

    @property
    def breaker_open(self) -> bool:
        return self._get_meta_int(_META_BREAKER) >= 1

    def consecutive_failures(self) -> int:
        return self._get_meta_int(_META_FAILURES)

    def resume(self) -> None:
        """Manually clear the circuit breaker (CLI ``qzone resume``)."""
        self.memory.db.set_meta(_META_FAILURES, "0")
        self.memory.db.set_meta(_META_BREAKER, "0")
        self.memory.log_decision(
            "qzone_poll",
            {},
            action="resume",
            outcome="ok",
            model="collector",
        )

    def _resolve_client(self, client: QzoneClient | None) -> QzoneClient:
        resolved = client or self._client
        if resolved is None:
            resolved = QzoneClient.from_config(self.config)
            self._client = resolved
        return resolved

    def _owner_uin(self, client: QzoneClient | None = None) -> str:
        """The owner's uin: explicit config wins, else the cookie identity."""
        owner = str(self.config.get("qzone.owner_uin", "") or "").strip()
        if owner:
            return owner
        return self._resolve_client(client).uin

    # ------------------------------------------------------------------- poll
    def fetch_and_ingest(
        self,
        client: QzoneClient | None = None,
        *,
        manual: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Poll feeds, store rows and promote new ones into memory.

        ``manual=True`` (CLI ``qzone fetch``) bypasses the ``qzone.enabled``
        gate: the gate exists to stop background polling, not to make manual
        diagnostics impossible. The circuit breaker applies either way.
        """
        if not manual and not self.enabled:
            return {"ok": False, "skipped": "disabled"}
        if self.breaker_open:
            self.memory.log_decision(
                "qzone_poll",
                {"failures": self.consecutive_failures()},
                action="circuit_break",
                outcome="skipped",
                model="collector",
            )
            return {"ok": False, "skipped": "circuit_break"}
        client = self._resolve_client(client)
        now = time.time() if now is None else float(now)
        errors: list[str] = []

        feeds_ok = True
        used_fallback = False
        emotions_ok = True
        items: list[dict[str, Any]] = []
        try:
            items = client.fetch_feeds()
        except QzoneError as exc:
            feeds_ok = False
            errors.append(f"feeds: {exc}")
            # 聚合 feed 被 ic2 判未登录时的回退：按已知好友逐个拉说说。
            # 一条都没拿到才算真的失败，避免把"端点挂了"记成"cookie 挂了"。
            fallback_items = self._fetch_friend_fallback(client, errors)
            if fallback_items:
                used_fallback = True
                items = fallback_items
        # Own shuoshuo are best-effort: the friend-feed endpoint is primary,
        # an emotions failure must not kill the whole poll.
        try:
            emotions = client.fetch_emotions()
            for entry in emotions:
                # The msglist payload may omit the author uin; those entries
                # are by construction the cookie account's own posts.
                if not entry.get("author_uin") and client.uin:
                    entry["author_uin"] = client.uin
            items.extend(emotions)
        except QzoneError as exc:
            emotions_ok = False
            errors.append(f"emotions: {exc}")

        new_items = self.store_items(items, now=now)
        promoted = self.ingest_into_memory(new_items, now=now)
        digest_scope = self._write_digest(new_items, now=now)
        if not feeds_ok and not used_fallback and not (self.feeds_optional and emotions_ok):
            # With feeds_optional a feeds-only outage never lands here: the
            # poll keeps going (partial) instead of feeding the breaker. Both
            # endpoints failing is still a real failure even then.
            result = self._register_failure(errors)
            result["fetched"] = len(items)
            result["new"] = len(new_items)
            result["promoted"] = promoted
            return result

        self.memory.db.set_meta(_META_FAILURES, "0")
        self.memory.db.set_meta(_META_BREAKER, "0")
        decision: dict[str, Any] = {
            "fetched": len(items),
            "new": len(new_items),
            "errors": errors,
            "digest_scope": digest_scope,
        }
        if not feeds_ok:
            # Feeds optional and degraded: keep the partial outcome but mark
            # the reason so night reports can tell it from an emotions blip.
            decision["feeds_error"] = True
        if used_fallback:
            decision["friend_fallback"] = True
            decision["friend_uins"] = self.friend_uins
        self.memory.log_decision(
            "qzone_poll",
            decision,
            action="poll",
            outcome="ok" if not errors else "partial",
            model="collector",
        )
        return {
            "ok": True,
            "fetched": len(items),
            "new": len(new_items),
            "promoted": promoted,
            "errors": errors,
        }

    def _fetch_friend_fallback(
        self, client: Any, errors: list[str]
    ) -> list[dict[str, Any]]:
        """逐个已知好友拉说说列表；单人失败不影响其他人。"""
        if not self.friend_fallback:
            return []
        items: list[dict[str, Any]] = []
        for friend_uin in self.friend_uins:
            try:
                entries = client.fetch_emotions(
                    self.friend_fallback_count, uin=friend_uin
                )
            except QzoneError as exc:
                errors.append(f"friend:{friend_uin}: {exc}")
                continue
            for entry in entries:
                # 好友的说说必须带作者 uin，否则会被当成主人自己的动态。
                entry.setdefault("author_uin", friend_uin)
            items.extend(entries)
        return items

    def _register_failure(self, errors: list[str]) -> dict[str, Any]:
        failures = self.consecutive_failures() + 1
        self.memory.db.set_meta(_META_FAILURES, str(failures))
        result: dict[str, Any] = {"ok": False, "failures": failures, "errors": errors}
        if failures >= self.breaker_threshold:
            self.memory.db.set_meta(_META_BREAKER, "1")
            self.memory.log_decision(
                "qzone_poll",
                {"failures": failures, "errors": errors},
                action="circuit_break",
                outcome="paused",
                model="collector",
            )
            result["circuit_break"] = True
        return result

    # ----------------------------------------------------------------- stores
    def author_scope_for(self, author_uin: str, client: QzoneClient | None = None) -> str:
        """'self' when the author is the owner, else 'friend'/'other'."""
        author = str(author_uin or "").strip()
        if not author:
            return "other"
        owner = self._owner_uin(client)
        return "self" if owner and author == owner else "friend"

    def store_items(
        self, items: list[dict[str, Any]], *, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Insert unseen items into ``qzone_feed_items``; return the new ones.

        Idempotent on ``UNIQUE(platform, external_id)``; already-known ids are
        skipped before touching memory.
        """
        now = time.time() if now is None else float(now)
        db = self.memory.db
        new_items: list[dict[str, Any]] = []
        for item in items:
            external_id = str(item.get("external_id") or "").strip()
            if not external_id:
                continue
            existing = db.query_one(
                "SELECT id FROM qzone_feed_items WHERE platform=? AND external_id=?",
                (PLATFORM, external_id),
            )
            if existing:
                continue
            author_uin = str(item.get("author_uin") or "").strip()
            author_name = str(item.get("author_name") or "").strip()
            scope = self.author_scope_for(author_uin)
            db.execute(
                "INSERT OR IGNORE INTO qzone_feed_items("
                "platform, external_id, author_uin, author_name, author_scope, content, "
                "media_json, url, published_at, raw_json, seen_at, ingested, hidden) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    PLATFORM,
                    external_id,
                    author_uin,
                    author_name,
                    scope,
                    str(item.get("content") or ""),
                    json.dumps([str(m) for m in (item.get("media") or [])], ensure_ascii=False),
                    str(item.get("url") or ""),
                    item.get("published_at"),
                    json.dumps(item.get("raw") or {}, ensure_ascii=False, default=str)[:20000],
                    now,
                    0,
                    0,
                ),
            )
            new_items.append({**item, "author_scope": scope})
        return new_items

    def ingest_into_memory(
        self, items: list[dict[str, Any]], *, now: float | None = None
    ) -> int:
        """Promote stored items into memory + RAG with the privacy contract."""
        now = time.time() if now is None else float(now)
        count = 0
        for item in items:
            external_id = str(item.get("external_id") or "").strip()
            if not external_id:
                continue
            scope = "qzone" if item.get("author_scope") == "self" else "qzone_friend"
            author_name = str(item.get("author_name") or "").strip()
            content = str(item.get("content") or "").strip()
            published = item.get("published_at")
            comments = _clean_comments(item.get("comments"))
            try:
                event_time = float(published) if published else now
            except (TypeError, ValueError):
                event_time = now
            summary = content or "（QQ空间图片动态，无文字）"
            # Embedded comments ride on the item's own privacy scope: they
            # only enter the memory/recall channel (wrap_untrusted), never
            # the 「看看空间动态」 bypass, and never raise importance.
            evidence_lines = [f"QQ空间动态 @{author_name or '未知作者'}"]
            evidence_lines.extend(
                f"评论 {entry['author'] or '匿名'}: {entry['text']}" for entry in comments
            )
            # add_item dedupes on source_type+source_id, so a replayed poll
            # (or a crash between the row insert and this step) cannot double.
            memory_item = self.memory.add_item(
                summary,
                type="episodic",
                event_time=event_time,
                observed_at=now,
                importance=0.3 if scope == "qzone" else 0.25,
                confidence=0.6,
                trust_level="external",
                origin_scope=scope,
                source_type=MEMORY_SOURCE_TYPE,
                source_id=external_id,
                evidence="\n".join(evidence_lines),
                tags=["QQ空间", "动态"] + ([author_name] if author_name else []),
                entities=[author_name] if author_name else [],
            )
            # T-0013: the author becomes an entity-layer first-class citizen --
            # entity card keyed by uin (nickname changes merge as aliases) plus
            # one mention anchored to this feed item. Never let this break the
            # privacy-gated ingest: a failure only loses the entity anchor.
            entity_sync: dict[str, Any] = {}
            try:
                entity_sync = sync_qzone_author(
                    self.memory.db,
                    uin=str(item.get("author_uin") or ""),
                    name=author_name,
                    scope=str(item.get("author_scope") or "other"),
                    source_id=external_id,
                    now=now,
                )
                tag_sync = sync_tags(
                    self.memory.db,
                    ["QQ空间", "动态"] + ([author_name] if author_name else []),
                    "memory_item",
                    str(getattr(memory_item, "id", "") or ""),
                    now=now,
                )
                entity_sync["tags_linked"] = tag_sync.get("linked", 0)
            except Exception as exc:  # noqa: BLE001 - entity layer is auxiliary
                entity_sync = {"error": f"{type(exc).__name__}: {exc}"[:120]}
            meta = {
                "author_uin": item.get("author_uin") or "",
                "author_name": author_name,
                "author_scope": item.get("author_scope") or "other",
                "url": item.get("url") or "",
                "published_at": event_time,
                "media_count": len(item.get("media") or []),
                "comments": comments,
                "trust_level": "external",
                "source": scope,
            }
            self.memory.add_rag_chunk(
                MEMORY_SOURCE_TYPE,
                external_id,
                summary,
                metadata=meta,
                trust_level="external",
                origin_scope=scope,
            )
            self.memory.log_decision(
                "qzone_ingest",
                {
                    "external_id": external_id,
                    "origin_scope": scope,
                    "entity": entity_sync.get("entity_id"),
                    "entity_created": bool(entity_sync.get("created")),
                    "mention": bool(entity_sync.get("mention")),
                    "tags_linked": entity_sync.get("tags_linked", 0),
                    **({"entity_error": entity_sync["error"]} if "error" in entity_sync else {}),
                },
                action="ingest",
                outcome="ok",
                model="collector",
            )
            self.memory.db.execute(
                "UPDATE qzone_feed_items SET ingested=1 WHERE platform=? AND external_id=?",
                (PLATFORM, external_id),
            )
            count += 1
        return count

    def _write_digest(self, new_items: list[dict[str, Any]], *, now: float) -> str:
        """One short per-day digest memory when the poll saw new items."""
        if not new_items:
            return ""
        local_date = to_local(now, self.config.timezone_name).date().isoformat()
        if self.memory.db.get_meta(_META_DIGEST_DATE) == local_date:
            return ""
        scopes = {
            "qzone" if item.get("author_scope") == "self" else "qzone_friend"
            for item in new_items
        }
        # Mixed digests travel with the strictest scope: any friend content
        # keeps the whole line out of the prompt by default.
        digest_scope = "qzone_friend" if "qzone_friend" in scopes else "qzone"
        names: list[str] = []
        for item in new_items:
            name = str(item.get("author_name") or "").strip()
            if name and name not in names:
                names.append(name)
        label = "自己的" if digest_scope == "qzone" else "好友的"
        text = f"今天刷到{len(new_items)}条{label}QQ空间动态（{'、'.join(names[:5])}）"
        self.memory.add_item(
            text,
            type="episodic",
            event_time=now,
            observed_at=now,
            importance=0.2,  # ≤0.3 per spec: digests are index lines, not facts
            confidence=0.6,
            trust_level="external",
            origin_scope=digest_scope,
            source_type=MEMORY_SOURCE_TYPE,
            source_id=f"digest:{local_date}",
            evidence="QQ空间轮询摘要",
            tags=["QQ空间", "动态", "摘要"],
        )
        self.memory.db.set_meta(_META_DIGEST_DATE, local_date)
        return digest_scope

    # ---------------------------------------------------------------- queries
    def recent_items(
        self,
        limit: int = 10,
        *,
        include_friend: bool = False,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """Newest feed rows for local display / prompt bypass.

        Local display (CLI/Web) is not bound by ``friends_content_to_llm`` --
        that flag only guards the external LLM surface. ``raw_json`` is never
        returned here.
        """
        limit = max(1, int(limit))
        clauses = [] if include_hidden else ["hidden=0"]
        if not include_friend:
            clauses.append("author_scope='self'")
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self.memory.db.query(
            "SELECT id, external_id, author_uin, author_name, author_scope, content, "
            "media_json, url, published_at, seen_at, ingested, hidden "
            f"FROM qzone_feed_items WHERE {where} "
            "ORDER BY COALESCE(published_at, seen_at) DESC LIMIT ?",
            (limit,),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                media = json.loads(row["media_json"] or "[]")
            except json.JSONDecodeError:
                media = []
            items.append(
                {
                    "id": row["id"],
                    "external_id": row["external_id"],
                    "author_uin": row["author_uin"],
                    "author_name": row["author_name"],
                    "author_scope": row["author_scope"],
                    "content": row["content"],
                    "media": media,
                    "url": row["url"],
                    "published_at": row["published_at"],
                    "seen_at": row["seen_at"],
                    "ingested": bool(row["ingested"]),
                    "hidden": bool(row["hidden"]),
                }
            )
        return items

    def status(self) -> dict[str, Any]:
        def _count(where: str) -> int:
            row = self.memory.db.query_one(
                f"SELECT COUNT(*) AS n FROM qzone_feed_items WHERE {where}"
            )
            return int(row["n"]) if row else 0

        cookie = self._resolve_client(None).cookie_status()
        status = {
            "enabled": self.enabled,
            "read_only": self.read_only,
            "breaker_open": self.breaker_open,
            "consecutive_failures": self.consecutive_failures(),
            "items_total": _count("1=1"),
            "items_self": _count("author_scope='self' AND hidden=0"),
            "items_friend": _count("author_scope<>'self' AND hidden=0"),
            "friends_content_to_llm": self.friends_content_to_llm,
            "owner_uin_configured": bool(
                str(self.config.get("qzone.owner_uin", "") or "").strip()
            ),
            "cookie": cookie,
        }
        try:
            status["posts"] = self.posts.status()
        except Exception:  # the read path report must survive a write-path bug
            status["posts"] = {"error": "unavailable"}
        return status

    # ---------------------------------------------------------------- cleanup
    def forget(self, external_id: str | None = None, *, all_items: bool = False) -> int:
        """Forget stored dynamics: drop feed rows and their memory/RAG traces."""
        db = self.memory.db
        if all_items:
            rows = db.query("SELECT external_id FROM qzone_feed_items")
        elif external_id:
            rows = db.query(
                "SELECT external_id FROM qzone_feed_items WHERE external_id=?",
                (str(external_id),),
            )
        else:
            return 0
        removed = 0
        for row in rows:
            item_id = str(row["external_id"])
            db.execute(
                "DELETE FROM qzone_feed_items WHERE platform=? AND external_id=?",
                (PLATFORM, item_id),
            )
            db.execute(
                "DELETE FROM rag_chunks WHERE source_type=? AND source_id=?",
                (MEMORY_SOURCE_TYPE, item_id),
            )
            db.execute(
                "UPDATE memory_items SET status='deleted', updated_at=? "
                "WHERE source_type=? AND source_id=?",
                (time.time(), MEMORY_SOURCE_TYPE, item_id),
            )
            removed += 1
        if removed:
            self.memory.log_decision(
                "qzone_forget",
                {"count": removed, "all": bool(all_items)},
                action="forget",
                outcome="ok",
                model="collector",
            )
        return removed


# ----------------------------------------------------------------- T-0013 backfill
def backfill_qzone_entities(db: Any, *, now: float | None = None) -> dict[str, Any]:
    """Sync entity cards + mentions for every stored feed item; idempotent.

    Walks ``qzone_feed_items`` (ingested or not -- the author card and the
    item-anchored mention stand on their own) and calls ``sync_qzone_author``
    for each row. Re-running only bumps ``last_seen_at``: a repeat pass
    reports zero created entities and zero new mentions. Only additive --
    nothing is deleted or rewritten.
    """
    now_value = time.time() if now is None else float(now)
    rows = db.query(
        "SELECT external_id, author_uin, author_name, author_scope "
        "FROM qzone_feed_items ORDER BY id"
    )
    entities_created = 0
    mentions = 0
    for row in rows:
        result = sync_qzone_author(
            db,
            uin=str(row["author_uin"] or ""),
            name=str(row["author_name"] or ""),
            scope=str(row["author_scope"] or "other"),
            source_id=str(row["external_id"] or ""),
            now=now_value,
        )
        entities_created += 1 if result.get("created") else 0
        mentions += 1 if result.get("mention") else 0
    return {
        "items": len(rows),
        "entities_created": entities_created,
        "mentions": mentions,
    }
