"""T-0046: 渴望阈值引擎 —— 线性衰减 / 抬升 / 夹取 / 软限判定，纯函数。

L2 只有算术，没有 IO：``now`` 由调用方传入，``settings`` 由 L1 传入。 这些用例
把卡上写死的语义逐条钉住，尤其是那条容易走偏的：

* ``decay_messages_per_hour``（默认 50）是**衰减基准**，不是限流；
  ``hard_cap_per_hour == 0``（默认）时哪怕 ``sent_last_hour=10000`` 也照样按
  阈值判定，代码里不存在"到 50 条就拒发"的分支。
* 抬升前先衰减：``base = current_threshold(...)``，再 ``min(ceiling, base + step)``。
* ``desire >= threshold`` 才算够（相等允许）。
"""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from pathlib import Path

from fatfish.ai_chat import engine
from fatfish.ai_chat.engine import (
    PASSIVE_BYPASSES_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_HARD_CAP,
    REASON_OK,
    AiChatState,
    can_proactive_send,
    current_threshold,
    describe,
    normalize_desire,
    on_sent,
    read_state,
    should_probe,
)
from fatfish.ai_chat.settings import AiChatSettings

T0 = 1_700_000_000.0


def settings(**overrides) -> AiChatSettings:
    """Default profile (step .12, floor .15, ceiling .95, 50/h -> 0.1/min)."""
    return replace(AiChatSettings(), **overrides)


class TestNormalizeDesire(unittest.TestCase):
    def test_plain_numbers_pass_through(self):
        self.assertEqual(normalize_desire(0.65), 0.65)
        self.assertEqual(normalize_desire(0), 0.0)
        self.assertEqual(normalize_desire(1), 1.0)

    def test_numeric_string_is_parsed(self):
        self.assertEqual(normalize_desire("0.9"), 0.9)
        self.assertEqual(normalize_desire(" 0.25 "), 0.25)
        self.assertEqual(normalize_desire("1"), 1.0)

    def test_missing_and_garbage_become_zero(self):
        for raw in (None, "", "abc", "nan", object(), [0.5], {"desire": 1}):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_desire(raw), 0.0)

    def test_nan_and_inf_become_zero(self):
        self.assertEqual(normalize_desire(float("nan")), 0.0)
        self.assertEqual(normalize_desire(float("inf")), 0.0)
        self.assertEqual(normalize_desire(float("-inf")), 0.0)

    def test_bool_is_not_a_desire(self):
        # True == 1.0 numerically; treating it as 1.0 would let a schema typo
        # look maximally eager, so booleans are unusable input.
        self.assertEqual(normalize_desire(True), 0.0)
        self.assertEqual(normalize_desire(False), 0.0)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(normalize_desire(1.7), 1.0)
        self.assertEqual(normalize_desire(-0.4), 0.0)


class TestCurrentThreshold(unittest.TestCase):
    def test_ten_minutes_decays_exactly_by_ten_minutes_worth(self):
        cfg = settings(decay_messages_per_hour=1)  # 0.12/60 = 0.002 / min
        state = {"threshold": 0.50, "updated_at": T0}
        gate = current_threshold(cfg, state, T0 + 600)

        self.assertAlmostEqual(gate, 0.48, places=12)
        self.assertAlmostEqual(
            0.50 - gate, cfg.threshold_decay_per_minute * 10, places=12
        )

    def test_default_profile_decays_one_tenth_per_minute(self):
        cfg = settings()
        self.assertAlmostEqual(cfg.threshold_decay_per_minute, 0.1, places=12)
        state = {"threshold": 0.50, "updated_at": T0}
        self.assertAlmostEqual(current_threshold(cfg, state, T0 + 60), 0.40, places=12)
        self.assertAlmostEqual(current_threshold(cfg, state, T0 + 120), 0.30, places=12)

    def test_decay_stops_at_floor(self):
        cfg = settings()
        state = {"threshold": 0.20, "updated_at": T0}
        self.assertEqual(current_threshold(cfg, state, T0 + 10_000), 0.15)

    def test_never_above_ceiling(self):
        cfg = settings()
        self.assertEqual(
            current_threshold(cfg, {"threshold": 5.0, "updated_at": T0}, T0), 0.95
        )

    def test_stored_below_floor_is_raised_to_floor(self):
        cfg = settings()
        self.assertEqual(
            current_threshold(cfg, {"threshold": 0.01, "updated_at": None}, T0), 0.15
        )

    def test_no_decay_without_updated_at(self):
        cfg = settings()
        state = {"threshold": 0.42, "updated_at": None}
        self.assertAlmostEqual(current_threshold(cfg, state, T0 + 86_400), 0.42, places=12)

    def test_clock_rollback_does_not_decay(self):
        cfg = settings()
        state = {"threshold": 0.42, "updated_at": T0}
        self.assertAlmostEqual(current_threshold(cfg, state, T0 - 600), 0.42, places=12)

    def test_fresh_state_uses_initial_threshold(self):
        cfg = settings()
        self.assertAlmostEqual(current_threshold(cfg, None, T0), 0.30, places=12)
        self.assertAlmostEqual(current_threshold(cfg, {}, T0), 0.30, places=12)


