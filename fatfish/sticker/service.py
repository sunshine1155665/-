"""Sticker service: intent -> candidates -> rerank -> send / ComfyUI fallback.

The persona never sees the taxonomy. It calls send_sticker(need=...).
"""

from __future__ import annotations

import heapq
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..config import Config
from ..db import Database
from ..memory.embedder import (
    HTTP_BACKENDS,
    Embedder,
    build_embedder,
    cosine,
    dumps,
    loads,
    pack_embedding,
)
from .emotion import emotion_intensity
from .generator import BaseStickerGenerator
from .lexicon import expand_query
from .intent import (
    INTENT_KEYWORDS,
    INTENT_PRIORITY,
    MAX_INTENTS,
    IntentGuess,
    RuleClassifier,
    build_classifier,
    score_intents,
)

# How much a mismatch between the request's strength and the sticker's strength
# costs. Comes out of a 0-1 score, and 4 levels of mismatch (1 vs 5) should be
# enough to lose to a slightly worse-scored but better-pitched candidate.
INTENSITY_PENALTY = 0.12


def sticker_index_text(
    caption: str,
    tags: list[str] | None,
    intents: list[str] | None,
    content: dict[str, Any] | None,
) -> str:
    """The text a sticker's vector is built from.

    Scene text wins when it exists. Measured on the 22-case golden set, keeping
    ``tags`` in the index is what sank retrieval: about half of them describe the
    picture ("女仆", "蓝发", "动漫", "表情包"), which says nothing about *when*
    to send it, and they drowned out the one useful phrase. With scene text
    alone, bge-base went from 1/22 to 9/22 top-1 and average rank 42 -> 9.

    A sticker that has no scene text yet keeps the old caption+tags+intents
    text, so a freshly imported image still matches on something.
    """
    usage = str((content or {}).get("usage_description") or "").strip()
    if usage:
        return usage
    parts = [caption or "", " ".join(tags or []), " ".join(intents or [])]
    return " ".join(part for part in (p.strip() for p in parts) if part)


class ComfyUIAdapter(Protocol):
    def generate(self, need: str, context: dict[str, Any] | None = None) -> str | None: ...


class DisabledComfyUI:
    def generate(self, need: str, context: dict[str, Any] | None = None) -> str | None:
        return None


@dataclass(slots=True)
class Sticker:
    id: str
    file_path: str
    caption: str
    tags: list[str]
    emotion: dict[str, Any]
    identity: dict[str, Any]
    content: dict[str, Any]
    intents: list[str]
    intensity: float
    style: str
    source: str
    usage_count: int
    embedding: list[float] | None


@dataclass(slots=True)
class StickerCandidate:
    sticker: Sticker
    score: float
    similarity: float
    keyword: float


@dataclass(slots=True)
class StickerDecision:
    send: bool
    reason: str
    score: float = 0.0
    sticker: Sticker | None = None
    candidates: list[StickerCandidate] = field(default_factory=list)
    generated: bool = False
    # 0-5, how charged the message looked. Reported even when the answer is
    # "no", because "it matched but the moment was flat" is a different failure
    # from "nothing matched" and they need different fixes.
    emotion: float = 0.0


