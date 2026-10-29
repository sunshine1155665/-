"""Pre-flight check before spending GPU: is there room to run the model?

Burning a run because the machine was already at 99% is the kind of mistake that
looks like a model bug ("渲染出来是人声怪响 / 进程被 OOM 杀掉"). So every TTS or
SVS run starts with this, not with the render:

    python scripts/preflight.py --for svs          # 唱歌（SoulX-Singer）
    python scripts/preflight.py --for tts          # 语音（IndexTTS，9880）
    python scripts/preflight.py --for svs --unload-tts   # 先卸载语音服务再跑

It answers three questions and exits non-zero when the answer is "no":

1. **显存够不够**：GPU 空闲显存 vs 这次要的额度（SVS 约 5.5 GB，TTS 约 6.5 GB）。
2. **内存够不够**：系统可用内存（Windows 用 GlobalMemoryStatusEx，Linux 读
   ``/proc/meminfo``），低于阈值直接判红。
3. **服务是不是正占着卡**：9880/9881 的 ``/health`` 里 ``loaded`` 为真就说明模型
   常驻，显存是它占的；``--unload-tts`` 会调 ``POST /unload`` 腾出来。

Only the standard library, and nothing here touches a model: it is safe to run
from any shell, and the unit tests exercise the parsers with fixed strings.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

# What each kind of run needs, in GiB, measured on this machine (2026-10-25):
#   SVS   peak 4.1 GB model + ~1 GB of activations/audio buffers
#   TTS   IndexTTS-2.5 resident 5.8 GB + decode buffers
NEED = {"svs": 5.5, "tts": 6.5}
# Below this much free RAM the run either swaps or gets killed; 6 GiB is what the
# SVS pipeline (torch + fp16 weights + librosa) actually holds.
NEED_RAM = 6.0
SERVICES = {9880: "indextts", 9881: "embedding"}


@dataclass
class Gpu:
    used: float
    total: float

    @property
    def free(self) -> float:
        return max(0.0, self.total - self.used)


def parse_nvidia_smi(text: str) -> Gpu | None:
    """``"6784, 8151"`` (MiB, from ``--format=csv,noheader,nounits``)."""
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.replace("MiB", "").split(",")]
        if len(parts) < 2:
            continue
        try:
            used, total = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        return Gpu(used=used / 1024, total=total / 1024)
    return None


def _ram_windows() -> tuple[float, float] | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    gib = 1024 ** 3
    return stat.ullAvailPhys / gib, stat.ullTotalPhys / gib


def parse_meminfo(text: str) -> tuple[float, float] | None:
    """Linux ``/proc/meminfo`` -> (available GiB, total GiB)."""
    vals: dict[str, float] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        num = rest.strip().split(" ")[0]
        try:
            vals[key] = float(num) / 1024 ** 2   # kB -> GiB
        except ValueError:
            continue
    if "MemTotal" not in vals:
        return None
    return vals.get("MemAvailable", vals.get("MemFree", 0.0)), vals["MemTotal"]


def ram() -> tuple[float, float] | None:
    if os.name == "nt":
        return _ram_windows()
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            return parse_meminfo(f.read())
    except OSError:
        return None


def gpu() -> Gpu | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_nvidia_smi(out.stdout) if out.returncode == 0 else None


def service_state(port: int, timeout: float = 3.0) -> dict | None:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def unload_tts(timeout: float = 60.0) -> bool:
    req = urllib.request.Request("http://127.0.0.1:9880/unload", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def verdict(need_gpu: float, need_ram: float, g: Gpu | None,
            mem: tuple[float, float] | None,
            loaded: dict[int, bool]) -> tuple[bool, list[str]]:
    """Pure decision function so the tests can pin the thresholds."""
    notes: list[str] = []
    ok = True
    if g is None:
        notes.append("GPU: nvidia-smi 不可用（无法确认显存）")
        ok = False
    elif g.free < need_gpu:
        notes.append(f"GPU: 只剩 {g.free:.2f} GiB，需要 {need_gpu:.1f} GiB")
        ok = False
    else:
        notes.append(f"GPU: 空闲 {g.free:.2f} / {g.total:.1f} GiB（够）")
    if mem is None:
        notes.append("RAM: 读不到（无法确认内存）")
        ok = False
    elif mem[0] < need_ram:
        notes.append(f"RAM: 只剩 {mem[0]:.1f} / {mem[1]:.1f} GiB，"
                     f"需要 {need_ram:.1f} GiB")
        ok = False
    else:
        notes.append(f"RAM: 可用 {mem[0]:.1f} / {mem[1]:.1f} GiB（够）")
    holders = [f"{SERVICES.get(p, p)}:{p}" for p, on in loaded.items() if on]
    if holders:
        notes.append("常驻服务占用显存：" + "、".join(holders)
                     + "（先用 --unload-tts 或 POST /unload 释放）")
    else:
        notes.append("常驻服务：无")
    return ok, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑模型前先看内存/显存")
    ap.add_argument("--for", dest="kind", choices=sorted(NEED), default="svs",
                    help="这次要跑什么（决定要多少显存）")
    ap.add_argument("--need-gpu", type=float, default=None,
                    help="覆盖默认显存阈值（GiB）")
    ap.add_argument("--need-ram", type=float, default=NEED_RAM)
    ap.add_argument("--unload-tts", action="store_true",
                    help="不够就先 POST /unload 卸掉语音服务，再复查")
    args = ap.parse_args(argv)

    need_gpu = args.need_gpu if args.need_gpu is not None else NEED[args.kind]
    g = gpu()
    mem = ram()
    loaded = {}
    for port in SERVICES:
        st = service_state(port)
        if st is not None:
            loaded[port] = bool(st.get("loaded"))
    ok, notes = verdict(need_gpu, args.need_ram, g, mem, loaded)
    print(f"== preflight: --for {args.kind} "
          f"(需要 GPU {need_gpu:.1f} GiB / RAM {args.need_ram:.1f} GiB)")
    for n in notes:
        print("  " + n)

    if not ok and args.unload_tts and loaded.get(9880):
        print("  -> 卸载 9880（IndexTTS）…")
        print("  -> " + ("unload ok" if unload_tts() else "unload 失败"))
        g = gpu()
        mem = ram()
        loaded[9880] = bool((service_state(9880) or {}).get("loaded"))
        ok, notes = verdict(need_gpu, args.need_ram, g, mem, loaded)
        for n in notes:
            print("  " + n)

    print("  => " + ("可以跑" if ok else "先别跑"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
