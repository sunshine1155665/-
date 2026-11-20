"""T-0052: 三域配置契约 —— 段、默认值、双键兼容、非法值回落。

钉住四件事：

* 三套 profile 都带 ``[self_action]`` 与 ``[owner_chat]``，且三份取值完全一致
  （漂移守卫 ``test_config_profiles`` 的"无新增 override"就是靠这个）；
* ``[ai_chat]`` 段一个键都没动：仍然是 ``decay_messages_per_hour``，没有新键；
* 通用层读衰减键时**新键优先、缺失回落旧键**，所以 [ai_chat] 不用改配置，
  [self_action] 用「次」这个单位的新键；
* 非法值一律回落默认 + 一条 warning，绝不抛异常。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from fatfish.config import Config, load_config
from fatfish.desire.settings import (
    AI_CHAT_DEFAULTS,
    OWNER_CHAT_DEFAULTS,
    SELF_ACTION_DEFAULTS,
    DesireSettings,
    read_desire_settings,
)

PROFILES = ("low", "standard", "remote")

EXPECTED_SELF_ACTION = {
    # 2026-10-26 boss 拍板：自主动作默认开（四道软限一个都没松）。
    # 2026-11-15 boss 拍板：渴望阈值下调（0.35→0.15 / 步长 0.10→0.06 /
    # 地板 0.20→0.05），日上限归零（0 = 不限）。静默时段/每轮上限不变。
    # 2026-11-17 boss：再降一档（0.15→0.10 / 0.06→0.05 / 地板 0.04，
    # 衰减 30→40 次/小时、探测 30→20 分钟）：照片多用，略少于表情包。
    "enabled": True,
    "initial_threshold": 0.10,
    "threshold_step": 0.05,
    "threshold_floor": 0.04,
    "threshold_ceiling": 0.90,
    "decay_events_per_hour": 40,
    "hard_cap_per_hour": 0,
    "daily_cap": 0,
    "max_events_per_turn": 2,
    "probe_interval_minutes": 20,
    "quiet_hours": ["01:00", "08:00"],
}

EXPECTED_OWNER_CHAT = {
    "enabled": False,
    "initial_threshold": 0.30,
    "threshold_step": 0.12,
    "threshold_floor": 0.15,
    "threshold_ceiling": 0.90,
    "decay_events_per_hour": 50,
    "hard_cap_per_hour": 0,
    "quiet_hours": ["01:00", "08:00"],
}


def _config(**sections: dict) -> Config:
    """A bare Config carrying only the sections given (no disk, no profile)."""
    return Config({name: dict(body) for name, body in sections.items()}, profile="test")


def _self_action(**overrides) -> DesireSettings:
    return read_desire_settings(
        _config(self_action=overrides), "self_action", defaults=SELF_ACTION_DEFAULTS
    )


class ProfileConfigTests(unittest.TestCase):
    def test_all_profiles_declare_the_full_self_action_block(self) -> None:
        for profile in PROFILES:
            section = load_config(profile).section("self_action")
            self.assertEqual(
                sorted(section),
                sorted(EXPECTED_SELF_ACTION),
                f"{profile}: [self_action] must carry exactly the documented keys",
            )
            for key, expected in EXPECTED_SELF_ACTION.items():
                self.assertEqual(section[key], expected, f"{profile}.{key}")

    def test_all_profiles_declare_the_owner_chat_placeholder(self) -> None:
        for profile in PROFILES:
            section = load_config(profile).section("owner_chat")
            self.assertEqual(
                sorted(section),
                sorted(EXPECTED_OWNER_CHAT),
                f"{profile}: [owner_chat] is a placeholder with a fixed key set",
            )
            for key, expected in EXPECTED_OWNER_CHAT.items():
                self.assertEqual(section[key], expected, f"{profile}.{key}")
            # The placeholder must stay off: nothing reads it this round.
            self.assertFalse(section["enabled"], profile)

    def test_profiles_agree_on_both_new_sections(self) -> None:
        low = load_config("low")
        for profile in PROFILES[1:]:
            other = load_config(profile)
            self.assertEqual(other.section("self_action"), low.section("self_action"))
            self.assertEqual(other.section("owner_chat"), low.section("owner_chat"))

    def test_ai_chat_section_is_untouched(self) -> None:
        # [ai_chat] keeps the legacy key and its 50/h baseline; the generic
        # layer adapts to the config, never the other way round.
        for profile in PROFILES:
            section = load_config(profile).section("ai_chat")
            self.assertIn("decay_messages_per_hour", section)
            self.assertEqual(section["decay_messages_per_hour"], 50)
            self.assertNotIn("decay_events_per_hour", section)
            self.assertNotIn("daily_cap", section)
            self.assertNotIn("max_events_per_turn", section)

    def test_derived_decay_key_is_not_configurable(self) -> None:
        for profile in PROFILES:
            config = load_config(profile)
            for section in ("ai_chat", "self_action", "owner_chat"):
                self.assertNotIn("threshold_decay_per_minute", config.section(section))


class DefaultsTests(unittest.TestCase):
    def test_self_action_defaults_match_the_shipped_section(self) -> None:
        settings = read_desire_settings(
            load_config("low"), "self_action", defaults=SELF_ACTION_DEFAULTS
        )
        self.assertEqual(settings.warnings, ())
        self.assertEqual(settings.enabled, True)
        self.assertAlmostEqual(settings.initial_threshold, 0.10)
        self.assertAlmostEqual(settings.threshold_step, 0.05)
        self.assertAlmostEqual(settings.threshold_floor, 0.04)
        self.assertAlmostEqual(settings.threshold_ceiling, 0.90)
        self.assertEqual(settings.decay_events_per_hour, 40)
        self.assertEqual(settings.hard_cap_per_hour, 0)
        self.assertEqual(settings.daily_cap, 0)
        self.assertEqual(settings.max_events_per_turn, 2)
        self.assertEqual(settings.probe_interval_minutes, 20)
        self.assertEqual(settings.quiet_hours, ("01:00", "08:00"))

    def test_self_action_decay_is_a_thirtieth_per_minute(self) -> None:
        settings = read_desire_settings(
            load_config("low"), "self_action", defaults=SELF_ACTION_DEFAULTS
        )
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 1/30, places=12)
        self.assertAlmostEqual(
            settings.threshold_decay_per_minute * 60, 2.0, places=12
        )

    def test_missing_section_falls_back_to_defaults_without_warnings(self) -> None:
        settings = read_desire_settings(
            _config(), "self_action", defaults=SELF_ACTION_DEFAULTS
        )
        self.assertEqual(settings.initial_threshold, SELF_ACTION_DEFAULTS.initial_threshold)
        self.assertEqual(settings.warnings, ())
        # ``[self_action]`` 的默认是开的（与 ``[ai_chat]`` 相反，见 settings.py
        # 里 SELF_ACTION_DEFAULTS 上方那段注释）；缺段 ≠ 关闭。
        self.assertTrue(settings.active)
        self.assertEqual(settings.disabled_reason, "")

    def test_enabled_activates_a_generic_domain(self) -> None:
        settings = _self_action(enabled=True)
        self.assertTrue(settings.active)
        self.assertEqual(settings.disabled_reason, "")

    def test_settings_are_immutable(self) -> None:
        settings = _self_action()
        with self.assertRaises(Exception):
            settings.threshold_step = 0.5  # type: ignore[misc]
        with self.assertRaises(Exception):
            settings.warnings = ("x",)  # type: ignore[misc]


class DecayKeyFallbackTests(unittest.TestCase):
    """``decay_events_per_hour`` 优先，缺失回落 ``decay_messages_per_hour``。"""

    def test_new_key_is_used_for_self_action(self) -> None:
        settings = _self_action(decay_events_per_hour=60)
        self.assertEqual(settings.decay_events_per_hour, 60)
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.05, places=12)

    def test_legacy_key_is_accepted_when_new_key_is_absent(self) -> None:
        settings = _self_action(decay_messages_per_hour=12)
        self.assertEqual(settings.decay_events_per_hour, 12)
        # 0.05 * 12 / 60 = 0.01/min with the 2026-11-17 step.
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.01, places=12)

    def test_new_key_wins_when_both_are_present(self) -> None:
        settings = _self_action(decay_events_per_hour=6, decay_messages_per_hour=600)
        self.assertEqual(settings.decay_events_per_hour, 6)
        self.assertEqual(settings.warnings, ())

    def test_ai_chat_still_reads_its_legacy_key(self) -> None:
        # 0.12 * 50 / 60 = 0.1/min, exactly what T-0041 shipped.
        settings = read_desire_settings(
            _config(ai_chat={"enabled": False}), "ai_chat", defaults=AI_CHAT_DEFAULTS
        )
        self.assertEqual(settings.decay_events_per_hour, 50)
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.10, places=12)
        self.assertAlmostEqual(settings.initial_threshold, 0.30, places=12)

    def test_legacy_key_can_overrule_an_ai_chat_override(self) -> None:
        settings = read_desire_settings(
            _config(ai_chat={"decay_messages_per_hour": 30, "threshold_step": 0.2}),
            "ai_chat",
            defaults=AI_CHAT_DEFAULTS,
        )
        self.assertEqual(settings.decay_events_per_hour, 30)
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.1, places=12)

    def test_owner_chat_placeholder_reads_its_new_key(self) -> None:
        settings = read_desire_settings(
            load_config("low"), "owner_chat", defaults=OWNER_CHAT_DEFAULTS
        )
        self.assertEqual(settings.decay_events_per_hour, 50)
        self.assertEqual(settings.warnings, ())


class InvalidConfigTests(unittest.TestCase):
    """Every illegal value degrades to the default with a warning -- never raises."""

    def test_step_zero_falls_back(self) -> None:
        settings = _self_action(threshold_step=0)
        self.assertAlmostEqual(settings.threshold_step, 0.05)
        self.assertTrue(settings.warnings)

    def test_step_negative_falls_back(self) -> None:
        settings = _self_action(threshold_step=-0.5)
        self.assertAlmostEqual(settings.threshold_step, 0.05)
        self.assertTrue(settings.warnings)

    def test_floor_above_ceiling_resets_both(self) -> None:
        settings = _self_action(threshold_floor=0.8, threshold_ceiling=0.4)
        self.assertAlmostEqual(settings.threshold_floor, 0.04)
        self.assertAlmostEqual(settings.threshold_ceiling, 0.90)
        self.assertLess(settings.threshold_floor, settings.threshold_ceiling)
        self.assertTrue(settings.warnings)

    def test_ceiling_above_one_is_clamped(self) -> None:
        settings = _self_action(threshold_ceiling=1.5)
        self.assertAlmostEqual(settings.threshold_ceiling, 1.0)
        self.assertTrue(settings.warnings)

    def test_initial_threshold_is_clamped_into_floor_ceiling(self) -> None:
        settings = _self_action(initial_threshold=2.0)
        self.assertAlmostEqual(settings.initial_threshold, 0.90)
        self.assertTrue(settings.warnings)

    def test_non_numeric_values_fall_back(self) -> None:
        settings = _self_action(threshold_step="fast", decay_events_per_hour="many")
        self.assertAlmostEqual(settings.threshold_step, 0.05)
        self.assertEqual(settings.decay_events_per_hour, 40)
        self.assertGreaterEqual(len(settings.warnings), 2)

    def test_negative_counters_fall_back(self) -> None:
        settings = _self_action(hard_cap_per_hour=-1, daily_cap=-5)
        self.assertEqual(settings.hard_cap_per_hour, 0)
        self.assertEqual(settings.daily_cap, 0)
        self.assertGreaterEqual(len(settings.warnings), 2)

    def test_zero_limits_fall_back(self) -> None:
        # probe 0 would mean "probe forever"; per-turn 0 would mean "never act".
        settings = _self_action(probe_interval_minutes=0, max_events_per_turn=0)
        self.assertEqual(settings.probe_interval_minutes, 20)
        self.assertEqual(settings.max_events_per_turn, 2)
        self.assertGreaterEqual(len(settings.warnings), 2)

    def test_zero_hard_cap_is_legal_and_silent(self) -> None:
        settings = _self_action(hard_cap_per_hour=0)
        self.assertEqual(settings.hard_cap_per_hour, 0)
        self.assertEqual(settings.warnings, ())

    def test_zero_daily_cap_is_legal_and_silent(self) -> None:
        settings = _self_action(daily_cap=0)
        self.assertEqual(settings.daily_cap, 0)
        self.assertEqual(settings.warnings, ())

    def test_bad_quiet_hours_fall_back(self) -> None:
        for bad in ("25:00", ["01:00"], ["1:00", "08:00"], ["01:00", "08:00", "09:00"]):
            with self.subTest(bad=bad):
                settings = _self_action(quiet_hours=bad)
                self.assertEqual(settings.quiet_hours, ("01:00", "08:00"))
                self.assertTrue(settings.warnings)

    def test_quiet_hours_may_be_explicitly_empty(self) -> None:
        settings = _self_action(quiet_hours=[])
        self.assertEqual(settings.quiet_hours, ())
        self.assertEqual(settings.warnings, ())

    def test_wrong_typed_section_falls_back(self) -> None:
        settings = read_desire_settings(
            Config({"self_action": "nope"}, profile="test"),
            "self_action",
            defaults=SELF_ACTION_DEFAULTS,
        )
        # 段写坏了 → 完整回落到默认值（本域默认开），关键是「不抛异常、不留半截」。
        self.assertEqual(settings.enabled, SELF_ACTION_DEFAULTS.enabled)
        self.assertTrue(settings.warnings)

    def test_object_without_get_does_not_explode(self) -> None:
        settings = read_desire_settings(
            object(), "self_action", defaults=SELF_ACTION_DEFAULTS
        )
        self.assertEqual(settings.enabled, SELF_ACTION_DEFAULTS.enabled)
        self.assertEqual(settings.warnings, ())

    def test_bad_value_never_breaks_the_decay_derivation(self) -> None:
        settings = _self_action(threshold_step=-1, decay_events_per_hour=-9)
        self.assertAlmostEqual(settings.threshold_step, 0.05)
        self.assertEqual(settings.decay_events_per_hour, 40)
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 1/30, places=12)

    def test_replace_keeps_the_derived_field_consistent(self) -> None:
        settings = replace(SELF_ACTION_DEFAULTS, decay_events_per_hour=60)
        # 0.05 * 60 / 60 = 0.05/min -- derived, never read from config.
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.05, places=12)


if __name__ == "__main__":
    unittest.main()
