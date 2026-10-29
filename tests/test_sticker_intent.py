"""Tests for contextual sticker intent classification.

The benchmark that motivated this module (21 real phrasings, same cases for
each backend) measured keyword rules at 62% top-1, an independent classifier
over the recent dialogue at 100%, and letting the main model report its own
intent at 52% — the persona labels what *she* wants to express, not what the
user asked for. These tests pin the behaviour that made the difference:
context reaches the classifier, and only taxonomy labels ever come back out.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.db import Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.sticker import StickerService
from fatfish.sticker.intent import (
    INTENT_KEYWORDS,
    INTENT_LABELS,
    HybridClassifier,
    IntentGuess,
    KevClassifier,
    LlmClassifier,
    RuleClassifier,
    build_classifier,
    score_intents,
)


class FakeLLM:
    available = True

    def __init__(self, text: str | None, *, fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls: list[list[dict[str, str]]] = []
        self.metrics: list[tuple[str, dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if self.fail:
            return None
        return LLMResult(
            text=self.text or "",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=10),
            model="fake",
        )

    def metric(self, key, value, tags=None):
        self.metrics.append((key, dict(tags or {})))


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'intent.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[sticker]
enabled = true
explicit_threshold = 0.2
base_generation_enabled = false
comfyui_enabled = false
intent_backend = "rules"
"""
    path = tmp / "intent.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


class TaxonomyTests(unittest.TestCase):
    def test_every_label_has_instructions(self) -> None:
        # A label added to one table but not the other is invisible to the
        # classifier prompt, which silently caps its accuracy.
        self.assertEqual(set(INTENT_KEYWORDS), set(INTENT_LABELS))

    def test_score_intents_is_deterministic(self) -> None:
        first = score_intents("晚安 好梦 抱抱 喜欢")
        second = score_intents("晚安 好梦 抱抱 喜欢")
        self.assertEqual(first, second)
        self.assertTrue(all(name in INTENT_KEYWORDS for name, _ in first))

    def test_goodnight_beats_shy_on_a_tie(self) -> None:
        # The bug that sent a pouting "才不是" for a bedtime goodnight.
        ranked = [name for name, _ in score_intents("晚安 撒娇")]
        self.assertEqual(ranked[0], "goodnight")


class RuleClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = RuleClassifier()

    def test_keyword_hit(self) -> None:
        self.assertEqual(
            [name for name, _ in self.classifier.classify("晚安表情包", "晚安")],
            ["goodnight"],
        )

    def test_ellipsis_has_nothing_to_match(self) -> None:
        # Documents the gap the contextual classifier exists to close: this is
        # a plain substring matcher, so a euphemism carries no listed keyword.
        self.assertEqual(list(self.classifier.classify("我先去会周公了", "我先去会周公了")), [])
        self.assertEqual(list(self.classifier.classify("你这个人", "哎呦你这个人")), [])

    def test_placeholder_meme_is_not_a_real_request(self) -> None:
        self.assertEqual(list(self.classifier.classify("发个表情包", "发个表情包")), [])


