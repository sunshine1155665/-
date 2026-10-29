"""域无关的「渴望阈值」配置层（T-0052）。

三个域共用同一套数学（见 :mod:`fatfish.desire.engine`），但**各读各的配置段、
各写各的状态键**，互不影响：

=============  ==================  ====================================
域             配置段              衰减量的单位
=============  ==================  ====================================
``ai_chat``    ``[ai_chat]``       「条」消息（旧键继续用）
``self_action````[self_action]``    「次」动作
``owner_chat`` ``[owner_chat]``     占位段，本轮代码不接
=============  ==================  ====================================

字段为什么叫 ``decay_events_per_hour`` 而不是 ``decay_messages_per_hour``：
公式同形（``threshold_step * rate / 60``），量纲不同（一条消息 / 一次动作）。
[ai_chat] 段继续写旧键，所以读取时**新键优先、缺失才回落到旧键**，配置一个字
都不用改。

设计约束（与 ai_chat 的既有约定一致）：

* ``threshold_decay_per_minute`` 是**派生**量，永远不从配置读：
  ``threshold_step * decay_events_per_hour / 60``。
* ``hard_cap_per_hour`` 默认 0 = 完全不拦，只是止血开关；任何时候都没有
  「到数就拒发」的隐含分支。
* 非法值一律回落文档默认值并追加一条 ``warnings``，**绝不抛异常**——运行时
  必须能在配置写错的情况下照常启动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_ENABLED = False
DEFAULT_INITIAL_THRESHOLD = 0.30
DEFAULT_THRESHOLD_STEP = 0.12
DEFAULT_THRESHOLD_FLOOR = 0.15
DEFAULT_THRESHOLD_CEILING = 0.95
DEFAULT_DECAY_EVENTS_PER_HOUR = 50
DEFAULT_HARD_CAP_PER_HOUR = 0
DEFAULT_PROBE_INTERVAL_MINUTES = 10
DEFAULT_QUIET_HOURS: tuple[str, str] = ("01:00", "08:00")
DEFAULT_DAILY_CAP = 0  # 0 = 不设日上限
DEFAULT_MAX_EVENTS_PER_TURN = 1

# 「值不存在」与「值是 None」是两回事：前者用默认值且不报警，后者是写错了。
_MISSING: Any = object()

_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off", "")


@dataclass(frozen=True, slots=True)
class DesireSettings:
    """Immutable, validated view of one ``[<domain>]`` section.

    Every field already holds a legal value; whatever had to be fixed is listed
    in ``warnings`` so ``fatfish doctor`` / logs can surface it.
    """

    enabled: bool = DEFAULT_ENABLED
    initial_threshold: float = DEFAULT_INITIAL_THRESHOLD
    threshold_step: float = DEFAULT_THRESHOLD_STEP
    threshold_floor: float = DEFAULT_THRESHOLD_FLOOR
    threshold_ceiling: float = DEFAULT_THRESHOLD_CEILING
    decay_events_per_hour: int = DEFAULT_DECAY_EVENTS_PER_HOUR
    hard_cap_per_hour: int = DEFAULT_HARD_CAP_PER_HOUR
    probe_interval_minutes: int = DEFAULT_PROBE_INTERVAL_MINUTES
    quiet_hours: tuple[str, ...] = DEFAULT_QUIET_HOURS
    daily_cap: int = DEFAULT_DAILY_CAP
    max_events_per_turn: int = DEFAULT_MAX_EVENTS_PER_TURN
    warnings: tuple[str, ...] = ()
    # Derived, not configured -- see the module docstring.
    threshold_decay_per_minute: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "threshold_decay_per_minute",
            self.threshold_step * float(self.decay_events_per_hour) / 60.0,
        )

    @property
    def active(self) -> bool:
        """Generic domains have no second gate; a domain may override it.

        ``ai_chat`` does: it also needs a bound ``peer_uin``.  ``self_action``
        only needs ``enabled``.
        """
        return self.enabled

    @property
    def disabled_reason(self) -> str:
        return "" if self.enabled else "enabled=false"


# --------------------------------------------------------------- per-domain
#: ``[ai_chat]`` 的默认值（0.12 * 50 / 60 = 0.1/分钟，与 T-0041 完全一致）。
AI_CHAT_DEFAULTS = DesireSettings()

#: ``[self_action]`` 的默认值：0.10 * 30 / 60 = 0.05/分钟；单位是「次动作」。
#: ``enabled=True`` 是本域**刻意背离** ``DEFAULT_ENABLED=False`` 的一处：ai_chat
#: 那条关着是因为没绑 peer 就无从开口，而自主动作只要主人跟她说话就有场景；
#: 关掉它要显式写 ``enabled=false``。闸门本身（阈值/静默/日上限）一个都没松。
SELF_ACTION_DEFAULTS = DesireSettings(
    enabled=True,
    initial_threshold=0.35,
    threshold_step=0.10,
    threshold_floor=0.20,
    threshold_ceiling=0.90,
    decay_events_per_hour=30,
    hard_cap_per_hour=0,
    probe_interval_minutes=30,
    daily_cap=12,
    max_events_per_turn=2,
)

#: ``[owner_chat]`` 的占位默认值；本轮没有代码读它，只是把位置留好。
OWNER_CHAT_DEFAULTS = DesireSettings(
    initial_threshold=0.30,
    threshold_step=0.12,
    threshold_floor=0.15,
    threshold_ceiling=0.90,
    decay_events_per_hour=50,
    hard_cap_per_hour=0,
    probe_interval_minutes=30,
)


# --------------------------------------------------------------------- input
def lookup(config: Any, section: str, key: str) -> Any:
    """``config.get("<section>.<key>")`` or :data:`_MISSING` when absent.

    Tolerates a config object without ``get`` (tests pass bare objects): every
    lookup then reports "missing", which degrades to the defaults.
    """
    get = getattr(config, "get", None)
    if not callable(get):
        return _MISSING
    return get(f"{section}.{key}", _MISSING)


def check_section(config: Any, section: str, warnings: list[str]) -> None:
    """Warn once when ``[<section>]`` exists but is not a table."""
    get = getattr(config, "get", None)
    if not callable(get):
        return
    raw = get(section, _MISSING)
    if raw is _MISSING or raw is None:
        return
    if not isinstance(raw, dict):
        warnings.append(f"[{section}] 不是表（{raw!r}），全部按默认值处理")


def to_bool(raw: Any, default: bool, warnings: list[str], key: str = "enabled") -> bool:
    if raw is _MISSING:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    warnings.append(f"{key}={raw!r} 不是布尔值，按 {default} 处理")
    return default


def to_text(raw: Any, default: str, key: str, warnings: list[str]) -> str:
    if raw is _MISSING or raw is None:
        return default
    if isinstance(raw, bool):
        warnings.append(f"{key}={raw!r} 不是字符串，按 {default!r} 处理")
        return default
    if isinstance(raw, int):
        # A QQ uin typed without quotes is still a uin, not an error.
        return str(raw)
    if isinstance(raw, str):
        return raw.strip()
    warnings.append(f"{key}={raw!r} 不是字符串，按 {default!r} 处理")
    return default


def to_float(raw: Any, default: float, key: str, warnings: list[str]) -> float:
    """Finite float or ``default`` (never raises, never returns NaN/inf)."""
    if raw is _MISSING:
        return default
    if isinstance(raw, bool) or raw is None:
        if raw is not None:
            warnings.append(f"{key}={raw!r} 不是数字，按默认 {default} 处理")
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        warnings.append(f"{key}={raw!r} 不是数字，按默认 {default} 处理")
        return default
    if value != value or value in (float("inf"), float("-inf")):
        warnings.append(f"{key}={raw!r} 不是有限数，按默认 {default} 处理")
        return default
    return value


def to_int(
    raw: Any, default: int, key: str, *, minimum: int, warnings: list[str]
) -> int:
    """Integer >= ``minimum`` or ``default``; fractions are truncated."""
    if raw is _MISSING:
        return default
    if isinstance(raw, bool) or raw is None:
        if raw is not None:
            warnings.append(f"{key}={raw!r} 不是整数，按默认 {default} 处理")
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        warnings.append(f"{key}={raw!r} 不是整数，按默认 {default} 处理")
        return default
    if value < minimum:
        warnings.append(f"{key}={raw!r} 小于 {minimum}，按默认 {default} 处理")
        return default
    return value


def to_quiet_hours(
    raw: Any, fallback: tuple[str, ...], warnings: list[str], key: str = "quiet_hours"
) -> tuple[str, ...]:
    """``["HH:MM", "HH:MM"]`` (empty list = no quiet window) or ``fallback``."""
    if raw is _MISSING:
        return fallback
    if isinstance(raw, str):
        items: Iterable[Any] = [part for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        warnings.append(f"{key}={raw!r} 不是列表，按默认 {list(fallback)} 处理")
        return fallback
    out = [str(item).strip() for item in items]
    if not out:
        return ()
    if len(out) != 2 or not all(_CLOCK_RE.match(item) for item in out):
        warnings.append(
            f"{key}={raw!r} 不是两个 HH:MM，按默认 {list(fallback)} 处理"
        )
        return fallback
    return (out[0], out[1])


# ------------------------------------------------------------------ entry point
def read_desire_settings(
    config: Any, section: str, *, defaults: DesireSettings
) -> DesireSettings:
    """Build :class:`DesireSettings` for ``[<section>]``.

    Never raises: a missing section, a wrong type or an out-of-range number all
    degrade to ``defaults`` with a note in ``warnings``.  ``decay_events_per_hour``
    wins over the legacy ``decay_messages_per_hour``; when neither is present the
    default (50 for [ai_chat]) stands.
    """
    warnings: list[str] = []
    check_section(config, section, warnings)

    enabled = to_bool(
        lookup(config, section, "enabled"), defaults.enabled, warnings
    )

    ceiling = to_float(
        lookup(config, section, "threshold_ceiling"),
        defaults.threshold_ceiling,
        "threshold_ceiling",
        warnings,
    )
    if ceiling > 1.0:
        warnings.append(f"threshold_ceiling={ceiling} 超过 1.0，夹到 1.0")
        ceiling = 1.0
    elif ceiling <= 0.0:
        warnings.append(
            f"threshold_ceiling={ceiling} 不是正数，按默认 {defaults.threshold_ceiling} 处理"
        )
        ceiling = defaults.threshold_ceiling

    floor = to_float(
        lookup(config, section, "threshold_floor"),
        defaults.threshold_floor,
        "threshold_floor",
        warnings,
    )
    if floor < 0.0:
        warnings.append(f"threshold_floor={floor} 小于 0，夹到 0")
        floor = 0.0
    if floor >= ceiling:
        warnings.append(
            f"threshold_floor={floor} >= threshold_ceiling={ceiling}，"
            f"两者回落到默认 {defaults.threshold_floor}/{defaults.threshold_ceiling}"
        )
        floor, ceiling = defaults.threshold_floor, defaults.threshold_ceiling

    step = to_float(
        lookup(config, section, "threshold_step"),
        defaults.threshold_step,
        "threshold_step",
        warnings,
    )
    if step <= 0.0:
        warnings.append(
            f"threshold_step={step} 必须为正（做一次要抬阈值），按默认 {defaults.threshold_step} 处理"
        )
        step = defaults.threshold_step

    initial = to_float(
        lookup(config, section, "initial_threshold"),
        defaults.initial_threshold,
        "initial_threshold",
        warnings,
    )
    if initial < floor or initial > ceiling:
        clamped = min(max(initial, floor), ceiling)
        warnings.append(
            f"initial_threshold={initial} 越界，夹到 [{floor}, {ceiling}] -> {clamped}"
        )
        initial = clamped

    decay_key = "decay_events_per_hour"
    decay_raw = lookup(config, section, decay_key)
    if decay_raw is _MISSING:
        # 旧键回落：[ai_chat] 继续写 decay_messages_per_hour。
        decay_key = "decay_messages_per_hour"
        decay_raw = lookup(config, section, decay_key)
    decay_rate = to_int(
        decay_raw,
        defaults.decay_events_per_hour,
        decay_key,
        minimum=1,
        warnings=warnings,
    )

    hard_cap = to_int(
        lookup(config, section, "hard_cap_per_hour"),
        defaults.hard_cap_per_hour,
        "hard_cap_per_hour",
        minimum=0,
        warnings=warnings,
    )
    probe = to_int(
        lookup(config, section, "probe_interval_minutes"),
        defaults.probe_interval_minutes,
        "probe_interval_minutes",
        minimum=1,
        warnings=warnings,
    )
    quiet_hours = to_quiet_hours(
        lookup(config, section, "quiet_hours"), defaults.quiet_hours, warnings
    )
    daily_cap = to_int(
        lookup(config, section, "daily_cap"),
        defaults.daily_cap,
        "daily_cap",
        minimum=0,
        warnings=warnings,
    )
    per_turn = to_int(
        lookup(config, section, "max_events_per_turn"),
        defaults.max_events_per_turn,
        "max_events_per_turn",
        minimum=1,
        warnings=warnings,
    )

    return DesireSettings(
        enabled=enabled,
        initial_threshold=initial,
        threshold_step=step,
        threshold_floor=floor,
        threshold_ceiling=ceiling,
        decay_events_per_hour=decay_rate,
        hard_cap_per_hour=hard_cap,
        probe_interval_minutes=probe,
        quiet_hours=quiet_hours,
        daily_cap=daily_cap,
        max_events_per_turn=per_turn,
        warnings=tuple(warnings),
    )
