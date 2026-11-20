"""Cloud-side photo generation (T-0068): degrade, never hang.

On the cloud tier the GPU lives at home, so ``[photo] mode = "remote"`` posts to
the home box instead of driving ComfyUI locally.  What matters here is not the
happy path but everything around it: a hibernating home box, a dead socket, a
500, or a 200 carrying HTML.  Every one of those must come back as *one
sentence* immediately -- never as a 30-120s wait for a ComfyUI cold start.

No real ComfyUI, no real network: the transport is a fake with the same two
methods the singing client exposes (``reachable`` / ``post``), so phase B can
swap in the shared client without touching these tests.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `from scripts.photo_server import ...`
    sys.path.insert(0, str(ROOT))

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.photo.service import (
    PhotoPrompt,
    PhotoResult,
    PhotoService,
    RemotePhotoClient,
    friendly_photo_error,
)
from fatfish.remote import HealthProbe, build_probe
from scripts.photo_server import (
    LocalPhotographer,
    PhotoApp,
    RenderOutcome,
    http_status_for,
    make_server,
)

logging.getLogger("photo_server").setLevel(logging.CRITICAL)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg=="
)


def make_env(tmp: Path, *, mode: str = "remote", remote_url: str = "https://home.test:9446"):
    data = tmp / "data"
    config_path = tmp / "photo_remote.toml"
    config_path.write_text(
        f"""
[app]
profile = "test"
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

[remote]
enabled = true
health_ttl_seconds = 30
timeout_seconds = 2

