#!/usr/bin/env python3
"""显存 / 内存闸门：让多个 agent 串行使用同一块 8GB 显卡。

这块卡（RTX 5060 Laptop，8151 MiB）上 TTS(9880) / ComfyUI(8188) / YuE2+RVC 不能同时驻留。
产品侧已经在用 ``data/run/vram.lock`` 做租约（``fatfish/photo/service.py``），
本脚本复用**同一把锁**，所以 agent 跑重活时和产品自己的拍照/唱歌不会互相踩。

用法（推荐 ``run``，锁随进程退出自动释放，崩了也不会卡死）：

    python scripts/agent_board/gpu.py status
    python scripts/agent_board/gpu.py run --agent zcode --wait 600 -- python -m soulx.song_yue2_rvc
    python scripts/agent_board/gpu.py run --agent zcode --unload-tts -- python tools/bench.py

约定：
- 任何会加载模型的活（TTS 常驻、YuE2/RVC、ComfyUI、embedding、训练/评测）都必须走 ``run``。
- 纯 CPU 的活（单测、文档、SQLite）不用抢，直接跑；但别同时开超过 2 个重 CPU 进程。
- 用完立刻退出子进程；``run`` 返回后锁就释放，别把服务挂在里面一直不放。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from fatfish.process_lock import acquire, lock_path  # noqa: E402  (stdlib-only module)

TTS_UNLOAD = "http://127.0.0.1:9880/unload"
TTS_LOAD = "http://127.0.0.1:9880/load"


def data_dir() -> Path:
    raw = os.environ.get("FATFISH_DATA_DIR")
    if not raw:
        try:
            from fatfish.config import Config

            raw = Config().get("app.data_dir", "data")
        except Exception:  # noqa: BLE001 - 配置读不到就退回默认值
            raw = "data"
    path = Path(str(raw))
    return path if path.is_absolute() else PROJECT / path


def vram() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip().splitlines()[0]
        total, used, free = (int(x) for x in out.split(","))
        return {"total": total, "used": used, "free": free}
    except Exception:  # noqa: BLE001
        return {}


def ram() -> dict:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        mb = 1024 * 1024
        return {
            "load_pct": stat.dwMemoryLoad,
            "total_mb": stat.ullTotalPhys // mb,
            "avail_mb": stat.ullAvailPhys // mb,
            "commit_avail_mb": stat.ullAvailPageFile // mb,
        }
    except Exception:  # noqa: BLE001
        return {}


def holder_path() -> Path:
    return lock_path(data_dir(), "vram").with_name("vram.holder.json")


def write_holder(agent: str, note: str) -> None:
    path = holder_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agent": agent, "note": note, "pid": os.getpid(),
        "since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_holder() -> None:
    try:
        holder_path().unlink()
    except OSError:
        pass


def read_holder() -> dict:
    try:
        return json.loads(holder_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def tts_post(url: str) -> str:
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - 固定 localhost
            return f"{resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"跳过（{type(exc).__name__}）"


def cmd_status() -> int:
    v = vram()
    r = ram()
    path = lock_path(data_dir(), "vram")
    print(f"锁文件   : {path}")
    if v:
        print(f"显存     : 已用 {v['used']} / {v['total']} MiB，空闲 {v['free']} MiB")
    else:
        print("显存     : nvidia-smi 不可用")
    if r:
        print(f"内存     : 已用 {r['load_pct']}%，可用 {r['avail_mb']} MiB / 共 {r['total_mb']} MiB")
    holder = read_holder()
    probe = acquire(path)
    if probe is None:
        who = f"{holder.get('agent')}（{holder.get('note')}，自 {holder.get('since')}，pid {holder.get('pid')}）" if holder else "未知持有者"
        print(f"租约     : 被占用 → {who}")
        return 1
    probe.release()
    clear_holder()
    print("租约     : 空闲")
    return 0


def cmd_run(args) -> int:
    path = lock_path(data_dir(), "vram")
    deadline = time.time() + max(0, args.wait)
    lock = acquire(path)
    while lock is None:
        if time.time() >= deadline:
            holder = read_holder()
            print(f"拿不到显存租约（等待 {args.wait}s 超时）。持有者：{holder or '未知'}", file=sys.stderr)
            return 3
        time.sleep(2)
        lock = acquire(path)

    write_holder(args.agent, args.note or " ".join(args.cmd)[:120])
    code: int | None = None
    try:
        v = vram()
        if v and v["free"] < args.min_free_mb:
            print(f"显存不足：空闲 {v['free']} MiB < 要求 {args.min_free_mb} MiB。先卸载 TTS 或关掉 ComfyUI。", file=sys.stderr)
            return 4
        if args.unload_tts:
            print(f"TTS /unload → {tts_post(TTS_UNLOAD)}")
        print(f"[gpu] {args.agent} 持有租约，开始：{' '.join(args.cmd)}", flush=True)
        code = subprocess.run(args.cmd, cwd=str(PROJECT)).returncode
        return code
    finally:
        if args.unload_tts and args.reload_tts:
            print(f"TTS /load → {tts_post(TTS_LOAD)}")
        lock.release()
        clear_holder()
        print(f"[gpu] 释放租约（exit={code}）", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(prog="gpu", description="显存/内存闸门：多 agent 串行使用 8GB 显卡")
    sub = ap.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="看显存、内存和当前租约")
    p_status.set_defaults(func=lambda a: cmd_status())

    p_run = sub.add_parser("run", help="拿租约 → 跑命令 → 释放（推荐）")
    p_run.add_argument("--agent", required=True, help="身份名，与看板一致（zcode / opencode / workbuddy）")
    p_run.add_argument("--note", default="", help="这活在干什么，写进 holder 文件便于排查")
    p_run.add_argument("--wait", type=int, default=300, help="等锁秒数，0 = 不等待直接失败")
    p_run.add_argument("--min-free-mb", type=int, default=0, help="要求的最小空闲显存（如 7000）")
    p_run.add_argument("--unload-tts", action="store_true", help="跑之前 POST 9880/unload")
    p_run.add_argument("--reload-tts", action="store_true", help="跑完 POST 9880/load（默认不重载，按需加载）")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后的真实命令")
    p_run.set_defaults(func=cmd_run)

    args = ap.parse_args()
    if getattr(args, "cmd", None) and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if args.command == "run" and not args.cmd:
        print("run 需要 `--` 之后的命令，例如：gpu.py run --agent zcode -- python -m staging_x", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
