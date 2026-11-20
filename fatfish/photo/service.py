"""On-demand photo generation: Anima Turbo + whale-girl LoRA via ComfyUI.

Design constraints
------------------
* The generated image prompt is **never** written to conversation history,
  memory, RAG or the next turn's prompt.  It is stored only in the
  ``photo_jobs`` table for debugging/audit.
* ComfyUI is started only when needed and stopped again when this service
  started it (``photo.stop_comfyui_after``).  An already-running ComfyUI is
  reused and left alone.
* One job at a time.  A second concurrent call returns ``busy`` instead of
  queueing unbounded work.
* No arbitrary shell: the only process ever spawned is the configured
  ComfyUI Python entry point.
* TTS is never killed.  When it holds VRAM we first try a cooperative
  ``POST /unload`` (and a shared service manager if one is installed); if
  that is unavailable and free VRAM is too low, the job fails with a clear
  "显存不足/请先卸载TTS" error.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import Config
from ..db import Database
from ..process_lock import acquire, lock_path
from .modelscope import (
    DEFAULT_BASE_URL as MODELSCOPE_DEFAULT_BASE_URL,
    DEFAULT_MODEL as MODELSCOPE_DEFAULT_MODEL,
    ModelScopePhotoClient,
    ModelScopePhotoError,
)

# ``PhotoService`` is created once per ``Runtime``, and the web server creates a
# new Runtime for every chat turn -- so an instance lock alone does not stop two
# "拍照" requests in different turns from starting two ComfyUI jobs.  This lock
# is process-wide; the file lock below extends the same rule across the web and
# QQ processes.
_PROCESS_JOB_LOCK = threading.Lock()

# The whale-girl LoRA is trained on these Danbooru-style tags; keeping them in
# every prompt is what makes the character consistent between photos.
CHARACTER_TAGS = (
    "1girl, solo, whale girl, blue hair, very long wavy hair, blue eyes, "
    "ahoge, whale tail"
)
CHARACTER_TAG_LIST = (
    "1girl",
    "solo",
    "whale girl",
    "blue hair",
    "very long wavy hair",
    "blue eyes",
    "ahoge",
    "whale tail",
)
STYLE_TAGS = (
    "masterpiece, best quality, highly detailed, cinematic lighting, "
    "film grain, anime style"
)
DEFAULT_OUTFIT = "dark navy and white maid dress"
DEFAULT_NEGATIVE = (
    "worst quality, low quality, score_1, score_2, score_3, blurry, "
    "jpeg artifacts, sepia, multiple girls, extra limbs, bad anatomy, "
    "bad hands, watermark, signature, text, cropped, out of frame, "
    "deformed body, extra arms, extra hands, bad proportions, pasted on"
)
DEFAULT_CAPTION = "鲸鱼娘的自拍"
_COMPOSITION_SCENE_WORDS = (
    "跑", "跳", "游", "坐", "躺", "站", "走", "转身", "举", "拿", "抱",
    "海边", "沙滩", "房间", "街", "雨", "雪", "咖啡", "书", "吉他", "花",
)
_COMPOSITION_CHARACTER_WORDS = (
    "自拍", "看镜头", "表情", "衣服", "裙子", "穿", "换", "妆", "发型",
    "特写", "上半身", "肖像", "装扮",
)
_NEW_OUTFIT_WORDS = (
    "换衣", "换件", "换套", "新衣服", "新裙子", "穿新", "泳装", "旗袍",
    "制服", "睡衣", "礼服", "婚纱", "和服", "女仆装", "打扮", "装扮",
)


def _choose_composition(request: str) -> str:
    """M0-M9 order A/B heuristic used by the deterministic fallback."""
    text = str(request or "")
    if any(word in text for word in _COMPOSITION_CHARACTER_WORDS):
        return "character"
    if any(word in text for word in _COMPOSITION_SCENE_WORDS):
        return "scene"
    return "character"


def _choose_outfit(request: str) -> str:
    """Default maid dress unless the request clearly asks for a new outfit."""
    text = str(request or "")
    if any(word in text for word in _NEW_OUTFIT_WORDS):
        return "new"
    return "default"


def _outfit_detail(request: str, outfit: str) -> str:
    if outfit == "new":
        cleaned = _clean_request_for_outfit(request)
        if cleaned:
            return f"a fresh SFW outfit chosen for the moment: {cleaned}"
        return "a fresh, scene-appropriate SFW outfit"
    return DEFAULT_OUTFIT


def _clean_request_for_outfit(text: str) -> str:
    raw = _CONTROL_RE.sub("", str(text or ""))
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:160]
PHOTO_REQUEST_RE = re.compile(
    r"(拍照|拍一张|拍张照|拍个照|拍照片|自拍|来张照片|来一张照片|来张自拍"
    # 2026-11-17: 「发几张图」「多发点照片」也是明确要图，旧正则只认
    # 「拍/自拍/来张照片」，于是她说「来了来了」但一张都没生成。
    r"|(?:发|来|给|再|多|整|帮)[^。！？!?]{0,8}(?:照片|图|自拍))"
)
# 引用/评价已有的图，不是要新图：你发的图、这张照片、刚才那张图。
_PHOTO_REFERENCE_RE = re.compile(
    r"(?:你|她|他|刚才|刚刚|上次|之前|上回)"
    r"[^。！？!?]{0,6}(?:发|拍|给)的[^。！？!?]{0,4}(?:照片|图|自拍)"
)
_PHOTO_DEMONSTRATIVE_RE = re.compile(
    r"(?:这|那)(?:张|些|个)?[^。！？!?]{0,4}(?:照片|图|自拍)"
)
_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_ACTIVITY_SCENES = {
    "browse_bilibili": "sitting on a sofa with a smartphone, cozy room, soft lamp light",
    "read": "reading a book in a quiet room, warm afternoon light",
    "rest": "lying on a bed with soft blankets, lazy afternoon",
    "write_notes": "desk with notebooks, warm desk lamp, cozy room",
    "listen_music": "headphones, window seat, city lights outside",
    "chores": "home interior, tidy room, daylight",
    "sleeping": "bedroom at night, sleepy, soft blanket",
    "chatting": "cozy room, looking at viewer",
    "project": "desk setup, computer glow, late night",
}

_KEYWORD_SCENES = (
    ("雨", "rainy street at night, transparent umbrella, wet pavement, neon reflections"),
    ("海", "seaside, ocean, sunset, sea breeze"),
    ("泳", "beach, ocean, bright summer sky"),
    ("睡", "bedroom at night, bed, soft blanket, sleepy"),
    ("夜", "night, city lights, cozy room"),
    ("吃", "cafe table, food, warm interior"),
    ("饭", "cafe table, food, warm interior"),
    ("咖啡", "cafe, coffee cup, warm interior"),
    ("公园", "park, cherry blossom trees, daylight"),
    ("花", "flower garden, soft daylight"),
    ("街", "city street, daylight, crowd in the distance"),
    ("猫", "cozy room, a cat nearby"),
    ("学习", "desk with books, study room, warm lamp"),
    ("工作", "desk setup, computer, late night"),
    ("生日", "birthday cake, candles, cozy room"),
)

_MOOD_EXPRESSIONS = {
    "happy": "happy smile",
    "energetic": "bright cheerful smile",
    "tired": "sleepy expression, soft eyes",
    "calm": "gentle smile",
    "sad": "soft melancholy expression",
}


def _image_extension(body: bytes) -> str:
    """Keep the real image format so the web/QQ send path gets a sane suffix."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".png"

_ERROR_MESSAGES = {
    "busy": "正在拍上一张呢，等它拍完再来嘛。",
    "photo_busy": "另一个窗口正在拍照，等这张拍完再来嘛。",
    "vram_busy": "显卡正忙着呢（TTS 正在加载或另一张图在画），等一会儿再拍。",
    "disabled": "拍照功能还没开呢。",
    "vram_conflict_tts": (
        "显存不足：TTS 还占着显卡，请先卸载 TTS 再拍照。"
    ),
    "comfyui_not_running": "ComfyUI 没在运行，自动启动也是关着的。",
    "comfyui_start_timeout": "ComfyUI 启动超时了，等会儿再试试。",
    "comfyui_start_failed": "ComfyUI 起不来：{detail}",
    "comfyui_spawn_error": "ComfyUI 启动进程失败，检查一下配置路径。",
    "comfyui_execution_error": "ComfyUI 画到一半报错了：{detail}",
    "comfyui_timeout": "拍照超时了，下次再试吧。",
    "comfyui_no_image": "ComfyUI 没把图交出来，再试一次？",
    "missing_assets": "模型文件不全：{detail}",
    "paths_missing": "ComfyUI 路径不对：{detail}",
    "photo_storage_error": "照片记录写不进去，先不拍了。",
    # -- 云端（T-0068）：云主机不画图，图在家里那台 GPU 机上画。
    # 文案与 T-0067 的 friendly_sing_error 同风格：一句话、不道歉、不解释。
    "home_offline": "家里那台画图的机器这会儿没连上，等我回到家再拍给你看。",
    "remote_not_configured": "云端还没配好拍照的地址（photo.remote_url），这次先不拍了。",
    "remote_timeout": "家里那边画太久没回音，这次先算了，等会儿再拍。",
    "remote_bad_photo": "家里传回来的图是坏的，这次先不拍了。",
    "remote_error": "家里画图的服务出了点岔子，等会儿再拍。",
    "unauthorized": "家里拍照服务的 token 对不上，配置里改一下 PHOTO_SERVER_TOKEN 吧。",
    "prompt_missing": "没说要画什么，这次先不拍了。",
    # -- 魔搭云端出图（[photo] mode = "modelscope"）：不占本机显存。
    "modelscope_not_configured": (
        "云端生图还没配好（photo.modelscope_model / MODELSCOPE_API_KEY），这次先不拍了。"
    ),
    "modelscope_unauthorized": (
        "魔搭的 key 不对或没权限，检查一下 MODELSCOPE_API_KEY 吧。"
    ),
    "modelscope_rate_limited": "魔搭今天的生图额度用完了，等明天再拍吧。",
    "modelscope_model_unavailable": (
        "这个 LoRA 模型云端暂时加载不了，换一个模型或稍后再试。"
    ),
    "modelscope_timeout": "云端画太久没回音，这次先算了，等会儿再拍。",
    "modelscope_failed": "云端画图出了点岔子，等会儿再拍。",
    "modelscope_no_image": "云端没把图交出来，再试一次？",
    "modelscope_bad_image": "云端传回来的图是坏的，这次先不拍了。",
}


