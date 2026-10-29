"""唤醒到点后的一次动作决策：聊天 / 刷 B 站 / 沉默。

背景：唤醒（闹钟）以前只有一条出口——发一句主动消息。于是「到点了」和
「想找人说话」被绑成一件事：既不能安静地去刷会儿视频，也不能什么都不做。
这里把「到点了」和「要做的事」拆开：过了 ``ProactiveGate`` 之后先花一次
极小的 LLM 调用做决策，再决定走哪条出口。

本模块只放纯函数（提示词 + 解析），不碰数据库、不碰网络，方便单测：

* :func:`build_prompt` —— 把上下文压成一段 ~300 token 的提示；
* :func:`parse_action` —— 从模型那几个字里认出 ``chat`` / ``browse`` /
  ``silent``，认不出返回 ``None``（调用方回退到老的模板路径）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ``decision_log.kind``：决策、以及三种动作各一条的审计口径。
WAKEUP_ACTION_KIND = "wakeup_action"

ACTION_CHAT = "chat"
ACTION_BROWSE = "browse"
ACTION_SILENT = "silent"
ACTIONS = (ACTION_CHAT, ACTION_BROWSE, ACTION_SILENT)

# 超时预算：唤醒决策跑在 2 秒一 tick 的主循环里，绝不能等到
# ``llm.timeout_seconds``（默认 90 秒）。8 秒足够一次短补全，超时就回退模板。
DEFAULT_DECIDE_TIMEOUT_SECONDS = 8.0
DEFAULT_DECIDE_MAX_TOKENS = 24
# 上下文里最多带几轮对话、每轮截多长——目的是「够判断」，不是「全量记忆」。
CONTEXT_MAX_TURNS = 4
CONTEXT_TURN_CHARS = 40

DECIDE_SYSTEM_PROMPT = (
    "你是「大肥鱼」本人。刚才是你自己定的闹钟到点了，现在做一个很小的决定："
    "这一次要不要找人说话。只回一个词，不要解释、不要标点。"
)

DECIDE_PROMPT_TEMPLATE = (
    "三个选项，只能选一个：\n"
    "chat=主动找主人哥哥说话；browse=不打扰他，自己去刷会儿B站；"
    "silent=什么都不做，就当没这回事。\n"
    "判断标准：\n"
    "1) 刚聊完、或者今天已经找过他好几次→browse 或 silent；\n"
    "2) 想他、隔了很久、或者真有话想说→chat；\n"
    "3) B站没得刷（见下）就不许选 browse，降级选 silent；\n"
    "4) 他在静默时段/深夜，或者最近一直很安静→silent。\n"
    "只回 chat / browse / silent 三者之一。\n"
    "\n【现在的情况】\n{context}"
)


def build_prompt(context: str) -> str:
    """The single user message: fixed rules + the compact situation block."""
    return DECIDE_PROMPT_TEMPLATE.format(context=(context or "").strip())


def _from_json(text: str) -> Any:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        return data.get("action") or data.get("decision")
    if isinstance(data, str):
        return data
    return None


def _earliest(text: str, patterns: tuple[tuple[str, str], ...]) -> str | None:
    """The candidate whose first match comes earliest in the reply.

    Position order (not pattern order) keeps a chatty answer like
    「不想说话，选 silent」 pointing at ``silent``: the later word is the one
    the sentence lands on. Ties resolve by pattern order, which is
    silent > browse > chat -- the conservative direction.
    """
    best: tuple[int, str] | None = None
    for action, pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        if best is None or match.start() < best[0]:
            best = (match.start(), action)
    return best[1] if best else None


def parse_action(text: str) -> str | None:
    """``chat`` / ``browse`` / ``silent``, or ``None`` when unreadable.

    Accepts the bare word, a JSON envelope (``{"action": "browse"}``) and the
    obvious Chinese synonyms -- a model that answers 「刷视频」 instead of
    ``browse`` should not cost her the decision.
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 160:
        # A one-word answer is expected; a long rambling reply is not a
        # decision, and guessing from it is how a message gets swallowed.
        return None
    lowered = raw.lower()
    if lowered.startswith("{") or lowered.startswith('"'):
        value = _from_json(raw)
        if isinstance(value, str):
            lowered = value.strip().lower()
    if not lowered:
        return None
    found = _earliest(
        lowered,
        (
            (ACTION_SILENT, r"\bsilent\b|沉默|不说话|不想说话|不想聊|什么都不做|不动"),
            (ACTION_BROWSE, r"\bbrowse\b|刷b站|刷视频|刷会|看视频|逛b站"),
            (ACTION_CHAT, r"\bchat\b|聊天|说话|找他|喊他"),
        ),
    )
    if found is not None:
        return found
    # 单个英文词后面可能跟了句号/中文标点，``\b`` 已经覆盖；再兜一层前缀匹配。
    head = lowered[:16]
    for action in ACTIONS:
        if head.startswith(action):
            return action
    return None
