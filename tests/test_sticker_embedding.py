"""Tests for the sticker retrieval vector path.

Three things here have bitten us or would bite silently:

* switching embedder backends leaves old vectors in the table. ``cosine``
  truncates to the shorter vector instead of complaining, so a 768-dim query
  against 256-dim rows used to produce confident nonsense rather than a miss.
* bge-base cosines are compressed into roughly 0.34-0.60. With the 0.45 weight
  that was calibrated for hashing, the semantic signal contributes ~0.05 and
  does almost nothing. Stretching needs fixed anchors; per-request min-max
  pins the best candidate at 1.0 and makes the send threshold meaningless.
* ``HttpEmbedder`` must never raise. A dead embedding service has to degrade a
  turn, not break it.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.memory.embedder import (
    HashEmbedder,
    HttpEmbedder,
    build_embedder,
    cosine,
    dumps,
    loads,
)
from fatfish.sticker.service import StickerService, sticker_index_text


def make_config(tmp: Path, extra_sticker: str = ""):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"

[storage]
db_path = "{(data / 'sticker.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[voice]
enabled = false

[tts]
enabled = false

[sticker]
enabled = true
intent_threshold = 0.5
base_generation_enabled = false
comfyui_enabled = false
{extra_sticker}
"""
    path = tmp / "sticker.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


class FixedEmbedder:
    """Returns a fixed vector, so widths and cosines are exact in tests."""

    def __init__(self, vector: list[float]):
        self.vector = list(vector)
        self.dim = len(self.vector)
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return list(self.vector)


class MapEmbedder:
    """Different vector per text; unknown text gets ``default``."""

    def __init__(self, table: dict[str, list[float]], default: list[float]):
        self.table = {k: list(v) for k, v in table.items()}
        self.default = list(default)
        self.dim = len(self.default)

    def embed(self, text: str) -> list[float]:
        return list(self.table.get(text, self.default))


class IndexTextTests(unittest.TestCase):
    """`usage_description` 单独入索引，画面词不入。"""

    def test_scene_text_replaces_picture_words(self) -> None:
        text = sticker_index_text(
            "闭眼合掌，鲸鱼尾巴",
            ["女仆", "蓝发", "动漫"],
            ["comfort"],
            {"usage_description": "想要被摸头、求安慰"},
        )
        # 保留画面词会把「求安慰」这唯一的有效信息淹掉。
        self.assertEqual(text, "想要被摸头、求安慰")

    def test_sticker_without_scene_text_still_indexes_something(self) -> None:
        text = sticker_index_text("鲸鱼娘摊手", ["无奈", "tease"], ["meme"], {})
        self.assertIn("鲸鱼娘摊手", text)
        self.assertIn("tease", text)

    def test_blank_scene_text_falls_back(self) -> None:
        text = sticker_index_text("晚安好梦", [], ["goodnight"], {"usage_description": "   "})
        # 空白的场景描述不能把 caption 顶掉，也不能留下双空格
        self.assertEqual(text, "晚安好梦 goodnight")

    def test_missing_content_does_not_raise(self) -> None:
        self.assertEqual(sticker_index_text("图", None, None, None), "图")


