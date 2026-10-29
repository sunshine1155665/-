#!/usr/bin/env python
"""Generate one Anima Turbo whale-girl photo through ComfyUI.

This is the manual/smoke-test entry point; the chat runtime uses the same
``fatfish.photo.PhotoService`` in-process instead of shelling out.

Examples
--------
  python scripts/anima_photo_worker.py --check
  python scripts/anima_photo_worker.py --user "下雨天在便利店门口自拍"
  python scripts/anima_photo_worker.py --prompt "1girl, solo, whale girl, ..." --seed 42
  python scripts/anima_photo_worker.py --user "拍一张" --keep   # keep ComfyUI warm
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.config import load_config  # noqa: E402
from fatfish.context import ensure_default_persona  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.photo import PhotoService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anima_photo_worker.py",
        description="用 Anima Turbo + 鲸鱼娘 LoRA 生成一张照片（按需启停 ComfyUI）。",
    )
    parser.add_argument("--profile", default=None, help="配置 profile（low/standard/remote）")
    parser.add_argument("--user", default="自拍一张", help="自然语言拍照请求（走一次 LLM 写提示词）")
    parser.add_argument("--prompt", default="", help="直接给 Anima 正向提示词，跳过 LLM")
    parser.add_argument("--negative", default="", help="覆盖负向提示词")
    parser.add_argument("--caption", default="", help="覆盖图片说明")
    parser.add_argument("--session", default="photo-worker", help="写入 photo_jobs 的 session_id")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="生成后不停止本次启动的 ComfyUI",
    )
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        help="只使用已在运行的 ComfyUI，不自动启动",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查配置、模型文件和 ComfyUI 状态",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="只输出 JSON（不打印友好提示）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.profile)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    service = PhotoService(config, db)
    if args.no_autostart:
        service.autostart = False
    if args.keep:
        service.stop_after = False

    try:
        if args.check:
            status = service.status()
            status["db"] = str(db.path)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            ok = bool(status.get("enabled")) and bool(status.get("assets_ok"))
            return 0 if ok else 1

        if args.prompt.strip():
            prompt = service.manual_prompt(
                args.prompt,
                negative=args.negative,
                caption=args.caption or "鲸鱼娘的自拍",
            )
        else:
            prompt = service.build_prompt(args.user)
            if args.negative.strip():
                prompt.negative = service._merge_negative(args.negative)  # noqa: SLF001
            if args.caption.strip():
                prompt.caption = args.caption.strip()[:60]

        result = service.generate(
            "",
            prompt=prompt,
            session_id=args.session,
            seed=args.seed,
        )
        payload = {
            **result.as_dict(),
            "job_id": result.job_id,
            "prompt_source": result.prompt_source,
            "elapsed_ms": round(result.elapsed_ms, 1),
            "comfyui_started": result.comfyui_started,
            "comfyui_stopped": result.comfyui_stopped,
            "tts_unloaded": result.tts_unloaded,
            "tts_stopped": result.tts_stopped,
            "tts_restarted": result.tts_restarted,
            "comfyui_url": service.base_url,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not result.ok and not args.json:
            from fatfish.photo import friendly_photo_error

            print(
                f"\n友好提示：{friendly_photo_error(result.error)}",
                file=sys.stderr,
            )
        return 0 if result.ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
