"""T-0071: QQ 群历史 / 合并转发 / 群成员只读平台接口（全部离线，fake client）。

Covers the three layers added for read-only QQ history reads:

* :class:`fatfish.platforms.onebot.OneBotClient` — NapCat extension wrappers
  ``get_group_msg_history`` / ``get_forward_msg`` / ``get_group_member_list``
  (scripted fake WebSocket, no sockets).
* :func:`fatfish.platforms.history.extract_history_rows` — pure row
  formatting (self/empty skip, text-segments-only, truncation, row cap).
* :class:`fatfish.platforms.qq.QQAdapter` — fault-tolerant read-only
  ``fetch_*`` wrappers plus :func:`fatfish.platforms.qq.extract_forward_ref`.

Nothing here touches the network, a real database, or sends any message.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from fatfish.platforms.history import extract_history_rows
from fatfish.platforms.onebot import OneBotClient
from fatfish.platforms.qq import QQAdapter, extract_forward_ref

SELF_ID = 999
GROUP_ID = "778899"
FRIEND_UIN = "20001"


def group_row(
    text: str,
    ts: float,
    *,
    user_id: int = 20001,
    nickname: str = "阿鱼",
    card: str = "群友A",
    message_id: int = 1,
) -> dict[str, Any]:
    """A NapCat ``get_group_msg_history`` row (OneBot message-event shape)."""
    return {
        "time": ts,
        "self_id": SELF_ID,
        "user_id": user_id,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": user_id, "nickname": nickname, "card": card},
    }


def forward_event(resid: str = "ResId-123", *, data_style: str = "string") -> dict[str, Any]:
    """A group message carrying one modern merged-forward json segment."""
    ark: dict[str, Any] = {
        "app": "com.tencent.multimsg",
        "config": {"forward": 1, "autosize": 1, "round": 1, "type": "normal", "width": 300},
        "desc": "聊天记录",
        "extra": {"filename": "uniseq-1", "tsum": 3},
        "meta": {
            "detail": {
                "news": [{"text": "阿鱼: 你们看这个"}],
                "resid": resid,
                "source": "群聊的聊天记录",
                "summary": "查看3条转发消息",
                "uniseq": "uniseq-1",
            }
        },
        "news": [{"text": "阿鱼: 你们看这个"}],
        "prompt": "[聊天记录]",
        "ver": "0.0.0.99",
        "view": "contact",
    }
    if data_style == "string":
        segment_data: Any = {"data": json.dumps(ark, ensure_ascii=False)}
    else:  # some implementations hand over the parsed dict directly
        segment_data = ark
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(GROUP_ID),
        "user_id": 20001,
        "message": [{"type": "json", "data": segment_data}],
        "raw_message": "[CQ:json,...]",
    }


# ------------------------------------------------------- extract_history_rows
class ExtractHistoryRowsTests(unittest.TestCase):
    def test_parses_rows_to_plain_dicts(self) -> None:
        rows = [
            group_row("早上好", 1730000000, user_id=20001, card="群友A"),
            group_row("早呀", 1730000060, user_id=20002, card="群友B"),
        ]
        result = extract_history_rows(rows, SELF_ID)
        self.assertEqual(
            result,
            [
                {
                    "sender_id": "20001",
                    "sender_name": "群友A",
                    "text": "早上好",
                    "time": 1730000000.0,
                },
                {
                    "sender_id": "20002",
                    "sender_name": "群友B",
                    "text": "早呀",
                    "time": 1730000060.0,
                },
            ],
        )

    def test_skips_own_messages(self) -> None:
        rows = [
            group_row("我自己说的", 1.0, user_id=SELF_ID),
            group_row("别人的话", 2.0, user_id=20001),
        ]
        result = extract_history_rows(rows, SELF_ID)
        self.assertEqual([row["text"] for row in result], ["别人的话"])

    def test_self_id_none_keeps_everything(self) -> None:
        rows = [group_row("我自己说的", 1.0, user_id=SELF_ID)]
        result = extract_history_rows(rows, None)
        self.assertEqual(len(result), 1)

    def test_skips_empty_and_media_only_rows(self) -> None:
        media_row = {
            "time": 3.0,
            "user_id": 20003,
            "message": [
                {"type": "image", "data": {"url": "https://example.com/x.jpg"}},
                {"type": "face", "data": {"id": "1"}},
                {"type": "record", "data": {}},
                {"type": "json", "data": {"data": "{}"}},
                {"type": "xml", "data": {"data": "<x/>"}},
            ],
            "sender": {"user_id": 20003, "nickname": "C", "card": ""},
        }
        rows = [
            group_row("   ", 1.0, user_id=20001),
            media_row,
            {"not-even-a-dict"},
            group_row("真消息", 2.0, user_id=20002),
        ]
        result = extract_history_rows(rows, SELF_ID)
        self.assertEqual([row["text"] for row in result], ["真消息"])
        self.assertEqual(result[0]["sender_id"], "20002")

    def test_only_text_segments_are_kept(self) -> None:
        row = group_row("", 5.0, user_id=20004)
        row["message"] = [
            {"type": "text", "data": {"text": "看这张 "}},
            {"type": "image", "data": {"url": "https://example.com/y.png"}},
            {"type": "text", "data": {"text": "图"}},
            {"type": "video", "data": {"url": "https://example.com/v.mp4"}},
        ]
        result = extract_history_rows([row], SELF_ID)
        self.assertEqual(result[0]["text"], "看这张 图")

    def test_truncates_each_row(self) -> None:
        long_text = "啊" * 300
        result = extract_history_rows([group_row(long_text, 1.0)], SELF_ID, max_chars=200)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["text"]), 200)
        self.assertTrue(result[0]["text"].endswith("…"))
        self.assertTrue(result[0]["text"].startswith("啊"))

    def test_row_cap_applies_after_filtering(self) -> None:
        rows = [
            group_row("第一条", 1.0, user_id=20001),
            group_row("自己", 2.0, user_id=SELF_ID),
            group_row("第二条", 3.0, user_id=20002),
            group_row("第三条", 4.0, user_id=20003),
        ]
        result = extract_history_rows(rows, SELF_ID, max_rows=2)
        self.assertEqual([row["text"] for row in result], ["第一条", "第二条"])

    def test_zero_row_cap_is_empty(self) -> None:
        result = extract_history_rows([group_row("x", 1.0)], SELF_ID, max_rows=0)
        self.assertEqual(result, [])

    def test_sender_name_prefers_card_then_nickname(self) -> None:
        row = group_row("hi", 1.0, user_id=20001, nickname="网名", card="")
        result = extract_history_rows([row], SELF_ID)
        self.assertEqual(result[0]["sender_name"], "网名")

    def test_row_without_sender_block_still_parses(self) -> None:
        row = {
            "time": 7.0,
            "user_id": 20005,
            "message": [{"type": "text", "data": {"text": "没有sender块"}}],
        }
        result = extract_history_rows([row], SELF_ID)
        self.assertEqual(result[0]["sender_id"], "20005")
        self.assertEqual(result[0]["sender_name"], "")
        self.assertEqual(result[0]["time"], 7.0)

    def test_non_segment_message_row_is_skipped(self) -> None:
        row = {"time": 1.0, "user_id": 20001, "message": "[CQ:image,file=abc]"}
        self.assertEqual(extract_history_rows([row], SELF_ID), [])

    def test_none_and_garbage_input_is_empty(self) -> None:
        self.assertEqual(extract_history_rows(None, SELF_ID), [])
        self.assertEqual(extract_history_rows([None, 1, "x"], SELF_ID), [])


# -------------------------------------------------------- extract_forward_ref
class ExtractForwardRefTests(unittest.TestCase):
    def test_modern_meta_detail_resid(self) -> None:
        self.assertEqual(extract_forward_ref(forward_event("ResId-123")), "ResId-123")

    def test_parsed_dict_segment(self) -> None:
        event = forward_event("ResId-456", data_style="dict")
        self.assertEqual(extract_forward_ref(event), "ResId-456")

    def test_legacy_top_level_resid(self) -> None:
        ark = {
            "app": "com.tencent.multimsg",
            "resid": "legacy-resid",
            "prompt": "[聊天记录]",
        }
        event = {
            "message": [{"type": "json", "data": {"data": json.dumps(ark)}}],
        }
        self.assertEqual(extract_forward_ref(event), "legacy-resid")

    def test_non_multimsg_json_is_ignored(self) -> None:
        ark = {"app": "com.tencent.miniapp", "meta": {"detail": {"resid": "x"}}}
        event = {"message": [{"type": "json", "data": {"data": json.dumps(ark)}}]}
        self.assertIsNone(extract_forward_ref(event))

    def test_multimsg_without_resid_is_none(self) -> None:
        ark = {"app": "com.tencent.multimsg", "meta": {"detail": {}}}
        event = {"message": [{"type": "json", "data": {"data": json.dumps(ark)}}]}
        self.assertIsNone(extract_forward_ref(event))

    def test_broken_json_is_none(self) -> None:
        event = {"message": [{"type": "json", "data": {"data": "{not json"}}]}
        self.assertIsNone(extract_forward_ref(event))

    def test_no_json_segment_is_none(self) -> None:
        event = {"message": [{"type": "text", "data": {"text": "hello"}}]}
        self.assertIsNone(extract_forward_ref(event))

    def test_string_message_is_none(self) -> None:
        self.assertIsNone(extract_forward_ref({"message": "[CQ:json,data=...]"}))

    def test_garbage_events_are_none(self) -> None:
        self.assertIsNone(extract_forward_ref(None))
        self.assertIsNone(extract_forward_ref({"no": "message"}))
        self.assertIsNone(extract_forward_ref({"message": [None, 42]}))


# ------------------------------------------- OneBotClient extension wrappers
class _FakeWS:
    """Scripted forward-WebSocket for ``call_action`` tests (no sockets)."""

    def __init__(self, script: list):
        self.script = list(script)
        self.sent: list[dict] = []
        self.connected = True
        self.last_echo = ""

    def send(self, raw):
        payload = json.loads(raw)
        self.sent.append(payload)
        self.last_echo = str(payload.get("echo") or "")

    def settimeout(self, value):
        pass

    def recv(self):
        if not self.script:
            raise TimeoutError("no more data")
        item = self.script.pop(0)
        if isinstance(item, dict) and "post_type" not in item:
            item = {**item, "echo": self.last_echo}
        return json.dumps(item, ensure_ascii=False)

    def close(self):
        self.connected = False


class ClientExtensionTests(unittest.TestCase):
    def _client(self, script: list) -> OneBotClient:
        client = OneBotClient("ws://127.0.0.1:1")
        client._ws = _FakeWS(script)
        return client

    # ------------------------------------------------- get_group_msg_history
    def test_group_history_sends_napcat_payload(self) -> None:
        row = group_row("群消息", 1730000000)
        client = self._client(
            [{"status": "ok", "retcode": 0, "data": {"messages": [row]}}]
        )
        rows = client.get_group_msg_history(GROUP_ID, count=30, message_seq=55)
        self.assertEqual(rows, [row])
        sent = client._ws.sent[0]
        self.assertEqual(sent["action"], "get_group_msg_history")
        self.assertEqual(
            sent["params"],
            {
                "group_id": 778899,
                "message_seq": "55",
                "count": 30,
                "reverse_order": False,
            },
        )

    def test_group_history_seq_zero_reads_newest_page(self) -> None:
        client = self._client([{"status": "ok", "retcode": 0, "data": {"messages": []}}])
        rows = client.get_group_msg_history(GROUP_ID)
        self.assertEqual(rows, [])
        self.assertEqual(client._ws.sent[0]["params"]["message_seq"], "0")

    def test_group_history_reverse_order_flag(self) -> None:
        client = self._client([{"status": "ok", "retcode": 0, "data": {"messages": []}}])
        client.get_group_msg_history(GROUP_ID, reverse_order=True)
        self.assertTrue(client._ws.sent[0]["params"]["reverse_order"])

    def test_group_history_re_dispatches_pushed_events(self) -> None:
        push = {"post_type": "message", "message_type": "group", "user_id": 7}
        response = {"status": "ok", "retcode": 0, "data": {"messages": []}}
        client = self._client([push, response])
        seen: list[dict] = []
        client.get_group_msg_history(GROUP_ID, on_event=seen.append)
        self.assertEqual(seen, [push])

    def test_group_history_failed_status_raises(self) -> None:
        client = self._client(
            [{"status": "failed", "retcode": 1200, "wording": "消息0不存在"}]
        )
        with self.assertRaises(RuntimeError):
            client.get_group_msg_history(GROUP_ID)

    # -------------------------------------------------------- get_forward_msg
    def test_forward_msg_sends_resid_and_returns_messages(self) -> None:
        node = {
            "type": "node",
            "data": {
                "user_id": 20001,
                "nickname": "阿鱼",
                "message": [{"type": "text", "data": {"text": "转发里的话"}}],
            },
        }
        client = self._client(
            [{"status": "ok", "retcode": 0, "data": {"messages": [node]}}]
        )
        rows = client.get_forward_msg("ResId-123")
        self.assertEqual(rows, [node])
        sent = client._ws.sent[0]
        self.assertEqual(sent["action"], "get_forward_msg")
        self.assertEqual(sent["params"], {"message_id": "ResId-123"})

    def test_forward_msg_accepts_numeric_message_id(self) -> None:
        client = self._client([{"status": "ok", "retcode": 0, "data": {"messages": []}}])
        client.get_forward_msg(12345)
        self.assertEqual(client._ws.sent[0]["params"], {"message_id": 12345})

    def test_forward_msg_re_dispatches_pushed_events(self) -> None:
        push = {"post_type": "message", "message_type": "group", "user_id": 7}
        response = {"status": "ok", "retcode": 0, "data": {"messages": []}}
        client = self._client([push, response])
        seen: list[dict] = []
        client.get_forward_msg("ResId-123", on_event=seen.append)
        self.assertEqual(seen, [push])

    def test_forward_msg_failed_status_raises(self) -> None:
        client = self._client(
            [{"status": "failed", "retcode": 1200, "wording": "ResId无效"}]
        )
        with self.assertRaises(RuntimeError):
            client.get_forward_msg("ResId-123")

    def test_forward_msg_missing_messages_is_empty(self) -> None:
        client = self._client([{"status": "ok", "retcode": 0, "data": {}}])
        self.assertEqual(client.get_forward_msg("ResId-123"), [])

    # ---------------------------------------------------- get_group_member_list
    def test_member_list_normalizes_rows(self) -> None:
        client = self._client(
            [
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {
                            "group_id": 778899,
                            "user_id": 20001,
                            "nickname": "阿鱼",
                            "card": "群友A",
                            "role": "member",
                        },
                        {"user_id": 20002, "nickname": "B", "card": "群友B"},
                        "not-a-dict",
                    ],
                }
            ]
        )
        members = client.get_group_member_list(GROUP_ID)
        self.assertEqual(
            members,
            [
                {"user_id": 20001, "nickname": "阿鱼", "card": "群友A"},
                {"user_id": 20002, "nickname": "B", "card": "群友B"},
            ],
        )
        sent = client._ws.sent[0]
        self.assertEqual(sent["action"], "get_group_member_list")
        self.assertEqual(sent["params"], {"group_id": 778899})

    def test_member_list_missing_fields_default_empty(self) -> None:
        client = self._client([{"status": "ok", "retcode": 0, "data": [{}]}])
        self.assertEqual(client.get_group_member_list(GROUP_ID),
                         [{"user_id": "", "nickname": "", "card": ""}])

    def test_member_list_non_list_data_is_empty(self) -> None:
        client = self._client([{"status": "ok", "retcode": 0, "data": {}}])
        self.assertEqual(client.get_group_member_list(GROUP_ID), [])

    def test_member_list_failed_status_raises(self) -> None:
        client = self._client([{"status": "failed", "retcode": 500, "wording": "boom"}])
        with self.assertRaises(RuntimeError):
            client.get_group_member_list(GROUP_ID)


# ------------------------------------------------- QQAdapter fetch_* wrappers
class _FakeConfig:
    """Minimal config: only ``section("qq")`` matters to the adapter."""

    def __init__(self, section: dict[str, Any] | None = None):
        self._section = dict(section or {})

    def section(self, name: str) -> dict[str, Any]:
        if name == "qq":
            return dict(self._section)
        return {}


class _ExplodeFactory:
    """A client factory that always fails (no sockets are ever touched)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cannot connect to OneBot at ws://127.0.0.1:1: boom")


