#!/usr/bin/env python
"""QQ Bot 官方协议连通性探针（真机联调用，非产品路径）。

只读环境变量里的凭据，不读写 ``data/``，不改配置：

    set QQBOT_APPID=...
    set QQBOT_SECRET=...
    python scripts/qqbot_probe.py                 # 连网关，打印收到的消息
    python scripts/qqbot_probe.py --echo          # 被动回复「收到：<原文>」
    python scripts/qqbot_probe.py --send-text "hi" --openid <openid>
    python scripts/qqbot_probe.py --send-text "hi" --group-openid <group_openid>

可选环境变量：``QQBOT_TOKEN_BASE_URL`` / ``QQBOT_API_BASE_URL`` /
``QQBOT_INTENTS``（默认均为代码内默认值，见
:mod:`fatfish.platforms.qqbot`）。

``--send-text`` 走 HTTP 主动消息（不带 ``msg_id``），只验证 token / 发消息
接口是否通；``--echo`` / 默认监听走 WebSocket 网关。收到一条消息就打印一行，
``Ctrl+C`` 退出。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.platforms.qqbot import (  # noqa: E402
    QQBotClient,
    make_client,
    mask_secret,
    websocket_available,
)

MAX_PRINT_CHARS = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqbot_probe.py",
        description="官方 QQ Bot 网关/发消息连通性探针（只读 env 凭据）。",
    )
    parser.add_argument(
        "--echo",
        action="store_true",
        help="收到消息后用被动回复发「收到：<原文>」",
    )
    parser.add_argument(
        "--send-text",
        default=None,
        metavar="TEXT",
        help="主动发一条消息做连通性测试（需要 --openid 或 --group-openid）",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--openid", default=None, help="C2C 用户 openid（主动发送目标）")
    target.add_argument(
        "--group-openid", default=None, help="群 openid（主动发送目标）"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="监听时长（秒）；0 = 一直听到 Ctrl+C",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="额外打印原始事件 JSON（凭据不会出现）",
    )
    return parser


def _short(value: object, limit: int = MAX_PRINT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _inbound_target(event: dict) -> tuple[str, str] | None:
    """(scope, target_id) to answer one inbound message, or None."""
    event_type = str(event.get("t") or "")
    data = event.get("d")
    if not isinstance(data, dict):
        return None
    if event_type == "C2C_MESSAGE_CREATE":
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        target = str(author.get("user_openid") or data.get("user_openid") or "")
        return ("c2c", target) if target else None
    if event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
        target = str(data.get("group_openid") or "")
        return ("group", target) if target else None
    return None


def _describe(event: dict) -> str:
    event_type = str(event.get("t") or "")
    data = event.get("d")
    if event_type in {"READY", "RESUMED"}:
        session_id = ""
        if isinstance(data, dict):
            session_id = str(data.get("session_id") or "")
        return f"{event_type} session={session_id or '-'}"
    if not isinstance(data, dict):
        return f"{event_type} (no data)"
    target = _inbound_target(event)
    where = f"{target[0]}:{target[1]}" if target else "-"
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    who = str(author.get("username") or author.get("member_openid") or "-")
    return (
        f"{event_type} id={data.get('id') or '-'} from={who} to={where} "
        f"text={_short(data.get('content'))!r}"
    )


def _send_proactive(client: QQBotClient, args: argparse.Namespace) -> int:
    if not args.send_text:
        print("error: --send-text needs --openid or --group-openid", file=sys.stderr)
        return 2
    scope = "group" if args.group_openid else "c2c"
    target_id = str(args.group_openid or args.openid or "")
    if not target_id:
        print("error: --send-text needs --openid or --group-openid", file=sys.stderr)
        return 2
    try:
        summary = client.send_proactive(scope, target_id, args.send_text)
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        print(
            json.dumps(
                {"ok": False, "scope": scope, "target_id": target_id, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "scope": scope,
                "target_id": target_id,
                "chunks": summary.get("chunks"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_loop(client: QQBotClient, args: argparse.Namespace) -> int:
    deadline = None if args.seconds <= 0 else time.monotonic() + args.seconds
    received = 0
    echoed = 0
    print(
        f"[probe] app_id={mask_secret(client.app_id)} "
        f"token_base={client.token_base_url} api_base={client.api_base_url} "
        f"intents={client.intents}",
        flush=True,
    )
    client.connect()
    print(
        f"[probe] connected: session={client.session_id or '-'} "
        f"heartbeat={client.heartbeat_interval:.1f}s",
        flush=True,
    )
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            event = client.recv(timeout=1.0)
            if not event:
                continue
            received += 1
            print(f"[event] {_describe(event)}", flush=True)
            if args.verbose:
                print(
                    "[raw] "
                    + json.dumps(event, ensure_ascii=False, default=str)[:1200],
                    flush=True,
                )
            if not args.echo:
                continue
            target = _inbound_target(event)
            data = event.get("d") if isinstance(event.get("d"), dict) else {}
            text = str(data.get("content") or "").strip()
            if target is None or not text:
                continue
            scope, target_id = target
            try:
                summary = client.send_text(
                    scope,
                    target_id,
                    f"收到：{text}",
                    reply_to=str(data.get("id") or "") or None,
                )
            except Exception as exc:  # noqa: BLE001 - a failed echo is not fatal
                print(f"[echo] failed: {type(exc).__name__}: {exc}", flush=True)
                continue
            echoed += 1
            print(
                f"[echo] sent chunks={summary.get('chunks')} "
                f"passive={summary.get('passive')} proactive={summary.get('proactive')}",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\n[probe] interrupted", flush=True)
    finally:
        client.close()
    print(f"[probe] done: received={received} echoed={echoed}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not websocket_available() and not args.send_text:
        print(
            "error: websocket-client is required for the gateway probe. "
            "Install it with: pip install websocket-client",
            file=sys.stderr,
        )
        return 2
    client = make_client()
    if not client.credentials_set:
        print(
            f"error: set QQBOT_APPID / QQBOT_SECRET (got app_id={mask_secret(client.app_id) or 'missing'})",
            file=sys.stderr,
        )
        return 2
    if args.send_text:
        return _send_proactive(client, args)
    try:
        return _run_loop(client, args)
    except Exception as exc:  # noqa: BLE001 - probe boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
