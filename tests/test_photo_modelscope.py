"""``[photo] mode = "modelscope"``: cloud LoRA generation, no local GPU.

The transport is faked here; this module only pins the contract around it:
the LoRA model id is what ``images/generations`` receives, the trigger word is
added once, the image lands in ``data/photos`` with a real suffix, and every
failure becomes one friendly sentence instead of an exception.

Wire-level probing (2026-11-12, real API) lives outside the test suite:
``sunny1155665/whalegirl-moss-v2`` -> 31s, 1.2 MB PNG.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.photo.modelscope import (
    ModelScopePhotoClient,
    ModelScopePhotoError,
    _extract_images,
    _looks_like_image,
)
from fatfish.photo.service import (
    PhotoPrompt,
    PhotoService,
    friendly_photo_error,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg=="
)


def make_env(tmp: Path) -> tuple[Any, Database]:
    data = tmp / "data"
    config_path = tmp / "photo_modelscope.toml"
    config_path.write_text(
        f"""
[app]
profile = "test"
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'photo.db').as_posix()}"

[voice]
enabled = false

[tts]
enabled = false
base_url = ""

[photo]
enabled = true
mode = "modelscope"
modelscope_model = "sunny1155665/whalegirl-moss-v2"
modelscope_accel_lora = "DiffSynth-Studio/MOSS-Image-SFT-TurboLoRA"
modelscope_steps = 8
modelscope_guidance = 1.0
modelscope_trigger = "whalegirl"
modelscope_api_key_env = "FATFISH_TEST_MISSING_KEY"
modelscope_timeout_seconds = 60
modelscope_poll_seconds = 0.5
""",
        encoding="utf-8",
    )
    config = load_config("test", config_path)
    db = Database(config.db_path)
    db.initialize()
    return config, db


class FakeModelScopeClient:
    available = True

    def __init__(self, *, reply: bytes = PNG_BYTES, raises: BaseException | None = None):
        self.reply = reply
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        positive: str,
        negative: str = "",
        *,
        seed: int | None = None,
        width: int = 1024,
        height: int = 1024,
    ) -> bytes:
        self.calls.append(
            {
                "positive": positive,
                "negative": negative,
                "seed": seed,
                "width": width,
                "height": height,
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.reply


class FakeAsyncModelScopeClient:
    available = True

    def __init__(self):
        self.submitted: list[dict[str, Any]] = []
        self.polls = 0

    def build_payload(self, positive, negative="", *, seed=None, width=1024, height=1024):
        return {
            "model": "sunny1155665/whalegirl-moss-v2",
            "prompt": positive,
            "negative_prompt": negative,
            "size": f"{width}x{height}",
        }

    def submit(self, payload):
        self.submitted.append(payload)
        return "TASK-1"

    def poll(self, task_id):
        self.polls += 1
        return {"status": "succeeded", "url": "https://cdn.invalid/image.png"}

    def download(self, url):
        return PNG_BYTES


class ModelScopeAsyncPhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)

    def test_submit_then_poll_saves_image_and_returns_job(self) -> None:
        client = FakeAsyncModelScopeClient()
        service = PhotoService(self.config, self.db, modelscope_client=client)
        result = service.start_modelscope_job(
            "在海边跑，穿新裙子",
            session_id="qq:private:USER-1",
            reply_to="MSG-1",
        )
        self.assertTrue(result.ok, result)
        self.assertTrue(result.pending)
        self.assertTrue(result.job_id)
        row = self.db.query_one(
            "SELECT provider, task_id, status, reply_to FROM photo_jobs WHERE id=?",
            (result.job_id,),
        )
        self.assertEqual(row["provider"], "modelscope")
        self.assertEqual(row["task_id"], "TASK-1")
        self.assertEqual(row["status"], "waiting")
        self.assertEqual(row["reply_to"], "MSG-1")

        jobs = service.poll_modelscope_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["ok"])
        self.assertTrue(Path(jobs[0]["path"]).exists())
        self.assertEqual(Path(jobs[0]["path"]).read_bytes(), PNG_BYTES)
        self.assertEqual(jobs[0]["reply_to"], "MSG-1")
        done = self.db.query_one(
            "SELECT status, image_path FROM photo_jobs WHERE id=?", (result.job_id,)
        )
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["image_path"])

    def test_second_submit_while_pending_returns_busy(self) -> None:
        service = PhotoService(self.config, self.db, modelscope_client=FakeAsyncModelScopeClient())
        first = service.start_modelscope_job("拍一张", session_id="qq:private:USER-1")
        second = service.start_modelscope_job("再拍一张", session_id="qq:private:USER-1")
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.error, "busy")


class ModelScopePhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)

    def _service(self, client: Any = None) -> PhotoService:
        return PhotoService(self.config, self.db, modelscope_client=client)

    def test_png_lands_in_data_photos_and_job_is_done(self) -> None:
        client = FakeModelScopeClient()
        service = self._service(client)

        result = service.generate("来张照片", session_id="qq:private:1")

        self.assertTrue(result.ok, result)
        self.assertTrue(Path(result.path).exists())
        self.assertEqual(Path(result.path).read_bytes(), PNG_BYTES)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["width"], 1024)
        row = self.db.execute(
            "SELECT status, image_path, prompt_source FROM photo_jobs WHERE id=?",
            (result.job_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["image_path"], result.path)

    def test_trigger_word_is_added_once(self) -> None:
        client = FakeModelScopeClient()
        service = self._service(client)
        prompt = PhotoPrompt(
            positive="1girl, solo, blue hair",
            negative="low quality",
            caption="自拍",
            source="manual",
        )

        result = service.generate("来张照片", prompt=prompt)

        self.assertTrue(result.ok, result)
        positive = client.calls[0]["positive"].lower()
        self.assertTrue(positive.startswith("whalegirl, "))
        self.assertEqual(positive.count("whalegirl"), 1)

        # Already-present trigger must not be duplicated.
        second = PhotoPrompt(
            positive="whalegirl, 1girl, solo",
            negative="",
            caption="",
            source="manual",
        )
        service.generate("再来一张", prompt=second)
        self.assertEqual(client.calls[1]["positive"].lower().count("whalegirl"), 1)

    def test_missing_key_returns_a_sentence_without_calling_out(self) -> None:
        service = self._service(None)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "modelscope_not_configured")
        self.assertIn("还没配好", friendly_photo_error(result.error))
        row = self.db.execute(
            "SELECT status, error FROM photo_jobs WHERE id=?", (result.job_id,)
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "modelscope_not_configured")

    def test_api_error_becomes_one_friendly_sentence(self) -> None:
        client = FakeModelScopeClient(
            raises=ModelScopePhotoError("modelscope_rate_limited", "429")
        )
        service = self._service(client)

        result = service.generate("来张照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "modelscope_rate_limited:429")
        self.assertIn("额度", friendly_photo_error(result.error))

    def test_backend_status_reports_the_lora_and_availability(self) -> None:
        client = FakeModelScopeClient()
        service = self._service(client)

        status = service.status()

        self.assertEqual(status["mode"], "modelscope")
        self.assertEqual(
            status["modelscope_model"], "sunny1155665/whalegirl-moss-v2"
        )
        self.assertTrue(status["modelscope_available"])
        self.assertEqual(status["comfyui_url"], "")
        self.assertEqual(status["assets"], {})

    def test_acceleration_lora_and_sampling_params_reach_the_api(self) -> None:
        # 2026-11-17: MOSS-Image-SFT defaults to 50 steps; the OPD turbo LoRA
        # makes 8 steps / CFG 1.0 usable.  Both must reach the request payload,
        # otherwise we silently render the slow 50-step path again.
        service = self._service(None)
        self.assertEqual(
            service.modelscope_accel_lora,
            "DiffSynth-Studio/MOSS-Image-SFT-TurboLoRA",
        )
        self.assertEqual(service.modelscope_steps, 8)
        self.assertAlmostEqual(service.modelscope_guidance, 1.0)
        params = service.modelscope_client.extra_params
        self.assertEqual(params["loras"], service.modelscope_accel_lora)
        self.assertEqual(params["steps"], 8)
        self.assertAlmostEqual(params["guidance"], 1.0)

        status = service.status()
        self.assertEqual(
            status["modelscope_accel_lora"], service.modelscope_accel_lora
        )
        self.assertEqual(status["modelscope_steps"], 8)
        self.assertAlmostEqual(status["modelscope_guidance"], 1.0)

    def test_disabling_accel_drops_sampling_overrides_too(self) -> None:
        # 8 steps without the turbo LoRA renders a blurry image.  Turning the
        # acceleration off must also restore the platform's 50-step defaults
        # instead of leaving a naked steps=8 behind.
        self.config._data["photo"]["modelscope_accel_lora"] = ""
        service = PhotoService(self.config, self.db)
        params = service.modelscope_client.extra_params
        self.assertNotIn("loras", params)
        self.assertNotIn("steps", params)
        self.assertNotIn("guidance", params)


class ModelScopeClientPayloadTests(unittest.TestCase):
    def test_payload_targets_the_lora_model_with_size_and_seed(self) -> None:
        client = ModelScopePhotoClient(
            "https://api-inference.modelscope.cn/v1",
            api_key="ms-test",
            model="sunny1155665/whalegirl-moss-v2",
            size="1024x1024",
        )
        payload = client.build_payload(
            "whalegirl, 1girl", "low quality", seed=7, width=768, height=512
        )
        self.assertEqual(payload["model"], "sunny1155665/whalegirl-moss-v2")
        self.assertEqual(payload["prompt"], "whalegirl, 1girl")
        self.assertEqual(payload["negative_prompt"], "low quality")
        self.assertEqual(payload["size"], "1024x1024")
        self.assertEqual(payload["seed"], 7)
        self.assertEqual(payload["n"], 1)
        self.assertTrue(client.available)

    def test_payload_carries_acceleration_lora_and_sampling(self) -> None:
        client = ModelScopePhotoClient(
            "https://api-inference.modelscope.cn/v1",
            api_key="ms-test",
            model="sunny1155665/whalegirl-moss-v2",
            extra_params={
                "loras": "DiffSynth-Studio/MOSS-Image-SFT-TurboLoRA",
                "steps": 8,
                "guidance": 1.0,
            },
        )
        payload = client.build_payload("whalegirl, 1girl")
        self.assertEqual(
            payload["loras"], "DiffSynth-Studio/MOSS-Image-SFT-TurboLoRA"
        )
        self.assertEqual(payload["steps"], 8)
        self.assertAlmostEqual(payload["guidance"], 1.0)

    def test_async_success_fields_are_normalized(self) -> None:
        self.assertEqual(
            _extract_images({"output_images": ["https://example.test/a.png"]}),
            ["https://example.test/a.png"],
        )
        self.assertEqual(
            _extract_images({"outputs": {"images": [{"url": "https://example.test/b.png"}]}}),
            ["https://example.test/b.png"],
        )
        self.assertEqual(_extract_images({"outputs": {}}), [])

    def test_image_magic_and_extension(self) -> None:
        self.assertTrue(_looks_like_image(PNG_BYTES))
        self.assertFalse(_looks_like_image(b"<html>not an image</html>"))


if __name__ == "__main__":
    unittest.main()