class LlmClassifierTests(unittest.TestCase):
    def test_labels_are_ranked_with_descending_scores(self) -> None:
        llm = FakeLLM('{"intents": ["comfort", "sad"]}')
        guess = LlmClassifier(llm).classify("安慰一下", "我考砸了")
        self.assertEqual(guess.labels, ["comfort", "sad"])
        self.assertGreater(guess.pairs[0][1], guess.pairs[1][1])

    def test_unknown_labels_are_dropped(self) -> None:
        llm = FakeLLM('{"intents": ["同情", "comfort", "angry-ish"]}')
        pairs = LlmClassifier(llm).classify("安慰", "我考砸了")
        self.assertEqual([name for name, _ in pairs], ["comfort"])

    def test_intensity_comes_back_from_the_same_request(self) -> None:
        llm = FakeLLM('{"intents": ["angry"], "intensity": 5}')
        guess = LlmClassifier(llm).classify("你气死我了哼", "你气死我了哼")
        self.assertEqual(guess.labels, ["angry"])
        self.assertEqual(guess.intensity, 5.0)
        self.assertEqual(len(llm.calls), 1)

    def test_out_of_range_intensity_is_ignored(self) -> None:
        llm = FakeLLM('{"intents": ["angry"], "intensity": 9}')
        self.assertIsNone(LlmClassifier(llm).classify("气死我了").intensity)

    def test_markdown_fence_and_prose_are_tolerated(self) -> None:
        llm = FakeLLM('好的\n```json\n{"intents": ["goodnight"]}\n```')
        self.assertEqual(
            [name for name, _ in LlmClassifier(llm).classify("睡", "抱被子的")],
            ["goodnight"],
        )

    def test_context_and_current_message_reach_the_model(self) -> None:
        llm = FakeLLM('{"intents": ["goodnight"]}')
        LlmClassifier(llm).classify(
            "抱被子的",
            "抱被子的",
            "用户: 我要睡了\n她: 快去躺下",
        )
        body = llm.calls[0][-1]["content"]
        self.assertIn("我要睡了", body)
        self.assertIn("抱被子的", body)

    def test_failure_disables_the_classifier_instead_of_stalling(self) -> None:
        llm = FakeLLM(None, fail=True)
        classifier = LlmClassifier(llm, cooldown_seconds=60.0)
        self.assertEqual(list(classifier.classify("晚安")), [])
        # Second call must not pay the timeout again.
        self.assertEqual(list(classifier.classify("晚安")), [])
        self.assertEqual(len(llm.calls), 1)

    def test_garbage_answer_is_recorded_as_failure(self) -> None:
        llm = FakeLLM("我不知道")
        self.assertEqual(list(LlmClassifier(llm).classify("晚安")), [])
        self.assertEqual(llm.metrics[-1][1]["ok"], False)

    def test_unavailable_client_is_skipped(self) -> None:
        llm = FakeLLM('{"intents": ["happy"]}')
        llm.available = False
        self.assertEqual(list(LlmClassifier(llm).classify("晚安")), [])
        self.assertEqual(llm.calls, [])


    def test_repeated_calls_in_one_turn_are_cached(self) -> None:
        # decide() asks once and the caller prints it once; without a cache the
        # two answers can disagree and the wrong sticker gets sent.
        llm = FakeLLM('{"intents": ["goodnight"]}')
        classifier = LlmClassifier(llm)
        first = classifier.classify("睡", "抱被子的", "用户: 我要睡了")
        second = classifier.classify("睡", "抱被子的", "用户: 我要睡了")
        self.assertEqual(first, second)
        self.assertEqual(len(llm.calls), 1)

    def test_cache_expires(self) -> None:
        llm = FakeLLM('{"intents": ["goodnight"]}')
        classifier = LlmClassifier(llm, cache_ttl_seconds=0.0)
        classifier.classify("睡", "抱被子的")
        classifier.classify("睡", "抱被子的")
        self.assertEqual(len(llm.calls), 2)


