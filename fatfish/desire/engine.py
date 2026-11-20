"""渴望阈值引擎（T-0052）：decay / lift / clamp / gate，域无关的纯函数。

从 ``fatfish.ai_chat.engine``（T-0046）平移而来，语义一字未改。三个域
（``ai_chat`` / ``self_action`` / ``owner_chat``）共用这一份算术，靠**传入的
settings 对象**区分参数、靠**各自的 state 键**区分状态——这里没有任何按域名的
条件分支，也不该有。

本模块只有算术：没有 db、没有网络、没有读写配置、没有时钟。``now`` 一律由
调用方传入，每个函数都是全函数（任何 state 形状、任何垃圾 desire 都不抛），
所以运行时可以在一个回合里直接调用，不用 try/except。

数值（见 :mod:`fatfish.desire.settings`）：

* 衰减速率 ``threshold_decay_per_minute = threshold_step * decay_events_per_hour / 60``
  （[ai_chat] 0.12 * 50 / 60 = 0.1/分钟；[self_action] 0.10 * 30 / 60 = 0.05/分钟）。
  ``decay_events_per_hour`` 是**衰减基准**（「沉默一小时抹掉多少次动作的抬升
  量」），**永远不是**动作次数上限；代码里刻意不存在「这一小时做满 30 次就拒绝」
  的分支。
* 抬升 ``threshold = min(ceiling, decayed_threshold + threshold_step)``。
* 夹取 ``threshold_floor <= threshold <= threshold_ceiling``。

状态形状（落库为 ``schema_meta["<domain>:state:<ident>"]`` 的 JSON）：:

    {"threshold": 0.30, "updated_at": 1712345678.0, "last_desire": 0.42}

:class:`DesireState` 是它的类型化形式（``AiChatState`` 是兼容别名）；
:func:`read_state` 接受它、普通 mapping、或任何带这些属性的对象，缺失/乱码字段
静默当作 ``updated_at=None`` / ``last_desire=None``。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .settings import DEFAULT_INITIAL_THRESHOLD

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

# 被动回应（对方先动手、我们接话）**不受**阈值闸门约束：拿到对方消息后直接走
# 回应路径，不要调用 can_proactive_send。被动回应仍然计数、仍然抬阈值（on_sent），
# 所以"回得越多越难主动开口"依然成立。
PASSIVE_BYPASSES_THRESHOLD = True

REASON_OK = "ok"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_HARD_CAP = "hard_cap"

DESIRE_MIN = 0.0
DESIRE_MAX = 1.0


class Thresholds(Protocol):
    """What the engine needs from a settings object (duck typing, no import).

    :class:`fatfish.desire.settings.DesireSettings` and the ``ai_chat`` wrapper
    both satisfy it structurally; nothing here mentions a concrete domain type.
    """

    initial_threshold: float
    threshold_step: float
    threshold_floor: float
    threshold_ceiling: float
    threshold_decay_per_minute: float
    hard_cap_per_hour: int
    probe_interval_minutes: int


@dataclass(frozen=True, slots=True)
class DesireState:
    """Persisted desire-threshold state for one domain instance.

    ``threshold`` is stored exactly as written (decay is applied on read), so a
    reload never loses the ceiling.  ``updated_at`` is epoch seconds or ``None``
    (never acted / unknown), ``last_desire`` is the normalized desire of the last
    event or ``None``.
    """

    threshold: float
    updated_at: float | None = None
    last_desire: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, for ``schema_meta`` round-trips."""
        return {
            "threshold": self.threshold,
            "updated_at": self.updated_at,
            "last_desire": self.last_desire,
        }


#: Backwards-compatible name for ``[ai_chat]`` callers (same class, not a copy).
AiChatState = DesireState


