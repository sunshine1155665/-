"""T-0052: 三域隔离 —— 数学共用，状态和配置各自独立。

父卡（T-0042）的定调是「AI 会话、AI 聊天、AI 自己搜索的 3 个值互相独立，但本质
类似，都是软限制」。这组用例是这一层唯一真正的验收证据：**同一个数据库**里

1. 把 ``ai_chat`` 的阈值连续抬到天花板，``self_action`` 读出来仍是自己的
   ``initial_threshold``（反过来也一样）；
2. 两个域的 settings 各读各的段，改一段不影响另一段；
3. ``ai_chat`` 因为 ``peer_uin`` 为空 / ``enabled=false`` 关掉时，
   ``self_action`` 的 ``active`` 不受影响；
4. 三个域的状态键互不覆盖（写一个，另一个的内容一字不变）。

状态一律用临时库（``tempfile``），不碰真实的 ``data/fatfish.db``，也不联网。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.ai_chat.settings import read_settings as read_ai_chat_settings
from fatfish.config import Config
from fatfish.db import Database
from fatfish.desire import engine
from fatfish.desire.settings import SELF_ACTION_DEFAULTS, read_desire_settings
from fatfish.desire.state import state_key

T0 = 1_700_000_000.0
PEER = "2002"
SCOPE = "qq:private:10001"

AI_KEY = state_key("ai_chat", PEER)
SELF_KEY = state_key("self_action", SCOPE)
OWNER_KEY = state_key("owner_chat", "owner")


def _config(**sections: dict) -> Config:
    return Config({name: dict(body) for name, body in sections.items()}, profile="test")


def ai_chat_settings(**overrides):
    section = {"enabled": True, "peer_uin": PEER}
    section.update(overrides)
    return read_ai_chat_settings(_config(ai_chat=section))


def self_action_settings(**overrides):
    section = {"enabled": True}
    section.update(overrides)
    return read_desire_settings(
        _config(self_action=section), "self_action", defaults=SELF_ACTION_DEFAULTS
    )


class IsolationBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "isolation.db")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.ai = ai_chat_settings()
        self.self_action = self_action_settings()

    # ---------------------------------------------------------- state helpers
    def load(self, key: str, settings):
        """Read a domain's persisted state; a missing key means "never acted"."""
        raw = self.db.get_meta(key, None)
        if raw is None:
            return engine.read_state(None, settings)
        return engine.read_state(json.loads(raw), settings)

    def save(self, key: str, state) -> None:
        self.db.set_meta(key, json.dumps(state.to_dict()))

    def raise_to_ceiling(self, settings, key: str, *, now: float = T0) -> None:
        """Drive one domain's threshold to its ceiling with repeated events."""
        state = self.load(key, settings)
        for _ in range(50):
            state = engine.on_sent(settings, state, 1.0, now)
        self.save(key, state)
        self.assertEqual(
            self.load(key, settings).threshold,
            settings.threshold_ceiling,
            "precondition: the domain must really be pinned at its ceiling",
        )


