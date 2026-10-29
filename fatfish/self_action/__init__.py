"""自主动作域（T-0042 L4）。

与 ``fatfish.ai_chat`` 同构，但**物理隔离**：自己的配置段 ``[self_action]``、
自己的状态键 ``self_action:state:global``、自己的计数表 ``self_action_evidence``。
数学一行都不在这层：衰减/抬升/软限判定全部来自 :mod:`fatfish.desire.engine`。
"""

from .service import (
    AUDIT_KIND,
    REASON_DAILY_CAP,
    REASON_DISABLED,
    REASON_QUIET_HOURS,
    REASON_TOO_MANY_PER_TURN,
    SELF_ACTION_TOOLS,
    STATE_KEY,
    SelfActionService,
)

__all__ = [
    "AUDIT_KIND",
    "REASON_DAILY_CAP",
    "REASON_DISABLED",
    "REASON_QUIET_HOURS",
    "REASON_TOO_MANY_PER_TURN",
    "SELF_ACTION_TOOLS",
    "STATE_KEY",
    "SelfActionService",
]
