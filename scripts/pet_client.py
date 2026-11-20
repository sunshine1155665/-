#!/usr/bin/env python3
"""Tiny local test client for the home pet gateway.

This is the stand-in for the future desktop pet shell: it connects to
``ws://127.0.0.1:9950/local``, sends one chat line, and prints state/bubble
frames coming back.  It never talks to QQ or the LLM directly.

    python scripts/pet_client.py --text "在吗？"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]


def load_token(explicit: str, env_name: str) -> str:
    if explicit:
        return explicit.strip()
    value = str(os.environ.get(env_name, "") or "").strip()
    if value:
        return value
    token_file = ROOT / "data" / "run" / "media_tokens.env"
    try:
        for line in token_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(env_name + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


async def run(host: str, port: int, token: str, text: str, timeout: float) -> int:
    url = f"ws://{host}:{port}/local"
    headers = {"X-Pet-Token": token} if token else None
    async with websockets.connect(
        url,
        additional_headers=headers,
        max_size=256 * 1024,
        ping_interval=20,
    ) as ws:
        msg_id = f"local-{uuid.uuid4().hex[:8]}"
        await ws.send(json.dumps({"type": "chat", "id": msg_id, "text": text}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.2, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                continue
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                print(raw)
                continue
            kind = str(event.get("type") or "")
            if kind == "state":
                print(f"[state] {event.get('state')}")
            elif kind == "bubble":
                print(f"[bubble] {event.get('text')}")
                if event.get("done"):
                    return 0
            elif kind == "error":
                print(f"[error] {event.get('error')}")
                return 1
            else:
                print(f"[{kind}] {event.get('id') or ''}")
    print("timeout", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="local pet gateway test client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9950)
    ap.add_argument("--token", default="")
    ap.add_argument("--token-env", default="PET_GATEWAY_TOKEN")
    ap.add_argument("--text", default="在吗？")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    token = load_token(args.token, args.token_env)
    return asyncio.run(run(args.host, args.port, token, args.text, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
