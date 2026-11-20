"""Tests for the sticker ``intensity`` knob and the "never eat the words" rule.

``TOOL_SCHEMA`` tells the model it may pass ``intensity`` (1-5). It used to be
dropped on the floor by ``execute_actions``, which is worse than not offering
it: the model believes it asked for "keep it mild" and the runtime silently
did nothing.  Rendering is async now (job queued, image delivered later), but
the parameter still has to reach the service and come back in the result.

There is deliberately **no** ``mode`` knob here. ``standalone`` ("just the
picture, no words") was implemented and then removed: a sticker is the tone and
the words are the content, so letting the model delete the words risks a turn
where the user receives nothing at all. Text is never suppressed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.runtime import Runtime, _sticker_intensity
from fatfish.sticker.service import StickerDecision

OWNER_SESSION = "qq:private:10001"


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'sticker.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[voice]
enabled = false

[tts]
enabled = false

[sticker]
generation_enabled = false

[proactive_media]
photo_enabled = true
sticker_enabled = true
photo_max_per_day = 0
sticker_max_per_day = 0
photo_min_interval_minutes = 0
sticker_min_interval_minutes = 0
quiet_hours = []
"""
    path = tmp / "sticker.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


def fake_reply(runtime: Runtime, text: str) -> None:
    runtime.llm.api_key = "fake"
    runtime.llm.chat = lambda messages, **kwargs: LLMResult(
        text=text,
        usage=LLMUsage(prompt_tokens=10, completion_tokens=3),
        model="fake-model",
    )


def plan(intensity=None, messages=("怎么啦…",)) -> str:
    args: dict = {"need": "有点想哭"}
    if intensity is not None:
        args["intensity"] = intensity
    return json.dumps(
        {"messages": list(messages), "actions": [{"tool": "send_sticker", "args": args}]},
        ensure_ascii=False,
    )


class IntensityParsingTests(unittest.TestCase):
    def test_clamped_to_1_5(self) -> None:
        self.assertEqual(_sticker_intensity(1), 1.0)
        self.assertEqual(_sticker_intensity(5), 5.0)
        self.assertEqual(_sticker_intensity(0), 1.0)
        self.assertEqual(_sticker_intensity(99), 5.0)
        self.assertEqual(_sticker_intensity("3"), 3.0)
        self.assertEqual(_sticker_intensity(3.7), 3.7)

    def test_no_opinion_returns_none(self) -> None:
        for value in (None, "", "x", True, False, float("nan"), float("inf")):
            self.assertIsNone(_sticker_intensity(value), value)


class TextIsNeverSuppressedTests(unittest.TestCase):
    """表情包是语气，话才是内容 —— 任何情况下都不能只剩一张图。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _stub(self, *, pending: bool):
        def decide(need, **kwargs):
            return StickerDecision(
                pending=pending,
                reason="generation_queued" if pending else "generation_unavailable",
                job_id="job-1" if pending else None,
                caption=str(need),
            )

        self.runtime.sticker.decide = decide

    def test_text_survives_a_queued_sticker(self) -> None:
        self._stub(pending=True)
        fake_reply(self.runtime, plan(intensity=4))
        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertTrue(result["sticker"]["pending"])
        self.assertEqual(result["messages"], ["怎么啦…"])
        self.assertEqual(result["reply"], "怎么啦…")

    def test_text_survives_a_refused_sticker(self) -> None:
        self._stub(pending=False)
        fake_reply(self.runtime, plan())
        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertFalse(result["sticker"]["pending"])
        self.assertEqual(result["messages"], ["怎么啦…"])

    def test_legacy_mode_argument_is_ignored_not_obeyed(self) -> None:
        # 老提示词残留 mode=standalone 时，绝不能真的把文字删掉。
        self._stub(pending=True)
        payload = json.loads(plan())
        payload["actions"][0]["args"]["mode"] = "standalone"
        fake_reply(self.runtime, json.dumps(payload, ensure_ascii=False))

        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")
        self.assertEqual(result["messages"], ["怎么啦…"])
        self.assertEqual(result["reply"], "怎么啦…")


class IntensityForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_intensity_reaches_decide(self) -> None:
        captured: dict = {}

        def decide(need, **kwargs):
            captured.update(kwargs)
            captured["need"] = need
            return StickerDecision(pending=True, reason="generation_queued", job_id="j1")

        self.runtime.sticker.decide = decide
        fake_reply(self.runtime, plan(intensity=5))
        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertEqual(captured["need"], "有点想哭")
        self.assertEqual(captured["intensity"], 5.0)
        self.assertEqual(result["sticker"]["intensity"], 5.0)

    def test_missing_intensity_means_no_opinion(self) -> None:
        captured: dict = {}

        def decide(need, **kwargs):
            captured.update(kwargs)
            return StickerDecision(pending=False, reason="generation_unavailable")

        self.runtime.sticker.decide = decide
        fake_reply(self.runtime, plan())
        self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertIsNone(captured["intensity"], "没说强度就别替她编一个")


if __name__ == "__main__":
    unittest.main()
