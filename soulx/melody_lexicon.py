"""A small, hand-written semantic lexicon for melody generation.

Why a hand-written table instead of a model: the generator has to stay
deterministic, offline and dependency-free (ADR-0002: the LLM never writes
notes). The table is also *auditable* — when a line gets a weird duration, you
can point at the word that caused it, which is impossible with an embedding.

Three jobs:

1. **Word weight** ``w`` in [0, 1] feeds the duration formula
   ``dur = base * word_class * (1 + 0.15 * w)``. Warm/comfort words hold a
   little longer; function words are already shortened by the caller.
2. **Concreteness**: a line with two or more concrete nouns gets a wider motif
   (a song about "热汤和拥抱" should not use the narrowest figure).
3. **Entry gesture**: a line that opens with a vocative (主人 / 哥哥 / 你) should
   start on a repeated note or a rising second, the way people actually call
   someone before saying the sentence.

The weights are deliberately small and clamped by the caller. This is a nudge,
not a rewrite: the tone-direction rules always win.
"""

from __future__ import annotations

# Categories are ordered by priority when a character appears in more than one:
# the first category with the highest weight wins in ``weights()`` because we
# take ``max``.
CATEGORY_WEIGHT = {
    "warm": 1.0,
    "vocative": 0.6,
    "wait": 0.6,
    "concrete": 0.7,
    "neutral": 0.2,
}

WARM = {
    # care / temperature / closeness
    "热", "暖", "汤", "拥抱", "抱", "陪", "伴", "想", "念", "喜欢", "爱",
    "亲", "摸", "贴", "哄", "疼", "担心", "安心", "守护", "照顾", "辛苦",
    "谢谢", "乖", "甜", "软", "温柔", "幸福", "开心", "快乐", "生日",
    "晚安", "早安", "回家", "等你", "回来", "等你回来", "心疼", "想念",
    "撒娇", "亲亲", "抱抱", "摸摸", "贴贴", "抱紧", "依靠", "陪着你",
    "准备好", "欢迎", "辛苦啦", "没关系", "别怕", "不怕", "慢慢", "轻轻",
}

VOCATIVE = {
    "主人", "哥哥", "姐姐", "宝宝", "宝贝", "亲爱的", "大肥鱼", "鲸鱼娘",
    "娘", "你", "我", "咱们", "我们", "人家",
}

# Only real terms of address trigger a call-like melodic entry. 我/你 are
# pronouns, not addresses: a line that opens with 我数着… is narration, and
# starting it on a held call note sounds like the wrong sentence.
ADDRESS = {
    "主人", "哥哥", "姐姐", "宝宝", "宝贝", "亲爱的", "大肥鱼", "鲸鱼娘",
    "娘",
}

# Verbs of waiting / caring. These lengthen the penultimate syllable: the
# sentence leans into the action before it lands.
WAIT = {
    "等", "数", "守", "盼", "望", "找", "回", "来", "去", "走", "跑", "跳",
    "飞", "游", "看", "听", "唱", "笑", "哭", "牵", "拍", "摇", "转",
    "数着", "守着", "等着", "盼着", "想着", "念着", "准备好", "回来",
}

CONCRETE = {
    # things you can point at; two of them justify a wider interval
    "海", "浪", "海浪", "汤", "饭", "菜", "灯", "家", "门", "窗", "雨",
    "雪", "风", "星", "星星", "月", "月亮", "太阳", "花", "蛋糕", "礼物",
    "床", "被", "枕", "船", "鲸", "鱼", "尾巴", "泡泡", "沙滩", "贝壳",
    "云", "天空", "糖", "茶", "杯", "碗", "筷子", "桌", "椅", "路", "街",
    "车站", "手机", "消息", "歌", "琴", "画", "书", "猫", "狗", "灯光",
    "热汤", "拥抱", "枕头", "被子", "房间", "客厅", "厨房", "阳台",
}

CATEGORIES = {
    "warm": WARM,
    "vocative": VOCATIVE,
    "wait": WAIT,
    "concrete": CONCRETE,
}
def _cjk(text: str) -> list[str]:
    return [c for c in text if "\u4e00" <= c <= "\u9fff"]


def weights(text: str) -> list[float]:
    """Per-character semantic weight, aligned to the CJK characters in ``text``."""
    chars = _cjk(text)
    out = [0.0] * len(chars)
    for cat, words in CATEGORIES.items():
        val = CATEGORY_WEIGHT[cat]
        for word in words:
            n = len(word)
            for i in range(len(chars) - n + 1):
                if "".join(chars[i:i + n]) == word:
                    for j in range(i, i + n):
                        out[j] = max(out[j], val)
    return out


def features(text: str) -> dict:
    """Counts used by motif dispatch. Matching is substring-based on CJK text."""
    chars = _cjk(text)
    joined = "".join(chars)

    def hits(words: set[str]) -> int:
        # Count non-overlapping-ish occurrences; short lyrics make this cheap.
        return sum(joined.count(w) for w in words if w)

    return {
        "concrete": hits(CONCRETE),
        "wait": hits(WAIT),
        "vocative": hits(VOCATIVE),
        "warm": hits(WARM),
        # "has_vocative" is specifically *opens with* an address: only then does
        # a call-like entry (hold, or rise a second) sound like someone being
        # spoken to. A 你 in the middle of the line is not an address.
        "has_vocative": bool(joined) and any(
            joined.startswith(w) for w in ADDRESS if w),
        "has_wait": any(w in joined for w in WAIT),
        "warm_first": bool(chars) and any(
            joined.startswith(w) for w in WARM if w),
    }
