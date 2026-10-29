from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from fatfish.config import Config
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.sticker.importer import (
    describe_with_deepseek,
    import_stickers,
    iter_images,
    sha256_file,
)
from fatfish.sticker.service import StickerService


def png_bytes(width: int = 1, height: int = 1, rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Build a tiny, valid PNG without Pillow."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, rgb: tuple[int, int, int] = (255, 0, 0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(rgb=rgb))
    return path


def make_env(root: Path) -> tuple[Config, Database]:
    data = root / "data"
    config = Config(
        {
            "app": {"data_dir": str(data)},
            "storage": {"db_path": str(data / "test.db")},
            "memory": {"embedding_backend": "hash", "embedding_dim": 64},
            "sticker": {
                "enabled": True,
                "library_dir": "stickers",
                "intent_threshold": 0.5,
                "max_candidates": 20,
            },
        },
        profile="test",
    )
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


VALID_JSON = json.dumps(
    {
        "caption": "开心猫猫",
        "description": "一只举爪的开心猫猫",
        "tags": ["开心", "猫", "举爪", "表情包", "可爱", "卡通", "a", "b", "c"],
        "emotion": {"primary": "开心", "intensity": 4},
        "intents": ["happy", "tease", "not_a_real_intent"],
        "intensity": 9,
        "style": "cartoon",
        "has_text": False,
        "is_base": False,
        "text_content": "",
    },
    ensure_ascii=False,
)


class FakeLLM:
    model = "fake-deepseek"
    timeout = 30.0

    def __init__(
        self,
        text: str = VALID_JSON,
        *,
        fail: bool = False,
        reject_images: bool = False,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
    ):
        self.text = text
        self.fail = fail
        self.reject_images = reject_images
        self.available = True
        self.calls: list[dict] = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.fail:
            return None
        if self.reject_images and isinstance(messages[-1].get("content"), list):
            return None
        return LLMResult(
            text=self.text,
            usage=LLMUsage(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                total_tokens=self.prompt_tokens + self.completion_tokens,
            ),
            latency_ms=12.5,
            model=self.model,
        )


class StickerImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)
        self.service = StickerService(self.db, self.config)
        self.incoming = self.root / "incoming"
        self.base = self.root / "base"
        self.cache = self.root / "cache" / "tag_cache.json"
        self.index = self.root / "cache" / "index.json"
        self.incoming.mkdir(parents=True, exist_ok=True)
        self.base.mkdir(parents=True, exist_ok=True)

    def sticker_count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) AS n FROM stickers")["n"])

    def run_import(self, llm, **overrides):
        kwargs = {
            "source_dir": self.incoming,
            "base_dir": self.base,
            "index_path": self.index,
            "cache_path": self.cache,
            "limit": 8,
            "apply": False,
            "use_vision": True,
            "min_interval": 0,
        }
        kwargs.update(overrides)
        return import_stickers(self.service, llm, **kwargs)

    def test_iter_images_dedupes_and_skips_invalid(self) -> None:
        first = write_png(self.incoming / "a.png", (255, 0, 0))
        write_png(self.incoming / "b.png", (255, 0, 0))  # identical content
        write_png(self.incoming / "c.png", (0, 0, 255))
        write_png(self.incoming / "pack" / "d.png", (0, 255, 0))
        (self.incoming / "empty.png").write_bytes(b"")
        (self.incoming / "broken.png").write_bytes(b"this is not a png")
        (self.incoming / "notes.txt").write_text("ignore me", encoding="utf-8")

        images = list(iter_images(self.incoming))

        self.assertEqual(3, len(images))
        self.assertEqual(
            {first.name, "c.png", "d.png"}, {image.path.name for image in images}
        )
        self.assertTrue(all(len(image.sha256) == 64 for image in images))
        self.assertTrue(all(image.width == 1 and image.height == 1 for image in images))
        self.assertNotIn("empty.png", {image.path.name for image in images})
        self.assertNotIn("broken.png", {image.path.name for image in images})

        self.assertEqual(1, len(list(iter_images(self.incoming, limit=1))))
        self.assertEqual([], list(iter_images(self.root / "missing")))

    def test_describe_vision_sends_image_and_caches(self) -> None:
        image = write_png(self.incoming / "happy.png")
        llm = FakeLLM()

        first = describe_with_deepseek(
            llm,
            image,
            "",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )

        self.assertTrue(first["vision_used"])
        self.assertFalse(first["needs_review"])
        self.assertFalse(first["from_cache"])
        self.assertEqual("开心猫猫", first["caption"])
        self.assertEqual(8, len(first["tags"]))
        self.assertEqual(["happy", "tease"], first["intents"])
        self.assertEqual(5, first["intensity"])
        self.assertEqual(1, first["api_calls"])
        self.assertEqual(100, first["prompt_tokens"])
        self.assertEqual(1, len(llm.calls))

        user_content = llm.calls[0]["messages"][1]["content"]
        self.assertIsInstance(user_content, list)
        text_part, image_part = user_content
        self.assertEqual("text", text_part["type"])
        self.assertEqual("image_url", image_part["type"])
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertGreater(len(image_part["image_url"]["url"]), 50)

        digest = sha256_file(image)
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertIn(digest, cache)
        self.assertEqual("开心猫猫", cache[digest]["caption"])

        second = describe_with_deepseek(
            llm,
            image,
            "",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )
        self.assertTrue(second["from_cache"])
        self.assertEqual(0, second["api_calls"])
        self.assertEqual(1, len(llm.calls))

    def test_vision_failure_falls_back_to_filename_and_ocr(self) -> None:
        image = write_png(self.incoming / "happy_cat.png")
        llm = FakeLLM(fail=True)

        result = describe_with_deepseek(
            llm,
            image,
            "hello",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )

        self.assertFalse(result["vision_used"])
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["has_text"])
        self.assertIn("happy", result["tags"])
        self.assertIn("cat", result["tags"])
        self.assertIn("happy", result["intents"])
        self.assertIn("hello", result["caption"])
        self.assertEqual(2, len(llm.calls))  # vision attempt, then text fallback
        self.assertFalse(self.cache.exists())  # local fallbacks are not cached

        no_ocr = describe_with_deepseek(
            llm,
            image,
            "",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )
        self.assertFalse(no_ocr["vision_used"])
        self.assertTrue(no_ocr["needs_review"])
        self.assertFalse(no_ocr["has_text"])
        self.assertEqual(4, len(llm.calls))

    def test_no_vision_skips_api(self) -> None:
        image = write_png(self.incoming / "smile.png")
        llm = FakeLLM()

        result = describe_with_deepseek(
            llm,
            image,
            "",
            cache_path=self.cache,
            use_vision=False,
            min_interval=0,
        )

        self.assertEqual([], llm.calls)
        self.assertFalse(result["vision_used"])
        self.assertTrue(result["needs_review"])

    def test_non_json_vision_response_falls_back(self) -> None:
        image = write_png(self.incoming / "smile.png")
        llm = FakeLLM("sorry, I cannot do that")

        result = describe_with_deepseek(
            llm,
            image,
            "",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )

        self.assertEqual(2, len(llm.calls))
        self.assertFalse(result["vision_used"])
        self.assertTrue(result["needs_review"])

    def test_vision_rejection_uses_text_metadata_fallback(self) -> None:
        image = write_png(self.incoming / "happy_cat.png")
        llm = FakeLLM(reject_images=True)

        result = describe_with_deepseek(
            llm,
            image,
            "hello",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )

        self.assertFalse(result["vision_used"])
        self.assertTrue(result["needs_review"])
        self.assertEqual(1, result["api_calls"])
        self.assertEqual(2, len(llm.calls))

        text_prompt = llm.calls[1]["messages"][1]["content"]
        self.assertIsInstance(text_prompt, str)
        self.assertIn("happy_cat.png", text_prompt)
        self.assertIn("1x1", text_prompt)
        self.assertIn("hello", text_prompt)
        self.assertNotIn("base64", text_prompt)
        self.assertIn("未使用视觉", result["description"])

        digest = sha256_file(image)
        cached = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertIn(digest, cached)
        self.assertFalse(cached[digest]["vision_used"])

        # A later vision-capable run ignores the metadata-only cache, upgrades
        # the tag, and replaces the cache entry.
        upgrade_llm = FakeLLM()
        upgraded = describe_with_deepseek(
            upgrade_llm,
            image,
            "hello",
            cache_path=self.cache,
            use_vision=True,
            min_interval=0,
        )
        self.assertTrue(upgraded["vision_used"])
        self.assertFalse(upgraded["from_cache"])
        self.assertEqual(1, len(upgrade_llm.calls))
        self.assertTrue(
            json.loads(self.cache.read_text(encoding="utf-8"))[digest]["vision_used"]
        )

    def test_import_dry_run_writes_nothing_then_apply_indexes(self) -> None:
        image = write_png(self.incoming / "smile.png")
        llm = FakeLLM()

        dry = self.run_import(llm, apply=False)

        self.assertEqual("dry-run", dry["mode"])
        self.assertEqual(1, dry["counts"]["scanned"])
        self.assertEqual(1, dry["counts"]["tagged"])
        self.assertEqual(0, dry["counts"]["indexed"])
        self.assertEqual(0, dry["counts"]["base"])
        self.assertEqual(0, self.sticker_count())
        self.assertFalse(self.index.exists())
        self.assertEqual([], list(self.base.iterdir()))
        self.assertEqual(1, dry["counts"]["api_calls"])
        self.assertEqual(100, dry["counts"]["prompt_tokens"])

        applied = self.run_import(llm, apply=True)

        self.assertEqual("apply", applied["mode"])
        self.assertEqual(1, applied["counts"]["indexed"])
        self.assertEqual(1, self.sticker_count())
        self.assertTrue(self.index.exists())

        sticker = self.service.get(sha256_file(image)[:16])
        self.assertIsNotNone(sticker)
        assert sticker is not None
        self.assertEqual("imported", sticker.source)
        self.assertEqual("开心猫猫", sticker.caption)
        self.assertEqual(["happy", "tease"], sticker.intents)
        self.assertEqual(5.0, sticker.intensity)
        self.assertEqual(sha256_file(image), sticker.content["sha256"])
        self.assertEqual(str(image), sticker.content["source_path"])
        self.assertTrue(sticker.content["vision_used"])
        self.assertFalse(sticker.content["is_base"])

        index = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(1, index["total"])
        entry = index["images"][0]
        self.assertEqual(sticker.id, entry["id"])
        self.assertEqual("开心猫猫", entry["caption"])
        self.assertTrue(entry["vision_used"])
        self.assertFalse(entry["is_base"])

        rerun = self.run_import(llm, apply=True)
        self.assertEqual(1, self.sticker_count())
        self.assertEqual(1, rerun["counts"]["already_indexed"])
        self.assertEqual(1, rerun["counts"]["cache_hits"])

    def test_base_classification_copies_and_marks(self) -> None:
        image = write_png(self.incoming / "cute.png")
        base_json = json.dumps(
            {
                "caption": "空白底图",
                "description": "无人物底图",
                "tags": ["底图"],
                "emotion": {"primary": "平静", "intensity": 1},
                "intents": [],
                "intensity": 1,
                "style": "template",
                "has_text": False,
                "is_base": True,
                "text_content": "",
            },
            ensure_ascii=False,
        )

        summary = self.run_import(FakeLLM(base_json), apply=True)
        self.assertEqual(1, summary["counts"]["base"])

        copied = self.base / "cute.png"
        self.assertTrue(copied.exists())
        sticker = self.service.get(sha256_file(image)[:16])
        self.assertIsNotNone(sticker)
        assert sticker is not None
        self.assertEqual(copied, Path(sticker.file_path))
        self.assertTrue(sticker.content["base"])
        self.assertTrue(sticker.content["is_base"])

        entry = json.loads(self.index.read_text(encoding="utf-8"))["images"][0]
        self.assertTrue(entry["is_base"])
        self.assertEqual(str(copied), entry["file"])

    def test_clean_template_without_llm_flag_is_copied(self) -> None:
        write_png(self.incoming / "blank_frame.png")
        template_json = json.dumps(
            {
                "caption": "空白框",
                "tags": ["空白"],
                "emotion": {"primary": "", "intensity": 1},
                "intents": [],
                "intensity": 1,
                "style": "line",
                "has_text": False,
                "is_base": False,
                "text_content": "",
            },
            ensure_ascii=False,
        )

        summary = self.run_import(FakeLLM(template_json), apply=True)

        self.assertEqual(1, summary["counts"]["base"])
        self.assertTrue((self.base / "blank_frame.png").exists())

    def test_promote_no_text_copies_plain_character_pose(self) -> None:
        write_png(self.incoming / "smile.png")

        summary = self.run_import(
            FakeLLM(),
            apply=True,
            promote_no_text=True,
        )

        self.assertEqual(1, summary["counts"]["base"])
        copied = self.base / "smile.png"
        self.assertTrue(copied.exists())
        sticker = self.service.get(sha256_file(self.incoming / "smile.png")[:16])
        self.assertIsNotNone(sticker)
        assert sticker is not None
        self.assertTrue(sticker.content["base"])
        self.assertTrue(sticker.content["is_base"])

    def test_invalid_images_are_skipped_by_import(self) -> None:
        (self.incoming / "broken.png").write_bytes(b"not an image")
        (self.incoming / "empty.jpg").write_bytes(b"")

        summary = self.run_import(FakeLLM(), apply=True)

        self.assertEqual(0, summary["counts"]["scanned"])
        self.assertEqual(0, self.sticker_count())
        self.assertFalse(self.index.exists())


if __name__ == "__main__":
    unittest.main()
