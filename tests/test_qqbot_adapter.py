"""Offline tests for the official QQ Bot adapter.

No network, no real ``data/fatfish.db`` and no real credentials: HTTP is faked
with an injectable opener, the WebSocket client is replaced by a small fake,
and the runtime is a recorder double.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fatfish.platforms import qqbot as qqbot_module
from fatfish.platforms.qqbot import (
    DEFAULT_INTENTS,
    DEFAULT_PASSIVE_REPLY_LIMIT,
    MAX_REPLY_COUNTS,
    MAX_SEEN_EVENTS,
    QQBotAdapter,
    QQBotAuthError,
    QQBotClient,
    QQBotHTTPError,
    resolve_settings,
    split_text,
)


# --------------------------------------------------------------------- doubles
class FakeSectionConfig:
    """Just the ``section()`` API the adapter uses."""

    def __init__(self, sections: dict[str, dict] | None = None) -> None:
        self._sections = sections or {}

    def section(self, name: str) -> dict:
        return dict(self._sections.get(name, {}))


class FakeSocket:
    """Records what the client writes to the gateway socket."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, raw) -> None:
        self.sent.append(json.loads(raw))


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeOpener:
    """``urllib.request.urlopen`` stand-in; records every request."""

    def __init__(
        self,
        *,
        token: str = "TEST-TOKEN",
        gateway: str = "wss://gateway.invalid/websocket",
        message_reply: dict | None = None,
    ) -> None:
        self.token = token
        self.gateway = gateway
        self.message_reply = dict(message_reply or {"id": "sent-1", "timestamp": 1})
        self.requests: list = []
        self.message_bodies: list[dict] = []
        self.fail: Exception | None = None

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        url = request.full_url
        if url.endswith("/app/getAppAccessToken"):
            return FakeResponse({"access_token": self.token, "expires_in": 7200})
        if url.endswith("/gateway"):
            return FakeResponse({"url": self.gateway})
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        self.message_bodies.append(body)
        return FakeResponse(dict(self.message_reply))


class FakeOutbox:
    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.all: list[dict] = []
        self._next_id = 0

    def enqueue(self, kind, payload=None, *, idempotency_key=None, **kwargs):
        self._next_id += 1
        entry = {
            "id": self._next_id,
            "kind": kind,
            "payload": dict(payload or {}),
            "idempotency_key": idempotency_key,
        }
        self.entries.append(entry)
        self.all.append(entry)
        return self._next_id, True


class FakeRuntime:
    """Records turns and runs the adapter's outbox handlers directly."""

    def __init__(self, messages: list[str] | None = None, error: Exception | None = None):
        self.outbox = FakeOutbox()
        self.messages = ["回复一"] if messages is None else list(messages)
        self.error = error
        self.extra_result: dict = {}
        self.calls: list[dict] = []
        self.extras: list[dict] = []
        self.tick_calls = 0
        self.process_calls = 0
        self.outcomes: list[dict] = []

    def handle_user_message(
        self,
        session_id,
        text,
        *,
        is_group=False,
        sender_id=None,
        sender_name="",
        **kwargs,
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "text": text,
                "is_group": is_group,
                "sender_id": sender_id,
                "sender_name": sender_name,
            }
        )
        self.extras.append(
            {
                "images": kwargs.get("images"),
                "extra_evidence": kwargs.get("extra_evidence"),
                "reply_to": kwargs.get("reply_to"),
            }
        )
        if self.error is not None:
            raise self.error
        return {"messages": list(self.messages), **dict(self.extra_result)}

    def process_outbox(self, *, handlers=None, limit=10, now=None):
        self.process_calls += 1
        handlers = handlers or {}
        results = []
        remaining = []
        for entry in self.outbox.entries:
            handler = handlers.get(entry["kind"])
            if handler is None:
                remaining.append(entry)
                continue
            outcome = handler(entry)
            self.outcomes.append(outcome)
            results.append(
                {
                    "id": entry["id"],
                    "kind": entry["kind"],
                    "status": "done" if outcome.get("ok") else "failed",
                    "result": outcome,
                    "error": outcome.get("error"),
                }
            )
        self.outbox.entries = remaining
        return results

    def tick(self, *, handlers=None, now=None, **kwargs):
        """Stand-in for Runtime.tick (scheduler)."""
        self.tick_calls += 1
        return {"life": {}, "wakeups": [], "outbox": []}


class FakeQQBotClient:
    """Stand-in for :class:`QQBotClient` (no HTTP, no socket)."""

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.connected = True
        self.sent: list[tuple[str, str, str, str | None]] = []
        self.media: list[dict] = []
        self.fail_voice = False
        self.session_id = "SESSION-1"
        self.text_chunk_chars = 2000
        self.passive_reply_limit = 4
        self.closed = False

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False
        self.closed = True

    def send_text(self, scope, target_id, text, *, reply_to=None):
        self.sent.append((str(scope), str(target_id), str(text), reply_to))
        return {
            "ok": True,
            "scope": scope,
            "target_id": str(target_id),
            "chunks": 1,
            "passive": 1 if reply_to else 0,
            "proactive": 0 if reply_to else 1,
        }

    def send_media(self, scope, target_id, path, *, kind="file", reply_to=None, content=""):
        if kind == "voice" and self.fail_voice:
            raise RuntimeError("voice upload rejected")
        self.media.append(
            {
                "scope": str(scope),
                "target_id": str(target_id),
                "path": str(path),
                "kind": kind,
                "reply_to": reply_to,
                "content": content,
            }
        )
        return {
            "ok": True,
            "scope": scope,
            "target_id": str(target_id),
            "kind": kind,
            "path": str(path),
        }

    def recv(self, timeout=1.0):
        return None


