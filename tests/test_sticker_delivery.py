"""Regression tests for the sticker delivery path.

Observed live (library era): the user asked for a goodnight sticker, the model
answered with plain prose ("表情包这就来"), and nothing was sent.  Two guards
still matter after the switch to generation:

* the assistant's own transcript must look like the schema the model has to
  obey, otherwise it imitates its old prose replies and forgets the envelope;
* an explicit sticker request that comes back without the action is fulfilled
  locally instead of shipping an empty promise.

The delivery path changed: ``send_sticker`` now submits a ModelScope LoRA job
and the PNG is queued through the outbox by ``Runtime.tick``.  These tests pin
that async contract with a fake client (the suite is offline).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ContextAssembler, ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.memory import MemoryService
from fatfish.runtime import Runtime, TurnAction, _owes_sticker

OWNER_SESSION = "qq:private:10001"


class FakeStickerClient:
    """Offline stand-in for ModelScopePhotoClient."""

    available = True

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.payloads: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.fail = False

    def build_payload(self, positive, negative="", *, seed=None, width=1024, height=1024):
        return {
            "prompt": positive,
            "negative_prompt": negative,
            "seed": seed,
            "size": f"{width}x{height}",
        }

    def submit(self, payload):
        task_id = f"task-{len(self.tasks) + 1}"
        self.tasks[task_id] = payload
        self.prompts.append(str(payload.get("prompt") or ""))
        self.payloads.append(dict(payload))
        return task_id

    def poll(self, task_id):
        if self.fail:
            return {"status": "failed"}
        return {"status": "succeeded", "url": "https://example.test/sticker.png"}

    def download(self, url):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 128


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


def attach_fake_client(runtime: Runtime) -> FakeStickerClient:
    client = FakeStickerClient()
    service = runtime.sticker
    service.client = client
    service.enabled = True
    service.model = "fake/model"
    service.max_per_10m = 0  # tests are not about the cost gate
    return client


class StickerDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.client = attach_fake_client(self.runtime)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _pending_jobs(self):
        return self.db.query("SELECT * FROM sticker_jobs")

    def test_prose_reply_still_queues_the_requested_sticker(self) -> None:
        fake_reply(self.runtime, "晚安表情包来啦～软乎乎的那种，给你。\n闭眼吧。")

        result = self.runtime.handle_user_message(
            OWNER_SESSION, "晚安(´-ωก`) 给我发一张晚安的表情包吧"
        )

        tools = [item["tool"] for item in result["actions"]]
        self.assertEqual(tools, ["send_sticker"], "请求表情包却没排上动作")
        self.assertTrue(result["actions"][0]["ok"], "任务没提交成功")
        sticker = result["sticker"]
        self.assertIsNotNone(sticker)
        self.assertTrue(sticker["pending"])
        jobs = self._pending_jobs()
        self.assertEqual(len(jobs), 1, "异步任务没有落库")
        self.assertEqual(jobs[0]["status"], "waiting")
        self.assertIn("whalegirl_sticker", self.client.prompts[0])

    def test_tick_delivers_the_finished_sticker_once(self) -> None:
        fake_reply(self.runtime, "来啦～")
        self.runtime.handle_user_message(OWNER_SESSION, "给我发一张表情包")

        tick = self.runtime.tick()

        self.assertEqual(tick["sticker"]["done"], 1)
        items = self.db.query(
            "SELECT * FROM outbox WHERE kind='qq_send_image'"
        )
        self.assertEqual(len(items), 1)
        payload = json.loads(items[0]["payload_json"])
        self.assertEqual(payload["path"], str(
            self.config.data_dir / "stickers" / "generated" / f"{self._pending_jobs()[0]['id']}.png"
        ))
        # Second tick must not queue the same image again.
        again = self.runtime.tick()
        self.assertEqual(again["sticker"]["done"], 0)
        self.assertEqual(
            len(self.db.query("SELECT * FROM outbox WHERE kind='qq_send_image'")), 1
        )

    def test_refusal_does_not_trigger_the_automatic_sticker(self) -> None:
        fake_reply(self.runtime, "好，那就不发了。")

        result = self.runtime.handle_user_message(
            OWNER_SESSION, "算了，不要表情包了"
        )

        self.assertEqual(result["actions"], [])
        self.assertIsNone(result["sticker"])
        self.assertEqual(self._pending_jobs(), [])

    def test_model_action_is_not_duplicated_by_the_guard(self) -> None:
        fake_reply(
            self.runtime,
            json.dumps(
                {
                    "messages": ["来啦～"],
                    "actions": [
                        {"tool": "send_sticker", "args": {"need": "晚安 好梦"}}
                    ],
                },
                ensure_ascii=False,
            ),
        )

        result = self.runtime.handle_user_message(
            OWNER_SESSION, "给我发一张晚安的表情包"
        )

        tools = [item["tool"] for item in result["actions"]]
        self.assertEqual(tools.count("send_sticker"), 1)

    def test_missing_envelope_is_repaired_in_one_retry(self) -> None:
        calls: list[list[dict]] = []

        def chat(messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return LLMResult(text="喏，抱被子的那个（递过去）", model="fake-model")
            return LLMResult(
                text=json.dumps(
                    {
                        "messages": ["喏，抱被子的那个（递过去）"],
                        "actions": [
                            {"tool": "send_sticker", "args": {"need": "晚安 抱被子"}}
                        ],
                    },
                    ensure_ascii=False,
                ),
                model="fake-model",
            )

        self.runtime.llm.api_key = "fake"
        self.runtime.llm.chat = chat

        # 「抱被子的」不含"表情包"三字，只能靠重问把动作捞回来。
        result = self.runtime.handle_user_message(OWNER_SESSION, "抱被子的")

        self.assertEqual(len(calls), 2, "应该重问一次，且只重问一次")
        self.assertIn("send_sticker", [item["tool"] for item in result["actions"]])
        self.assertTrue(result["sticker"]["pending"])
        trace = self.db.query_one(
            "SELECT result_json FROM turn_traces ORDER BY at DESC LIMIT 1"
        )
        self.assertTrue(json.loads(trace["result_json"])["json_repaired"])

    def test_trace_records_whether_the_sticker_was_synthesised(self) -> None:
        fake_reply(self.runtime, "表情包这就来。")
        self.runtime.handle_user_message(OWNER_SESSION, "给我发张表情包嘛")

        trace = self.db.query_one(
            "SELECT result_json FROM turn_traces ORDER BY at DESC LIMIT 1"
        )
        payload = json.loads(trace["result_json"])
        self.assertTrue(payload["sticker"])
        self.assertTrue(payload["sticker_auto"])

    def test_activity_sticker_is_retargeted_to_owner(self) -> None:
        # 2026-11-18: spontaneous media from the activity sandbox must land in
        # the owner's queue, otherwise the poller skips the activity session.
        results = self.runtime.execute_actions(
            [TurnAction("send_sticker", {"need": "开心 转圈"})],
            session_id="activity-20261118-1000",
            reply="",
            now=time.time(),
            proactive_media=True,
        )
        self.assertTrue(results[0]["ok"], results)
        jobs = self._pending_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["session_id"], OWNER_SESSION)


class StickerIntentTests(unittest.TestCase):
    def test_only_unambiguous_requests_are_auto_fulfilled(self) -> None:
        self.assertTrue(_owes_sticker("给我发一张晚安的表情包吧"))
        self.assertTrue(_owes_sticker("来斗图"))
        # "发个图" may just mean a photo; leave that to the model.
        self.assertFalse(_owes_sticker("给我发个图看看"))
        self.assertFalse(_owes_sticker("在吗"))

    def test_retracted_request_is_not_fulfilled(self) -> None:
        self.assertFalse(_owes_sticker("算了不要表情包了"))
        self.assertFalse(_owes_sticker("别发表情包，好好说话"))


class HistoryEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.memory = MemoryService(self.db, self.config)
        self.assembler = ContextAssembler(self.db, self.config, self.memory)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_assistant_turns_keep_the_tool_envelope(self) -> None:
        self.memory.add_turn("qq:private:1", "user", "给我发个表情包")
        self.memory.add_turn("qq:private:1", "assistant", "来啦～\n软乎乎的")

        messages = self.assembler.history_messages("qq:private:1")

        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["messages"], ["来啦～", "软乎乎的"])
        self.assertNotIn("actions", payload, "空的 actions 会让模型以为这角色从不行动")


if __name__ == "__main__":
    unittest.main()
