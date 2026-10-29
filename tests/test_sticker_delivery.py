"""Regression tests for "the sticker was promised but never sent".

Observed live: the user asked for a goodnight sticker, the model answered with
plain prose ("表情包这就来"), and nothing was sent. The JSON envelope carrying
``actions`` was dropped by the model, so the runtime had nothing to execute.
Two guards are covered here:

* the assistant's own transcript must look like the schema the model has to
  obey, otherwise it imitates its old prose replies and forgets the envelope;
* an explicit sticker request that comes back without the action is fulfilled
  locally instead of shipping an empty promise.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import ContextAssembler, ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.memory import MemoryService
from fatfish.runtime import Runtime, _owes_sticker
from fatfish.sticker import StickerService
from fatfish.sticker.generator import BaseStickerGenerator

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
enabled = true
intent_threshold = 0.5
explicit_threshold = 0.2
base_generation_enabled = false
comfyui_enabled = false
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


class StickerDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.sticker = StickerService(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _seed_sticker(self) -> str:
        sticker = self.sticker.add_sticker(
            str(self.tmp / "goodnight.png"),
            caption="鲸鱼娘晚安 好梦",
            tags=["晚安", "睡觉"],
            intents=["goodnight", "comfort"],
        )
        return sticker.id

    def test_prose_reply_still_delivers_the_requested_sticker(self) -> None:
        self._seed_sticker()
        fake_reply(self.runtime, "晚安表情包来啦～软乎乎的那种，给你。\n闭眼吧。")

        result = self.runtime.handle_user_message(
            OWNER_SESSION, "晚安(´-ωก`) 给我发一张晚安的表情包吧"
        )

        tools = [item["tool"] for item in result["actions"]]
        self.assertEqual(tools, ["send_sticker"], "请求表情包却没排上动作")
        self.assertTrue(result["actions"][0]["ok"])
        self.assertIsNotNone(result["sticker"], "promised a sticker, sent nothing")
        usage = self.db.query("SELECT * FROM sticker_usage")
        self.assertEqual(len(usage), 1, "表情包没记进 usage")

    def test_refusal_does_not_trigger_the_automatic_sticker(self) -> None:
        self._seed_sticker()
        fake_reply(self.runtime, "好，那就不发了。")

        result = self.runtime.handle_user_message(
            OWNER_SESSION, "算了，不要表情包了"
        )

        self.assertEqual(result["actions"], [])
        self.assertIsNone(result["sticker"])

    def test_model_action_is_not_duplicated_by_the_guard(self) -> None:
        self._seed_sticker()
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
        self._seed_sticker()
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
        self.assertIsNotNone(result["sticker"])
        trace = self.db.query_one(
            "SELECT result_json FROM turn_traces ORDER BY at DESC LIMIT 1"
        )
        self.assertTrue(json.loads(trace["result_json"])["json_repaired"])

    def test_trace_records_whether_the_sticker_was_synthesised(self) -> None:
        self._seed_sticker()
        fake_reply(self.runtime, "表情包这就来。")
        self.runtime.handle_user_message(OWNER_SESSION, "给我发张表情包嘛")

        trace = self.db.query_one(
            "SELECT result_json FROM turn_traces ORDER BY at DESC LIMIT 1"
        )
        payload = json.loads(trace["result_json"])
        self.assertTrue(payload["sticker"])
        self.assertTrue(payload["sticker_auto"])


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


class IntentRankingTests(unittest.TestCase):
    """挑错图的两个真实原因：场景词被情绪词盖过、命中被高阈值挡掉。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.sticker = StickerService(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_user_words_beat_the_model_paraphrase(self) -> None:
        # 模型把"晚安"改写成"带着睡意和一点撒娇"，只凭这段会判成 shy。
        need = "软乎乎的晚安表情包，带着睡意和一点撒娇，适合哄人睡觉"
        self.assertEqual(
            self.sticker.requested_intents(need, "晚安(´-ωก`) 给我发一张晚安的表情包吧"),
            ["goodnight"],
        )
        # 没有用户原话时，至少场景词要排在情绪词前面。
        self.assertEqual(self.sticker.requested_intents(need)[0], "goodnight")

    def test_tie_on_score_is_broken_towards_the_scene(self) -> None:
        ranked = [name for name, _ in self.sticker.classify_intents("晚安 带一点撒娇")]
        self.assertEqual(ranked[0], "goodnight", ranked)

    def test_best_candidate_must_match_the_requested_intent(self) -> None:
        from fatfish.sticker.service import StickerCandidate

        wrong = self.sticker.add_sticker(
            str(self.tmp / "pout.png"), caption="傲娇小鲸鱼", intents=["shy", "tease"]
        )
        right = self.sticker.add_sticker(
            str(self.tmp / "night.png"), caption="晚安 好梦", intents=["goodnight", "comfort"]
        )
        # 分数是错的更高——按分数取第一条就会发出"才不是"。
        candidates = [
            StickerCandidate(wrong, 0.90, 0.90, 0.90),
            StickerCandidate(right, 0.30, 0.30, 0.30),
        ]
        picked = StickerService._best_for({"goodnight"}, candidates)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.sticker.id, right.id)

    def test_explicit_request_uses_the_low_threshold(self) -> None:
        # hash 嵌入下好匹配也只有 ~0.3，用 0.5 会被判成"没命中"然后去合成错图。
        night = self.sticker.add_sticker(
            str(self.tmp / "night.png"),
            caption="晚安 好梦",
            tags=["晚安", "睡觉"],
            intents=["goodnight", "comfort"],
        )
        decision = self.sticker.decide(
            "软乎乎的晚安表情包，带一点撒娇",
            user_text="晚安，给我发一张晚安的表情包",
            force=True,
        )
        self.assertTrue(decision.send)
        self.assertEqual(decision.sticker.id, night.id)

    def test_no_sticker_is_better_than_a_wrong_one(self) -> None:
        self.sticker.add_sticker(
            str(self.tmp / "pout.png"), caption="傲娇小鲸鱼", intents=["shy"]
        )
        decision = self.sticker.decide(
            "晚安的表情包", user_text="晚安，来张表情包", force=True
        )
        self.assertFalse(decision.send)
        self.assertEqual(decision.reason, "no_matching_sticker")


class GeneratedCaptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.service = StickerService(self.db, self.config)
        self.generator = BaseStickerGenerator(self.db, self.config, self.service)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_goodnight_caption_follows_the_scene(self) -> None:
        caption = self.generator._fallback_caption(
            "软乎乎的晚安表情包，带着睡意和一点撒娇", {"goodnight"}
        )
        self.assertEqual(caption, "晚安好梦")

    def test_render_without_pillow_raises_a_clear_error(self) -> None:
        # 纯文字部署可以不装 Pillow；少了它只该在「现画」那一刻报错，
        # 而不是 import fatfish 的时候就崩（无头云主机上曾因此起不来）。
        import fatfish.sticker.generator as generator

        original = generator.Image
        try:
            generator.Image = None  # type: ignore[assignment]
            with self.assertRaises(RuntimeError):
                self.generator._render(None, "晚安", {}, "digest")
        finally:
            generator.Image = original  # type: ignore[assignment]

    def test_base_is_chosen_by_the_requested_scene(self) -> None:
        sleepy = self.service.add_sticker(
            str(self.tmp / "sleepy.webp"),
            caption="蓝发鲸鱼女仆",
            intents=["goodnight", "comfort"],
            content={"is_base": True},
        )
        self.service.add_sticker(
            str(self.tmp / "pout.webp"),
            caption="蓝发女仆害羞",
            intents=["shy", "meme"],
            content={"is_base": True},
        )
        base = self.generator.select_base(
            "软乎乎的晚安表情包，带着睡意和一点撒娇", preferred_intents={"goodnight"}
        )
        self.assertEqual(base.id, sleepy.id)


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
