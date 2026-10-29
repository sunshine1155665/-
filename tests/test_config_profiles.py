"""T-0030: profile drift guard.

The shipped profiles (low/standard/remote) merge over low.toml, and keys
copied between them have drifted before (llm.user_agent sat at 0.2.0 in low
while standard/remote carried 0.1.0). This module pins each profile's
effective override set as a snapshot: adding an override is fine, but it has
to be a conscious edit of the snapshot below, never a silent copy-paste.
The physical base/profile split (config/base.toml) stays a follow-up -- the
guard already turns silent drift into a red test.
"""

from __future__ import annotations

import unittest

from fatfish import __version__
from fatfish.config import _deep_merge, _read_toml, load_config
from fatfish.config import PROJECT_ROOT


def _flatten(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{dotted}."))
        else:
            out[dotted] = value
    return out


def _overrides_over_low(profile: str) -> list[str]:
    """Dotted keys whose effective value the profile changes vs low.toml."""
    low = _read_toml(PROJECT_ROOT / "config" / "low.toml")
    selected = _read_toml(PROJECT_ROOT / "config" / f"{profile}.toml")
    merged = _deep_merge(low, selected)
    low_flat = _flatten(low)
    merged_flat = _flatten(merged)
    return sorted(k for k, v in merged_flat.items() if low_flat.get(k) != v)


class ProfileSnapshotTests(unittest.TestCase):
    def test_standard_overrides(self) -> None:
        # Snapshot of standard.toml's effective changes vs low (2026-10-30,
        # local paths in tts/photo deliberately emptied for the public release).
        # Adding/removing an override is fine -- do it consciously, here too.
        self.assertEqual(
            _overrides_over_low("standard"),
            [
                "app.profile",
                "bilibili.max_videos_per_day",
                "decision.backend",
                "decision.kev_model",
                "decision.kev_timeout_seconds",
                "life.default_energy",
                "life.tick_seconds",
                "llm.max_output_tokens",
                "llm.timeout_seconds",
                "memory.candidate_limit",
                "memory.embedding_dim",
                "memory.max_items_per_search",
                "memory.nightly_voice_max_tokens",
                "memory.rag_threshold",
                "memory.rag_top_k",
                "memory.recall_kev_threshold",
                "memory.recall_threshold",
                "memory.recall_top_k",
                "photo.min_free_vram_mb",
                "qq.enabled",
                "sticker.comfyui_enabled",
                "sticker.intent_cooldown_seconds",
                "sticker.intent_threshold",
                "sticker.max_candidates",
                "tts.timeout_seconds",
                "voice.max_per_day",
                "voice.max_per_hour",
                "voice.min_score",
                "voice.provider",
                "voice.text_with_voice_min",
                "wakeup.max_chain_depth",
                # 2026-11-01 config(wakeup) 把 low 的每日上限抬到 24；
                # standard 仍是 12，所以 max_per_day 现在是真实 override。
                "wakeup.max_per_day",
                "wakeup.proactive_min_interval_minutes",
            ],
        )

    def test_remote_overrides(self) -> None:
        self.assertEqual(
            _overrides_over_low("remote"),
            [
                "app.profile",
                "decision.backend",
                "decision.kev_base_url",
                "decision.kev_model",
                "decision.kev_timeout_seconds",
                "llm.api_key_env",
                "llm.base_url",
                "llm.model",
                "llm.provider",
                "llm.send_session_header",
                "memory.candidate_limit",
                "memory.embedding_backend",
                "memory.embedding_dim",
                "memory.max_items_per_search",
                "memory.nightly_voice_max_tokens",
                "memory.rag_threshold",
                "memory.rag_top_k",
                "memory.recall_kev_threshold",
                "memory.recall_threshold",
                "memory.recall_top_k",
                "photo.min_free_vram_mb",
                "qq.enabled",
                "sticker.comfyui_url",
                "sticker.embedding_backend",
                "sticker.similarity_ceil",
                "sticker.similarity_floor",
                "tts.base_url",
                "tts.enabled",
                "tts.mode",
                "tts.timeout_seconds",
                "voice.max_per_day",
                "voice.max_per_hour",
                "voice.min_score",
                "voice.provider",
                "voice.text_with_voice_min",
                # 同 standard：low 上限 24，remote 12/90 是真实 override。
                "wakeup.max_per_day",
                "wakeup.proactive_min_interval_minutes",
            ],
        )

    def test_cloud_overrides(self) -> None:
        # cloud.toml 是「在线层」的最小覆盖层（ADR-0012）：只关掉必须 GPU
        # 的能力，其余全部继承 low。和 standard/remote 同一规矩——新增覆盖
        # 必须在这里同步，不能悄悄复制粘贴。
        self.assertEqual(
            _overrides_over_low("cloud"),
            [
                "app.profile",
                "photo.mode",
                "photo.remote_timeout_seconds",
                "photo.remote_token_env",
                "photo.remote_url",
                "qzone.enabled",
                # 云端必须有探针（T-0069）：没有它，家里关机时每句话都要
                # 等满 tts.timeout_seconds 才失败，而不是秒回文字。
                "remote.enabled",
                "remote.health_ttl_seconds",
                "remote.timeout_seconds",
                "sing.mode",
                "sing.remote_timeout_seconds",
                "sing.remote_token_env",
                "sing.remote_url",
                "sticker.embedding_backend",
                "tts.enabled",
                "voice.enabled",
            ],
        )

    def test_cloud_overrides(self) -> None:
        # cloud.toml 是「在线层」的最小覆盖层（ADR-0012）：只关掉必须 GPU
        # 的能力，其余全部继承 low。和 standard/remote 同一规矩——新增覆盖
        # 必须在这里同步，不能悄悄复制粘贴。
        self.assertEqual(
            _overrides_over_low("cloud"),
            [
                "app.profile",
                "photo.mode",
                "photo.remote_timeout_seconds",
                "photo.remote_token_env",
                "photo.remote_url",
                "qzone.enabled",
                "remote.enabled",
                "remote.health_ttl_seconds",
                "remote.timeout_seconds",
                "sing.mode",
                "sing.remote_timeout_seconds",
                "sing.remote_token_env",
                "sing.remote_url",
                "sticker.embedding_backend",
                "tts.enabled",
                "voice.enabled",
            ],
        )


class CloudProfileTests(unittest.TestCase):
    """cloud = 在线层部署形态（ADR-0012），不是第四个资源档位。"""

    def test_gpu_features_are_off(self) -> None:
        config = load_config("cloud")
        for key in (
            "tts.enabled",
            "voice.enabled",
            "sing.enabled",
            "photo.enabled",
            "qzone.enabled",
        ):
            self.assertFalse(config.get(key), key)
        self.assertEqual(config.get("sticker.embedding_backend"), "hash")

    def test_doctor_does_not_second_guess_the_online_tier(self) -> None:
        from fatfish.doctor import _recommend

        # 2C2G 的云主机上 doctor 不该提示「建议换成 low」。
        self.assertEqual(_recommend(2 * 1024**3, "cloud"), "cloud")
        self.assertEqual(_recommend(None, "cloud"), "cloud")


class CloudProfileTests(unittest.TestCase):
    """cloud = 在线层部署形态（ADR-0012），不是第四个资源档位。"""

    def test_gpu_features_are_off(self) -> None:
        config = load_config("cloud")
        for key in (
            "tts.enabled",
            "voice.enabled",
            "qzone.enabled",
        ):
            self.assertFalse(config.get(key), key)
        self.assertEqual(config.get("sticker.embedding_backend"), "hash")
        # 唱歌是例外（T-0067）：云端不渲染，但走家里的 sing_server，所以
        # enabled 从 low 继承 true，真正决定「唱不唱得成」的是家里在不在。
        self.assertTrue(config.get("sing.enabled"))
        self.assertEqual(config.get("sing.mode"), "remote")
        self.assertTrue(str(config.get("sing.remote_url") or "").startswith("https://"))
        # 拍照同理（T-0068）：云端没有 ComfyUI，图在家里画。
        self.assertTrue(config.get("photo.enabled"))
        self.assertEqual(config.get("photo.mode"), "remote")
        self.assertTrue(str(config.get("photo.remote_url") or "").startswith("https://"))
        self.assertGreaterEqual(config.get("photo.remote_timeout_seconds"), 120)
        # 能力层探针默认开（T-0069）：云端唯一的「家里还在不在」判断依据。
        self.assertTrue(config.get("remote.enabled"))
        self.assertLessEqual(config.get("remote.timeout_seconds"), 5)

    def test_doctor_does_not_second_guess_the_online_tier(self) -> None:
        from fatfish.doctor import _recommend

        # 2C2G 的云主机上 doctor 不该提示「建议换成 low」。
        self.assertEqual(_recommend(2 * 1024**3, "cloud"), "cloud")
        self.assertEqual(_recommend(None, "cloud"), "cloud")


class ProfileInvariantTests(unittest.TestCase):
    def test_llm_user_agent_is_single_sourced(self) -> None:
        # The profiles no longer pin a literal: the default must come from
        # the package version, for every profile AND for a bare config.
        from fatfish.config import Config
        from fatfish.llm import LLMClient

        for profile in ("low", "standard", "remote"):
            config = load_config(profile)
            client = LLMClient(config, db=None)
            self.assertEqual(
                client.user_agent,
                f"fatfish/{__version__}",
                f"{profile}: llm.user_agent must come from the package version",
            )
        bare = LLMClient(Config({"llm": {}}, profile="test"), db=None)
        self.assertEqual(bare.user_agent, f"fatfish/{__version__}")

    def test_load_bearing_keys_are_stable(self) -> None:
        for profile in ("low", "standard", "remote"):
            config = load_config(profile)
            self.assertEqual(config.get("app.name"), "fatfish", profile)
            self.assertEqual(config.get("web.host"), "127.0.0.1", profile)
            self.assertEqual(config.get("app.timezone"), "Asia/Shanghai", profile)
            # quiet hours are a behavior contract shared by memory/wakeup/share
            self.assertEqual(config.get("memory.quiet_start"), "23:30", profile)
            self.assertEqual(config.get("memory.quiet_end"), "08:00", profile)

    def test_cloud_profile_inherits_the_online_tier(self) -> None:
        config = load_config("cloud")
        self.assertEqual(config.profile, "cloud")
        self.assertEqual(config.get("app.profile"), "cloud")
        # 继承 low 的接入层：本机回环 + 本地 NapCat，不另开公网端口。
        self.assertEqual(config.get("web.host"), "127.0.0.1")
        self.assertEqual(config.get("qq.ws_url"), "ws://127.0.0.1:3001")
        # 继承 low 的 LLM：云端不另起一套 provider。
        self.assertEqual(config.get("llm.provider"), "opencode_go")

    def test_profiles_parse_and_declare_themselves(self) -> None:
        for profile in ("low", "standard", "remote"):
            config = load_config(profile)
            self.assertEqual(config.profile, profile)
            self.assertEqual(config.get("app.profile"), profile)


if __name__ == "__main__":
    unittest.main()