class HybridClassifierTests(unittest.TestCase):
    def _hybrid(self, text_answer='{"intents": ["comfort"]}', **kwargs):
        llm = FakeLLM(text_answer, **kwargs)
        return HybridClassifier(RuleClassifier(), LlmClassifier(llm)), llm

    def test_clear_keyword_winner_costs_nothing(self) -> None:
        classifier, llm = self._hybrid()
        pairs = classifier.classify("晚安表情包", "晚安表情包")
        self.assertEqual([name for name, _ in pairs][:1], ["goodnight"])
        self.assertEqual(llm.calls, [])

    def test_emotional_intent_still_asks_for_the_strength(self) -> None:
        # Rules settle "angry" instantly but cannot say how angry; the second
        # dimension only exists if the call happens.
        classifier, llm = self._hybrid('{"intents": ["angry"], "intensity": 5}')
        guess = classifier.classify("你气死我了哼", "你气死我了哼")
        self.assertEqual(guess.labels[0], "angry")
        self.assertEqual(guess.intensity, 5.0)
        self.assertEqual(len(llm.calls), 1)

    def test_a_disagreement_does_not_mix_sources(self) -> None:
        # If the second opinion names a different intent, the confident label
        # wins outright rather than borrowing a strength that belongs to another.
        classifier, _llm = self._hybrid('{"intents": ["sad"], "intensity": 5}')
        guess = classifier.classify("你气死我了哼", "你气死我了哼")
        self.assertEqual(guess.labels[0], "angry")
        self.assertIsNone(guess.intensity)

    def test_no_keyword_hit_asks_the_contextual_model(self) -> None:
        classifier, llm = self._hybrid()
        pairs = classifier.classify("我先去会周公了", "我先去会周公了")
        self.assertEqual([name for name, _ in pairs][:1], ["comfort"])
        self.assertEqual(len(llm.calls), 1)

    def test_a_tie_asks_the_contextual_model(self) -> None:
        classifier, llm = self._hybrid()
        # Two keywords, same score: the case that used to pick the wrong one.
        classifier.classify("晚安 撒娇", "晚安 撒娇")
        self.assertEqual(len(llm.calls), 1)

    def test_contextual_failure_keeps_the_rule_answer(self) -> None:
        classifier, _llm = self._hybrid(fail=True)
        pairs = classifier.classify("晚安 撒娇", "晚安 撒娇")
        self.assertTrue(pairs)

    def test_build_classifier_returns_hybrid(self) -> None:
        self.config = None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config, db = make_env(Path(tmp.name))
        self.addCleanup(db.close)
        config._data.setdefault("sticker", {})["intent_backend"] = "hybrid"
        built = build_classifier(config, FakeLLM('{"intents": ["happy"]}'))
        self.assertIsInstance(built, HybridClassifier)


class KevClassifierTests(unittest.TestCase):
    class _Answer:
        def __init__(self, value, probabilities=None):
            self.value = value
            self.probabilities = probabilities or {}

    class _Client:
        def __init__(self, answers=None, *, boom: bool = False):
            self.answers = answers or {}
            self.boom = boom
            self.states: list[dict] = []
            self.questions: list[dict] = []

        def system_one(self, state, questions, **kwargs):
            self.states.append(dict(state))
            self.questions.append({k: dict(v) for k, v in questions.items()})
            if self.boom:
                raise RuntimeError("kev down")
            return self.answers

    def test_probabilities_are_ranked_and_filtered(self) -> None:
        client = self._Client(
            {
                "intent": self._Answer(
                    "goodnight", {"goodnight": 0.8, "nonsense": 0.9, "shy": 0.2}
                )
            }
        )
        guess = KevClassifier(client).classify("睡", "抱被子的", "用户: 我要睡了")
        self.assertEqual([name for name, _ in guess], ["goodnight", "shy"])
        self.assertEqual(client.states[0]["recent_context"], "用户: 我要睡了")

    def test_kind_and_intensity_share_one_request(self) -> None:
        client = self._Client(
            {
                "intent": self._Answer("angry", {"angry": 0.9}),
                "intensity": self._Answer(5.0),
            }
        )
        guess = KevClassifier(client).classify("气死我了", "你气死我了哼")
        # One state, one question set: Kev answers both in parallel and neither
        # question can read the other's answer.
        self.assertEqual(len(client.questions), 1)
        self.assertEqual(set(client.questions[0]), {"intent", "intensity"})
        self.assertEqual(guess.intensity, 5.0)

    def test_missing_intensity_answer_is_none(self) -> None:
        client = self._Client({"intent": self._Answer("happy", {"happy": 0.9})})
        self.assertIsNone(KevClassifier(client).classify("好耶").intensity)

    def test_server_error_falls_back_without_raising(self) -> None:
        classifier = KevClassifier(self._Client(boom=True), cooldown_seconds=60.0)
        self.assertEqual(list(classifier.classify("晚安")), [])