[photo]
enabled = true
mode = "{mode}"
remote_url = "{remote_url}"
remote_timeout_seconds = 180
comfyui_autostart = false
comfyui_url = "http://127.0.0.1:9"
""",
        encoding="utf-8",
    )
    config = load_config("test", config_path)
    db = Database(config.db_path)
    db.initialize()
    return config, db


class FakeClient:
    """Same shape as ``RemoteSingerClient``: cheap probe + one long POST."""

    def __init__(self, *, reachable: bool = True, reply=None, raises: BaseException | None = None):
        self._reachable = reachable
        self._reply = reply
        self._raises = raises
        self.posted: list[dict] = []

    def reachable(self) -> bool:
        return self._reachable

    def post(self, payload: dict):
        self.posted.append(payload)
        if self._raises is not None:
            raise self._raises
        return self._reply


class RemotePhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)

    def _service(self, client=None) -> PhotoService:
        return PhotoService(self.config, self.db, remote_client=client)

    # -- happy path -------------------------------------------------------
    def test_png_comes_back_and_lands_in_data_photos(self) -> None:
        client = FakeClient(reply=(200, PNG_BYTES, {"content-type": "image/png"}))
        service = self._service(client)

        result = service.generate("来张照片", session_id="qq:private:1")

        self.assertTrue(result.ok, result)
        self.assertTrue(Path(result.path).exists())
        self.assertEqual(Path(result.path).read_bytes(), PNG_BYTES)
        self.assertEqual(len(client.posted), 1)
        self.assertTrue(client.posted[0]["prompt"])

    # -- degraded paths ---------------------------------------------------
    def test_home_offline_returns_a_sentence_without_posting(self) -> None:
        client = FakeClient(reachable=False)
        service = self._service(client)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "home_offline")
        self.assertEqual(client.posted, [])
        self.assertIn("没连上", friendly_photo_error(result.error))

    def test_timeout_is_reported_as_remote_timeout(self) -> None:
        client = FakeClient(raises=TimeoutError("read timed out"))
        service = self._service(client)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_timeout")
        self.assertIn("没回音", friendly_photo_error(result.error))

    def test_http_500_uses_the_body_error_code(self) -> None:
        # 与 sing 同一套约定：家里给的 ``error`` 比 HTTP 码更精确，优先用它；
        # 拿不到 JSON 时才退回 ``remote_error:<status>``（下面那条）。
        client = FakeClient(reply=(500, b'{"error": "comfyui_execution_error:boom"}', {}))
        service = self._service(client)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "comfyui_execution_error:boom")

    def test_http_500_without_a_body_error_keeps_the_status(self) -> None:
        client = FakeClient(reply=(500, b"<html>oops</html>", {}))
        service = self._service(client)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_error:500")

    def test_html_instead_of_png_is_refused(self) -> None:
        client = FakeClient(reply=(200, b"<html>not an image</html>", {}))
        service = self._service(client)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_bad_photo")
        self.assertIsNone(result.path)

    def test_missing_remote_url_is_reported_not_guessed(self) -> None:
        config, db = make_env(self.root, mode="remote", remote_url="")
        self.addCleanup(db.close)
        service = PhotoService(config, db)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_not_configured")

    # -- mode / probe -----------------------------------------------------
    def test_default_mode_is_local(self) -> None:
        config, db = make_env(self.root, mode="local")
        self.addCleanup(db.close)
        self.assertEqual(PhotoService(config, db).mode, "local")

    def test_probe_registers_photo_as_optional(self) -> None:
        probe = build_probe(self.config)

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertIn("photo", probe.endpoints)
        self.assertIn("photo", probe.optional)
        # a dead photo endpoint is a capability bit, not "home is offline"
        self.assertFalse(probe.check("photo", force=True).ok)
        self.assertFalse(probe.photo_online())
        # no required endpoint is configured in this profile, so home is not
        # claimed to be online either
        self.assertFalse(probe.home_online())

    def test_probe_without_remote_photo_has_no_photo_endpoint(self) -> None:
        config, db = make_env(self.root, mode="local")
        self.addCleanup(db.close)

        probe = build_probe(config)

        self.assertIsNone(probe)  # no TTS home_url and no endpoints configured

    def test_probe_budget_is_seconds_not_the_drawing_timeout(self) -> None:
        # 180s is how long a cold ComfyUI may take to *draw*; deciding "the box is
        # gone" must cost seconds, or every photo request stalls the reply.
        probe = build_probe(self.config)
        assert probe is not None
        self.assertLessEqual(probe.timeout, 5.0)

    def test_friendly_text_covers_every_remote_code(self) -> None:
        for code in (
            "home_offline",
            "remote_not_configured",
            "remote_timeout",
            "remote_bad_photo",
            "remote_error:503",
        ):
            text = friendly_photo_error(code)
            self.assertTrue(text and text != "拍照没拍成，主人哥哥等会儿再试试。", code)


class RealTransportTests(unittest.TestCase):
    """The 180s budget is for drawing, not for discovering the box is asleep."""

    def test_reachable_gives_up_fast_on_a_dead_local_port(self) -> None:
        client = RemotePhotoClient(
            "http://127.0.0.1:9", timeout=180.0, connect_timeout=2.0
        )
        started = time.monotonic()
        self.assertFalse(client.reachable())
        self.assertLess(time.monotonic() - started, 5.0)

    def test_reachable_is_false_without_a_host(self) -> None:
        self.assertFalse(RemotePhotoClient("").reachable())

    def test_dead_transport_still_returns_one_sentence(self) -> None:
        # No fake here: the real client against a closed port must surface as
        # ``home_offline`` without ever reaching the 180s POST budget.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config, db = make_env(Path(tmp.name), remote_url="http://127.0.0.1:9")
        self.addCleanup(db.close)
        service = PhotoService(config, db)

        started = time.monotonic()
        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "home_offline")
        self.assertLess(time.monotonic() - started, 5.0)


class ProbeContractTests(unittest.TestCase):
    """The optional-endpoint rule, pinned without a photo service in the way."""

    def test_optional_endpoint_never_decides_home_online(self) -> None:
        def only_photo_dies(request, timeout):
            if "photo" in request.full_url:
                raise OSError("unreachable")

        probe = HealthProbe(
            endpoints={"tts": "http://home/health", "photo": "http://home/photo/health"},
            optional={"photo"},
            ttl=0.0,
            opener=only_photo_dies,
            clock=lambda: 0.0,
        )
        self.assertFalse(probe.photo_online())
        self.assertTrue(probe.home_online())

    def test_status_marks_the_optional_flag(self) -> None:
        probe = HealthProbe(
            endpoints={"photo": "http://home/photo/health"},
            optional={"photo"},
            opener=lambda request, timeout: None,
            clock=lambda: 0.0,
        )
        status = probe.status()
        self.assertTrue(status["endpoints"]["photo"]["optional"])
        self.assertTrue(status["photo_online"])


# --------------------------------------------------------------------------- #
# scripts/photo_server.py：HTTP 层（T-0068 第二段）
#
# 全部 mock：不跑 ComfyUI、不启子进程、不碰 data/run/vram.lock。HTTP 那一段
# 用真的 http.server（随机端口 + 127.0.0.1），要验的是 401 / 409 / 200 image/png
# 这些协议细节，而不是函数返回值。
# --------------------------------------------------------------------------- #
class FakePhotographer:
    """``LocalPhotographer`` 的替身：说清「被调用了什么」，不画图。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises: BaseException | None = None
        self.release: threading.Event | None = None
        self.png = PNG_BYTES

    def render(self, **kwargs: Any) -> RenderOutcome:
        self.calls.append(kwargs)
        if self.release is not None:
            self.release.wait(5)
        if self.raises is not None:
            raise self.raises
        return RenderOutcome(ok=True, png=self.png, job_id="job-photo-1", caption="给你画好啦")

    def health(self, *, busy: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "busy": busy,
            "mode": "local",
            "comfyui_url": "http://127.0.0.1:8188",
        }


