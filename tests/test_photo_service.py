"""Tests for the on-demand Anima Turbo photo service and its integrations.

No real model and no real ComfyUI process is started here: HTTP is served by a
fake ComfyUI, the LLM is a stub, and unit tests always run with
``comfyui_autostart = false`` (or an already-"running" fake).
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.memory import MemoryService
from fatfish.photo import (
    PhotoResult,
    PhotoService,
    friendly_photo_error,
    wants_photo,
)
from fatfish.platforms.base import InboundMessage
from fatfish.platforms.qq import QQAdapter
from fatfish.runtime import Runtime

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg=="
)
PROMPT_TOKEN = "whalephototoken"


class FakeLLM:
    available = True

    def __init__(self, text: str):
        self.text = text
        self.calls: list[tuple[object, dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            model="fake-model",
        )


class _ComfyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib signature
        state = self.server.state
        path = urlsplit(self.path).path
        if path == "/system_stats":
            self._json(200, {"system": {"os": "test"}})
            return
        if path.startswith("/history/"):
            state["history_polls"] += 1
            if state["fail"]:
                self._json(
                    200,
                    {
                        state["prompt_id"]: {
                            "status": {
                                "status_str": "error",
                                "completed": False,
                                "messages": [
                                    ["execution_error", {"exception_message": "boom"}]
                                ],
                            },
                            "outputs": {},
                        }
                    },
                )
                return
            if state["history_polls"] < 2:
                self._json(200, {})
                return
            self._json(
                200,
                {
                    state["prompt_id"]: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "70": {
                                "images": [
                                    {
                                        "filename": "fatfish_photo_00001_.png",
                                        "subfolder": "fatfish_photo",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
            return
        if path == "/view":
            query = parse_qs(urlsplit(self.path).query)
            state["view_queries"].append(query)
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(PNG_BYTES)))
            self.end_headers()
            self.wfile.write(PNG_BYTES)
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802 - stdlib signature
        state = self.server.state
        path = urlsplit(self.path).path
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/prompt":
            if state["fail_submit"]:
                self._json(500, {"error": "prompt_rejected"})
                return
            try:
                state["prompts"].append(json.loads(raw.decode("utf-8")))
            except ValueError:
                state["prompts"].append({})
            self._json(200, {"prompt_id": state["prompt_id"]})
            return
        self._json(404, {"error": "not_found"})


class FakeComfyUI:
    def __init__(self, *, fail: bool = False, fail_submit: bool = False):
        self.state = {
            "prompt_id": "pid-1",
            "prompts": [],
            "history_polls": 0,
            "view_queries": [],
            "fail": fail,
            "fail_submit": fail_submit,
        }
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ComfyHandler)
        self.httpd.daemon_threads = True
        self.httpd.state = self.state
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeComfyUI":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class FakeOneBotClient:
    def __init__(self, *args, **kwargs):
        self.messages: list[tuple[str, str, list[dict]]] = []

    def send_private_text(self, user_id, text):
        return "echo"

    def send_group_text(self, group_id, text):
        return "echo"

    def send_private_message(self, user_id, message):
        self.messages.append(("private", str(user_id), list(message)))
        return "echo-media"

    def send_group_message(self, group_id, message):
        self.messages.append(("group", str(group_id), list(message)))
        return "echo-media"


def make_env(tmp: Path, *, photo_url: str = "http://127.0.0.1:9", autostart: bool = False):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'photo.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[voice]
enabled = false

[tts]
enabled = false
base_url = ""

[sticker]
enabled = false

[photo]
enabled = true
# 2026-11-15: pin the mode -- low.toml now defaults to modelscope (cloud), and
# a test must never reach the network or submit real ModelScope jobs.
mode = "local"
comfyui_url = "{photo_url}"
comfyui_autostart = {"true" if autostart else "false"}
comfyui_python = ""
comfyui_main = ""
comfyui_cwd = ""
models_root = ""
unet = "anima-turbo-v1.1.safetensors"
clip = "qwen_3_06b_base.safetensors"
vae = "qwen_image_vae.safetensors"
lora = "deepseek_whalegirl_v1.safetensors"
lora_strength = 0.8
steps = 8
cfg = 1.0
width = 1024
height = 1024
timeout_seconds = 5
poll_seconds = 0.02
stop_comfyui_after = true
unload_tts_before_start = false
stop_tts_process = false
restart_tts_after = false
min_free_vram_mb = 0
"""
    path = tmp / "photo.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


def _llm_prompt_json() -> str:
    return json.dumps(
        {
            "positive": (
                f"selfie, rainy street at night, transparent umbrella, {PROMPT_TOKEN}"
            ),
            "negative": "blurry, bad hands",
            "caption": "雨夜自拍",
        },
        ensure_ascii=False,
    )