# ---------------------------------------------------------------------- events
def c2c_event(
    *,
    openid: str = "USER-OPENID-1",
    text: str = "你好呀",
    msg_id: str = "EVENT-1",
    username: str = "小明",
) -> dict:
    return {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "s": 3,
        "d": {
            "id": msg_id,
            "content": text,
            "author": {"user_openid": openid, "username": username},
            "timestamp": "2026-11-07T00:00:00+08:00",
        },
    }


def group_at_event(
    *,
    group: str = "GROUP-OPENID-1",
    member: str = "MEMBER-1",
    text: str = "<@!123456> 在吗",
    msg_id: str = "GROUP-EVENT-1",
    username: str = "小红",
) -> dict:
    return {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "s": 4,
        "d": {
            "id": msg_id,
            "content": text,
            "group_openid": group,
            "author": {"member_openid": member, "username": username},
            "timestamp": "2026-11-07T00:00:01+08:00",
        },
    }


def group_plain_event(*, text: str = "随便聊聊", msg_id: str = "GROUP-PLAIN-1") -> dict:
    event = group_at_event(text=text, msg_id=msg_id)
    event["t"] = "GROUP_MESSAGE_CREATE"
    return event


def quote_ref_event(*, ref: str = "Q100", text: str = "你看这个", msg_id: str = "EVENT-Q2") -> dict:
    event = c2c_event(text=text, msg_id=msg_id)
    event["d"]["message_scene"] = {"ext": [f"ref_msg_idx={ref}"]}
    event["d"]["message_type"] = 103
    event["d"]["msg_elements"] = [
        {"msg_idx": ref, "content": "被引用的原话"}
    ]
    return event


def event_with_msg_idx(*, msg_idx: str = "Q100", text: str = "被引用的原话", msg_id: str = "EVENT-Q1") -> dict:
    event = c2c_event(text=text, msg_id=msg_id)
    event["d"]["message_scene"] = {"ext": [f"msg_idx={msg_idx}"]}
    return event


def media_event(*, msg_id: str = "MEDIA-1", text: str = "") -> dict:
    event = c2c_event(text=text, msg_id=msg_id)
    event["d"]["attachments"] = [
        {"content_type": "image/png", "url": "https://example.invalid/a.png"},
        {"content_type": "voice/silk", "url": "https://example.invalid/a.silk", "asr_refer_text": "我给你发语音啦"},
        {"content_type": "file/pdf", "url": "https://example.invalid/a.pdf", "filename": "合同.pdf"},
    ]
    return event


def make_client(**kwargs):
    opener = kwargs.pop("opener", None) or FakeOpener()
    client = QQBotClient(
        app_id="APPID-123456", app_secret="SECRET-VALUE", opener=opener, **kwargs
    )
    return client, opener


def _token_requests(opener: FakeOpener) -> list:
    return [r for r in opener.requests if r.full_url.endswith("/app/getAppAccessToken")]


