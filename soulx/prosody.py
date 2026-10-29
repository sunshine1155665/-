"""Prosody layer: read the *meaning* of a line and pick a melodic gesture.

Why this exists: the pattern library chose its motif by syllable count, so
「快夸夸我嘛」(coy, asking for praise) and 「今天也要开开心心」(bright, declarative)
received the identical 小星星 shape. The melody was singable but semantically
wrong — which is the complaint that replaced "太平".

Mandarin gives us unusually strong, cheap signals for this, and none of them
need a language model:

  * the final particle decides the sentence mood — 吗/呢/吧/嘛 is asking,
    呀/啦/哦 is light and finished, 好不好 is inviting;
  * a small closed vocabulary marks affect — 不/别/怕/累 is withdrawn,
    超/最/厉害/棒 is boasting;
  * reduplication (开开心心, 夸夸) wants the *same pitch twice*, which is also
    what makes a line feel like a nursery rhyme;
  * punctuation is nearly free: ？ asks, ！ exclaims, 。 states.

A phrase-level gesture then picks motif + register + cadence together. The LLM
(if any) only picks the gesture — it never writes notes.
"""

from __future__ import annotations

import re

# --- closed-vocabulary signals -------------------------------------------------
ASK_FINAL = {"吗", "呢", "么"}      # real questions
COAX_FINAL = {"嘛", "吧"}         # 撒娇/征求，语气软而不是问
SOFT_FINAL = {"呀", "啦", "哦", "喔", "耶", "咯"}
ASK_PHRASES = ("好不好", "行不行", "要不要", "对不对", "是不是", "可不可以", "好吗")
WITHDRAWN = {"不", "别", "怕", "累", "哭", "疼", "难过", "委屈", "凶", "凶凶", "困"}
HYPE = {"超", "最", "厉害", "棒", "开心", "喜欢", "爱", "加", "耶", "好棒", "太"}
INVITE = {"一起", "去", "来", "陪", "走", "出发", "要不要"}

CJK = re.compile(r"[\u4e00-\u9fff]")

# gesture -> how the generator should behave.
#   pattern   : motif key in melody_v2.PATTERNS
#   end_mode  : "tonic" (settle) or "rise" (leave it hanging / asking)
#   register  : semitone offset applied to tonic (positive = brighter)
#   beat_mul  : tempo multiplier (smaller = faster)
#   span_mul  : contour amplitude multiplier
GESTURES: dict[str, dict] = {
    # 征求/疑问：句尾往上挑，但整句不要宽
    "ask":      dict(pattern="call", end_mode="rise",  register=+2, beat_mul=0.95, span_mul=0.8),
    # 邀功/炫耀：宽、快、往上冲，最后收住
    "boast":    dict(pattern="star", end_mode="tonic", register=+4, beat_mul=0.85, span_mul=1.15),
    # 邀请/祈使：拱形，落到句中再上挑
    "invite":   dict(pattern="arc",  end_mode="rise",  register=+2, beat_mul=0.95, span_mul=1.0),
    # 示弱/委屈：窄、慢、下行，别亮
    "soft":     dict(pattern="climb", end_mode="tonic", register=-3, beat_mul=1.15, span_mul=0.6),
    # 打气/叠词：同音重复，方正
    "cheer":    dict(pattern="tiger", end_mode="tonic", register=+2, beat_mul=0.9,  span_mul=0.9),
    # 平铺陈述
    "plain":    dict(pattern="auto", end_mode="tonic", register=0,  beat_mul=1.0,  span_mul=1.0),
}


def _chars(text: str) -> list[str]:
    return CJK.findall(text)


def analyze(text: str) -> dict:
    """Return {gesture, reason, emphasis, repeats, final}."""
    chars = _chars(text)
    if not chars:
        return {"gesture": "plain", "reason": "no cjk", "emphasis": [],
                "repeats": [], "final": ""}

    final = chars[-1]
    stripped = text.strip()
    last_punct = stripped[-1] if stripped else ""

    emphasis: list[int] = []
    repeats: list[int] = []

    # reduplication: adjacent identical characters share a pitch
    for i in range(1, len(chars)):
        if chars[i] == chars[i - 1]:
            repeats.append(i)

    # a two-character reduplication (开开心心, 夸夸) just doubles the pair
    for i in range(2, len(chars)):
        if chars[i] == chars[i - 2] and chars[i] != chars[i - 1]:
            repeats.append(i)

    gesture, reason = "plain", "默认陈述"
    has_ask_phrase = any(p in stripped for p in ASK_PHRASES)
    has_withdrawn = (not has_ask_phrase
                     and any(w in stripped for w in WITHDRAWN))
    if has_ask_phrase:
        gesture, reason = "invite", "含『好不好/要不要』类征求短语"
    elif has_withdrawn and last_punct not in "？?":
        # 「我有点怕嘛」里的『嘛』是撒娇不是提问，示弱词优先
        gesture, reason = "soft", "含『不/别/怕/累』类示弱词"
    elif final in ASK_FINAL or last_punct in "？?":
        gesture, reason = "ask", f"句尾『{final}』/问号，是疑问"
    elif any(w in stripped for w in HYPE):
        gesture, reason = "boast", "含『超/最/厉害』类邀功词"
    elif any(w in stripped for w in INVITE):
        gesture, reason = "invite", "含『一起/去/来』类邀请词"
    elif repeats:
        gesture, reason = "cheer", "有叠字，适合同音重复"
    elif final in COAX_FINAL:
        gesture, reason = "plain", f"句尾『{final}』是撒娇式轻收"
    elif final in SOFT_FINAL:
        gesture, reason = "plain", f"句尾『{final}』只是语气轻收"

    # emphasis: the content word before the final particle carries the accent
    if len(chars) >= 2 and final in ASK_FINAL | SOFT_FINAL:
        emphasis.append(len(chars) - 2)
    elif chars:
        emphasis.append(len(chars) - 1)

    return {"gesture": gesture, "reason": reason, "emphasis": emphasis,
            "repeats": repeats, "final": final, "chars": "".join(chars)}