class WidthGuardTests(unittest.TestCase):
    """换后端后旧向量必须判 0，不能靠 cosine 截断蒙出一个数。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_config(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_short_vector_is_scored_zero_not_truncated(self) -> None:
        wide = [0.0] * 768
        wide[0] = 1.0
        writer = StickerService(self.db, self.config, embedder=FixedEmbedder(wide))
        sticker = writer.add_sticker(
            str(self.tmp / "a.png"), caption="晚安 好梦", intents=["goodnight"]
        )
        self.assertEqual(len(sticker.embedding or []), 768)

        # 256 维查询打 768 维库存：宽度不符，相似度必须是 0。
        narrow = [0.0] * 256
        narrow[0] = 1.0
        reader = StickerService(self.db, self.config, embedder=FixedEmbedder(narrow))
        found = reader.search("晚安 好梦", limit=10)
        self.assertTrue(found, "关键词/意图应该还能把它带出来")
        self.assertEqual(found[0].similarity, 0.0)

    def test_cosine_really_does_truncate(self) -> None:
        # 这条不是在纵容截断，而是钉住「为什么必须自己加宽度守卫」。
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0, 9.9]), 1.0)

    def test_startup_check_reports_a_mismatch(self) -> None:
        wide = [0.0] * 768
        wide[0] = 1.0
        writer = StickerService(self.db, self.config, embedder=FixedEmbedder(wide))
        writer.add_sticker(str(self.tmp / "a.png"), caption="晚安", intents=[])

        narrow = [0.0] * 256
        narrow[0] = 1.0
        reader = StickerService(self.db, self.config, embedder=FixedEmbedder(narrow))
        self.assertIsNotNone(reader.width_warning)
        self.assertIn("768", reader.width_warning or "")
        self.assertIn("256", reader.width_warning or "")

    def test_startup_check_stays_quiet_before_the_width_is_known(self) -> None:
        # HttpEmbedder 的宽度是第一次请求后才学到的，此时 dim=0。
        # 把它当成「后端是 0 维」来比对会每次启动都误报（曾经真的误报过）。
        wide = [0.0] * 768
        wide[0] = 1.0
        writer = StickerService(self.db, self.config, embedder=FixedEmbedder(wide))
        writer.add_sticker(str(self.tmp / "a.png"), caption="晚安", intents=[])
        reader = StickerService(self.db, self.config, embedder=FixedEmbedder([]))
        self.assertEqual(len(reader.embedder.vector), 0)
        self.assertIsNone(reader.width_warning)


class CalibrationTests(unittest.TestCase):
    """固定锚点拉伸 vs 默认不拉伸。

    两个候选的原始余弦刻意做成 0.45 / 0.55 —— 这正是 bge 在本库上的实际区间，
    在 0.45 权重下只差 0.045 分，几乎分不出高下。
    """

    QUERY = "晚安"
    CAPTION_A = "xyz"   # 与查询零字面重合，把 keyword 那一项压成 0
    CAPTION_B = "qrs"
    # cos([1,0], v) 就是 v[0]
    VEC_A = [0.45, 0.8930286]
    VEC_B = [0.55, 0.8351647]

    def _run(self, floor: float, ceil: float):
        tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_obj.cleanup)
        tmp = Path(tmp_obj.name)
        extra = f"similarity_floor = {floor}\nsimilarity_ceil = {ceil}"
        config, db = make_config(tmp, extra)
        self.addCleanup(db.close)

        writer = StickerService(
            db, config,
            embedder=MapEmbedder(
                {self.CAPTION_A: self.VEC_A, self.CAPTION_B: self.VEC_B},
                [1.0, 0.0],
            ),
        )
        writer.add_sticker(
            str(tmp / "a.png"), caption=self.CAPTION_A, intents=[], sticker_id="a" * 32
        )
        writer.add_sticker(
            str(tmp / "b.png"), caption=self.CAPTION_B, intents=[], sticker_id="b" * 32
        )
        reader = StickerService(db, config, embedder=FixedEmbedder([1.0, 0.0]))
        found = reader.search(self.QUERY, limit=10)
        return {c.sticker.id[0]: c for c in found}

    def test_raw_similarity_is_reported_even_when_calibrated(self) -> None:
        # 不管排分用哪个数，接口里露出来的 similarity 必须是真实余弦。
        by_id = self._run(0.42, 0.60)
        self.assertAlmostEqual(by_id["a"].similarity, 0.45, places=5)
        self.assertAlmostEqual(by_id["b"].similarity, 0.55, places=5)

    def test_defaults_leave_scores_alone(self) -> None:
        by_id = self._run(0.0, 1.0)
        # 无关键词、无意向时，分数就是 0.45 * 余弦
        self.assertAlmostEqual(by_id["b"].score, 0.45 * 0.55, places=5)
        self.assertAlmostEqual(by_id["a"].score, 0.45 * 0.45, places=5)

    def test_anchors_widen_the_gap_between_candidates(self) -> None:
        loose = self._run(0.0, 1.0)
        tight = self._run(0.42, 0.60)
        loose_gap = loose["b"].score - loose["a"].score
        tight_gap = tight["b"].score - tight["a"].score
        self.assertGreater(tight_gap, loose_gap * 4)
        # 0.45 -> 0.167，0.55 -> 0.722，所以分数直接翻了几倍而不是只挪一点
        self.assertAlmostEqual(tight["a"].score, 0.45 * (0.03 / 0.18), places=5)
        self.assertAlmostEqual(tight["b"].score, 0.45 * (0.13 / 0.18), places=5)

    def test_anchors_clip_outside_the_window(self) -> None:
        from fatfish.sticker.service import _clip01

        self.assertEqual(_clip01((0.10 - 0.42) / 0.18), 0.0)
        self.assertEqual(_clip01((0.95 - 0.42) / 0.18), 1.0)


class HttpEmbedderTests(unittest.TestCase):
    """服务挂了也得把这一轮答完。"""

    def test_dead_service_falls_back_to_hashing(self) -> None:
        embedder = HttpEmbedder("http://127.0.0.1:9/embed", timeout=0.2)
        vector = embedder.embed("今天天气不错")
        self.assertTrue(vector)
        self.assertEqual(len(vector), 256)  # 哈希兜底的宽度
        self.assertEqual(embedder.failures, 1)
        self.assertTrue(embedder.degraded)
        self.assertIsNotNone(embedder.last_error)

    def test_success_resets_the_failure_counter(self) -> None:
        embedder = HttpEmbedder("http://127.0.0.1:9/embed", timeout=0.2)
        embedder.embed("x")
        self.assertEqual(embedder.failures, 1)
        body = json.dumps({"vectors": [[0.1, 0.2, 0.3]]}).encode("utf-8")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Response(body)):
            vector = embedder.embed("x")
        self.assertEqual(vector, [0.1, 0.2, 0.3])
        self.assertEqual(embedder.dim, 3, "维度应该从响应里学到")
        self.assertEqual(embedder.failures, 0)

    def test_count_mismatch_falls_back_to_hashing(self) -> None:
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        embedder = HttpEmbedder("http://127.0.0.1:9/embed", timeout=0.2)
        body = json.dumps({"vectors": []}).encode("utf-8")
        with mock.patch("urllib.request.urlopen", return_value=Response(body)):
            vector = embedder.embed("a")
        self.assertEqual(embedder.failures, 1, "数量不符必须算失败")
        self.assertEqual(len(vector), 256, "并且退回哈希")

    def test_health_never_raises(self) -> None:
        embedder = HttpEmbedder("http://127.0.0.1:9/embed", timeout=0.2)
        health = embedder.health()
        self.assertFalse(health["ok"])
        self.assertIn("error", health)


class BuildEmbedderTests(unittest.TestCase):
    def test_http_needs_a_url(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            build_embedder("http", 256)
        self.assertIn("url", str(ctx.exception))

    def test_unknown_backend_is_rejected_loudly(self) -> None:
        with self.assertRaises(ValueError):
            build_embedder("magic", 256)

    def test_hash_is_the_default(self) -> None:
        self.assertIsInstance(build_embedder("", 128), HashEmbedder)
        self.assertEqual(build_embedder("hash", 128).dim, 128)


class QueryExpansionTests(unittest.TestCase):
    """hash 后端 + 查询侧同义词扩展：换一种说法也要找得到。

    病例来自真库评测（scripts/sticker_recall_eval.py）：用户说「惊到」，
    库里写「惊讶、没想到」，hash 零重合，扩展前这张图进不了 top-3。
    """

    def test_synonym_bridge_finds_the_sticker(self) -> None:
        tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_obj.cleanup)
        tmp = Path(tmp_obj.name)
        config, db = make_config(tmp)
        self.addCleanup(db.close)

        service = StickerService(db, config)
        service.add_sticker(
            str(tmp / "surprise.png"),
            caption="瞪大眼睛",
            intents=["surprise"],
            # 用法文本要够厚：hash 是 256 桶签名，别名只桥出 1-2 个共享桶时
            # 可能被别的 token 反号抵消（真库实测踩过），两个别名都落在
            # 文本里才能稳定给出正余弦。
            content={"usage_description": "惊讶、没想到，直接被震惊到了"},
        )
        service.add_sticker(
            str(tmp / "filler.png"),
            caption="日常唠嗑",
            intents=["meme"],
            content={"usage_description": "陪聊、闲聊"},
        )
        found = service.search("真的惊到我了", limit=2)
        self.assertTrue(found, "扩展后必须还能出候选")
        self.assertEqual(found[0].sticker.caption, "瞪大眼睛")
        self.assertGreater(found[0].similarity, 0.0, "别名必须桥出非零余弦")

    def test_intent_keywords_score_the_original_text(self) -> None:
        # 意图评分吃原文不吃展开：加进查询的「惊讶」不能替规则分类器做决定。
        tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_obj.cleanup)
        tmp = Path(tmp_obj.name)
        config, db = make_config(tmp)
        self.addCleanup(db.close)
        service = StickerService(db, config)
        # 「委屈」是 sad 的规则关键词，「惊讶」不是；展开若混进意图评分，
        # 一次都没提 sad 的查询就会被硬拗成 sad。
        intents = dict(service.classify_intents("我真的惊到我了"))
        self.assertNotIn("sad", intents)


class StorageRoundTripTests(unittest.TestCase):
    def test_dumps_rounds_and_loads_restores(self) -> None:
        vector = [0.1234567891, -0.0000001234]
        raw = dumps(vector)
        self.assertIsNotNone(raw)
        self.assertLess(len(raw or ""), 40)
        restored = loads(raw)
        self.assertIsNotNone(restored)
        self.assertAlmostEqual(restored[0], 0.123457, places=6)  # type: ignore[index]

    def test_garbage_does_not_raise(self) -> None:
        self.assertIsNone(loads("not json"))
        self.assertIsNone(loads("{}"))
        self.assertIsNone(loads(None))


if __name__ == "__main__":
    unittest.main()
