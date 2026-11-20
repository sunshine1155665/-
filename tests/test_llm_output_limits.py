"""Output-cap observability: ``finish_reason``, truncation metrics, photo cap.

Three things are pinned here:

1. ``LLMResult.finish_reason`` — extracted from Chat Completions
   ``choices[0].finish_reason`` and, for Responses-style payloads, from
   ``status`` / ``incomplete_details``; anything weird degrades to ``""``.
2. ``finish_reason == "length"`` records the ``llm_output_truncated`` metric
   (and other reasons do not).
3. ``PhotoService.build_prompt`` asks for 10000 output tokens instead of the
   old 900, so a reasoning model cannot eat the whole budget before writing
   the JSON prompt.

No network and no real ``data/``: the HTTP seam is a fake response and every
database lives in a TemporaryDirectory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fatfish.config import Config
from fatfish.db import Database
from fatfish.llm import LLMClient, LLMResult, LLMUsage
from fatfish.photo import PhotoService

MESSAGES = [{"role": "user", "content": "hi"}]
PROMPT_JSON = json.dumps(
    {
        "composition": "character",
        "outfit": "default",
        "outfit_detail": "",
        "positive": (
            "selfie, whale girl, blue hair, very long wavy hair, blue eyes, "
            "ahoge, whale tail, maid dress"
        ),
        "negative": "blurry, bad hands",
        "caption": "自拍",
    },
    ensure_ascii=False,
)


class _FakeResponse:
    """Just enough of an ``http.client.HTTPResponse`` for ``LLMClient.chat``."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _make_config(data_dir: Path, *, api_style: str = "chat_completions") -> Config:
    return Config(
        {
            "app": {"data_dir": str(data_dir)},
            "storage": {"db_path": str(data_dir / "test.db")},
            "llm": {
                "provider": "opencode_go",
                "base_url": "https://example.invalid/v1",
                "model": "test-model",
                "api_key_env": "FATFISH_TEST_KEY",
                "api_style": api_style,
                "max_output_tokens": 800,
                "temperature": 0.8,
                "reasoning_effort": "low",
                "keepalive": False,
            },
        },
        profile="test",
    )


def _make_env(root: Path, **kwargs: object) -> tuple[Config, Database]:
    config = _make_config(root / "data", **kwargs)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def _make_client(config: Config, db: Database) -> LLMClient:
    client = LLMClient(config, db)
    client.api_key = "test-key"
    # Force the urllib fallback; the keep-alive pool has its own test and this
    # keeps the HTTP seam a single ``urlopen`` to mock.
    client.keepalive = False
    return client


def _chat(client: LLMClient, payload: dict) -> LLMResult:
    with mock.patch(
        "fatfish.llm.urllib.request.urlopen", return_value=_FakeResponse(payload)
    ):
        result = client.chat(MESSAGES)
    assert result is not None
    return result


class FinishReasonExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db = _make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)

    def _client(self, *, api_style: str = "chat_completions") -> LLMClient:
        client = _make_client(self.config, self.db)
        client.api_style = api_style
        return client

    def test_chat_completions_finish_reason_is_exposed(self) -> None:
        result = _chat(
            _make_client(self.config, self.db),
            {
                "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage.completion_tokens, 3)

    def test_missing_finish_reason_degrades_to_empty_string(self) -> None:
        for payload in (
            {"choices": [{"message": {"content": "no reason"}}]},
            {"choices": []},
            {"choices": [{"finish_reason": None, "message": {"content": "x"}}]},
            {"output_text": "responses style without status"},
        ):
            with self.subTest(payload=payload):
                result = _chat(_make_client(self.config, self.db), payload)
                self.assertEqual(result.finish_reason, "")

    def test_non_string_finish_reason_degrades_to_empty_string(self) -> None:
        for bogus in (42, {"reason": "length"}, ["length"]):
            with self.subTest(bogus=bogus):
                result = _chat(
                    _make_client(self.config, self.db),
                    {"choices": [{"finish_reason": bogus, "message": {"content": "x"}}]},
                )
                self.assertEqual(result.finish_reason, "")

    def test_responses_incomplete_max_output_tokens_maps_to_length(self) -> None:
        result = _chat(
            self._client(api_style="responses"),
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": "partial answer"}
                        ]
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 4000},
            },
        )
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.text, "partial answer")

    def test_responses_completed_maps_to_stop(self) -> None:
        result = _chat(
            self._client(api_style="responses"),
            {"status": "completed", "output_text": "done"},
        )
        self.assertEqual(result.finish_reason, "stop")

    def test_responses_unknown_incomplete_reason_is_preserved(self) -> None:
        result = _chat(
            self._client(api_style="responses"),
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output_text": "",
            },
        )
        self.assertEqual(result.finish_reason, "content_filter")

    def test_responses_incomplete_without_details_is_safe(self) -> None:
        result = _chat(
            self._client(api_style="responses"),
            {"status": "incomplete", "output_text": ""},
        )
        self.assertEqual(result.finish_reason, "incomplete")

    def test_llm_result_default_keeps_old_construction_compatible(self) -> None:
        self.assertEqual(LLMResult(text="x").finish_reason, "")
        self.assertEqual(LLMResult(text="x", finish_reason="length").finish_reason, "length")
        result = LLMResult(
            text="y",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=2),
            latency_ms=1.5,
            model="m",
            raw_usage={},
        )
        self.assertEqual(result.finish_reason, "")


class TruncationMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db = _make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)

    def _rows(self) -> list:
        return self.db.query(
            "SELECT value, tags_json FROM metrics WHERE key=?",
            ("llm_output_truncated",),
        )

    def test_length_records_truncation_metric(self) -> None:
        result = _chat(
            _make_client(self.config, self.db),
            {
                "choices": [{"finish_reason": "length", "message": {"content": "cut"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4000},
            },
        )
        self.assertEqual(result.finish_reason, "length")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["value"]), 1.0)
        tags = json.loads(rows[0]["tags_json"])
        self.assertEqual(tags["model"], "test-model")
        self.assertEqual(tags["provider"], "opencode_go")

    def test_responses_length_also_records_truncation_metric(self) -> None:
        client = _make_client(self.config, self.db)
        client.api_style = "responses"
        result = _chat(
            client,
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "cut",
            },
        )
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(len(self._rows()), 1)

    def test_stop_does_not_record_truncation_metric(self) -> None:
        _chat(
            _make_client(self.config, self.db),
            {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )
        self.assertEqual(self._rows(), [])

    def test_missing_finish_reason_does_not_record_truncation_metric(self) -> None:
        _chat(
            _make_client(self.config, self.db),
            {"choices": [{"message": {"content": "ok"}}]},
        )
        self.assertEqual(self._rows(), [])


class _CapturingLLM:
    """Stub LLM that records the kwargs ``build_prompt`` passes to ``chat``."""

    available = True

    def __init__(self, text: str = PROMPT_JSON):
        self.text = text
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            model="fake-model",
        )


def _make_photo_config(data_dir: Path, mode: str) -> Config:
    return Config(
        {
            "app": {"data_dir": str(data_dir)},
            "storage": {"db_path": str(data_dir / "photo-test.db")},
            "photo": {
                "enabled": True,
                "mode": mode,
                # Keep this test independent of the on-disk skill file.
                "skill_path": "",
            },
        },
        profile="test",
    )


class PhotoPromptOutputCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        db = Database(self.data_dir / "photo-test.db")
        db.initialize()
        self.addCleanup(db.close)
        self.db = db

    def test_build_prompt_requests_10000_tokens(self) -> None:
        for mode in ("local", "modelscope"):
            with self.subTest(mode=mode):
                config = _make_photo_config(self.data_dir, mode)
                config.ensure_dirs()
                llm = _CapturingLLM()
                service = PhotoService(config, self.db, llm=llm)
                prompt = service.build_prompt("拍张自拍给我看看")
                self.assertEqual(prompt.source, "llm")
                self.assertEqual(len(llm.calls), 1)
                self.assertEqual(llm.calls[0]["kwargs"]["max_tokens"], 10000)
                self.assertNotEqual(llm.calls[0]["kwargs"]["max_tokens"], 900)
                self.assertEqual(llm.calls[0]["kwargs"]["temperature"], 0.7)

    def test_build_prompt_falls_back_when_llm_is_none(self) -> None:
        config = _make_photo_config(self.data_dir, "local")
        config.ensure_dirs()
        service = PhotoService(config, self.db, llm=None)
        prompt = service.build_prompt("拍张自拍给我看看")
        self.assertEqual(prompt.source, "fallback")


if __name__ == "__main__":
    unittest.main()
