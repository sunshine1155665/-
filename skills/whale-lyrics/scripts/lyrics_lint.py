#!/usr/bin/env python3
"""Lint Chinese short-song lyrics for structure, coherence, rhyme and tone risk.

Usage:
  python lyrics_lint.py lyrics.txt
  python lyrics_lint.py lyrics.txt --json > lint_report.json

Only stdlib is required. If g2pM is installed, rhyme/tone checks are enabled;
otherwise they are reported as skipped warnings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

CJK = re.compile(r"[\u4e00-\u9fff]")
PARTICLES = set("呀啦吧呢哦嘛啊么")
PRONOUNS = set("我你他她它咱这那")
STOP = set("的了着过是在和与就又也都很太不没一一个这那哪什么怎样吗呢吧啊呀啦哦嘛么把被给让向从对为以于而且但所以因为如果就还再才只")
ABSTRACT = set("开心快乐幸福温暖温柔美好美丽孤单寂寞想念思念爱情永远一直感觉心情情绪")

# final -> 十三辙
RHYME = {
    "a": "发花", "ia": "发花", "ua": "发花",
    "o": "梭波", "e": "梭波", "uo": "梭波",
    "ie": "乜斜", "üe": "乜斜",
    "u": "姑苏",
    "i": "一七", "ü": "一七", "er": "一七",
    "ai": "怀来", "uai": "怀来",
    "ei": "灰堆", "ui": "灰堆",
    "ao": "遥条", "iao": "遥条",
    "ou": "由求", "iu": "由求",
    "an": "言前", "ian": "言前", "uan": "言前", "üan": "言前",
    "en": "人辰", "in": "人辰", "un": "人辰", "ün": "人辰",
    "ang": "江阳", "iang": "江阳", "uang": "江阳",
    "eng": "中东", "ing": "中东", "ong": "中东", "iong": "中东", "ueng": "中东",
}
INITIALS = sorted(
    ["zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
     "j", "q", "x", "r", "z", "c", "s", "y", "w"],
    key=len, reverse=True,
)


def cjk_chars(text: str) -> list[str]:
    return CJK.findall(text)


def read_lines(path: str) -> list[str]:
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("["):
            continue
        s = "".join(cjk_chars(s))
        if s:
            lines.append(s)
    return lines


def load_g2p():
    try:
        from g2pM import G2pM
        return G2pM()
    except Exception:
        return None


def pinyin_of(g2p, chars: list[str]) -> list[str]:
    if g2p is None:
        return []
    try:
        return list(g2p("".join(chars), tone=True, char_split=True))
    except Exception:
        return []


def split_final(py: str) -> tuple[str, int]:
    """'liang2' -> ('iang', 2); 'u:4' -> ('ü', 4)."""
    m = re.match(r"^([a-zü:]+?)([1-5])$", py or "")
    if not m:
        return "", 0
    body, tone = m.group(1), int(m.group(2))
    body = body.replace("u:", "ü").replace("v", "ü")
    for ini in INITIALS:
        if body.startswith(ini) and len(body) > len(ini):
            return body[len(ini):], tone
    return body, tone


def rhyme_family(final: str) -> str:
    return RHYME.get(final, "")


def lint(lines: list[str]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    info: dict = {}

    n = len(lines)
    lengths = [len(x) for x in lines]
    info["lines"] = n
    info["lengths"] = lengths

    # structure
    if n < 3 or n > 6:
        errors.append(f"句数 {n} 不在 3–6 句")
    bad_len = [(i + 1, L) for i, L in enumerate(lengths) if L < 5 or L > 11]
    if bad_len:
        errors.append(f"句长越界（5–11 字）：{bad_len}")
    same3 = [i + 1 for i in range(n - 2) if lengths[i] == lengths[i + 1] == lengths[i + 2]]
    if same3:
        warnings.append(f"连续 3 句同字数（4×9 常见，允许但注意长度曲线）：{same3}")
    if len(set(lengths)) == 1 and n >= 3:
        warnings.append("所有句长相同，长度曲线太平")

    # duplicate / endings
    if len(set(lines)) != n:
        errors.append("有完全重复的句子")
    for i in range(n - 1):
        if lines[i][-1] == lines[i + 1][-1]:
            warnings.append(f"第{i+1}/{i+2}句同字收尾：{lines[i][-1]}")

    # particles
    total_particles = sum(1 for ln in lines for c in ln if c in PARTICLES)
    per_line = [sum(1 for c in ln if c in PARTICLES) for ln in lines]
    info["particles"] = per_line
    if total_particles > 2:
        warnings.append(f"全歌语气词 {total_particles} 个 > 2")
    for i, k in enumerate(per_line, 1):
        if k >= 2:
            warnings.append(f"第{i}句语气词 {k} 个 ≥ 2")
    if n and all(k >= 1 for k in per_line):
        warnings.append("每句都带语气词，容易念经")

    # g2pM: rhyme + tone
    g2p = load_g2p()
    if g2p is None:
        warnings.append("g2pM 不可用：押韵/声调检查已跳过")
        info["rhyme"] = []
        info["tones"] = []
    else:
        py_lines = [pinyin_of(g2p, list(ln)) for ln in lines]
        finals = []
        tones = []
        for py in py_lines:
            fs, ts = [], []
            for p in py:
                f, t = split_final(p)
                fs.append(f)
                ts.append(t)
            finals.append(fs)
            tones.append(ts)
        info["finals"] = finals
        info["tones"] = tones

        # 2/4 rhyme (or last two lines if 3 lines)
        i2 = 1 if n >= 2 else 0
        i4 = 3 if n >= 4 else n - 1
        if finals[i2] and finals[i4]:
            f2, f4 = finals[i2][-1], finals[i4][-1]
            fam2, fam4 = rhyme_family(f2), rhyme_family(f4)
            info["rhyme"] = {"line2": f2, "line4": f4, "family2": fam2, "family4": fam4}
            if not fam2 or not fam4:
                warnings.append("2/4 句末韵母无法归辙，需人审")
            elif fam2 != fam4:
                warnings.append(f"2/4 句不押韵：{fam2} vs {fam4}")
            if lines[i2][-1] == lines[i4][-1]:
                warnings.append("2/4 句用同一个字押韵")
            if lines[i2][-1] in PARTICLES or lines[i4][-1] in PARTICLES:
                warnings.append("语气词被用作韵脚（短歌可接受，但记忆点弱）")

        # tone runs
        flat = [t for ts in tones for t in ts if t]
        run, prev = 1, None
        max_run, max_tone = 0, None
        run1 = 0
        for t in flat:
            if t == prev:
                run += 1
            else:
                run = 1
            if run > max_run:
                max_run, max_tone = run, t
            if t == 1:
                run1 = run1 + 1 if prev == 1 else 1
                if run1 >= 4:
                    errors.append("连续 ≥4 个一声，旋律容易被逼成同音摆动")
            prev = t
        info["max_tone_run"] = [max_tone, max_run]
        if max_run >= 4 and max_tone != 1:
            warnings.append(f"连续 {max_run} 个同调（tone {max_tone}），注意旋律")
        if any(all(t == 3 for t in ts[i:i+3]) for ts in tones for i in range(len(ts)-2)):
            warnings.append("三声连读 ≥3（普通话常见，仅提示）")

    # coherence: adjacent content overlap / pronoun / zero subject
    def content(ln: str) -> set[str]:
        return {c for c in ln if c not in STOP and c not in PARTICLES}

    links = 0
    for i in range(n - 1):
        a, b = content(lines[i]), content(lines[i + 1])
        if a & b:
            links += 1
        elif any(c in PRONOUNS for c in lines[i + 1][:2]):
            links += 1
        else:
            warnings.append(f"第{i+1}→{i+2}句缺少明显链接（无重叠/代词）")
    info["link_rate"] = round(links / max(1, n - 1), 2)
    if n >= 3 and links == 0:
        errors.append("相邻句完全没有内容链接")

    # last line new entity (rough): chars never seen before, excluding stop/particle
    seen = set("".join(lines[:-1]))
    new_last = [c for c in lines[-1] if c not in seen and c not in STOP and c not in PARTICLES]
    info["new_last_chars"] = new_last
    if len(new_last) >= 2:
        warnings.append(f"第4句引入 {len(new_last)} 个新字：{''.join(new_last)}")

    # AI tells
    abstract = sum(1 for ln in lines for c in ln if c in ABSTRACT)
    if abstract >= 2:
        warnings.append(f"抽象情绪字 {abstract} 个，建议换成具体动作/物件")
    if total_particles > 0 and total_particles == n:
        warnings.append("每句一个语气词，模板感强")

    return {
        "lines": lines,
        "lengths": lengths,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "ok": not errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lyrics", help="lyrics txt (one line per lyric, '-' for stdin)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    lines = read_lines(args.lyrics)
    if not lines:
        print("no lyric lines found", file=sys.stderr)
        return 2
    r = lint(lines)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        for i, (ln, L) in enumerate(zip(lines, r["lengths"]), 1):
            print(f"{i}. {ln}  ({L} 字)")
        print()
        for e in r["errors"]:
            print("ERROR", e)
        for w in r["warnings"]:
            print("WARN ", w)
        print("OK" if r["ok"] else "FAIL")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