class StickerService:
    def __init__(
        self,
        db: Database,
        config: Config,
        embedder: Embedder | None = None,
        comfyui: ComfyUIAdapter | None = None,
        llm: Any | None = None,
        classifier: Any | None = None,
    ):
        self.db = db
        self.config = config
        # Stickers may use a stronger embedder than the rest of the system.
        # Memory RAG keeps its own backend: switching it would invalidate every
        # stored vector in memory_items/rag_chunks, and nobody measured that.
        backend = str(
            config.get("sticker.embedding_backend")
            or config.get("memory.embedding_backend", "hash")
        )
        self.embedder = embedder or build_embedder(
            backend,
            # HTTP 后端的宽度由服务决定，不该拿 memory.embedding_dim 去预填：
            # 预填 256 会让「宽度是否一致」的启动检查当场误报（本库是 768）。
            0 if backend.strip().lower() in HTTP_BACKENDS
            else int(config.get("memory.embedding_dim", 256)),
            url=config.get("sticker.embedding_url") or config.get("memory.embedding_url"),
            timeout=float(config.get("sticker.embedding_timeout_seconds", 10.0)),
        )
        self.comfyui = comfyui or DisabledComfyUI()
        # Rules stay the last line of defence: a sticker is never worth stalling
        # a turn, so an unavailable classifier must not block the reply.
        self.classifier = classifier or build_classifier(config, llm)
        self.rule_classifier = RuleClassifier()
        self.threshold = float(config.get("sticker.intent_threshold", 0.5))
        self.explicit_threshold = float(
            config.get("sticker.explicit_threshold", 0.20)
        )
        self.max_candidates = int(config.get("sticker.max_candidates", 20))
        # A second, orthogonal gate: similarity says "this picture fits the
        # occasion", this says "the moment is charged enough to bother". 0
        # disables it and restores the old behaviour exactly.
        # Measured on the 40-sentence baseline (staging_spontaneous.py): at 2.0
        # the task group tops out at 1.60 and 19/20 emotional sentences pass,
        # so the two gates can be tuned independently -- lowering the
        # similarity threshold for recall no longer lets task-ish chit-chat
        # through, because those sentences have no feeling in them.
        self.min_emotion = max(0.0, float(config.get("sticker.min_emotion_intensity", 0.0)))
        # The 0.45 similarity weight above was calibrated against hashing
        # vectors, whose cosines spread over 0-0.4. bge-base cosines on this
        # library are compressed into 0.34-0.60, so the same weight contributes
        # almost nothing and the semantic signal goes quiet. Stretching with
        # fixed anchors (rather than per-request min-max) restores it without
        # destroying the absolute level, which the send threshold still needs:
        # per-request normalisation pins the best candidate at 1.0 and made the
        # service send a sticker on 9 of 10 ordinary sentences.
        # Defaults are a no-op, so the hash backend keeps its old behaviour.
        self.sim_floor = float(config.get("sticker.similarity_floor", 0.0))
        self.sim_ceil = float(config.get("sticker.similarity_ceil", 1.0))
        # Keep the default path bit-identical to the un-calibrated one.
        self._calibrating = (self.sim_floor, self.sim_ceil) != (0.0, 1.0)
        self.width_warning: str | None = self._check_stored_width()
        # "The library has nothing close, so draw one" is only acceptable when
        # somebody asked for a picture. Allowing it on the spontaneous path
        # turns ``intent_threshold`` into decoration, because a fresh meme is
        # scored 1.0 by construction.
        self.spontaneous_generation = bool(
            config.get("sticker.base_generation_spontaneous", False)
        )
        self.base_generator = (
            BaseStickerGenerator(db, config, self)
            if bool(config.get("sticker.base_generation_enabled", True))
            else None
        )

    def _check_stored_width(self) -> str | None:
        """Warn when the table's vectors cannot match the current embedder.

        Switching backends without re-embedding is the one failure mode that
        looks like nothing is wrong: every similarity is 0, so ranking silently
        falls back to keyword overlap and the sticker service gets worse
        without saying anything. Probing one row is cheap; probing them all
        would mean parsing every vector on startup.
        """
        try:
            row = self.db.query_one(
                "SELECT embedding_text FROM stickers WHERE active=1 AND embedding_text IS NOT NULL LIMIT 1"
            )
        except Exception:  # noqa: BLE001 - a missing table is not our problem here
            return None
        stored = len(loads(row["embedding_text"]) or []) if row else 0
        expected = int(getattr(self.embedder, "dim", 0) or 0)
        # HttpEmbedder only learns its width from the first response, so 0 means
        # "not asked yet" rather than a mismatch.
        if not stored or not expected or stored == expected:
            return None
        return (
            f"贴纸向量宽度 {stored} 与当前后端 {expected} 不一致，相似度会全部为 0。"
            f"请按当前后端重建向量索引（重算库内全部贴纸向量）。"
        )

    def add_sticker(
        self,
        file_path: str,
        *,
        caption: str,
        tags: list[str] | None = None,
        emotion: dict[str, Any] | None = None,
        identity: dict[str, Any] | None = None,
        content: dict[str, Any] | None = None,
        intents: list[str] | None = None,
        intensity: float = 2.0,
        style: str = "",
        source: str = "library",
        sticker_id: str | None = None,
    ) -> Sticker:
        sid = sticker_id or uuid.uuid4().hex
        tags = tags or []
        intents = intents or []
        text = sticker_index_text(caption, tags, intents, content)
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO stickers(id, file_path, caption, tags_json, emotion_json, identity_json, "
            "content_json, intent_json, intensity, style, source, usage_count, embedding_text, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT usage_count FROM stickers WHERE id=?),0),?,1,?,?)",
            (
                sid, str(file_path), caption,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(emotion or {}, ensure_ascii=False),
                json.dumps(identity or {}, ensure_ascii=False),
                json.dumps(content or {}, ensure_ascii=False),
                json.dumps(intents, ensure_ascii=False),
                float(intensity), style, source, sid,
                pack_embedding(self.embedder.embed(text)), now, now,
            ),
        )
        return self.get(sid)  # type: ignore[return-value]

    def get(self, sticker_id: str) -> Sticker | None:
        row = self.db.query_one("SELECT * FROM stickers WHERE id=?", (sticker_id,))
        return self._row_to_sticker(row) if row else None

    def list(self, limit: int = 50) -> list[Sticker]:
        rows = self.db.query(
            "SELECT * FROM stickers WHERE active=1 ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        return [self._row_to_sticker(row) for row in rows]

    def classify_intents(self, need: str) -> list[tuple[str, float]]:
        """Keyword scoring used for candidate ranking.

        Retrieval keeps the cheap deterministic signal; the contextual
        classifier only decides *which* intent was asked for.
        """
        return score_intents(need)

    def requested_intents(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> list[str]:
        """Intents the *user* asked for, in descending fit.

        Delegates to the configured classifier, which reads the recent dialogue
        and normalises its answer against the taxonomy. ``need`` alone is a
        poor input: the chat model rewrites the request and likes to add colour
        ("…带一点撒娇"), which used to lose the one word that mattered.

        The keyword matcher is still consulted when the classifier is off,
        unavailable, or returns nothing it can validate — this method never
        returns an empty list because of an infrastructure failure alone.
        """
        return self._classify(need, user_text, context_text).labels

    def _classify(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> IntentGuess:
        """Classifier answer with a hard whitelist and a rules fallback."""
        classifier = self.classifier
        guess = IntentGuess()
        if not isinstance(classifier, RuleClassifier):
            try:
                guess = classifier.classify(need, user_text, context_text)
            except Exception:
                guess = IntentGuess()
        if not guess:
            return self.rule_classifier.classify(need, user_text, context_text)
        # Whatever the backend says, only taxonomy labels leave this method.
        # "Naturally valid output" is enforced here, not asked for.
        ordered: list[tuple[str, float]] = []
        for name, score in guess:
            label = str(name)
            if label in INTENT_KEYWORDS and all(label != kept for kept, _ in ordered):
                ordered.append((label, float(score)))
        if not ordered:
            return self.rule_classifier.classify(need, user_text, context_text)
        # Backends that only answer labels (older stubs, rules) simply leave the
        # second dimension unset.
        return IntentGuess(
            ordered[:MAX_INTENTS], getattr(guess, "intensity", None)
        )

    @staticmethod
    def _best_for(
        requested: list[str],
        candidates: list["StickerCandidate"],
    ) -> "StickerCandidate | None":
        """Best candidate that actually matches what was asked for.

        Walking the requested intents in order keeps a weaker *scene* match
        ahead of a stronger *mood* match: a sleepy sticker beats a pouting one
        for "晚安，带一点撒娇" even when the pout scores higher.
        """
        if not candidates:
            return None
        if not requested:
            return candidates[0]
        for intent in requested:
            for cand in candidates:
                if intent in cand.sticker.intents:
                    return cand
        return None

    def search(self, need: str, limit: int = 10) -> list[StickerCandidate]:
        return self._rank(
            need,
            "SELECT * FROM stickers WHERE active=1 ORDER BY created_at DESC LIMIT 5000",
            (),
            limit,
        )

    def search_by_intents(
        self, intents: list[str], need: str, limit: int = 8
    ) -> list[StickerCandidate]:
        """Shortlist restricted to the intents that were actually asked for.

        ``search()`` ranks by blended similarity, so a rare intent can be pushed
        out of the top-N entirely: there is exactly one ``goodnight`` sticker in
        the library, and it never survives a ranking dominated by the 62
        ``meme`` ones. Without this, a correct classification still ends up
        synthesising a sticker from a base image.
        """
        wanted = [name for name in intents if name in INTENT_KEYWORDS][:MAX_INTENTS]
        if not wanted:
            return []
        clause = " OR ".join("intent_json LIKE ?" for _ in wanted)
        # Rank these by the classifier's own belief, not by keyword hits on the
        # request text: that is the whole point of asking a classifier.
        belief = [(name, 1.0 - 0.15 * index) for index, name in enumerate(wanted)]
        return self._rank(
            need,
            f"SELECT * FROM stickers WHERE active=1 AND ({clause}) "
            "ORDER BY created_at DESC LIMIT 5000",
            tuple(f'%"{name}"%' for name in wanted),
            limit,
            intent_scores=belief,
        )

    def _rank(
        self,
        need: str,
        sql: str,
        params: tuple[Any, ...],
        limit: int,
        intent_scores: list[tuple[str, float]] | None = None,
    ) -> list[StickerCandidate]:
        # 查询侧同义词扩展，但**不稀释**：相似度取 max(原句, 扩展) 两个向量。
        # 只用扩展向量会让本来就匹配的查询被别名 token 稀释（实测晚安查询
        # 0.545 -> 0.453），丢掉恰好卡在阈值上的匹配；只用原句则「惊到」
        # 桥不到「惊讶」。关键词通道回到原句，理由同前：原话出现和意思
        # 相近是两个信号，混在一起会让规则分类器吃到自己没见过的词。
        expanded = expand_query(need)
        qvecs = [self.embedder.embed(need)]
        if expanded != need:
            qvecs.append(self.embedder.embed(expanded))
        qlower = need.lower()
        if intent_scores is None:
            intent_scores = self.classify_intents(need)
        q_tokens = _tokens_for_overlap(need)
        span = self.sim_ceil - self.sim_floor

        def scored() -> Iterator[StickerCandidate]:
            # Stream the table instead of materialising every sticker and two
            # parallel similarity lists.  At the 5000-row cap each 768-float
            # embedding is ~24 KB of Python floats, so the old shape could
            # allocate well over 100 MB for one decision; ``nlargest`` keeps
            # only the rows that can actually be returned.
            for row in self.db.execute(sql, params):
                sticker = self._row_to_sticker(row)
                # A vector written by another backend has a different width (256
                # for hash, 768 for bge-base). ``cosine`` truncates to the
                # shorter one without complaining, which would turn a backend
                # switch into silent noise. Score it 0 instead and let
                # keyword/intent carry the row.
                embedding = sticker.embedding
                width_ok = bool(embedding) and bool(qvecs[0]) and len(embedding) == len(qvecs[0])
                raw = max(cosine(qv, embedding) for qv in qvecs) if width_ok else 0.0
                # ``similarity`` on the candidate stays the raw cosine because
                # the web debug view shows it to a human; the stretched value
                # only feeds the score, where the spread matters.
                sim = raw
                if self._calibrating and span > 1e-6:
                    sim = _clip01((raw - self.sim_floor) / span)
                hay = (
                    f"{sticker.caption} {' '.join(sticker.tags)} "
                    f"{' '.join(sticker.intents)}"
                ).lower()
                # 场景描述（"想要被摸头、求安慰"）比画面描述（"闭眼合掌，鲸鱼尾巴"）
                # 更贴近「用户此刻想要什么」，查询词能直接落在它上面。
                usage = str(sticker.content.get("usage_description") or "").lower()
                if usage:
                    hay = f"{hay} {usage}"
                keyword = (
                    1.0
                    if qlower and qlower in hay
                    else _overlap(q_tokens, _tokens_for_overlap(hay))
                )
                intent_bonus = 0.0
                for intent, prob in intent_scores:
                    if intent in sticker.intents:
                        intent_bonus = max(intent_bonus, prob)
                score = 0.45 * sim + 0.25 * keyword + 0.30 * intent_bonus
                if score <= 0.03:
                    continue
                yield StickerCandidate(
                    sticker=sticker,
                    score=round(score, 6),
                    similarity=round(raw, 6),
                    keyword=round(keyword, 6),
                )

        return heapq.nlargest(max(1, int(limit)), scored(), key=lambda c: c.score)

    @staticmethod
    def _pitch_to_intensity(
        candidates: list["StickerCandidate"],
        wanted: float,
        *,
        penalty: float = INTENSITY_PENALTY,
    ) -> list["StickerCandidate"]:
        """Prefer a sticker whose strength matches the request's.

        The library already stores an intensity per image (2-5) and it was
        never used for matching, so "气死我了" and "有点烦" landed on the same
        picture. The adjustment only reorders; the raw score is left untouched
        because the send thresholds are calibrated against it.
        """
        return sorted(
            candidates,
            key=lambda cand: cand.score
            - penalty * abs(float(cand.sticker.intensity) - float(wanted)),
            reverse=True,
        )

    def decide(
        self,
        need: str,
        *,
        context: dict[str, Any] | None = None,
        threshold: float | None = None,
        session_id: str | None = None,
        force: bool = False,
        user_text: str | None = None,
        context_text: str | None = None,
        intensity: float | None = None,
    ) -> StickerDecision:
        threshold = self.threshold if threshold is None else float(threshold)
        # What the *user* said, when we have it: the chat model rewrites the
        # request into ``need`` and likes to add colour ("带一点撒娇"), so the
        # paraphrase is the wrong thing to read a thermometer off.
        emotion = emotion_intensity(user_text or need)
        candidates = self.search(need, limit=self.max_candidates)
        guess = (
            self._classify(need, user_text, context_text) if force else IntentGuess()
        )
        requested = guess.labels
        if requested:
            # Widen recall for the intents that were actually asked for; the
            # blended ranking alone can bury a rare label out of sight. Keep the
            # better of the two scores per sticker — the shortlist ranks by the
            # classifier's belief, which is usually the more accurate number.
            merged = {cand.sticker.id: cand for cand in candidates}
            for cand in self.search_by_intents(requested, need, limit=8):
                current = merged.get(cand.sticker.id)
                if current is None or cand.score > current.score:
                    merged[cand.sticker.id] = cand
            candidates = sorted(merged.values(), key=lambda c: c.score, reverse=True)
        if intensity is not None:
            # The model stated the strength it wants ("别太夸张"), so it wins
            # over the classifier's guess. This used to be dropped on the floor
            # even though TOOL_SCHEMA promised the parameter.
            candidates = self._pitch_to_intensity(candidates, float(intensity))
        elif requested and guess.intensity is not None:
            candidates = self._pitch_to_intensity(candidates, guess.intensity)
        if not force and self.min_emotion and emotion < self.min_emotion:
            # The second gate. Similarity answers "is this picture about the
            # right thing"; this answers "is this worth interrupting for". A
            # flat "嗯" or "我下午要去图书馆" can still scrape past a lowered
            # threshold, and for a while the only defence was a threshold high
            # enough to also silence the emotional sentences we wanted.
            #
            # Deliberately *after* search: the score is the whole diagnostic
            # value here. ``emotion_too_flat`` with score 0.71 means the gate
            # is doing real work; with score 0.22 the sentence was going to be
            # skipped anyway and lowering ``min_emotion_intensity`` buys
            # nothing. One embedding round trip on a local CPU port is cheap
            # next to tuning this blind.
            return StickerDecision(
                send=False,
                reason="emotion_too_flat",
                score=candidates[0].score if candidates else 0.0,
                candidates=candidates,
                emotion=emotion,
            )
        if force:
            # The chat model supplied the need; trust its judgement about
            # whether an "angry" image is actually flirtatious. An explicit
            # request is judged on ``explicit_threshold`` instead of the normal
            # threshold: with the hash embedder even a good library match only
            # scores ~0.3, and rejecting it used to send a synthesised sticker
            # with an unrelated caption instead of the right one that was
            # already in the library.
            best = self._best_for(requested, candidates)
            if best is not None and best.score >= self.explicit_threshold:
                return StickerDecision(
                    send=True,
                    reason="explicit_request",
                    score=best.score,
                    sticker=best.sticker,
                    candidates=candidates,
                    emotion=emotion,
                )
            if self.base_generator is not None:
                generated = self.base_generator.generate(
                    need, context, intents=requested
                )
                if generated is not None:
                    return StickerDecision(
                        send=True,
                        reason="generated_from_base",
                        score=1.0,
                        sticker=generated,
                        candidates=candidates,
                        generated=True,
                        emotion=emotion,
                    )
            if best is not None:
                return StickerDecision(
                    send=True,
                    reason="explicit_request_low_score",
                    score=best.score,
                    sticker=best.sticker,
                    candidates=candidates,
                    emotion=emotion,
                )
            # Nothing in the library matches and synthesis failed. Sending a
            # generic sticker here is what put a pouting "才不是" behind a
            # goodnight request: staying silent is the lesser evil.
            return StickerDecision(
                send=False,
                reason="no_matching_sticker",
                score=candidates[0].score if candidates else 0.0,
                candidates=candidates,
                emotion=emotion,
            )
        elif candidates and candidates[0].score >= threshold:
            return StickerDecision(
                send=True,
                reason="library_match",
                score=candidates[0].score,
                sticker=candidates[0].sticker,
                candidates=candidates,
                emotion=emotion,
            )
        if self.base_generator is not None and self.spontaneous_generation:
            # Synthesis is a fallback for *asked-for* stickers, not a way for
            # her to always have something to send. On the spontaneous path it
            # made the threshold meaningless: ``generate()`` hands back a
            # sticker scored 1.0, so every sentence that fell below
            # ``intent_threshold`` came back as a send. Measured on a copy of
            # the production library with the rate cap lifted
            # (staging_spontaneous.py --allow-generate): 20/20 on "she should
            # send" *and* 20/20 on "she should not" -- and 8 of those 20 task
            # requests got the same fallback caption. With it off: 8/20 and
            # 0/20.
            generated = self.base_generator.generate(need, context)
            if generated is not None:
                return StickerDecision(
                    send=True,
                    reason="generated_from_base",
                    score=1.0,
                    sticker=generated,
                    candidates=candidates,
                    generated=True,
                    emotion=emotion,
                )
        if bool(self.config.get("sticker.comfyui_enabled", False)):
            path = self.comfyui.generate(need, context)
            if path:
                sticker = self.add_sticker(
                    path,
                    caption=need[:80],
                    tags=[],
                    intents=[intent for intent, _ in self.classify_intents(need)[:2]],
                    source="comfyui",
                )
                return StickerDecision(
                    send=True,
                    reason="comfyui_generated",
                    score=1.0,
                    sticker=sticker,
                    candidates=candidates,
                    generated=True,
                    emotion=emotion,
                )
        return StickerDecision(
            send=False,
            reason="no_suitable_sticker" if not candidates else "below_threshold",
            score=candidates[0].score if candidates else 0.0,
            candidates=candidates,
            emotion=emotion,
        )

    def record_usage(
        self,
        sticker_id: str,
        *,
        need: str,
        session_id: str | None = None,
        reaction: str | None = None,
    ) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO sticker_usage(sticker_id, session_id, need, sent_at, reaction) VALUES (?,?,?,?,?)",
            (sticker_id, session_id, need[:500], now, reaction),
        )
        self.db.execute(
            "UPDATE stickers SET usage_count=usage_count+1, last_used_at=? WHERE id=?",
            (now, sticker_id),
        )

    @staticmethod
    def _row_to_sticker(row: Any) -> Sticker:
        return Sticker(
            id=row["id"],
            file_path=row["file_path"],
            caption=row["caption"],
            tags=_json_list(row["tags_json"]),
            emotion=_json_dict(row["emotion_json"]),
            identity=_json_dict(row["identity_json"]),
            content=_json_dict(row["content_json"]),
            intents=_json_list(row["intent_json"]),
            intensity=float(row["intensity"]),
            style=row["style"],
            source=row["source"],
            usage_count=int(row["usage_count"]),
            embedding=loads(row["embedding_text"]),
        )


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _tokens_for_overlap(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    for match in re.finditer(r"[a-zA-Z0-9_]+", text):
        tokens.add(match.group(0))
    chars = re.findall(r"[一-鿿]", text)
    tokens.update(chars)
    tokens.update("".join(pair) for pair in zip(chars, chars[1:]))
    return tokens


def _overlap(query_tokens: set[str], hay_tokens: set[str]) -> float:
    if not query_tokens or not hay_tokens:
        return 0.0
    common = len(query_tokens & hay_tokens)
    return common / max(1, min(len(query_tokens), len(hay_tokens)))


def _clip01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value
