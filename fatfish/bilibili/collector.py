"""Read-only Bilibili ingestion: turn a video into a finding card + RAG chunks.

This module only reads. Login/cookies are handled by ``BilibiliClient``; this
collector never comments, likes, follows or sends DMs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..memory import MemoryService
from .client import BilibiliClient, BilibiliError


@dataclass(slots=True)
class FindingCard:
    bvid: str
    title: str
    up: str
    url: str
    gist: str
    summary: str
    topics: list[str] = field(default_factory=list)
    hot_score: float = 0.5
    memory_id: str | None = None
    rag_id: str | None = None


class BilibiliCollector:
    def __init__(self, memory: MemoryService, config: Config):
        self.memory = memory
        self.config = config
        # Comment read limit mirrors the client's config key; 0 disables the
        # comment step entirely (no call, no request).
        self.comments_per_video = max(
            0, int(config.get("bilibili.comments_per_video", 10) or 0)
        )

    def ingest_video(
        self,
        *,
        bvid: str,
        title: str,
        up: str = "",
        subtitle: str = "",
        url: str = "",
        published_at: float | None = None,
        topics: list[str] | None = None,
        hot_score: float = 0.5,
        comments: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> FindingCard:
        subtitle = (subtitle or "").strip()
        gist = _gist(subtitle or title)
        summary = subtitle[:1200] if subtitle else title
        topics = [t for t in (topics or []) if t]
        observed = time.time()
        event_time = published_at or observed
        tags = ["B站", "视频"] + topics
        if up:
            tags.append(up)
        item = self.memory.add_item(
            summary,
            type="episodic",
            gist=gist,
            event_time=event_time,
            observed_at=observed,
            importance=max(0.35, min(0.65, float(hot_score))),
            confidence=0.6,
            trust_level="external",
            origin_scope="bilibili",
            source_type="bilibili",
            source_id=bvid,
            evidence=f"{title} @ {up}",
            tags=tags,
            entities=[up] if up else [],
        )
        rag_meta = {
            "bvid": bvid,
            "title": title,
            "up": up,
            "url": url or f"https://www.bilibili.com/video/{bvid}",
            "published_at": event_time,
            "watched_at": observed,
            "hot_score": float(hot_score),
            "comments": comments[:2000],
            **(metadata or {}),
        }
        rag_text = f"{title}\n{summary}\n{comments}".strip()
        rag_id = self.memory.add_rag_chunk(
            "bilibili",
            bvid,
            rag_text,
            metadata=rag_meta,
            trust_level="external",
            origin_scope="bilibili",
        )
        self.memory.log_decision(
            "bilibili_ingest",
            {"bvid": bvid, "title": title, "hot_score": hot_score},
            action="ingest",
            outcome="ok",
            model="collector",
        )
        return FindingCard(
            bvid=bvid,
            title=title,
            up=up,
            url=rag_meta["url"],
            gist=gist,
            summary=summary,
            topics=topics,
            hot_score=hot_score,
            memory_id=item.id,
            rag_id=rag_id,
        )

    # ------------------------------------------------------------------ dedupe
    def is_ingested(self, bvid: str) -> bool:
        """True when any RAG chunk for this Bilibili video already exists."""
        bvid = str(bvid or "").strip()
        if not bvid:
            return False
        db = getattr(self.memory, "db", None)
        if db is None:
            return False
        row = db.query_one(
            "SELECT 1 AS n FROM rag_chunks WHERE source_type='bilibili' AND source_id=? LIMIT 1",
            (bvid,),
        )
        return bool(row)

    def ingested_bvids(self) -> set[str]:
        db = getattr(self.memory, "db", None)
        if db is None:
            return set()
        rows = db.query(
            "SELECT DISTINCT source_id FROM rag_chunks WHERE source_type='bilibili'"
        )
        return {str(row["source_id"]) for row in rows if row["source_id"]}

    # ------------------------------------------------------------------ browse
    def browse_once(
        self,
        limit: int = 5,
        keyword: str | None = None,
        client: BilibiliClient | None = None,
    ) -> list[FindingCard]:
        """Fetch the popular feed (or search) and ingest new videos.

        Already-ingested ``bvid`` values are skipped before touching memory, so
        calling this repeatedly is cheap and idempotent. Returns only the cards
        that were newly ingested.
        """
        limit = max(1, int(limit))
        client = client or BilibiliClient.from_config(self.config)
        videos = client.browse(limit=limit, keyword=keyword)
        existing = self.ingested_bvids()
        cards: list[FindingCard] = []
        skipped = 0
        for video in videos:
            bvid = str(video.get("bvid") or "").strip()
            if not bvid:
                continue
            if bvid in existing:
                skipped += 1
                continue
            title = str(video.get("title") or bvid)
            # Comments are additive context: a failed/odd reply fetch must
            # never block the video itself from being ingested. The client
            # enforces the config limit (0 -> no request); the gate here just
            # skips the call when disabled.
            comments = ""
            if self.comments_per_video > 0:
                try:
                    comments = _join_comments(
                        client.video_comments(bvid, limit=self.comments_per_video)
                    )
                except BilibiliError:
                    comments = ""
            card = self.ingest_video(
                bvid=bvid,
                title=title,
                up=str(video.get("up") or ""),
                subtitle=str(video.get("desc") or ""),
                url=str(video.get("url") or ""),
                published_at=_as_float(video.get("published_at")),
                topics=[str(t) for t in (video.get("topics") or [])],
                hot_score=_as_float(video.get("hot_score"), 0.5) or 0.5,
                comments=comments,
                metadata={"collected_by": "browse_once"},
            )
            existing.add(bvid)
            cards.append(card)
        if cards or skipped:
            self.memory.log_decision(
                "bilibili_browse",
                {"limit": limit, "keyword": keyword, "fetched": len(videos)},
                action="browse",
                outcome="ok",
                model="collector",
            )
        return cards


def _gist(text: str, limit: int = 100) -> str:
    clean = " ".join((text or "").replace("\n", " ").split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def _join_comments(raw: Any) -> str:
    """Join client comment lines into one block, tolerating odd payloads."""
    if not isinstance(raw, (list, tuple)):
        return ""
    lines = [str(line).strip() for line in raw if str(line or "").strip()]
    return "\n".join(lines)


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