class ServiceIntegrationTests(unittest.TestCase):
    class _Stub:
        name = "stub"

        def __init__(self, labels, intensity=None):
            self.labels = labels
            self.intensity = intensity
            self.seen: list[tuple] = []

        def classify(self, need, user_text=None, context_text=None):
            self.seen.append((need, user_text, context_text))
            return IntentGuess(
                [(label, 0.9 - 0.1 * index) for index, label in enumerate(self.labels)],
                self.intensity,
            )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _service(self, classifier=None) -> StickerService:
        return StickerService(self.db, self.config, classifier=classifier)

    def test_context_is_forwarded_to_the_classifier(self) -> None:
        stub = self._Stub(["goodnight"])
        service = self._service(stub)
        labels = service.requested_intents("抱被子的", "抱被子的", "用户: 我要睡了")
        self.assertEqual(labels, ["goodnight"])
        self.assertEqual(stub.seen[0][2], "用户: 我要睡了")

    def test_invalid_labels_never_escape_the_service(self) -> None:
        stub = self._Stub(["nostalgic", "goodnight", "goodnight"])
        service = self._service(stub)
        self.assertEqual(service.requested_intents("睡", "抱被子的"), ["goodnight"])

    def test_empty_classifier_answer_falls_back_to_rules(self) -> None:
        service = self._service(self._Stub([]))
        self.assertEqual(service.requested_intents("晚安", "晚安表情包"), ["goodnight"])

    def test_raising_classifier_falls_back_to_rules(self) -> None:
        class Boom:
            name = "boom"

            def classify(self, *args, **kwargs):
                raise RuntimeError("model on fire")

        service = self._service(Boom())
        self.assertEqual(service.requested_intents("晚安", "晚安表情包"), ["goodnight"])

    def test_default_service_without_llm_uses_rules(self) -> None:
        service = self._service()
        self.assertIsInstance(service.classifier, RuleClassifier)

    def test_rare_intent_is_reachable(self) -> None:
        # 62 meme stickers drown the single goodnight one in blended ranking;
        # the intent shortlist has to rescue it or the classifier's accuracy
        # never reaches the library.
        service = self._service(self._Stub(["goodnight"]))
        for index in range(30):
            service.add_sticker(
                str(self.tmp / f"meme{index}.png"),
                caption=f"抽象梗图{index}",
                tags=["meme"],
                intents=["meme"],
            )
        service.add_sticker(
            str(self.tmp / "sleep.png"),
            caption="晚安好梦",
            tags=["sleep"],
            intents=["goodnight"],
        )
        decision = service.decide(
            "抱被子的", user_text="抱被子的", context_text="用户: 我要睡了", force=True
        )
        self.assertTrue(decision.send)
        self.assertEqual(decision.reason, "explicit_request")
        self.assertIn("goodnight", decision.sticker.intents)

    def test_shortlist_respects_the_taxonomy(self) -> None:
        service = self._service()
        self.assertEqual(service.search_by_intents(["made-up"], "晚安"), [])

    def _seed_angry_pair(self) -> None:
        # Identical captions and tags so the blended score ties; only the stored
        # intensity can break it, which is what the second dimension is for.
        for name, level in (("mild", 2), ("furious", 5)):
            self._service().add_sticker(
                str(self.tmp / f"{name}.png"),
                caption="生气",
                tags=["angry"],
                intents=["angry"],
                intensity=float(level),
            )

    def test_intensity_picks_the_matching_sticker(self) -> None:
        self._seed_angry_pair()
        mild = self._service(self._Stub(["angry"], intensity=2)).decide(
            "有点烦", user_text="有点烦", force=True
        )
        furious = self._service(self._Stub(["angry"], intensity=5)).decide(
            "气死我了", user_text="气死我了", force=True
        )
        self.assertEqual(mild.sticker.intensity, 2.0)
        self.assertEqual(furious.sticker.intensity, 5.0)
        self.assertTrue(mild.send and furious.send)

    def test_without_intensity_the_old_ranking_applies(self) -> None:
        self._seed_angry_pair()
        decision = self._service(self._Stub(["angry"])).decide(
            "生气", user_text="生气", force=True
        )
        self.assertTrue(decision.send)

    def test_build_classifier_honours_the_config(self) -> None:
        self.assertIsInstance(build_classifier(self.config), RuleClassifier)
        # Config has no setter; the backend switch is a config-file concern.
        self.config._data.setdefault("sticker", {})["intent_backend"] = "llm"
        self.assertIsInstance(build_classifier(self.config), RuleClassifier)
        self.assertIsInstance(
            build_classifier(self.config, FakeLLM("{}")), LlmClassifier
        )


if __name__ == "__main__":
    unittest.main()