# ------------------------------------------------------------------- parse tests
class QQBotParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = QQBotAdapter(FakeRuntime(), FakeSectionConfig())

    def test_c2c_event_becomes_private_session(self) -> None:
        message = self.adapter.parse_event(c2c_event())
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.platform, "qq")
        self.assertEqual(message.session_id, "qq:private:USER-OPENID-1")
        self.assertEqual(message.user_id, "USER-OPENID-1")
        self.assertIsNone(message.group_id)
        self.assertFalse(message.is_group)
        self.assertTrue(message.mentioned)
        self.assertEqual(message.text, "你好呀")
        self.assertEqual(message.event_id, "EVENT-1")
        self.assertEqual(message.sender_name, "小明")

    def test_group_at_event_strips_mention_token(self) -> None:
        config = FakeSectionConfig({"qqbot": {"group_enabled": True}})
        adapter = QQBotAdapter(FakeRuntime(), config)
        message = adapter.parse_event(group_at_event())
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.session_id, "qq:group:GROUP-OPENID-1")
        self.assertEqual(message.group_id, "GROUP-OPENID-1")
        self.assertEqual(message.user_id, "MEMBER-1")
        self.assertTrue(message.is_group)
        self.assertTrue(message.mentioned)
        self.assertEqual(message.text, "在吗")
        self.assertEqual(message.sender_name, "小红")

    def test_group_events_need_group_enabled(self) -> None:
        self.assertIsNone(self.adapter.parse_event(group_at_event()))

    def test_group_message_without_mention_ignored_by_default(self) -> None:
        config = FakeSectionConfig({"qqbot": {"group_enabled": True}})
        adapter = QQBotAdapter(FakeRuntime(), config)
        self.assertIsNone(adapter.parse_event(group_plain_event()))

        config = FakeSectionConfig(
            {"qqbot": {"group_enabled": True, "require_mention": False}}
        )
        adapter = QQBotAdapter(FakeRuntime(), config)
        message = adapter.parse_event(group_plain_event())
        self.assertIsNotNone(message)
        assert message is not None
        self.assertFalse(message.mentioned)
        self.assertEqual(message.text, "随便聊聊")

    def test_empty_content_and_unrelated_events(self) -> None:
        self.assertIsNone(self.adapter.parse_event(c2c_event(text="   ")))
        self.assertIsNone(self.adapter.parse_event({"op": 0, "t": "GUILD_CREATE", "d": {}}))
        self.assertIsNone(self.adapter.parse_event({"t": "C2C_MESSAGE_CREATE"}))
        self.assertIsNone(self.adapter.parse_event(None))
        self.assertIsNone(self.adapter.parse_event("not-a-dict"))  # type: ignore[arg-type]

    def test_private_whitelist_filters_c2c(self) -> None:
        config = FakeSectionConfig({"qqbot": {"private_whitelist": ["ALLOWED"]}})
        adapter = QQBotAdapter(FakeRuntime(), config)
        self.assertIsNone(adapter.parse_event(c2c_event(openid="OTHER")))
        message = adapter.parse_event(c2c_event(openid="ALLOWED"))
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.session_id, "qq:private:ALLOWED")

    def test_defaults_match_spec(self) -> None:
        settings = resolve_settings(None)
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["app_id_env"], "QQBOT_APPID")
        self.assertEqual(settings["app_secret_env"], "QQBOT_SECRET")
        self.assertEqual(settings["private_whitelist"], [])
        self.assertFalse(settings["group_enabled"])
        self.assertTrue(settings["require_mention"])
        self.assertEqual(settings["max_message_chars"], 2000)
        self.assertEqual(settings["text_chunk_chars"], 2000)
        self.assertEqual(settings["passive_reply_limit"], DEFAULT_PASSIVE_REPLY_LIMIT)
        self.assertEqual(settings["merge_window_seconds"], 1.0)
        self.assertEqual(settings["quote_cache_size"], 500)
        self.assertTrue(settings["immersion_hint"])
        self.assertTrue(settings["media_download"])
        self.assertEqual(settings["media_max_bytes"], 5_000_000)
        self.assertEqual(settings["media_max_images"], 4)
        self.assertEqual(settings["media_timeout"], 10.0)
        self.assertTrue(settings["fallback_enabled"])
        self.assertIn("走神", settings["fallback_text"])
        self.assertEqual(settings["fallback_cooldown_seconds"], 120.0)
        self.assertFalse(settings["debug"])
        self.assertEqual(settings["intents"], DEFAULT_INTENTS)

    def test_env_overrides_settings(self) -> None:
        env = {
            "QQBOT_INTENTS": "1",
            "QQBOT_API_BASE_URL": "https://api.example.invalid",
            "QQBOT_TOKEN_BASE_URL": "https://token.example.invalid",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            settings = resolve_settings(None)
        self.assertEqual(settings["intents"], 1)
        self.assertEqual(settings["api_base_url"], "https://api.example.invalid")
        self.assertEqual(settings["token_base_url"], "https://token.example.invalid")


# -------------------------------------------------------------------- split tests
class QQBotSplitTests(unittest.TestCase):
    def test_chunks_respect_limit_and_lose_nothing(self) -> None:
        text = (
            "第一句话。第二句话，有点长。\n"
            + "あ" * 15
            + "\n第三段没有标点但是要切开"
            + "x" * 40
        )
        for limit in (5, 7, 13, 64, 2000):
            chunks = split_text(text, limit)
            self.assertTrue(all(len(chunk) <= limit for chunk in chunks), (limit, chunks))
            self.assertEqual("".join(chunks), text, limit)

    def test_prefers_break_boundaries(self) -> None:
        text = "一二三\n四五六七八九"
        chunks = split_text(text, 8)
        self.assertEqual(chunks, ["一二三\n", "四五六七八九"])

    def test_empty_text(self) -> None:
        self.assertEqual(split_text("", 10), [])
        self.assertEqual(split_text(None, 10), [])  # type: ignore[arg-type]


# ------------------------------------------------------------------- client tests
class QQBotClientTests(unittest.TestCase):
    def test_token_is_cached_and_gateway_uses_bearer_header(self) -> None:
        client, opener = make_client()
        first = client.access_token()
        second = client.access_token()
        self.assertEqual(first, "TEST-TOKEN")
        self.assertEqual(second, "TEST-TOKEN")
        self.assertEqual(len(_token_requests(opener)), 1)

        url = client.gateway_url()
        self.assertEqual(url, "wss://gateway.invalid/websocket")
        self.assertEqual(len(_token_requests(opener)), 1)
        gateway_request = [
            r for r in opener.requests if r.full_url.endswith("/gateway")
        ][0]
        headers = {key.lower(): value for key, value in gateway_request.headers.items()}
        self.assertEqual(headers.get("authorization"), "QQBot TEST-TOKEN")
        # Token body carries the app id + secret, and nothing else.
        token_body = json.loads(_token_requests(opener)[0].data.decode("utf-8"))
        self.assertEqual(token_body, {"appId": "APPID-123456", "clientSecret": "SECRET-VALUE"})

    def test_http_error_reports_status_and_body(self) -> None:
        client, opener = make_client()
        error = urllib.error.HTTPError(
            "https://api.bot.qq.com/v2/users/u/messages",
            401,
            "Unauthorized",
            None,
            io.BytesIO(b'{"code": 10001, "message": "invalid token"}'),
        )
        opener.fail = error
        with self.assertRaises(QQBotHTTPError) as ctx:
            client.access_token(force=True)
        message = str(ctx.exception)
        self.assertIn("401", message)
        self.assertIn("invalid token", message)
        self.assertEqual(ctx.exception.status, 401)

    def test_http_error_status_on_body_preview(self) -> None:
        class ErrorOpener(FakeOpener):
            def __call__(self, request, timeout=None):
                return FakeResponse({"code": 40054, "message": "bad"}, status=400)

        client = QQBotClient(
            app_id="APPID-123456", app_secret="SECRET", opener=ErrorOpener()
        )
        with self.assertRaises(QQBotHTTPError) as ctx:
            client.access_token()
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("40054", str(ctx.exception))

    def test_token_business_error_on_http_200(self) -> None:
        class RejectOpener(FakeOpener):
            def __call__(self, request, timeout=None):
                return FakeResponse({"code": 100016, "message": "invalid appid or secret"})

        client = QQBotClient(
            app_id="APPID-123456", app_secret="SECRET", opener=RejectOpener()
        )
        with self.assertRaises(QQBotAuthError) as ctx:
            client.access_token()
        self.assertIn("100016", str(ctx.exception))
        self.assertIn("invalid appid or secret", str(ctx.exception))

    def test_send_fails_on_err_code_envelope_http_200(self) -> None:
        """腾讯有时用 HTTP 200 + err_code 正文拒发；2xx 不能想当然算成功。"""
        opener = FakeOpener(
            message_reply={
                "err_code": 40034005,
                "message": "回复消息msg_id已过期",
                "trace_id": "t-123",
            }
        )
        client = QQBotClient(
            app_id="APPID-123456", app_secret="SECRET", opener=opener
        )
        with self.assertRaises(QQBotHTTPError) as ctx:
            client.send_text("c2c", "OPENID", "你好呀")
        self.assertIn("err_code=40034005", str(ctx.exception))
        self.assertIn("msg_id已过期", str(ctx.exception))
        self.assertIn("t-123", str(ctx.exception))

    def test_send_ok_when_err_code_is_zero(self) -> None:
        opener = FakeOpener(message_reply={"err_code": 0, "id": "sent-9"})
        client = QQBotClient(
            app_id="APPID-123456", app_secret="SECRET", opener=opener
        )
        summary = client.send_text("c2c", "OPENID", "你好呀")
        self.assertTrue(summary["ok"])

    def test_missing_credentials_raise_auth_error(self) -> None:
        client = QQBotClient(opener=FakeOpener())
        with mock.patch.dict(os.environ, {"QQBOT_APPID": "", "QQBOT_SECRET": ""}, clear=False):
            with self.assertRaises(QQBotAuthError) as ctx:
                client.access_token()
        self.assertIn("QQBOT_APPID", str(ctx.exception))

    def test_passive_replies_use_msg_id_and_increasing_seq_then_go_proactive(self) -> None:
        client, opener = make_client()
        for _ in range(DEFAULT_PASSIVE_REPLY_LIMIT):
            client.send_text("c2c", "USER-1", "hi", reply_to="MSG-1")
        bodies = opener.message_bodies
        self.assertEqual([body.get("msg_id") for body in bodies], ["MSG-1"] * 4)
        self.assertEqual([body.get("msg_seq") for body in bodies], [1, 2, 3, 4])
        self.assertTrue(all(body["msg_type"] == 0 for body in bodies))

        client.send_text("c2c", "USER-1", "hi", reply_to="MSG-1")
        self.assertNotIn("msg_id", bodies[-1])
        self.assertNotIn("msg_seq", bodies[-1])
        self.assertEqual(bodies[-1]["content"], "hi")

    def test_proactive_send_has_no_msg_id(self) -> None:
        client, opener = make_client()
        summary = client.send_proactive("group", "GROUP-1", "主动消息")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["passive"], 0)
        self.assertEqual(summary["chunks"], 1)
        self.assertNotIn("msg_id", opener.message_bodies[-1])
        self.assertEqual(opener.message_bodies[-1]["content"], "主动消息")

    def test_long_text_chunks_and_degrades_after_passive_limit(self) -> None:
        client, opener = make_client()
        client.text_chunk_chars = 10
        text = "一二三四五六七八九十" * 5  # 50 chars -> 5 chunks of 10
        summary = client.send_text("c2c", "USER-1", text, reply_to="MSG-LONG")
        self.assertEqual(summary["chunks"], 5)
        self.assertEqual(summary["passive"], DEFAULT_PASSIVE_REPLY_LIMIT)
        self.assertEqual(summary["proactive"], 1)
        bodies = opener.message_bodies
        self.assertEqual([b.get("msg_seq") for b in bodies[:4]], [1, 2, 3, 4])
        self.assertNotIn("msg_id", bodies[4])
        self.assertEqual("".join(b["content"] for b in bodies), text)
        self.assertTrue(all(len(b["content"]) <= 10 for b in bodies))

    def test_identify_then_resume_payloads(self) -> None:
        client, _opener = make_client()
        client.ws = FakeSocket()
        client.connected = True
        client._send_identify_or_resume("TOKEN")
        identify = client.ws.sent[0]
        self.assertEqual(identify["op"], 2)
        self.assertEqual(identify["d"]["token"], "QQBot TOKEN")
        self.assertEqual(identify["d"]["intents"], DEFAULT_INTENTS)
        self.assertEqual(identify["d"]["shard"], [0, 1])

        client.session_id = "SESSION-ABC"
        client.last_seq = 42
        client._send_identify_or_resume("TOKEN")
        resume = client.ws.sent[1]
        self.assertEqual(resume["op"], 6)
        self.assertEqual(
            resume["d"],
            {"token": "QQBot TOKEN", "session_id": "SESSION-ABC", "seq": 42},
        )

    def test_scope_maps_to_protocol_paths(self) -> None:
        client, opener = make_client()
        client.send_text("private", "USER-X", "a")
        self.assertTrue(opener.requests[-1].full_url.endswith("/v2/users/USER-X/messages"))
        client.send_text("group", "GROUP-X", "b")
        self.assertTrue(
            opener.requests[-1].full_url.endswith("/v2/groups/GROUP-X/messages")
        )

    def test_reply_count_cache_is_bounded(self) -> None:
        client, opener = make_client()
        self.assertEqual(client.reply_counts_limit, MAX_REPLY_COUNTS)
        client.reply_counts_limit = 3
        for index in range(5):
            client.send_text("c2c", "USER-1", "hi", reply_to=f"MSG-{index}")
        self.assertEqual(len(client._reply_counts), 3)
        self.assertEqual(sorted(client._reply_counts), ["MSG-2", "MSG-3", "MSG-4"])

    def test_status_never_leaks_the_app_secret(self) -> None:
        client, _opener = make_client()
        client.access_token()
        dumped = json.dumps(client.status(), ensure_ascii=False)
        self.assertNotIn("SECRET-VALUE", dumped)
        self.assertTrue(client.status()["token_cached"])


    def test_send_media_uploads_then_sends_msg_type_7(self) -> None:
        class MediaOpener(FakeOpener):
            def __call__(self, request, timeout=None):
                self.requests.append(request)
                url = request.full_url
                if url.endswith("/app/getAppAccessToken"):
                    return FakeResponse(
                        {"access_token": self.token, "expires_in": 7200}
                    )
                body = json.loads(request.data.decode("utf-8")) if request.data else {}
                if url.endswith("/files"):
                    return FakeResponse(
                        {"file_info": "FILE-INFO-1", "file_uuid": "UUID-1", "ttl": 600}
                    )
                self.message_bodies.append(body)
                return FakeResponse({"id": "sent-media", "timestamp": 1})

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(b"RIFF-fake-wav")
            audio_path = handle.name
        try:
            opener = MediaOpener()
            client = QQBotClient(
                app_id="APPID-123456", app_secret="SECRET", opener=opener
            )
            summary = client.send_media(
                "c2c", "USER-1", audio_path, kind="voice", reply_to="MSG-1"
            )
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["kind"], "voice")
            upload_urls = [r.full_url for r in opener.requests if r.full_url.endswith("/files")]
            self.assertEqual(len(upload_urls), 1)
            self.assertTrue(upload_urls[0].endswith("/v2/users/USER-1/files"))
            message = opener.message_bodies[-1]
            self.assertEqual(message["msg_type"], 7)
            self.assertEqual(message["media"], {"file_info": "FILE-INFO-1"})
            self.assertEqual(message["msg_id"], "MSG-1")
        finally:
            Path(audio_path).unlink(missing_ok=True)

    def test_send_media_proactive_omits_msg_seq(self) -> None:
        # Without a passive msg_id, including msg_seq is malformed and the
        # platform can reject the rich-media send.  Mirror send_text: omit both.
        class MediaOpener(FakeOpener):
            def __call__(self, request, timeout=None):
                self.requests.append(request)
                url = request.full_url
                if url.endswith("/app/getAppAccessToken"):
                    return FakeResponse(
                        {"access_token": self.token, "expires_in": 7200}
                    )
                body = json.loads(request.data.decode("utf-8")) if request.data else {}
                if url.endswith("/files"):
                    return FakeResponse({"file_info": "FILE-INFO-1"})
                self.message_bodies.append(body)
                return FakeResponse({"id": "sent-proactive-media"})

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(b"\x89PNG\r\n\x1a\nfake")
            image_path = handle.name
        try:
            opener = MediaOpener()
            client = QQBotClient(
                app_id="APPID-123456", app_secret="SECRET", opener=opener
            )
            client.send_media("c2c", "USER-1", image_path, kind="image")
            body = opener.message_bodies[-1]
            self.assertEqual(body["msg_type"], 7)
            self.assertNotIn("msg_id", body)
            self.assertNotIn("msg_seq", body)
        finally:
            Path(image_path).unlink(missing_ok=True)

    def test_send_media_rejects_missing_file(self) -> None:
        client = QQBotClient(
            app_id="APPID-123456", app_secret="SECRET", opener=FakeOpener()
        )
        with self.assertRaises(qqbot_module.QQBotError):
            client.send_media("c2c", "USER-1", "Z:/definitely-missing.wav", kind="voice")


