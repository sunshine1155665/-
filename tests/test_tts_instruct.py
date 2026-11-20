"""TTS 客户端发出去的 payload：语气走 instruct，断句走 text_frontend。

为什么这两件事要单独测：它们和参考音无关，但决定「听起来自不自然」。接这个
之前，客户端确实会把 `emotion` 塞进 payload，服务端也确实收下 —— 然后丢掉。
一个参数"传了但没人看"比参数不存在更难发现，所以这里直接断言离开端口的那份
JSON 里有什么。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fatfish.config import Config
from fatfish.tts import TTSClient
from fatfish.tts.instruct import (
    ALIASES,
    DEFAULT_SPEED,
    INSTRUCTS,
    TONE_SPEED,
    TONES,
    describe_tone,
    instruct_for,
    normalize_tone,
    speed_for,
)

WAV = b"RIFF\x00\x00\x00\x00WAVEfmt \x00" + b"\x00" * 24


class _FakeDB:
    def execute(self, *args: object, **kwargs: object) -> None:
        return None


def make_client(tmp: Path, **overrides: object) -> TTSClient:
    data = {
        "app": {"data_dir": str(tmp / "data")},
        "tts": {
            "enabled": True,
            "mode": "http_json",
            "base_url": "http://127.0.0.1:9880/tts",
            "format": "wav",
            **overrides,
        },
    }
    return TTSClient(Config(data, profile="low"), _FakeDB())  # type: ignore[arg-type]


class InstructMappingTests(unittest.TestCase):
    """模型只选类别，措辞由代码映射 —— 所以映射本身必须钉住。"""

    def test_every_tone_has_an_instruction(self) -> None:
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertIn(tone, INSTRUCTS)

    def test_neutral_means_no_instruct_at_all(self) -> None:
        # neutral 不是"一条中性的指令"，而是"别 instruct" —— 它必须返回 None，
        # 这样关掉语气不需要另一个开关。
        self.assertIsNone(instruct_for("neutral"))

    def test_instruction_format_matches_cosyvoice(self) -> None:
        # 见 test_instruct_formatter_still_matches_cosyvoice：表格现在是空的，
        # 格式约束改到那条测试里用 patch 验证。留一个空壳会让人误以为有用。
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertIsNone(instruct_for(tone))

    def test_unknown_tone_degrades_to_neutral(self) -> None:
        # 宁可没有语气，也不要给合成器一句它没见过的指令。
        for value in ("", None, "???", "extremely-happy", 42):
            with self.subTest(value=value):
                self.assertIsNone(instruct_for(value))

    def test_aliases_and_chinese_are_understood(self) -> None:
        self.assertEqual(normalize_tone("Cheerful"), "happy")
        self.assertEqual(normalize_tone("gentle"), "soft")
        self.assertEqual(normalize_tone("撒娇"), "soft")
        self.assertEqual(normalize_tone("元气"), "lively")
        self.assertEqual(normalize_tone("  ANGRY "), "annoyed")

    def test_alias_targets_are_real_tones(self) -> None:
        # 拼错一个 alias 会让整个语气静默失效，这里挡住。
        for alias, tone in ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(tone, TONES)

    def test_describe_is_human_readable(self) -> None:
        # 现在每一档的说明都要讲清"不提措辞"，因为这是刻意的设计，不是没做完。
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertIn("不提措辞", describe_tone(tone))


class SpeedTests(unittest.TestCase):
    """速度就是"元气"。这张表是实测标定的（data/audio_genki/），测试钉住**不变量**。"""

    def test_genki_floor_no_tone_goes_below_normal(self) -> None:
        # 元气地板：任何情绪都不许比裸模型更慢。慢是她垮掉的第一个信号，
        # 上一版把 soft 设成 1.12 + very soft，mimo 直接判"音色 1 分、虚弱"。
        for tone, rate in TONE_SPEED.items():
            with self.subTest(tone=tone):
                self.assertGreaterEqual(rate, 1.0)

    def test_default_is_the_measured_best(self) -> None:
        # 1.05 是唯一拿到 mimo 满分的档位；改它等于改所有语音的默认听感。
        self.assertEqual(DEFAULT_SPEED, 1.05)
        self.assertEqual(speed_for(None), DEFAULT_SPEED)

    def test_excitement_is_faster_than_calm(self) -> None:
        self.assertGreater(speed_for("lively"), speed_for("neutral"))
        self.assertGreater(speed_for("happy"), speed_for("soft"))

    def test_soft_no_longer_slows_her_down(self) -> None:
        # 温柔的"柔"不再从声学参数上要：实测 1.02（只慢 3%）mimo 就判"偏闷、
        # 元气 3 分"。soft 与默认同速，软靠文本层（短句、语气词、标点）。
        self.assertEqual(speed_for("soft"), DEFAULT_SPEED)

    def test_speed_stays_inside_the_server_clamp(self) -> None:
        # 服务端夹在 0.8~1.3；越界不会报错，但会让"配的值"和"实际的值"不一致。
        for tone, rate in TONE_SPEED.items():
            with self.subTest(tone=tone):
                self.assertGreaterEqual(rate, 0.8)
                self.assertLessEqual(rate, 1.3)

    def test_speed_keys_are_real_tones(self) -> None:
        # 六个枚举都要有速度，否则会静默退到 DEFAULT
        for tone in TONES:
            with self.subTest(tone=tone):
                self.assertIn(tone, TONE_SPEED)


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "a.wav"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _payload(self, client: TTSClient, emotion: str | None) -> dict:
        response = mock.MagicMock()
        response.headers.get.return_value = "audio/wav"
        response.read.return_value = WAV
        context = mock.MagicMock()
        context.__enter__.return_value = response
        with mock.patch(
            "fatfish.tts.client.urllib.request.urlopen", return_value=context
        ) as urlopen:
            client._http_json("你好呀", emotion, self.out)
        request = urlopen.call_args[0][0]
        return json.loads(request.data.decode("utf-8"))

    def test_tone_never_becomes_an_instruct_string_now(self) -> None:
        """现在**没有**任何一档会发措辞（lively 也不发）。

        这条曾经断言的是反面（lively 发官方措辞），改的原因是实测：在
        Fun-CosyVoice3-0.5B 上 instruct2 要么在 LLM 线程里断言失败（503），
        要么把指令当台词念出来（3 倍时长 + 多念废话）。所以语气只剩速度。
        """
        payload = self._payload(make_client(self.tmp), "lively")
        self.assertNotIn("instruct", payload)
        self.assertEqual(payload["emotion"], "lively")
        self.assertEqual(payload["speed"], speed_for("lively"))

    def test_instruct_formatter_still_matches_cosyvoice(self) -> None:
        # 表格现在是空的，但格式化函数留着：哪天换 Instruct 版模型要能直接用。
        # <|endofprompt|> 必须在句尾 —— 缺它会让服务端的 LLM 线程断言失败。
        with mock.patch.dict(INSTRUCTS, {"lively": "请非常开心地说一句话"}):
            text = instruct_for("lively")
        assert text is not None
        self.assertTrue(text.startswith("You are a helpful assistant. "))
        self.assertTrue(text.endswith("<|endofprompt|>"))

    def test_emotional_tones_send_no_instruct_on_purpose(self) -> None:
        # 六档全部不发措辞 —— 六条理由写在 instruct.py 的模块文档里。
        for tone in TONES:
            with self.subTest(tone=tone):
                payload = self._payload(make_client(self.tmp), tone)
                self.assertNotIn("instruct", payload)
                self.assertEqual(payload["emotion"], "" if tone == "neutral" else tone)

    def test_neutral_sends_no_instruct_field(self) -> None:
        # 没有语气就是没有这条键，而不是空字符串 —— 空字符串会让服务端多走
        # 一次判断，也让抓包的人分不清"没情绪"和"字段没填"。
        payload = self._payload(make_client(self.tmp), None)
        self.assertNotIn("instruct", payload)
        self.assertEqual(payload["emotion"], "")

    def test_text_frontend_defaults_to_on(self) -> None:
        payload = self._payload(make_client(self.tmp), "soft")
        self.assertIs(payload["text_frontend"], True)

    def test_text_frontend_can_be_switched_off(self) -> None:
        client = make_client(self.tmp, text_frontend=False)
        payload = self._payload(client, "soft")
        self.assertIs(payload["text_frontend"], False)

    def test_instruct_can_be_switched_off(self) -> None:
        # 退路：语气不对时先关这条，声音立刻回到改动前，而不用回滚代码。
        client = make_client(self.tmp, instruct_enabled=False)
        payload = self._payload(client, "happy")
        self.assertNotIn("instruct", payload)

    def test_emotional_tone_also_carries_speed(self) -> None:
        payload = self._payload(make_client(self.tmp), "lively")
        self.assertEqual(payload["speed"], speed_for("lively"))

    def test_neutral_carries_the_genki_default_speed(self) -> None:
        # "没有情绪"也不再是裸模型：1.05 那档是 mimo 唯一给满分的听感。
        payload = self._payload(make_client(self.tmp), None)
        self.assertEqual(payload["speed"], DEFAULT_SPEED)

    def test_every_emotional_tone_carries_its_speed(self) -> None:
        for tone in ("happy", "soft", "sad", "annoyed", "lively"):
            with self.subTest(tone=tone):
                self.assertEqual(
                    self._payload(make_client(self.tmp), tone)["speed"],
                    speed_for(tone),
                )

    def test_speed_is_neutralized_along_with_instruct(self) -> None:
        # 只关 instruct 却继续用 1.08 的速度，等于"没有语气但说话变快"——
        # 那是没人要求的第三种行为，退路必须真的退回老样子。
        client = make_client(self.tmp, instruct_enabled=False)
        payload = self._payload(client, "lively")
        self.assertNotIn("instruct", payload)
        self.assertEqual(payload["speed"], 1.0)

    def test_cache_key_separates_tones(self) -> None:
        # 同一句话、不同语气必须是两个缓存文件，否则换了语气还会命中旧 WAV。
        client = make_client(self.tmp)
        self.assertNotEqual(
            client._cache_path("你好呀", "happy"),
            client._cache_path("你好呀", "sad"),
        )
        self.assertNotEqual(
            client._cache_path("你好呀", "happy"),
            client._cache_path("你好呀", None),
        )

    def test_engine_defaults_to_cosyvoice_and_is_sent(self) -> None:
        # 老配置没有 tts.engine；默认必须还是 CosyVoice，不能悄悄换后端。
        client = make_client(self.tmp)
        self.assertEqual(client.engine, "cosyvoice")
        payload = self._payload(client, "soft")
        self.assertEqual(payload["engine"], "cosyvoice")

    def test_engine_can_be_indextts(self) -> None:
        client = make_client(self.tmp, engine="indextts")
        self.assertEqual(client.engine, "indextts")
        self.assertEqual(self._payload(client, "soft")["engine"], "indextts")

    def test_cache_key_separates_engines(self) -> None:
        # 两个服务共用 9880/tts；只按 base_url 做键会把 CosyVoice 的 WAV
        # 当成 IndexTTS 的结果播出来。
        cosy = make_client(self.tmp)
        index = make_client(self.tmp, engine="indextts")
        self.assertNotEqual(
            cosy._cache_path("你好呀", "soft"),
            index._cache_path("你好呀", "soft"),
        )

    def test_cache_key_separates_pitch(self) -> None:
        # pitch_semitones 决定服务端是否叠加升调；键里漏了它，改配置后听到的
        # 还是旧 pitch 的 WAV（缓存键漏项是最难查的一类 bug）。
        flat = make_client(self.tmp)
        up = make_client(self.tmp, pitch_semitones=2)
        self.assertEqual(flat.pitch_semitones, 0)
        self.assertNotEqual(
            flat._cache_path("你好呀", "soft"),
            up._cache_path("你好呀", "soft"),
        )

    def test_pitch_defaults_to_zero_and_rejects_garbage(self) -> None:
        # 参考音 cand_home_up1.wav 已含 +2，客户端默认也必须是 0；配置错成
        # 垃圾值/负值时按 0 处理，不能让构造函数崩掉。
        self.assertEqual(make_client(self.tmp).pitch_semitones, 0)
        self.assertEqual(
            make_client(self.tmp, pitch_semitones="x").pitch_semitones, 0
        )
        self.assertEqual(
            make_client(self.tmp, pitch_semitones=-3).pitch_semitones, 0
        )
        self.assertEqual(make_client(self.tmp, pitch_semitones=2).pitch_semitones, 2)


if __name__ == "__main__":
    unittest.main()