class PhotoServerCase(unittest.TestCase):
    token = "s3cret"

    def setUp(self) -> None:
        self.backend = FakePhotographer()
        self.app = PhotoApp(self.backend, timeout=150)
        self.httpd = make_server("127.0.0.1", 0, self.app, token=self.token)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}
        )
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    # -- HTTP helpers（绕开系统代理，别让环境变量把 127.0.0.1 也代理走）
    def _opener(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="GET")
        try:
            with self._opener().open(request, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def post(self, payload: dict[str, Any], *, token: str | None = None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Token"] = token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/photo", data=body, method="POST", headers=headers
        )
        try:
            with self._opener().open(request, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()


class PhotoServerProtocolTests(PhotoServerCase):
    def test_health_is_200_and_needs_no_token(self) -> None:
        status, _, body = self.get("/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"ok", "busy", "mode", "comfyui_url"})
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["busy"])

    def test_wrong_token_is_401_and_never_reaches_the_renderer(self) -> None:
        status, _, body = self.post({"prompt": "一只猫"}, token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")
        self.assertEqual(self.backend.calls, [])

    def test_missing_token_is_401(self) -> None:
        self.assertEqual(self.post({"prompt": "一只猫"})[0], 401)

    def test_success_returns_png_bytes_and_job_header(self) -> None:
        status, headers, body = self.post({"prompt": "一只猫"}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(headers.get("X-Job-Id"), "job-photo-1")
        self.assertEqual(body, PNG_BYTES)
        self.assertEqual(self.backend.calls[0]["positive"], "一只猫")

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self.get("/nope")[0], 404)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/nope",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Token": self.token},
        )
        try:
            with self._opener().open(request, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)

    def test_bad_json_is_400(self) -> None:
        self.assertEqual(self.post({}, token=self.token, raw=b"{not json")[0], 400)

    def test_missing_prompt_is_400(self) -> None:
        status, _, body = self.post({"negative": "blurry"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "prompt_missing")
        self.assertEqual(self.backend.calls, [])

    def test_render_exception_is_500_and_the_gate_is_released(self) -> None:
        self.backend.raises = RuntimeError("boom")
        status, _, body = self.post({"prompt": "一只猫"}, token=self.token)
        self.assertEqual(status, 500)
        self.assertIn("server_error", json.loads(body)["error"])
        # 例外之后闸门必须放回去：下一张不能被上一张的异常永久堵死。
        self.backend.raises = None
        self.assertEqual(self.post({"prompt": "一只猫"}, token=self.token)[0], 200)
        self.assertFalse(json.loads(self.get("/health")[2])["busy"])

    def test_second_request_while_rendering_is_409(self) -> None:
        release = threading.Event()
        self.backend.release = release
        codes: list[int] = []

        def first() -> None:
            codes.append(self.post({"prompt": "一张慢图"}, token=self.token)[0])

        worker = threading.Thread(target=first)
        worker.daemon = True
        worker.start()
        deadline = time.monotonic() + 5
        while not self.backend.calls and time.monotonic() < deadline:
            time.sleep(0.01)

        status, _, body = self.post({"prompt": "另一张"}, token=self.token)
        release.set()
        worker.join(5)

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "busy")
        self.assertEqual(codes, [200])
        # 忙的时候给的是人话，不是把 busy 原样吐给用户
        self.assertTrue(json.loads(body)["message"])


class PhotoServerStatusMappingTests(unittest.TestCase):
    def test_busy_codes_map_to_409(self) -> None:
        for code in ("busy", "sing_busy", "photo_busy", "vram_busy", "vram_conflict_tts"):
            self.assertEqual(http_status_for(code), 409, code)

    def test_timeout_maps_to_504(self) -> None:
        self.assertEqual(http_status_for("comfyui_timeout"), 504)

    def test_asset_problems_map_to_503(self) -> None:
        for code in (
            "disabled",
            "comfyui_not_running",
            "comfyui_start_timeout",
            "missing_assets",
            "paths_missing",
        ):
            self.assertEqual(http_status_for(code), 503, code)

    def test_generic_failure_maps_to_500(self) -> None:
        self.assertEqual(http_status_for("comfyui_execution_error:boom"), 500)
        self.assertEqual(http_status_for("render_failed"), 500)

    def test_every_code_it_produces_has_a_human_sentence_on_the_cloud(self) -> None:
        # 家里回什么，云端就得有一句人话；不然用户看到的会是兜底句。
        default = friendly_photo_error("definitely-not-a-code")
        for code in ("busy", "vram_busy", "vram_conflict_tts", "unauthorized", "prompt_missing"):
            self.assertNotEqual(friendly_photo_error(code), default, code)


class PhotoAppSummaryTests(unittest.TestCase):
    """每张图一行摘要：默认人读，``--emit-json`` 时机器读。"""

    def _app(self, *, emit_json: bool) -> PhotoApp:
        return PhotoApp(FakePhotographer(), timeout=150, emit_json=emit_json)

    def test_emit_json_writes_one_parseable_line(self) -> None:
        app = self._app(emit_json=True)
        with self.assertLogs("photo_server", level="INFO") as captured:
            app.render(positive="一只猫")

        lines = [r.getMessage() for r in captured.records if r.getMessage().startswith("summary ")]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0][len("summary ") :])
        self.assertEqual(payload["event"], "photo_job")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["job_id"], "job-photo-1")
        self.assertEqual(payload["bytes"], len(PNG_BYTES))
        self.assertEqual(payload["error"], "")

    def test_default_summary_is_human_readable(self) -> None:
        app = self._app(emit_json=False)
        with self.assertLogs("photo_server", level="INFO") as captured:
            app.render(positive="一只猫")

        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(any(m.startswith("job=") for m in messages), messages)
        self.assertFalse(any(m.startswith("summary ") for m in messages), messages)

    def test_rejected_busy_job_is_also_logged(self) -> None:
        # gate 拒绝的那张没有 service 结果，但云端日志得能看出「409 是因为上一张没完」。
        app = self._app(emit_json=True)
        app._gate.acquire()
        try:
            with self.assertLogs("photo_server", level="INFO") as captured:
                outcome = app.render(positive="一只猫")
        finally:
            app._gate.release()

        self.assertEqual(outcome.error, "busy")
        line = [r.getMessage() for r in captured.records if r.getMessage().startswith("summary ")][0]
        self.assertEqual(json.loads(line[len("summary ") :])["error"], "busy")