def wants_photo(text: str | None) -> bool:
    """True when a private-chat message explicitly asks for a photo."""
    raw = str(text or "")
    if _PHOTO_REFERENCE_RE.search(raw):
        return False
    # 「这张照片拍得真好」是在评价，不是在要新图；只有同时带请求动词
    # （再来/多发/给我发…）才算。
    if _PHOTO_DEMONSTRATIVE_RE.search(raw) and not re.search(
        r"(?:再|多|发|来|给)", raw
    ):
        return False
    return bool(PHOTO_REQUEST_RE.search(raw))


@dataclass(slots=True)
class PhotoPrompt:
    positive: str
    negative: str
    caption: str = DEFAULT_CAPTION
    source: str = "fallback"
    # M0-M9 composition choice: "character" = character-first (自拍/立绘),
    # "scene" = scene-first (动作/交互/环境为主).
    composition: str = "character"
    # "default" = 默认海军蓝女仆装；"new" = 按场景/心情换一套 SFW 衣服。
    outfit: str = "default"
    outfit_detail: str = ""


@dataclass(slots=True)
class PhotoResult:
    ok: bool
    path: str | None = None
    caption: str = ""
    error: str | None = None
    job_id: str = ""
    prompt_source: str = ""
    elapsed_ms: float = 0.0
    comfyui_started: bool = False
    comfyui_stopped: bool = False
    tts_unloaded: bool = False
    tts_stopped: bool = False
    tts_restarted: bool = False
    pending: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "caption": self.caption,
            "error": self.error,
            "job_id": self.job_id,
            "pending": self.pending,
        }


def friendly_photo_error(error: str | None) -> str:
    """Map an internal error code to a short in-character message."""
    code = str(error or "").strip()
    base = code.split(":", 1)[0]
    detail = code.split(":", 1)[1].strip() if ":" in code else ""
    template = _ERROR_MESSAGES.get(base)
    if template is None:
        return "拍照没拍成，主人哥哥等会儿再试试。"
    if "{detail}" in template:
        return template.format(detail=detail[:120] or "未知错误")
    return template


class PhotoError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


#: 云端拍照的默认上限。ComfyUI 冷启动 30–120s，给到 180s 留余量；比本机的
#: ``photo.timeout_seconds``（300s）短，因为它还要盖住网络往返。
DEFAULT_REMOTE_PHOTO_TIMEOUT = 180.0
DEFAULT_REMOTE_CONNECT_TIMEOUT = 2.0
_MIN_PHOTO_BYTES = 64
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class RemotePhotoClient:
    """Transport to the home box for ``[photo] mode = "remote"`` (T-0068).

    Two methods, both replaceable in tests: :meth:`reachable` is the cheap "is
    the box awake" probe, :meth:`post` is the one long request.  It mirrors
    :class:`fatfish.sing.service.RemoteSingerClient` on purpose -- phase B
    swaps both sides onto the same server conventions (``X-Token``, 409 busy,
    JSON error bodies), and this file then only changes the endpoint/parser.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = DEFAULT_REMOTE_PHOTO_TIMEOUT,
        connect_timeout: float = DEFAULT_REMOTE_CONNECT_TIMEOUT,
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "")
        self.timeout = max(10.0, float(timeout))
        self.connect_timeout = max(0.5, float(connect_timeout))

    def _endpoint(self) -> str:
        return self.base_url + "/photo"

    def _host_port(self) -> tuple[str, int] | None:
        parts = urlsplit(self.base_url)
        if not parts.hostname:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.hostname, port

    def reachable(self) -> bool:
        """TCP probe with a short timeout.

        DNS for a Tailscale MagicDNS name fails outright when the box is off
        (``OSError``); that is the "home is asleep" answer, not an exception.
        """
        target = self._host_port()
        if target is None:
            return False
        try:
            with socket.create_connection(target, timeout=self.connect_timeout):
                return True
        except OSError:
            return False

    def post(self, payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        """POST ``/photo``; HTTP errors come back as ``(code, body, headers)``."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "image/png, application/json",
        }
        if self.token:
            headers["X-Token"] = self.token
        request = urllib.request.Request(
            self._endpoint(), data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers or {})
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            return exc.code, raw, dict(exc.headers or {})


