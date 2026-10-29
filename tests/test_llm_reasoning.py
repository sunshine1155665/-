from __future__ import annotations

import unittest

from fatfish.config import Config, load_config
from fatfish.llm import LLMClient, normalize_reasoning_effort

MESSAGES = [{"role": "user", "content": "hi"}]


def make_config(**llm: object) -> Config:
    section = {
        "provider": "opencode_go",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "deepseek-v4.1-flash",
        "api_key_env": "FATFISH_TEST_KEY",
        "api_style": "chat_completions",
        "max_output_tokens": 800,
        "temperature": 0.8,
    }
    section.update(llm)
    return Config({"llm": section}, profile="test")


def make_client(**llm: object) -> LLMClient:
    return LLMClient(make_config(**llm), None)


class ReasoningEffortTests(unittest.TestCase):
    def test_chat_completions_includes_low_effort(self) -> None:
        client = make_client(reasoning_effort="low")
        url, payload = client.build_payload(MESSAGES)
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("reasoning", payload)

    def test_responses_includes_reasoning_effort_object(self) -> None:
        client = make_client(reasoning_effort="low", api_style="responses")
        url, payload = client.build_payload(MESSAGES)
        self.assertTrue(url.endswith("/responses"))
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning_effort", payload)

    def test_missing_config_defaults_to_low(self) -> None:
        client = make_client()
        _, chat_payload = client.build_payload(MESSAGES)
        self.assertEqual(chat_payload["reasoning_effort"], "low")

        client.api_style = "responses"
        _, responses_payload = client.build_payload(MESSAGES)
        self.assertEqual(responses_payload["reasoning"], {"effort": "low"})

    def test_empty_and_off_omit_the_field(self) -> None:
        for value in ("", "   ", "off", "OFF"):
            with self.subTest(value=value):
                client = make_client(reasoning_effort=value)
                _, chat_payload = client.build_payload(MESSAGES)
                self.assertNotIn("reasoning_effort", chat_payload)

                client.api_style = "responses"
                _, responses_payload = client.build_payload(MESSAGES)
                self.assertNotIn("reasoning", responses_payload)

    def test_normalizes_supported_values_and_unknown_to_low(self) -> None:
        self.assertEqual(normalize_reasoning_effort("low"), "low")
        self.assertEqual(normalize_reasoning_effort(" Medium "), "medium")
        self.assertEqual(normalize_reasoning_effort("HIGH"), "high")
        self.assertEqual(normalize_reasoning_effort(""), "")
        self.assertEqual(normalize_reasoning_effort("off"), "")
        self.assertEqual(normalize_reasoning_effort("none"), "low")
        self.assertEqual(normalize_reasoning_effort("turbo"), "low")
        self.assertEqual(normalize_reasoning_effort(None), "")

    def test_explicit_medium_and_high_reach_both_payload_styles(self) -> None:
        client = make_client(reasoning_effort="medium")
        _, chat_payload = client.build_payload(MESSAGES)
        self.assertEqual(chat_payload["reasoning_effort"], "medium")

        client.api_style = "responses"
        _, responses_payload = client.build_payload(MESSAGES)
        self.assertEqual(responses_payload["reasoning"], {"effort": "medium"})

        client = make_client(reasoning_effort="high")
        _, chat_payload = client.build_payload(MESSAGES)
        self.assertEqual(chat_payload["reasoning_effort"], "high")

    def test_max_tokens_and_temperature_defaults_unchanged(self) -> None:
        client = make_client()
        _, payload = client.build_payload(MESSAGES)
        self.assertEqual(payload["max_tokens"], 800)
        self.assertEqual(payload["temperature"], 0.8)

        _, overridden = client.build_payload(MESSAGES, max_tokens=200, temperature=0.3)
        self.assertEqual(overridden["max_tokens"], 200)
        self.assertEqual(overridden["temperature"], 0.3)

        client.api_style = "responses"
        _, responses_payload = client.build_payload(MESSAGES)
        self.assertEqual(responses_payload["max_output_tokens"], 800)
        self.assertEqual(responses_payload["temperature"], 0.8)

    def test_shipped_profiles_enable_low_reasoning(self) -> None:
        for profile in ("low", "standard", "remote"):
            with self.subTest(profile=profile):
                config = load_config(profile)
                self.assertEqual(config.get("llm.reasoning_effort"), "low")
                client = LLMClient(config, None)
                _, payload = client.build_payload(MESSAGES)
                self.assertEqual(payload["reasoning_effort"], "low")


if __name__ == "__main__":
    unittest.main()
