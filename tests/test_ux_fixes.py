"""T-0031 fixes from the UX walkthrough -- the three findings that were real
product bugs (everything else in findings.md is prompt tuning or sandbox
artifacts, recorded there for the boss):

1. A fired wakeup delivered nothing when its reason matched none of the three
   ritual templates -- every "到点我喊你" promise broke (3/3 in the sim).
   Now: payload.text (new ``text`` arg on schedule_wakeup) wins, the ritual
   template comes next, and the reason line itself is the final fallback --
   a fired wakeup can no longer fire into silence.
2. "行了不聊了 我去洗澡睡觉了" set no stop flag: the keyword list missed the
   phrasings people actually type, so the following silence was luck
   (no_thread_no_desire), not respect.
3. Her own closing line ("那我不催了") had no effect on the ladder: rung 2 was
   already hanging. Now a rung turn that self-reports desire below the arm
   threshold collapses the rest of the ladder.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.continue_policy import classify_stop_keywords
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.owner import OwnerRegistry
from fatfish.runtime import Runtime
from fatfish.tts import TTSResult
from fatfish.voice import VoiceDecision
from fatfish.wakeup import WakeupScheduler

TZ8 = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 24, 14, 0, tzinfo=TZ8).timestamp()
OWNER_SESSION = "qq:private:10001"


class FakeLLM:
    available = True

    def __init__(self, texts):
        self.texts = [str(t) for t in texts]
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, session_id=None):
        self.calls.append({"messages": messages})
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return LLMResult(
            text=text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=4),
            model="fake-model",
        )


class FakeVoicePolicy:
    def decide(self, text, **kwargs):
        return VoiceDecision("text_only", 0.0, ["fake"])

    def record(self, decision, **kwargs):
        pass


class FakeTTS:
    def synthesize(self, text, *, emotion=None):
        return TTSResult(True, audio_path="fake.wav")

    def synthesize_segments(self, text, *, emotion=None):
        return [(text[:10], TTSResult(True, audio_path="seg.wav"))]


def make_env(tmp: Path):
    data = tmp / "data"
    path = tmp / "ux_fixes.toml"
    path.write_text(
        "\n".join(
            [
                f'[app]\ndata_dir = "{data.as_posix()}"',
                'timezone = "Asia/Shanghai"',
                f'[storage]\ndb_path = "{(data / "ux.db").as_posix()}"',
                "[memory]",
                'embedding_backend = "hash"',
                "embedding_dim = 64",
                "[qzone]",
                "enabled = false",
                "[bilibili]",
                "enabled = false",
                "[sticker]",
                "enabled = false",
                "[voice]",
                "enabled = false",
                "[tts]",
                "enabled = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    runtime = Runtime(db, config)
    runtime.voice = FakeVoicePolicy()
    runtime.tts = FakeTTS()
    OwnerRegistry(db, config).bind(OWNER_SESSION, "主人哥哥")
    return config, db, runtime


class UxFixTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, self.runtime = make_env(self.tmp)
        self.addCleanup(self.db.close)

    def outbox_texts(self) -> list[str]:
        rows = self.db.query(
            "SELECT payload_json FROM outbox WHERE kind='proactive_message' ORDER BY id"
        )
        return [str(json.loads(r["payload_json"]).get("text") or "") for r in rows]


class WakeupDeliveryTests(UxFixTestBase):
    def test_reason_only_wakeup_still_delivers(self) -> None:
        """The 3/3 sim failure: fired wakeup, nothing queued."""
        scheduler = WakeupScheduler(self.db, self.config)
        wakeup = scheduler.schedule(
            NOON + 60,
            intent="remind",
            reason="提醒主人哥哥看S赛决赛",
            session_id=OWNER_SESSION,
            now=NOON,
        )

        results = self.runtime.process_due_wakeups(now=wakeup.fire_at)

        self.assertTrue(results[0]["acted"])
        self.assertEqual(
            self.outbox_texts(), ["提醒主人哥哥看S赛决赛"]
        )

    def test_schedule_wakeup_action_carries_her_words(self) -> None:
        plan = json.dumps(
            {
                "messages": ["好呀，八点我叫你"],
                "actions": [
                    {
                        "tool": "schedule_wakeup",
                        "args": {
                            "when": "+2h",
                            "intent": "remind",
                            "reason": "提醒看决赛",
                            "text": "主人哥哥！决赛开始了，快去看！",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        )
        self.runtime.llm = FakeLLM([plan])
        self.runtime.handle_user_message(OWNER_SESSION, "明晚八点提醒我看决赛", now=NOON)

        rows = self.db.query(
            "SELECT payload_json FROM wakeups WHERE intent='remind'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["payload_json"])["text"],
            "主人哥哥！决赛开始了，快去看！",
        )

        row = self.db.query_one(
            "SELECT fire_at FROM wakeups WHERE intent='remind'"
        )
        self.runtime.process_due_wakeups(now=float(row["fire_at"]))
        self.assertIn("决赛开始了", self.outbox_texts()[-1])

    def test_ritual_template_still_wins_over_reason(self) -> None:
        scheduler = WakeupScheduler(self.db, self.config)
        wakeup = scheduler.schedule(
            NOON + 60, intent="check_user", reason="早安问候",
            session_id=OWNER_SESSION, now=NOON,
        )
        self.runtime.process_due_wakeups(now=wakeup.fire_at)
        self.assertIn("早安", self.outbox_texts()[-1])


class StopKeywordTests(unittest.TestCase):
    def test_real_goodbye_phrasings(self) -> None:
        self.assertEqual(classify_stop_keywords("行了不聊了 我去洗澡睡觉了"), "chat_stop")
        self.assertEqual(classify_stop_keywords("晚安，明天见"), "chat_stop")
        self.assertEqual(classify_stop_keywords("我先睡了，你也早点睡"), "chat_stop")

    def test_busy_phrasings(self) -> None:
        self.assertEqual(classify_stop_keywords("我先去洗澡，一会儿再说"), "busy")
        self.assertEqual(classify_stop_keywords("我先忙了"), "busy")

    def test_normal_chat_stays_clean(self) -> None:
        self.assertIsNone(classify_stop_keywords("今天那个猫视频也太好笑了"))
        self.assertIsNone(classify_stop_keywords("睡不着，陪我聊会儿"))


class RungSelfCollapseTests(UxFixTestBase):
    def _arm(self, *, desire: float) -> None:
        plan = json.dumps(
            {
                "messages": ["嗯，本鲸鱼娘在听"],
                "continue": {"thread_open": True, "desire": desire},
            },
            ensure_ascii=False,
        )
        self.runtime.llm = FakeLLM([plan])
        self.runtime.handle_user_message(OWNER_SESSION, "在吗", now=NOON)

    def _pending(self) -> list:
        return self.db.query(
            "SELECT * FROM wakeups WHERE intent='self_continue' AND status='pending'"
        )

    def test_low_desire_rung_collapses_the_ladder(self) -> None:
        self._arm(desire=0.9)  # three rungs armed
        row = self._pending()[0]
        # rung 1 answers, then self-reports "I've said enough" (desire 0.2)
        self.runtime.llm = FakeLLM(
            [
                json.dumps(
                    {
                        "messages": ["那我不催了，你先靠着"],
                        "continue": {"thread_open": False, "desire": 0.2},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        self.runtime.process_due_wakeups(now=float(row["fire_at"]))

        self.assertEqual(self._pending(), [])  # no rung 2 despite max_rungs=3
        rows = self.db.query(
            "SELECT outcome FROM decision_log WHERE kind='continue_policy' "
            "AND action='ladder_exhausted'"
        )
        self.assertEqual([r["outcome"] for r in rows], ["self_reported_low_desire"])

    def test_high_desire_rung_keeps_chaining(self) -> None:
        self._arm(desire=0.9)
        row = self._pending()[0]
        self.runtime.llm = FakeLLM(
            [
                json.dumps(
                    {
                        "messages": ["在等你回我嘛"],
                        "continue": {"thread_open": True, "desire": 0.7},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        self.runtime.process_due_wakeups(now=float(row["fire_at"]))
        self.assertEqual(len(self._pending()), 1)  # rung 2 hangs as before


if __name__ == "__main__":
    unittest.main()
