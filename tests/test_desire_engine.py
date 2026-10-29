"""T-0052: 通用渴望阈值引擎 —— 与 ai_chat 等价的语义，换一个域跑一遍。

``tests/test_ai_chat_engine.py`` 钉的是 ``[ai_chat]`` 那份配置（step 0.12、floor
0.15、ceiling 0.95、50/h → 0.1/分钟）。这里用 ``[self_action]`` 的参数
（step 0.10、floor 0.20、ceiling 0.90、30/h → 0.05/分钟）把同一套语义再钉一次：
如果通用层哪个函数偷偷绑死了 ai_chat 的常量，这组用例会红。

语义清单（与 T-0046 一致）：

* 线性衰减，地板/天花板夹取，先衰减再抬升，时钟回退不衰减；
* ``desire >= 阈值`` 才算够（相等允许）；
* ``decay_events_per_hour`` 是**衰减基准**不是限流：``hard_cap_per_hour == 0``
  时哪怕 ``sent_last_hour=10000`` 也只看阈值；
* 引擎是纯函数：没有 db / 网络 / 时钟 / 配置读取。
"""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from pathlib import Path

from fatfish import desire
from fatfish.desire import engine
from fatfish.desire.engine import (
    PASSIVE_BYPASSES_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_HARD_CAP,
    REASON_OK,
    AiChatState,
    DesireState,
    can_proactive_send,
    current_threshold,
    describe,
    normalize_desire,
    on_sent,
    read_state,
    should_probe,
)
from fatfish.desire.settings import SELF_ACTION_DEFAULTS

T0 = 1_700_000_000.0


def settings(**overrides):
    """[self_action] defaults: step .10, floor .20, ceiling .90, 30/h -> .05/min."""
    return replace(SELF_ACTION_DEFAULTS, **overrides)


class TestNormalizeDesire(unittest.TestCase):
    def test_plain_numbers_pass_through(self):
        self.assertEqual(normalize_desire(0.65), 0.65)
        self.assertEqual(normalize_desire(0), 0.0)
        self.assertEqual(normalize_desire(1), 1.0)

    def test_numeric_string_is_parsed(self):
        self.assertEqual(normalize_desire("0.9"), 0.9)
        self.assertEqual(normalize_desire(" 0.25 "), 0.25)

    def test_missing_and_garbage_become_zero(self):
        for raw in (None, "", "abc", "nan", object(), [0.5], {"desire": 1}):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_desire(raw), 0.0)

    def test_nan_and_inf_become_zero(self):
        for raw in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_desire(raw), 0.0)

    def test_bool_is_not_a_desire(self):
        # True == 1.0 numerically; a schema typo must not look maximally eager.
        self.assertEqual(normalize_desire(True), 0.0)
        self.assertEqual(normalize_desire(False), 0.0)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(normalize_desire(1.7), 1.0)
        self.assertEqual(normalize_desire(-0.4), 0.0)


class TestCurrentThreshold(unittest.TestCase):
    def test_default_self_action_rate_is_five_hundredths_per_minute(self):
        cfg = settings()
        self.assertAlmostEqual(cfg.threshold_decay_per_minute, 0.05, places=12)
        state = {"threshold": 0.50, "updated_at": T0}
        self.assertAlmostEqual(current_threshold(cfg, state, T0 + 60), 0.45, places=12)
        self.assertAlmostEqual(current_threshold(cfg, state, T0 + 120), 0.40, places=12)

    def test_ten_minutes_decays_exactly_by_ten_minutes_worth(self):
        cfg = settings(decay_events_per_hour=1)  # 0.10/60 = 0.001666.. / min
        state = {"threshold": 0.50, "updated_at": T0}
        gate = current_threshold(cfg, state, T0 + 600)
        self.assertAlmostEqual(
            0.50 - gate, cfg.threshold_decay_per_minute * 10, places=12
        )

    def test_decay_stops_at_floor(self):
        cfg = settings()
        self.assertEqual(
            current_threshold(cfg, {"threshold": 0.25, "updated_at": T0}, T0 + 10_000),
            0.20,
        )

    def test_never_above_ceiling(self):
        cfg = settings()
        self.assertEqual(
            current_threshold(cfg, {"threshold": 5.0, "updated_at": T0}, T0), 0.90
        )

    def test_stored_below_floor_is_raised_to_floor(self):
        cfg = settings()
        self.assertEqual(
            current_threshold(cfg, {"threshold": 0.01, "updated_at": None}, T0), 0.20
        )

    def test_no_decay_without_updated_at(self):
        cfg = settings()
        state = {"threshold": 0.42, "updated_at": None}
        self.assertAlmostEqual(
            current_threshold(cfg, state, T0 + 86_400), 0.42, places=12
        )

    def test_clock_rollback_does_not_decay(self):
        cfg = settings()
        state = {"threshold": 0.42, "updated_at": T0}
        self.assertAlmostEqual(current_threshold(cfg, state, T0 - 600), 0.42, places=12)

    def test_fresh_state_uses_initial_threshold(self):
        cfg = settings()
        self.assertAlmostEqual(current_threshold(cfg, None, T0), 0.35, places=12)
        self.assertAlmostEqual(current_threshold(cfg, {}, T0), 0.35, places=12)