# ----------------------------------------------------------------- primitives
def _finite_float(raw: Any) -> float | None:
    """Finite float, else ``None``.  ``bool`` is not a number here."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _seconds(raw: Any) -> float | None:
    """Epoch seconds, or ``None`` when the value cannot be trusted."""
    return _finite_float(raw)


def _count(raw: Any) -> int:
    """Event count for the optional hard cap; garbage counts as 0."""
    value = _finite_float(raw)
    if value is None or value <= 0.0:
        return 0
    return int(value)


def _field(state: Any, key: str) -> Any:
    """Read one state field from a mapping / dataclass / object, tolerantly."""
    if state is None:
        return None
    if isinstance(state, Mapping):
        try:
            return state.get(key)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
    try:
        return getattr(state, key, None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def read_state(state: Any, settings: Thresholds | None = None) -> DesireState:
    """Coerce anything state-shaped into :class:`DesireState`.

    Never raises.  A missing or non-numeric ``threshold`` falls back to
    ``settings.initial_threshold`` (or the package default without settings);
    a garbled ``updated_at`` / ``last_desire`` becomes ``None``, which means
    "no decay yet" / "no previous desire".
    """
    fallback = (
        settings.initial_threshold
        if settings is not None
        else DEFAULT_INITIAL_THRESHOLD
    )
    threshold = _finite_float(_field(state, "threshold"))
    if threshold is None:
        threshold = fallback
    last_desire = _finite_float(_field(state, "last_desire"))
    if last_desire is not None:
        last_desire = min(max(last_desire, DESIRE_MIN), DESIRE_MAX)
    return DesireState(
        threshold=threshold,
        updated_at=_seconds(_field(state, "updated_at")),
        last_desire=last_desire,
    )


def _clamp(value: float, floor: float, ceiling: float) -> float:
    return min(max(value, floor), ceiling)


def _elapsed_minutes(updated_at: Any, now: Any) -> float:
    """Minutes since ``updated_at``; 0.0 when unknown or the clock went back."""
    start = _seconds(updated_at)
    end = _seconds(now)
    if start is None or end is None:
        return 0.0
    delta = end - start
    if delta <= 0.0:
        return 0.0
    return delta / 60.0


# --------------------------------------------------------------------- public
def normalize_desire(raw: Any) -> float:
    """Desire in ``[0, 1]``; anything unusable means "not interested" (0.0).

    ``None`` / ``NaN`` / ``inf`` / ``bool`` / non-numeric / unparsable string
    all degrade to 0.0 -- a malformed answer must never look like an eager one.
    Numeric strings (``"0.9"``) are parsed; out-of-range numbers are clamped.
    """
    value = _finite_float(raw)
    if value is None:
        return 0.0
    return _clamp(value, DESIRE_MIN, DESIRE_MAX)


def current_threshold(settings: Thresholds, state: Any, now: Any) -> float:
    """Threshold right now: linear decay from the stored value, clamped.

    ``max(floor, stored - decay_per_minute * elapsed_minutes)``.  ``updated_at``
    missing, ``now`` unusable, or a clock that moved backwards all mean "no
    decay" rather than an exception.  The result is always within
    ``[threshold_floor, threshold_ceiling]``.
    """
    current = read_state(state, settings)
    floor = settings.threshold_floor
    ceiling = settings.threshold_ceiling
    stored = _clamp(current.threshold, floor, ceiling)
    decay = max(0.0, settings.threshold_decay_per_minute)
    decayed = stored - decay * _elapsed_minutes(current.updated_at, now)
    return _clamp(decayed, floor, ceiling)


def on_sent(
    settings: Thresholds, state: Any, desire: Any, now: Any
) -> DesireState:
    """State after one event happened: decay first, then lift.

    ``base = current_threshold(settings, state, now)`` (doing something must not
    freeze the decay it "would have had"), then
    ``threshold = min(ceiling, base + threshold_step)``.  ``last_desire`` is
    stored normalized so a garbled model answer can never set a low bar.
    """
    current = read_state(state, settings)
    base = current_threshold(settings, current, now)
    lifted = base + max(0.0, settings.threshold_step)
    threshold = _clamp(lifted, settings.threshold_floor, settings.threshold_ceiling)
    return DesireState(
        threshold=threshold,
        updated_at=_seconds(now),
        last_desire=normalize_desire(desire),
    )


def can_proactive_send(
    settings: Thresholds,
    state: Any,
    desire: Any,
    now: Any,
    sent_last_hour: Any = 0,
) -> tuple[bool, str]:
    """May we start a proactive turn?  ``(allowed, reason)``.

    Order matters: the optional hard cap wins over desire (it is a
    stop-the-bleeding switch, ``0`` = off), then ``desire >= threshold`` allows
    (equality counts as enough).  ``sent_last_hour`` is *only* consulted by the
    hard cap; the decay baseline never blocks anything.
    """
    cap = settings.hard_cap_per_hour
    if cap > 0 and _count(sent_last_hour) >= int(cap):
        return False, REASON_HARD_CAP
    threshold = current_threshold(settings, state, now)
    if normalize_desire(desire) < threshold:
        return False, REASON_BELOW_THRESHOLD
    return True, REASON_OK


def should_probe(
    settings: Thresholds,
    state: Any,
    now: Any,
    last_probe_at: Any = None,
    last_desire: Any = None,
) -> bool:
    """Is it worth spending a turn asking the model how much it wants to act?

    True when the threshold has decayed to at most the desire recorded last
    time (so that answer would now pass) *and* at least
    ``probe_interval_minutes`` have passed since ``last_probe_at``.  Without a
    recorded desire it is a first round: still rate-limited by the interval.
    ``last_desire`` wins over ``state.last_desire`` when both exist; an
    unusable clock or a backwards one means "don't probe".
    """
    current = read_state(state, settings)
    threshold = current_threshold(settings, current, now)
    reference = _finite_float(last_desire)
    if reference is None:
        reference = current.last_desire
    else:
        reference = _clamp(reference, DESIRE_MIN, DESIRE_MAX)
    if reference is not None and threshold > reference:
        return False

    now_s = _seconds(now)
    if now_s is None:
        return False
    start = _seconds(last_probe_at)
    if start is None:
        # Never probed before: the interval has trivially elapsed.
        return True
    return (now_s - start) >= max(0, settings.probe_interval_minutes) * 60.0


def describe(settings: Thresholds, state: Any, now: Any) -> dict[str, Any]:
    """Snapshot for logs / ``fatfish status`` / a prompt段.

    ``steps_to_ceiling`` is how many more events (at the current instant) would
    be needed to pin the threshold at the ceiling, ignoring decay.
    """
    current = read_state(state, settings)
    threshold = current_threshold(settings, current, now)
    step = settings.threshold_step
    remaining = max(0.0, settings.threshold_ceiling - threshold)
    if step > 0.0:
        # The epsilon keeps float noise (0.12/0.12 = 1.0000000000000002) from
        # reporting an extra step.
        steps = int(math.ceil(remaining / step - 1e-9))
    else:
        steps = 0
    return {
        "threshold": threshold,
        "decay_per_minute": settings.threshold_decay_per_minute,
        "elapsed_minutes": _elapsed_minutes(current.updated_at, now),
        "steps_to_ceiling": max(0, steps),
    }
