"""Tests for the sticker ``intensity`` knob the model is promised.

``TOOL_SCHEMA`` tells the model it may pass ``intensity`` (1-5). It used to be
dropped on the floor by ``execute_actions``, which is worse than not offering
it: the model believes it asked for "keep it mild" and the runtime silently
did nothing.

There is deliberately **no** ``mode`` knob here. ``standalone`` ("just the
picture, no words") was implemented and then removed: a sticker is the tone and
the words are the content, so letting the model delete the words risks a turn
where the user receives nothing at all. Text is never suppressed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import Config, load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.runtime import Runtime, _sticker_intensity
from fatfish.sticker.emotion import breakdown, emotion_intensity
from fatfish.sticker.service import StickerDecision, StickerService

OWNER_SESSION = "qq:private:10001"


def make_env(
    tmp: Path,
    *,
    generate: bool = False,
    spontaneous: bool = False,
    min_emotion: float = 0.0,
):
    """Pin every knob these tests are not about.

    ``min_emotion_intensity`` defaults to 0 here even though production ships
    2.0: the gate is orthogonal to the thresholds the other cases exercise, and
    letting a config default leak in means a future tuning pass turns unrelated
    tests red for a reason that has nothing to do with what they assert.
    ``EmotionGateTests`` turns it on explicitly.
    """
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
base_generation_enabled = {str(generate).lower()}
base_generation_spontaneous = {str(spontaneous).lower()}
min_emotion_intensity = {min_emotion}
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


class _NoLLM:
    """Caption writer that cannot reach the network -- these tests are offline."""

    available = False


def base_png(path: Path) -> str:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow 为可选依赖
        raise unittest.SkipTest("Pillow not installed (optional dependency)")

    Image.new("RGB", (64, 64), (30, 60, 90)).save(path)
    return str(path)


def fake_reply(runtime: Runtime, text: str) -> None:
    runtime.llm.api_key = "fake"
    runtime.llm.chat = lambda messages, **kwargs: LLMResult(
        text=text,
        usage=LLMUsage(prompt_tokens=10, completion_tokens=3),
        model="fake-model",
    )


def plan(intensity=None, messages=("怎么啦…",)) -> str:
    args: dict = {"need": "有点想哭"}
    if intensity is not None:
        args["intensity"] = intensity
    return json.dumps(
        {"messages": list(messages), "actions": [{"tool": "send_sticker", "args": args}]},
        ensure_ascii=False,
    )


class IntensityParsingTests(unittest.TestCase):
    def test_clamped_to_1_5(self) -> None:
        self.assertEqual(_sticker_intensity(1), 1.0)
        self.assertEqual(_sticker_intensity(5), 5.0)
        self.assertEqual(_sticker_intensity(0), 1.0)
        self.assertEqual(_sticker_intensity(99), 5.0)
        self.assertEqual(_sticker_intensity("3"), 3.0)
        self.assertEqual(_sticker_intensity(3.7), 3.7)

    def test_no_opinion_returns_none(self) -> None:
        for value in (None, "", "x", True, False, float("nan"), float("inf")):
            self.assertIsNone(_sticker_intensity(value), value)


class TextIsNeverSuppressedTests(unittest.TestCase):
    """表情包是语气，话才是内容 —— 任何情况下都不能只剩一张图。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.seeded = StickerService(self.db, self.config).add_sticker(
            str(self.tmp / "cry.png"),
            caption="垃圾桶里委屈哭",
            tags=["想哭", "委屈"],
            intents=["sad", "comfort"],
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _stub(self, *, send: bool):
        def decide(need, **kwargs):
            return StickerDecision(
                send=send,
                reason="library_match" if send else "no_matching_sticker",
                score=0.9 if send else 0.0,
                sticker=self.seeded if send else None,
                candidates=[],
            )

        self.runtime.sticker.decide = decide

    def test_text_survives_a_sent_sticker(self) -> None:
        self._stub(send=True)
        fake_reply(self.runtime, plan(intensity=4))
        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertTrue(result["sticker"]["sent"])
        self.assertEqual(result["messages"], ["怎么啦…"])
        self.assertEqual(result["reply"], "怎么啦…")

    def test_text_survives_a_failed_sticker(self) -> None:
        self._stub(send=False)
        fake_reply(self.runtime, plan())
        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertFalse(result["sticker"]["sent"])
        self.assertEqual(result["messages"], ["怎么啦…"])

    def test_legacy_mode_argument_is_ignored_not_obeyed(self) -> None:
        # 老提示词残留 mode=standalone 时，绝不能真的把文字删掉。
        self._stub(send=True)
        payload = json.loads(plan())
        payload["actions"][0]["args"]["mode"] = "standalone"
        fake_reply(self.runtime, json.dumps(payload, ensure_ascii=False))

        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")
        self.assertEqual(result["messages"], ["怎么啦…"])
        self.assertEqual(result["reply"], "怎么啦…")


class IntensityForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.runtime = Runtime(self.db, self.config)
        self.runtime.owner.bind(OWNER_SESSION, source="cli")
        self.seeded = StickerService(self.db, self.config).add_sticker(
            str(self.tmp / "cry.png"), caption="委屈", intents=["sad"]
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_intensity_reaches_decide(self) -> None:
        captured: dict = {}

        def decide(need, **kwargs):
            captured.update(kwargs)
            captured["need"] = need
            return StickerDecision(
                send=True, reason="library_match", score=1.0,
                sticker=self.seeded, candidates=[],
            )

        self.runtime.sticker.decide = decide
        fake_reply(self.runtime, plan(intensity=5))
        result = self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertEqual(captured["need"], "有点想哭")
        self.assertEqual(captured["intensity"], 5.0)
        self.assertEqual(result["sticker"]["intensity"], 5.0)

    def test_missing_intensity_means_no_opinion(self) -> None:
        captured: dict = {}

        def decide(need, **kwargs):
            captured.update(kwargs)
            return StickerDecision(send=False, reason="no_matching_sticker")

        self.runtime.sticker.decide = decide
        fake_reply(self.runtime, plan())
        self.runtime.handle_user_message(OWNER_SESSION, "有点想哭")

        self.assertIsNone(captured["intensity"], "没说强度就别替她编一个")


class IntensityAffectsRankingTests(unittest.TestCase):
    """intensity 不是被记下来就算，它得真的改变选哪张图。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.service = StickerService(self.db, self.config)
        # 两张几乎一样的图，只有力度不同：温和版 1、爆发版 5。
        self.mild = self.service.add_sticker(
            str(self.tmp / "mild.png"),
            caption="有点小委屈 想哭",
            tags=["委屈"],
            intents=["sad", "comfort"],
            intensity=1.0,
        )
        self.strong = self.service.add_sticker(
            str(self.tmp / "strong.png"),
            caption="委屈 想哭 哭到不行",
            tags=["委屈"],
            intents=["sad", "comfort"],
            intensity=5.0,
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _top_id(self, intensity: float | None) -> str:
        decision = self.service.decide("委屈 想哭", force=True, intensity=intensity)
        self.assertTrue(decision.send, decision.reason)
        return decision.sticker.id  # type: ignore[union-attr]

    def test_strong_request_picks_the_strong_sticker(self) -> None:
        self.assertEqual(self._top_id(5.0), self.strong.id)

    def test_mild_request_picks_the_mild_sticker(self) -> None:
        self.assertEqual(self._top_id(1.0), self.mild.id)

    def test_without_intensity_it_falls_back_to_its_own_guess(self) -> None:
        self.assertIn(self._top_id(None), {self.mild.id, self.strong.id})


class SpontaneousSynthesisTests(unittest.TestCase):
    """库里没有合适图时，自发路径必须沉默。

    ``base_generator`` 造出来的图固定记 1.00 分，所以它一旦挂在自发路径上，
    ``intent_threshold`` 就是个装饰：每句没匹配上的话都会被"造一张"接住。
    副本库实测（限流解除）：该发组 20/20、不该发组也 20/20。
    现画一张只该服务于「用户明确要图但库里没有」这个场景。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp, generate=True)
        self.service = StickerService(self.db, self.config)
        self.service.base_generator.llm = _NoLLM()
        # 唯一一张图是底图，和"翻译英文"毫无关系 —— 它拿去现画可以，
        # 拿去当匹配结果不行。
        self.service.add_sticker(
            base_png(self.tmp / "base.png"),
            caption="本鲸鱼娘来啦",
            tags=["鲸鱼娘"],
            intents=["meme"],
            content={"is_base": True},
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _count(self) -> int:
        return self.db.query_one("SELECT COUNT(*) AS n FROM stickers WHERE active=1")["n"]

    def test_plain_task_does_not_summon_a_sticker(self) -> None:
        before = self._count()
        decision = self.service.decide("帮我把这段话翻译成英文")

        self.assertFalse(decision.send, decision.reason)
        self.assertNotEqual(decision.reason, "generated_from_base")
        self.assertEqual(self._count(), before, "没发图就不该往库里写新图")

    def test_emotional_need_without_a_match_stays_silent(self) -> None:
        decision = self.service.decide("有点想哭")
        self.assertFalse(decision.send, decision.reason)

    def test_knob_can_bring_the_old_behaviour_back(self) -> None:
        # 这个开关不是死代码：打开它，旧行为（现画一张）真的会回来。
        data = self.config.as_dict()
        data["sticker"]["base_generation_spontaneous"] = True
        data["sticker"]["embedding_backend"] = "hash"
        enabled = Config(data, profile=self.config.profile, source=self.config.source)
        service = StickerService(self.db, enabled)
        service.base_generator.llm = _NoLLM()

        decision = service.decide("帮我把这段话翻译成英文")
        self.assertTrue(decision.send, decision.reason)
        self.assertEqual(decision.reason, "generated_from_base")


class EmotionScaleTests(unittest.TestCase):
    """锁住刻度的几个刀刃位置。

    这些句子不是随手挑的：每一条都对应一次真的算错了。第一版给所有情绪词
    同一个 0.6、所有事务词同一个 -0.8，于是：

        「我好难过，怎么办啊」 1.40 —— 被「怎么」判成公事，比「有点饿」还低
        「写代码写到崩溃」     1.40 —— 被「代码」判成公事，可它明明是崩溃

    修法是把词表分档（情绪分强弱、事务分求助/请求/话题），这几条就是那个
    分档的回归线。刻度本身没有「对错」，但这两类句子掉到阈值以下是明确的错。
    """

    def test_a_feeling_wrapped_in_a_question_still_counts(self) -> None:
        # 「怎么办」是难受时的求助措辞，不是任务指令。
        self.assertGreaterEqual(emotion_intensity("我好难过，怎么办啊"), 2.0)

    def test_a_topic_word_does_not_flatten_a_feeling(self) -> None:
        self.assertGreaterEqual(emotion_intensity("写代码写到崩溃"), 2.0)

    def test_a_request_that_also_carries_feeling_survives(self) -> None:
        for text in ("想哭，帮我看看这个错在哪", "累死了，今天还要写代码"):
            self.assertGreaterEqual(emotion_intensity(text), 2.0, text)

    def test_plain_work_requests_stay_low(self) -> None:
        for text in ("帮我把这段话翻译成英文", "把这份材料整理成表格", "这个公式怎么推导"):
            self.assertLess(emotion_intensity(text), 1.5, text)

    def test_scene_words_have_a_floor(self) -> None:
        # 道晚安不需要"很激动"才配图，场合本身就是理由。
        for text in ("晚安", "早安", "谢谢你"):
            self.assertGreaterEqual(emotion_intensity(text), 3.0, text)

    def test_bare_acknowledgement_is_almost_flat(self) -> None:
        for text in ("嗯", "好的", "哦"):
            self.assertLess(emotion_intensity(text), 1.5, text)

    def test_breakdown_explains_the_score(self) -> None:
        parts = breakdown("累死了……")
        self.assertIn("mood", parts)
        self.assertIn("intensifier", parts)
        self.assertAlmostEqual(parts["score"], emotion_intensity("累死了……"))

    def test_empty_text_is_zero_not_a_quote(self) -> None:
        self.assertEqual(emotion_intensity(""), 0.0)
        self.assertEqual(emotion_intensity("   "), 0.0)


class EmotionGateTests(unittest.TestCase):
    """第二道闸门：相似度管「图对不对」，情绪管「这一刻值不值得配图」。

    它存在的理由是召回和打扰原本被同一个数字绑住：想多发几张就得降
    ``intent_threshold``，而「我下午要去图书馆」和某张图的余弦并不低，于是
    一起涌进来。分开之后可以只降召回闸门。副本库实测（staging_spontaneous.py
    --sweep-emotion，40 句基线）：

        意图阈值   情绪闸门关      情绪闸门=2.0
        0.45      16/20 + 5/20   16/20 + 0/20
        0.50      12/20 + 4/20   12/20 + 0/20
        0.60       8/20 + 0/20    8/20 + 0/20

    也就是说：情绪闸门在所有阈值上都没砍掉一句该发的，却把误发清零。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp, min_emotion=2.0)
        self.service = StickerService(self.db, self.config)
        # 用哈希后端时，索引文本和查询文本一致就能拿到接近 1.0 的余弦，
        # 于是「相似度这一关」肯定过 —— 剩下的判断只可能来自情绪闸门。
        self.service.add_sticker(
            str(self.tmp / "task.png"),
            caption="帮我把这段话翻译成英文",
            tags=["翻译"],
            intents=["meme"],
        )
        self.service.add_sticker(
            str(self.tmp / "tired.png"),
            caption="累死了",
            tags=["累"],
            intents=["tired"],
        )
        # 中性词，本身不带情绪，但库里有同名图 —— 用来验证测温用的是原话
        # 而不是模型改写后的 need。
        self.service.add_sticker(
            str(self.tmp / "neutral.png"),
            caption="表情包",
            tags=["表情包"],
            intents=["meme"],
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_flat_sentence_is_gated(self) -> None:
        decision = self.service.decide("帮我把这段话翻译成英文")

        self.assertFalse(decision.send)
        self.assertEqual(decision.reason, "emotion_too_flat")

    def test_gated_decision_still_reports_what_it_would_have_matched(self) -> None:
        # 这是这道闸门最好用的诊断信息：分数很高却因为情绪被拦，说明闸门在
        # 真的干活；分数本来就低，那降 min_emotion_intensity 也白搭。
        decision = self.service.decide("帮我把这段话翻译成英文")

        self.assertGreaterEqual(decision.score, self.service.threshold)
        self.assertTrue(decision.candidates)

    def test_charged_sentence_still_sends(self) -> None:
        decision = self.service.decide("累死了……")

        self.assertTrue(decision.send, decision.reason)
        self.assertGreaterEqual(decision.emotion, 2.0)

    def test_zero_disables_the_gate_entirely(self) -> None:
        # 0 必须等于「没有这个功能」，而不是「阈值 0 也算一道闸门」。
        data = self.config.as_dict()
        data["sticker"]["min_emotion_intensity"] = 0.0
        off = Config(data, profile=self.config.profile, source=self.config.source)
        service = StickerService(self.db, off)

        decision = service.decide("帮我把这段话翻译成英文")
        self.assertTrue(decision.send, decision.reason)
        self.assertEqual(decision.reason, "library_match")

    def test_explicit_request_ignores_the_gate(self) -> None:
        # 用户明确要图（force）时，「这句话不够激动」不是拒绝的理由。
        decision = self.service.decide("帮我把这段话翻译成英文", force=True)

        self.assertTrue(decision.send, decision.reason)

    def test_emotion_is_measured_on_what_the_user_said(self) -> None:
        # need 是模型改写过的（"带一点撒娇"），照着它量体温会失真，所以有
        # user_text 时优先用它。这里 need 是个平词、user_text 才是原话。
        self.assertEqual(self.service.decide("表情包").reason, "emotion_too_flat")

        decision = self.service.decide("表情包", user_text="累死了……")

        self.assertTrue(decision.send, decision.reason)
        self.assertGreaterEqual(decision.emotion, 2.0)

    def test_silent_decisions_still_carry_the_temperature(self) -> None:
        # "没匹配上" 和 "匹配上了但这一刻太平" 是两种不同的失败，修法也不同，
        # 所以 send=False 时也要把读数带出来。
        decision = self.service.decide("这个积分怎么算")
        self.assertFalse(decision.send)
        self.assertGreater(decision.emotion, 0.0)

        flat = self.service.decide("这个积分怎么算", user_text="帮我把这份材料整理成表格")
        self.assertEqual(flat.emotion, 0.0)


if __name__ == "__main__":
    unittest.main()