class TestOnSent(unittest.TestCase):
    def test_lifts_after_decaying_first(self):
        cfg = settings()
        state = {"threshold": 0.35, "updated_at": T0}
        new = on_sent(cfg, state, 0.8, T0 + 60)  # 0.35 - 0.05 = 0.30, +0.10

        self.assertIsInstance(new, DesireState)
        self.assertAlmostEqual(new.threshold, 0.40, places=12)
        self.assertEqual(new.updated_at, T0 + 60)
        self.assertEqual(new.last_desire, 0.8)

    def test_lift_is_capped_by_ceiling(self):
        cfg = settings()
        new = on_sent(cfg, {"threshold": 0.88, "updated_at": None}, 1.0, T0)
        self.assertEqual(new.threshold, 0.90)

    def test_lift_from_floor_stays_in_range(self):
        cfg = settings()
        new = on_sent(cfg, {"threshold": 0.20, "updated_at": T0}, 1.0, T0)
        self.assertAlmostEqual(new.threshold, 0.30, places=12)

    def test_desire_is_normalized_on_write(self):
        cfg = settings()
        for raw, expected in ((None, 0.0), ("0.9", 0.9), (float("nan"), 0.0), (3.0, 1.0)):
            with self.subTest(raw=raw):
                new = on_sent(cfg, {"threshold": 0.35}, raw, T0)
                self.assertEqual(new.last_desire, expected)

    def test_returns_state_accepted_by_other_functions(self):
        cfg = settings()
        new = on_sent(cfg, {}, 0.9, T0)
        restored = read_state(new.to_dict(), cfg)
        self.assertEqual(restored, new)
        self.assertEqual(current_threshold(cfg, new.to_dict(), T0), new.threshold)

    def test_garbled_now_does_not_raise(self):
        cfg = settings()
        new = on_sent(cfg, {"threshold": 0.35}, 0.5, "not-a-time")
        self.assertIsNone(new.updated_at)
        self.assertAlmostEqual(new.threshold, 0.45, places=12)


