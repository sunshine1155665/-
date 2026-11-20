"""tone → 合成参数（速度，以及历史遗留的 instruct 措辞）。

**先说结论：在这台机器现在的模型上，instruct 这条路是坏的，六个类别全部不发措辞。**
语气只剩 `TONE_SPEED` 那一段速度谱，加上文本层（标点、语气词、断句）。

为什么坏的（`docs/branches/voice.md` 有完整记录）：

  * 官方 `instruct2` 的示例加载的是 **`CosyVoice-300M-Instruct`** —— 一个专门用
    instruct 数据微调过的模型；`repos/CosyVoice/cosyvoice/utils/common.py::instruct_list`
    那 27 条措辞也是给它用的。我们用的是 `Fun-CosyVoice3-0.5B`（zero-shot 底座）。
  * 在底座上调用 instruct2，会先把这串措辞当 prompt_text 拼进模型输入，而
    CosyVoice3 的 LLM 里有一句硬断言要求 `<|endofprompt|>` 必须在场。缺了它
    assert 在 **LLM 线程里**炸掉，外面看到的是「音频 0 个 token → token2wav 在空
    张量上 interpolate → 503」。
  * 补上 token 之后不崩了，但更糟：**模型把指令当台词念出来**。实测同一句
    「晚安啦，做个好梦哦。我明天还在这里等你。」，instruct 版 9.82s（正常 3.26s），
    mimo 听写回来是「……做个好梦哦**的语气说这句话，他都说提冰电到了，是吧？**」。
    另一次是「……好梦哦。**语气，当多大低棒？请用非常开心的语气，尽可能快速的讲
    一句话。**」—— 也就是说那 3 倍时长不是"念得慢"，是多念了一堆废话。

所以现在这个模块只剩两件事：**类别归一化** 和 **速度映射**。`INSTRUCTS` 保留成
全 None 的字典，是为了让"想再试一次措辞"有个明确的落点，而不是散在代码各处。

想加新语气：先想清楚它是"快一点还是慢一点"，改 `TONE_SPEED`；声学上想换音色只能
换参考音（`tts.ref_audio_path`），不是换措辞。
"""

from __future__ import annotations

# 六个类别够用了。再加会让模型的选择变得随意（它只有 400 token 的预算）。
# 委屈系实验档（aggrieved_a/b/c）是 RVC 语料的临时产物，不是产线语气，已移除。
TONES: tuple[str, ...] = (
    "neutral", "happy", "soft", "sad", "annoyed", "lively",
)

# 官方训练措辞（英文两条也是官方的，soft/loud 只有英文版）。
#
# **六条故意全是 None。** 这里曾经只剩 lively 一条，理由是"它是唯一不减速的情绪
# 措辞"—— 但那条测量是在 instruct 还"看起来能用"的时候做的，后来发现两件事：
#   1) 缺 <|endofprompt|> 时 instruct2 会在 LLM 线程里断言失败（503）；
#   2) 补齐之后模型会把指令当台词念出来（实测 3 倍时长 + 多念一堆废话）。
# 两条都记在 docs/branches/voice.md。留着这个全 None 的字典是为了让
# "哪天换了 Instruct 版模型想再试"有个唯一入口，而不是让人满代码找字符串。
INSTRUCTS: dict[str, str | None] = {
    "neutral": None,
    "happy": None,
    "soft": None,
    "sad": None,
    "annoyed": None,
    "lively": None,
}

# CosyVoice3 的格式：前缀 + 指令 + <|endofprompt|> 放在**句尾**。
# 放前面会让模型直接 assert 失败。
PREFIX = "You are a helpful assistant. "
END = "<|endofprompt|>"

