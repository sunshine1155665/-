"""Serve a live system-stats page (CPU / RAM / VRAM / GPU temperature) so the
phone can watch the machine over Tailscale during long renders.

    python scripts/serve_stats.py --host 127.0.0.1 --port 8910
    tailscale serve --bg --https=8447 http://127.0.0.1:8910

Same idea as ``serve_audio.py`` — the tailnet is already trusted, so the phone
gets a direct link and nothing touches the public internet. The server binds
to 127.0.0.1 by default and only becomes reachable through ``tailscale serve``.

Standard library only (hard rule for this repo): CPU and RAM come from
``ctypes`` calls into the Win32 API, the GPU summary comes from ``nvidia-smi``,
and per-process VRAM comes from the ``\\GPU Process Memory`` performance
counter via ``typeperf`` — on this WDDM machine ``nvidia-smi --query-*-apps``
lists processes but reports every memory as ``[N/A]``, while the PDH counter
gives real bytes (minus some system processes that report garbage, which the
cap filters out). Heavy subprocess results are cached so phone polls don't
spawn processes every time.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "data" / "run" / "vram.lock"

# Ports that mean "the model servers are resident" (see AGENTS.md).
RESIDENT_PORTS = {
    "tts_9880": 9880,
    "embed_9881": 9881,
    "comfy_8188": 8188,
}

NVIDIA_SMI = "nvidia-smi"


def _run(args: list[str], timeout: float = 5) -> str:
    out = subprocess.run(
        args, capture_output=True, text=True, errors="replace", timeout=timeout)
    return out.stdout


# --------------------------------------------------------------------------- #
# GPU summary (nvidia-smi)                                                    #
# --------------------------------------------------------------------------- #

def parse_gpu_line(line: str) -> dict | None:
    """One CSV row of ``--query-gpu=temperature,util,power.draw,power.limit,
    memory.used,memory.total --format=csv,noheader,nounits`` -> dict.

    ``power.draw`` reports ``[N/A]`` when the GPU idles on some drivers, so any
    field that fails to parse becomes None instead of poisoning the row.
    """
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 6:
        return None

    def num(text: str, kind=float) -> int | float | None:
        try:
            return kind(text)
        except ValueError:
            return None

    return {
        "temp_c": num(parts[0], int),
        "util": num(parts[1], int),
        "power_w": num(parts[2]),
        "power_limit_w": num(parts[3]),
        "vram_used_mb": num(parts[4], int),
        "vram_total_mb": num(parts[5], int),
    }


# --------------------------------------------------------------------------- #
# Per-process VRAM (typeperf counter, tasklist for names)                     #
# --------------------------------------------------------------------------- #

_PID_RE = re.compile(r"pid_(\d+)")


def parse_typeperf(out: str) -> dict[int, float]:
    """``typeperf "\\GPU Process Memory(*)\\Dedicated Usage" -sc 1`` output ->
    ``{pid: dedicated bytes}``. A process appears once per LUID/adapter, so
    instances of the same pid are summed; zero values are dropped."""
    lines = [line for line in out.splitlines() if line.startswith('"')]
    if len(lines) < 2:
        return {}
    try:
        header = next(csv.reader([lines[0]]))
        values = next(csv.reader([lines[1]]))
    except (csv.Error, StopIteration):
        return {}
    totals: dict[int, float] = {}
    for instance, raw in zip(header[1:], values[1:]):
        match = _PID_RE.search(instance)
        if not match:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            pid = int(match.group(1))
            totals[pid] = totals.get(pid, 0.0) + value
    return totals


def parse_tasklist(out: str) -> dict[int, str]:
    """``tasklist /fo csv /nh`` output -> ``{pid: image name}``. The header
    row (``"Image Name","PID",...``) fails the digit check and is skipped."""
    names: dict[int, str] = {}
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2 and row[1].isdigit():
            names[int(row[1])] = row[0]
    return names


_tasklist_lock = threading.Lock()
_tasklist_cache: dict = {"t": 0.0, "data": {}}


def _tasklist_names(ttl: float = 30.0) -> dict[int, str]:
    with _tasklist_lock:
        if time.monotonic() - _tasklist_cache["t"] > ttl:
            try:
                out = _run(["tasklist", "/fo", "csv", "/nh"], timeout=10)
                _tasklist_cache["data"] = parse_tasklist(out)
            except (OSError, subprocess.SubprocessError):
                _tasklist_cache["data"] = {}
            _tasklist_cache["t"] = time.monotonic()
        return _tasklist_cache["data"]


def vram_apps(vram_total_mb: int | None) -> list[dict]:
    """Per-process dedicated VRAM, biggest first.

    WDDM counters report nonsense (terabytes) for some protected system
    processes, so anything above the card's own VRAM is dropped, as are
    sub-16 MiB rounding artifacts.
    """
    try:
        out = _run(["typeperf", r"\GPU Process Memory(*)\Dedicated Usage",
                    "-sc", "1"], timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    per_pid = parse_typeperf(out)
    if not per_pid:
        return []
    cap = (vram_total_mb or 32 * 1024) * 2**20 * 1.1
    names = _tasklist_names()
    apps: dict[str, dict] = {}
    for pid, raw_bytes in per_pid.items():
        if raw_bytes > cap or raw_bytes < 16 * 2**20:
            continue
        name = names.get(pid) or f"PID {pid}"
        entry = apps.setdefault(name, {"name": name, "count": 0, "mb": 0.0})
        entry["count"] += 1
        entry["mb"] += raw_bytes / 2**20
    return sorted(apps.values(), key=lambda e: e["mb"], reverse=True)


# --------------------------------------------------------------------------- #
# Snapshot assembly                                                           #
# --------------------------------------------------------------------------- #

_gpu_lock = threading.Lock()
_gpu_cache: dict = {"t": 0.0, "data": None}


def _query_gpu() -> dict:
    data: dict = {"gpu": None, "vram_apps": [], "error": None}
    try:
        rows = [parse_gpu_line(line) for line in _run([
            NVIDIA_SMI,
            "--query-gpu=temperature.gpu,utilization.gpu,power.draw,"
            "power.limit,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]).splitlines()]
        rows = [row for row in rows if row]
        if rows:
            data["gpu"] = rows[0]
    except (OSError, subprocess.SubprocessError) as exc:
        data["error"] = f"nvidia-smi 不可用: {exc}"
        return data
    data["vram_apps"] = vram_apps(
        data["gpu"]["vram_total_mb"] if data["gpu"] else None)
    return data


def gpu_snapshot(ttl: float = 3.0) -> dict:
    """Cached GPU query — the phone polls every ~2s, the source costs ~2s."""
    with _gpu_lock:
        now = time.monotonic()
        if now - _gpu_cache["t"] >= ttl:
            _gpu_cache["data"] = _query_gpu()
            _gpu_cache["t"] = now
        return _gpu_cache["data"]


def cpu_percent_from_samples(
        prev: tuple[int, int, int], now: tuple[int, int, int]) -> float | None:
    """``(idle, kernel, user)`` tick deltas -> busy percent.

    On Windows ``kernel`` already includes idle, so busy = kernel + user - idle.
    """
    idle = now[0] - prev[0]
    total = (now[1] - prev[1]) + (now[2] - prev[2])
    if total <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - idle / total) * 100))


def _win_cpu_times() -> tuple[int, int, int] | None:
    idle = ctypes.c_ulonglong()
    kernel = ctypes.c_ulonglong()
    user = ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return idle.value, kernel.value, user.value


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def ram_snapshot() -> dict | None:
    """Physical memory via GlobalMemoryStatusEx — one call, no subprocess."""
    if os.name != "nt":
        return None
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total = status.ullTotalPhys
    used = total - status.ullAvailPhys
    return {
        "total_gb": round(total / 2**30, 2),
        "used_gb": round(used / 2**30, 2),
        "percent": round(used / total * 100, 1) if total else None,
        "load_percent": status.dwMemoryLoad,
    }


_cpu_lock = threading.Lock()
_last_cpu: tuple[int, int, int] | None = None


def cpu_snapshot() -> float | None:
    """CPU busy percent since the previous call (None on the first call)."""
    global _last_cpu
    if os.name != "nt":
        return None
    now = _win_cpu_times()
    if now is None:
        return None
    with _cpu_lock:
        prev = _last_cpu
        _last_cpu = now
    if prev is None:
        return None
    return cpu_percent_from_samples(prev, now)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def lease_snapshot() -> str | None:
    """Who holds the VRAM lease, if anyone (raw snippet is fine for display)."""
    try:
        text = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    text = text.replace("\x00", "").strip()   # empty locks contain NUL padding
    if not text:
        return None
    try:
        info = json.loads(text)
        if isinstance(info, dict):
            bits = [f"{k}={info[k]}" for k in ("agent", "owner", "task", "pid")
                    if info.get(k) is not None]
            if bits:
                return " ".join(bits)
    except ValueError:
        pass
    return text[:160]


def snapshot() -> dict:
    """Everything the phone page shows, in one JSON document."""
    ram = ram_snapshot() or {}
    disk = shutil.disk_usage(ROOT)
    gpu = gpu_snapshot()
    return {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "cpu": {"percent": cpu_snapshot()},
        "ram": ram,
        "gpu": gpu["gpu"],
        "vram_apps": gpu["vram_apps"],
        "gpu_error": gpu["error"],
        "services": {name: port_open(port)
                     for name, port in RESIDENT_PORTS.items()},
        "lease": lease_snapshot(),
        "disk": {
            "total_gb": round(disk.total / 2**30, 1),
            "free_gb": round(disk.free / 2**30, 1),
        },
    }


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #

PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>fatfish 监控</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0}
body{background:#0f1115;color:#e8eaf0;font:15px/1.5 system-ui,sans-serif;
  padding:16px;max-width:560px;margin:0 auto}
h1{font-size:18px;margin-bottom:12px;display:flex;justify-content:space-between;
  align-items:baseline}
h1 small{font-size:12px;color:#8a90a0;font-weight:400}
.card{background:#171a21;border-radius:12px;padding:12px 14px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;align-items:baseline}
.big{font-size:34px;font-weight:700}
.unit{font-size:13px;color:#8a90a0}
.label{font-size:12px;color:#8a90a0}
.bar{height:8px;border-radius:4px;background:#262a35;margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%;background:#4299FF;border-radius:4px;
  transition:width .4s}
.warn i{background:#f5a623}
.crit i{background:#e5484d}
ul{list-style:none;padding:0;margin:6px 0 0;font-size:13px}
ul li{display:flex;justify-content:space-between;padding:2px 0;color:#c4c9d4}
.pills{display:flex;flex-wrap:wrap;gap:8px}
.pill{font-size:12px;padding:4px 10px;border-radius:999px;background:#262a35;color:#8a90a0}
.pill.on{background:#12335c;color:#7ab8ff}
.pill.on.hot{background:#4a1d1d;color:#ff8a8a}
.muted{color:#8a90a0;font-size:13px}
.err{color:#ff8a8a;font-size:12px;margin-top:8px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
</style></head><body>
<h1>🖥️ 本机状态 <small id="ts">连接中…</small></h1>

<div class="grid">
<div class="card"><div class="row"><span class="label">GPU 温度</span>
<span id="gputemp" class="big">…<span class="unit">°C</span></span></div>
<div class="row"><span class="unit" id="gpuutil">—</span></div></div>
<div class="card"><div class="row"><span class="label">GPU 功耗</span>
<span id="gpupower" class="big">…<span class="unit">W</span></span></div>
<div class="row"><span class="unit" id="gpulimit">—</span></div></div>
</div>

<div class="card"><div class="row"><span class="label">显存</span>
<span id="vramtext" class="unit">—</span></div>
<div class="bar" id="vrambar"><i></i></div>
<ul id="vramapps"></ul></div>

<div class="card"><div class="row"><span class="label">内存</span>
<span id="ramtext" class="unit">—</span></div>
<div class="bar" id="rambar"><i></i></div></div>

<div class="card"><div class="row"><span class="label">CPU</span>
<span id="cpu" class="big">…<span class="unit">%</span></span></div></div>

<div class="card"><div class="row"><span class="label">常驻服务</span></div>
<div class="pills" id="services" style="margin-top:8px"></div>
<div class="muted" id="lease" style="margin-top:8px"></div></div>

<div class="card"><div class="row"><span class="label">磁盘 (代码盘)</span>
<span id="disk" class="unit">—</span></div></div>

<div class="err" id="err"></div>
<script>
const $=id=>document.getElementById(id);
function setBar(el,pct){el.classList.toggle("crit",pct>=85);
  el.classList.toggle("warn",pct>=70&&pct<85);el.firstElementChild.style.width=pct+"%";}
function tempColor(el,t){el.style.color=t>=78?"#e5484d":t>=60?"#f5a623":"#4ade80";}
async function tick(){
 try{
  const r=await fetch("/stats.json",{cache:"no-store"});
  if(!r.ok)throw new Error(r.status);
  const d=await r.json();
  $("err").textContent="";$("ts").textContent="更新于 "+d.ts;
  if(d.gpu){
   $("gputemp").innerHTML=(d.gpu.temp_c??"—")+'<span class="unit">°C</span>';
   if(d.gpu.temp_c!=null)tempColor($("gputemp"),d.gpu.temp_c);
   $("gpuutil").textContent="利用率 "+(d.gpu.util??"—")+"%";
   $("gpupower").innerHTML=(d.gpu.power_w!=null?Math.round(d.gpu.power_w):"—")
     +'<span class="unit">W</span>';
   $("gpulimit").textContent=d.gpu.power_limit_w!=null?("/ "+d.gpu.power_limit_w+"W 上限"):"";
   if(d.gpu.vram_total_mb){
    const p=d.gpu.vram_used_mb/d.gpu.vram_total_mb*100;
    $("vramtext").textContent=(d.gpu.vram_used_mb/1024).toFixed(1)+" / "
      +(d.gpu.vram_total_mb/1024).toFixed(1)+" GB ("+p.toFixed(0)+"%)";
    setBar($("vrambar"),p);
   }
  }
  $("vramapps").innerHTML="";
  (d.vram_apps||[]).slice(0,8).forEach(a=>{
   const li=document.createElement("li");
   li.textContent=a.name+(a.count>1?" ×"+a.count:"");
   const b=document.createElement("span");b.textContent=a.mb.toFixed(0)+" MiB";
   li.appendChild(b);$("vramapps").appendChild(li);});
  if(d.ram&&d.ram.total_gb){
   $("ramtext").textContent=d.ram.used_gb+" / "+d.ram.total_gb+" GB ("
     +d.ram.percent+"%)";setBar($("rambar"),d.ram.percent);}
  if(d.cpu&&d.cpu.percent!=null)
   $("cpu").innerHTML=Math.round(d.cpu.percent)+'<span class="unit">%</span>';
  const names={tts_9880:"TTS 9880",embed_9881:"Embedding 9881",comfy_8188:"ComfyUI 8188"};
  const pills=$("services");pills.innerHTML="";
  Object.entries(names).forEach(([k,label])=>{
   const on=(d.services||{})[k];
   const s=document.createElement("span");
   s.className="pill"+(on?(k==="comfy_8188"?" on hot":" on"):"");
   s.textContent=label+(on?" 在跑":" 空闲");pills.appendChild(s);});
  $("lease").textContent=d.lease?("GPU 租约: "+d.lease):"GPU 租约: 空闲";
  $("disk").textContent="剩 "+d.disk.free_gb+" / "+d.disk.total_gb+" GB";
 }catch(e){$("err").textContent="连接断开，2 秒后重试…";}
}
tick();setInterval(tick,2000);
</script></body></html>
"""


class StatsHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        sys.stderr.write("[stats] %s\n" % (fmt % args))

    def do_GET(self) -> None:
        if self.path.startswith("/stats.json"):
            body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用 Tailscale 内网远程看 CPU/内存/显存/温度")
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址；保持 127.0.0.1，由 tailscale serve 对外")
    ap.add_argument("--port", type=int, default=8910)
    args = ap.parse_args(argv)
    cpu_snapshot()  # prime the tick baseline so the first request has a number
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), StatsHandler)
    print(f"stats page on http://{args.host}:{args.port}/")
    print("手机端先跑: tailscale serve --bg --https=8447 "
          f"http://{args.host}:{args.port}")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