LEAP_TONES = {2, 4}   # only these syllables can carry a 3rd-or-wider interval


def choose_pattern(text: str, info: dict, patterns: dict[str, list[int]],
                   tones: list[int] | None = None) -> str:
    """Pick the motif whose leaps can actually be sung on these syllables.

    A motif is not free: 「快夸夸我嘛」is tone 4/1/1/3/5, so a motif that opens
    1 -> 5 would need a rising fifth on a tone-1 syllable, which is 倒字, and the
    repair step then flattens the motif into a scale run. Scoring candidate
    motifs by where their leaps land keeps the shape intact.

    ``tones`` can be supplied by the caller (``text2melody.analyze`` already
    knows them). Without it we try g2pM, then fall back to neutral tones — the
    old fallback made every leap look equally bad, which is why callers used to
    hard-code a motif instead.
    """
    from melody_v2 import TONE_RANGE  # local import to avoid a cycle

    chars = list(info["chars"])
    if tones is None:
        try:
            from g2pM import G2pM

            sylls = G2pM()("".join(chars), tone=True, char_split=False)
            tones = [int(s[-1]) for s in sylls if s[-1].isdigit()]
        except Exception:  # noqa: BLE001
            try:
                import text2melody as t1

                tones = [s.tone for s in t1.analyze("".join(chars))
                         if s.kind == "note"]
            except Exception:  # noqa: BLE001
                tones = [1] * len(chars)

    def cost(motif: list[int]) -> float:
        bad = 0.0
        for i in range(1, min(len(motif), len(tones))):
            d = motif[i] - motif[i - 1]
            if abs(d) < 3:
                continue
            tone = tones[i]
            if tone not in LEAP_TONES:
                bad += 2.0
            elif tone == 2 and d < 0:
                bad += 1.5
            elif tone == 4 and d > 0:
                bad += 1.5
        # Reward width. Without this the scorer simply picks the motif with no
        # leaps (tiger/swing) and we are back to "太平" — which is exactly the
        # failure the user heard.
        width = max(motif) - min(motif)
        return bad * 2.0 - min(width, 9) * 0.45

    ranked = sorted(patterns, key=lambda k: (cost(patterns[k]), len(patterns[k])))
    g = GESTURES[info["gesture"]]
    if g["pattern"] != "auto":
        pref = g["pattern"]
        # keep the gesture's motif unless it fights the tones much harder than
        # the best alternative. ``pref`` may have been filtered out by the
        # caller (e.g. a closing phrase may not use a narrow motif).
        if pref in patterns and cost(patterns[pref]) <= cost(patterns[ranked[0]]) + 1.0:
            return pref
    return ranked[0]


def apply(base_spec: dict, info: dict) -> dict:
    """Merge a gesture's overrides into a mode spec, without mutating it."""
    g = GESTURES[info["gesture"]]
    spec = dict(base_spec)
    spec["pattern"] = g["pattern"]
    spec["end_mode"] = g["end_mode"]
    spec["tonic"] = base_spec["tonic"] + g["register"]
    spec["lo"] = base_spec.get("lo", base_spec["tonic"]) + g["register"]
    spec["hi"] = base_spec.get("hi", base_spec["tonic"] + 12) + g["register"]
    spec["beat"] = round(base_spec["beat"] * g["beat_mul"], 3)
    spec["span"] = round(base_spec.get("span", 6.0) * g["span_mul"], 2)
    return spec


def describe(text: str) -> str:
    i = analyze(text)
    g = GESTURES[i["gesture"]]
    return (f"{i['chars']:12} gesture={i['gesture']:6} "
            f"pattern={g['pattern']:6} end={g['end_mode']:5} "
            f"reg={g['register']:+d}  ({i['reason']})")
