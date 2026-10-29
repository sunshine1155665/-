"""自主动作的软限闸门 + 阈值状态（T-0042 L4）。

这一层只回答一个问题：**她此刻有多想动手，和她自己这条阈值比，够不够**。
算术在 :mod:`fatfish.desire.engine`（三域共用、无域名分支），配置在
:mod:`fatfish.desire.settings`（``[self_action]`` 段），本文件只做 IO：

* **一个状态键**：``state_key("self_action", "global")`` == ``"self_action:state:global"``。
  不带 uin、不带 peer —— 和 ``ai_chat:state:<peer_uin>`` 是两个 key，
  写一个绝不会动另一个（跨域隔离靠键，不靠 if）。
* **一个计数源**：``self_action_evidence``。每日兜底 ``daily_cap`` 和
  ``sent_last_hour`` 都数这张表，不数 ``decision_log``（L2 的留言明确提醒过：
  动作分支不写 decision_log，按它统计会永远是 0）。
* **一条红线**：本服务不 import ``AiChatService``，不读写 ``ai_chat`` 的任何键，
  也没有任何"如果域是 X"的分支。

判定顺序是刻意固定的（``can_run``）：``explicit`` → ``enabled`` → 静默时段
→ ``daily_cap`` → ``engine.can_proactive_send``。前三条是"根本不该动"的硬事实，
最后一条才是软限；把软限放前面会让"关掉了"和"不够想"看起来一样。

``explicit`` 排在最前面，因为它是**唯一一个不来自她自己**的输入：主人已经明说
「你去搜一下 X」，那四道闸门（关掉/静默/日上限/不够想）防的都不是这种情况。
判据只能是他原话（见 :mod:`fatfish.self_action.explicit`），模型自报的字段一律
不采信——不然这就是模型自己能开的门。豁免**不免记账**：成功执行照样写
``self_action_evidence``、照样 ``record_run`` 抬阈值，"做得越多越难自己主动"
依然成立。

``daily_cap`` 是兜底，不是主体：它只防一天刷到失控，正常的节制依旧是
``desire >= 衰减后的阈值``。``hard_cap_per_hour`` 默认 0（关），语义同 ai_chat。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..clock import day_bounds, in_quiet_hours
from ..desire import engine
from ..desire.settings import DesireSettings
from ..desire.state import state_key

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_KIND",
    "REASON_DAILY_CAP",
    "REASON_DISABLED",
    "REASON_EXPLICIT",
    "REASON_QUIET_HOURS",
    "REASON_TOO_MANY_PER_TURN",
    "SELF_ACTION_TOOLS",
    "STATE_KEY",
    "SelfActionService",
]

#: ``decision_log.kind`` for the gate rows the runtime writes.
AUDIT_KIND = "self_action"
#: The actions this domain owns. The runtime imports this same set, so the
#: gate and the counters can never drift apart. ``read_chat`` (T-0072) joined
#: as the fifth, ``watch_video`` (T-0074) as the sixth: both are reads --
#: nothing is sent anywhere, the material lands in evidence for a later turn.
SELF_ACTION_TOOLS = frozenset(
    {
        "search_memory",
        "read_qzone",
        "make_photo",
        "search_videos",
        "read_chat",
        "watch_video",
    }
)
#: One scope for the whole domain: self-actions are never per-peer.
STATE_SCOPE = "global"
#: ``schema_meta`` key; kept as a module constant so tests can pin it.
STATE_KEY = state_key("self_action", STATE_SCOPE)

REASON_DISABLED = "self_action_disabled"
REASON_QUIET_HOURS = "quiet_hours"
REASON_DAILY_CAP = "daily_cap"
#: 主人原话明确指使 → 四道闸门全部让路。审计里用这个串（而不是 "")是为了让
#: "她这次为什么被允许"在 decision_log 里查得到，和拒绝原因同一个字段。
REASON_EXPLICIT = "explicit_request"
#: Per-turn budget (owned by the runtime loop, named here so the audit strings
#: live in one place).
REASON_TOO_MANY_PER_TURN = "too_many_self_actions"

HOUR_SECONDS = 3600.0
DEFAULT_TZ = "Asia/Shanghai"


class SelfActionService:
    """Threshold state + evidence counters for ``[self_action]``."""

    def __init__(
        self,
        db: Any,
        settings: DesireSettings,
        *,
        now_fn: Any = time.time,
        tz_name: str = DEFAULT_TZ,
    ):
        self.db = db
        self.settings = settings
        self._now_fn = now_fn or time.time
        # Only used to bucket "today" and quiet hours; the arithmetic never
        # looks at it.
        self.tz_name = str(tz_name or DEFAULT_TZ)

    # ------------------------------------------------------------------ basics
    def _now(self, now: Any = None) -> float:
        return float(self._now_fn()) if now is None else float(now)

    @property
    def state_key(self) -> str:
        return STATE_KEY

    # ------------------------------------------------------------------- state
    def load_state(self) -> engine.DesireState:
        """Persisted state; never raises.

        Missing key = "never acted" (``initial_threshold``, ``updated_at=None``).
        Corrupt JSON / wrong type / missing ``threshold`` all fall back to the
        same shape with a warning: one bad meta row must not break a turn.
        """
        try:
            raw = self.db.get_meta(self.state_key, None)
        except Exception as exc:  # a db hiccup is not a decision
            logger.warning("self_action 状态读取失败（%s），按初始阈值处理", exc)
            return engine.read_state(None, self.settings)
        if raw is None or raw == "":
            return engine.read_state(None, self.settings)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("self_action 状态 JSON 损坏，回落到 initial_threshold")
            return engine.read_state(None, self.settings)
        if not isinstance(data, dict):
            logger.warning("self_action 状态不是 JSON 对象，回落到 initial_threshold")
            return engine.read_state(None, self.settings)
        if _number(data.get("threshold")) is None:
            logger.warning("self_action 状态缺 threshold，回落到 initial_threshold")
            return engine.read_state(None, self.settings)
        return engine.read_state(data, self.settings)

    def save_state(self, state: Any) -> None:
        """Persist ``{"threshold", "updated_at", "last_desire"}`` as JSON."""
        normalized = engine.read_state(state, self.settings)
        self.db.set_meta(
            self.state_key,
            json.dumps(normalized.to_dict(), ensure_ascii=False, sort_keys=True),
        )

    def current_threshold(self, now: Any = None) -> float:
        """Threshold right now (decayed, clamped) -- engine math, our IO."""
        moment = self._now(now)
        return engine.current_threshold(self.settings, self.load_state(), moment)

    # ---------------------------------------------------------------- counters
    def _evidence_count(self, sql: str, params: tuple[Any, ...]) -> int:
        try:
            row = self.db.query_one(sql, params)
        except Exception as exc:  # table missing on an old db: count 0
            logger.warning("self_action 计数失败（%s），按 0 处理", exc)
            return 0
        return int(row["n"]) if row else 0

    def sent_last_hour(self, now: Any = None) -> int:
        """Self-actions in the last rolling hour (audit + optional hard cap)."""
        moment = self._now(now)
        names = tuple(sorted(SELF_ACTION_TOOLS))
        marks = ",".join("?" for _ in names)
        return self._evidence_count(
            "SELECT COUNT(*) AS n FROM self_action_evidence "
            f"WHERE action IN ({marks}) AND created_at>?",
            (*names, moment - HOUR_SECONDS),
        )

    def daily_count(self, now: Any = None) -> int:
        """Self-actions since local midnight (the ``daily_cap`` counter).

        "Today" is a calendar day in the configured timezone, not a rolling
        24h window -- a cap that refills at midnight is easier to reason about
        than one that refills at a random hour.
        """
        moment = self._now(now)
        start, end = self._today_bounds(moment)
        names = tuple(sorted(SELF_ACTION_TOOLS))
        marks = ",".join("?" for _ in names)
        return self._evidence_count(
            "SELECT COUNT(*) AS n FROM self_action_evidence "
            f"WHERE action IN ({marks}) AND created_at>=? AND created_at<?",
            (*names, start, end),
        )

    def _today_bounds(self, moment: float) -> tuple[float, float]:
        try:
            return day_bounds(moment, self.tz_name)
        except Exception as exc:  # bad tz config: degrade to a rolling day
            logger.warning("self_action 计算自然日失败（%s），按 24 小时窗口处理", exc)
            return moment - 86400.0, moment + 86400.0

    # -------------------------------------------------------------------- gate
    def can_run(
        self, desire: Any, *, now: Any = None, explicit: bool = False
    ) -> tuple[bool, str, dict[str, Any]]:
        """``(allowed, reason, meta)`` for one prospective self-action.

        Order is part of the contract: ``explicit`` -> ``enabled`` -> quiet hours
        -> daily cap -> ``engine.can_proactive_send`` (hard cap, then desire vs
        threshold).  ``meta`` is always populated, including on refusal, so the
        audit row can record the number the gate actually compared against --
        and on an exemption it records *why* the number did not matter.

        ``explicit`` is the owner's own instruction (see
        :mod:`fatfish.self_action.explicit`); it is not a bypass the model can
        grant itself, and it still leaves evidence + a threshold lift behind.
        """
        moment = self._now(now)
        state = self.load_state()
        threshold = engine.current_threshold(self.settings, state, moment)
        sent = self.sent_last_hour(moment)
        daily = self.daily_count(moment)
        meta: dict[str, Any] = {
            "threshold": threshold,
            "desire": engine.normalize_desire(desire),
            "sent_last_hour": sent,
            "daily_count": daily,
            "decay_per_minute": self.settings.threshold_decay_per_minute,
            "explicit": bool(explicit),
        }
        if explicit:
            return True, REASON_EXPLICIT, meta
        if not self.settings.enabled:
            return False, REASON_DISABLED, meta
        if self._in_quiet_hours(moment):
            return False, REASON_QUIET_HOURS, meta
        if self.settings.daily_cap > 0 and daily >= int(self.settings.daily_cap):
            return False, REASON_DAILY_CAP, meta
        allowed, reason = engine.can_proactive_send(
            self.settings, state, desire, moment, sent
        )
        return allowed, reason, meta

    def _in_quiet_hours(self, moment: float) -> bool:
        hours = tuple(getattr(self.settings, "quiet_hours", ()) or ())
        if len(hours) != 2:
            return False
        try:
            return in_quiet_hours(moment, hours[0], hours[1], self.tz_name)
        except Exception as exc:  # malformed HH:MM survived validation: no quiet
            logger.warning("self_action 静默时段判断失败（%s），按不在静默处理", exc)
            return False

    # ------------------------------------------------------------------ ledger
    def record_run(
        self,
        action: str,
        *,
        desire: Any,
        threshold: Any,
        now: Any = None,
    ) -> None:
        """Lift the threshold after a **successful** action.

        Only called on ``ok=True``; a refused or failed action must cost
        nothing.  ``engine.on_sent`` owns the order (decay first, then
        ``+threshold_step``, clamped) so the number here can never disagree
        with the one the gate compared against.  ``threshold`` is the value the
        gate used -- audit metadata only, never re-used as the new base.
        """
        moment = self._now(now)
        try:
            lifted = engine.on_sent(
                self.settings, self.load_state(), desire, moment
            )
            self.save_state(lifted)
        except Exception as exc:  # ledger failure must not break the turn
            logger.warning("self_action 记录动作 %s 失败：%s", action, exc)
            return
        logger.debug(
            "self_action %s 完成：阈值抬到 %.3f（闸门判定时 %.3f）",
            action,
            lifted.threshold,
            _number(threshold) if _number(threshold) is not None else -1.0,
        )


def _number(value: Any) -> float | None:
    """Finite float or ``None``; ``bool`` is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