class ThresholdIndependenceTests(IsolationBase):
    def test_lifting_ai_chat_does_not_move_self_action(self) -> None:
        self.raise_to_ceiling(self.ai, AI_KEY)
        # self_action never acted, so its own state stays at its own initial value.
        self.assertAlmostEqual(
            engine.current_threshold(self.self_action, self.load(SELF_KEY, self.self_action), T0),
            0.10,
            places=12,
        )
        # ... and it can still act: the ai_chat ceiling is irrelevant here.
        self.assertEqual(
            engine.can_proactive_send(
                self.self_action, self.load(SELF_KEY, self.self_action), 0.16, T0, 0
            ),
            (True, engine.REASON_OK),
        )
        # Whereas ai_chat itself is now blocked at 0.36 < 0.95.
        self.assertEqual(
            engine.can_proactive_send(
                self.ai, self.load(AI_KEY, self.ai), 0.36, T0, 0
            ),
            (False, engine.REASON_BELOW_THRESHOLD),
        )

    def test_lifting_self_action_does_not_move_ai_chat(self) -> None:
        self.raise_to_ceiling(self.self_action, SELF_KEY)
        self.assertAlmostEqual(
            engine.current_threshold(self.ai, self.load(AI_KEY, self.ai), T0),
            0.30,
            places=12,
        )
        self.assertEqual(
            engine.can_proactive_send(self.ai, self.load(AI_KEY, self.ai), 0.30, T0, 0),
            (True, engine.REASON_OK),
        )
        self.assertEqual(
            engine.can_proactive_send(
                self.self_action, self.load(SELF_KEY, self.self_action), 0.30, T0, 0
            ),
            (False, engine.REASON_BELOW_THRESHOLD),
        )

    def test_both_domains_at_ceiling_decay_at_their_own_rate(self) -> None:
        self.raise_to_ceiling(self.ai, AI_KEY)
        self.raise_to_ceiling(self.self_action, SELF_KEY)
        ai_threshold = engine.current_threshold(
            self.ai, self.load(AI_KEY, self.ai), T0 + 120
        )
        sa_threshold = engine.current_threshold(
            self.self_action, self.load(SELF_KEY, self.self_action), T0 + 120
        )
        # ai_chat 0.1/min -> 0.95 - 0.2 = 0.75; self_action 1/30/min -> 0.90 - 0.1
        # = 0.84: same maths, different per-domain config, no cross-talk.
        self.assertAlmostEqual(ai_threshold, 0.75, places=12)
        self.assertAlmostEqual(sa_threshold, 0.9 - 2/30, places=12)
        self.assertAlmostEqual(self.ai.threshold_decay_per_minute, 0.1, places=12)
        self.assertAlmostEqual(
            self.self_action.threshold_decay_per_minute, 1/30, places=12
        )

    def test_lifting_ai_chat_never_touches_the_self_action_key(self) -> None:
        self.raise_to_ceiling(self.self_action, SELF_KEY)
        before = self.db.get_meta(SELF_KEY)
        self.raise_to_ceiling(self.ai, AI_KEY)
        self.assertEqual(self.db.get_meta(SELF_KEY), before)
        self.assertIsNotNone(self.db.get_meta(AI_KEY, None))


class SettingsIndependenceTests(unittest.TestCase):
    def test_each_domain_reads_its_own_section(self) -> None:
        config = _config(
            ai_chat={"enabled": True, "peer_uin": PEER, "threshold_step": 0.12},
            self_action={"enabled": True, "threshold_step": 0.10},
        )
        ai = read_ai_chat_settings(config)
        sa = read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS)
        self.assertAlmostEqual(ai.threshold_step, 0.12)
        self.assertAlmostEqual(sa.threshold_step, 0.10)

    def test_changing_self_action_step_does_not_touch_ai_chat(self) -> None:
        config = _config(
            ai_chat={"enabled": True, "peer_uin": PEER},
            self_action={"enabled": True, "threshold_step": 0.50},
        )
        self.assertAlmostEqual(
            read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS).threshold_step,
            0.50,
        )
        self.assertAlmostEqual(read_ai_chat_settings(config).threshold_step, 0.12)

    def test_changing_ai_chat_thresholds_does_not_touch_self_action(self) -> None:
        config = _config(
            ai_chat={
                "enabled": True,
                "peer_uin": PEER,
                "initial_threshold": 0.80,
                "threshold_step": 0.02,
                "decay_messages_per_hour": 600,
            },
            self_action={"enabled": True},
        )
        ai = read_ai_chat_settings(config)
        sa = read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS)
        self.assertAlmostEqual(ai.initial_threshold, 0.80)
        self.assertAlmostEqual(ai.threshold_decay_per_minute, 0.2, places=12)
        self.assertAlmostEqual(sa.initial_threshold, 0.10)
        self.assertAlmostEqual(sa.threshold_step, 0.05)
        self.assertAlmostEqual(sa.threshold_decay_per_minute, 1/30, places=12)

    def test_disabled_ai_chat_does_not_disable_self_action(self) -> None:
        config = _config(
            ai_chat={"enabled": False, "peer_uin": PEER},
            self_action={"enabled": True},
        )
        ai = read_ai_chat_settings(config)
        sa = read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS)
        self.assertFalse(ai.active)
        self.assertEqual(ai.disabled_reason, "enabled=false")
        self.assertTrue(sa.active)
        self.assertEqual(sa.disabled_reason, "")

    def test_empty_peer_uin_does_not_disable_self_action(self) -> None:
        config = _config(
            ai_chat={"enabled": True, "peer_uin": ""},
            self_action={"enabled": True},
        )
        ai = read_ai_chat_settings(config)
        sa = read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS)
        self.assertFalse(ai.active)
        self.assertEqual(ai.disabled_reason, "peer_uin_empty")
        self.assertTrue(sa.active)

    def test_self_action_disabled_does_not_disable_ai_chat(self) -> None:
        config = _config(
            ai_chat={"enabled": True, "peer_uin": PEER},
            self_action={"enabled": False},
        )
        self.assertTrue(read_ai_chat_settings(config).active)
        self.assertFalse(
            read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS).active
        )

    def test_shipped_profiles_keep_the_three_sections_apart(self) -> None:
        # 三套 profile 的 [ai_chat] 与 [self_action] 必须各自读出来自不同的段。
        from fatfish.config import load_config

        for profile in ("low", "standard", "remote"):
            config = load_config(profile)
            ai = read_ai_chat_settings(config)
            sa = read_desire_settings(config, "self_action", defaults=SELF_ACTION_DEFAULTS)
            self.assertAlmostEqual(ai.initial_threshold, 0.30, msg=profile)
            self.assertAlmostEqual(sa.initial_threshold, 0.10, msg=profile)
            self.assertAlmostEqual(ai.threshold_decay_per_minute, 0.1, msg=profile)
            self.assertAlmostEqual(sa.threshold_decay_per_minute, 1/30, msg=profile)


