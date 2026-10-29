"""能力层在线探测：云主机判断「家里那台 GPU 机还在不在」。

为什么需要
----------
上云之后，在线层（QQ/Web/记忆）常驻云主机，能力层（TTS/唱歌/拍照）留在
家里。家里可能关机、睡眠、断网——这时云上如果照常发起合成，每次都要等
满 ``tts.timeout_seconds``（默认 30s）才失败，用户看到的是「半天不回」。

所以这里做一件事：**带 TTL 缓存的健康探测**。探测结果缓存
``remote.health_ttl_seconds``（默认 30s），一次失败只影响一个窗口，不会
每个句子都重新等超时。

设计边界
--------
* 只做 GET，不带 cookie/token，不写任何东西；
* 只探**配置里明确给出的**地址，不做端口扫描；
* 探测失败 = 「不在线」，不是异常：调用方自行降级（文字代替语音）。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 3.0

#: 健康检查默认路径。TTS 软服务（scripts/cosyvoice_soft_server.py）在模型
#: 未加载时也会回 ``/health``，所以它可以当「进程还在」的信号。
DEFAULT_HEALTH_PATH = "/health"

#: 这些主机名说明「服务就在本机」，不需要（也不应该）为它建远程探针。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def home_url_from_tts(base_url: str) -> str:
    """从 ``tts.base_url`` 推导家里那台机器的根地址（云端专用）。

    云端开语音时只配了 ``[tts] base_url = "https://host:9443/tts"``，
    很容易忘记再配一行 ``[remote] home_url``——后果是家里关机时每一句都要
    等满 ``tts.timeout_seconds`` 才失败（2026-10-30 云端实测 20s）。
    这里按 ``scheme://host:port`` 取根，拼默认的 ``/health`` 就能判活。

    本机 profile 的 ``base_url`` 是 loopback，返回空串，行为完全不变。
    """
    parts = urlsplit(str(base_url or "").strip())
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return ""
    scheme = parts.scheme or "http"
    return f"{scheme}://{parts.netloc}"


@dataclass
class ProbeResult:
    ok: bool
    checked_at: float
    error: str | None = None


@dataclass
class HealthProbe:
    """一组能力端点的健康探测（带缓存）。

    ``endpoints`` 形如 ``{"tts": "http://100.64.0.5:9880/health"}``。地址里
    不要放 token：这里只 GET，且结果会出现在状态输出里。

    ``optional`` 里的端点只是**能力位**，不参与 :meth:`home_online`：拍照那台
    服务没起，不代表家里整机离线，语音不该因此被降级。
    """

    endpoints: dict[str, str]
    ttl: float = DEFAULT_TTL_SECONDS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    clock: Callable[[], float] = time.monotonic
    opener: Callable[[urllib.request.Request, float], Any] | None = None
    optional: set[str] = field(default_factory=set)
    _cache: dict[str, ProbeResult] = field(default_factory=dict)

    # -- 内部
    def _fetch(self, url: str) -> None:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "fatfish-capability-probe"},
            method="GET",
        )
        if self.opener is not None:
            self.opener(request, self.timeout)
            return
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read(64)

    # -- 对外
    def check(self, name: str, *, force: bool = False) -> ProbeResult:
        url = self.endpoints.get(name)
        now = self.clock()
        if not url:
            return ProbeResult(ok=False, checked_at=now, error="not_configured")
        cached = self._cache.get(name)
        if cached is not None and not force and now - cached.checked_at < self.ttl:
            return cached
        try:
            self._fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            result = ProbeResult(ok=False, checked_at=now, error=f"{type(exc).__name__}")
        else:
            result = ProbeResult(ok=True, checked_at=now)
        self._cache[name] = result
        return result

    def home_online(self) -> bool:
        """能力层是否在线：**所有**已配置的必需端点都通才算在线。

        宁可保守——少发一条语音，也不要让每句话都去等超时。
        ``optional`` 里的能力位（拍照）不参与这个判断。
        """
        names = [
            name
            for name, url in self.endpoints.items()
            if url and name not in self.optional
        ]
        if not names:
            return False
        return all(self.check(name).ok for name in names)

    def photo_online(self) -> bool:
        """拍照能力位（T-0068）。没配就当作不在线。"""
        return self.check("photo").ok

    def status(self) -> dict[str, Any]:
        now = self.clock()
        entries: dict[str, Any] = {}
        for name, url in self.endpoints.items():
            result = self.check(name)
            entries[name] = {
                "url": url,
                "ok": result.ok,
                "optional": name in self.optional,
                "age_seconds": round(max(0.0, now - result.checked_at), 1),
                "error": result.error,
            }
        return {
            "home_online": self.home_online(),
            "photo_online": self.photo_online() if "photo" in self.endpoints else None,
            "ttl_seconds": self.ttl,
            "endpoints": entries,
        }


def build_probe(config: Any) -> HealthProbe | None:
    """按配置建探针；没开或没配地址时返回 ``None``（调用方据此跳过检查）。"""
    getter = getattr(config, "get", None)
    if not callable(getter):
        return None
    if not bool(getter("remote.enabled", False)):
        return None
    raw = getter("remote.endpoints", {}) or {}
    endpoints: dict[str, str] = {}
    if isinstance(raw, dict):
        for name, url in raw.items():
            text = str(url or "").strip()
            if text:
                endpoints[str(name)] = text
    # 便捷写法：只给一个 home_url 时，按默认路径拼 TTS 健康地址。
    home_url = str(getter("remote.home_url", "") or "").strip()
    if not home_url:
        # 云端常见漏配：只写了 [tts] base_url，忘了 [remote] home_url。
        # 此时从 base_url 推导，避免「家里关机 → 每句等满 tts.timeout」。
        home_url = home_url_from_tts(str(getter("tts.base_url", "") or ""))
    if home_url and "tts" not in endpoints:
        path = str(getter("remote.tts_health_path", DEFAULT_HEALTH_PATH) or DEFAULT_HEALTH_PATH)
        endpoints["tts"] = home_url.rstrip("/") + "/" + path.lstrip("/")

    # 拍照（T-0068）：云端没有 GPU，`[photo] mode = "remote"` 时家里那台
    # photo_server 才有意义。它是**可选能力位**：服务没起只影响拍照，不能把
    # 「家里在线」判成离线，否则语音也会跟着降级。
    # 没配 remote_url（或 mode 不是 remote）时这里什么都不加——本模块不猜，
    # 由 PhotoService 按自己的 mode 决定走本地 ComfyUI 还是远端；探针只负责
    # 「配了才探，探不到也不连累别人」。
    optional: set[str] = set()
    photo_mode = str(getter("photo.mode", "local") or "local").strip().lower()
    photo_url = str(getter("photo.remote_url", "") or "").strip().rstrip("/")
    if "photo" not in endpoints:
        if photo_mode == "remote" and photo_url:
            path = str(
                getter("photo.remote_health_path", DEFAULT_HEALTH_PATH)
                or DEFAULT_HEALTH_PATH
            )
            endpoints["photo"] = photo_url + "/" + path.lstrip("/")
        else:
            photo_url = ""
    if "photo" in endpoints:
        optional.add("photo")

    if not endpoints:
        return None
    return HealthProbe(
        endpoints=endpoints,
        ttl=max(1.0, float(getter("remote.health_ttl_seconds", DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS)),
        timeout=max(0.5, float(getter("remote.timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS)),
        optional=optional,
    )
