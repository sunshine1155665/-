"""大肥鱼间聊天的渴望阈值引擎——通用层的薄包装（T-0052 重构）。

算术、常量、状态形状全部住在 :mod:`fatfish.desire.engine`（与 ``self_action`` /
``owner_chat`` 共用同一份实现，靠 settings 对象和状态键区分域）。本文件只做
re-export，``runtime`` / ``service`` / 既有测试的 import 路径与符号名一个都不用
改，行为与 T-0046 完全一致。

为什么保留这个名字：``ai_chat`` 是第一个落地这套数学的域，调用方（``runtime``
里 ``ai_chat_engine.normalize_desire`` 等）已经稳定；通用化不该产生一次全仓改名。
新代码请直接 ``from fatfish.desire import engine``。
"""

from __future__ import annotations

from ..desire.engine import (
    AiChatState,
    DesireState,
    PASSIVE_BYPASSES_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_HARD_CAP,
    REASON_OK,
    can_proactive_send,
    current_threshold,
    describe,
    normalize_desire,
    on_sent,
    read_state,
    should_probe,
)

__all__ = [
    "AiChatState",
    "DesireState",
    "PASSIVE_BYPASSES_THRESHOLD",
    "REASON_BELOW_THRESHOLD",
    "REASON_HARD_CAP",
    "REASON_OK",
    "can_proactive_send",
    "current_threshold",
    "describe",
    "normalize_desire",
    "on_sent",
    "read_state",
    "should_probe",
]
