from __future__ import annotations

import array
import concurrent.futures
import contextlib
import http.client
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_server_module():
    spec = importlib.util.spec_from_file_location(
        "fatfish_cosyvoice_soft_server", ROOT / "scripts" / "cosyvoice_soft_server.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/cosyvoice_soft_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server = _load_server_module()

EXPECTED_PROMPT = (
    "You are a helpful assistant.<|endofprompt|>"
    "你好呀，我是大肥鱼。今天海面很安静，我在这里等你。 "
    "你好呀，我是大肥鱼。今天海面很安静，我在这里等你。"
)


class FakeCuda:
    def __init__(self) -> None:
        self.synchronized = 0
        self.empty_calls = 0

    def synchronize(self) -> None:
        self.synchronized += 1

    def empty_cache(self) -> None:
        self.empty_calls += 1


class FakeTorch:
    """Minimal torch double: seed, inference context and CUDA cache calls."""

    def __init__(self) -> None:
        self.seeds: list[int] = []
        self.cuda = FakeCuda()

    def manual_seed(self, value: int) -> None:
        self.seeds.append(value)

    def inference_mode(self):
        return contextlib.nullcontext()


class FakeModelFactory:
    """Lazy-load factory double; counts loads and never imports torch."""

    def __init__(
        self,
        model: FakeModel,
        torch_module: FakeTorch | None = None,
        *,
        device: str = "cpu",
        delay: float = 0.0,
    ) -> None:
        self.model = model
        self.torch_module = torch_module if torch_module is not None else FakeTorch()
        self.device = device
        self.delay = delay
        self.calls = 0
        self.fail_message: str | None = None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_message:
            raise RuntimeError(self.fail_message)
        return self.model, self.torch_module, self.device


class FakeModel:
    """Contract double for AutoModel; never loads torch or touches the GPU."""

    sample_rate = 24000

    def __init__(
        self,
        *,
        fail_message: str | None = None,
        delay: float = 0.0,
        amplitude: float = 0.5,
        chunk_count: int = 2,
    ) -> None:
        self.fail_message = fail_message
        self.delay = delay
        self.amplitude = amplitude
        self.chunk_count = chunk_count
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def inference_zero_shot(
        self,
        text: str,
        prompt_text: str,
        ref_path: str,
        *,
        stream: bool,
        speed: float,
        text_frontend: bool,
    ):
        return self._run(
            {
                "kind": "zero_shot",
                "text": text,
                "prompt_text": prompt_text,
                "ref_path": ref_path,
                "stream": stream,
                "speed": speed,
                "text_frontend": text_frontend,
            }
        )

    def inference_instruct2(
        self,
        text: str,
        instruct_text: str,
        ref_path: str,
        *,
        stream: bool,
        speed: float,
        text_frontend: bool,
    ):
        # 真实的 CosyVoice3 第二个位置是 instruct 字符串，不是 prompt_text ——
        # 这正是接错参数后"看起来能跑但语气永远不变"的地方，所以单独记下来。
        return self._run(
            {
                "kind": "instruct2",
                "text": text,
                "instruct_text": instruct_text,
                "ref_path": ref_path,
                "stream": stream,
                "speed": speed,
                "text_frontend": text_frontend,
            }
        )

    def _run(self, record: dict[str, Any]):
        self.calls.append(record)
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_message:
                raise RuntimeError(self.fail_message)
            for _ in range(self.chunk_count):
                yield {
                    "tts_speech": [
                        self.amplitude,
                        -self.amplitude / 2,
                        0.0,
                        0.1,
                    ]
                }
        finally:
            with self._lock:
                self.active -= 1


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class ServerTestCase(unittest.TestCase):
    model: FakeModel
    lazy = False
    idle_unload_seconds = 300.0
    # 子类覆盖这两个就能测「关掉某一路」而不用重建服务器。
    instruct_enabled = True
    text_frontend = True

    def make_model(self) -> FakeModel:
        return FakeModel()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ref = Path(self._tmp.name) / "ref_base_x2.wav"
        self.model = self.make_model()
        self.fake_torch = FakeTorch()
        self.factory = FakeModelFactory(self.model, self.fake_torch)
        if self.lazy:
            self.service = server.create_app(
                model=None,
                model_factory=self.factory,
                lazy_load=True,
                idle_unload_seconds=self.idle_unload_seconds,
                ref=self.ref,
                model_label="fake-model",
                device="unknown",
                instruct_enabled=self.instruct_enabled,
                text_frontend=self.text_frontend,
            )
        else:
            self.service = server.create_app(
                model=self.model,
                ref=self.ref,
                model_label="fake-model",
                torch_module=self.fake_torch,
                device="cpu",
                idle_unload_seconds=self.idle_unload_seconds,
                instruct_enabled=self.instruct_enabled,
                text_frontend=self.text_frontend,
            )
        self.httpd = server.make_server("127.0.0.1", 0, self.service)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.service.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def post_json(self, payload: Any) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.request(
            "POST",
            "/tts",
            body=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def health(self) -> dict[str, Any]:
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        return json.loads(body.decode("utf-8"))


class ContractTests(ServerTestCase):
    def test_prompt_and_reference_constants(self) -> None:
        self.assertEqual(server.PROMPT_TEXT, EXPECTED_PROMPT)
        self.assertEqual(server.REF_PATH.name, "ref_base_x2.wav")
        self.assertEqual(server.SAMPLE_RATE, 24000)
        self.assertEqual(server.SEED, 42)
        self.assertEqual(server.MAX_TEXT_CHARS, 300)
        self.assertEqual(server.MAX_BODY_BYTES, 16 * 1024)

    def test_health_reports_required_fields(self) -> None:
        data = self.health()
        self.assertTrue(
            {
                "ok",
                "model",
                "ref",
                "loaded",
                "device",
                "sample_rate",
                "requests",
                "errors",
                "loads",
                "unloads",
                "load_seconds",
                "last_request_at",
                "last_unload_at",
                "idle_unload_seconds",
                "lazy_load",
            }
            <= set(data)
        )
        self.assertTrue(data["ok"])
        self.assertTrue(data["loaded"])
        self.assertFalse(data["lazy_load"])
        self.assertEqual(data["model"], "fake-model")
        self.assertEqual(data["ref"], str(self.ref))
        self.assertEqual(data["device"], "cpu")
        self.assertEqual(data["sample_rate"], 24000)
        self.assertEqual(data["requests"], 0)
        self.assertEqual(data["errors"], 0)
        self.assertEqual(data["loads"], 1)
        self.assertEqual(data["unloads"], 0)
        self.assertEqual(data["load_seconds"], 0.0)
        self.assertIsNone(data["last_request_at"])
        self.assertIsNone(data["last_unload_at"])
        self.assertEqual(data["idle_unload_seconds"], 300.0)
        self.assertEqual(data["in_flight"], 0)

    def test_valid_post_returns_normalized_mono_wav(self) -> None:
        text = "你好呀，我是大肥鱼。"
        status, headers, body = self.post_json(
            {"text": text, "voice": "", "format": "wav", "emotion": "happy"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertEqual(body[8:12], b"WAVE")

        with wave.open(io.BytesIO(body), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 24000)
            self.assertEqual(handle.getnframes(), 8)
            raw = handle.readframes(handle.getnframes())
        pcm = array.array("h")
        pcm.frombytes(raw)
        self.assertEqual(max(abs(value) for value in pcm), 29490)  # 0.9 * 32767

        self.assertEqual(len(self.model.calls), 1)
        call = self.model.calls[0]
        self.assertEqual(call["kind"], "zero_shot")
        self.assertEqual(call["text"], text)
        self.assertEqual(call["prompt_text"], EXPECTED_PROMPT)
        self.assertEqual(call["ref_path"], str(self.ref))
        self.assertFalse(call["stream"])
        self.assertEqual(call["speed"], 1.0)
        # 断句交给文本前端是新的默认：20 字硬切再拼正是"断句听着不对"的来源。
        self.assertTrue(call["text_frontend"])

        data = self.health()
        self.assertEqual(data["requests"], 1)
        self.assertEqual(data["errors"], 0)
        self.assertEqual(data["loads"], 1)
        self.assertIsNotNone(data["last_request_at"])
        self.assertEqual(data["in_flight"], 0)

    def test_three_hundred_chars_is_accepted(self) -> None:
        status, _, body = self.post_json({"text": "鱼" * 300})
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"RIFF"))

    def test_unknown_path_returns_404(self) -> None:
        status, _, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not_found")

    def test_get_tts_is_405(self) -> None:
        status, _, _ = self.request("GET", "/tts")
        self.assertEqual(status, 405)


class InstructTests(ServerTestCase):
    """语气走 instruct2，断句走 text_frontend —— 两条独立的路。

    接这个之前服务端只调 inference_zero_shot，`emotion` 收下就丢，所以「语音
    有没有情绪」在链路上是断的：客户端传了、服务端不看、声音不变。这里钉住
    修好之后的契约，也钉住两条退路（不传 instruct = zero-shot，坏值不炸服务）。
    """

    def test_instruct_switches_to_instruct2(self) -> None:
        instruct = (
            "You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>"
        )
        status, headers, body = self.post_json({"text": "你好呀", "instruct": instruct})

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertEqual(len(self.model.calls), 1)
        call = self.model.calls[0]
        self.assertEqual(call["kind"], "instruct2")
        self.assertEqual(call["instruct_text"], instruct)
        # 变的是语气，不是音色：参考音和 seed 一个字没动。
        self.assertEqual(call["ref_path"], str(self.ref))
        self.assertIn(server.SEED, self.fake_torch.seeds)

    def test_without_instruct_it_stays_zero_shot(self) -> None:
        # 不传 instruct 就等于"没有情绪"，不需要第二个开关。
        status, _, _ = self.post_json({"text": "你好呀"})
        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[0]["kind"], "zero_shot")

    def test_blank_instruct_is_treated_as_absent(self) -> None:
        status, _, _ = self.post_json({"text": "你好呀", "instruct": "   "})
        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[0]["kind"], "zero_shot")

    def test_instruct_gets_the_endofprompt_token(self) -> None:
        """Bare 措辞必须被补上 <|endofprompt|>。

        这是真机上「lively 一档必挂」的根因：CosyVoice3 的 LLM 里有一句硬断言，
        拼进模型的 text 里必须有这个 token；instruct2 路径下 instruct 整串充当
        prompt_text，我们原来只发措辞。断言失败发生在 **LLM 线程里**，所以外面
        看到的不是断言，而是「音频 0 个 token -> token2wav 在空张量上崩 -> 503」。
        也就是说这个字面量不补，那一档语气就等于没有。
        """
        status, _, _ = self.post_json({"text": "你好呀", "instruct": "请非常开心地说一句话"})
        self.assertEqual(status, 200)
        self.assertEqual(
            self.model.calls[0]["instruct_text"],
            "请非常开心地说一句话<|endofprompt|>",
        )

    def test_existing_endofprompt_is_not_doubled(self) -> None:
        instruct = "用四川话说这句话<|endofprompt|>"
        status, _, _ = self.post_json({"text": "你好呀", "instruct": instruct})
        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[0]["instruct_text"], instruct)

    def test_client_can_opt_out_of_the_text_frontend(self) -> None:
        status, _, _ = self.post_json({"text": "你好呀", "text_frontend": False})
        self.assertEqual(status, 200)
        self.assertFalse(self.model.calls[0]["text_frontend"])

    def test_speed_is_clamped_not_rejected(self) -> None:
        # 语音彻底失败比"没那么快"更糟，所以越界只夹取。
        status, _, _ = self.post_json({"text": "你好呀", "speed": 9.0})
        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[0]["speed"], server.MAX_SPEED)

        status, _, _ = self.post_json({"text": "你好呀", "speed": 0.1})
        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[1]["speed"], server.MIN_SPEED)

    def test_junk_speed_falls_back_to_the_configured_one(self) -> None:
        status, _, _ = self.post_json({"text": "你好呀", "speed": "很快"})
        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[0]["speed"], server.SPEED)

    def test_instruct_must_be_a_string(self) -> None:
        status, _, body = self.post_json({"text": "你好呀", "instruct": {"a": 1}})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "instruct_must_be_string")

    def test_instruct_length_is_capped(self) -> None:
        status, _, body = self.post_json(
            {"text": "你好呀", "instruct": "x" * (server.MAX_INSTRUCT_CHARS + 1)}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "instruct_too_long")

    def test_text_frontend_must_be_a_boolean(self) -> None:
        # "false" 是字符串、在 Python 里是真值 —— 静默接受会让"关掉前端"变成
        # "打开前端"，所以这里必须硬拒。
        status, _, body = self.post_json({"text": "你好呀", "text_frontend": "false"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text_frontend_must_be_boolean")

    def test_health_reports_both_switches(self) -> None:
        data = self.health()
        self.assertTrue(data["instruct"])
        self.assertTrue(data["text_frontend"])
        self.assertEqual(data["speed"], 1.0)


class InstructDisabledTests(ServerTestCase):
    """--no-instruct 是退路：请求里的 instruct 被忽略，回到改动前的行为。"""

    instruct_enabled = False

    def test_instruct_is_ignored_when_the_service_disables_it(self) -> None:
        instruct = "You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>"
        status, _, _ = self.post_json({"text": "你好呀", "instruct": instruct})

        self.assertEqual(status, 200)
        self.assertEqual(self.model.calls[0]["kind"], "zero_shot")

    def test_health_says_it_is_off(self) -> None:
        self.assertFalse(self.health()["instruct"])


class ValidationTests(ServerTestCase):
    def test_invalid_json_returns_400(self) -> None:
        status, _, body = self.request(
            "POST",
            "/tts",
            body=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_json")
        self.assertEqual(self.health()["errors"], 1)

    def test_json_array_returns_400(self) -> None:
        status, _, body = self.post_json([1, 2, 3])
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "json_object_required")

    def test_missing_text_returns_400(self) -> None:
        status, _, body = self.post_json({"voice": "x", "format": "wav"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text_required")

    def test_empty_text_returns_400(self) -> None:
        status, _, body = self.post_json({"text": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text_empty")

    def test_too_long_text_returns_400(self) -> None:
        status, _, body = self.post_json({"text": "鱼" * 301})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text_too_long")

    def test_body_over_16kb_returns_413(self) -> None:
        status, _, body = self.request(
            "POST",
            "/tts",
            body=b"x" * (16 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"], "body_too_large")
        self.assertEqual(self.health()["errors"], 1)


class ModelFailureTests(ServerTestCase):
    def make_model(self) -> FakeModel:
        return FakeModel(fail_message="CUDA out of memory")

    def test_oom_returns_503_json(self) -> None:
        status, headers, body = self.post_json({"text": "你好呀"})
        self.assertEqual(status, 503)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "out_of_memory")
        data = self.health()
        self.assertEqual(data["requests"], 1)
        self.assertEqual(data["errors"], 1)


class GenericModelFailureTests(ServerTestCase):
    def make_model(self) -> FakeModel:
        return FakeModel(fail_message="decoder exploded")

    def test_model_error_returns_503(self) -> None:
        status, _, body = self.post_json({"text": "你好呀"})
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "model_error")


class SerializationTests(ServerTestCase):
    def make_model(self) -> FakeModel:
        return FakeModel(delay=0.15)

    def test_concurrent_requests_are_serialized(self) -> None:
        def call(index: int) -> int:
            status, _, body = self.post_json({"text": f"并发测试{index}"})
            self.assertTrue(body.startswith(b"RIFF"))
            return status

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(call, range(3)))
        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(self.model.max_active, 1)
        data = self.health()
        self.assertEqual(data["requests"], 3)
        self.assertEqual(data["errors"], 0)


class LoggingPrivacyTests(ServerTestCase):
    def test_request_text_is_not_logged(self) -> None:
        marker = "日志泄露探针ABC123"
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            status, _, _ = self.post_json({"text": marker})
        self.assertEqual(status, 200)
        self.assertNotIn(marker, buffer.getvalue())

    def test_model_error_detail_does_not_echo_text(self) -> None:
        marker = "错误响应探针XYZ789"
        self.model.fail_message = "decoder exploded"
        _, _, body = self.post_json({"text": marker})
        self.assertNotIn(marker.encode("utf-8"), body)


class SampleRateTests(ServerTestCase):
    def make_model(self) -> FakeModel:
        model = FakeModel()
        model.sample_rate = 16000
        return model

    def test_sample_rate_flows_to_health_and_wav(self) -> None:
        self.assertEqual(self.health()["sample_rate"], 16000)
        status, _, body = self.post_json({"text": "你好"})
        self.assertEqual(status, 200)
        with wave.open(io.BytesIO(body), "rb") as handle:
            self.assertEqual(handle.getframerate(), 16000)


class LazyLoadTests(ServerTestCase):
    lazy = True
    idle_unload_seconds = 30.0

    def test_health_before_first_request_reports_unloaded(self) -> None:
        data = self.health()
        self.assertTrue(data["ok"])
        self.assertFalse(data["loaded"])
        self.assertTrue(data["lazy_load"])
        self.assertEqual(data["loads"], 0)
        self.assertEqual(data["unloads"], 0)
        self.assertEqual(data["load_seconds"], 0.0)
        self.assertIsNone(data["last_request_at"])
        self.assertEqual(data["device"], "unknown")
        self.assertEqual(self.factory.calls, 0)

    def test_first_request_loads_then_reuses_model(self) -> None:
        status, _, body = self.post_json({"text": "你好呀"})
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"RIFF"))
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["loads"], 1)
        self.assertGreaterEqual(data["load_seconds"], 0.0)
        self.assertIsNotNone(data["last_request_at"])
        self.assertIsNotNone(data["last_load_at"])
        self.assertEqual(self.factory.calls, 1)

        status, _, _ = self.post_json({"text": "再来一句"})
        self.assertEqual(status, 200)
        data = self.health()
        self.assertEqual(data["requests"], 2)
        self.assertEqual(data["loads"], 1)
        self.assertEqual(self.factory.calls, 1)

    def test_forced_unload_and_load_endpoints(self) -> None:
        status, _, body = self.request("POST", "/unload")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["unloaded"])
        self.assertFalse(payload["loaded"])
        self.assertEqual(self.health()["unloads"], 0)

        status, _, _ = self.post_json({"text": "你好"})
        self.assertEqual(status, 200)

        status, _, body = self.request("POST", "/unload")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["unloaded"])
        self.assertFalse(payload["loaded"])
        data = self.health()
        self.assertFalse(data["loaded"])
        self.assertEqual(data["unloads"], 1)
        self.assertEqual(data["last_unload_reason"], "manual")
        self.assertIsNotNone(data["last_unload_at"])
        self.assertGreaterEqual(self.fake_torch.cuda.empty_calls, 1)

        status, _, body = self.request("POST", "/load")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["loaded"])
        self.assertGreaterEqual(payload["load_seconds"], 0.0)
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["loads"], 2)
        self.assertEqual(data["unloads"], 1)
        self.assertEqual(self.factory.calls, 2)

    def test_load_failure_returns_503_then_recovers(self) -> None:
        self.factory.fail_message = "CUDA out of memory"
        status, _, body = self.post_json({"text": "你好"})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "out_of_memory")
        data = self.health()
        self.assertTrue(data["ok"])
        self.assertFalse(data["loaded"])
        self.assertEqual(data["loads"], 0)
        self.assertEqual(data["errors"], 1)

        self.factory.fail_message = None
        status, _, _ = self.post_json({"text": "你好"})
        self.assertEqual(status, 200)
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["loads"], 1)
        self.assertEqual(data["errors"], 1)

    def test_concurrent_first_requests_load_once(self) -> None:
        self.factory.delay = 0.1

        def call(index: int) -> int:
            status, _, body = self.post_json({"text": f"并发{index}"})
            self.assertTrue(body.startswith(b"RIFF"))
            return status

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(call, range(3)))
        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.model.max_active, 1)
        data = self.health()
        self.assertEqual(data["requests"], 3)
        self.assertEqual(data["loads"], 1)

    def test_health_works_after_unload(self) -> None:
        self.post_json({"text": "你好"})
        self.request("POST", "/unload")
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertFalse(data["loaded"])

    def test_health_responds_while_model_is_loading(self) -> None:
        self.factory.delay = 1.0
        result: dict[str, Any] = {}

        def call() -> None:
            result["status"], _, _ = self.post_json({"text": "加载中"})

        thread = threading.Thread(target=call)
        thread.start()
        time.sleep(0.2)
        started = time.monotonic()
        data = self.health()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertFalse(data["loaded"])
        self.assertEqual(data["in_flight"], 1)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["status"], 200)
        self.assertTrue(self.health()["loaded"])

    def test_get_control_endpoints_are_405(self) -> None:
        for path in ("/load", "/unload"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 405)