class TestOnSent(unittest.TestCase):
    def test_lifts_after_decaying_first(self):
        cfg = settings()
        state = {"threshold": 0.30, "updated_at": T0}
        new = on_sent(cfg, state, 0.8, T0 + 60)  # decayed 0.30 -> 0.20, +0.12

        self.assertIsInstance(new, AiChatState)
        self.assertAlmostEqual(new.threshold, 0.32, places=12)
        self.assertEqual(new.updated_at, T0 + 60)
        self.assertEqual(new.last_desire, 0.8)

    def test_lift_is_capped_by_ceiling(self):
        cfg = settings()
        new = on_sent(cfg, {"threshold": 0.93, "updated_at": None}, 1.0, T0)
        self.assertEqual(new.threshold, 0.95)

    def test_lift_from_floor_stays_in_range(self):
        cfg = settings()
        new = on_sent(cfg, {"threshold": 0.15, "updated_at": T0}, 1.0, T0)
        self.assertAlmostEqual(new.threshold, 0.27, places=12)

    def test_desire_is_normalized_on_write(self):
        cfg = settings()
        for raw, expected in ((None, 0.0), ("0.9", 0.9), (float("nan"), 0.0), (3.0, 1.0)):
            with self.subTest(raw=raw):
                new = on_sent(cfg, {"threshold": 0.30}, raw, T0)
                self.assertEqual(new.last_desire, expected)

    def test_returns_state_accepted_by_other_functions(self):
        cfg = settings()
        new = on_sent(cfg, {}, 0.9, T0)
        # Round-trip through the persisted shape: the state must be reusable.
        restored = read_state(new.to_dict(), cfg)
        self.assertEqual(restored, new)
        self.assertEqual(current_threshold(cfg, new.to_dict(), T0), new.threshold)

    def test_garbled_now_does_not_raise(self):
        cfg = settings()
        new = on_sent(cfg, {"threshold": 0.30}, 0.5, "not-a-time")
        self.assertIsNone(new.updated_at)
        self.assertAlmostEqual(new.threshold, 0.42, places=12)


class TestCanProactiveSend(unittest.TestCase):
    def test_allows_when_desire_above_threshold(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, 0.31, T0, 0),
            (True, REASON_OK),
        )

    def test_equal_to_threshold_is_allowed(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30, "updated_at": None}, 0.30, T0, 0),
            (True, REASON_OK),
        )

    def test_slightly_below_threshold_is_rejected(self):
        cfg = settings()
        allowed, reason = can_proactive_send(
            cfg, {"threshold": 0.30, "updated_at": None}, 0.2999, T0, 0
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, REASON_BELOW_THRESHOLD)

    def test_missing_desire_is_rejected(self):
        cfg = settings()
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, None, T0, 0),
            (False, REASON_BELOW_THRESHOLD),
        )

    def test_decay_makes_a_previous_desire_enough_again(self):
        cfg = settings()
        state = {"threshold": 0.40, "updated_at": T0}
        self.assertEqual(
            can_proactive_send(cfg, state, 0.25, T0, 0),
            (False, REASON_BELOW_THRESHOLD),
        )
        # 0.1/min: after two minutes 0.40 -> 0.20, so 0.25 is enough.
        self.assertEqual(can_proactive_send(cfg, state, 0.25, T0 + 120, 0), (True, REASON_OK))

    def test_hard_cap_disabled_never_blocks_50_messages(self):
        # The whole point of the boss's correction: 50 is a decay baseline.
        cfg = settings(hard_cap_per_hour=0, decay_messages_per_hour=50)
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, 0.9, T0, 10_000),
            (True, REASON_OK),
        )
        # ... and it still judges purely on the threshold at that volume.
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, 0.1, T0, 10_000),
            (False, REASON_BELOW_THRESHOLD),
        )

    def test_hard_cap_blocks_at_the_limit(self):
        cfg = settings(hard_cap_per_hour=50)
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, 1.0, T0, 50),
            (False, REASON_HARD_CAP),
        )
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, 1.0, T0, 49),
            (True, REASON_OK),
        )

    def test_hard_cap_wins_over_desire(self):
        cfg = settings(hard_cap_per_hour=5)
        self.assertEqual(
            can_proactive_send(cfg, {"threshold": 0.30}, 0.0, T0, 6),
            (False, REASON_HARD_CAP),
        )

    def test_bad_sent_count_counts_as_zero(self):
        cfg = settings(hard_cap_per_hour=5)
        for raw in (None, "junk", -3, float("nan")):
            with self.subTest(raw=raw):
                self.assertEqual(
                    can_proactive_send(cfg, {"threshold": 0.30}, 1.0, T0, raw),
                    (True, REASON_OK),
                )


