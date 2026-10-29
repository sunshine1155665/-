"""Transcript fidelity for voice turns.

``send_voice`` synthesises ``args.text or reply``, and with
``[qq].send_text_with_voice = false`` (the default) the words are *not*
re-sent as text -- the spoken line is the only thing that reached him. The
transcript used to keep ``reply`` regardless, so she remembered saying
something he never received.

No modality marker is added: the assistant transcript is a JSON envelope and
anything inside ``messages`` gets imitated next turn (that is why timestamps
had to be pulled out of it once already).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.runtime import Runtime
from tests.test_runtime_voice_stream import (
    FakeLLM,
    FakeTTS,
    FakeVoicePolicy,
    make_env,
)

SESSION = "qq:private:10001"
REPLY = "好，这就发。"
SPOKEN = "主人哥哥，晚安啦。"


class _Cfg:
    """Minimal ``Config`` stand-in: one key, one value."""

    def __init__(self, value: object = False) -> None:
        self.value = value
        self.seen: list[str] = []

    def get(self, key: str, default: object = None) -> object:
        self.seen.append(key)
        return self.value


def _stub(value: object = False) -> SimpleNamespace:
    stub = SimpleNamespace(config=_Cfg(value))
    stub._text_goes_out_with_voice = lambda: Runtime._text_goes_out_with_voice(stub)
    return stub


def _voice(*, spoken: str = SPOKEN, ok: bool = True) -> dict[str, object]:
    return {"tool": "send_voice", "ok": ok, "result": None, "spoken": spoken}


def transcript(reply, results, *, text_with_voice=False):
    return Runtime._transcript_reply(_stub(text_with_voice), reply, results)


class TranscriptReplyTests(unittest.TestCase):
    def test_spoken_line_replaces_the_undelivered_text(self) -> None:
        # voice_only: the words never went out as text, so only the spoken
        # line actually reached him.
        self.assertEqual(transcript(REPLY, [_voice()]), SPOKEN)

    def test_text_mode_keeps_the_full_reply(self) -> None:
        self.assertEqual(
            transcript(REPLY, [_voice()], text_with_voice=True), REPLY
        )

    def test_identical_spoken_line_keeps_the_reply(self) -> None:
        self.assertEqual(transcript(SPOKEN, [_voice()]), SPOKEN)

    def test_whitespace_only_difference_counts_as_identical(self) -> None:
        self.assertEqual(transcript(SPOKEN, [_voice(spoken=f"  {SPOKEN}  ")]), SPOKEN)

    def test_no_voice_action_leaves_the_reply_alone(self) -> None:
        results = [{"tool": "send_sticker", "ok": True, "spoken": "不该被用"}]
        self.assertEqual(transcript(REPLY, results), REPLY)

    def test_failed_synthesis_is_ignored(self) -> None:
        self.assertEqual(transcript(REPLY, [_voice(ok=False)]), REPLY)

    def test_missing_or_blank_spoken_falls_back(self) -> None:
        self.assertEqual(transcript(REPLY, [{"tool": "send_voice", "ok": True}]), REPLY)
        self.assertEqual(transcript(REPLY, [_voice(spoken="   ")]), REPLY)

    def test_none_results_are_tolerated(self) -> None:
        self.assertEqual(transcript(REPLY, None), REPLY)
        self.assertEqual(transcript(REPLY, []), REPLY)

    def test_empty_reply_is_never_turned_into_speech(self) -> None:
        self.assertEqual(transcript("", [_voice()]), "")


class TextWithVoiceFlagTests(unittest.TestCase):
    def test_default_is_text_suppressed(self) -> None:
        stub = SimpleNamespace(config=_Cfg(False))
        self.assertFalse(Runtime._text_goes_out_with_voice(stub))

    def test_missing_key_means_text_suppressed(self) -> None:
        class _Empty:
            def get(self, key, default=None):
                return default

        self.assertFalse(Runtime._text_goes_out_with_voice(SimpleNamespace(config=_Empty())))

    def test_string_flags_are_understood(self) -> None:
        for raw, expected in (
            ("true", True),
            ("YES", True),
            ("1", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("", False),
        ):
            with self.subTest(raw=raw):
                stub = SimpleNamespace(config=_Cfg(raw))
                self.assertEqual(Runtime._text_goes_out_with_voice(stub), expected)


class VoiceTurnIntegrationTests(unittest.TestCase):
    """One real turn through ``handle_user_message`` -- no network, no audio."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, *, text_with_voice: bool = False) -> tuple[Runtime, object]:
        config, db = make_env(self.tmp)
        self.addCleanup(db.close)
        if text_with_voice:
            config._data.setdefault("qq", {})["send_text_with_voice"] = True
        runtime = Runtime(db, config)
        runtime.llm = FakeLLM(
            json.dumps(
                {
                    "messages": [REPLY],
                    "actions": [
                        {
                            "tool": "send_voice",
                            "args": {"text": SPOKEN, "emotion": "soft"},
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        runtime.voice = FakeVoicePolicy()
        runtime.tts = FakeTTS()
        return runtime, db

    def _last_assistant_turn(self, db) -> str:
        row = db.query_one(
            "SELECT content FROM conversation_turns WHERE role='assistant' "
            "ORDER BY id DESC LIMIT 1"
        )
        return str(row["content"]) if row else ""

    def test_transcript_keeps_what_was_actually_said(self) -> None:
        runtime, db = self._runtime()
        result = runtime.handle_user_message(SESSION, "发个语音")

        self.assertTrue(any(a.get("spoken") for a in result["actions"]))
        self.assertEqual([a.get("spoken") for a in result["actions"]], [SPOKEN])
        # The text was suppressed on the wire, so the transcript must not claim
        # she sent REPLY.
        self.assertEqual(self._last_assistant_turn(db), SPOKEN)

    def test_transcript_keeps_the_text_when_it_was_also_sent(self) -> None:
        runtime, db = self._runtime(text_with_voice=True)
        runtime.handle_user_message(SESSION, "发个语音")
        self.assertEqual(self._last_assistant_turn(db), REPLY)

    def test_turn_without_voice_is_unchanged(self) -> None:
        config, db = make_env(self.tmp)
        self.addCleanup(db.close)
        runtime = Runtime(db, config)
        runtime.llm = FakeLLM(json.dumps({"messages": [REPLY]}, ensure_ascii=False))
        runtime.voice = FakeVoicePolicy()
        runtime.tts = FakeTTS()
        runtime.handle_user_message(SESSION, "在吗")
        self.assertEqual(self._last_assistant_turn(db), REPLY)


if __name__ == "__main__":
    unittest.main()
