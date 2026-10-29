"""端到端：从模型的那句话，到合成器真正收到的指令。

为什么要一条穿过 Runtime 的测试：语气这条链上有四个各自"看起来对"的环节 ——
提示词告诉模型可以填 emotion、Runtime 把它传给 TTSClient、客户端把它翻成
instruct、服务端用它走 inference_instruct2。任何一环松掉，单测都能全绿而线上
声音一点不变（这正是接这个之前的状态：客户端传了，服务端收下就丢）。

所以这里不测任何单个函数，只测**离开端口的那个 JSON**。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.runtime import Runtime
from fatfish.tts.instruct import instruct_for, speed_for

OWNER_SESSION = "qq:private:10001"
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt \x00" + b"\x00" * 24


def make_runtime(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'voice.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[tts]
enabled = true
mode = "http_json"
base_url = "http://127.0.0.1:9880/tts"
format = "wav"
# 关掉分段，一条语音只发一个请求 —— 断言 payload 时少一层噪音。
short_sentence_mode = false
stream_sentences = false

[voice]
enabled = true
# 让语音策略放行：这些用例测的是"放行之后语气对不对"，不是"该不该发语音"。
min_score = 0.0
text_with_voice_min = 0.0

[sticker]
enabled = false
intent_threshold = 0.6
explicit_threshold = 0.2

[photo]
enabled = false

[qq]
enabled = false
"""
    path = tmp / "voice.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


def reply(runtime: Runtime, payload: dict) -> dict:
    """把模型的一次回复喂进去，返回 handle_user_message 的结果。"""
    runtime.llm.api_key = "fake"
    runtime.llm.chat = lambda messages, **kwargs: LLMResult(
        text=json.dumps(payload, ensure_ascii=False),
        usage=LLMUsage(prompt_tokens=10, completion_tokens=3),
        model="fake-model",
    )
    return runtime.handle_user_message(OWNER_SESSION, "嗯")


class ToneReachesTheSynthesizerTests(unittest.TestCase):
    """每条用例都断言「离开端口的那份 JSON」。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.payloads: list[dict] = []

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _handle(self, plan: dict) -> dict:
        body = mock.MagicMock()
        body.headers.get.return_value = "audio/wav"
        body.read.return_value = WAV
        context = mock.MagicMock()
        context.__enter__.return_value = body

        def fake_urlopen(request, *args, **kwargs):
            self.payloads.append(json.loads(request.data.decode("utf-8")))
            return context

        with mock.patch(
            "fatfish.tts.client.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            return reply(self.runtime, plan)

    def test_spontaneous_voice_carries_a_tone(self) -> None:
        # 没有工具调用 —— 模型没机会指定语气，所以走 voice/tone.py 的兜底。
        result = self._handle({"messages": ["晚安啦，做个好梦。"]})

        self.assertTrue(self.payloads, "放行语音之后应该有合成请求")
        payload = self.payloads[0]
        # soft 现在靠语气标签 + 速度表达，不再发 very soft 措辞（那会压垮元气）。
        self.assertEqual(payload["emotion"], "soft")
        self.assertEqual(payload["speed"], speed_for("soft"))
        self.assertNotIn("instruct", payload)
        self.assertTrue(payload["text_frontend"])
        self.assertEqual(result["reply"], "晚安啦，做个好梦。")

    def test_flat_reply_sends_no_instruct(self) -> None:
        # 说事实不该换语气：没有 instruct 键 = zero-shot，等于改动前的行为。
        self._handle({"messages": ["我回来了。"]})

        payload = self.payloads[0]
        self.assertNotIn("instruct", payload)
        self.assertEqual(payload["emotion"], "")

    def test_model_choice_beats_the_text_heuristic(self) -> None:
        # 这句话的文本读起来是 neutral（"随便说说"），但模型明说了要 lively ——
        # 模型知道自己想表达什么，文本规则读不到。所以模型的判断必须优先。
        self._handle(
            {
                "messages": ["随便说说嘛。"],
                "actions": [{"tool": "send_voice", "args": {"emotion": "lively"}}],
            }
        )

        payload = self.payloads[0]
        # 模型的判断仍然优先 —— 但"lively"现在也只体现为速度（不发措辞）。
        self.assertEqual(payload["emotion"], "lively")
        self.assertEqual(payload["speed"], speed_for("lively"))
        self.assertNotIn("instruct", payload)

    def test_chinese_tone_from_the_model_is_understood(self) -> None:
        self._handle(
            {
                "messages": ["那就多陪你一会儿。"],
                "actions": [{"tool": "send_voice", "args": {"emotion": "撒娇"}}],
            }
        )

        self.assertEqual(self.payloads[0]["emotion"], "soft")
        self.assertEqual(self.payloads[0]["speed"], speed_for("soft"))

    def test_junk_tone_never_breaks_the_voice(self) -> None:
        # 模型多打一个字不该让整条语音失败，也不该让服务端收到一句它没见过的指令。
        result = self._handle(
            {
                "messages": ["哼，才不理你了。"],
                "actions": [{"tool": "send_voice", "args": {"emotion": "???"}}],
            }
        )

        self.assertTrue(result["tts"]["ok"], result["tts"])
        self.assertNotIn("instruct", self.payloads[0])

    def test_two_tones_do_not_share_a_cache_file(self) -> None:
        # 同一句话、不同语气必须是两个音频，否则换了语气还会放旧录音。
        # 现在两个语气的差别就在 speed 与 emotion 上，两者都进缓存键。
        self._handle({"messages": ["晚安啦，做个好梦。"]})
        self._handle(
            {
                "messages": ["晚安啦，做个好梦。"],
                "actions": [{"tool": "send_voice", "args": {"emotion": "lively"}}],
            }
        )
        first, second = self.payloads
        self.assertNotEqual(first["emotion"], second["emotion"])
        self.assertNotEqual(first["speed"], second["speed"])


class ToneCanBeTurnedOffTests(unittest.TestCase):
    """退路：语气不对时关一个开关就回到改动前，不用回滚代码。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.payloads: list[dict] = []

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_instruct_can_be_disabled(self) -> None:
        self.runtime.tts.instruct_enabled = False
        body = mock.MagicMock()
        body.headers.get.return_value = "audio/wav"
        body.read.return_value = WAV
        context = mock.MagicMock()
        context.__enter__.return_value = body
        with mock.patch(
            "fatfish.tts.client.urllib.request.urlopen",
            side_effect=lambda request, *a, **k: (
                self.payloads.append(json.loads(request.data.decode("utf-8")))
                or context
            ),
        ):
            reply(self.runtime, {"messages": ["晚安啦，做个好梦。"]})

        self.assertTrue(self.payloads)
        self.assertNotIn("instruct", self.payloads[0])


if __name__ == "__main__":
    unittest.main()