class _RecordingClient:
    """Fake client: scripted read results + send methods that must stay cold."""

    connected = True

    def __init__(
        self,
        *,
        rows: dict[str, list] | None = None,
        error: Exception | None = None,
        returns: Any = None,
    ):
        self.rows = dict(rows or {})
        self.error = error
        self.returns = returns
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, str, str]] = []

    def _record(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        if self.returns is not None:
            return self.returns
        return list(self.rows.get(name, []))

    def get_group_msg_history(
        self, group_id, count=20, message_seq=0, *, reverse_order=False, on_event=None
    ):
        return self._record(
            "get_group_msg_history",
            group_id=group_id,
            count=count,
            message_seq=message_seq,
            reverse_order=reverse_order,
            on_event=on_event,
        )

    def get_forward_msg(self, message_id, *, on_event=None):
        return self._record("get_forward_msg", message_id=message_id, on_event=on_event)

    def get_friend_msg_history(self, user_id, count=20, *, message_seq=0, on_event=None):
        return self._record(
            "get_friend_msg_history",
            user_id=user_id,
            count=count,
            message_seq=message_seq,
            on_event=on_event,
        )

    def get_group_member_list(self, group_id):
        return self._record("get_group_member_list", group_id=group_id)

    # Cold-path guards: the fetch wrappers must never call these.
    def send_private_text(self, user_id, text):
        self.sent.append(("private", str(user_id), str(text)))
        return "echo-private"

    def send_group_text(self, group_id, text):
        self.sent.append(("group", str(group_id), str(text)))
        return "echo-group"


class AdapterFetchTests(unittest.TestCase):
    def _adapter(self, client: Any = None) -> QQAdapter:
        config = _FakeConfig(
            {"enabled": True, "self_id": SELF_ID, "catchup_enabled": False}
        )
        adapter = QQAdapter(object(), config)
        if client is not None:
            adapter.client = client
        return adapter

    def test_fetch_group_history_passes_args_through(self) -> None:
        rows = [group_row("群消息", 1730000000)]
        client = _RecordingClient(rows={"get_group_msg_history": rows})
        adapter = self._adapter(client)

        seen: list[dict] = []
        result = adapter.fetch_group_history(
            GROUP_ID, count=5, message_seq=42, reverse_order=True, on_event=seen.append
        )

        self.assertEqual(result, rows)
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_group_msg_history")
        self.assertEqual(kwargs["group_id"], GROUP_ID)
        self.assertEqual(kwargs["count"], 5)
        self.assertEqual(kwargs["message_seq"], 42)
        self.assertTrue(kwargs["reverse_order"])
        self.assertEqual(kwargs["on_event"], seen.append)
        self.assertEqual(client.sent, [])

    def test_fetch_forward_messages_passes_message_id(self) -> None:
        rows = [{"type": "node", "data": {}}]
        client = _RecordingClient(rows={"get_forward_msg": rows})
        adapter = self._adapter(client)

        seen: list[dict] = []
        result = adapter.fetch_forward_messages("ResId-123", on_event=seen.append)

        self.assertEqual(result, rows)
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_forward_msg")
        self.assertEqual(kwargs["message_id"], "ResId-123")
        self.assertEqual(kwargs["on_event"], seen.append)
        self.assertEqual(client.sent, [])

    def test_fetch_friend_history_passes_through(self) -> None:
        rows = [group_row("私聊消息", 1730000000, user_id=int(FRIEND_UIN))]
        client = _RecordingClient(rows={"get_friend_msg_history": rows})
        adapter = self._adapter(client)

        result = adapter.fetch_friend_history(FRIEND_UIN, count=7, message_seq=9)

        self.assertEqual(result, rows)
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_friend_msg_history")
        self.assertEqual(kwargs["user_id"], FRIEND_UIN)
        self.assertEqual(kwargs["count"], 7)
        self.assertEqual(kwargs["message_seq"], 9)
        self.assertEqual(client.sent, [])

    def test_fetch_group_members_passes_through(self) -> None:
        members = [{"user_id": 20001, "nickname": "阿鱼", "card": "群友A"}]
        client = _RecordingClient(rows={"get_group_member_list": members})
        adapter = self._adapter(client)

        result = adapter.fetch_group_members(GROUP_ID)

        self.assertEqual(result, members)
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_group_member_list")
        self.assertEqual(kwargs["group_id"], GROUP_ID)
        self.assertEqual(client.sent, [])

    def test_fetch_tolerates_client_error(self) -> None:
        client = _RecordingClient(error=RuntimeError("boom"))
        adapter = self._adapter(client)
        self.assertEqual(adapter.fetch_group_history(GROUP_ID), [])
        self.assertEqual(adapter.fetch_forward_messages("ResId-123"), [])
        self.assertEqual(adapter.fetch_friend_history(FRIEND_UIN), [])
        self.assertEqual(adapter.fetch_group_members(GROUP_ID), [])
        self.assertEqual(client.sent, [])

    def test_fetch_tolerates_non_list_results(self) -> None:
        client = _RecordingClient(returns="not-a-list")
        adapter = self._adapter(client)
        self.assertEqual(adapter.fetch_group_history(GROUP_ID), [])
        self.assertEqual(adapter.fetch_forward_messages("ResId-123"), [])
        self.assertEqual(adapter.fetch_friend_history(FRIEND_UIN), [])
        self.assertEqual(adapter.fetch_group_members(GROUP_ID), [])
        self.assertEqual(client.sent, [])

    def test_fetch_filters_non_dict_rows(self) -> None:
        client = _RecordingClient(
            rows={"get_group_msg_history": [group_row("ok", 1.0), "junk", None]}
        )
        adapter = self._adapter(client)
        result = adapter.fetch_group_history(GROUP_ID)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message"], [{"type": "text", "data": {"text": "ok"}}])
        self.assertEqual(client.sent, [])

    def test_fetch_without_client_support_is_empty(self) -> None:
        adapter = self._adapter(object())  # a client with none of the getters
        self.assertEqual(adapter.fetch_group_history(GROUP_ID), [])
        self.assertEqual(adapter.fetch_forward_messages("ResId-123"), [])
        self.assertEqual(adapter.fetch_friend_history(FRIEND_UIN), [])
        self.assertEqual(adapter.fetch_group_members(GROUP_ID), [])

    def test_fetch_when_connection_fails_is_empty(self) -> None:
        adapter = self._adapter(None)
        adapter.client = None
        adapter.client_factory = _ExplodeFactory()
        self.assertEqual(adapter.fetch_group_history(GROUP_ID), [])
        self.assertEqual(adapter.fetch_forward_messages("ResId-123"), [])
        self.assertEqual(adapter.fetch_friend_history(FRIEND_UIN), [])
        self.assertEqual(adapter.fetch_group_members(GROUP_ID), [])

    def test_fetch_end_to_end_with_extract_helpers(self) -> None:
        """The T-0072 shape: fetch rows, then format them for the prompt."""
        rows = [
            group_row("谁在说话", 1730000000, user_id=20001, card="群友A"),
            group_row("是我", 1730000030, user_id=SELF_ID),
        ]
        client = _RecordingClient(rows={"get_group_msg_history": rows})
        adapter = self._adapter(client)

        history = adapter.fetch_group_history(GROUP_ID)
        self.assertEqual(
            extract_history_rows(history, SELF_ID),
            [
                {
                    "sender_id": "20001",
                    "sender_name": "群友A",
                    "text": "谁在说话",
                    "time": 1730000000.0,
                }
            ],
        )

        event = forward_event("ResId-123")
        resid = extract_forward_ref(event)
        self.assertEqual(resid, "ResId-123")
        forward_client = _RecordingClient(
            rows={"get_forward_msg": [{"type": "node", "data": {}}]}
        )
        adapter.client = forward_client
        self.assertEqual(len(adapter.fetch_forward_messages(resid or "")), 1)
        self.assertEqual(client.sent, [])
        self.assertEqual(forward_client.sent, [])


if __name__ == "__main__":
    unittest.main()
