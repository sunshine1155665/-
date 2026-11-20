"""域无关的「渴望阈值」通用层（T-0052，父卡 T-0042）。

数学共用、状态独立：``settings`` 负责把某个 ``[<domain>]`` 段读成不可变配置，
``engine`` 负责同一套衰减/抬升/夹取/软限判定，``state_key`` 保证三个域的状态
键不会互相覆盖。

这里刻意不出现任何 ``if domain == ...`` 分支——域之间的差异全部来自传入的
settings 对象和状态键，所以新增一个域只需要加一段配置，不用改算术。
"""

from .engine import (
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
from .settings import (
    AI_CHAT_DEFAULTS,
    OWNER_CHAT_DEFAULTS,
    SELF_ACTION_DEFAULTS,
    DesireSettings,
    read_desire_settings,
)
from .state import state_key

__all__ = [
    "AI_CHAT_DEFAULTS",
    "AiChatState",
    "DesireSettings",
    "DesireState",
    "OWNER_CHAT_DEFAULTS",
    "PASSIVE_BYPASSES_THRESHOLD",
    "REASON_BELOW_THRESHOLD",
    "REASON_HARD_CAP",
    "REASON_OK",
    "SELF_ACTION_DEFAULTS",
    "can_proactive_send",
    "current_threshold",
    "describe",
    "normalize_desire",
    "on_sent",
    "read_desire_settings",
    "read_state",
    "should_probe",
    "state_key",
]