# 语气自带的速度。全部来自真机测量（data/audio_genki/，同一参考音、同一句话）。
#
# **元气地板：这张表里没有一个值低于 1.00。** 这不是巧合，是硬约束 —— 鲸鱼娘
# 任何情绪下都不能有气无力，而"慢"是她垮掉的第一个信号。有测试钉住这条。
#
# 各档的来历（字/秒、F0 中位、起伏半音，晚安句 32 字；mimo 打分见 review*.json）：
#   zero-shot 1.00   5.44 / 338.0 / 5.19   mimo: 甜但偏快偏硬（4/4/4/5）
#   zero-shot 0.97   5.27 / 346.6 / 5.00   mimo: 能量偏低、元气 3
#   soft 1.02        5.54 / 342.9 / 5.27   mimo: 偏闷、元气 3 —— 只慢 3% 就掉一档
#   neutral 1.05     5.70 / 342.9 / 5.27   mimo: 5/5/5/5（最佳，定为默认）
#   lively 1.10      5.32 / 303.8 / 4.61   mimo: 元气 5，但"语速过快、音色偏尖"
#
# 由此定下五条：
#   neutral 1.05  —— 实测唯一满分的听感，就是默认
#   soft    1.05  —— 温柔**不再从参数上要**：1.02 就掉一档元气，改由文本层给
#   happy   1.08  —— 兴奋靠"快"表达，不靠"非常开心"那句措辞（它会掉 37% 语速）
#   sad     1.00  —— 难过可以压到地板，但不许更低；"伤心+尽可能快"实测反而更慢更累
#   annoyed 1.05  —— 傲娇靠语气词和节奏，不靠"非常生气"（会尖利、吞字）
#   lively  1.10  —— 最快的一档；原本叠着"尽可能快"那句措辞，现在也只有速度
#
# 之前一版按"情绪要匹配"把 soft 设成 1.12 + very soft，结果 mimo 判
# "音色 1 分、元气 1 分，虚弱" —— 用户的原话是"晚安过于有气无力"。那条路是错的。
TONE_SPEED: dict[str, float] = {
    "neutral": 1.05,
    "happy": 1.08,
    # soft 也叫 1.05：实测 1.02（只慢 3%）mimo 就判"元气 3 分、偏闷"。
    # 温柔不再从声学参数上要，改由文本层给（短句、语气词、标点）。
    "soft": 1.05,
    "sad": 1.00,
    "annoyed": 1.05,
    "lively": 1.10,
}

# 没有语气的声音也应该比裸模型稍微快一点：这是 A2 那条 5/5/5/5 的实测值。
DEFAULT_SPEED = 1.05

# 模型可能写的近义词。不在这里的一律当 neutral —— 宁可没有语气，
# 也不要给合成器一句它没见过的指令。
ALIASES: dict[str, str] = {
    "": "neutral",
    "none": "neutral",
    "calm": "neutral",
    "normal": "neutral",
    "plain": "neutral",
    "gentle": "soft",
    "tender": "soft",
    "warm": "soft",
    "shy": "soft",
    "sweet": "soft",
    "cheerful": "happy",
    "glad": "happy",
    "joyful": "happy",
    "excited": "lively",
    "playful": "lively",
    "energetic": "lively",
    "genki": "lively",
    "sorry": "sad",
    "upset": "sad",
    "low": "sad",
    "angry": "annoyed",
    "mad": "annoyed",
    "tsundere": "annoyed",
}


def normalize_tone(value: object) -> str:
    """把模型给的东西收成一个合法 tone。

    大小写、空格、未知值都在这里被吃掉 —— 合成器不该因为模型多打了一个字
    就整条语音失败。
    """
    raw = str(value or "").strip().lower()
    if raw in TONES:
        return raw
    # 中文也认，提示词是中文写的，模型很可能直接回中文。
    chinese = {
        "平静": "neutral",
        "普通": "neutral",
        "温柔": "soft",
        "轻声": "soft",
        "撒娇": "soft",
        "开心": "happy",
        "高兴": "happy",
        "伤心": "sad",
        "难过": "sad",
        "生气": "annoyed",
        "不耐烦": "annoyed",
        "傲娇": "annoyed",
        "活力": "lively",
        "元气": "lively",
        "兴奋": "lively",
    }
    if raw in chinese:
        return chinese[raw]
    return ALIASES.get(raw, "neutral")


def instruct_for(tone: object) -> str | None:
    """tone → 可以直接发给 CosyVoice 的 instruct 字符串（None = zero-shot）。"""
    text = INSTRUCTS.get(normalize_tone(tone))
    if not text:
        return None
    return f"{PREFIX}{text}{END}"


def speed_for(tone: object) -> float:
    """tone → 合成速度。没测过的语气返回 1.0，不猜。"""
    return TONE_SPEED.get(normalize_tone(tone), DEFAULT_SPEED)


def describe_tone(tone: str) -> str:
    """给日志/决策面板用的一行中文说明。"""
    return {
        "neutral": "不提措辞，速度 1.05（实测最佳）",
        "happy": "不提措辞，靠语速偏快表达兴奋",
        "soft": "不提措辞，只慢 3% —— 保住元气",
        "sad": "不提措辞，压在地板上（1.00）",
        "annoyed": "不提措辞，傲娇靠语气词不靠「凶」",
        "lively": "不提措辞（这代模型会念出指令），只把速度拉到 1.10",
    }.get(tone, tone)
