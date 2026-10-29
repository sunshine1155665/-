"""Memory answer behaviour: sibling context, memory-check prompt, runtime meta."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fatfish.context import (
    FACT_RULES,
    MEMORY_CHECK_RULES,
    ContextAssembler,
)
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.memory.eval import score_known, score_opinion, score_unknown
from fatfish.memory.intent import wants_memory_check, wants_verbatim
from fatfish.runtime import (
    memory_privacy_reply,
    wants_activity_review,
    wants_bili_review,
    wants_day_review,
    wants_sing_review,
)
from tests.test_owner import OWNER_SESSION, fake_runtime, make_env


def add_event(db: Database, event_id: str, title: str, when: float) -> None:
    db.execute(
        "INSERT OR IGNORE INTO conversation_events(id, session_id, start_at, end_at, "
        "status, title, summary, turn_ids_json, memory_ids_json, topics_json, "
        "importance, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            "s1",
            when,
            when,
            "archived",
            title,
            "",
            "[]",
            "[]",
            "[]",
            0.6,
            when,
            when,
        ),
    )


def add_detail(
    db: Database,
    *,
    detail_id: str,
    event_id: str,
    text: str,
    when: float,
    seq: int = 0,
    quote: str = "",
    session_id: str = "s1",
) -> None:
    db.execute(
        "INSERT INTO event_details(id, event_id, session_id, seq, kind, subject, "
        "text, quote_text, quote_turn_id, occurred_at, importance, detail_hash, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            detail_id,
            event_id,
            session_id,
            seq,
            "fact",
            "owner",
            text,
            quote or text,
            seq + 1,
            when,
            0.6,
            f"hash-{detail_id}",
            when,
        ),
    )


class MemoryIntentTests(unittest.TestCase):
    def test_memory_questions_are_detected(self) -> None:
        for text in (
            "主人哥哥答应姐姐看你的QQ空间了吗？",
            "上次姐姐是不是问你有没有翻她的动态？",
            "晚安表情包那次，你是不是先逗了我才发的？",
            "我问你今年多大的时候，你怎么说的？",
            "我上次说我养的那只猫叫什么名字？",
            "我们上次去海边捡到的贝壳是什么颜色的？",
            "我跟你说的那家火锅店叫什么？",
            "我去年生日你送了我什么？",
            "你还记得我们第一次聊天吗？",
            "你昨晚在干什么？",
        ):
            self.assertTrue(wants_memory_check(text), text)

    def test_normal_chat_is_not_a_memory_check(self) -> None:
        for text in (
            "今天天气怎么样？",
            "你现在在干什么呀",
            "你今年多大",
            "记得吃饭",
            "别忘了带伞",
            "你答应给我唱歌",
            "给我唱首歌",
        ):
            self.assertFalse(wants_memory_check(text), text)

    def test_verbatim_questions_ask_for_the_quote(self) -> None:
        self.assertTrue(wants_verbatim("你当时怎么说的？"))
        self.assertTrue(wants_verbatim("把原话给我看看"))
        self.assertFalse(wants_verbatim("你还记得那次吗"))

    def test_activity_questions_are_detected(self) -> None:
        for text in (
            "你昨晚在干什么？",
            "你最近在忙什么",
            "今天都在干嘛",
            "凌晨你做了什么",
        ):
            self.assertTrue(wants_activity_review(text), text)
        for text in (
            "你现在在干什么",
            "今天天气怎么样？",
            "昨晚睡得好吗",
            "给我唱首歌",
        ):
            self.assertFalse(wants_activity_review(text), text)

    def test_bilibili_review_questions_are_detected(self) -> None:
        for text in (
            "你上次在B站看的那个视频是什么？",
            "那个视频你怎么看？",
            "《绝区零》挽昼那个视频讲了什么？",
            "你最近在B站刷到的最有意思的视频是哪个？",
        ):
            self.assertTrue(wants_bili_review(text), text)
        for text in (
            "看看B站有什么新视频",
            "给我唱首歌",
            "今天天气怎么样？",
            "B站是什么",
        ):
            self.assertFalse(wants_bili_review(text), text)

    def test_day_review_questions_are_detected(self) -> None:
        for text in (
            "昨天都发生了什么？",
            "今天聊了什么",
            "昨天过得怎么样",
            "最近都发生了什么",
        ):
            self.assertTrue(wants_day_review(text), text)
        for text in (
            "你昨天在干什么",
            "今天天气怎么样？",
            "给我唱首歌",
        ):
            self.assertFalse(wants_day_review(text), text)

    def test_sing_review_questions_are_detected(self) -> None:
        for text in (
            "你上次唱的歌是什么？",
            "你最近唱了什么歌",
            "你刚才唱的是什么",
            "你唱过《等你回家》吗",
        ):
            self.assertTrue(wants_sing_review(text), text)
        for text in (
            "唱首歌给我听",
            "你喜欢唱歌吗",
            "今天天气怎么样？",
        ):
            self.assertFalse(wants_sing_review(text), text)


class MemoryEvalTests(unittest.TestCase):
    def test_known_answer_passes_on_a_key_fact(self) -> None:
        score = score_known("答应了的，姐姐可以看空间。", ["答应", "空间"])
        self.assertTrue(score["pass"])
        self.assertEqual(["答应", "空间"], score["hits"])
        self.assertFalse(score_known("这个我记不清了。", ["答应", "空间"])["pass"])
        normalized = score_known(
            "第 0 天那条，花了 14 块 1 毛 4", ["第0天", "14块1毛4"]
        )
        self.assertTrue(normalized["pass"])
        self.assertIn("第0天", normalized["hits"])

    def test_unknown_answer_needs_a_playful_line(self) -> None:
        self.assertTrue(score_unknown("主人哥哥你耍我！本鲸鱼娘不知道嘛")["pass"])
        self.assertTrue(score_unknown("你是不是记混啦，哼哼")["pass"])
        self.assertTrue(score_unknown("本鲸鱼娘脑子里压根没有这一条呀")["pass"])
        self.assertTrue(score_unknown("你是不是把梦里的东西当真了，我可没跟你捡过贝壳")["pass"])
        self.assertTrue(score_unknown("这我真没有印象，你是不是逗我呀")["pass"])
        self.assertTrue(score_unknown("是不是存心考验本小姐呀，哼")["pass"])
        self.assertTrue(score_unknown("又是一道送命题，你压根没跟我说过")["pass"])
        self.assertTrue(score_unknown("连着考本鲸鱼娘呢，我拿小本本记着")["pass"])
        flat = score_unknown("记录里没有这条，我记性差。")
        self.assertFalse(flat["pass"])
        self.assertTrue(flat["systemy"])
        self.assertFalse(score_unknown("确实不知道。")["pass"])

    def test_opinion_needs_a_stance_not_just_the_title(self) -> None:
        op = score_opinion(
            "那个视频说大肥鱼适合当老婆，我看完尾巴都炸了，哼",
            ["大肥鱼", "老婆"],
        )
        self.assertTrue(op["pass"])
        self.assertTrue(op["stance"])
        bare = score_opinion("《大肥鱼适合做女朋友吗》那个视频", ["大肥鱼"])
        self.assertFalse(bare["pass"])
        self.assertFalse(bare["stance"])
        self.assertTrue(
            score_opinion("最带劲的是绝区零那条，摆明要掏空钱包嘛", ["绝区零"])[
                "stance"
            ]
        )
        self.assertTrue(
            score_opinion("这也太直接了吧，我还没想那么远", ["女友"])["stance"]
        )
        self.assertTrue(
            score_opinion("这结论我认了，别拿体型开玩笑", ["女友"])["stance"]
        )
        self.assertTrue(
            score_opinion("我的看法是当个乐子看，别拿它定身份", ["女友"])["stance"]
        )
        self.assertTrue(
            score_opinion("听着不重，落到身上挺沉，可没装模作样敷衍你", ["辜负"])["stance"]
        )


class SiblingContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        self.memory = MemoryService(self.db, self.config)
        self.now = time.time()

    def test_answer_sibling_rides_along_when_the_question_matches(self) -> None:
        add_event(self.db, "ev-age", "年龄那晚", self.now)
        add_detail(
            self.db,
            detail_id="d-q",
            event_id="ev-age",
            text="鲸鲸今年多大了",
            when=self.now,
            seq=0,
        )
        add_detail(
            self.db,
            detail_id="d-a",
            event_id="ev-age",
            text="按人类那套算岁数不太对得上，我记不清具体是几岁啦",
            when=self.now,
            seq=1,
        )
        # A different event that also says 多大 must not be mixed in unless
        # it matches on its own.
        add_event(self.db, "ev-other", "别的事", self.now)
        add_detail(
            self.db,
            detail_id="d-x",
            event_id="ev-other",
            text="那家店多大来着",
            when=self.now,
        )

        rows = self.memory.recall_details("鲸鲸今年多大了", limit=4)
        texts = [row["text"] for row in rows]
        self.assertTrue(any("鲸鲸今年多大了" in text for text in texts))
        self.assertTrue(any("记不清具体是几岁" in text for text in texts))
        context = [row for row in rows if row.get("context")]
        self.assertEqual(["d-a"], [row["id"] for row in context])
        self.assertEqual("d-q", context[0]["context_for"])
        self.assertFalse(any("那家店多大" in text for text in texts))

    def test_sibling_context_respects_the_per_event_cap(self) -> None:
        add_event(self.db, "ev-cap", "长事件", self.now)
        add_detail(
            self.db,
            detail_id="d-hit",
            event_id="ev-cap",
            text="被批评了",
            when=self.now,
            seq=0,
        )
        for index in range(5):
            add_detail(
                self.db,
                detail_id=f"d-s{index}",
                event_id="ev-cap",
                text=f"同事件补充第{index}句",
                when=self.now + index,
                seq=index + 1,
            )
        self.config._data["memory"]["detail_context_per_event"] = 1
        self.config._data["memory"]["detail_context_max"] = 1
        rows = self.memory.recall_details("被批评了", limit=4)
        context = [row for row in rows if row.get("context")]
        self.assertEqual(1, len(context))
        self.assertIn("同事件补充", context[0]["text"])

    def test_no_sibling_when_the_event_already_matched_twice(self) -> None:
        add_event(self.db, "ev-two", "同一事件", self.now)
        add_detail(
            self.db,
            detail_id="d1",
            event_id="ev-two",
            text="被批评了，好累",
            when=self.now,
            seq=0,
        )
        add_detail(
            self.db,
            detail_id="d2",
            event_id="ev-two",
            text="被批评之后想早点睡",
            when=self.now,
            seq=1,
        )
        rows = self.memory.recall_details("被批评", limit=8)
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([], [row for row in rows if row.get("context")])

    def test_function_bigrams_do_not_make_a_detail_relevant(self) -> None:
        add_event(self.db, "ev-noise", "闲聊", self.now)
        add_detail(
            self.db,
            detail_id="d-noise",
            event_id="ev-noise",
            text="姐姐是来找我聊天的吗",
            when=self.now,
        )
        add_event(self.db, "ev-real", "动态那晚", self.now)
        add_detail(
            self.db,
            detail_id="d-real",
            event_id="ev-real",
            text="姐姐问我是不是翻了她动态，我承认翻过公开动态",
            when=self.now,
        )
        rows = self.memory.recall_details(
            "上次姐姐是不是问你有没有翻她的动态？", limit=4
        )
        texts = [row["text"] for row in rows]
        self.assertTrue(any("公开动态" in text for text in texts))
        self.assertFalse(any("来找我聊天" in text for text in texts))

    def test_same_person_is_not_the_same_event(self) -> None:
        add_event(self.db, "ev-space", "空间约定", self.now)
        add_detail(
            self.db,
            detail_id="d-space",
            event_id="ev-space",
            text="主人哥哥答应姐姐看大肥鱼的QQ空间",
            when=self.now,
        )
        add_event(self.db, "ev-coffee", "口味", self.now)
        add_detail(
            self.db,
            detail_id="d-coffee",
            event_id="ev-coffee",
            text="主人哥哥喜欢喝冰美式，不加糖",
            when=self.now,
        )
        rows = self.memory.recall_details(
            "主人哥哥答应姐姐看你的QQ空间了吗？", limit=4
        )
        texts = [row["text"] for row in rows]
        self.assertTrue(any("QQ空间" in text for text in texts))
        self.assertFalse(any("冰美式" in text for text in texts))

    def test_format_recall_can_put_memory_summaries_first(self) -> None:
        add_event(self.db, "ev-age", "年龄那晚", self.now)
        add_detail(
            self.db,
            detail_id="d-q",
            event_id="ev-age",
            text="鲸鲸今年多大了",
            when=self.now,
        )
        rows = self.memory.recall_details("鲸鲸今年多大了", limit=4)
        item = SimpleNamespace(
            id="m1",
            event_time_start=None,
            observed_at=self.now,
            source_type="model_action",
            gist="姐姐问我是不是翻了她动态，我承认翻过公开动态",
            trust_level="private",
            tags=[],
            entities=[],
            summary="",
            subject="friend",
            about=[],
        )
        hit = SimpleNamespace(item=item, score=0.52, similarity=0.4)
        result = self.memory.format_recall(
            "鲸鲸今年多大了", [hit], [], details=rows, memory_first=True
        )
        text = result.ephemeral_text
        self.assertLess(text.index("[记忆召回]"), text.index("[细节召回]"))
        default = self.memory.format_recall(
            "鲸鲸今年多大了", [hit], [], details=rows
        ).ephemeral_text
        self.assertLess(default.index("[细节召回]"), default.index("[记忆召回]"))

    def test_raw_archive_lines_are_dropped_when_a_private_memory_exists(self) -> None:
        def make_hit(gist: str, trust: str, source: str):
            item = SimpleNamespace(
                id=f"m-{source}",
                event_time_start=None,
                observed_at=self.now,
                source_type=source,
                gist=gist,
                trust_level=trust,
                tags=[],
                entities=[],
                summary="",
                subject="friend",
                about=[],
            )
            return SimpleNamespace(item=item, score=0.5, similarity=0.4)

        private = make_hit(
            "姐姐问我是不是翻了她动态，我承认翻过公开动态", "private", "model_action"
        )
        raw = make_hit("跟我说声晚安，明天见啦", "internal", "event_archive")
        text = self.memory.format_recall(
            "上次姐姐是不是问你有没有翻她的动态？",
            [private, raw],
            [],
            details=[],
            memory_first=True,
        ).ephemeral_text
        self.assertIn("公开动态", text)
        self.assertNotIn("跟我说声晚安", text)

    def test_format_recall_marks_context_separately(self) -> None:
        add_event(self.db, "ev-age", "年龄那晚", self.now)
        add_detail(
            self.db,
            detail_id="d-q",
            event_id="ev-age",
            text="鲸鲸今年多大了",
            when=self.now,
        )
        add_detail(
            self.db,
            detail_id="d-a",
            event_id="ev-age",
            text="我记不清具体是几岁啦",
            when=self.now,
            seq=1,
        )
        rows = self.memory.recall_details("鲸鲸今年多大了", limit=4)
        result = self.memory.format_recall("鲸鲸今年多大了", [], [], details=rows)
        self.assertIn("[细节召回]", result.ephemeral_text)
        self.assertIn("[同事件补充", result.ephemeral_text)
        self.assertIn("我记不清具体是几岁啦", result.ephemeral_text)
        self.assertIn("detail:d-a", result.refs)


class MemoryPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        self.memory = MemoryService(self.db, self.config)
        self.context = ContextAssembler(self.db, self.config, self.memory)

    def test_fact_rules_require_specific_answers_and_cute_absence(self) -> None:
        self.assertIn("具体到", FACT_RULES)
        self.assertIn("主人哥哥你耍我", FACT_RULES)
        self.assertIn("不要编造", FACT_RULES)

    def test_memory_check_block_carries_the_cute_example(self) -> None:
        self.assertIn("主人哥哥你耍我！本鲸鱼娘根本就不知道那东西嘛", MEMORY_CHECK_RULES)
        self.assertIn("不要用“好像/大概/记不清/可能是”", MEMORY_CHECK_RULES)
        self.assertIn("问看法", MEMORY_CHECK_RULES)
        self.assertIn("最/哪个", MEMORY_CHECK_RULES)

    def test_memory_question_gets_the_block(self) -> None:
        bundle = self.context.build(
            OWNER_SESSION, "我上次说养的那只猫叫什么？", now=time.time()
        )
        self.assertIn("[记忆核查]", bundle.text())
        self.assertIn("主人哥哥你耍我", bundle.text())

    def test_direct_basis_block_echoes_the_top_memory(self) -> None:
        item = SimpleNamespace(
            id="m1",
            event_time_start=None,
            observed_at=time.time(),
            source_type="model_action",
            gist="姐姐问我是不是翻了她动态，我承认翻过公开动态",
            trust_level="private",
            tags=[],
            entities=[],
            summary="",
            subject="friend",
            about=[],
        )
        hit = SimpleNamespace(item=item, score=0.52, similarity=0.4)
        recall = self.memory.format_recall(
            "上次姐姐是不是问你有没有翻她的动态？", [hit], [], details=[]
        )
        bundle = self.context.build(
            OWNER_SESSION,
            "上次姐姐是不是问你有没有翻她的动态？",
            recall=recall,
            now=time.time(),
            memory_check=True,
        )
        text = bundle.text()
        self.assertIn("[记忆核查·直接依据]", text)
        self.assertIn("我承认翻过公开动态", text)
        self.assertLess(text.index("[记忆核查·直接依据]"), text.index("[记忆核查]"))

    def test_neutral_question_does_not_get_the_block(self) -> None:
        bundle = self.context.build(
            OWNER_SESSION, "今天天气怎么样？", now=time.time()
        )
        self.assertNotIn("[记忆核查]", bundle.text())

    def test_bilibili_rag_evidence_names_the_video(self) -> None:
        hit = SimpleNamespace(
            id="rag1",
            text="女友不适合，因为女友关系不够确定。适合当老婆！！",
            metadata={"title": "大肥鱼适合做女朋友吗？", "up": "学姐圆"},
            score=0.8,
            source_type="bilibili",
        )
        text = self.memory.format_recall(
            "B站那个视频", [], [hit], details=[]
        ).ephemeral_text
        self.assertIn("《大肥鱼适合做女朋友吗？》 @学姐圆", text)
        self.assertIn("适合当老婆", text)


class MemoryPrivacyGuardTests(unittest.TestCase):
    def _recall(self, session: str = "other", subject: str = "friend"):
        item = SimpleNamespace(
            session_id=session,
            subject=subject,
            trust_level="private",
            gist="姐姐问我是不是翻了她动态，我承认翻过公开动态",
        )
        return SimpleNamespace(memory_hits=[SimpleNamespace(item=item)])

    def test_false_denial_becomes_an_honest_privacy_refusal(self) -> None:
        for reply in (
            "本鲸鱼娘脑子里没这条",
            "没有“姐姐问我动态”这条记录",
            "这个我真没印象",
            "这条本鲸鱼娘脑子里没有呢",
            "本鲸鱼娘脑子里没这段哦",
        ):
            out = memory_privacy_reply(
                reply,
                self._recall(),
                "current",
                True,
                query="上次姐姐是不是问你有没有翻她的动态？",
            )
            self.assertIn("确实跟我提过", out)
            self.assertIn("不方便", out)

    def test_unrelated_question_is_not_rewritten(self) -> None:
        # The friend memory is in the window, but the question is about a cat:
        # "it exists" must not be claimed for an unrelated miss.
        reply = "本鲸鱼娘脑子里没这条"
        out = memory_privacy_reply(
            reply,
            self._recall(),
            "current",
            True,
            query="我上次说我养的那只猫叫什么名字？",
        )
        self.assertEqual(reply, out)

    def test_same_session_denial_is_not_rewritten(self) -> None:
        reply = "本鲸鱼娘脑子里没这条"
        out = memory_privacy_reply(
            reply,
            self._recall("current"),
            "current",
            True,
            query="上次姐姐是不是问你有没有翻她的动态？",
        )
        self.assertEqual(reply, out)

    def test_genuine_miss_is_not_rewritten(self) -> None:
        reply = "本鲸鱼娘脑子里没这条"
        out = memory_privacy_reply(
            reply,
            SimpleNamespace(memory_hits=[]),
            "current",
            True,
            query="上次姐姐是不是问你有没有翻她的动态？",
        )
        self.assertEqual(reply, out)

    def test_normal_chat_is_not_rewritten(self) -> None:
        reply = "本鲸鱼娘脑子里没这条"
        out = memory_privacy_reply(
            reply,
            self._recall(),
            "current",
            False,
            query="上次姐姐是不是问你有没有翻她的动态？",
        )
        self.assertEqual(reply, out)


class RuntimeMemoryMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        from fatfish.owner import OwnerRegistry
        from fatfish.runtime import Runtime

        OwnerRegistry(self.db, self.config).bind(OWNER_SESSION, "主人哥哥")
        self.runtime = Runtime(self.db, self.config)
        self.calls = fake_runtime(self.runtime)

    def _add_activity_turn(
        self, content: str, when: float, *, session: str = "night-test"
    ) -> None:
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at, "
            "ephemeral) VALUES (?,?,?,?,0)",
            (session, "assistant", content, float(when)),
        )

    def test_recent_activity_block_reads_night_and_activity_turns(self) -> None:
        now = time.time()
        self._add_activity_turn(
            "凌晨翻了一圈空间，B站没新东西，本鲸鱼娘困得尾巴都抬不起来",
            now - 3600,
        )
        self._add_activity_turn("休息", now - 1800, session="activity-test")
        blocks = self.runtime._recent_activity_evidence(now)
        text = "\n".join(blocks)
        self.assertIn("[她最近的自主活动", text)
        self.assertIn("B站", text)
        self.assertIn("休息", text)

    def test_recent_activity_block_is_empty_without_records(self) -> None:
        self.assertEqual([], self.runtime._recent_activity_evidence(time.time()))

    def test_activity_question_injects_the_block_into_the_prompt(self) -> None:
        now = time.time()
        self._add_activity_turn(
            "空间翻过了，萌的动态最新，B站没新东西，说说草稿也写好了",
            now - 3600,
        )
        self.runtime.handle_user_message(OWNER_SESSION, "你昨晚在干什么？", now=now)
        sent = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("[她最近的自主活动", sent)
        self.assertIn("B站", sent)

    def test_bilibili_review_injects_stock_and_answer_rules(self) -> None:
        self.runtime.bilibili.ingest_video(
            bvid="BV_TEST_REVIEW",
            title="大肥鱼适合做女朋友吗？",
            up="学姐圆",
            subtitle="女友不适合，因为女友关系不够确定。适合当老婆！！",
            comments="鱼之大，一锅炖不下",
        )
        self.runtime.handle_user_message(
            OWNER_SESSION, "你上次在B站看的那个视频是什么？", now=time.time()
        )
        sent = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("[B站最近入库", sent)
        self.assertIn("大肥鱼适合做女朋友吗？", sent)
        self.assertIn("[B站视频回忆]", sent)

    def test_day_review_injects_the_cross_session_rollup(self) -> None:
        from fatfish.memory.querytime import parse_time_window

        start, end = parse_time_window(
            "昨天都发生了什么？", self.config.timezone_name
        )
        self.runtime.memory.add_item(
            "2026-10-25 日汇总：主人哥哥答应姐姐看空间；姐姐问了动态",
            type="episodic",
            gist="2026-10-25 日汇总",
            session_id="day:2026-10-25",
            event_time_start=start,
            event_time_end=end,
            importance=0.65,
            source_type="day_summary",
            source_id="2026-10-25",
        )
        self.runtime.handle_user_message(
            OWNER_SESSION, "昨天都发生了什么？", now=time.time()
        )
        sent = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("[日汇总（跨会话", sent)
        self.assertIn("答应姐姐看空间", sent)

    def test_sing_review_injects_the_previous_song(self) -> None:
        self.runtime.memory.add_item(
            "给主人哥哥唱了《等你回家》（soft）",
            type="episodic",
            observed_at=time.time() - 60,
            tags=["唱歌", "sing"],
            subject="owner",
            source_type="sing",
            source_id="sing-test",
            trust_level="private",
        )
        self.runtime.handle_user_message(
            OWNER_SESSION, "你上次唱的歌是什么？", now=time.time()
        )
        sent = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("[最近唱过的歌", sent)
        self.assertIn("等你回家", sent)

    def test_result_reports_the_memory_check_and_detail_count(self) -> None:
        add_event(self.db, "ev-space", "空间约定", time.time())
        add_detail(
            self.db,
            detail_id="d-space",
            event_id="ev-space",
            text="主人哥哥答应姐姐看大肥鱼的QQ空间",
            when=time.time(),
        )
        add_detail(
            self.db,
            detail_id="d-space-when",
            event_id="ev-space",
            text="是 2026-10-25 晚上答应的",
            when=time.time() + 1,
            seq=1,
        )
        result = self.runtime.handle_user_message(
            OWNER_SESSION, "主人哥哥答应姐姐看我的QQ空间了吗？", now=time.time()
        )
        self.assertTrue(result["memory"]["check"])
        self.assertGreaterEqual(result["memory"]["details"], 1)
        self.assertEqual(1, result["memory"]["detail_context"])

    def test_neutral_turn_is_not_a_memory_check(self) -> None:
        result = self.runtime.handle_user_message(
            OWNER_SESSION, "今天天气怎么样？", now=time.time()
        )
        self.assertFalse(result["memory"]["check"])
        self.assertEqual(0, result["memory"]["details"])



class QQMemorySharingTests(unittest.TestCase):
    """QQ private/group sessions share memory (owner decision 2026-10-26).

    The owner controls which groups she joins, so there is no group/private
    memory wall.  Only test-session fixtures stay filtered, and Bilibili
    content is shared with the owner in private rather than in a group.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config, self.db, _ = make_env(Path(self._tmp.name))
        self.addCleanup(self.db.close)
        from fatfish.owner import OwnerRegistry
        from fatfish.runtime import Runtime

        OwnerRegistry(self.db, self.config).bind(OWNER_SESSION, "主人哥哥")
        self.runtime = Runtime(self.db, self.config)
        self.calls = fake_runtime(self.runtime)

    def test_group_and_private_share_memories_and_details(self) -> None:
        self.runtime.memory.add_item(
            "主人哥哥昨天被老板骂了，心情很差",
            session_id=OWNER_SESSION,
            source_type="model_action",
            importance=0.8,
        )
        self.runtime.memory.add_item(
            "主人哥哥在群里说今晚要加班到很晚",
            session_id="qq:group:2001",
            source_type="model_action",
            importance=0.8,
        )
        add_detail(
            self.db,
            detail_id="d-owner",
            event_id="ev-owner",
            text="主人哥哥被老板骂了",
            when=time.time(),
            session_id=OWNER_SESSION,
        )
        add_detail(
            self.db,
            detail_id="d-group",
            event_id="ev-group",
            text="主人哥哥昨天在群里说加班到很晚",
            when=time.time() - 86400,
            session_id="qq:group:2001",
        )
        self.runtime.handle_user_message(
            "qq:group:2001", "老板骂 加班", is_group=True, now=time.time()
        )
        group_prompt = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("被老板骂", group_prompt)
        self.assertIn("在群里说加班到很晚", group_prompt)

        self.runtime.handle_user_message(
            OWNER_SESSION, "老板骂 加班", now=time.time()
        )
        private_prompt = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("被老板骂", private_prompt)
        self.assertIn("在群里说加班到很晚", private_prompt)

    def test_friend_sees_group_memories(self) -> None:
        from fatfish.owner import OwnerRegistry

        OwnerRegistry(self.db, self.config).bind_friend("qq:private:2", "姐姐")
        self.runtime.memory.add_item(
            "群友小李说他明天结婚",
            session_id="qq:group:2001",
            source_type="model_action",
            importance=0.8,
        )
        self.runtime.handle_user_message(
            "qq:private:2", "明天谁结婚？", now=time.time()
        )
        friend_prompt = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("群友小李", friend_prompt)

    def test_group_gets_memory_check_owner_address_and_bili_private_rule(self) -> None:
        self.runtime.handle_user_message(
            "qq:group:2001",
            "我上次说我养的那只猫叫什么名字？",
            is_group=True,
            now=time.time(),
        )
        group_prompt = "\n".join(
            str(message.get("content") or "")
            for message in self.calls[-1]["messages"]
        )
        self.assertIn("[记忆核查]", group_prompt)
        self.assertIn("照样称呼主人", group_prompt)
        self.assertNotIn("不要使用主人称呼", group_prompt)
        self.assertIn("B站", group_prompt)
        self.assertIn("私聊说给主人听", group_prompt)


if __name__ == "__main__":
    unittest.main()