class TestCanProactiveSend(unittest.TestCase):
    def test_allows_when_desire_above_threshold(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, 0.36, T0, 0),
            (True, REASON_OK),
        )

    def test_equal_to_threshold_is_allowed(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35, "updated_at": None}, 0.35, T0, 0),
            (True, REASON_OK),
        )

    def test_slightly_below_threshold_is_rejected(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(
                cfg, {"threshold": 0.35, "updated_at": None}, 0.3499, T0, 0
            ),
            (False, REASON_BELOW_THRESHOLD),
        )

    def test_missing_desire_is_rejected(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, None, T0, 0),
            (False, REASON_BELOW_THRESHOLD),
        )

    def test_decay_makes_a_previous_desire_enough_again(self):
        cfg = settings()
        state = {"threshold": 0.45, "updated_at": T0}
        self.assertEqual(
            can_proactive_send(cfg, state, 0.30, T0, 0), (False, REASON_BELOW_THRESHOLD)
        )
        # 0.05/min: after four minutes 0.45 -> 0.25, so 0.30 is enough.
        self.assertEqual(
            can_proactive_send(cfg, state, 0.30, T0 + 240, 0), (True, REASON_OK)
        )

    def test_daily_volume_never_blocks_when_hard_cap_is_off(self):
        # 30/h is a decay baseline; with hard_cap_per_hour=0 there is no
        # "did 30 things this hour so stop" branch anywhere.
        cfg = settings(hard_cap_per_hour=0, decay_events_per_hour=30)
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, 0.9, T0, 10_000),
            (True, REASON_OK),
        )
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, 0.1, T0, 10_000),
            (False, REASON_BELOW_THRESHOLD),
        )

    def test_hard_cap_blocks_at_the_limit(self):
        cfg = settings(hard_cap_per_hour=5)
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, 1.0, T0, 5),
            (False, REASON_HARD_CAP),
        )
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, 1.0, T0, 4),
            (True, REASON_OK),
        )

    def test_hard_cap_wins_over_desire(self):
        cfg = settings(hard_cap_per_hour=2)
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.35}, 0.0, T0, 3),
            (False, REASON_HARD_CAP),
        )

    def test_bad_sent_count_counts_as_zero(self):
        cfg = settings(hard_cap_per_hour=5)
        for raw in (None, "junk", -3, float("nan")):
            with self.subTest(raw=raw):
                self.assertEqual(
                    can_proactive_send(cfg, {"threshold": 0.35}, 1.0, T0, raw),
                    (True, REASON_OK),
                )


class TestShouldProbe(unittest.TestCase):
    """[self_action] probe interval is 30 minutes."""

    def test_false_while_threshold_still_above_last_desire(self):
        cfg = settings()
        self.assertFalse(
            should_probe(cfg, {"threshold": 0.80, "updated_at": T0}, T0, T0 - 3_600, 0.5)
        )
        # 30 minutes of decay (1.5 in total, clamped by floor) clears 0.5.
        self.assertTrue(
            should_probe(
                cfg, {"threshold": 0.80, "updated_at": T0}, T0 + 1_800, T0 - 3_600, 0.5
            )
        )

    def test_true_once_decayed_to_last_desire_and_interval_elapsed(self):
        cfg = settings()
        state = {"threshold": 0.35, "updated_at": T0}
        self.assertTrue(should_probe(cfg, state, T0 + 1_800, T0, 0.25))

    def test_false_before_interval_elapsed(self):
        cfg = settings()
        state = {"threshold": 0.35, "updated_at": T0}
        self.assertFalse(should_probe(cfg, state, T0 + 1_799, T0, 0.25))

    def test_first_round_without_last_desire_is_probeable(self):
        cfg = settings()
        state = {"threshold": 0.90, "updated_at": T0}
        self.assertTrue(should_probe(cfg, state, T0 + 1_800, None, None))

    def test_state_last_desire_is_used_when_argument_missing(self):
        cfg = settings()
        state = {"threshold": 0.20, "updated_at": None, "last_desire": 0.5}
        self.assertTrue(should_probe(cfg, state, T0 + 1_800, T0, None))
        tight = {"threshold": 0.80, "updated_at": None, "last_desire": 0.5}
        self.assertFalse(should_probe(cfg, tight, T0 + 1_800, T0, None))

    def test_clock_rollback_does_not_probe(self):
        cfg = settings()
        state = {"threshold": 0.20, "updated_at": None}
        self.assertFalse(should_probe(cfg, state, T0, T0 + 1_800, 0.5))

    def test_unusable_now_does_not_probe(self):
        cfg = settings()
        self.assertFalse(should_probe(cfg, {"threshold": 0.20}, None, T0, 0.5))


