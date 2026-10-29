"""Lightweight, rule-based phrase miner for the expression library.

No LLM is involved.  The miner only proposes ``candidate`` entries; approval
is always a human action, so mined text can never reach a prompt on its own.
"""

from __future__ import annotations

import re
import time

from .service import LexiconService

MAX_CANDIDATES = 30
MAX_TURN_CHARS = 500
MAX_RUN_CHARS = 240

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_CQ = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_AT = re.compile(r"@\S+")
_HTML = re.compile(r"<[^>]{1,120}>")
_DIGITS = re.compile(r"\d+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_HAN_ANY = re.compile(r"[\u4e00-\u9fff]")

_QUOTED = re.compile(r"[“\"「『]([^”\"」』\n]{2,12})[”\"」』]")
_REPEATED = re.compile(r"([\u4e00-\u9fff]{2,8})\1{2,}")
_WEAK_PATTERNS = (
    re.compile(r"太[\u4e00-\u9fff]{1,4}了"),
    re.compile(r"[\u4e00-\u9fff]{1,4}死了"),
    re.compile(r"好[\u4e00-\u9fff]{1,4}"),
    re.compile(r"[\u4e00-\u9fff]{1,4}到"),
)

# Function words, pronouns and very common verbs.  Phrases made only of these
# carry no catchphrase value; the list is intentionally conservative so real
# slang such as "好耶" or "绷不住" survives.
_STOPWORDS = frozenset(
    """
    我们 你们 他们 她们 它们 咱们 大家 自己 别人 人家 我的 你的 他的 她的
    这个 那个 这些 那些 这样 那样 这么 那么 什么 怎么 为什么 哪个 哪里
    这里 那里 一下 一些 一点 有点 很多 好多 非常 特别 已经 还是 就是
    不是 可以 可能 应该 需要 觉得 感觉 知道 看到 听到 想到 做到 得到
    遇到 达到 迟到 来到 回到 收到 等到 用到 不到 起来 出来 进来 下来
    上来 过去 过来 开始 结束 继续 然后 但是 因为 所以 如果 虽然 不过
    而且 或者 还有 没有 不会 不能 不要 不用 不是 干嘛 怎样 怎么样
    今天 明天 昨天 现在 以后 以前 时候 时间 东西 事情 问题 地方 朋友
    老师 工作 学习 生活 世界 真的 确实 好像 好的 好吧 行吧 是的 对的
    来吧 去了 来了 说吧 你说 我说 他是 她是 我是 你是 在吗 谢谢 拜拜
    对不起 没关系 你好 早安 晚安 早上 晚上 中午 中午好 早上好 晚上好
    哈哈 哈哈哈 哈哈哈哈 呵呵 嘿嘿 嘻嘻 嗯嗯 哦哦 啊啊 哎呀 哎哟 唉
    一个 一直 一定 一样 一起 一般 一边 一次 一天 一年 一点 有点 有些
    的话 之后 之前 以及 以为 认为 希望 打算 准备 差不多 没办法 什么样
    这种 那种 哪种 这么 那么 多么 多少 几个 第一 第二 最后 其他 另外
    天气 好吃 好玩 好看 好听 回事 意思 办法 感觉 心情 眼睛 嘴巴 手里
    """.split()
)

# A phrase made only of these characters (or starting with a common pronoun
# / conjunction) is grammar, not slang.
_FUNCTION_CHARS = frozenset(
    "我们你们他她它们这那什么怎么哪哪里个的了着过吗呢啊吧呀哦嗯是在有就不都很太也还又才要会能可被把给对从向于与及或但而且所以因为如果虽然然后已经还是就是没有可以一个一下一些有点真的觉得知道看到听到想到时候现在今天明天昨天"
)
_PREFIX_STOP = (
    "我们", "你们", "他们", "她们", "它们", "自己", "这个", "那个",
    "什么", "怎么", "因为", "所以", "但是", "如果", "然后", "已经",
    "还是", "就是", "不是",
)


def mine(
    lexicon: LexiconService,
    *,
    days: int = 14,
    limit_turns: int = 800,
    min_count: int = 3,
) -> dict[str, int]:
    """Scan recent conversation turns and propose new ``jargon`` candidates."""
    days = max(1, min(365, int(days)))
    limit_turns = max(1, min(2000, int(limit_turns)))
    min_count = max(1, min(50, int(min_count)))

    cutoff = time.time() - days * 86400
    rows = lexicon.db.query(
        "SELECT role, content FROM conversation_turns "
        "WHERE role IN ('user', 'assistant') AND ephemeral=0 "
        "AND created_at>=? AND length(content) BETWEEN 2 AND ? "
        "ORDER BY id DESC LIMIT ?",
        (cutoff, MAX_TURN_CHARS, limit_turns),
    )
    texts: list[str] = []
    for row in rows:
        cleaned = _clean_turn(str(row["content"] or ""))
        if cleaned:
            texts.append(cleaned)

    ngrams: dict[str, int] = {}
    strong_hits: dict[str, int] = {}
    weak_hits: dict[str, int] = {}
    for text in texts:
        _count_ngrams(text, ngrams)
        for match in _QUOTED.finditer(text):
            phrase = _han_only(match.group(1))
            if _valid_phrase(phrase):
                strong_hits[phrase] = strong_hits.get(phrase, 0) + 1
        for match in _REPEATED.finditer(text):
            phrase = match.group(1)
            if _valid_phrase(phrase):
                strong_hits[phrase] = strong_hits.get(phrase, 0) + 1
        for pattern in _WEAK_PATTERNS:
            for match in pattern.finditer(text):
                phrase = _han_only(match.group(0))
                if _valid_phrase(phrase):
                    weak_hits[phrase] = weak_hits.get(phrase, 0) + 1

    proposals: dict[str, int] = {}
    for phrase, count in ngrams.items():
        if count >= min_count and _valid_phrase(phrase):
            proposals[phrase] = max(proposals.get(phrase, 0), count)
    pattern_phrases: set[str] = set()
    for phrase, count in strong_hits.items():
        if _valid_phrase(phrase):
            proposals[phrase] = max(proposals.get(phrase, 0), count)
            pattern_phrases.add(phrase)
    for phrase, count in weak_hits.items():
        if not _valid_phrase(phrase):
            continue
        # A single short "好X" is usually grammar; a one-off longer pattern
        # such as "太好玩了" is still a useful review candidate.
        if count >= 2 or len(phrase) >= 3:
            proposals[phrase] = max(proposals.get(phrase, 0), count)
            pattern_phrases.add(phrase)

    pool = _prune_subphrases(proposals, protected=pattern_phrases)
    pool.sort(key=lambda phrase: (-proposals[phrase], -len(phrase), phrase))
    candidates = len(pool)

    existing = {
        str(row["content"])
        for row in lexicon.db.query(
            "SELECT content FROM lexicon_entries WHERE kind='jargon'"
        )
    }
    fresh = [phrase for phrase in pool if phrase not in existing]
    duplicates = candidates - len(fresh)
    capped = fresh[:MAX_CANDIDATES]
    truncated = max(0, len(fresh) - len(capped))

    added = 0
    for phrase in capped:
        count = proposals[phrase]
        snippets = _snippets_for(texts, phrase, limit=2)
        lexicon.add(
            "jargon",
            phrase,
            meaning="",
            situation=snippets[0] if snippets else "",
            style="",
            tags=[],
            examples=snippets,
            status="candidate",
            source="miner",
            confidence=round(min(0.9, 0.3 + count / 20.0), 3),
        )
        added += 1

    skipped = duplicates + truncated
    return {
        "scanned": len(rows),
        "candidates": candidates,
        "added": added,
        "skipped": skipped,
        "duplicates": duplicates,
        "truncated": truncated,
    }


def _clean_turn(text: str) -> str:
    cleaned = _URL.sub(" ", text)
    cleaned = _CQ.sub(" ", cleaned)
    cleaned = _EMAIL.sub(" ", cleaned)
    cleaned = _AT.sub(" ", cleaned)
    cleaned = _HTML.sub(" ", cleaned)
    cleaned = _DIGITS.sub(" ", cleaned)
    cleaned = _CONTROL.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _count_ngrams(text: str, counts: dict[str, int]) -> None:
    for run in _HAN_RUN.findall(text):
        run = run[:MAX_RUN_CHARS]
        length = len(run)
        if length < 2:
            continue
        for size in range(2, 9):
            if size > length:
                break
            for start in range(0, length - size + 1):
                phrase = run[start : start + size]
                if phrase in _STOPWORDS or len(set(phrase)) < 2:
                    continue
                counts[phrase] = counts.get(phrase, 0) + 1


def _han_only(text: str) -> str:
    return "".join(_HAN_ANY.findall(text or ""))


def _valid_phrase(phrase: str) -> bool:
    if not 2 <= len(phrase) <= 8:
        return False
    if phrase in _STOPWORDS:
        return False
    if len(set(phrase)) < 2:
        return False
    if all(char in _FUNCTION_CHARS for char in phrase):
        return False
    if len(phrase) <= 4 and phrase.startswith(_PREFIX_STOP):
        return False
    return all(_HAN_ANY.fullmatch(char) for char in phrase)


def _prune_subphrases(
    proposals: dict[str, int], *, protected: set[str] | None = None
) -> list[str]:
    """Drop fragments such as "绷不" when "绷不住" has the same frequency."""
    protected = protected or set()
    ordered = sorted(proposals, key=lambda phrase: (-len(phrase), -proposals[phrase], phrase))
    kept: list[str] = []
    by_count: dict[int, list[str]] = {}
    for phrase in ordered:
        count = proposals[phrase]
        if phrase not in protected and any(
            phrase in longer for longer in by_count.get(count, ())
        ):
            continue
        kept.append(phrase)
        by_count.setdefault(count, []).append(phrase)
    return kept


def _snippets_for(texts: list[str], phrase: str, *, limit: int = 2) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    for text in texts:
        index = text.find(phrase)
        if index < 0:
            continue
        snippet = _snippet(text, index, len(phrase))
        if not snippet or snippet in seen:
            continue
        seen.add(snippet)
        snippets.append(snippet)
        if len(snippets) >= limit:
            break
    return snippets


def _snippet(text: str, index: int, length: int, *, radius: int = 18, limit: int = 60) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + length + radius)
    chunk = text[start:end].strip(" ，。！？、；：,.!?;:…")
    chunk = re.sub(r"\s+", " ", chunk).strip()
    if start > 0:
        chunk = "…" + chunk
    if end < len(text):
        chunk = chunk + "…"
    return chunk[:limit].strip()