class PhotoService:
    """Single-flight Anima Turbo photo generator backed by ComfyUI."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        llm: Any = None,
        life: Any = None,
        service_manager: Any = None,
        remote_client: Any = None,
        modelscope_client: Any = None,
    ):
        self.config = config
        self.db = db
        self.llm = llm
        self.life = life
        self.enabled = bool(config.get("photo.enabled", False))
        # 云端（T-0068）：mode="remote" 时图在家里画，本地 ComfyUI 一概不碰。
        self.mode = (
            str(config.get("photo.mode", "local") or "local").strip().lower()
            or "local"
        )
        self.remote_url = str(
            config.get("photo.remote_url", "") or ""
        ).strip().rstrip("/")
        self.remote_timeout = max(
            10.0,
            float(
                config.get(
                    "photo.remote_timeout_seconds", DEFAULT_REMOTE_PHOTO_TIMEOUT
                )
            ),
        )
        self.remote_client = remote_client
        if self.remote_client is None and self.mode == "remote" and self.remote_url:
            token_env = str(
                config.get("photo.remote_token_env", "PHOTO_SERVER_TOKEN") or ""
            ).strip()
            self.remote_client = RemotePhotoClient(
                self.remote_url,
                token=os.environ.get(token_env, "") if token_env else "",
                timeout=self.remote_timeout,
                connect_timeout=float(
                    config.get(
                        "photo.remote_connect_timeout_seconds",
                        DEFAULT_REMOTE_CONNECT_TIMEOUT,
                    )
                ),
            )
        # 魔搭（mode="modelscope"）：图在云端 API-Inference 画，本机不占显存。
        # ``model`` 直接填 LoRA 的 ModelScope model id，平台会自动加载底模+LoRA。
        self.modelscope_base_url = str(
            config.get("photo.modelscope_base_url", MODELSCOPE_DEFAULT_BASE_URL)
            or MODELSCOPE_DEFAULT_BASE_URL
        ).strip().rstrip("/")
        self.modelscope_model = str(
            config.get("photo.modelscope_model", MODELSCOPE_DEFAULT_MODEL)
            or MODELSCOPE_DEFAULT_MODEL
        ).strip()
        self.modelscope_trigger = str(
            config.get("photo.modelscope_trigger", "whalegirl") or ""
        ).strip()
        self.modelscope_timeout = max(
            30.0, float(config.get("photo.modelscope_timeout_seconds", 300))
        )
        self.modelscope_poll_seconds = max(
            0.5, float(config.get("photo.modelscope_poll_seconds", 3.0))
        )
        self.modelscope_extra = config.get("photo.modelscope_extra", {}) or {}
        if not isinstance(self.modelscope_extra, dict):
            self.modelscope_extra = {}
        # 加速（2026-11-17）：底模 MOSS-Image-SFT 原生 50 步；DiffSynth 的 OPD
        # 蒸馏 LoRA 把它压到 8 步 / CFG 1.0，角色 LoRA 仍按 model 字段加载。
        # ``modelscope_extra`` 是兼容旧配置的逃生口：显式写在里面的同名字段优先。
        self.modelscope_accel_lora = str(
            config.get("photo.modelscope_accel_lora", "") or ""
        ).strip()
        self.modelscope_steps = max(
            0, int(config.get("photo.modelscope_steps", 0) or 0)
        )
        self.modelscope_guidance = max(
            0.0, float(config.get("photo.modelscope_guidance", 0.0) or 0.0)
        )
        self.modelscope_request_params = dict(self.modelscope_extra)
        if self.modelscope_accel_lora:
            self.modelscope_request_params.setdefault(
                "loras", self.modelscope_accel_lora
            )
            # 8 步只在加速 LoRA 在场时成立；单独把 accel_lora 置空而留下
            # steps=8 会退回「无加速的 8 步」——实测就是糊图，所以这两个
            # 采样参数跟着 accel_lora 一起生效/一起失效。
            if self.modelscope_steps:
                self.modelscope_request_params.setdefault(
                    "steps", self.modelscope_steps
                )
            if self.modelscope_guidance:
                self.modelscope_request_params.setdefault(
                    "guidance", self.modelscope_guidance
                )
        self.modelscope_client = modelscope_client
        if self.modelscope_client is None and self.mode == "modelscope":
            key_env = str(
                config.get("photo.modelscope_api_key_env", "MODELSCOPE_API_KEY") or ""
            ).strip()
            self.modelscope_client = ModelScopePhotoClient(
                self.modelscope_base_url,
                api_key=os.environ.get(key_env, "") if key_env else "",
                model=self.modelscope_model,
                timeout=self.modelscope_timeout,
                poll_seconds=self.modelscope_poll_seconds,
                size=(
                    f"{max(64, int(config.get('photo.width', 1024)))}x"
                    f"{max(64, int(config.get('photo.height', 1024)))}"
                ),
                extra_params=self.modelscope_request_params,
            )
        self.base_url = str(
            config.get("photo.comfyui_url", "http://127.0.0.1:8188")
        ).strip().rstrip("/")
        self.autostart = bool(config.get("photo.comfyui_autostart", True))
        self.python = str(config.get("photo.comfyui_python", "") or "").strip()
        self.main = str(config.get("photo.comfyui_main", "") or "").strip()
        self.cwd = str(config.get("photo.comfyui_cwd", "") or "").strip()
        self.models_root = str(config.get("photo.models_root", "") or "").strip()
        self.unet = str(
            config.get("photo.unet", "anima-turbo-v1.1.safetensors")
        )
        self.clip = str(config.get("photo.clip", "qwen_3_06b_base.safetensors"))
        self.vae = str(config.get("photo.vae", "qwen_image_vae.safetensors"))
        self.lora = str(
            config.get("photo.lora", "deepseek_whalegirl_v1.safetensors")
        )
        self.lora_strength = float(config.get("photo.lora_strength", 0.8))
        self.steps = max(1, int(config.get("photo.steps", 8)))
        self.cfg = float(config.get("photo.cfg", 1.0))
        self.width = max(64, int(config.get("photo.width", 1024)))
        self.height = max(64, int(config.get("photo.height", 1024)))
        self.timeout = max(10.0, float(config.get("photo.timeout_seconds", 300)))
        self.startup_timeout = max(
            5.0, float(config.get("photo.startup_timeout_seconds", 90))
        )
        self.poll_seconds = max(
            0.05, float(config.get("photo.poll_seconds", 1.0))
        )
        self.stop_after = bool(config.get("photo.stop_comfyui_after", True))
        self.unload_tts = bool(
            config.get("photo.unload_tts_before_start", True)
        )
        self.min_free_vram_mb = max(
            0, int(config.get("photo.min_free_vram_mb", 7000))
        )
        self.output_dir = config.data_dir / "photos"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = config.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Composition skill is loaded lazily per PhotoService instance, never
        # in the main chat context.  Missing file is fine: the built-in
        # M0-M9 summary in the system prompt remains the fallback.
        self._skill_text = ""
        try:
            raw_skill = str(config.get("photo.skill_path", "") or "").strip()
            if raw_skill:
                skill_path = Path(raw_skill)
                if not skill_path.is_absolute():
                    skill_path = Path(__file__).resolve().parents[2] / skill_path
                if skill_path.is_file():
                    self._skill_text = skill_path.read_text(encoding="utf-8")[:2600]
        except (OSError, UnicodeError, ValueError):
            self._skill_text = ""

        # Discovery is lazy: ``Runtime`` is built for every chat turn, and
        # probing a control plane that is not running used to cost the full
        # connect timeout before the turn even started.  A photo job is the
        # only caller that actually needs the manager.
        self._service_manager = service_manager
        self._service_manager_resolved = service_manager is not None
        self._control_token_file = config.data_dir / "run" / "control.token"
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._proc_started_by_us = False
        self._log_handle: Any = None
        self._tts_unloaded_this_job = False
        # Stopping the TTS *process* is sometimes required: /unload releases
        # the model but CUDA context/ONNX arenas keep several GB resident.
        self.stop_tts_process = bool(
            config.get("photo.stop_tts_process", True)
        )
        self.restart_tts_after = bool(
            config.get("photo.restart_tts_after", True)
        )
        self._tts_process_stopped = False
        self._tts_proc: subprocess.Popen[bytes] | None = None
        self._root = Path(__file__).resolve().parents[2]
        # One photo job at a time across every Runtime in this process and,
        # via the file lock, across the web and QQ processes.  The VRAM lease
        # is shared with scripts/cosyvoice_soft_server.py.
        self._photo_lock_path = lock_path(config.data_dir, "photo")
        self._vram_lock_path = lock_path(config.data_dir, "vram")
        self._tts_pid_path = config.data_dir / "run" / "tts_soft.pid"
        self.tts_python = str(
            config.get(
                "photo.tts_python",
                str(self._root / ".venv-cosyvoice" / "Scripts" / "python.exe"),
            )
        )
        self.tts_script = str(
            config.get(
                "photo.tts_script",
                str(self._root / "scripts" / "cosyvoice_soft_server.py"),
            )
        )
        self.tts_idle_seconds = max(
            0, int(config.get("photo.tts_idle_unload_seconds", 300))
        )

    @property
    def service_manager(self) -> Any | None:
        """The shared supervisor client, resolved only on first real use."""
        if not self._service_manager_resolved:
            self._service_manager = discover_service_manager(
                token_file=self._control_token_file
            )
            self._service_manager_resolved = True
        return self._service_manager

    # ------------------------------------------------------------- public API
    def status(self) -> dict[str, Any]:
        assets = self.asset_report() if self.mode == "local" else {}
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "remote_url": self.remote_url if self.mode == "remote" else "",
            "modelscope_model": self.modelscope_model
            if self.mode == "modelscope"
            else "",
            "modelscope_base_url": self.modelscope_base_url
            if self.mode == "modelscope"
            else "",
            "modelscope_accel_lora": self.modelscope_accel_lora
            if self.mode == "modelscope"
            else "",
            "modelscope_steps": self.modelscope_steps
            if self.mode == "modelscope"
            else 0,
            "modelscope_guidance": self.modelscope_guidance
            if self.mode == "modelscope"
            else 0.0,
            "modelscope_available": bool(
                self.mode == "modelscope"
                and self.modelscope_client is not None
                and getattr(self.modelscope_client, "available", False)
            ),
            "comfyui_url": self.base_url if self.mode == "local" else "",
            "comfyui_running": self._comfyui_up() if self.mode == "local" else False,
            "autostart": self.autostart,
            "stop_after": self.stop_after,
            "unet": self.unet,
            "clip": self.clip,
            "vae": self.vae,
            "lora": self.lora,
            "lora_strength": self.lora_strength,
            "steps": self.steps,
            "cfg": self.cfg,
            "size": [self.width, self.height],
            "output_dir": str(self.output_dir),
            "assets": assets,
            "assets_ok": all(assets.values()) if assets else True,
            # Do not probe the control plane just to render this panel.
            "service_manager": type(self._service_manager).__name__
            if self._service_manager_resolved and self._service_manager is not None
            else None,
        }

    def asset_report(self) -> dict[str, bool]:
        """Best-effort check of the configured launcher and model files."""
        report: dict[str, bool] = {
            "python": bool(self.python) and Path(self.python).is_file(),
            "main": bool(self.main) and Path(self.main).is_file(),
            "cwd": bool(self.cwd) and Path(self.cwd).is_dir(),
        }
        if self.models_root:
            root = Path(self.models_root)
            report["unet"] = (
                root / "models" / "diffusion_models" / self.unet
            ).is_file()
            report["clip"] = (
                root / "models" / "text_encoders" / self.clip
            ).is_file()
            report["vae"] = (root / "models" / "vae" / self.vae).is_file()
            report["lora"] = (root / "models" / "loras" / self.lora).is_file()
        return report

    def manual_prompt(
        self,
        positive: str,
        negative: str = "",
        caption: str = "",
    ) -> PhotoPrompt:
        """Build a prompt from explicit tags (worker/smoke-test path)."""
        clean = self._clean_prompt_text(positive)
        return PhotoPrompt(
            positive=self._ensure_character_tags(clean),
            negative=self._merge_negative(negative),
            caption=self._clean_caption(caption) or DEFAULT_CAPTION,
            source="manual",
        )

    def build_prompt(
        self,
        user_text: str,
        context: dict[str, Any] | None = None,
        *,
        session_id: str = "",
    ) -> PhotoPrompt:
        """One low-effort LLM call; deterministic fallback on any failure.

        The result is *not* persisted here.  ``generate`` writes it only to
        ``photo_jobs``; no memory/conversation table is touched.
        """
        request = self._clean_request(user_text)
        life = self._life_state(context)
        fallback = self._fallback_prompt(request, life)
        if self.mode == "modelscope":
            fallback = self._with_modelscope_trigger(fallback)
        if self.llm is None or not getattr(self.llm, "available", False):
            return fallback
        if self.mode == "modelscope":
            trigger = self.modelscope_trigger or "whalegirl"
            system_content = (
                "You write one safe-for-work anime image prompt for MOSS-Image. "
                "The character is fixed: an adult whale girl with blue gradient "
                "hair, very long wavy hair, blue eyes, an ahoge, whale ear fins "
                "and a single whale tail. "
                f"Start the positive prompt with the trigger word {trigger!r}. "
                "Use the M0-M9 composition rules: choose composition='character' "
                "for selfies/portraits/outfit/expression focus (M0->M3->M4->M1), "
                "or composition='scene' when an action, interaction, place or "
                "object is the main subject (M0->M1->M2->M3->M4->M5). "
                f"The default outfit is {DEFAULT_OUTFIT!r}; use outfit='default' "
                "unless the user asks for different clothes or the scene/mood "
                "clearly calls for one.  For outfit='new', invent one fresh, "
                "SFW, in-character outfit and describe it in outfit_detail. "
                "User wishes about clothes always win. "
                "Return strict JSON only, no markdown: "
                '{"composition":"character|scene","outfit":"default|new",'
                '"outfit_detail":"English outfit description or empty",'
                '"positive":"English prompt",'
                '"negative":"short English negative prompt",'
                '"caption":"short Chinese caption <= 20 chars"}. '
                "Never mention the user's identity, location, real names or "
                "private data."
            )
            if self._skill_text:
                system_content += (
                    "\n\nApply this on-demand composition skill, but follow its "
                    "shape rather than inventing extra sections:\n"
                    + self._skill_text
                )
        else:
            system_content = (
                "You write English Danbooru/Anima tags for one safe-for-work "
                "anime selfie. The character is fixed: an adult whale girl "
                "with blue hair, very long wavy hair, blue eyes, an ahoge and "
                "a whale tail. Include the character tags in every positive "
                "prompt. Choose composition='character' for selfies/portraits/"
                "outfit focus, or composition='scene' when an action, place or "
                f"object matters. The default outfit is {DEFAULT_OUTFIT!r}; "
                "use outfit='default' unless clothes are requested or clearly "
                "implied. For outfit='new', invent a fresh SFW outfit and put "
                "it in outfit_detail. User wishes about clothes always win. "
                "Return strict JSON only, no markdown: "
                '{"composition":"character|scene","outfit":"default|new",'
                '"outfit_detail":"outfit description or empty",'
                '"positive":"comma separated English tags",'
                '"negative":"comma separated English tags",'
                '"caption":"short Chinese caption <= 20 chars"}. '
                "Never mention the user's identity, location, real names or "
                "private data."
            )
        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": (
                    f"User request: {request or 'a casual selfie'}\n"
                    f"Current life state: {self._format_life(life)}\n"
                    "Pick the composition order and outfit yourself. "
                    "Scene should fit the request and the life state. "
                    "Camera: selfie or close-up, looking at viewer."
                ),
            },
        ]
        try:
            result = self.llm.chat(
                messages,
                # The gateway's max_tokens is a *total* completion budget and
                # reasoning tokens count against it (measured on deepseek-v4.1-
                # flash: 800/900 could be consumed before a single JSON token
                # was emitted).  The prompt is short and the answer is one
                # small JSON object, so a large cap costs nothing when unused
                # and is the only way to survive a chatty reasoning model.
                max_tokens=10000,
                temperature=0.7,
                # 2026-11-15: the gateway now rejects requests without an
                # ``x-opencode-session`` header (400 MissingSessionID), which
                # silently degraded every photo prompt to the fallback.  Use
                # the turn's session when the caller has one, else a stable
                # pseudo-session so the same prompt build is routable.
                session_id=session_id or "photo:prompt",
            )
        except Exception:
            return fallback
        text = str(getattr(result, "text", "") or "") if result is not None else ""
        data = _json_object(text)
        if not isinstance(data, dict):
            return fallback
        positive = self._clean_prompt_text(
            data.get("positive") or data.get("prompt") or ""
        )
        if not positive:
            return fallback
        negative = self._clean_prompt_text(data.get("negative") or "")
        caption = self._clean_caption(data.get("caption") or "")
        composition = str(data.get("composition") or "").strip().lower()
        if composition not in {"character", "scene"}:
            composition = _choose_composition(request)
        outfit = str(data.get("outfit") or "").strip().lower()
        if outfit not in {"default", "new"}:
            outfit = _choose_outfit(request)
        outfit_detail = self._clean_prompt_text(data.get("outfit_detail") or "")
        if not outfit_detail:
            outfit_detail = _outfit_detail(request, outfit)
        if outfit == "default" and DEFAULT_OUTFIT.lower() not in positive.lower():
            positive = f"{positive}, {DEFAULT_OUTFIT}"
        elif outfit == "new" and outfit_detail and outfit_detail.lower() not in positive.lower():
            positive = f"{positive}, {outfit_detail}"
        photo = PhotoPrompt(
            positive=self._ensure_character_tags(positive),
            negative=self._merge_negative(negative),
            caption=caption or fallback.caption,
            source="llm",
            composition=composition,
            outfit=outfit,
            outfit_detail=outfit_detail,
        )
        if self.mode == "modelscope":
            photo = self._with_modelscope_trigger(photo)
        return photo

    def generate(
        self,
        user_text: str = "",
        *,
        context: dict[str, Any] | None = None,
        prompt: PhotoPrompt | None = None,
        session_id: str = "",
        seed: int | None = None,
        now: float | None = None,
    ) -> PhotoResult:
        """Generate one image.  Never raises; failures are returned as codes."""
        started = time.time()
        now = time.time() if now is None else now
        if not self.enabled:
            return PhotoResult(False, error="disabled", elapsed_ms=0.0)
        if self.mode == "remote":
            return self._generate_remote(
                user_text,
                context=context,
                prompt=prompt,
                session_id=session_id,
                seed=seed,
                now=now,
                started=started,
            )
        if self.mode == "modelscope":
            return self._generate_modelscope(
                user_text,
                context=context,
                prompt=prompt,
                session_id=session_id,
                seed=seed,
                now=now,
                started=started,
            )
        if not self._lock.acquire(blocking=False):
            return PhotoResult(False, error="busy", elapsed_ms=0.0)
        if not _PROCESS_JOB_LOCK.acquire(blocking=False):
            self._lock.release()
            return PhotoResult(False, error="busy", elapsed_ms=0.0)
        process_lock = acquire(self._photo_lock_path)
        if process_lock is None:
            _PROCESS_JOB_LOCK.release()
            self._lock.release()
            return PhotoResult(False, error="photo_busy", elapsed_ms=0.0)
        job_id = uuid.uuid4().hex[:16]
        photo: PhotoPrompt | None = None
        result: PhotoResult | None = None
        comfy_started = False
        comfy_stopped = False
        tts_stopped = False
        tts_restarted = False
        vram_lock = None
        self._tts_unloaded_this_job = False
        try:
            photo = prompt or self.build_prompt(
                user_text, context, session_id=session_id
            )
            try:
                self._insert_job(job_id, session_id, now, photo)
            except sqlite3.Error:
                result = PhotoResult(
                    False,
                    error="photo_storage_error",
                    job_id=job_id,
                    elapsed_ms=(time.time() - started) * 1000,
                )
            else:
                # Hold the shared VRAM lease for the whole job: a TTS load
                # arriving now must fail fast instead of allocating next to
                # ComfyUI.  The TTS process only takes this lease while it is
                # loading weights, not while the model sits idle.
                vram_lock = acquire(self._vram_lock_path)
                if vram_lock is None:
                    ok, error, comfy_started = False, "vram_busy", False
                else:
                    ok, error, comfy_started = self._ensure_comfyui()
                if not ok:
                    self._update_job(
                        job_id,
                        status="failed",
                        error=error or "comfyui_unavailable",
                        comfyui_started=comfy_started,
                    )
                    result = PhotoResult(
                        False,
                        error=error or "comfyui_unavailable",
                        job_id=job_id,
                        prompt_source=photo.source,
                        elapsed_ms=(time.time() - started) * 1000,
                        comfyui_started=comfy_started,
                        tts_unloaded=self._tts_unloaded_this_job,
                    )
                else:
                    graph = self.build_graph(
                        photo.positive, photo.negative, seed=seed, job_id=job_id
                    )
                    prompt_id = self._submit(graph)
                    image = self._wait_for_image(prompt_id, started)
                    target = self.output_dir / f"{job_id}.png"
                    self._download_image(image, target)
                    self._update_job(
                        job_id,
                        status="done",
                        image_path=str(target),
                        comfyui_started=comfy_started,
                    )
                    result = PhotoResult(
                        True,
                        path=str(target),
                        caption=photo.caption,
                        job_id=job_id,
                        prompt_source=photo.source,
                        elapsed_ms=(time.time() - started) * 1000,
                        comfyui_started=comfy_started,
                        tts_unloaded=self._tts_unloaded_this_job,
                    )
        except PhotoError as exc:
            error = f"{exc.code}:{exc.detail}" if exc.detail else exc.code
            self._update_job(
                job_id,
                status="failed",
                error=error,
                comfyui_started=comfy_started,
            )
            result = PhotoResult(
                False,
                error=error,
                job_id=job_id,
                prompt_source=photo.source if photo else "",
                elapsed_ms=(time.time() - started) * 1000,
                comfyui_started=comfy_started,
            )
        except Exception as exc:  # one bad job must not kill the runtime
            error = f"{type(exc).__name__}:{str(exc)[:160]}"
            self._update_job(
                job_id,
                status="failed",
                error=error,
                comfyui_started=comfy_started,
            )
            result = PhotoResult(
                False,
                error=error,
                job_id=job_id,
                prompt_source=photo.source if photo else "",
                elapsed_ms=(time.time() - started) * 1000,
                comfyui_started=comfy_started,
            )
        finally:
            if comfy_started and self.stop_after:
                comfy_stopped = self._stop_comfyui()
            # Release the card before asking TTS back: the restarted server
            # must be allowed to load on its next request.
            if vram_lock is not None:
                vram_lock.release()
                vram_lock = None
            tts_stopped = bool(self._tts_process_stopped)
            if tts_stopped and self.restart_tts_after:
                # ComfyUI is stopped by now; bring the lazy TTS process back.
                tts_restarted = self._restart_tts_process()
            process_lock.release()
            _PROCESS_JOB_LOCK.release()
            self._lock.release()
        if result is None:  # defensive: the try block always assigns one
            result = PhotoResult(
                False,
                error="photo_failed",
                job_id=job_id,
                elapsed_ms=(time.time() - started) * 1000,
            )
        result.comfyui_stopped = comfy_stopped
        result.tts_unloaded = self._tts_unloaded_this_job
        result.tts_stopped = tts_stopped
        result.tts_restarted = tts_restarted
        return result

    # -- 云端（T-0068） ---------------------------------------------------
    def _generate_remote(
        self,
        user_text: str,
        *,
        context: dict[str, Any] | None,
        prompt: PhotoPrompt | None,
        session_id: str,
        seed: int | None,
        now: float,
        started: float,
    ) -> PhotoResult:
        """One POST to the home box; every failure is one sentence.

        The user's reply has already gone out by the time this runs, so the
        only thing that matters here is **latency**: probe with a 2s TCP
        connect first (a sleeping laptop's Tailscale name simply fails to
        resolve), and never sit through a ComfyUI cold start on this side.
        """
        if not self._lock.acquire(blocking=False):
            return PhotoResult(False, error="busy", elapsed_ms=0.0)
        job_id = uuid.uuid4().hex[:16]
        photo: PhotoPrompt | None = None
        try:
            photo = prompt or self.build_prompt(
                user_text, context, session_id=session_id
            )
            try:
                self._insert_job(job_id, session_id, now, photo)
            except sqlite3.Error:
                return PhotoResult(
                    False,
                    error="photo_storage_error",
                    job_id=job_id,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            error, path = self._remote_job(photo, job_id, seed)
            if error is None:
                return PhotoResult(
                    True,
                    path=path,
                    caption=photo.caption,
                    job_id=job_id,
                    prompt_source=photo.source,
                    elapsed_ms=(time.time() - started) * 1000,
                )
            self._update_job(job_id, status="failed", error=error)
            return PhotoResult(
                False,
                error=error,
                job_id=job_id,
                prompt_source=photo.source,
                elapsed_ms=(time.time() - started) * 1000,
            )
        except Exception as exc:  # one bad job must not kill the runtime
            error = f"{type(exc).__name__}:{str(exc)[:160]}"
            self._update_job(job_id, status="failed", error=error)
            return PhotoResult(
                False,
                error=error,
                job_id=job_id,
                prompt_source=photo.source if photo else "",
                elapsed_ms=(time.time() - started) * 1000,
            )
        finally:
            self._lock.release()

    # -- 魔搭云端出图（[photo] mode = "modelscope"） ------------------------
    def _generate_modelscope(
        self,
        user_text: str,
        *,
        context: dict[str, Any] | None,
        prompt: PhotoPrompt | None,
        session_id: str,
        seed: int | None,
        now: float,
        started: float,
    ) -> PhotoResult:
        """One ModelScope API-Inference job; no local VRAM, no ComfyUI.

        The LoRA is selected by ``photo.modelscope_model`` (a ModelScope model
        id such as ``sunny1155665/whalegirl-moss-v2``); the platform loads the
        declared base model and the LoRA together.  Everything else mirrors the
        remote path: one lock, one row in ``photo_jobs``, one image on disk.
        """
        if not self._lock.acquire(blocking=False):
            return PhotoResult(False, error="busy", elapsed_ms=0.0)
        job_id = uuid.uuid4().hex[:16]
        photo: PhotoPrompt | None = None
        try:
            photo = prompt or self.build_prompt(
                user_text, context, session_id=session_id
            )
            photo = self._with_modelscope_trigger(photo)
            try:
                self._insert_job(job_id, session_id, now, photo)
            except sqlite3.Error:
                return PhotoResult(
                    False,
                    error="photo_storage_error",
                    job_id=job_id,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            client = self.modelscope_client
            if client is None or not getattr(client, "available", False):
                self._update_job(
                    job_id, status="failed", error="modelscope_not_configured"
                )
                return PhotoResult(
                    False,
                    error="modelscope_not_configured",
                    job_id=job_id,
                    prompt_source=photo.source,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            try:
                raw = client.generate(
                    photo.positive,
                    photo.negative,
                    seed=seed,
                    width=self.width,
                    height=self.height,
                )
            except ModelScopePhotoError as exc:
                self._update_job(
                    job_id, status="failed", error=exc.error_text
                )
                return PhotoResult(
                    False,
                    error=exc.error_text,
                    job_id=job_id,
                    prompt_source=photo.source,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            target = self.output_dir / f"{job_id}{_image_extension(raw)}"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(raw)
                os.replace(tmp, target)
            except OSError:
                self._update_job(
                    job_id, status="failed", error="photo_storage_error"
                )
                return PhotoResult(
                    False,
                    error="photo_storage_error",
                    job_id=job_id,
                    prompt_source=photo.source,
                    elapsed_ms=(time.time() - started) * 1000,
                )

            self._update_job(
                job_id, status="done", image_path=str(target)
            )
            return PhotoResult(
                True,
                path=str(target),
                caption=photo.caption,
                job_id=job_id,
                prompt_source=photo.source,
                elapsed_ms=(time.time() - started) * 1000,
            )
        except Exception as exc:  # one bad job must not kill the runtime
            error = f"{type(exc).__name__}:{str(exc)[:160]}"
            self._update_job(job_id, status="failed", error=error)
            return PhotoResult(
                False,
                error=error,
                job_id=job_id,
                prompt_source=photo.source if photo else "",
                elapsed_ms=(time.time() - started) * 1000,
            )
        finally:
            self._lock.release()

    # ------------------------------------------------ async ModelScope jobs
    def _pending_modelscope_job(self) -> dict[str, Any] | None:
        try:
            return self.db.query_one(
                "SELECT id FROM photo_jobs WHERE provider='modelscope' "
                "AND status IN ('waiting','running') ORDER BY created_at LIMIT 1"
            )
        except sqlite3.Error:
            return None

    def start_modelscope_job(
        self,
        user_text: str = "",
        *,
        context: dict[str, Any] | None = None,
        session_id: str = "",
        now: float | None = None,
        reply_to: str = "",
    ) -> PhotoResult:
        """Submit one async ModelScope job and return immediately.

        The LLM prompt build and the HTTP submit are fast (seconds); polling and
        download happen in :meth:`poll_modelscope_jobs`, driven by
        ``Runtime.tick``.  This is the multi-round photo path: the turn can
        answer "等我一下下" while the image is still rendering.
        """
        started = time.time()
        now = time.time() if now is None else now
        if not self.enabled:
            return PhotoResult(False, error="disabled", elapsed_ms=0.0)
        if self.mode != "modelscope":
            return PhotoResult(False, error="modelscope_not_configured", elapsed_ms=0.0)
        client = self.modelscope_client
        if client is None or not getattr(client, "available", False):
            return PhotoResult(False, error="modelscope_not_configured", elapsed_ms=0.0)
        if self._pending_modelscope_job() is not None:
            return PhotoResult(False, error="busy", elapsed_ms=0.0)
        if not self._lock.acquire(blocking=False):
            return PhotoResult(False, error="busy", elapsed_ms=0.0)
        photo: PhotoPrompt | None = None
        job_id = uuid.uuid4().hex[:16]
        try:
            photo = self.build_prompt(user_text, context, session_id=session_id)
            photo = self._with_modelscope_trigger(photo)
            payload = client.build_payload(
                photo.positive,
                photo.negative,
                seed=None,
                width=self.width,
                height=self.height,
            )
            task_id = client.submit(payload)
            self._insert_job(
                job_id,
                session_id,
                now,
                photo,
                provider="modelscope",
                task_id=task_id,
                request_text=user_text,
                status="waiting",
                reply_to=reply_to,
            )
            return PhotoResult(
                True,
                caption=photo.caption,
                job_id=job_id,
                prompt_source=photo.source,
                elapsed_ms=(time.time() - started) * 1000,
                pending=True,
            )
        except ModelScopePhotoError as exc:
            return PhotoResult(
                False,
                error=exc.error_text,
                job_id=job_id,
                prompt_source=photo.source if photo else "",
                elapsed_ms=(time.time() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the runtime
            return PhotoResult(
                False,
                error=f"{type(exc).__name__}:{str(exc)[:160]}",
                job_id=job_id,
                prompt_source=photo.source if photo else "",
                elapsed_ms=(time.time() - started) * 1000,
            )
        finally:
            self._lock.release()

    def poll_modelscope_jobs(
        self,
        *,
        now: float | None = None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Poll due ModelScope jobs once; download successes for delivery."""
        now = time.time() if now is None else now
        if self.mode != "modelscope":
            return []
        client = self.modelscope_client
        if client is None or not getattr(client, "available", False):
            return []
        try:
            rows = self.db.query(
                "SELECT id, session_id, created_at, status, task_id, caption, reply_to "
                "FROM photo_jobs WHERE provider='modelscope' "
                "AND status IN ('waiting','running') ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            )
        except sqlite3.Error:
            return []
        completed: list[dict[str, Any]] = []
        for row in rows:
            job_id = str(row["id"])
            session_id = str(row["session_id"] or "")
            task_id = str(row["task_id"] or "").strip()
            created_at = float(row["created_at"] or now)
            if not task_id:
                self._update_job(job_id, status="failed", error="missing_task_id")
                continue
            if now - created_at > self.modelscope_timeout + 30.0:
                self._update_job(job_id, status="failed", error="modelscope_timeout")
                continue
            if not self._lock.acquire(blocking=False):
                continue
            try:
                result = client.poll(task_id)
                if result.get("status") != "succeeded":
                    self.db.execute(
                        "UPDATE photo_jobs SET updated_at=? WHERE id=?",
                        (now, job_id),
                    )
                    continue
                raw = client.download(str(result["url"]))
                target = self.output_dir / f"{job_id}{_image_extension(raw)}"
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(raw)
                os.replace(tmp, target)
                self._update_job(job_id, status="done", image_path=str(target))
                completed.append(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "session_id": session_id,
                        "path": str(target),
                        "caption": str(row["caption"] or ""),
                        "reply_to": str(row["reply_to"] or ""),
                    }
                )
            except ModelScopePhotoError as exc:
                self._update_job(job_id, status="failed", error=exc.error_text)
                completed.append(
                    {
                        "ok": False,
                        "job_id": job_id,
                        "session_id": session_id,
                        "error": exc.error_text,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self._update_job(
                    job_id,
                    status="failed",
                    error=f"{type(exc).__name__}:{str(exc)[:160]}",
                )
                completed.append(
                    {
                        "ok": False,
                        "job_id": job_id,
                        "session_id": session_id,
                        "error": f"{type(exc).__name__}:{str(exc)[:160]}",
                    }
                )
            finally:
                self._lock.release()
        return completed

    def _remote_job(
        self, photo: PhotoPrompt, job_id: str, seed: int | None
    ) -> tuple[str | None, str | None]:
        """Run the remote job; return ``(error_code, path)``."""
        client = self.remote_client
        if client is None:
            return "remote_not_configured", None
        try:
            reachable = bool(client.reachable())
        except Exception:
            reachable = False
        if not reachable:
            return "home_offline", None

        payload: dict[str, Any] = {
            "prompt": photo.positive,
            "negative": photo.negative,
            "job_id": job_id,
            "width": self.width,
            "height": self.height,
        }
        if seed is not None:
            payload["seed"] = int(seed)
        try:
            status, body, _headers = client.post(payload)
        except TimeoutError:
            return "remote_timeout", None
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                return "remote_timeout", None
            return "home_offline", None

        if status != 200:
            # 与 sing 侧同一套约定：家里在 JSON 里给的 ``error`` 比 HTTP 码更精确
            # （``409 {"error":"vram_busy"}`` 才能落到「显卡正忙着」那句人话），
            # 拿不到才退回 ``remote_error:<status>``。
            code = ""
            try:
                data = json.loads(bytes(body or b"").decode("utf-8", errors="replace"))
            except ValueError:
                data = None
            if isinstance(data, dict):
                code = str(data.get("error") or "").strip()[:120]
            return (code or f"remote_error:{status}"), None
        raw = bytes(body or b"")
        if len(raw) < _MIN_PHOTO_BYTES or not raw.startswith(_PNG_MAGIC):
            return "remote_bad_photo", None
        target = self.output_dir / f"{job_id}.png"
        try:
            target.write_bytes(raw)
        except OSError:
            return "photo_storage_error", None
        self._update_job(job_id, status="done", image_path=str(target))
        return None, str(target)

    def build_graph(
        self,
        positive: str,
        negative: str,
        *,
        seed: int | None = None,
        job_id: str = "",
    ) -> dict[str, Any]:
        """ComfyUI API graph for one 1024px Anima Turbo photo."""
        seed = (
            int(seed)
            if seed is not None
            else int.from_bytes(os.urandom(4), "big") % 2_147_483_647
        )
        prefix = f"fatfish_photo/{job_id or 'photo'}"
        return {
            "10": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": self.clip,
                    "type": "stable_diffusion",
                    "device": "default",
                },
            },
            "11": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": self.vae},
            },
            "12": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": self.width,
                    "height": self.height,
                    "batch_size": 1,
                },
            },
            "13": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": self.unet,
                    "weight_dtype": "default",
                },
            },
            "20": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": positive, "clip": ["10", 0]},
            },
            "21": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative, "clip": ["10", 0]},
            },
            "40": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": ["13", 0],
                    "lora_name": self.lora,
                    "strength_model": self.lora_strength,
                },
            },
            "50": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": self.steps,
                    "cfg": self.cfg,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "denoise": 1,
                    "model": ["40", 0],
                    "positive": ["20", 0],
                    "negative": ["21", 0],
                    "latent_image": ["12", 0],
                },
            },
            "60": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["50", 0], "vae": ["11", 0]},
            },
            "70": {
                "class_type": "SaveImage",
                "inputs": {
                    "images": ["60", 0],
                    "filename_prefix": prefix,
                },
            },
        }

    # ----------------------------------------------------------- prompt text
    def _fallback_prompt(
        self, request: str, life: dict[str, Any]
    ) -> PhotoPrompt:
        scene = ""
        for keyword, description in _KEYWORD_SCENES:
            if keyword in request:
                scene = description
                break
        if not scene:
            activity = str(life.get("activity") or life.get("current_activity") or "")
            scene = _ACTIVITY_SCENES.get(
                activity, "cozy room, soft daylight, casual clothes"
            )
        mood = str(life.get("mood") or "calm").lower()
        expression = _MOOD_EXPRESSIONS.get(mood, "gentle smile")
        composition = _choose_composition(request)
        outfit = _choose_outfit(request)
        outfit_detail = _outfit_detail(request, outfit)
        if composition == "scene":
            positive = (
                f"{STYLE_TAGS}, {scene}, {CHARACTER_TAGS}, {outfit_detail}, "
                f"selfie, looking at viewer, upper body, smartphone camera, {expression}"
            )
        else:
            positive = (
                f"{STYLE_TAGS}, {CHARACTER_TAGS}, {outfit_detail}, "
                f"selfie, looking at viewer, upper body, from above, "
                f"smartphone camera, {expression}, {scene}"
            )
        return PhotoPrompt(
            positive=positive,
            negative=DEFAULT_NEGATIVE,
            caption=DEFAULT_CAPTION,
            source="fallback",
            composition=composition,
            outfit=outfit,
            outfit_detail=outfit_detail,
        )

    def _ensure_character_tags(self, positive: str) -> str:
        low = positive.lower()
        missing = [tag for tag in CHARACTER_TAG_LIST if tag not in low]
        if missing:
            positive = ", ".join(missing) + ", " + positive
        if "masterpiece" not in positive.lower():
            positive = f"{STYLE_TAGS}, {positive}"
        return positive

    def _with_modelscope_trigger(self, photo: PhotoPrompt) -> PhotoPrompt:
        """Prepend the LoRA trigger word (``whalegirl``) exactly once."""
        trigger = self.modelscope_trigger
        if not trigger or trigger.lower() in photo.positive.lower():
            return photo
        return PhotoPrompt(
            positive=f"{trigger}, {photo.positive}",
            negative=photo.negative,
            caption=photo.caption,
            source=photo.source,
            composition=photo.composition,
            outfit=photo.outfit,
            outfit_detail=photo.outfit_detail,
        )

    @staticmethod
    def _merge_negative(negative: str) -> str:
        extra = negative.strip()
        if not extra:
            return DEFAULT_NEGATIVE
        if "worst quality" in extra.lower():
            return extra
        return f"{extra}, {DEFAULT_NEGATIVE}"

    @staticmethod
    def _clean_request(text: str) -> str:
        raw = _CQ_RE.sub(" ", str(text or ""))
        raw = _CONTROL_RE.sub("", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw[:400]

    @staticmethod
    def _clean_prompt_text(value: Any) -> str:
        text = _CONTROL_RE.sub("", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip().strip('"').strip("'")
        return text[:1200]

    @staticmethod
    def _clean_caption(value: Any) -> str:
        text = _CONTROL_RE.sub("", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:60]

    def _life_state(self, context: dict[str, Any] | None) -> dict[str, Any]:
        raw = (context or {}).get("life")
        if isinstance(raw, dict):
            return raw
        if raw is not None:
            return {
                "mood": getattr(raw, "mood", ""),
                "energy": getattr(raw, "energy", 0),
                "phase": getattr(raw, "phase", ""),
                "activity": getattr(raw, "current_activity", ""),
            }
        if self.life is not None and hasattr(self.life, "state"):
            try:
                state = self.life.state()
                return {
                    "mood": getattr(state, "mood", ""),
                    "energy": getattr(state, "energy", 0),
                    "phase": getattr(state, "phase", ""),
                    "activity": getattr(state, "current_activity", ""),
                }
            except Exception:
                pass
        return {}

    @staticmethod
    def _format_life(life: dict[str, Any]) -> str:
        if not life:
            return "unknown"
        return (
            f"mood={life.get('mood', 'calm')}, "
            f"energy={life.get('energy', 70)}, "
            f"phase={life.get('phase', 'idle')}, "
            f"activity={life.get('activity') or life.get('current_activity') or 'idle'}"
        )

    # ------------------------------------------------------------- jobs table
    def _insert_job(
        self,
        job_id: str,
        session_id: str,
        now: float,
        photo: PhotoPrompt,
        *,
        provider: str = "local",
        task_id: str | None = None,
        request_text: str = "",
        status: str = "running",
        reply_to: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO photo_jobs(id, session_id, created_at, status, "
            "provider, task_id, request_text, updated_at, positive, negative, "
            "caption, prompt_source, reply_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                session_id[:120],
                float(now),
                status,
                provider,
                task_id,
                request_text[:500],
                float(now),
                photo.positive,
                photo.negative,
                photo.caption,
                photo.source,
                str(reply_to or "")[:200],
            ),
        )

    def _update_job(
        self,
        job_id: str,
        *,
        status: str,
        image_path: str | None = None,
        error: str | None = None,
        comfyui_started: bool = False,
        task_id: str | None = None,
    ) -> None:
        try:
            self.db.execute(
                "UPDATE photo_jobs SET status=?, image_path=?, error=?, "
                "finished_at=?, comfyui_started=?, updated_at=?, "
                "task_id=COALESCE(?, task_id) WHERE id=?",
                (
                    status,
                    image_path,
                    (error or "")[:500],
                    time.time(),
                    1 if comfyui_started else 0,
                    time.time(),
                    task_id,
                    job_id,
                ),
            )
        except sqlite3.Error:
            pass

    # ------------------------------------------------------ ComfyUI lifecycle
    def _comfyui_up(self) -> bool:
        try:
            self._get_json("/system_stats", timeout=2.0)
            return True
        except PhotoError:
            return False

    def _ensure_comfyui(self) -> tuple[bool, str | None, bool]:
        """Return ``(ok, error, started_by_us)``."""
        if self._comfyui_up():
            return True, None, False
        if not self.autostart:
            return False, "comfyui_not_running", False

        missing = self._missing_paths()
        if missing:
            return False, "paths_missing:" + ",".join(missing), False

        # A shared service manager may own the lifecycle; use it if present.
        handled, _ = self._manager_call(("ensure_comfyui", "start_comfyui"))
        if handled:
            if self._wait_for_comfyui(min(30.0, self.startup_timeout)):
                return True, None, False
            # fall through to our own launcher only if the manager did nothing.

        assets = self.asset_report()
        broken = [name for name in ("unet", "clip", "vae", "lora") if assets.get(name) is False]
        if broken:
            return False, "missing_assets:" + ",".join(broken), False

        if not self._vram_available():
            return False, "vram_conflict_tts", False

        if not self._spawn_comfyui():
            return False, "comfyui_spawn_error", False
        if not self._wait_for_comfyui(self.startup_timeout):
            proc = self._proc
            exited = proc is not None and proc.poll() is not None
            tail = self._log_tail(400)
            self._stop_comfyui()
            detail = tail if tail else "no_log_tail"
            code = "comfyui_start_failed" if exited else "comfyui_start_timeout"
            return False, code + ":" + detail[:300], False
        return True, None, True

    def _missing_paths(self) -> list[str]:
        missing: list[str] = []
        if not self.python or not Path(self.python).is_file():
            missing.append("python")
        if not self.main or not Path(self.main).is_file():
            missing.append("main")
        if not self.cwd or not Path(self.cwd).is_dir():
            missing.append("cwd")
        return missing

    def _spawn_comfyui(self) -> bool:
        bind = self._local_bind()
        if bind is None:
            return False
        host, port = bind
        args = [
            self.python,
            self.main,
            "--listen",
            host,
            "--port",
            str(port),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._log_handle = (self.log_dir / "comfyui-photo.log").open(
                "wb", buffering=0
            )
            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            self._proc = subprocess.Popen(
                args,
                cwd=self.cwd,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                env=env,
            )
            self._proc_started_by_us = True
            return True
        except (OSError, ValueError):
            self._close_log()
            self._proc = None
            self._proc_started_by_us = False
            return False

    def _wait_for_comfyui(self, timeout: float) -> bool:
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if self._comfyui_up():
                return True
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(min(1.0, self.poll_seconds))
        return False

    def _stop_comfyui(self) -> bool:
        proc = self._proc
        if proc is None or not self._proc_started_by_us:
            return False
        stopped = False
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            stopped = True
        except (OSError, ValueError):
            stopped = False
        finally:
            self._proc = None
            self._proc_started_by_us = False
            self._close_log()
        return stopped

    def stop(self) -> bool:
        """Stop ComfyUI only when this service started it."""
        if not _PROCESS_JOB_LOCK.acquire(blocking=False):
            return False
        try:
            if not self._lock.acquire(blocking=False):
                return False
            try:
                return self._stop_comfyui()
            finally:
                self._lock.release()
        finally:
            _PROCESS_JOB_LOCK.release()

    def _close_log(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _log_tail(self, limit: int) -> str:
        path = self.log_dir / "comfyui-photo.log"
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        text = data[-max(1, limit * 4):].decode("utf-8", errors="ignore")
        text = _ANSI_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[-limit:]

    def _local_bind(self) -> tuple[str, int] | None:
        parts = urlsplit(self.base_url)
        host = (parts.hostname or "").strip().lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return None
        if parts.scheme not in {"http", ""}:
            return None
        port = parts.port or 80
        return ("127.0.0.1" if host == "localhost" else host), port

    # ---------------------------------------------------------- VRAM / TTS
    def _vram_available(self) -> bool:
        """True when ComfyUI can start without silently killing TTS."""
        free = self._gpu_free_mb()
        if free is not None and free >= self.min_free_vram_mb:
            return True
        if not self._tts_reachable():
            # No TTS to release.  Without nvidia-smi we cannot know better
            # than the configured timeout, so allow the attempt.
            return free is None
        loaded = self._tts_loaded()
        if loaded is False:
            return free is None
        if self.unload_tts and self._try_unload_tts():
            free = self._gpu_free_mb()
            if free is None or free >= self.min_free_vram_mb:
                return True
        if self.stop_tts_process and self._stop_tts_process():
            free = self._gpu_free_mb()
            return free is None or free >= self.min_free_vram_mb
        return False

    def _stop_tts_process(self) -> bool:
        """Stop the TTS process cooperatively, not by killing arbitrary PIDs.

        When the supervisor is present it must do the killing: a process this
        service spawned by hand would be owned by nobody, Ctrl+C would not
        reclaim it, and the Services panel would keep offering "start".
        """
        handled, job = self._manager_call(("stop_and_wait",), ("tts", 40.0))
        if handled and isinstance(job, dict) and job.get("state") == "done":
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if not self._tts_reachable():
                    self._tts_process_stopped = True
                    return True
                time.sleep(0.2)
        if not self._tts_reachable():
            return False
        stop_bat = self._root / "scripts" / "stop_tts_soft.bat"
        if not stop_bat.is_file():
            return False
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["cmd", "/c", str(stop_bat)],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not self._tts_reachable():
                self._tts_process_stopped = True
                return True
            time.sleep(0.5)
        return False

    def _restart_tts_process(self) -> bool:
        """Start the lazy TTS server again; it loads on the next request."""
        # Let the supervisor respawn its own child when it is available, so the
        # restarted process stays in the ownership tree.  Only fall back to a
        # manual Popen when running without a control plane.
        handled, job = self._manager_call(("start_and_wait",), ("tts", 60.0))
        if (
            handled
            and isinstance(job, dict)
            and job.get("state") == "done"
            and self._tts_reachable()
        ):
            self._tts_process_stopped = False
            return True
        if self._tts_proc is not None and self._tts_proc.poll() is None:
            return True
        if self._tts_reachable():
            self._tts_process_stopped = False
            return True
        if not Path(self.tts_python).is_file() or not Path(self.tts_script).is_file():
            return False
        log = self.log_dir / "tts-restart.log"
        env = dict(os.environ)
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            handle = log.open("ab")
            proc = subprocess.Popen(
                [
                    self.tts_python,
                    self.tts_script,
                    "--lazy-load",
                    "--idle-unload-seconds",
                    str(self.tts_idle_seconds),
                ],
                cwd=str(self._root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
        except (OSError, ValueError):
            return False
        self._tts_proc = proc
        # Make the fallback visible to stop_tts_soft.bat and to the next
        # supervisor boot; the server overwrites this with the same PID once it
        # binds.
        if proc.pid:
            try:
                self._tts_pid_path.parent.mkdir(parents=True, exist_ok=True)
                self._tts_pid_path.write_text(str(proc.pid), encoding="ascii")
            except OSError:
                pass
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if self._tts_reachable():
                self._tts_process_stopped = False
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def _tts_base(self) -> tuple[str, str, int] | None:
        raw = str(self.config.get("tts.base_url", "") or "").strip()
        if not raw:
            return None
        parts = urlsplit(raw)
        host = (parts.hostname or "").strip().lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return None
        scheme = parts.scheme or "http"
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, ("127.0.0.1" if host == "localhost" else host), port

    def _tts_reachable(self) -> bool:
        target = self._tts_base()
        if target is None:
            return False
        _, host, port = target
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            return False

    def _tts_health(self) -> dict[str, Any] | None:
        target = self._tts_base()
        if target is None:
            return None
        scheme, host, port = target
        url = f"{scheme}://{host}:{port}/health"
        try:
            data = self._get_json_url(url, timeout=2.0)
        except PhotoError:
            return None
        return data if isinstance(data, dict) else None

    def _tts_loaded(self) -> bool | None:
        health = self._tts_health()
        if health is None:
            return None
        value = health.get("loaded")
        return value if isinstance(value, bool) else None

    def _try_unload_tts(self) -> bool:
        """Cooperative TTS unload; never kills or restarts the TTS service."""
        handled, job = self._manager_call(("unload_and_wait",), ("tts", 12.0))
        if (
            handled
            and isinstance(job, dict)
            and job.get("state") == "done"
            and self._wait_for_tts_unloaded(8.0)
        ):
            self._tts_unloaded_this_job = True
            return True

        target = self._tts_base()
        if target is None:
            return False
        scheme, host, port = target
        url = f"{scheme}://{host}:{port}/unload"
        try:
            data = self._post_json_url(url, {}, timeout=15.0)
        except PhotoError:
            return False
        if not isinstance(data, dict):
            return False
        if self._wait_for_tts_unloaded(8.0):
            self._tts_unloaded_this_job = True
            return True
        return self._tts_loaded() is False

    def _wait_for_tts_unloaded(self, timeout: float) -> bool:
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline:
            if self._tts_loaded() is False:
                return True
            free = self._gpu_free_mb()
            if free is not None and free >= self.min_free_vram_mb:
                return True
            time.sleep(0.5)
        return False

    def _gpu_free_mb(self) -> int | None:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        values = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                values.append(int(line))
        return min(values) if values else None

    # ------------------------------------------------------ service manager
    def _manager_call(
        self,
        names: tuple[str, ...],
        args: tuple[Any, ...] = (),
    ) -> tuple[bool, Any]:
        manager = self.service_manager
        if manager is None:
            return False, None
        for name in names:
            method = getattr(manager, name, None)
            if not callable(method):
                continue
            try:
                return True, method(*args)
            except TypeError:
                try:
                    return True, method()
                except Exception:
                    continue
            except Exception:
                continue
        return False, None

    # -------------------------------------------------------------- HTTP/API
    def _get_json(
        self, path: str, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        return self._get_json_url(self.base_url + path, timeout=timeout)

    def _get_json_url(self, url: str, *, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            raise PhotoError("http_error", f"{exc.code}:{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PhotoError("connection_error", type(exc).__name__) from exc
        return _decode_json(body)

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return self._post_json_url(
            self.base_url + path, payload, timeout=timeout
        )

    def _post_json_url(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            raise PhotoError("http_error", f"{exc.code}:{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PhotoError("connection_error", type(exc).__name__) from exc
        return _decode_json(body)

    def _submit(self, graph: dict[str, Any]) -> str:
        try:
            data = self._post_json(
                "/prompt",
                {"prompt": graph, "client_id": "fatfish-photo"},
                timeout=60.0,
            )
        except PhotoError as exc:
            raise PhotoError("comfyui_submit_error", exc.detail) from exc
        node_errors = data.get("node_errors")
        if isinstance(node_errors, dict) and node_errors:
            detail = json.dumps(node_errors, ensure_ascii=False)[:300]
            raise PhotoError("comfyui_execution_error", detail)
        prompt_id = str(data.get("prompt_id") or "").strip()
        if not prompt_id:
            raise PhotoError("comfyui_execution_error", "missing_prompt_id")
        return prompt_id

    def _wait_for_image(
        self, prompt_id: str, started: float
    ) -> dict[str, Any]:
        quoted = urllib.parse.quote(prompt_id, safe="")
        while True:
            if time.time() - started > self.timeout:
                raise PhotoError("comfyui_timeout")
            try:
                history = self._get_json(
                    f"/history/{quoted}", timeout=10.0
                )
            except PhotoError as exc:
                if exc.code == "http_error" and exc.detail.startswith("404"):
                    time.sleep(self.poll_seconds)
                    continue
                raise PhotoError("comfyui_execution_error", exc.detail) from exc
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status")
                if isinstance(status, dict):
                    state = str(status.get("status_str") or "").lower()
                    if state == "error":
                        detail = json.dumps(
                            status.get("messages") or [], ensure_ascii=False
                        )[:300]
                        raise PhotoError("comfyui_execution_error", detail)
                    if status.get("completed") is True or state == "success":
                        image = _first_image(entry.get("outputs"))
                        if image is None:
                            raise PhotoError("comfyui_no_image")
                        return image
                image = _first_image(entry.get("outputs"))
                if image is not None:
                    return image
            time.sleep(self.poll_seconds)

    def _download_image(
        self, image: dict[str, Any], target: Path
    ) -> None:
        filename = str(image.get("filename") or "").strip()
        if not filename:
            raise PhotoError("comfyui_no_image")
        query = urllib.parse.urlencode(
            {
                "filename": filename,
                "subfolder": str(image.get("subfolder") or ""),
                "type": str(image.get("type") or "output"),
            }
        )
        url = f"{self.base_url}/view?{query}"
        try:
            with urllib.request.urlopen(url, timeout=60.0) as resp:
                body = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PhotoError("download_failed", type(exc).__name__) from exc
        if not body:
            raise PhotoError("download_failed", "empty_body")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise PhotoError("download_failed", "write_failed") from exc


# --------------------------------------------------------------------- utils
def discover_service_manager(token_file: str | Path | None = None) -> Any | None:
    """Find an optional shared service manager without hard dependency.

    The integration is duck-typed: a manager may expose ``ensure_comfyui`` /
    ``start_comfyui`` for lazy start and ``unload_tts`` /
    ``ensure_tts_unloaded`` for cooperative VRAM release.
    """
    try:
        module = importlib.import_module("fatfish.services")
    except Exception:
        return None
    for name in ("get_service_manager", "service_manager", "manager"):
        value = getattr(module, name, None)
        if value is None:
            continue
        if callable(value) and name == "get_service_manager":
            try:
                value = value(token_file) if token_file is not None else value()
            except TypeError:
                # A duck-typed manager with the old zero-argument signature.
                try:
                    value = value()
                except Exception:
                    continue
            except Exception:
                continue
        if value is not None:
            return value
    return None


def _json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _decode_json(body: bytes) -> dict[str, Any]:
    if not body.strip():
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PhotoError("invalid_json") from exc
    if not isinstance(data, dict):
        raise PhotoError("invalid_json")
    return data


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="ignore")[:300]
    except Exception:
        return ""
    finally:
        try:
            exc.close()
        except Exception:
            pass


def _first_image(outputs: Any) -> dict[str, Any] | None:
    if not isinstance(outputs, dict):
        return None
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        images = node.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and image.get("filename"):
                    return image
    return None