class PhotoDetectionTests(unittest.TestCase):
    def test_keywords(self) -> None:
        for text in ("拍张照", "拍一张", "自拍一张", "来张照片", "帮我拍照"):
            self.assertTrue(wants_photo(text), text)
        # 2026-11-17 云端实测漏掉的真实说法：她只回了「来了来了」，没生成图。
        for text in (
            "能再给我发几张图吗？",
            "多发点表情包和照片吧",
            "给我发两张照片",
            "再来几张自拍",
            "发张图看看",
            "你先给我发几张照片吧",
        ):
            self.assertTrue(wants_photo(text), text)
        for text in (
            "今天好累",
            "发个表情",
            "晚安",
            # 引用/评价已有图片：不能误触发新图
            "你发的图好好笑",
            "这张照片拍得真好",
            "刚才那张图呢",
            "她在群里发的照片",
        ):
            self.assertFalse(wants_photo(text), text)

    def test_image_placeholder_is_never_a_request(self) -> None:
        """适配器合成的图片占位符不是主人的话（2026-11-22 云端实测）。

        qqbot 曾把无文字图片合成「（发来一张图）」，被「发…图」分支命中，
        于是发任何一张图都机械回「好，我去拍」。占位符必须整体剥掉。
        """
        for text in ("[图片]", "[图片]×2", "[图片]\n这张好可爱"):
            self.assertFalse(wants_photo(text), text)
        # 占位符剥掉后，剩下的正文照常判定。
        self.assertTrue(wants_photo("[图片]\n帮我拍一张"), "占位符+明确请求")
        self.assertFalse(wants_photo("[图片]\n今天好累"), "占位符+无关正文")


class PhotoPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)

    def test_llm_prompt_has_character_tags_and_one_call(self) -> None:
        llm = FakeLLM(_llm_prompt_json())
        service = PhotoService(self.config, self.db, llm=llm)
        prompt = service.build_prompt("下雨天在便利店门口自拍")
        self.assertEqual(prompt.source, "llm")
        self.assertIn("whale girl", prompt.positive)
        self.assertIn("blue hair", prompt.positive)
        self.assertIn("whale tail", prompt.positive)
        self.assertIn(PROMPT_TOKEN, prompt.positive)
        self.assertIn("worst quality", prompt.negative)
        self.assertEqual(prompt.caption, "雨夜自拍")
        self.assertEqual(len(llm.calls), 1)
        # 2026-11-15: the gateway requires an x-opencode-session header, so the
        # prompt build must always carry one (turn session, else a stable
        # pseudo-session).  Missing it used to 400 and silently fall back.
        self.assertEqual(llm.calls[0][1].get("session_id"), "photo:prompt")
        self.assertEqual(prompt.composition, "character")
        self.assertEqual(prompt.outfit, "default")
        self.assertIn("maid dress", prompt.positive.lower())

    def test_llm_can_choose_scene_order_and_new_outfit(self) -> None:
        payload = json.loads(_llm_prompt_json())
        payload.update(
            {
                "composition": "scene",
                "outfit": "new",
                "outfit_detail": "white summer sundress with a straw hat",
            }
        )
        llm = FakeLLM(json.dumps(payload, ensure_ascii=False))
        service = PhotoService(self.config, self.db, llm=llm)
        prompt = service.build_prompt("穿着新裙子在海边跑")
        self.assertEqual(prompt.composition, "scene")
        self.assertEqual(prompt.outfit, "new")
        self.assertIn("summer sundress", prompt.positive.lower())
        self.assertNotIn("maid dress", prompt.positive.lower())

    def test_build_prompt_forwards_the_turn_session(self) -> None:
        """The prompt-build LLM call inherits the conversation's session."""
        llm = FakeLLM(_llm_prompt_json())
        service = PhotoService(self.config, self.db, llm=llm)
        service.build_prompt("自拍一张", session_id="qq:private:10001")
        self.assertEqual(llm.calls[0][1].get("session_id"), "qq:private:10001")

    def test_fallback_character_first_default_outfit(self) -> None:
        service = PhotoService(self.config, self.db, llm=None)
        prompt = service.build_prompt("来自拍一张")
        self.assertEqual(prompt.composition, "character")
        self.assertEqual(prompt.outfit, "default")
        self.assertIn("maid dress", prompt.positive.lower())

    def test_fallback_scene_first_when_action_is_central(self) -> None:
        service = PhotoService(self.config, self.db, llm=None)
        prompt = service.build_prompt("在海边跑起来")
        self.assertEqual(prompt.composition, "scene")

    def test_fallback_on_invalid_llm_json(self) -> None:
        service = PhotoService(self.config, self.db, llm=FakeLLM("not json"))
        prompt = service.build_prompt("来张照片")
        self.assertEqual(prompt.source, "fallback")
        self.assertIn("whale girl", prompt.positive)
        self.assertIn("1girl", prompt.positive)

    def test_fallback_when_llm_unavailable(self) -> None:
        service = PhotoService(self.config, self.db, llm=None)
        prompt = service.build_prompt("海边自拍")
        self.assertEqual(prompt.source, "fallback")
        self.assertIn("seaside", prompt.positive)

    def test_friendly_errors(self) -> None:
        self.assertIn("显存不足", friendly_photo_error("vram_conflict_tts"))
        self.assertIn("TTS", friendly_photo_error("vram_conflict_tts"))
        self.assertIn("上一张", friendly_photo_error("busy"))
        self.assertIn("没拍成", friendly_photo_error("something_unknown"))


