"""Environment and resource checks for the low profile."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config
from .security import KernelError, load_kernel

# Mirrors fatfish.memory.embedder.HTTP_BACKENDS; imported by value to keep the
# doctor importable even if that module grows heavy dependencies later.
_HTTP_BACKENDS = frozenset({"http", "service", "bge", "remote"})

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


def _sticker_embedding_report(config: Config) -> dict[str, Any]:
    """Is the sticker vector path actually working?

    The interesting failure is silent: if the stored vectors were written by a
    different backend, every similarity is 0 and ranking quietly degrades to
    keyword overlap. Nothing crashes, so nothing complains -- unless we look.
    """
    backend = str(
        config.get("sticker.embedding_backend")
        or config.get("memory.embedding_backend", "hash")
    ).strip().lower()
    url = str(config.get("sticker.embedding_url") or "").strip()
    info: dict[str, Any] = {"backend": backend, "reachable": None, "stored_width": None}

    stored = None
    try:
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT embedding_text FROM stickers "
                "WHERE active=1 AND embedding_text IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            stored = len(json.loads(row[0]))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        stored = None
    info["stored_width"] = stored

    if backend in _HTTP_BACKENDS:
        info["url"] = url
        if not url:
            info["reachable"] = False
        else:
            health_url = url.rsplit("/", 1)[0] + "/health"
            try:
                with urllib.request.urlopen(health_url, timeout=1.5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                info["reachable"] = bool(payload.get("ok"))
                info["served_width"] = payload.get("dim")
            except (OSError, ValueError, urllib.error.URLError):
                info["reachable"] = False

    served = info.get("served_width") or stored
    info["width_matches"] = (
        None if not stored or not served else int(stored) == int(served)
    )
    return info


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
        "sticker_embedding": _sticker_embedding_report(config),
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
    emb = report["sticker_embedding"]
    if emb["backend"] in _HTTP_BACKENDS and emb.get("reachable") is False:
        report["warnings"].append(
            f"Sticker embedding service at {emb.get('url') or '(no url)'} is unreachable; "
            "sticker ranking falls back to hashing (no crash, weaker matches)."
        )
    if emb.get("width_matches") is False:
        report["warnings"].append(
            f"Sticker vectors are {emb['stored_width']}-dim but the service serves "
            f"{emb.get('served_width')}-dim; every similarity will be 0. "
            "Rebuild the sticker vector index for the active backend."
        )
    return report


def _recommend(total_ram: int | None, current: str) -> str:
    if current == "cloud":
        # cloud 是部署形态（在线层 + 家里 GPU），不是资源档位：这台机器
        # 只跑接入/编排/记忆，按内存反推 low/standard 没有意义。
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
    emb = report.get("sticker_embedding") or {}
    if emb:
        served = emb.get("served_width") or "?"
        lines.append(
            f"sticker vectors:      backend={emb.get('backend')} "
            f"stored={emb.get('stored_width') or '?'}d served={served}d "
            f"reachable={emb.get('reachable')}"
        )
    if report["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in report["warnings"])
    lines.append(f"status:               {'OK' if report['ok'] else 'ATTENTION'}")
    return "\n".join(lines)
