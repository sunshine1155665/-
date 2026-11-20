"""How strongly is this message feeling something, on a 1-5 scale.

This started life as the sticker service's second gate ("is the moment charged
enough to deserve a picture").  Sticker selection is gone -- stickers are now
generated per request -- but the voice layer still uses the same scale to pick
a tone when the model did not name one (``voice/tone.py``), so the scorer moved
here rather than dying with the retrieval code.

Why not just ask the classifier? Because the spontaneous path never calls it,
and adding a network call to every turn to pick a tone is the wrong trade.  An
LLM's 1-5 also wobbles enough that a threshold on it would be untunable.

So this is deliberately mechanical: what is visible in the text and nothing
else. It is cheap, offline, identical every time, and — most importantly —
measurable, which is what lets the threshold be calibrated against real
sentences instead of guessed.

The scale matches the classifier's instructions ("1=很淡, 3=平常, 5=很激烈") so
a model-supplied number and an estimate can be compared without conversion.
"""

from __future__ import annotations

import re

EMOTION_MIN = 0.0
EMOTION_MAX = 5.0
# Every sentence starts mildly charged: "嗯" is faint, but it is not a spreadsheet.
BASE = 1.6

# Wide on purpose -- this list only ever decides *how much*, never *whether the
# wording fits the situation*. Missing a word here is a missed feeling ("刚被
# 骂了一顿" scored 1.60 before 骂 was added), which is worse than an extra hit.
#
# Two tiers, because "饿" and "崩溃" are not the same amount of feeling. With a
# flat weight for both, "我好难过，怎么办啊" and "有点饿" came out the same, and
# the first one -- a real feeling wrapped in a request for help -- got scored
# below the second.
STRONG_MOOD: tuple[str, ...] = (
    # tired
    "累", "疲惫", "撑不住", "摆烂", "熬夜", "没睡",
    # down
    "难过", "委屈", "想哭", "哭", "心疼", "低落", "难受", "丧", "emo", "郁闷",
    # up
    "开心", "高兴", "好耶", "爽", "得意", "飘", "兴奋", "激动", "幸福", "乐",
    # angry / annoyed
    "气", "烦", "讨厌", "怒", "崩溃", "炸", "受不了", "服了", "无语", "离谱",
    # anxious
    "紧张", "焦虑", "慌", "担心", "压力", "怕", "不安", "纠结",
    # lonely / bored
    "无聊", "寂寞", "孤单", "想你", "没人",
    # social
    "夸", "表扬", "骂", "凶", "冤枉", "欺负", "不理", "道歉", "对不起",
    "谢谢", "感谢", "抱歉",
    # reactions
    "哼", "呸", "略", "呜呜", "555", "震惊", "居然", "没想到", "天啊", "笑",
    # scenes that carry their own weight
    "晚安", "早安", "抱抱", "贴贴", "摸摸", "亲亲",
)

# Bodily states and idle states. They are feelings, but small ones: "有点饿"
# does not carry the weight "崩溃了" does.
MILD_MOOD: tuple[str, ...] = ("冷", "热", "饿", "渴", "困", "疼", "头疼", "没力气", "不想动", "瘫", "睡", "在吗")


# Words that only ever raise the temperature. "好" is *not* here on purpose --
# it is far more often "好的"/"你好" than "好累".
# "太" is matched separately because "不太明白" is the opposite of intense.
INTENSIFIERS: tuple[str, ...] = (
    "特别", "超级", "非常", "极其", "简直", "完全", "根本", "死了", "死",
    "爆", "疯了", "崩了", "到不行", "极了", "怎么会", "真的", "超",
)
_INTENSE_RE = re.compile(r"(?<!不)太")

# Doing-a-task wording. Strong on purpose: "帮我把这段话翻译成英文" can easily
# contain an emotional-looking word ("不明白", "不对") and still be pure business.
#
# Also two tiers. The question words are *soft*: "怎么办啊" is how people ask for
# help when they are upset, and punishing it as hard as "翻译" turned "我好难过，
# 怎么办啊" into 1.40 -- lower than "有点饿".
HARD_TASK: tuple[str, ...] = (
    "帮我", "麻烦", "请帮", "翻译", "列出", "整理",
    "计算", "解释", "介绍", "对比", "推荐", "查一下", "告诉我", "写一下",
    "改成", "报错", "配置", "安装", "定义", "推导", "证明",
)