class IdleUnloadTests(ServerTestCase):
    lazy = True
    idle_unload_seconds = 0.15

    def test_idle_unload_then_reload(self) -> None:
        status, _, _ = self.post_json({"text": "第一句"})
        self.assertEqual(status, 200)
        self.assertEqual(self.factory.calls, 1)
        self.assertTrue(wait_until(lambda: not self.health()["loaded"], timeout=3.0))
        data = self.health()
        self.assertFalse(data["loaded"])
        self.assertEqual(data["unloads"], 1)
        self.assertEqual(data["loads"], 1)
        self.assertEqual(data["last_unload_reason"], "idle")
        self.assertIsNotNone(data["last_unload_at"])
        self.assertGreaterEqual(self.fake_torch.cuda.empty_calls, 1)

        status, _, body = self.post_json({"text": "第二句"})
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"RIFF"))
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["loads"], 2)
        self.assertEqual(self.factory.calls, 2)

    def test_manual_load_also_unloads_when_idle(self) -> None:
        status, _, body = self.request("POST", "/load")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["loaded"])
        self.assertTrue(wait_until(lambda: not self.health()["loaded"], timeout=3.0))
        data = self.health()
        self.assertEqual(data["loads"], 1)
        self.assertEqual(data["unloads"], 1)
        self.assertEqual(data["last_unload_reason"], "idle")

    def test_idle_timer_does_not_unload_during_synthesis(self) -> None:
        self.model.delay = 0.4
        status, _, _ = self.post_json({"text": "慢速合成"})
        self.assertEqual(status, 200)
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["unloads"], 0)
        self.assertTrue(wait_until(lambda: not self.health()["loaded"], timeout=3.0))
        self.assertEqual(self.health()["unloads"], 1)


