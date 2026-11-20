"""``[ai_chat]`` settings（T-0041）——通用渴望配置层的薄包装（T-0052）。

本文件现在只保留 ``[ai_chat]`` **独有**的东西：``peer_uin`` / ``peer_name``
以及"两个条件都满足才算启用"的 ``active`` 判定。阈值字段的读取、默认值、非法值
回落与派生量全部委托 :mod:`fatfish.desire.settings`，所以三个域不会各写一套
校验逻辑而慢慢跑偏。

外层契约（L2/L3 与既有测试依赖）一个字都没变：

* ``read_settings(config)`` 永远返回合法、不可变的 :class:`AiChatSettings`，
  坏输入夹取/回落 + 进 ``warnings``，不抛异常；
* ``threshold_decay_per_minute`` 是派生量 ``threshold_step *
  decay_messages_per_hour / 60``（0.12 * 50 / 60 = 0.1），不接受手写；
* ``hard_cap_per_hour`` 默认 0 = 完全没硬拦（止血开关，不是限流设计）；
* ``active`` 是唯一该被判定的标志：``enabled=true`` 但 ``peer_uin`` 为空仍然关，
  ``disabled_reason`` 说明原因。

``decay_messages_per_hour`` 这个字段名同样是契约的一部分（[ai_chat] 段还这么写、
``tests/test_ai_chat_engine.py`` 用 ``dataclasses.replace`` 改它），所以它在这里
保留旧名；内部映射到通用层的 ``decay_events_per_hour``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..desire.settings import (
    AI_CHAT_DEFAULTS,
    read_desire_settings,
    to_text,
)
from ..desire.settings import lookup as _lookup

SECTION = "ai_chat"

DEFAULT_ENABLED = AI_CHAT_DEFAULTS.enabled
DEFAULT_PEER_UIN = ""
DEFAULT_PEER_NAME = "大肥鱼"
DEFAULT_INITIAL_THRESHOLD = AI_CHAT_DEFAULTS.initial_threshold
DEFAULT_THRESHOLD_STEP = AI_CHAT_DEFAULTS.threshold_step
DEFAULT_THRESHOLD_FLOOR = AI_CHAT_DEFAULTS.threshold_floor
DEFAULT_THRESHOLD_CEILING = AI_CHAT_DEFAULTS.threshold_ceiling
DEFAULT_DECAY_MESSAGES_PER_HOUR = AI_CHAT_DEFAULTS.decay_events_per_hour
DEFAULT_HARD_CAP_PER_HOUR = AI_CHAT_DEFAULTS.hard_cap_per_hour
DEFAULT_PROBE_INTERVAL_MINUTES = AI_CHAT_DEFAULTS.probe_interval_minutes
DEFAULT_QUIET_HOURS = AI_CHAT_DEFAULTS.quiet_hours

# Config keys this feature owns, in file order; the three profiles must carry
# exactly this set (tests/test_ai_chat_config.py pins it).  Note the legacy
# decay key: the generic layer accepts it as the fallback name.
SETTING_KEYS: tuple[str, ...] = (
    "enabled",
    "peer_uin",
    "peer_name",
    "initial_threshold",
    "threshold_step",
    "threshold_floor",
    "threshold_ceiling",
    "decay_messages_per_hour",
    "hard_cap_per_hour",
    "probe_interval_minutes",
    "quiet_hours",
)


@dataclass(frozen=True, slots=True)
class AiChatSettings:
    """Immutable, validated view of ``[ai_chat]``.

    Every field already holds a legal value: out-of-range input is clamped or
    reset to the documented default, and whatever had to be fixed is listed in
    ``warnings`` so ``fatfish doctor`` / logs can surface it.
    """

    enabled: bool = DEFAULT_ENABLED
    peer_uin: str = DEFAULT_PEER_UIN
    peer_name: str = DEFAULT_PEER_NAME
    initial_threshold: float = DEFAULT_INITIAL_THRESHOLD
    threshold_step: float = DEFAULT_THRESHOLD_STEP
    threshold_floor: float = DEFAULT_THRESHOLD_FLOOR
    threshold_ceiling: float = DEFAULT_THRESHOLD_CEILING
    decay_messages_per_hour: int = DEFAULT_DECAY_MESSAGES_PER_HOUR
    hard_cap_per_hour: int = DEFAULT_HARD_CAP_PER_HOUR
    probe_interval_minutes: int = DEFAULT_PROBE_INTERVAL_MINUTES
    quiet_hours: tuple[str, ...] = DEFAULT_QUIET_HOURS
    warnings: tuple[str, ...] = ()
    # Derived, not configured -- see the module docstring.
    threshold_decay_per_minute: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "threshold_decay_per_minute",
            self.threshold_step * float(self.decay_messages_per_hour) / 60.0,
        )

    @property
    def active(self) -> bool:
        """True only when the feature may touch QQ at all."""
        return self.enabled and bool(self.peer_uin)

    @property
    def disabled_reason(self) -> str:
        """``""`` when active; otherwise the first reason that turned it off."""
        if not self.enabled:
            return "enabled=false"
        if not self.peer_uin:
            return "peer_uin_empty"
        return ""


# ------------------------------------------------------------------ entry point
def read_settings(config: Any) -> AiChatSettings:
    """Build :class:`AiChatSettings` from a :class:`fatfish.config.Config`.

    Never raises: a missing section, a wrong type or an out-of-range number
    all degrade to the documented default with a note in ``warnings``.  The
    runtime must be able to start with a broken profile.
    """
    desire = read_desire_settings(config, SECTION, defaults=AI_CHAT_DEFAULTS)
    warnings = list(desire.warnings)

    peer_uin = to_text(
        _lookup(config, SECTION, "peer_uin"), DEFAULT_PEER_UIN, "peer_uin", warnings
    )
    peer_name = to_text(
        _lookup(config, SECTION, "peer_name"),
        DEFAULT_PEER_NAME,
        "peer_name",
        warnings,
    )
    if not peer_name:
        peer_name = DEFAULT_PEER_NAME

    return AiChatSettings(
        enabled=desire.enabled,
        peer_uin=peer_uin,
        peer_name=peer_name,
        initial_threshold=desire.initial_threshold,
        threshold_step=desire.threshold_step,
        threshold_floor=desire.threshold_floor,
        threshold_ceiling=desire.threshold_ceiling,
        decay_messages_per_hour=desire.decay_events_per_hour,
        hard_cap_per_hour=desire.hard_cap_per_hour,
        probe_interval_minutes=desire.probe_interval_minutes,
        quiet_hours=desire.quiet_hours,
        warnings=tuple(warnings),
    )