class TestShouldProbe(unittest.TestCase):
    def test_false_while_threshold_still_above_last_desire(self):
        cfg = settings()
        self.assertFalse(
            should_probe(cfg, {"threshold": 0.60, "updated_at": T0}, T0, T0 - 3_600, 0.5)
        )
        # One minute of decay (0.1) still leaves 0.50 == last_desire -> probe.
        self.assertTrue(
            should_probe(cfg, {"threshold": 0.60, "updated_at": T0}, T0 + 60, T0 - 600, 0.5)
        )

    def test_true_once_decayed_to_last_desire_and_interval_elapsed(self):
        cfg = settings()  # probe interval 10 min
        state = {"threshold": 0.30, "updated_at": T0}
        self.assertTrue(should_probe(cfg, state, T0 + 600, T0, 0.2))

    def test_false_before_interval_elapsed(self):
        cfg = settings()
        state = {"threshold": 0.30, "updated_at": T0}
        self.assertFalse(should_probe(cfg, state, T0 + 599, T0, 0.2))

    def test_first_round_without_last_desire_is_probeable(self):
        cfg = settings()
        state = {"threshold": 0.90, "updated_at": T0}
        self.assertTrue(should_probe(cfg, state, T0 + 600, None, None))

    def test_first_round_still_respects_the_interval(self):
        cfg = settings()
        state = {"threshold": 0.90, "updated_at": T0}
        self.assertFalse(should_probe(cfg, state, T0 + 60, T0, None))

    def test_state_last_desire_is_used_when_argument_missing(self):
        cfg = settings()
        state = {"threshold": 0.20, "updated_at": None, "last_desire": 0.5}
        self.assertTrue(should_probe(cfg, state, T0 + 600, T0, None))
        tight = {"threshold": 0.60, "updated_at": None, "last_desire": 0.5}
        self.assertFalse(should_probe(cfg, tight, T0 + 600, T0, None))

    def test_clock_rollback_does_not_probe(self):
        cfg = settings()
        state = {"threshold": 0.20, "updated_at": None}
        self.assertFalse(should_probe(cfg, state, T0, T0 + 600, 0.5))

    def test_unusable_now_does_not_probe(self):
        cfg = settings()
        self.assertFalse(should_probe(cfg, {"threshold": 0.20}, None, T0, 0.5))


