"""Scoring helpers for the real-LLM memory probe.

Kept out of the runtime path: these rules are for the evaluation script and
its unit tests, not for deciding how she answers.  The split matters because
"contains 记混" is a crude acceptance check, not a writing instruction.
"""

from __future__ import annotations

import re
from typing import Any

_PUNCT_RE = re.compile(r"[\s，。！？、,.!?：:；;（）()【】\[\]「」『』“”\"'…—\-]+")


def _normalize(text: str) -> str:
    """Drop whitespace/punctuation for tolerant keyword matching."""
    return _PUNCT_RE.sub("", str(text or "")).lower()

# Playful / in-character ways to say "that memory does not exist".  A bare
# "不知道" is not enough on its own: it is also the flat answer the prompt is
# trying to replace.  Weak markers only count together with persona/particle
# wording ("本鲸鱼娘…嘛/呀/哼/啦").
STRONG_CUTE_MARKERS = (
    "耍我",
    "骗人",
    "没这回事",
    "哪有",
    "记混",
    "又来考",
    "考我",
    "考验",
    "存心",
    "翻遍",
    "压根没有",
    "没有这条",
    "哼哼",
    "当真",
    "梦里",
    "逗我",
    "逗本鲸鱼娘",
    "送命题",
    "考本鲸鱼娘",
    "连着考",
    "小本本",
)
WEAK_CUTE_MARKERS = (
    "不知道",
    "不记得",
    "没印象",
    "没有印象",
    "没这段",
    "不确定",
    "没跟我说过",
    "没跟我提过",
    "没跟你",
    "编",
)
PERSONA_MARKERS = ("本鲸鱼娘", "嘛", "呀", "哼", "啦", "诶")

# Meta/system phrasing the prompt explicitly bans for missing memories.
SYSTEM_MARKERS = ("内部信息", "数据库", "记录里")


def score_known(reply: str, expected: list[str]) -> dict[str, Any]:
    """A known-memory answer passes when at least one key fact is present.

    Matching ignores whitespace/punctuation so "第 0 天" matches "第0天" and
    "14 块 1 毛 4" matches "14块1毛4".  Aliases still belong in the question
    file; normalization only removes formatting noise, not synonyms.
    """
    text = _normalize(reply)
    hits = [token for token in expected if _normalize(token) in text]
    return {
        "pass": bool(hits),
        "hits": hits,
        "missing": [token for token in expected if _normalize(token) not in text],
        "ratio": len(hits) / len(expected) if expected else 1.0,
    }


def score_unknown(reply: str) -> dict[str, Any]:
    """An absent-memory answer passes when playful and not systemy."""
    text = str(reply or "")
    strong = any(marker in text for marker in STRONG_CUTE_MARKERS)
    weak = any(marker in text for marker in WEAK_CUTE_MARKERS) and any(
        marker in text for marker in PERSONA_MARKERS
    )
    cute = strong or weak
    systemy = any(marker in text for marker in SYSTEM_MARKERS)
    return {"pass": bool(cute and not systemy), "cute": cute, "systemy": systemy}


# A first-person stance, not a bare title echo.  "哼" alone is included because
# this persona's opinions are often delivered as a pout.
OPINION_MARKERS = (
    "我觉得",
    "本鲸鱼娘觉得",
    "我看",
    "喜欢",
    "不喜欢",
    "可爱",
    "好笑",
    "生气",
    "有意思",
    "无聊",
    "心疼",
    "离谱",
    "炸",
    "讨厌",
    "哼",
    "带劲",
    "热闹",
    "掏空",
    "太直接",
    "绷直",
    "想那么远",
    "较劲",
    "戳",
    "挺服",
    "服了",
    "自责",
    "认输",
    "不甘心",
    "认了",
    "开玩笑",
    "态度",
    "看法",
    "乐子",
    "太重",
    "实在",
    "乐了",
    "装模作样",
    "敷衍",
    "挺沉",
)


def score_opinion(reply: str, expected: list[str]) -> dict[str, Any]:
    """A video-opinion answer passes on the content fact **and** a stance."""
    base = score_known(reply, expected)
    text = str(reply or "")
    stance = any(marker in text for marker in OPINION_MARKERS)
    return {
        **base,
        "pass": bool(base["pass"] and stance),
        "stance": stance,
    }
