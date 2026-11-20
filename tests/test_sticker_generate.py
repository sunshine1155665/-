"""Offline tests for the ModelScope LoRA sticker generation service.

The runtime path (send_sticker -> job -> tick -> outbox) is covered in
``test_sticker_delivery.py``; this module pins the service itself: prompt
assembly, the turbo+LoRA payload, the cost gates, and the poll/download state
machine.  No network: a fake client stands in for the ModelScope client.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.photo.modelscope import ModelScopePhotoClient, ModelScopePhotoError
from fatfish.sticker.service import (
    ANCHOR,
    StickerService,
    _image_extension,
    _side,
)


class FakeClient:
    available = True

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.task_id = "task-1"
        self.poll_status = "succeeded"
        self.fail_download = False

    def build_payload(self, positive, negative="", *, seed=None, width=1024, height=1024):
        return {"prompt": positive, "negative_prompt": negative,
                "seed": seed, "size": f"{width}x{height}"}

    def submit(self, payload):
        self.prompts.append(str(payload.get("prompt") or ""))
        return self.task_id

    def poll(self, task_id):
        # The real client raises on FAILED/CANCELED; the fake mirrors that.
        if self.poll_status == "failed":
            raise ModelScopePhotoError("modelscope_failed", "task failed")
        return {"status": self.poll_status, "url": "https://example.test/s.png"}

    def download(self, url):
        if self.fail_download:
            raise RuntimeError("boom")
        return b"\x89PNG\r\n\x1a\n" + b"0" * 64


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

[sticker]
generation_enabled = true
generation_model = "fake/model"
generation_loras = ["fake/lora"]
generation_trigger = "whalegirl_sticker"
generation_negative = "写实，多人"
generation_steps = 8
generation_guidance = 1.0
generation_size = "512x512"
generation_max_per_10m = 0
generation_max_pending = 1
"""
    path = tmp / "sticker.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


class PromptTests(unittest.TestCase):
    def test_trigger_anchor_and_need(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, db = make_env(Path(tmp))
            try:
                service = StickerService(db, config, client=FakeClient())
                prompt = service.build_prompt("挥手打招呼")
                self.assertTrue(prompt.startswith("whalegirl_sticker, "))
                self.assertIn(ANCHOR, prompt)
                self.assertTrue(prompt.endswith("，挥手打招呼"))
                self.assertEqual(prompt.count("whalegirl_sticker"), 1)
            finally:
                db.close()

    def test_need_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, db = make_env(Path(tmp))
            try:
                service = StickerService(db, config, client=FakeClient())
                prompt = service.build_prompt("啊" * 500)
                self.assertLess(len(prompt), 1500)
            finally:
                db.close()

    def test_payload_carries_turbo_loras_and_sampler(self) -> None:
        # Real client, payload assembly only -- no HTTP.
        client = ModelScopePhotoClient(
            api_key="x",
            model="Qwen/Qwen-Image-2.1-Turbo",
            size="512x512",
            extra_params={"loras": ["sunny1155665/whalegirl-sticker-v1"],
                          "steps": 8, "guidance": 1.0},
        )
        payload = client.build_payload("prompt", "neg")
        self.assertEqual(payload["model"], "Qwen/Qwen-Image-2.1-Turbo")
        self.assertEqual(payload["loras"], ["sunny1155665/whalegirl-sticker-v1"])
        self.assertEqual(payload["steps"], 8)
        self.assertEqual(payload["guidance"], 1.0)
        self.assertEqual(payload["size"], "512x512")


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.client = FakeClient()
        self.service = StickerService(self.db, self.config, client=self.client)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_submit_creates_a_waiting_job(self) -> None:
        decision = self.service.decide("挥手打招呼", session_id="qq:private:1")
        self.assertTrue(decision.pending)
        self.assertEqual(decision.reason, "generation_queued")
        self.assertTrue(decision.job_id)
        jobs = self.db.query("SELECT * FROM sticker_jobs")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "waiting")
        self.assertEqual(jobs[0]["session_id"], "qq:private:1")
        self.assertEqual(jobs[0]["task_id"], "task-1")
        self.assertIn("whalegirl_sticker", jobs[0]["prompt"])

    def test_no_client_means_unavailable(self) -> None:
        service = StickerService(self.db, self.config, client=None)
        service.client = None
        decision = service.decide("挥手")
        self.assertFalse(decision.pending)
        self.assertEqual(decision.reason, "generation_unavailable")

    def test_second_pending_job_is_busy(self) -> None:
        self.assertTrue(self.service.decide("一").pending)
        decision = self.service.decide("二")
        self.assertFalse(decision.pending)
        self.assertEqual(decision.reason, "busy")

    def test_cost_gate(self) -> None:
        self.service.max_pending = 10
        self.service.max_per_10m = 1
        self.assertTrue(self.service.decide("一", now=1000.0).pending)
        # The first job is still pending; finish it so the busy gate is not what
        # we are measuring.
        self.db.execute("UPDATE sticker_jobs SET status='done'")
        decision = self.service.decide("二", now=1000.0)
        self.assertFalse(decision.pending)
        self.assertEqual(decision.reason, "rate_limited")


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.client = FakeClient()
        self.service = StickerService(self.db, self.config, client=self.client)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _submit(self) -> str:
        decision = self.service.decide("挥手", now=1000.0)
        assert decision.job_id
        return decision.job_id

    def test_success_downloads_the_image(self) -> None:
        job_id = self._submit()
        results = self.service.poll_jobs(now=1500.0)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        target = self.config.data_dir / "stickers" / "generated" / f"{job_id}.png"
        self.assertTrue(target.is_file())
        row = self.db.query_one("SELECT * FROM sticker_jobs WHERE id=?", (job_id,))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["image_path"], str(target))

    def test_failed_task_marks_the_job_failed(self) -> None:
        job_id = self._submit()
        self.client.poll_status = "failed"
        results = self.service.poll_jobs(now=1500.0)
        self.assertFalse(results[0]["ok"])
        row = self.db.query_one("SELECT * FROM sticker_jobs WHERE id=?", (job_id,))
        self.assertEqual(row["status"], "failed")

    def test_download_error_marks_the_job_failed(self) -> None:
        job_id = self._submit()
        self.client.fail_download = True
        results = self.service.poll_jobs(now=1500.0)
        self.assertFalse(results[0]["ok"])
        row = self.db.query_one("SELECT * FROM sticker_jobs WHERE id=?", (job_id,))
        self.assertEqual(row["status"], "failed")

    def test_stale_job_times_out(self) -> None:
        job_id = self._submit()
        results = self.service.poll_jobs(now=1000.0 + self.service.timeout + 60)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "modelscope_timeout")
        row = self.db.query_one("SELECT * FROM sticker_jobs WHERE id=?", (job_id,))
        self.assertEqual(row["status"], "failed")

    def test_recent_jobs_and_get_job(self) -> None:
        job_id = self._submit()
        self.assertIsNotNone(self.service.get_job(job_id))
        self.assertEqual(self.service.recent_jobs()[0]["id"], job_id)


class HelpersTests(unittest.TestCase):
    def test_side_and_extension(self) -> None:
        self.assertEqual(_side("1024x768"), 1024)
        self.assertEqual(_side("bogus"), 1024)
        self.assertEqual(_image_extension(b"\x89PNG\r\n\x1a\n"), ".png")
        self.assertEqual(_image_extension(b"\xff\xd8\xff"), ".jpg")
        self.assertEqual(_image_extension(b"RIFF0000WEBP"), ".webp")


if __name__ == "__main__":
    unittest.main()
