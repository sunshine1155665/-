#!/usr/bin/env python3
"""Run the official QQ Bot adapter as a standalone service.

This avoids touching the cloud's existing CLI/build: the cloud package is
older than the local dev tree, so only the adapter-specific files are synced.

Usage (cloud service):
    /opt/fatfish/.venv/bin/python /opt/fatfish/scripts/qqbot_service.py \
        --profile cloud --config /opt/fatfish/config/cloud.local.toml

``--check`` boots the runtime, prints the adapter status (no gateway connect)
and exits, used by the deployment health check.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# Running this file directly puts scripts/ (not the app root) on sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.cli import _bootstrap
from fatfish.pet_link import PetLink
from fatfish.platforms.qqbot import QQBotAdapter, make_client
from fatfish.runtime import Runtime

log = logging.getLogger("fatfish.qqbot.service")


def build_runtime(profile: str, config_path: str):
    # Reuse the CLI's bootstrap so persona seeding, migrations and dirs behave
    # exactly like every other fatfish entry point.
    args = SimpleNamespace(profile=profile, config=config_path)
    config, db = _bootstrap(args)
    runtime = Runtime(db, config)
    adapter = QQBotAdapter(runtime, config)
    return config, runtime, adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fatfish official QQ Bot service")
    parser.add_argument("--profile", default="cloud")
    parser.add_argument("--config", default="/opt/fatfish/config/cloud.local.toml")
    parser.add_argument("--check", action="store_true", help="boot and print status, do not connect")
    parser.add_argument("--send-target", default="", help="proactive test target (openid)")
    parser.add_argument("--send-text", default="", help="proactive test text")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config, runtime, adapter = build_runtime(args.profile, args.config)
    if args.send_target:
        client = make_client(config)
        client.connect()
        try:
            summary = client.send_proactive("private", args.send_target, args.send_text)
        finally:
            try:
                client.close()
            except Exception:
                pass
        print(json.dumps({"ok": True, "send": summary}, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.check:
        print(json.dumps(adapter.status(), ensure_ascii=False, indent=2, default=str))
        return 0
    print(
        f"[qqbot-service] starting profile={config.profile} "
        f"data={config.data_dir} enabled={adapter.enabled}",
        flush=True,
    )
    pet_link = PetLink.from_env()
    if pet_link is not None:
        # The desktop pet is the same owner, not a second owner binding.
        try:
            runtime.owner.bind_surface(
                "desktop:owner",
                platform="desktop",
                role="owner",
                display_name=runtime.owner.default_display_name,
            )
        except Exception:  # noqa: BLE001 - a broken surface must not stop QQ
            log.exception("could not seed desktop owner surface")
        adapter.pet_link = pet_link
        pet_link.start()
        print("[qqbot-service] pet link started", flush=True)
    # A previous process may have died mid-render; never leave a song "running"
    # forever (and never re-deliver a half-written artifact).
    try:
        db = runtime.db
        db.execute(
            "UPDATE sing_jobs SET status='failed', error='interrupted', finished_at=? "
            "WHERE status='running'",
            (time.time(),),
        )
    except Exception:  # noqa: BLE001 - ledger repair must not block startup
        log.exception("could not reconcile stale sing_jobs")
    try:
        while True:
            try:
                adapter.run_forever()
                break
            except KeyboardInterrupt:
                break
            except Exception:  # noqa: BLE001 - a service should keep trying
                log.exception("qqbot service crashed; retrying in 10s")
                time.sleep(10)
    finally:
        if pet_link is not None:
            pet_link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