class StateKeyTests(IsolationBase):
    def test_three_domains_share_one_db_without_overwriting(self) -> None:
        ai_state = engine.DesireState(threshold=0.80, updated_at=T0, last_desire=0.9)
        sa_state = engine.DesireState(threshold=0.60, updated_at=T0, last_desire=0.7)
        owner_state = engine.DesireState(threshold=0.40, updated_at=T0, last_desire=0.5)
        self.save(AI_KEY, ai_state)
        self.save(SELF_KEY, sa_state)
        self.save(OWNER_KEY, owner_state)

        self.assertEqual(self.load(AI_KEY, self.ai), ai_state)
        self.assertEqual(self.load(SELF_KEY, self.self_action), sa_state)
        self.assertEqual(self.load(OWNER_KEY, self.self_action), owner_state)

        # Overwriting one key leaves the other two byte-identical.
        before = {key: self.db.get_meta(key) for key in (SELF_KEY, OWNER_KEY)}
        self.save(AI_KEY, engine.DesireState(threshold=0.55, updated_at=T0 + 10))
        for key, raw in before.items():
            self.assertEqual(self.db.get_meta(key), raw, key)

    def test_state_keys_are_distinct(self) -> None:
        self.assertEqual(AI_KEY, f"ai_chat:state:{PEER}")
        self.assertEqual(SELF_KEY, f"self_action:state:{SCOPE}")
        self.assertEqual(OWNER_KEY, "owner_chat:state:owner")
        self.assertEqual(len({AI_KEY, SELF_KEY, OWNER_KEY}), 3)

    def test_ai_chat_state_key_matches_the_service_convention(self) -> None:
        # AiChatService writes f"ai_chat:state:{peer_uin}"; the generic helper
        # must produce exactly the same string or T-0041's state would vanish.
        from fatfish.ai_chat.service import STATE_KEY_PREFIX

        self.assertEqual(AI_KEY, f"{STATE_KEY_PREFIX}{PEER}")

    def test_a_domain_without_state_reads_its_own_initial_threshold(self) -> None:
        self.save(AI_KEY, engine.DesireState(threshold=0.95, updated_at=T0))
        self.assertEqual(self.load(SELF_KEY, self.self_action).threshold, 0.10)
        self.assertEqual(self.load(OWNER_KEY, self.self_action).threshold, 0.10)
        # ai_chat's own fallback stays 0.30 -- defaults are per-domain too.
        self.assertEqual(engine.read_state(None, self.ai).threshold, 0.30)


if __name__ == "__main__":
    unittest.main()
