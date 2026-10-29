"""Singing modes: one melody spec + one review rubric per mode.

Why modes at all: the flywheel failed for five rounds because a *single* rubric
was applied to everything. Its top complaint was "旋律太好猜、听两句就腻" — but
for a line she hums to comfort you, being predictable is the whole point. The
generator was fine; the measuring stick was wrong.

Two things have to stay together per mode, otherwise they drift apart:
  * ``spec``   — how the melody is generated
  * ``rubric`` — what the reviewer is told to care about, *and what to ignore*

The rubric deliberately states what NOT to penalise. Without that line the
reviewer keeps docking points for narrow range and repetition, which is exactly
the behaviour we want in ``hum`` and ``lullaby``.
"""

from __future__ import annotations

# Melody parameters map 1:1 onto ``melody_v2.Spec``.
MODES: dict[str, dict] = {
    "hum": {
        "label": "日常哼唱",
        "desc": "她随口哼给你听：哄人、黏人、没什么表演性，像贴在耳边唱",
        # Nursery-rhyme spec: 6-9 degree range, motif tiled from the canon,
        # cadence forced onto the tonic. 小星星 is the reference.
        "spec": dict(tonic=62, scale="major", lo=60, hi=76, beat=0.46,
                     contour="arch", span=6.5, rhythm="nursery", motif_len=2,
                     end_rise=False, end_mode="tonic", pattern="auto",
                     run_limit=0, repeat_scale=0.2, break_triples=True),
        "focus": "能不能跟着哼（好记、顺口）、够不够温柔亲近",
        "ignore": "不要因为「旋律太好猜」「太简单」「重复太多」「音域窄」扣分——"
                  "这几项在本档是**优点**，正是随口哼歌该有的样子",
        "need": ("fit", "hum", "natural"),
        "min": 4,
    },
    "genki": {
        "label": "元气表演",
        "desc": "她认真唱给你听：邀功、庆祝、兴头上来了，有表演欲",
        # The "尾音太尖" the user heard came from end_rise + a long high final
        # note. Now the phrase still climbs, but cadences down onto the tonic.
        "spec": dict(tonic=65, scale="major", lo=62, hi=79, beat=0.34,
                     contour="arch", span=9.0, rhythm="nursery", motif_len=3,
                     end_rise=False, end_mode="tonic", pattern="auto",
                     run_limit=3, repeat_scale=1.0, break_triples=True),
        "focus": "精神头、冲劲、有没有一个能记住的乐句",
        "ignore": "不要因为「太跳」「太吵」「音域太宽」扣分——本档就是要外放",
        "need": ("fit", "energy", "hum"),
        "min": 4,
    },
    "chant": {
        "label": "顺口溜",
        "desc": "她带着你打气/喊口号，节奏强，像儿歌或拉拉队",
        "spec": dict(tonic=65, scale="major", lo=62, hi=74, beat=0.30,
                     contour="wave", span=5.0, rhythm="bounce", motif_len=2,
                     end_rise=True, run_limit=2, repeat_scale=0.6,
                     break_triples=False),
        "focus": "节奏咬得准不准、能不能跟着一起喊出来",
        "ignore": "不要因为音域窄、同音重复多扣分——口号本来就靠重复",
        "need": ("hum", "energy"),
        "min": 4,
    },
    # Below are the three reserved modes from docs/melody-modes.md. They are not
    # part of the first validation round; keep them here so the table lives in
    # one place and wiring them later is data, not code.
    "lullaby": {
        "label": "哄睡",
        "desc": "夜里轻声哄睡，慢、软、几乎没有起伏",
        "spec": dict(tonic=58, scale="major", lo=57, hi=68, beat=0.65,
                     contour="fall", span=2.5, rhythm="even", motif_len=2,
                     end_rise=False, run_limit=0, repeat_scale=0.25,
                     break_triples=False),
        "focus": "够不够安静柔软、像不像哄睡",
        "ignore": "不要因为平淡、没有起伏、没有记忆点扣分——哄睡不需要记忆点",
        "need": ("fit", "natural"),
        "min": 4,
    },
    "tsundere": {
        "label": "傲娇",
        "desc": "吃醋嘴硬，假装生气其实在撒娇",
        "spec": dict(tonic=62, scale="minor", lo=60, hi=72, beat=0.42,
                     contour="wave", span=6.0, rhythm="shortlong", motif_len=3,
                     end_rise=True, run_limit=3, repeat_scale=1.0,
                     break_triples=True),
        "focus": "有没有那股又气又软的劲儿",
        "ignore": "不要因为它不像正经唱歌扣分",
        "need": ("fit", "hum"),
        "min": 4,
    },
    "serious": {
        "label": "认真",
        "desc": "郑重、安慰、告白，不闹",
        "spec": dict(tonic=62, scale="major", lo=60, hi=70, beat=0.48,
                     contour="arch", span=3.5, rhythm="even", motif_len=3,
                     end_rise=False, run_limit=4, repeat_scale=0.9,
                     break_triples=True),
        "focus": "稳不稳、有没有诚意、句子收得住",
        "ignore": "不要因为不够活泼扣分——本档不用活泼",
        "need": ("fit", "natural"),
        "min": 4,
    },
}

DIMENSIONS = ("fit", "hum", "natural", "energy")
# Only asked for when the lyric is supplied: does the tune match the *words*,
# not just the scene. Without this the reviewer rated a coy "快夸夸我嘛" and a
# declarative "今天也要开开心心" as equally good matches for the same melody.
SEMANTIC_DIM = "fits_words"
DIM_LABELS = {
    "fit": "像不像这个场景该有的唱法",
    "hum": "听完能不能跟着哼（好记、顺口）",
    "natural": "像不像真人在唱，不机械",
    "energy": "精神头、冲劲",
    "fits_words": "旋律和这句词的**意思**配不配（语气、情绪走向、句尾落点）",
}


def spec_for(mode: str) -> dict:
    return MODES[mode]["spec"]


def rubric_text(mode: str, lyric: str | None = None) -> str:
    m = MODES[mode]
    scored = list(DIMENSIONS) + ([SEMANTIC_DIM] if lyric else [])
    dims = "\n".join(f"- {d}（{DIM_LABELS[d]}）" for d in scored)
    lyric_block = ""
    keys = 'fit":n,"hum":n,"natural":n,"energy":n'
    if lyric:
        lyric_block = (
            f"\n这段话的歌词是：『{lyric}』。\n"
            f"请额外判断 {SEMANTIC_DIM}：这段旋律像不像在说这句话——"
            "该上扬的地方上扬了吗？该收住的地方收住了吗？语气和词义对不对得上？"
            "（不要因为旋律好听就给高分，只看配不配）\n")
        keys += f',"{SEMANTIC_DIM}":n'
    return (
        f"你在评审一段「{m['label']}」：{m['desc']}。\n"
        f"{lyric_block}\n"
        f"给以下维度各打 1-5 的整数分：\n{dims}\n\n"
        f"本档最看重：{m['focus']}。\n"
        f"特别注意：{m['ignore']}。\n\n"
        "另外给：\n"
        "- feel：你听完的体感，一两句，像人说话那样直说\n"
        "- suggest：2-3 条具体可执行的改法（指向节奏/音域/跳进/重复度/句尾），不要空话\n"
        "- problem：20 字内最主要的问题\n\n"
        "只输出一行 JSON：\n"
        '{"' + keys + ',"problem":"...","feel":"...","suggest":["...","..."]}'
    )
