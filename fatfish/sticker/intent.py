"""Intent classification for sticker requests.

The taxonomy defined here is the only vocabulary the rest of the system knows
about: every label must be a key of ``INTENT_KEYWORDS``. Classifiers return
``(label, probability)`` pairs in descending fit, and anything outside the
taxonomy is dropped before it can reach the library — that is what makes the
output "naturally valid" rather than merely asking a model to behave.

Three backends share the same interface:

``rules``
    The original keyword matcher. Cheap, offline, and the fallback when the
    others fail or are disabled.
``llm``
    One small extra request that reads the recent dialogue instead of just the
    request text. This is what resolves an ellipsis like "抱被子的" to
    ``goodnight``; keyword matching can only see that no listed word appeared
    and falls through to ``meme``.
``kev``
    The local Kev-0.8B decision model (``choice`` question over the same
    taxonomy, calibrated probabilities, no network). Same whitelist, ~100ms.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "happy": ("开心", "高兴", "好耶", "哈哈", "笑", "愉快", "棒"),
    "comfort": ("安慰", "抱抱", "难过", "别哭", "心疼", "陪你"),
    "tease": ("吐槽", "调侃", "阴阳", "无奈", "好笑", "嫌弃", "欠揍", "得意"),
    "agree": ("同意", "赞同", "对对", "确实", "有道理"),
    "disagree": ("不同意", "反对", "不对", "才不是", "拒绝"),
    "surprise": ("震惊", "惊讶", "没想到", "居然", "哇", "惊到", "吓到", "惊呆"),
    "confused": ("困惑", "不懂", "问号", "啊这", "迷惑", "无语", "离谱", "服了", "什么鬼"),
    "shy": ("害羞", "脸红", "不好意思", "撒娇"),
    "angry": ("生气", "愤怒", "气死", "哼", "炸毛"),
    "sad": ("难过", "伤心", "委屈", "想哭", "低落"),
    "celebrate": ("庆祝", "恭喜", "胜利", "干杯", "撒花"),
    "greeting": ("你好", "早安", "嗨", "在吗", "打招呼"),
    "goodnight": ("晚安", "睡", "好梦", "哄睡", "被子", "抱枕", "眯一会儿", "闭眼"),
    "thanks": ("谢谢", "感谢", "多谢"),
    "apology": ("对不起", "抱歉", "我错了", "道歉"),
    "flirt": ("喜欢", "想你", "亲亲", "抱抱", "心动", "爱你"),
    "meme": ("梗", "抽象", "离谱", "整活", "乐子"),
}

# When several intents score the same, the *scene* the user asked for wins over
# the mood the model happened to mention. "晚安…带点撒娇" is a goodnight
# request: ranking ``shy`` first is what produced a pouting "才不是" sticker for
# a bedtime goodnight.
INTENT_PRIORITY: tuple[str, ...] = (
    "goodnight",
    "apology",
    "thanks",
    "greeting",
    "celebrate",
    "comfort",
    "happy",
    "agree",
    "surprise",
    "confused",
    "sad",
    "angry",
    "shy",
    "tease",
    "flirt",
    "disagree",
    "meme",
)

# One short line per label, used as classifier instructions. Kept next to the
# keywords so a new label cannot be added to only one of the two.
INTENT_LABELS: dict[str, str] = {
    "happy": "开心、笑、遇到好事",
    "comfort": "需要安慰、想要抱抱、被陪着",
    "tease": "调侃、吐槽、嫌弃、欠揍",
    "agree": "赞同、认可、有道理",
    "disagree": "不认同、拒绝、反对",
    "surprise": "震惊、没想到、被吓到",
    "confused": "无语、离谱、看不懂、问号",
    "shy": "害羞、被夸得不好意思",
    "angry": "生气、炸毛、赌气",
    "sad": "难过、委屈、低落",
    "celebrate": "庆祝、恭喜、事情成了",
    "greeting": "打招呼、在吗、早上好",
    "goodnight": "要睡了、晚安、困了、躺下盖被子",
    "thanks": "道谢、被帮了忙",
    "apology": "道歉、认错",
    "flirt": "想你、喜欢、贴贴",
    "meme": "纯玩梗、整活、抽象",
}

MAX_INTENTS = 3
RANK_PROBABILITIES = (0.90, 0.70, 0.55)
UNKNOWN = "meme"


@dataclass(slots=True)
class IntentGuess:
    """One classifier answer: which label, how strong.

    Iterable over ``(label, probability)`` pairs so callers that only care about
    the label keep working unchanged; ``intensity`` is the extra dimension the
    same request returns for free.
    """

    pairs: list[tuple[str, float]] = field(default_factory=list)
    intensity: float | None = None

    def __iter__(self):
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def __bool__(self) -> bool:
        return bool(self.pairs)

    @property
    def labels(self) -> list[str]:
        return [name for name, _ in self.pairs]


class IntentClassifier(Protocol):
    name: str

    def classify(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> IntentGuess: ...


def score_intents(text: str) -> list[tuple[str, float]]:
    """Keyword scoring, unchanged: 0.35 base, +0.25 per extra hit, capped at 1."""
    lowered = (text or "").lower()
    scores: list[tuple[str, float]] = []
    for intent, words in INTENT_KEYWORDS.items():
        hits = sum(1 for word in words if word in lowered)
        if hits:
            scores.append((intent, min(1.0, 0.35 + 0.25 * hits)))
    if not scores:
        scores.append((UNKNOWN, 0.30))
    return sorted(
        scores,
        key=lambda kv: (
            -kv[1],
            INTENT_PRIORITY.index(kv[0]) if kv[0] in INTENT_PRIORITY else len(INTENT_PRIORITY),
        ),
    )


def _mentions_meme(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in INTENT_KEYWORDS[UNKNOWN])


class RuleClassifier:
    """Keyword-only baseline. Always available, never raises."""

    name = "rules"

    def classify(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> IntentGuess:
        for raw in (user_text, need):
            text = str(raw or "")
            scored = [(name, score) for name, score in score_intents(text) if score >= 0.3]
            # ``score_intents`` falls back to ``meme`` when nothing matched.
            # That is a placeholder, not something the user asked for.
            if len(scored) == 1 and scored[0][0] == UNKNOWN and not _mentions_meme(text):
                scored = []
            if scored:
                # Keywords carry no notion of intensity; leaving it unset lets
                # the caller rank on the blended score alone.
                return IntentGuess(scored[:MAX_INTENTS])
        return IntentGuess()


CLASSIFIER_INSTRUCTIONS = (
    "你是聊天表情包的意图分类器。读最近对话和当前这句话，判断此刻如果发一张"
    "表情包，最贴近哪一类。只看用户想要什么，不要用你自己的情绪去替换。\n"
    "只能从下面的标签里选，不要发明新标签：\n"
    + "\n".join(f"- {name}: {label}" for name, label in INTENT_LABELS.items())
    + "\n另外判断语气有多强：1=很淡，3=平常，5=很激烈。\n"
    '只输出 JSON：{"intents": ["标签1", "标签2"], "intensity": 1-5}，'
    "intents 按贴合度降序、1-2 个。"
)


def _parse_guess(raw: str) -> IntentGuess:
    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    obj: Any = None
    for candidate in (text, _first_object(text)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if isinstance(obj, list):
        return _ranked(_clean_labels(obj))
    if not isinstance(obj, dict):
        return IntentGuess()
    raw_labels = obj.get("intents") or obj.get("labels") or []
    if isinstance(raw_labels, str):
        raw_labels = [raw_labels]
    intensity = obj.get("intensity")
    try:
        value = float(intensity)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = None
    if value is not None and not 1.0 <= value <= 5.0:
        value = None
    return _ranked(_clean_labels(raw_labels), value)


def _clean_labels(raw_labels: Any) -> list[str]:
    if not isinstance(raw_labels, list):
        return []
    labels: list[str] = []
    for item in raw_labels:
        name = str(item).strip().lower()
        if name in INTENT_KEYWORDS and name not in labels:
            labels.append(name)
    return labels[:MAX_INTENTS]


def _parse_labels(raw: str) -> list[str]:
    """Labels only. Kept for callers that do not need the intensity."""
    return _parse_guess(raw).labels


def _first_object(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else ""


def _ranked(labels: list[str], intensity: float | None = None) -> IntentGuess:
    return IntentGuess(
        [
            (
                label,
                RANK_PROBABILITIES[index] if index < len(RANK_PROBABILITIES) else 0.4,
            )
            for index, label in enumerate(labels)
        ],
        intensity,
    )


class LlmClassifier:
    """One short chat request over the recent dialogue.

    Falls back to nothing (the caller then uses rules) when the model is
    unavailable, answers with unusable text, or keeps failing: a sticker is
    never worth stalling a turn.
    """

    name = "llm"

    def __init__(
        self,
        llm: Any,
        *,
        max_tokens: int = 400,
        cooldown_seconds: float = 120.0,
        cache_ttl_seconds: float = 180.0,
    ):
        self.llm = llm
        # Thinking models spend output tokens before they emit the answer; 120
        # was enough to produce ``finish_reason=length`` with empty content for
        # roughly one phrasing in six, which then fell back to keyword rules.
        self.max_tokens = int(max_tokens)
        self.cooldown_seconds = float(cooldown_seconds)
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._disabled_until = 0.0
        self._cache: dict[str, tuple[float, IntentGuess]] = {}

    def _cache_key(
        self, need: str, user_text: str | None, context_text: str | None
    ) -> str:
        return "\u241f".join(
            (
                " ".join(str(need or "").split())[:200],
                " ".join(str(user_text or "").split())[:200],
                " ".join(str(context_text or "").split())[:600],
            )
        )

    def _cached(self, key: str) -> IntentGuess | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, guess = entry
        # ``>=``, not ``>``: with a TTL of 0 the entry is already stale the
        # moment it is written, and ``>`` compared against a same-tick
        # ``time.time()`` would let it slip through as a hit.
        if time.time() >= expires_at:
            self._cache.pop(key, None)
            return None
        return IntentGuess(list(guess.pairs), guess.intensity)

    def _store(self, key: str, guess: IntentGuess) -> None:
        if len(self._cache) > 128:
            self._cache.clear()
        self._cache[key] = (
            time.time() + self.cache_ttl_seconds,
            IntentGuess(list(guess.pairs), guess.intensity),
        )

    def _build_messages(
        self, need: str, user_text: str | None, context_text: str | None
    ) -> list[dict[str, str]]:
        context = (context_text or "").strip() or "（无）"
        current = (user_text or need or "").strip()
        body = [
            f"[最近对话]\n{context}",
            f"[当前这句话]\n{current}",
        ]
        request = (need or "").strip()
        if request and request != current:
            body.append(f"[她想发的表情包需求]\n{request}")
        body.append("这张表情包最贴近哪一类？")
        return [
            {"role": "system", "content": CLASSIFIER_INSTRUCTIONS},
            {"role": "user", "content": "\n\n".join(body)},
        ]

    def classify(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> IntentGuess:
        if self.llm is None or time.time() < self._disabled_until:
            return IntentGuess()
        if getattr(self.llm, "available", True) is False:
            return IntentGuess()
        key = self._cache_key(need, user_text, context_text)
        hit = self._cached(key)
        if hit is not None:
            return hit
        started = time.time()
        result = self.llm.chat(
            self._build_messages(need, user_text, context_text),
            max_tokens=self.max_tokens,
            temperature=0.0,
            session_id="sticker:intent",
        )
        latency_ms = (time.time() - started) * 1000
        if result is None:
            self._disabled_until = time.time() + self.cooldown_seconds
            self._metric("intent_classify", {"backend": self.name, "ok": False, "latency_ms": round(latency_ms)})
            return IntentGuess()
        guess = _parse_guess(str(getattr(result, "text", "") or ""))
        self._metric(
            "intent_classify",
            {
                "backend": self.name,
                "ok": bool(guess),
                "labels": guess.labels,
                "intensity": guess.intensity,
                "latency_ms": round(latency_ms),
            },
        )
        if guess:
            self._store(key, guess)
        return guess

    def _metric(self, key: str, payload: dict[str, Any]) -> None:
        for attr in ("metric", "_metric"):
            writer = getattr(self.llm, attr, None)
            if callable(writer):
                try:
                    writer(key, 1.0, payload)
                except Exception:
                    pass
                return


class KevClassifier:
    """Local Kev decision model: a ``choice`` question over the taxonomy."""

    name = "kev"

    def __init__(self, client: Any, *, cooldown_seconds: float = 60.0, min_probability: float = 0.05):
        self.client = client
        self.cooldown_seconds = float(cooldown_seconds)
        self.min_probability = float(min_probability)
        self._disabled_until = 0.0

    def classify(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> IntentGuess:
        if self.client is None or time.time() < self._disabled_until:
            return IntentGuess()
        state = {
            "current_message": (user_text or need or "")[:400],
            "sticker_need": (need or "")[:200],
            "recent_context": (context_text or "")[:1200],
        }
        # Two questions, one request: Kev answers them in parallel and neither
        # can read the other, so "which kind" and "how strong" stay independent
        # — the property an autoregressive chat model cannot offer.
        questions = {
            "intent": {
                "type": "choice",
                "instructions": "这段对话此刻要发的表情包，最贴近哪一类？",
                "criteria": dict(INTENT_LABELS),
            },
            "intensity": {
                "type": "score",
                "instructions": "这句话的语气有多强？（1=很淡，5=很激烈）",
                "criteria": ["1", "2", "3", "4", "5"],
            },
        }
        try:
            answers = self.client.system_one(state, questions)
        except Exception:
            self._disabled_until = time.time() + self.cooldown_seconds
            return IntentGuess()
        answer = answers.get("intent")
        probabilities = getattr(answer, "probabilities", None) or {}
        ranked = sorted(
            (
                (str(name), float(value))
                for name, value in probabilities.items()
                if str(name) in INTENT_KEYWORDS and float(value) >= self.min_probability
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        if not ranked:
            chosen = str(getattr(answer, "value", "") or "")
            ranked = [(chosen, 1.0)] if chosen in INTENT_KEYWORDS else []
        intensity = None
        rating = answers.get("intensity")
        if rating is not None and isinstance(getattr(rating, "value", None), (int, float)):
            intensity = max(1.0, min(5.0, float(rating.value)))
        return IntentGuess(ranked[:MAX_INTENTS], intensity)


# Where strength actually changes which picture is right. For social intents
# (thank you, sorry, goodnight) the library holds near-identical strengths, so
# asking costs seconds and moves nothing.
INTENSITY_SENSITIVE: frozenset[str] = frozenset(
    {"angry", "happy", "sad", "comfort", "tease", "surprise", "celebrate", "flirt", "shy"}
)


class HybridClassifier:
    """Keyword rules when they are unambiguous, context when they are not.

    Measured over the 21-case benchmark: rules alone are right 62% of the time,
    but they fail by returning *nothing* (an ellipsis with no listed keyword)
    or by tying. Whenever a single keyword produces one clear winner the rules
    are already correct, so paying 3-6 seconds for a second opinion there buys
    nothing — with one exception: the rules never report *how strong* the
    request is, so for emotionally variable intents the call is still worth it
    to fill in that second dimension.
    """

    name = "hybrid"
    CONFIDENT_SCORE = 0.6

    def __init__(
        self,
        rules: Any,
        contextual: Any,
        sensitive: frozenset[str] = INTENSITY_SENSITIVE,
    ):
        self.rules = rules
        self.contextual = contextual
        self.sensitive = sensitive

    def classify(
        self,
        need: str,
        user_text: str | None = None,
        context_text: str | None = None,
    ) -> IntentGuess:
        fallback = self.rules.classify(need, user_text, context_text)
        if self.contextual is None:
            return fallback
        if self._confident(fallback):
            if fallback.pairs[0][0] not in self.sensitive:
                return fallback
            # Confident about *what*, blind about *how much*: take the strength
            # only, and keep the label the rules already settled on.
            guess = self.contextual.classify(need, user_text, context_text)
            if (
                guess
                and guess.intensity is not None
                and guess.labels[:1] == fallback.labels[:1]
            ):
                return IntentGuess(list(fallback.pairs), guess.intensity)
            return fallback
        guess = self.contextual.classify(need, user_text, context_text)
        return guess or fallback

    def _confident(self, guess: IntentGuess) -> bool:
        if not guess:
            return False
        top = guess.pairs[0][1]
        if top < self.CONFIDENT_SCORE:
            return False
        # A tie is exactly the situation that sent a pouting "才不是" for a
        # bedtime goodnight; let the contextual classifier break it.
        if len(guess.pairs) > 1 and guess.pairs[1][1] >= top:
            return False
        return True


def build_classifier(config: Any, llm: Any = None) -> IntentClassifier:
    """Pick a backend from ``sticker.intent_backend``; degrade to rules."""
    backend = str(config.get("sticker.intent_backend", "rules")).strip().lower()
    contextual: IntentClassifier | None = None
    if llm is not None:
        contextual = LlmClassifier(
            llm,
            max_tokens=int(config.get("sticker.intent_max_tokens", 400)),
            cooldown_seconds=float(config.get("sticker.intent_cooldown_seconds", 120.0)),
        )
    if backend == "llm":
        return contextual or RuleClassifier()
    if backend == "hybrid":
        if contextual is None:
            return RuleClassifier()
        return HybridClassifier(RuleClassifier(), contextual)
    if backend == "kev":
        try:
            from ..decision.kev import KevClient

            client = KevClient(
                str(config.get("decision.kev_base_url", "http://127.0.0.1:8009")),
                str(config.get("decision.kev_model", "")),
                float(config.get("decision.kev_timeout_seconds", 3.0)),
            )
            return KevClassifier(
                client,
                cooldown_seconds=float(config.get("sticker.intent_cooldown_seconds", 60.0)),
            )
        except Exception:
            return RuleClassifier()
    return RuleClassifier()