class NoIdleUnloadTests(ServerTestCase):
    lazy = True
    idle_unload_seconds = 0.0

    def test_disabled_idle_unload_keeps_model_loaded(self) -> None:
        status, _, _ = self.post_json({"text": "你好"})
        self.assertEqual(status, 200)
        time.sleep(0.4)
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["unloads"], 0)
        self.assertEqual(data["idle_unload_seconds"], 0.0)


class LocalOnlyTests(ServerTestCase):
    lazy = True
    idle_unload_seconds = 0.0

    def test_control_endpoints_reject_non_local_clients(self) -> None:
        self.post_json({"text": "你好"})
        with mock.patch.object(server, "is_loopback_address", return_value=False):
            status, _, body = self.request("POST", "/unload")
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["error"], "local_only")
            status, _, body = self.request("POST", "/load")
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["error"], "local_only")
        data = self.health()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["unloads"], 0)

    def test_loopback_detection_helper(self) -> None:
        for value in ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(server.is_loopback_address(value), value)
        for value in (
            "",
            "not-an-ip",
            "10.0.0.1",
            "192.168.1.5",
            "::ffff:192.168.1.5",
        ):
            self.assertFalse(server.is_loopback_address(value), value)


class ParserTests(unittest.TestCase):
    def test_dynamic_defaults(self) -> None:
        args = server.build_parser().parse_args([])
        self.assertTrue(args.lazy_load)
        self.assertEqual(args.idle_unload_seconds, 300.0)

    def test_flags_can_restore_eager_mode_and_disable_unload(self) -> None:
        args = server.build_parser().parse_args(
            ["--no-lazy-load", "--idle-unload-seconds", "0"]
        )
        self.assertFalse(args.lazy_load)
        self.assertEqual(args.idle_unload_seconds, 0.0)

    def test_negative_idle_seconds_is_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.build_parser().parse_args(["--idle-unload-seconds", "-1"])

    def test_start_script_passes_dynamic_defaults(self) -> None:
        script = (ROOT / "scripts" / "start_tts_soft.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("--lazy-load", script)
        self.assertIn("--idle-unload-seconds 300", script)


class PortCheckTests(unittest.TestCase):
    def test_listening_port_is_detected_and_bind_refused(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            self.assertTrue(server.port_in_use("127.0.0.1", port))
            with self.assertRaises(OSError):
                server.make_server("127.0.0.1", port, self._service())
        finally:
            holder.close()

    def _service(self):
        return server.create_app(
            model=FakeModel(), ref=Path("fake_ref.wav"), model_label="fake-model"
        )


class SeedTests(unittest.TestCase):
    def test_seed_is_set_before_every_synthesis(self) -> None:
        class FakeTorch:
            def __init__(self) -> None:
                self.seeds: list[int] = []

            def manual_seed(self, value: int) -> None:
                self.seeds.append(value)

            def inference_mode(self):
                return contextlib.nullcontext()

        torch_module = FakeTorch()
        service = server.create_app(
            model=FakeModel(),
            ref=Path("fake_ref.wav"),
            torch_module=torch_module,
            sample_rate=24000,
        )
        service.synthesize("你好")
        service.synthesize("再见")
        self.assertEqual(torch_module.seeds, [42, 42])


if __name__ == "__main__":
    unittest.main()