class PhotoLazyManagerTests(unittest.TestCase):
    def test_status_does_not_probe_the_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, db = make_env(Path(tmp))
            try:
                with mock.patch(
                    "fatfish.photo.service.discover_service_manager", return_value=None
                ) as discover:
                    service = PhotoService(config, db)
                    self.assertFalse(discover.called)
                    service.status()
                    self.assertFalse(
                        discover.called, "status() must not start discovery"
                    )
                    ok, _ = service._manager_call(("ensure_comfyui",))
                    self.assertFalse(ok)
                    self.assertTrue(
                        discover.called, "a real job may discover lazily"
                    )
            finally:
                db.close()


class PhotoGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _service(self, fake: FakeComfyUI, **kwargs):
        config, db = make_env(self.root, photo_url=fake.url)
        self.addCleanup(db.close)
        return config, db, PhotoService(config, db, **kwargs)

    def test_graph_uses_turbo_and_lora_and_saves_png(self) -> None:
        with FakeComfyUI() as fake:
            config, db, service = self._service(fake)
            result = service.generate(
                "",
                prompt=service.manual_prompt(
                    "selfie, rainy street at night, close-up"
                ),
                seed=1234,
            )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.path)
            path = Path(str(result.path))
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), PNG_BYTES)
            self.assertEqual(path.parent, config.data_dir / "photos")
            self.assertFalse(result.comfyui_started)

            graph = fake.state["prompts"][0]["prompt"]
            self.assertEqual(
                graph["13"]["inputs"]["unet_name"],
                "anima-turbo-v1.1.safetensors",
            )
            self.assertEqual(
                graph["40"]["inputs"]["lora_name"],
                "deepseek_whalegirl_v1.safetensors",
            )
            self.assertAlmostEqual(graph["40"]["inputs"]["strength_model"], 0.8)
            self.assertEqual(graph["50"]["inputs"]["steps"], 8)
            self.assertAlmostEqual(graph["50"]["inputs"]["cfg"], 1.0)
            self.assertEqual(graph["50"]["inputs"]["sampler_name"], "euler")
            self.assertEqual(graph["50"]["inputs"]["scheduler"], "simple")
            self.assertEqual(graph["12"]["inputs"]["width"], 1024)
            self.assertEqual(graph["12"]["inputs"]["height"], 1024)
            self.assertIn("whale girl", graph["20"]["inputs"]["text"])
            self.assertTrue(
                graph["70"]["inputs"]["filename_prefix"].startswith(
                    "fatfish_photo/"
                )
            )
            # Polling happened at least once before output was available.
            self.assertGreaterEqual(fake.state["history_polls"], 2)

            row = db.query_one("SELECT * FROM photo_jobs WHERE id=?", (result.job_id,))
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["image_path"], str(path))

    def test_prompt_is_not_stored_in_conversation_or_memory(self) -> None:
        with FakeComfyUI() as fake:
            config, db, service = self._service(
                fake, llm=FakeLLM(_llm_prompt_json())
            )
            result = service.generate("下雨天自拍")
            self.assertTrue(result.ok, result.error)
            memory = MemoryService(db, config)
            turns = memory.recent_turns("qq:private:1", limit=50)
            self.assertEqual(turns, [])
            for table, columns in (
                ("conversation_turns", ("content",)),
                ("memory_items", ("gist", "summary")),
                ("rag_chunks", ("text",)),
            ):
                for column in columns:
                    row = db.query_one(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE {column} LIKE ?",
                        (f"%{PROMPT_TOKEN}%",),
                    )
                    self.assertEqual(int(row["n"]), 0, f"{table}.{column}")
            row = db.query_one(
                "SELECT positive FROM photo_jobs WHERE id=?", (result.job_id,)
            )
            self.assertIn(PROMPT_TOKEN, row["positive"])

    def test_comfyui_execution_error_returns_error(self) -> None:
        with FakeComfyUI(fail=True) as fake:
            _, _, service = self._service(fake)
            result = service.generate("", prompt=service.manual_prompt("selfie"))
            self.assertFalse(result.ok)
            self.assertIsNone(result.path)
            self.assertTrue(str(result.error).startswith("comfyui_execution_error"))

    def test_submit_failure_returns_error(self) -> None:
        with FakeComfyUI(fail_submit=True) as fake:
            _, _, service = self._service(fake)
            result = service.generate("", prompt=service.manual_prompt("selfie"))
            self.assertFalse(result.ok)
            self.assertIn("comfyui_submit_error", str(result.error))

    def test_not_running_without_autostart(self) -> None:
        config, db = make_env(self.root, photo_url="http://127.0.0.1:9", autostart=False)
        self.addCleanup(db.close)
        service = PhotoService(config, db)
        result = service.generate("", prompt=service.manual_prompt("selfie"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "comfyui_not_running")

    def test_busy_returns_quickly(self) -> None:
        with FakeComfyUI() as fake:
            _, _, service = self._service(fake)
            acquired = service._lock.acquire(blocking=False)  # noqa: SLF001
            self.assertTrue(acquired)
            try:
                result = service.generate("", prompt=service.manual_prompt("selfie"))
            finally:
                service._lock.release()  # noqa: SLF001
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "busy")
            self.assertEqual(fake.state["prompts"], [])

    def test_second_runtime_is_busy_while_another_job_owns_the_process(self) -> None:
        # Web constructs a Runtime (and therefore a PhotoService) per turn.
        # The instance lock cannot serialise those, so the module-level lock
        # must reject a second job even when it is a different object.
        from fatfish.photo.service import _PROCESS_JOB_LOCK

        with FakeComfyUI() as fake:
            _, _, service = self._service(fake)
            self.assertTrue(_PROCESS_JOB_LOCK.acquire(blocking=False))
            try:
                result = service.generate("", prompt=service.manual_prompt("selfie"))
            finally:
                _PROCESS_JOB_LOCK.release()
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "busy")
            self.assertEqual(fake.state["prompts"], [])

    def test_vram_lease_busy_fails_before_starting_comfyui(self) -> None:
        from fatfish.process_lock import acquire as acquire_lock

        with FakeComfyUI() as fake:
            _, _, service = self._service(fake)
            lease = acquire_lock(service._vram_lock_path)  # noqa: SLF001
            self.assertIsNotNone(lease)
            assert lease is not None
            try:
                result = service.generate("", prompt=service.manual_prompt("selfie"))
            finally:
                lease.release()
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "vram_busy")
            self.assertEqual(fake.state["prompts"], [])

    def test_vram_conflict_reports_tts_instead_of_killing_it(self) -> None:
        config, db = make_env(self.root, photo_url="http://127.0.0.1:9", autostart=True)
        self.addCleanup(db.close)
        service = PhotoService(config, db)
        service.min_free_vram_mb = 7000
        service.stop_tts_process = False
        with mock.patch.object(service, "_tts_reachable", return_value=True), mock.patch.object(
            service, "_tts_loaded", return_value=True
        ), mock.patch.object(service, "_try_unload_tts", return_value=False), mock.patch.object(
            service, "_gpu_free_mb", return_value=1024
        ), mock.patch.object(
            service, "_missing_paths", return_value=[]
        ), mock.patch.object(
            service, "asset_report", return_value={}
        ):
            ok, error, started = service._ensure_comfyui()  # noqa: SLF001
        self.assertFalse(ok)
        self.assertEqual(error, "vram_conflict_tts")
        self.assertFalse(started)


class PhotoRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config, self.db = make_env(Path(self.tmp.name))
        self.addCleanup(self.db.close)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.llm = FakeLLM(
            json.dumps({"reply": "好呀", "messages": ["好呀"]}, ensure_ascii=False)
        )

    def _stub(self, result: PhotoResult | None = None) -> object:
        class Stub:
            enabled = True

            def __init__(self) -> None:
                self.calls = []

            def generate(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return result or PhotoResult(
                    True, path="data/photos/test.png", caption="测试"
                )

        stub = Stub()
        self.runtime.photo = stub
        return stub

    def test_private_photo_appends_note_and_returns_payload(self) -> None:
        stub = self._stub()
        result = self.runtime.handle_user_message("qq:private:1001", "来张照片")
        self.assertEqual(len(stub.calls), 1)
        self.assertTrue(result["photo"]["ok"])
        self.assertEqual(result["photo"]["path"], "data/photos/test.png")
        self.assertTrue(any("拍好啦" in message for message in result["messages"]))
        turns = self.runtime.memory.recent_turns("qq:private:1001", limit=10)
        self.assertTrue(any("拍好啦" in str(t["content"]) for t in turns))

    def test_group_photo_is_not_generated(self) -> None:
        stub = self._stub()
        result = self.runtime.handle_user_message(
            "qq:group:2001", "来张照片", is_group=True
        )
        self.assertEqual(stub.calls, [])
        self.assertIsNone(result["photo"])

    def test_photo_failure_keeps_text_reply(self) -> None:
        self._stub(PhotoResult(False, error="vram_conflict_tts"))
        result = self.runtime.handle_user_message("qq:private:1002", "拍一张")
        self.assertFalse(result["photo"]["ok"])
        self.assertEqual(result["reply"].splitlines()[0], "好呀")
        self.assertTrue(any("显存不足" in message for message in result["messages"]))

    def test_disabled_photo_explains_it(self) -> None:
        self.runtime.photo.enabled = False
        result = self.runtime.handle_user_message("qq:private:1003", "自拍一张")
        self.assertFalse(result["photo"]["ok"])
        self.assertEqual(result["photo"]["error"], "disabled")
        self.assertTrue(any("还没开" in message for message in result["messages"]))


class PhotoQQTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)
        self.runtime = Runtime(self.db, self.config)
        self.adapter = QQAdapter(self.runtime, self.config)
        self.fake = FakeOneBotClient()
        self.adapter.client = self.fake
        self.image = self.root / "photo.png"
        self.image.write_bytes(PNG_BYTES)

    def _message(self, *, group: bool = False) -> InboundMessage:
        return InboundMessage(
            platform="qq",
            event_id="evt-photo",
            session_id="qq:group:2" if group else "qq:private:1",
            user_id="1",
            group_id="2" if group else None,
            text="来张照片",
            is_group=group,
        )

    def test_async_photo_outbox_keeps_reply_to(self) -> None:
        class FakeAsyncPhoto:
            def __init__(self, path: str):
                self.path = path

            def poll_modelscope_jobs(self, *, now=None):
                return [
                    {
                        "ok": True,
                        "job_id": "job-1",
                        "session_id": "qq:private:1",
                        "path": self.path,
                        "caption": "自拍",
                        "reply_to": "MSG-1",
                    }
                ]

        self.runtime.photo = FakeAsyncPhoto(str(self.image))
        result = self.runtime._poll_photo_jobs(now=time.time())  # noqa: SLF001
        self.assertEqual(result["done"], 1)
        row = self.db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='qq_send_image'"
        )
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["reply_to"], "MSG-1")
        self.assertEqual(payload["path"], str(self.image))

    def test_private_photo_is_queued_and_sent_as_image(self) -> None:
        queued, errors, item_ids = self.adapter._queue_media_result(  # noqa: SLF001
            {"photo": {"ok": True, "path": str(self.image)}},
            target_type="private",
            target_id="1",
            event_id="evt-photo",
            is_group=False,
        )
        self.assertEqual(queued, 1)
        self.assertEqual(errors, [])
        self.assertEqual(len(item_ids), 1)
        results = self.runtime.process_outbox(handlers=self.adapter.handlers())
        self.assertEqual(results[0]["status"], "done")
        target_type, target_id, segments = self.fake.messages[0]
        self.assertEqual((target_type, target_id), ("private", "1"))
        self.assertEqual(segments[0]["type"], "image")
        encoded = segments[0]["data"]["file"].split("base64://", 1)[1]
        self.assertEqual(base64.b64decode(encoded), PNG_BYTES)

    def test_group_photo_is_not_queued(self) -> None:
        queued, errors, _ = self.adapter._queue_media_result(  # noqa: SLF001
            {"photo": {"ok": True, "path": str(self.image)}},
            target_type="group",
            target_id="2",
            event_id="evt-photo",
            is_group=True,
        )
        self.assertEqual(queued, 0)
        self.assertEqual(errors, [])

    def test_failed_photo_is_not_queued(self) -> None:
        queued, errors, _ = self.adapter._queue_media_result(  # noqa: SLF001
            {"photo": {"ok": False, "path": None, "error": "busy"}},
            target_type="private",
            target_id="1",
            event_id="evt-photo",
            is_group=False,
        )
        self.assertEqual(queued, 0)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
