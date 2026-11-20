"""Environment and resource checks for the low profile."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .security import KernelError, load_kernel

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional
    psutil = None


def _total_ram_bytes() -> int | None:
    if psutil:
        return int(psutil.virtual_memory().total)
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys)
    return None


def _available_ram_bytes() -> int | None:
    if psutil:
        return int(psutil.virtual_memory().available)
    total = _total_ram_bytes()
    return int(total * 0.5) if total else None


def _sticker_generation_report(config: Config) -> dict[str, Any]:
    """Is the LoRA sticker path configured?

    There is no local service to probe any more: the render happens on
    ModelScope, so the interesting facts are the model/loras and whether the
    API key the client will look for is present.  Pending jobs are counted so
    a stuck async queue is visible without opening the DB.
    """
    env_name = str(
        config.get("sticker.modelscope_api_key_env") or "MODELSCOPE_API_KEY"
    ).strip()
    raw_loras = config.get("sticker.generation_loras") or []
    if isinstance(raw_loras, str):
        raw_loras = [raw_loras]
    pending: int | None
    try:
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sticker_jobs "
                "WHERE status IN ('waiting','running')"
            ).fetchone()
        finally:
            conn.close()
        pending = int(row[0]) if row else 0
    except (OSError, sqlite3.Error):
        pending = None
    return {
        "enabled": bool(config.get("sticker.generation_enabled", True)),
        "model": str(config.get("sticker.generation_model") or ""),
        "loras": [str(item) for item in raw_loras if str(item).strip()],
        "api_key_env": env_name,
        "api_key_present": bool(os.environ.get(env_name, "").strip()),
        "pending_jobs": pending,
    }


def check(config: Config) -> dict[str, Any]:
    config.ensure_dirs()
    total_ram = _total_ram_bytes()
    avail_ram = _available_ram_bytes()
    disk = shutil.disk_usage(config.data_dir)
    fts = sqlite3.connect(":memory:").execute(
        "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
    ).fetchone()
    db_parent = config.db_path.parent
    writable = os.access(db_parent, os.W_OK)
    profile = config.profile
    recommended = _recommend(total_ram, profile)
    llm_key_env = str(config.get("llm.api_key_env", "OPENCODE_API_KEY"))
    llm_key_present = bool(os.environ.get(llm_key_env))
    if not llm_key_present and llm_key_env != "OPENCODE_API_KEY":
        llm_key_present = bool(os.environ.get("OPENCODE_API_KEY"))
    tts_enabled = bool(config.get("tts.enabled", False))
    tts_mode = str(config.get("tts.mode", "http_json"))
    tts_configured = bool(
        (tts_mode == "http_json" and config.get("tts.base_url"))
        or (
            tts_mode == "gpt_sovits"
            and config.get("tts.base_url")
            and config.get("tts.ref_audio_path")
        )
        or (tts_mode == "command" and config.get("tts.command"))
    )
    kernel_report: dict[str, Any] = {}
    kernel_ok = True
    try:
        kernel = load_kernel(config)
        kernel_report = kernel.to_dict()
        kernel_ok = kernel.verify_file()
    except KernelError as exc:
        kernel_ok = False
        kernel_report = {"error": str(exc)}
    report = {
        "profile": profile,
        "identity_kernel": kernel_report,
        "identity_kernel_ok": kernel_ok,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "fts5": bool(fts and fts[0]),
        "data_dir": str(config.data_dir),
        "db_path": str(config.db_path),
        "db_parent_writable": writable,
        "disk_total_gb": round(disk.total / 1e9, 1),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "ram_total_gb": round(total_ram / 1e9, 1) if total_ram else None,
        "ram_available_gb": round(avail_ram / 1e9, 1) if avail_ram else None,
        "recommended_profile": recommended,
        "llm_provider": config.get("llm.provider", ""),
        "llm_model": config.get("llm.model", ""),
        "llm_api_style": config.get("llm.api_style", "chat_completions"),
        "llm_key_env": llm_key_env,
        "llm_key_present": llm_key_present,
        "tts_enabled": tts_enabled,
        "tts_configured": tts_configured,
        "sticker_generation": _sticker_generation_report(config),
        "warnings": [],
        "ok": True,
    }
    if not report["identity_kernel_ok"]:
        report["warnings"].append("Identity kernel missing or hash mismatch; refusing to trust persona.")
        report["ok"] = False
    if not report["fts5"]:
        report["warnings"].append("SQLite FTS5 unavailable; keyword search will be limited.")
        report["ok"] = False
    if not writable:
        report["warnings"].append("DB directory is not writable.")
        report["ok"] = False
    if report["disk_free_gb"] < 5:
        report["warnings"].append("Less than 5 GB free disk space.")
        report["ok"] = False
    if total_ram and total_ram < 12e9:
        report["warnings"].append("Less than 12 GB RAM; use low profile and remote heavy services.")
    if recommended != profile:
        report["warnings"].append(
            f"Resource profile suggests {recommended}, current is {profile}."
        )
    if not report["llm_key_present"]:
        report["warnings"].append(
            f"{llm_key_env} is not set; chat will use the local fallback reply."
        )
    if report["tts_enabled"] and not report["tts_configured"]:
        report["warnings"].append(
            "TTS is enabled but base_url/command is empty; voice output will fail."
        )
    gen = report["sticker_generation"]
    if gen["enabled"] and not gen["api_key_present"]:
        report["warnings"].append(
            f"{gen['api_key_env']} is not set; sticker generation will be skipped "
            "(no crash, she just sends no sticker)."
        )
    return report


def _recommend(total_ram: int | None, current: str) -> str:
    if current in {"cloud", "modelscope", "moss-photo"}:
        # cloud 是部署形态（在线层 + 家里 GPU）；modelscope / moss-photo 是
        # LLM 线路或拍照后端的叠加层（资源设置继承 low）。它们都不是资源
        # 档位，按内存反推 low/standard 没有意义，也不该报「建议换档」。
        return current
    if total_ram is None:
        return current
    gb = total_ram / 1e9
    if gb < 16:
        return "low"
    if gb < 40:
        return "low"
    return "standard"


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "fatfish doctor",
        "=" * 40,
        f"profile:              {report['profile']}",
        f"recommended profile:  {report['recommended_profile']}",
        f"python:               {report['python']}",
        f"platform:             {report['platform']}",
        f"sqlite:               {report['sqlite']} (FTS5={report['fts5']})",
        f"identity kernel:      ok={report['identity_kernel_ok']} {report['identity_kernel'].get('version', '?')} {str(report['identity_kernel'].get('digest', ''))[:12]}",
        f"data dir:             {report['data_dir']}",
        f"db path:              {report['db_path']}",
        f"db parent writable:   {report['db_parent_writable']}",
        f"disk:                 {report['disk_free_gb']} GB free / {report['disk_total_gb']} GB",
        f"ram:                  {report['ram_available_gb']} GB free / {report['ram_total_gb']} GB",
        f"llm:                  {report['llm_provider']} / {report['llm_model']} ({report['llm_api_style']})",
        f"llm key:              {report['llm_key_env']} present={report['llm_key_present']}",
        f"tts:                  enabled={report['tts_enabled']} configured={report['tts_configured']}",
    ]
    gen = report.get("sticker_generation") or {}
    if gen:
        lines.append(
            f"sticker gen:          enabled={gen.get('enabled')} "
            f"model={gen.get('model') or '?'} loras={len(gen.get('loras') or [])} "
            f"key={gen.get('api_key_present')} pending={gen.get('pending_jobs')}"
        )
    if report["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in report["warnings"])
    lines.append(f"status:               {'OK' if report['ok'] else 'ATTENTION'}")
    return "\n".join(lines)
