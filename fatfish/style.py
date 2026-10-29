"""Deterministic emoji / kaomoji style control.

LLMs understand emoji semantics, but frequency and appropriateness are not
reliable. This layer enforces a small, configurable budget and strips emoji in
serious turns *after* generation, so the persona prompt doesn't have to be
perfect.

A budget of ``-1`` means "no cap" (the owner asked for normal emoji use rather
than a fixed count); ``0`` still means "strip them all", so the two are not
confused. Serious turns keep stripping regardless — that is a safety behaviour,
not a style preference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .config import Config

# Common emoji ranges; good enough for chat text.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002600-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\uFE0F"
    "]"
)

# Kaomoji markers. Chinese parenthetical actions such as （尾巴晃了一下） are
# deliberately not matched: they contain Chinese characters, not these marks.
KAOMOJI_MARKERS = set("๑•︿≧▽ωฅﾉ✧´｀∀Д°≖ᴗ◡ヽノヾ")
KAOMOJI_RE = re.compile(r"[（(][^（）()]{0,24}[)）]")

DEFAULT_BLACKLIST = {"😅", "🙏", "👍", "🙂", "😊", "🤝"}

SERIOUS_WORDS = (
    "难过", "崩溃", "想哭", "自杀", "自残", "生病", "住院", "去世", "死亡",
    "分手", "吵架", "冲突", "焦虑", "抑郁", "害怕", "求救", "危机", "认真点",
    "别闹", "停止", "不要开玩笑", "紧急",
)


@dataclass(slots=True)
class StyleConfig:
    emoji_mode: str = "light"  # off | light | medium
    max_emoji_per_message: int = 1
    max_kaomoji_per_message: int = 1
    allow_kaomoji: bool = True
    blacklist: set[str] | None = None


class StyleController:
    def __init__(self, config: Config):
        self.config = StyleConfig(
            emoji_mode=str(config.get("style.emoji_mode", "light")),
            max_emoji_per_message=int(config.get("style.max_emoji_per_message", 1)),
            max_kaomoji_per_message=int(config.get("style.max_kaomoji_per_message", 1)),
            allow_kaomoji=bool(config.get("style.allow_kaomoji", True)),
            blacklist=set(config.get("style.emoji_blacklist", DEFAULT_BLACKLIST)),
        )

    def polish(self, messages: Iterable[str], *, serious: bool = False) -> list[str]:
        result: list[str] = []
        for message in messages:
            text = str(message or "").strip()
            if not text:
                continue
            if serious or self.config.emoji_mode == "off":
                text = EMOJI_RE.sub("", text)
                text = self._remove_kaomoji(text)
            else:
                text = self._strip_blacklist(text)
                text = self._limit_emoji(text, self.config.max_emoji_per_message)
                if not self.config.allow_kaomoji:
                    text = self._remove_kaomoji(text)
                else:
                    text = self._limit_kaomoji(text, self.config.max_kaomoji_per_message)
            text = re.sub(r"[ \t]{2,}", " ", text).strip()
            if text:
                result.append(text)
        return result

    def _strip_blacklist(self, text: str) -> str:
        for emoji in self.config.blacklist or set():
            text = text.replace(emoji, "")
        return text

    @staticmethod
    def _limit_emoji(text: str, limit: int) -> str:
        if limit < 0:
            return text  # 负值 = 不限量（主人要"正常用"），0 = 全删
        if limit == 0:
            return EMOJI_RE.sub("", text)
        seen = 0
        out: list[str] = []
        for char in text:
            if EMOJI_RE.fullmatch(char):
                seen += 1
                if seen > limit:
                    continue
            out.append(char)
        return "".join(out)

    def _remove_kaomoji(self, text: str) -> str:
        return KAOMOJI_RE.sub(
            lambda m: "" if any(ch in KAOMOJI_MARKERS for ch in m.group(0)) else m.group(0),
            text,
        )

    def _limit_kaomoji(self, text: str, limit: int) -> str:
        if limit < 0:
            return text  # 同 emoji：负值不限量
        seen = 0

        def repl(match: re.Match[str]) -> str:
            nonlocal seen
            value = match.group(0)
            if not any(ch in KAOMOJI_MARKERS for ch in value):
                return value  # Chinese parenthetical action
            seen += 1
            return value if seen <= limit else ""

        return KAOMOJI_RE.sub(repl, text)


def is_serious(text: str) -> bool:
    return any(word in (text or "") for word in SERIOUS_WORDS)


def count_emoji(text: str) -> int:
    return len(EMOJI_RE.findall(text or ""))


def count_kaomoji(text: str) -> int:
    return sum(
        1
        for match in KAOMOJI_RE.findall(text or "")
        if any(ch in KAOMOJI_MARKERS for ch in match)
    )
