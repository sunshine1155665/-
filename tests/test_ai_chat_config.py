"""T-0041 / T-0045: [ai_chat] base layer -- config contract + schema v11.

The 大肥鱼间聊天 feature is gated entirely by config (one bound QQ account,
empty = off), and its "50 条/小时" number is a **decay baseline**, not a send
limit.  These tests pin the three things L2/L3 are allowed to rely on:

* the three shipped profiles carry the same, complete ``[ai_chat]`` block;
* ``read_settings`` always returns a legal, immutable object -- bad input is
  clamped/replaced with a warning, never an exception;
* ``threshold_decay_per_minute`` is derived (0.12 * 50 / 60 = 0.1), so nobody
  can reintroduce a hand-written ``threshold_decay_per_minute`` key;
* the v10 -> v11 upgrade creates ``ai_chat_messages`` idempotently.

Nothing here touches the network or the real ``data/fatfish.db``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.ai_chat import AiChatSettings, read_settings
from fatfish.ai_chat.settings import SETTING_KEYS
from fatfish.config import Config, load_config
from fatfish.db import SCHEMA_VERSION, Database

EXPECTED_DEFAULTS = {
    "enabled": False,
    "peer_uin": "",
    "peer_name": "大肥鱼",
    "initial_threshold": 0.30,
    "threshold_step": 0.12,
    "threshold_floor": 0.15,
    "threshold_ceiling": 0.95,
    "decay_messages_per_hour": 50,
    "hard_cap_per_hour": 0,
    "probe_interval_minutes": 10,
    "quiet_hours": ["01:00", "08:00"],
}

PROFILES = ("low", "standard", "remote")


def _config(ai_chat: dict | None = None) -> Config:
    """A bare Config carrying only [ai_chat] (no disk, no real profile)."""
    data = {"ai_chat": dict(ai_chat)} if ai_chat is not None else {}
    return Config(data, profile="test")


class ProfileConfigTests(unittest.TestCase):
    def test_all_profiles_declare_the_full_ai_chat_block(self) -> None:
        for profile in PROFILES:
            config = load_config(profile)
            section = config.section("ai_chat")
            self.assertEqual(
                sorted(section),
                sorted(SETTING_KEYS),
                f"{profile}: [ai_chat] must carry exactly the documented keys",
            )
            for key, expected in EXPECTED_DEFAULTS.items():
                self.assertEqual(section[key], expected, f"{profile}.{key}")

    def test_profiles_agree_on_every_ai_chat_value(self) -> None:
        # Same values in all three profiles means the profile-drift snapshot in
        # test_config_profiles.py gains no override -- deliberate, not luck.
        low = load_config("low").section("ai_chat")
        for profile in PROFILES[1:]:
            self.assertEqual(load_config(profile).section("ai_chat"), low, profile)

    def test_decay_rate_is_not_a_config_key(self) -> None:
        # It is derived from step * decay_messages_per_hour / 60. A literal
        # key is how the 12.5x error (0.008/min) got in; keep it out of config.
        for profile in PROFILES:
            self.assertNotIn(
                "threshold_decay_per_minute", load_config(profile).section("ai_chat")
            )
        self.assertNotIn("threshold_decay_per_minute", SETTING_KEYS)


class DerivedValuesTests(unittest.TestCase):
    def test_decay_per_minute_is_derived(self) -> None:
        settings = read_settings(_config(EXPECTED_DEFAULTS))
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.1)
        # 0.12 * 50 = 6.0 units of lift erased per hour.
        self.assertAlmostEqual(
            settings.threshold_decay_per_minute * 60, 6.0
        )

    def test_decay_follows_step_and_rate(self) -> None:
        settings = read_settings(
            _config(
                {
                    "threshold_step": 0.2,
                    "decay_messages_per_hour": 30,
                    "threshold_ceiling": 0.95,
                }
            )
        )
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.2 * 30 / 60)
        self.assertEqual(settings.warnings, ())

    def test_settings_are_immutable(self) -> None:
        settings = read_settings(_config(EXPECTED_DEFAULTS))
        with self.assertRaises(Exception):
            settings.threshold_step = 0.5  # type: ignore[misc]
        with self.assertRaises(Exception):
            settings.warnings = ("x",)  # type: ignore[misc]


class ActiveGatingTests(unittest.TestCase):
    def test_default_is_inactive_because_disabled(self) -> None:
        settings = read_settings(_config(EXPECTED_DEFAULTS))
        self.assertFalse(settings.active)
        self.assertEqual(settings.disabled_reason, "enabled=false")

    def test_enabled_but_empty_peer_uin_is_inactive(self) -> None:
        settings = read_settings(_config({"enabled": True, "peer_uin": ""}))
        self.assertTrue(settings.enabled)
        self.assertFalse(settings.active)
        self.assertEqual(settings.disabled_reason, "peer_uin_empty")

    def test_blank_peer_uin_is_treated_as_empty(self) -> None:
        settings = read_settings(_config({"enabled": True, "peer_uin": "   "}))
        self.assertEqual(settings.peer_uin, "")
        self.assertFalse(settings.active)
        self.assertEqual(settings.disabled_reason, "peer_uin_empty")

    def test_bound_peer_activates(self) -> None:
        settings = read_settings(_config({"enabled": True, "peer_uin": "10001"}))
        self.assertTrue(settings.active)
        self.assertEqual(settings.disabled_reason, "")
        self.assertEqual(settings.peer_uin, "10001")

    def test_numeric_peer_uin_from_toml_is_accepted(self) -> None:
        # 10001 unquoted is an int in TOML; that is a typo guard, not a config error.
        settings = read_settings(_config({"enabled": True, "peer_uin": 10001}))
        self.assertEqual(settings.peer_uin, "10001")
        self.assertEqual(settings.warnings, ())

    def test_missing_section_never_raises(self) -> None:
        settings = read_settings(_config())
        self.assertIsInstance(settings, AiChatSettings)
        self.assertFalse(settings.active)
        self.assertEqual(settings.warnings, ())


class InvalidConfigTests(unittest.TestCase):
    """Every illegal value degrades to the default with a warning -- never raises."""

    def test_step_zero_falls_back(self) -> None:
        settings = read_settings(_config({"threshold_step": 0}))
        self.assertAlmostEqual(settings.threshold_step, 0.12)
        self.assertTrue(settings.warnings)
        self.assertAlmostEqual(settings.threshold_decay_per_minute, 0.1)

    def test_step_negative_falls_back(self) -> None:
        settings = read_settings(_config({"threshold_step": -0.5}))
        self.assertAlmostEqual(settings.threshold_step, 0.12)
        self.assertTrue(settings.warnings)

    def test_floor_above_ceiling_resets_both(self) -> None:
        settings = read_settings(
            _config({"threshold_floor": 0.8, "threshold_ceiling": 0.4})
        )
        self.assertAlmostEqual(settings.threshold_floor, 0.15)
        self.assertAlmostEqual(settings.threshold_ceiling, 0.95)
        self.assertLess(settings.threshold_floor, settings.threshold_ceiling)
        self.assertTrue(settings.warnings)

    def test_ceiling_above_one_is_clamped(self) -> None:
        settings = read_settings(_config({"threshold_ceiling": 1.5}))
        self.assertAlmostEqual(settings.threshold_ceiling, 1.0)
        self.assertTrue(settings.warnings)
        self.assertLessEqual(settings.threshold_ceiling, 1.0)

    def test_non_numeric_values_fall_back(self) -> None:
        settings = read_settings(
            _config({"threshold_step": "fast", "decay_messages_per_hour": "many"})
        )
        self.assertAlmostEqual(settings.threshold_step, 0.12)
        self.assertEqual(settings.decay_messages_per_hour, 50)
        self.assertGreaterEqual(len(settings.warnings), 2)

    def test_integer_fields_are_coerced_and_bounded(self) -> None:
        settings = read_settings(
            _config(
                {
                    "decay_messages_per_hour": 50.0,
                    "hard_cap_per_hour": "7",
                    "probe_interval_minutes": 0,
                }
            )
        )
        self.assertEqual(settings.decay_messages_per_hour, 50)
        self.assertIsInstance(settings.decay_messages_per_hour, int)
        self.assertEqual(settings.hard_cap_per_hour, 7)
        # 0 would mean "probe forever"; below the floor -> default.
        self.assertEqual(settings.probe_interval_minutes, 10)
        self.assertTrue(settings.warnings)

    def test_hard_cap_zero_means_no_cap(self) -> None:
        settings = read_settings(_config({"hard_cap_per_hour": 0}))
        self.assertEqual(settings.hard_cap_per_hour, 0)
        self.assertEqual(settings.warnings, ())

    def test_initial_threshold_is_clamped_into_floor_ceiling(self) -> None:
        settings = read_settings(_config({"initial_threshold": 2.0}))
        self.assertAlmostEqual(settings.initial_threshold, 0.95)
        self.assertTrue(settings.warnings)

    def test_bad_quiet_hours_fall_back(self) -> None:
        for bad in ("25:00", ["01:00"], ["1:00", "08:00"], ["01:00", "08:00", "09:00"]):
            settings = read_settings(_config({"quiet_hours": bad}))
            self.assertEqual(settings.quiet_hours, ("01:00", "08:00"), bad)
            self.assertTrue(settings.warnings, bad)

    def test_quiet_hours_may_be_explicitly_empty(self) -> None:
        settings = read_settings(_config({"quiet_hours": []}))
        self.assertEqual(settings.quiet_hours, ())
        self.assertEqual(settings.warnings, ())

    def test_wrong_typed_section_falls_back(self) -> None:
        # `ai_chat = "nope"` in a hand-edited toml: no attribute access, no crash.
        settings = read_settings(Config({"ai_chat": "nope"}, profile="test"))
        self.assertFalse(settings.active)
        self.assertTrue(settings.warnings)

    def test_object_without_get_does_not_explode(self) -> None:
        settings = read_settings(object())
        self.assertFalse(settings.active)


class SchemaV11Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "v11.db"
        # addCleanup is LIFO: the directory must go last, after the db closes.
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.initialize()

    def test_version_and_table_and_index(self) -> None:
        self.assertEqual(SCHEMA_VERSION, "17")
        self.assertEqual(self.db.get_meta("schema_version"), SCHEMA_VERSION)
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("ai_chat_messages", tables)
        indexes = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='index'")
        }
        self.assertIn("idx_ai_chat_peer_created", indexes)

    def test_v10_database_upgrades_and_stays_idempotent(self) -> None:
        self.db.set_meta("schema_version", "10")
        self.db.execute("DROP TABLE ai_chat_messages")
        self.db.close()

        self.db = Database(self.path)
        first = self.db.migrate()
        self.assertEqual(first["schema_version"], SCHEMA_VERSION)
        self.assertIn("ai_chat_messages", first["applied"])
        self.assertEqual(self.db.get_meta("schema_version"), SCHEMA_VERSION)
        self.assertIn(
            "peer_uin",
            self.db.columns("ai_chat_messages"),
        )
        second = self.db.migrate()
        self.assertEqual(second["schema_version"], SCHEMA_VERSION)
        self.assertEqual(second["applied"], [])

    def test_audit_row_round_trips_offline(self) -> None:
        self.db.execute(
            "INSERT INTO ai_chat_messages(peer_uin, direction, content, desire, "
            "threshold, turn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("10001", "in", "你也在吗", None, None, None, 1000.0),
        )
        row = self.db.query_one(
            "SELECT * FROM ai_chat_messages WHERE peer_uin='10001' AND created_at > ?",
            (999.0,),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["direction"], "in")
        self.assertIsNone(row["desire"])


if __name__ == "__main__":
    unittest.main()