class TestDescribe(unittest.TestCase):
    def test_reports_the_four_documented_fields(self):
        cfg = settings()
        info = describe(cfg, {"threshold": 0.35, "updated_at": T0}, T0 + 60)
        self.assertEqual(
            set(info),
            {"threshold", "decay_per_minute", "elapsed_minutes", "steps_to_ceiling"},
        )
        self.assertAlmostEqual(info["threshold"], 0.30, places=12)
        self.assertAlmostEqual(info["decay_per_minute"], 0.05, places=12)
        self.assertAlmostEqual(info["elapsed_minutes"], 1.0, places=12)
        # (0.90 - 0.30) / 0.10 = 6 -> six more events pin the ceiling.
        self.assertEqual(info["steps_to_ceiling"], 6)

    def test_steps_to_ceiling_at_ceiling_is_zero(self):
        self.assertEqual(
            describe(settings(), {"threshold": 0.90}, T0)["steps_to_ceiling"], 0
        )

    def test_steps_to_ceiling_from_floor(self):
        # (0.90 - 0.20) / 0.10 = 7
        self.assertEqual(
            describe(settings(), {"threshold": 0.20}, T0)["steps_to_ceiling"], 7
        )

    def test_elapsed_minutes_is_zero_without_updated_at(self):
        self.assertEqual(
            describe(settings(), {"threshold": 0.5}, T0)["elapsed_minutes"], 0.0
        )


class TestStateTolerance(unittest.TestCase):
    def test_none_and_empty_mapping(self):
        cfg = settings()
        self.assertEqual(read_state(None, cfg), DesireState(threshold=0.35))
        self.assertEqual(read_state({}, cfg), DesireState(threshold=0.35))

    def test_wrong_types_are_treated_as_missing(self):
        state = read_state(
            {"threshold": "junk", "updated_at": "yesterday", "last_desire": True},
            settings(),
        )
        self.assertEqual(state.threshold, 0.35)
        self.assertIsNone(state.updated_at)
        self.assertIsNone(state.last_desire)

    def test_numeric_strings_are_accepted(self):
        state = read_state(
            {"threshold": "0.5", "updated_at": str(T0), "last_desire": "0.7"}, settings()
        )
        self.assertEqual(state.threshold, 0.5)
        self.assertEqual(state.updated_at, T0)
        self.assertEqual(state.last_desire, 0.7)

    def test_state_like_object_with_attributes(self):
        class Duck:
            threshold = 0.40
            updated_at = None
            last_desire = 0.2

        self.assertEqual(read_state(Duck(), settings()).threshold, 0.40)

    def test_out_of_range_last_desire_is_clamped(self):
        self.assertEqual(read_state({"last_desire": 9.0}).last_desire, 1.0)
        self.assertEqual(read_state({"last_desire": -9.0}).last_desire, 0.0)

    def test_state_is_immutable(self):
        state = DesireState(threshold=0.3)
        with self.assertRaises(Exception):
            state.threshold = 0.4  # type: ignore[misc]


class TestModuleHygiene(unittest.TestCase):
    def test_ai_chat_state_is_the_same_class(self):
        # Backwards-compatible alias, not a second implementation.
        self.assertIs(AiChatState, DesireState)
        self.assertIs(desire.AiChatState, DesireState)

    def test_passive_events_bypass_the_gate(self):
        self.assertTrue(PASSIVE_BYPASSES_THRESHOLD)

    def test_reason_strings_are_stable(self):
        self.assertEqual(REASON_OK, "ok")
        self.assertEqual(REASON_BELOW_THRESHOLD, "below_threshold")
        self.assertEqual(REASON_HARD_CAP, "hard_cap")

    def test_engine_is_domain_neutral_and_side_effect_free(self):
        source = Path(engine.__file__).read_text(encoding="utf-8")
        # Hard requirement of T-0052: the shared arithmetic must not branch on
        # the domain; the domain is expressed by the settings object it gets.
        self.assertNotIn("if domain", source)
        self.assertNotIn('== "ai_chat"', source)
        self.assertNotIn('"self_action"', source)
        # ... and it stays pure: no db, no network, no clock, no config files.
        self.assertNotIn("import sqlite3", source)
        self.assertNotIn("fatfish.runtime", source)
        self.assertNotIn("fatfish.platforms", source)
        self.assertNotIn("time.time()", source)
        self.assertNotIn("import time", source)

    def test_decay_baseline_is_only_a_baseline(self):
        cfg = settings(decay_events_per_hour=30, threshold_step=0.10)
        self.assertAlmostEqual(cfg.threshold_decay_per_minute, 0.05, places=12)
        self.assertEqual(cfg.hard_cap_per_hour, 0)
        self.assertTrue(math.isclose(0.10 * 30 / 60, 0.05, rel_tol=1e-12))


if __name__ == "__main__":
    unittest.main()
