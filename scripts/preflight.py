"""Pre-flight check before spending GPU: is there room to run the model?

Burning a run because the machine was already at 99% is the kind of mistake that
looks like a model bug ("渲染出来是人声怪响 / 进程被 OOM 杀掉"). So every TTS or
SVS run starts with this, not with the render:

    python scripts/preflight.py --for svs          # 唱歌（YuE2 -> RVC）
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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Measured 2026-11-14 on this machine (see
# .qqbot-lab/reports/task-memory-measurements.md), plus a safety margin.
#   TTS loaded peak: RSS 7.2 GiB, GPU +5.6 GiB; unload keeps RSS 5.6 GiB
#   singing peak:    RSS 12.4 GiB, GPU +4.7 GiB, 276 s
#   embedding peak:  RSS 0.7 GiB, GPU +0.16 GiB
# ``kill_tts`` is mandatory for sing/RVC: the TTS *process* must die, not just
# unload, because its RSS does not shrink after /unload.
# sing 的 GPU 门槛取实测峰值 4.7 GiB + 0.8 GiB 余量 = 5.5 GiB（2026-11-15 修正：
# 之前照抄 TTS 的 6.5 GiB，导致桌面/浏览器占 2 GiB 时云端唱歌被误判"内存/显存紧"）。
PROFILES: dict[str, dict[str, object]] = {
    "embedding": {"gpu": 0.25, "ram": 1.0, "duration": 26, "kill_tts": False},
    "tts": {"gpu": 6.5, "ram": 8.5, "duration": 91, "kill_tts": False},
    "sing": {"gpu": 5.5, "ram": 14.5, "duration": 276, "kill_tts": True},
}
PROFILES["svs"] = dict(PROFILES["sing"])

# Backwards-compatible names for older callers/tests.
NEED = {name: float(profile["gpu"]) for name, profile in PROFILES.items()}
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


def _control_client(data_dir: str | Path | None):
    """The supervisor control client, or None when no supervisor is running."""
    try:
        from fatfish.services import ControlClient, token_path
    except Exception:  # pragma: no cover - app import must never break preflight
        return None
    token = token_path(data_dir or (ROOT / "data"))
    if not token.is_file():
        return None
    return ControlClient(token)


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # pragma: no cover - optional
        return None


def _tts_listener_pid() -> int:
    ps = _psutil()
    if ps is None:
        return 0
    try:
        for conn in ps.net_connections(kind="tcp"):
            if (
                conn.status == ps.CONN_LISTEN
                and conn.laddr
                and conn.laddr.port == 9880
                and conn.pid
            ):
                return int(conn.pid)
    except (ps.Error, OSError):
        pass
    return 0


def stop_tts_process(data_dir: str | Path | None = None, *, timeout: float = 45.0) -> bool:
    """Kill the TTS process, not merely unload its model.

    Prefer the supervisor; fall back to the real listener PID and verify its
    command line before killing, so a stale pid file can never kill an
    unrelated process.
    """
    client = _control_client(data_dir)
    if client is not None:
        try:
            client.stop_and_wait("tts", timeout=timeout)
            if service_state(9880) is None:
                return True
        except Exception:
            pass
    unload_tts(timeout=10.0)
    ps = _psutil()
    pid = _tts_listener_pid() or 0
    if ps is not None and pid:
        try:
            proc = ps.Process(pid)
            cmdline = " ".join(proc.cmdline())
            if "indextts_server.py" in cmdline:
                for child in proc.children(recursive=True) + [proc]:
                    try:
                        child.kill()
                    except ps.Error:
                        pass
                ps.wait_procs([proc], timeout=10)
        except ps.Error:
            pass
    if pid and ps is None:
        # No psutil: use the pid file, but only after a port/listener match.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"] if os.name == "nt"
            else ["kill", "-9", str(pid)],
            capture_output=True, timeout=15,
        )
    return service_state(9880) is None


def start_tts_process(data_dir: str | Path | None = None, *, timeout: float = 60.0) -> bool:
    """Start a fresh lightweight TTS process (no model warmup).

    The supervisor path keeps the normal start_all ownership; the direct path
    is a safety net for a crashed/absent control plane (observed once after a
    song handoff).  Both paths pass the configured pitch so a restart does not
    silently change her voice.
    """
    if service_state(9880) is not None:
        return True
    client = _control_client(data_dir)
    if client is not None:
        try:
            client.start_and_wait("tts", timeout=timeout)
            if service_state(9880) is not None:
                return True
        except Exception:
            pass
    python = ROOT / ".venv-indextts" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    server = ROOT / "scripts" / "indextts_server.py"
    if not python.is_file() or not server.is_file():
        return False
    pitch = 1
    try:
        from fatfish.config import load_config

        pitch = int(load_config("low").get("tts.pitch_semitones", 1))
    except Exception:
        pass
    log_path = Path(data_dir or (ROOT / "data")) / "logs" / "tts.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("ab") as handle:
        proc = subprocess.Popen(
            [
                str(python), str(server),
                "--host", "127.0.0.1", "--port", "9880",
                "--idle-unload-seconds", "0",
                "--pitch-semitones", str(pitch),
            ],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    try:
        (Path(data_dir or (ROOT / "data")) / "run" / "tts.pid").write_text(
            str(proc.pid) + "\n", encoding="ascii"
        )
    except OSError:
        pass
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if service_state(9880) is not None:
            return True
        time.sleep(0.4)
    return False


def loaded_services() -> dict[int, bool]:
    loaded: dict[int, bool] = {}
    for port in SERVICES:
        state = service_state(port)
        if state is not None:
            loaded[port] = bool(state.get("loaded"))
    return loaded


def check(
    task: str,
    *,
    data_dir: str | Path | None = None,
    kill_tts: bool = False,
    need_gpu: float | None = None,
    need_ram: float | None = None,
) -> tuple[bool, list[str], dict]:
    """Programmatic preflight used by service startup hooks and the CLI."""
    key = "sing" if task == "svs" else task
    profile = PROFILES.get(key)
    if profile is None:
        raise KeyError(f"unknown preflight task: {task}")
    if kill_tts:
        stop_tts_process(data_dir)
    need_gpu = float(profile["gpu"] if need_gpu is None else need_gpu)
    need_ram = float(profile["ram"] if need_ram is None else need_ram)
    g = gpu()
    mem = ram()
    loaded = loaded_services()
    ok, notes = verdict(need_gpu, need_ram, g, mem, loaded)
    return ok, notes, {
        "task": key,
        "need_gpu_gib": need_gpu,
        "need_ram_gib": need_ram,
        "gpu_free_gib": None if g is None else round(g.free, 3),
        "ram_available_gib": None if mem is None else round(mem[0], 3),
        "loaded": {str(k): v for k, v in loaded.items()},
        "ok": ok,
    }


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
    ap.add_argument("--for", dest="kind", choices=sorted(PROFILES), default="svs",
                    help="这次要跑什么（决定要多少显存/内存）")
    ap.add_argument("--need-gpu", type=float, default=None,
                    help="覆盖默认显存阈值（GiB）")
    ap.add_argument("--need-ram", type=float, default=None,
                    help="覆盖默认内存阈值（GiB）")
    ap.add_argument("--unload-tts", action="store_true",
                    help="不够就先 POST /unload 卸掉语音服务，再复查")
    ap.add_argument("--kill-tts", action="store_true",
                    help="唱歌/RVC 前杀掉 TTS 进程（不是只 unload）")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)

    profile = PROFILES[args.kind]
    need_gpu = float(args.need_gpu if args.need_gpu is not None else profile["gpu"])
    need_ram = float(args.need_ram if args.need_ram is not None else profile["ram"])
    if args.unload_tts and not (args.kill_tts or profile.get("kill_tts")):
        print("  -> 只 unload 不够：TTS 进程 RSS 不会释放；唱歌建议 --kill-tts")
        unload_tts()
    ok, notes, data = check(
        args.kind,
        data_dir=args.data_dir,
        kill_tts=bool(args.kill_tts),
        need_gpu=need_gpu,
        need_ram=need_ram,
    )
    if args.json:
        print(json.dumps({**data, "notes": notes}, ensure_ascii=False, indent=2))
    else:
        print(f"== preflight: --for {args.kind} "
              f"(需要 GPU {need_gpu:.1f} GiB / RAM {need_ram:.1f} GiB)")
        for n in notes:
            print("  " + n)
        print("  => " + ("可以跑" if ok else "先别跑"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