class FakePhotoService:
    """``PhotoService`` 的替身：只关心「被传了什么」，不碰 ComfyUI / 数据库。"""

    def __init__(self, *, raises: BaseException | None = None, write: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises
        self.write = write
        self.timeout = 300.0
        self.width = 1024
        self.height = 1024
        self.db = None

    def generate(self, user_text: str = "", *, prompt=None, session_id: str = "", seed=None):
        self.calls.append(
            {
                "user_text": user_text,
                "prompt": prompt,
                "session_id": session_id,
                "seed": seed,
                "timeout": self.timeout,
                "size": (self.width, self.height),
            }
        )
        if self.raises is not None:
            raise self.raises
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "shot.png"
        if self.write:
            path.write_bytes(PNG_BYTES)
        return PhotoResult(
            ok=True,
            path=str(path),
            caption=getattr(prompt, "caption", ""),
            job_id="job-local-1",
        )


class LocalPhotographerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)

    def _photographer(self, service: Any) -> LocalPhotographer:
        return LocalPhotographer(self.config, timeout=150, service_factory=lambda: service)

    def test_timeout_and_size_reach_the_service(self) -> None:
        service = FakePhotoService()
        outcome = self._photographer(service).render(
            positive="一只猫", negative="blurry", caption="猫", seed=7, width=512, height=768
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.png, PNG_BYTES)
        call = service.calls[0]
        self.assertEqual(call["timeout"], 150)
        self.assertEqual(call["size"], (512, 768))
        self.assertEqual(call["seed"], 7)
        self.assertEqual(call["session_id"], "photo-server")
        self.assertEqual(call["prompt"].positive, "一只猫")

    def test_empty_prompt_never_touches_the_service(self) -> None:
        service = FakePhotoService()
        outcome = self._photographer(service).render(positive="   ")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "prompt_missing")
        self.assertEqual(service.calls, [])

    def test_missing_png_is_a_storage_error_not_an_exception(self) -> None:
        outcome = self._photographer(FakePhotoService(write=False)).render(positive="一只猫")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "photo_storage_error")

    def test_service_exception_is_a_server_error(self) -> None:
        outcome = self._photographer(FakePhotoService(raises=RuntimeError("boom"))).render(
            positive="一只猫"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "server_error:generate")

    def test_health_never_touches_the_vram_lease(self) -> None:
        # /health 只是探针；它一 acquire 就会创建 data/run/vram.lock，等于在
        # 健康检查里偷偷占显卡。这条把「只读」钉住。
        photographer = LocalPhotographer(self.config)
        payload = photographer.health(busy=False)
        self.assertTrue(payload["ok"])
        self.assertFalse((self.root / "data" / "run" / "vram.lock").exists())


class RemoteErrorBodyTests(unittest.TestCase):
    """409 之类不能只剩一个状态码——家里的 ``error`` 字段才是给人看的那句。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)

    def _result(self, reply):
        service = PhotoService(self.config, self.db, remote_client=FakeClient(reply=reply))
        return service.generate("来张照片")

    def test_409_body_error_becomes_the_code(self) -> None:
        result = self._result((409, b'{"error": "vram_busy"}', {}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "vram_busy")
        self.assertIn("显卡", friendly_photo_error(result.error))

    def test_401_body_error_is_unauthorized(self) -> None:
        result = self._result((401, b'{"error": "unauthorized"}', {}))
        self.assertEqual(result.error, "unauthorized")
        self.assertIn("token", friendly_photo_error(result.error))


if __name__ == "__main__":
    unittest.main()