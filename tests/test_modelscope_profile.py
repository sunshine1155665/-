"""ModelScope API-Inference profile (config/modelscope.toml).

Pins the OpenAI-compatible wiring without touching the network: the profile
must point at api-inference, inherit every non-LLM key from low, and keep
``reasoning_effort`` out of the payload (ModelScope's compat layer does not
document that field).

2026-11-12 实测：/v1/models 0.3s，Qwen/Qwen3.8-Flash-Next 一句话约 1.7s，
走 LLMClient 端到端约 2.9s（含 reasoning tokens）。
"""

from __future__ import annotations

import unittest

from fatfish.config import load_config
from fatfish.doctor import _recommend
from fatfish.llm import LLMClient

MESSAGES = [{"role": "user", "content": "hi"}]


class ModelscopeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("modelscope")

    def test_points_at_modelscope_api_inference(self) -> None:
        self.assertEqual(self.config.profile, "modelscope")
        self.assertEqual(self.config.get("app.profile"), "modelscope")
        self.assertEqual(self.config.get("llm.provider"), "modelscope")
        self.assertEqual(
            self.config.get("llm.base_url"),
            "https://api-inference.modelscope.cn/v1",
        )
        self.assertEqual(self.config.get("llm.api_key_env"), "MODELSCOPE_API_KEY")
        self.assertEqual(self.config.get("llm.api_style"), "chat_completions")
        self.assertFalse(self.config.get("llm.send_session_header"))

    def test_payload_is_plain_chat_completions(self) -> None:
        client = LLMClient(self.config, None)
        url, payload = client.build_payload(MESSAGES)
        self.assertEqual(
            url, "https://api-inference.modelscope.cn/v1/chat/completions"
        )
        # ModelScope 的兼容层不保证认 reasoning_effort：关掉即不进请求体。
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["model"], "Qwen/Qwen3.8-Flash-Next")
        headers = client.headers("sess-1")
        self.assertNotIn("x-opencode-session", headers)
        self.assertIn("authorization", headers)

    def test_inherits_low_outside_llm(self) -> None:
        low = load_config("low")
        for key in (
            "app.timezone",
            "web.host",
            "memory.embedding_dim",
            "memory.rag_top_k",
            "tts.enabled",
        ):
            self.assertEqual(self.config.get(key), low.get(key), key)

    def test_doctor_treats_it_as_an_overlay_not_a_resource_tier(self) -> None:
        self.assertEqual(_recommend(None, "modelscope"), "modelscope")
        self.assertEqual(_recommend(32 * 1024**3, "modelscope"), "modelscope")

    def test_modelscope_profile_also_switches_photo_generation(self) -> None:
        config = load_config("modelscope")
        self.assertEqual(config.get("photo.mode"), "modelscope")
        self.assertEqual(
            config.get("photo.modelscope_model"),
            "sunny1155665/whalegirl-moss-v2",
        )
        self.assertEqual(config.get("photo.modelscope_trigger"), "whalegirl")
        self.assertEqual(
            config.get("photo.modelscope_api_key_env"), "MODELSCOPE_API_KEY"
        )

    def test_moss_photo_profile_switches_only_the_photo_backend(self) -> None:
        config = load_config("moss-photo")
        self.assertEqual(config.profile, "moss-photo")
        self.assertEqual(config.get("app.profile"), "moss-photo")
        self.assertEqual(config.get("photo.mode"), "modelscope")
        # 聊天仍走 low 的 OpenCode Go，没有被 ModelScope 线路覆盖。
        self.assertEqual(config.get("llm.provider"), "opencode_go")
        self.assertEqual(config.get("llm.api_key_env"), "FATFISH_OPENCODE_KEY")
        self.assertEqual(_recommend(None, "moss-photo"), "moss-photo")
        self.assertEqual(_recommend(32 * 1024**3, "moss-photo"), "moss-photo")


if __name__ == "__main__":
    unittest.main()