# ------------------------------------------------------------------ adapter tests
class QQBotAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeRuntime(messages=["第一句", "第二句"])
        self.config = FakeSectionConfig(
            {"qqbot": {"group_enabled": True, "merge_window_seconds": 0}}
        )
        self.adapter = QQBotAdapter(self.runtime, self.config)
        self.client = FakeQQBotClient()
        self.adapter.client = self.client

    def test_async_photo_image_sends_with_reply_to(self) -> None:
        outcome = self.adapter.send_media_outbox_item(
            {
                "id": 1,
                "kind": "qq_send_image",
                "payload": {
                    "target_type": "private",
                    "target_id": "USER-OPENID-1",
                    "path": "photo.png",
                    "kind": "image",
                    "content": "自拍",
                    "reply_to": "EVENT-1",
                },
            }
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.client.media[-1]["reply_to"], "EVENT-1")
        self.assertEqual(self.client.media[-1]["content"], "自拍")

    def test_private_event_turns_enqueues_and_sends(self) -> None:
        result = self.adapter.handle_event(c2c_event())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["ok"])
        self.assertEqual(result["event_id"], "EVENT-1")
        self.assertEqual(result["session_id"], "qq:private:USER-OPENID-1")
        self.assertEqual(result["messages"], ["第一句", "第二句"])

        self.assertEqual(
            self.runtime.calls,
            [
                {
                    "session_id": "qq:private:USER-OPENID-1",
                    "text": "你好呀",
                    "is_group": False,
                    "sender_id": "USER-OPENID-1",
                    "sender_name": "小明",
                }
            ],
        )
        entries = self.runtime.outbox.all
        self.assertEqual([entry["kind"] for entry in entries], ["qq_send", "qq_send"])
        self.assertEqual(
            [entry["payload"] for entry in entries],
            [
                {
                    "target_type": "private",
                    "target_id": "USER-OPENID-1",
                    "text": "第一句",
                    "reply_to": "EVENT-1",
                },
                {
                    "target_type": "private",
                    "target_id": "USER-OPENID-1",
                    "text": "第二句",
                    "reply_to": None,
                },
            ],
        )
        self.assertEqual(
            [entry["idempotency_key"] for entry in entries],
            ["qqbot:EVENT-1:0", "qqbot:EVENT-1:1"],
        )
        self.assertEqual(
            self.client.sent,
            [
                ("private", "USER-OPENID-1", "第一句", "EVENT-1"),
                ("private", "USER-OPENID-1", "第二句", None),
            ],
        )
        self.assertEqual(self.adapter.counters["events"], 1)
        self.assertEqual(self.adapter.counters["turns"], 1)
        self.assertEqual(self.adapter.counters["sent"], 2)
        # Async photo jobs stash this id on the job row and send the image as a
        # passive rich-media reply later.
        self.assertEqual(self.runtime.extras[0]["reply_to"], "EVENT-1")

    def test_group_event_targets_group_session(self) -> None:
        self.adapter.handle_event(group_at_event())
        entries = self.runtime.outbox.all
        self.assertEqual(entries[0]["payload"]["target_type"], "group")
        self.assertEqual(entries[0]["payload"]["target_id"], "GROUP-OPENID-1")
        self.assertEqual(
            self.client.sent[0],
            ("group", "GROUP-OPENID-1", "第一句", "GROUP-EVENT-1"),
        )
        self.assertTrue(self.runtime.calls[0]["is_group"])
        self.assertEqual(self.runtime.calls[0]["session_id"], "qq:group:GROUP-OPENID-1")

    def test_duplicate_event_is_processed_once(self) -> None:
        first = self.adapter.handle_event(c2c_event())
        second = self.adapter.handle_event(c2c_event())
        self.assertTrue(first is not None and first["ok"])
        self.assertIsNotNone(second)
        assert second is not None
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(self.runtime.outbox.all), 2)
        self.assertEqual(self.adapter.counters["duplicates"], 1)

    def test_turn_failure_is_contained(self) -> None:
        runtime = FakeRuntime(error=RuntimeError("boom"))
        adapter = QQBotAdapter(
            runtime,
            FakeSectionConfig(
                {"qqbot": {"merge_window_seconds": 0, "fallback_enabled": False}}
            ),
        )
        adapter.client = FakeQQBotClient()
        with self.assertLogs("fatfish.platforms.qqbot", level="ERROR"):
            result = adapter.handle_event(c2c_event())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])
        self.assertEqual(adapter.counters["turn_errors"], 1)
        self.assertEqual(runtime.outbox.all, [])

    def test_turn_failure_sends_in_character_fallback(self) -> None:
        runtime = FakeRuntime(error=RuntimeError("boom"))
        adapter = QQBotAdapter(
            runtime,
            FakeSectionConfig(
                {"qqbot": {"merge_window_seconds": 0, "fallback_enabled": True}}
            ),
        )
        adapter.client = FakeQQBotClient()
        with self.assertLogs("fatfish.platforms.qqbot", level="ERROR"):
            result = adapter.handle_event(c2c_event())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["ok"])
        self.assertTrue(result["fallback"])
        self.assertEqual(len(runtime.outbox.all), 1)
        fallback = runtime.outbox.all[0]["payload"]["text"]
        self.assertIn("走神", fallback)
        self.assertNotIn("错误", fallback)
        self.assertNotIn("失败", fallback)
        self.assertNotIn("接口", fallback)
        self.assertEqual(adapter.counters["fallbacks"], 1)

    def test_tts_audio_is_sent_as_voice_media(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(b"RIFF-fake-wav")
            audio_path = handle.name
        try:
            self.runtime.extra_result = {
                "tts": {"ok": True, "audio_path": audio_path}
            }
            result = self.adapter.handle_event(c2c_event())
            assert result is not None
            self.assertTrue(result["ok"])
            self.assertEqual(result["media_queued"], 1)
            self.assertEqual(result["media_sent"], 1)
            self.assertEqual(self.client.media[-1]["kind"], "voice")
            self.assertEqual(self.client.media[-1]["path"], audio_path)
        finally:
            Path(audio_path).unlink(missing_ok=True)

    def test_voice_upload_failure_falls_back_to_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(b"RIFF-fake-wav")
            audio_path = handle.name
        try:
            self.client.fail_voice = True
            self.runtime.extra_result = {
                "tts": {"ok": True, "audio_path": audio_path}
            }
            result = self.adapter.handle_event(c2c_event())
            assert result is not None
            self.assertTrue(result["ok"])
            self.assertEqual(self.client.media[-1]["kind"], "file")
            self.assertEqual(self.adapter.counters["media_send_failures"], 1)
        finally:
            Path(audio_path).unlink(missing_ok=True)

    def test_send_failure_is_reported_not_raised(self) -> None:
        class FailingClient(FakeQQBotClient):
            def send_text(self, scope, target_id, text, *, reply_to=None):
                raise RuntimeError("gateway down")

        self.adapter.client = FailingClient()
        result = self.adapter.send_outbox_item(
            {
                "kind": "qq_send",
                "id": 7,
                "payload": {
                    "target_type": "private",
                    "target_id": "U1",
                    "text": "hi",
                    "reply_to": None,
                },
            }
        )
        self.assertFalse(result["ok"])
        self.assertIn("gateway down", result["error"])
        self.assertEqual(self.adapter.counters["send_failures"], 1)

    def test_proactive_message_handler(self) -> None:
        result = self.adapter.send_outbox_item(
            {
                "kind": "proactive_message",
                "id": 1,
                "payload": {"session_id": "qq:private:USER-9", "text": "在忙吗"},
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.sent[-1], ("private", "USER-9", "在忙吗", None))
        skipped = self.adapter.send_outbox_item(
            {
                "kind": "proactive_message",
                "id": 2,
                "payload": {"session_id": "web:private:1", "text": "x"},
            }
        )
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["skipped"], "not_qq_session")

        invalid = self.adapter.send_outbox_item(
            {
                "kind": "proactive_message",
                "id": 3,
                "payload": {"session_id": "qq:private:", "text": "x"},
            }
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"], "invalid_qq_session_id")

    def test_empty_message_is_rejected(self) -> None:
        result = self.adapter.send_outbox_item(
            {
                "kind": "qq_send",
                "id": 4,
                "payload": {"target_type": "private", "target_id": "U1", "text": "  "},
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "empty_message")

    def test_status_shape_and_no_secret_leak(self) -> None:
        env = {"QQBOT_APPID": "APPID-SECRETISH", "QQBOT_SECRET": "TOPSECRET"}
        with mock.patch.dict(os.environ, env, clear=False):
            status = self.adapter.status()
        self.assertEqual(status["platform"], "qqbot")
        self.assertTrue(status["enabled"] is False)
        self.assertIn("connected", status)
        self.assertIn("last_event_at", status)
        self.assertIn("counters", status)
        dumped = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("TOPSECRET", dumped)
        self.assertNotIn("APPID-SECRETISH", dumped)

    def test_reconnect_reuses_client_to_keep_resume_state(self) -> None:
        created: list[FakeQQBotClient] = []

        class ReusableClient(FakeQQBotClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.session_id = None
                created.append(self)

            def connect(self):
                self.connected = True
                if self.session_id is None:
                    self.session_id = "SESSION-1"
                return self

        adapter = QQBotAdapter(
            FakeRuntime(),
            FakeSectionConfig({"qqbot": {"enabled": True}}),
            client_factory=ReusableClient,
        )
        with mock.patch.object(qqbot_module, "websocket", object()), mock.patch(
            "builtins.print"
        ):
            adapter._connect()
            first = adapter.client
            assert first is not None
            first.last_seq = 9
            adapter._disconnect()
            self.assertIs(adapter.client, first)
            self.assertFalse(first.connected)
            adapter._connect()
        self.assertIs(adapter.client, first)
        self.assertEqual(len(created), 1)
        self.assertEqual(adapter.client.session_id, "SESSION-1")
        self.assertEqual(adapter.client.last_seq, 9)

    def test_run_forever_requires_websocket_client(self) -> None:
        with mock.patch.object(qqbot_module, "websocket", None):
            self.assertFalse(qqbot_module.websocket_available())
            with self.assertRaises(RuntimeError):
                self.adapter.run_forever()

    def test_run_forever_reconnects_and_stops(self) -> None:
        stop = __import__("threading").Event()
        script = []

        class LoopClient(FakeQQBotClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.recv_calls = 0

            def connect(self):
                self.connected = True
                script.append("connect")
                return self

            def recv(self, timeout=1.0):
                self.recv_calls += 1
                if self.recv_calls >= 2:
                    stop.set()
                return None

            def close(self):
                self.connected = False
                script.append("close")

        adapter = QQBotAdapter(
            FakeRuntime(),
            FakeSectionConfig({"qqbot": {"enabled": True}}),
            client_factory=LoopClient,
        )
        # The loop guard only needs the dependency to look installed; the fake
        # client owns the "socket" so nothing touches the network.
        with mock.patch.object(qqbot_module, "websocket", object()), mock.patch(
            "builtins.print"
        ):
            adapter.run_forever(stop)
        self.assertGreaterEqual(script.count("connect"), 1)
        self.assertIn("close", script)
        self.assertIsNotNone(adapter.connected_at)


class QQBotPhaseATests(unittest.TestCase):
    """Phase A context quality: quotes, media, merge window, hidden style hint."""

    def _config(self, **overrides) -> FakeSectionConfig:
        section = {"group_enabled": True, "merge_window_seconds": 0}
        section.update(overrides)
        return FakeSectionConfig({"qqbot": section})

    def test_quote_is_resolved_from_local_ref_cache(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        adapter.handle_event(event_with_msg_idx(msg_idx="Q100", text="被引用的原话"))
        adapter.handle_event(quote_ref_event(ref="Q100"))
        joined = "\n".join(runtime.extras[-1]["extra_evidence"] or [])
        self.assertIn("被引用的原话", joined)
        self.assertEqual(adapter.counters["quotes_resolved"], 1)

    def test_quote_is_resolved_from_msg_elements_fallback(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        adapter.handle_event(quote_ref_event(ref="NOT-IN-CACHE"))
        joined = "\n".join(runtime.extras[-1]["extra_evidence"] or [])
        self.assertIn("被引用的原话", joined)

    def test_missing_quote_uses_soft_in_character_hint(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        event = c2c_event(text="你看这个", msg_id="EVENT-Q3")
        event["d"]["message_scene"] = {"ext": ["ref_msg_idx=UNKNOWN"]}
        event["d"]["message_type"] = 103
        adapter.handle_event(event)
        joined = "\n".join(runtime.extras[-1]["extra_evidence"] or [])
        self.assertIn("引用", joined)
        self.assertIn("撒娇", joined)
        self.assertNotIn("原文不可见", joined)
        self.assertEqual(adapter.counters["quotes_missed"], 1)

    def test_image_download_voice_asr_and_file_note(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(
            runtime,
            self._config(),
            media_downloader=lambda url, **kwargs: b"png-bytes",
        )
        adapter.handle_event(media_event())
        self.assertEqual(len(runtime.calls), 1)
        call = runtime.calls[0]
        self.assertIn("我给你发语音啦", call["text"])
        self.assertIn("合同.pdf", call["text"])
        images = runtime.extras[0]["images"] or []
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))
        joined = "\n".join(runtime.extras[0]["extra_evidence"] or [])
        self.assertIn("撒娇", joined)
        self.assertEqual(adapter.counters["media_downloaded"], 1)

    def test_image_only_message_uses_bare_placeholder(self) -> None:
        """无文字图片的占位符是 `[图片]`，不是「（发来一张图）」（2026-11-22）。

        旧占位语被 runtime 的 wants_photo 旁路当成「发…图」拍照指令：
        发任何一张图都机械回「好，我去拍」。占位符只描述媒体存在。
        """
        runtime = FakeRuntime()
        adapter = QQBotAdapter(
            runtime,
            self._config(),
            media_downloader=lambda url, **kwargs: b"png-bytes",
        )
        event = c2c_event(text="", msg_id="IMG-ONLY-1")
        event["d"]["attachments"] = [
            {"content_type": "image/png", "url": "https://example.invalid/a.png"},
        ]
        adapter.handle_event(event)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(runtime.calls[0]["text"], "[图片]")
        images = runtime.extras[0]["images"] or []
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))

    def test_image_only_multiple_images_get_counted_placeholder(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(
            runtime,
            self._config(),
            media_downloader=lambda url, **kwargs: b"png-bytes",
        )
        event = c2c_event(text="", msg_id="IMG-ONLY-2")
        event["d"]["attachments"] = [
            {"content_type": "image/png", "url": "https://example.invalid/a.png"},
            {"content_type": "image/png", "url": "https://example.invalid/b.png"},
        ]
        adapter.handle_event(event)
        self.assertEqual(runtime.calls[0]["text"], "[图片]×2")

    def test_captioned_image_keeps_owner_words_untouched(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(
            runtime,
            self._config(),
            media_downloader=lambda url, **kwargs: b"png-bytes",
        )
        event = c2c_event(text="看我家猫", msg_id="IMG-CAPTION-1")
        event["d"]["attachments"] = [
            {"content_type": "image/png", "url": "https://example.invalid/a.png"},
        ]
        adapter.handle_event(event)
        self.assertEqual(runtime.calls[0]["text"], "看我家猫")

    def test_merge_window_collapses_burst_into_one_turn(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config(merge_window_seconds=0.5))
        first = adapter.handle_event(c2c_event(text="第一句", msg_id="M1"))
        second = adapter.handle_event(c2c_event(text="第二句", msg_id="M2"))
        self.assertTrue(first and first.get("buffered"))
        self.assertTrue(second and second.get("buffered"))
        self.assertEqual(len(runtime.calls), 0)
        results = adapter.flush_due(now=time.monotonic() + 1.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(runtime.calls), 1)
        self.assertIn("第一句", runtime.calls[0]["text"])
        self.assertIn("第二句", runtime.calls[0]["text"])
        self.assertEqual(runtime.calls[0]["text"].count("\n"), 1)
        self.assertEqual(adapter.counters["merged"], 1)

    def test_quote_cache_is_bounded(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config(quote_cache_size=2))
        for index in range(5):
            adapter.handle_event(event_with_msg_idx(msg_idx=f"Q{index}"))
        self.assertLessEqual(len(adapter._refs), 2)

    def test_immersion_hint_never_names_channels_or_ids(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        adapter.handle_event(c2c_event())
        joined = "\n".join(runtime.extras[-1]["extra_evidence"] or [])
        self.assertIn("撒娇", joined)
        self.assertIn("不要照读", joined)
        self.assertNotIn("openid", joined)
        self.assertNotIn("官方 Bot", joined)

    def test_group_stage_hint_is_included(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        adapter.handle_event(group_at_event())
        joined = "\n".join(runtime.extras[-1]["extra_evidence"] or [])
        self.assertIn("群里没叫你", joined)

    def test_group_mentions_are_kept_as_soft_context(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        event = group_at_event()
        event["d"]["mentions"] = [
            {"nickname": "小明", "is_you": False},
            {"nickname": "鲸鱼娘", "is_you": True},
        ]
        adapter.handle_event(event)
        blocks = runtime.extras[-1]["extra_evidence"] or []
        mention_blocks = [block for block in blocks if "群里这条还提到了" in block]
        self.assertTrue(mention_blocks)
        self.assertIn("小明", mention_blocks[0])
        self.assertNotIn("鲸鱼娘", mention_blocks[0])

    def test_status_reports_phase_a_and_no_catchup(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(runtime, self._config())
        status = adapter.status()
        self.assertEqual(status["merge_window_seconds"], 0)
        self.assertTrue(status["immersion_hint"])
        self.assertFalse(status["catchup_supported"])
        self.assertIn("pending_batches", status)

    def test_text_only_hint_when_tts_disabled(self) -> None:
        runtime = FakeRuntime()
        adapter = QQBotAdapter(
            runtime,
            FakeSectionConfig(
                {"qqbot": {"merge_window_seconds": 0}, "tts": {"enabled": False}}
            ),
        )
        adapter.handle_event(c2c_event())
        joined = "\n".join(runtime.extras[-1]["extra_evidence"] or [])
        self.assertIn("只能用文字", joined)


# ------------------------------------------------------------------- memory tests
class QQBotMemoryTests(unittest.TestCase):
    def test_dedupe_deque_is_bounded(self) -> None:
        adapter = QQBotAdapter(FakeRuntime(), FakeSectionConfig())
        self.assertEqual(adapter._seen_events.maxlen, MAX_SEEN_EVENTS)
        for index in range(MAX_SEEN_EVENTS + 64):
            adapter._seen_events.append(f"event-{index}")
        self.assertEqual(len(adapter._seen_events), MAX_SEEN_EVENTS)
        self.assertNotIn("event-0", adapter._seen_events)
        self.assertIn(f"event-{MAX_SEEN_EVENTS + 63}", adapter._seen_events)

    def test_reply_counter_default_cap(self) -> None:
        self.assertEqual(MAX_REPLY_COUNTS, 512)
        client, _opener = make_client()
        self.assertEqual(client.reply_counts_limit, 512)


if __name__ == "__main__":
    unittest.main()