class TestDescribe(unittest.TestCase):
    def test_reports_the_four_documented_fields(self):
        cfg = settings()
        info = describe(cfg, {"threshold": 0.30, "updated_at": T0}, T0 + 60)
        self.assertEqual(
            set(info), {"threshold", "decay_per_minute", "elapsed_minutes", "steps_to_ceiling"}
        )
        self.assertAlmostEqual(info["threshold"], 0.20, places=12)
        self.assertAlmostEqual(info["decay_per_minute"], 0.1, places=12)
        self.assertAlmostEqual(info["elapsed_minutes"], 1.0, places=12)
        # (0.95 - 0.20) / 0.12 = 6.25 -> 7 more messages to pin the ceiling.
        self.assertEqual(info["steps_to_ceiling"], 7)

    def test_steps_to_ceiling_at_ceiling_is_zero(self):
        self.assertEqual(
            describe(settings(), {"threshold": 0.95}, T0)["steps_to_ceiling"], 0
        )

    def test_steps_to_ceiling_exact_multiple_is_not_rounded_up(self):
        # (0.95 - 0.83) / 0.12 == 1.0000000000000002 in floats; still one step.
        self.assertEqual(
            describe(settings(), {"threshold": 0.83}, T0)["steps_to_ceiling"], 1
        )

    def test_steps_to_ceiling_from_floor(self):
        # (0.95 - 0.15) / 0.12 = 6.67 -> 7
        self.assertEqual(
            describe(settings(), {"threshold": 0.15}, T0)["steps_to_ceiling"], 7
        )

    def test_elapsed_minutes_is_zero_without_updated_at(self):
        self.assertEqual(
            describe(settings(), {"threshold": 0.5}, T0)["elapsed_minutes"], 0.0
        )


class TestStateTolerance(unittest.TestCase):
    def test_none_and_empty_mapping(self):
        cfg = settings()
        self.assertEqual(read_state(None, cfg), AiChatState(threshold=0.30))
        self.assertEqual(read_state({}, cfg), AiChatState(threshold=0.30))

    def test_missing_fields_default(self):
        self.assertEqual(
            read_state({"threshold": 0.5}, settings()),
            AiChatState(threshold=0.5, updated_at=None, last_desire=None),
        )

    def test_wrong_types_are_treated_as_missing(self):
        state = read_state(
            {"threshold": "junk", "updated_at": "yesterday", "last_desire": True},
            settings(),
        )
        self.assertEqual(state.threshold, 0.30)  # initial_threshold
        self.assertIsNone(state.updated_at)
        self.assertIsNone(state.last_desire)

    def test_nan_and_inf_are_treated_as_missing(self):
        state = read_state(
            {
                "threshold": float("nan"),
                "updated_at": float("inf"),
                "last_desire": float("nan"),
            },
            settings(),
        )
        self.assertEqual(state.threshold, 0.30)
        self.assertIsNone(state.updated_at)
        self.assertIsNone(state.last_desire)

    def test_numeric_strings_are_accepted(self):
        state = read_state(
            {"threshold": "0.5", "updated_at": str(T0), "last_desire": "0.7"}, settings()
        )
        self.assertEqual(state.threshold, 0.5)
        self.assertEqual(state.updated_at, T0)
        self.assertEqual(state.last_desire, 0.7)

    def test_state_object_is_accepted(self):
        cfg = settings()
        state = AiChatState(threshold=0.40, updated_at=T0, last_desire=0.6)
        self.assertAlmostEqual(current_threshold(cfg, state, T0 + 60), 0.30, places=12)

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
        state = AiChatState(threshold=0.3)
        with self.assertRaises(Exception):
            state.threshold = 0.4  # type: ignore[misc]


class TestModuleHygiene(unittest.TestCase):
    def test_passive_replies_bypass_the_gate(self):
        self.assertTrue(PASSIVE_BYPASSES_THRESHOLD)

    def test_reason_strings_are_stable(self):
        self.assertEqual(REASON_OK, "ok")
        self.assertEqual(REASON_BELOW_THRESHOLD, "below_threshold")
        self.assertEqual(REASON_HARD_CAP, "hard_cap")

    def test_engine_does_not_import_runtime_or_platforms(self):
        source = Path(engine.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fatfish.runtime", source)
        self.assertNotIn("fatfish.platforms", source)
        self.assertNotIn("import sqlite3", source)
        self.assertNotIn("open(", source)

    def test_fifty_is_only_a_decay_baseline(self):
        # Guards against someone reintroducing a "50 sent this hour -> refuse"
        # branch: the derived rate is a function of the baseline, nothing else.
        cfg = settings(decay_messages_per_hour=50, threshold_step=0.12)
        self.assertAlmostEqual(cfg.threshold_decay_per_minute, 0.1, places=12)
        self.assertEqual(cfg.hard_cap_per_hour, 0)
        self.assertTrue(math.isclose(cfg.threshold_step * 50 / 60, 0.1, rel_tol=1e-12))


if __name__ == "__main__":
    unittest.main()