# Question words ("怎么办啊") and topic nouns ("代码", "公式"). Both are weak
# signals: a topic noun says what the sentence is *about*, not that it is a
# request. "写代码写到崩溃" is a feeling first and a topic second.
SOFT_TASK: tuple[str, ...] = (
    "怎么", "如何", "为什么", "几点", "是不是", "能不能", "是什么", "什么是", "哪个", "多少",
    "代码", "公式", "显存", "语法", "量纲", "表格",
)

# Acknowledging, not feeling.
ACK_WORDS: frozenset[str] = frozenset(
    {"嗯", "嗯嗯", "哦", "哦哦", "好的", "好", "收到", "行", "对", "是", "在", "ok", "okay"}
)

# Rituals, not emotions. Saying goodnight at 1 intensity still deserves a soft
# tone -- the occasion is the reason, not the feeling.
SCENE_WORDS: tuple[str, ...] = ("晚安", "睡", "好梦", "早安", "在吗", "谢谢", "感谢", "对不起", "抱歉")
SCENE_FLOOR = 3.0

_REPEAT_RE = re.compile(r"([\u4e00-\u9fff])\1{1,}")
_LONG = 24
_VERY_LONG = 48


def _hits(lowered: str, words: tuple[str, ...]) -> int:
    return sum(1 for word in words if word in lowered)


def breakdown(text: str) -> dict[str, float]:
    """Score plus its reasons, so a threshold can be argued with."""
    raw = str(text or "").strip()
    if not raw:
        return {"score": 0.0}
    lowered = raw.lower()
    score = BASE
    notes: dict[str, float] = {"base": BASE}

    strong = _hits(lowered, STRONG_MOOD)
    mild = _hits(lowered, MILD_MOOD)
    if strong or mild:
        # One word is a hint, three is a mood. A named feeling ("难过") is worth
        # more than a bodily state ("饿") -- see the tier comment above.
        delta = min(2.0, 0.8 * strong + 0.5 * mild)
        score += delta
        notes["mood"] = delta

    loud = _hits(lowered, INTENSIFIERS) + len(_INTENSE_RE.findall(lowered))
    if loud:
        delta = min(1.2, 0.6 * loud)
        score += delta
        notes["intensifier"] = delta

    if _REPEAT_RE.search(raw):
        # "啊啊啊" / "嘿嘿" / "呜呜" -- drawn out on purpose.
        score += 0.7
        notes["repeat"] = 0.7

    bangs = raw.count("！") + raw.count("!")
    if bangs:
        delta = min(1.0, 0.5 * bangs)
        score += delta
        notes["bang"] = delta

    if "…" in raw or "。。。" in raw or "..." in raw:
        score += 0.3
        notes["trail_off"] = 0.3

    length = len(raw)
    if length >= _VERY_LONG:
        score -= 1.5
        notes["very_long"] = -1.5
    elif length >= _LONG:
        score -= 0.7
        notes["long"] = -0.7

    tasks = 0.8 * _hits(lowered, HARD_TASK) + 0.3 * _hits(lowered, SOFT_TASK)
    if tasks:
        delta = -min(1.6, tasks)
        score += delta
        notes["task"] = delta

    if lowered.strip("。，！？!? ") in ACK_WORDS:
        score -= 0.9
        notes["ack"] = -0.9

    if _hits(lowered, SCENE_WORDS) and score < SCENE_FLOOR:
        notes["scene_floor"] = round(SCENE_FLOOR - score, 2)
        score = SCENE_FLOOR

    return {"score": round(min(EMOTION_MAX, max(EMOTION_MIN, score)), 2), **notes}


def emotion_intensity(text: str) -> float:
    """0-5, where 1 is flat, 3 is ordinary, 5 is bursting."""
    return float(breakdown(text)["score"])
