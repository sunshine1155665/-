"""QQ 空间扫码登录（ptlogin2 ``ptqrshow`` / ``ptqrlogin``），stdlib only.

流程
----
1. ``generate()``：GET ``ssl.ptlogin2.qq.com/ptqrshow`` 拿二维码图片字节，
   落盘 ``data/qq_login_qr.png``；响应携带的 ``qrsig`` 会话 cookie 保存在
   本实例私有的内存 CookieJar（会话隔离：每个 :class:`QrLogin` 实例一套）。
2. ``poll_step()``：GET ``ptqrlogin``（``ptqrtoken = hash33(qrsig)``），
   解析 ``ptuiCB(...)`` 回调的状态码。STATE 7（提交参数错误）等参数类失败
   会自动降级到下一个候选参数组合（见 ``PARAM_CANDIDATES``）。
   T-0021 加固：HTTP 403/429 是 WAF 级拦截（与参数无关）→ ``waf_cooldown``
   状态 + 指数退避（首拦 ≥5 分钟，300→600→1200，三拦放弃），退避窗内
   绝不再打 ptqrlogin；扫码确认后 20 秒宽限期内不换码、不重置参数阶梯
   （手机点确认后约 5 秒登录才完成，期间的 65/7 多为风控噪声）。
3. ``accept_success()``：跟随 ``check_sig`` 重定向链截获 ``uin`` / ``skey``，
   再补一跳 ``user.qzone.qq.com`` 触发 ``p_skey`` 下发；过滤出需要的字段，
   与现有 cookie 合并后经 ``QzoneClient.save_cookies()`` 落盘。

安全红线
--------
* ``qrsig``、cookie 值、``check_sig`` URL（内含 uin）**绝不打印、绝不写日志**。
* 允许出现在输出里的敏感相关信息只有：状态码、字段名、掩码 uin。
* ``poll_step`` 成功路径返回的 result 字典内含 cookie 值与 ``check_url``，
  只供程序内部使用；对外输出一律走 ``run()`` / UI 的白名单文案。

参数候选
--------
所有 ``ptqrlogin`` 参数组合都是候选（离线无法验证真实可达性，见每组注释
中的【待实测】标记）；运行时从第一组开始，遇到参数类失败自动换下一组。
今晚 pre-flight（主对话 + boss 实扫）后回填结论。

测试全程离线 fixture（合成响应头 / 假 opener），本模块不做任何真实请求。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable

PTQRSHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
PTQRLOGIN_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
# check_sig 截获 uin/skey 后再补一跳 qzone，触发 user.qzone.qq.com 域的 p_skey。
CHECK_HOP_URL = "https://user.qzone.qq.com/"

DEFAULT_APPID = "549000912"  # Qzone web 登录 appid（公开资料广泛记载）
DEFAULT_DAID = "5"
# 备用 appid：出现在部分第三方 Qzone 登录工具中。【待实测】
APPID_CANDIDATES: tuple[tuple[str, str], ...] = (
    (DEFAULT_APPID, DEFAULT_DAID),
    ("715030901", DEFAULT_DAID),
)

DEFAULT_U1 = "https://qzs.qq.com/qzone/v5/loginsucc.html?qqlogin"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _xui_referer(appid: str, daid: str) -> str:
    return (
        "https://xui.ptlogin2.qq.com/cgi-bin/xui_2/pc/qrcode.html"
        f"?appid={appid}&daid={daid}&style=8&hide_border=1"
    )


PNG_FILE_NAME = "qq_login_qr.png"

# 扫码成功后需要截获的字段；``uin``/``skey`` 是 QzoneClient 的必需字段，
# ``p_skey`` 决定 g_tk（user.qzone.qq.com 读/写路径）。
WANT_COOKIE_FIELDS = ("uin", "skey", "p_skey", "p_uin", "luin", "ptcz", "qzone_cookie")
REQUIRED_RESULT_FIELDS = ("uin", "skey")

# ptuiCB 状态码 -> 归一化状态。67/68 都是"已扫"语义（不同版本文案：
# 认证中 / 已扫描待确认），统一按已扫码处理。
PTQR_STATUS = {
    "0": "success",
    "65": "expired",
    "66": "waiting",
    "67": "scanned",
    "68": "scanned",
    "7": "param_error",
}

# 4 = 生成二维码失败（常见于 appid 被关/风控），按硬错误处理而非参数降级。
HARD_ERROR_CODES = {"4"}

POLL_INTERVAL_SECONDS = 3.0
MAX_QR_REGENERATIONS = 3
REQUEST_TIMEOUT_SECONDS = 20.0

# ---- WAF / 风控识别（T-0021，2026-10-25 实测：ptqrlogin 对本 IP 返回 HTTP 403
# 空响应，参数无关；当日多次失败轮询加剧动态冷却）----
# 403/429 是墙，不是参数错误也不是码过期：换参数、换码都只会打得更疼。
WAF_STATUS_CODES = frozenset({403, 429})
# 第一个 403 后 ≥5 分钟内绝不再打 ptqrlogin（boss 实测冷却量级）。
WAF_BACKOFF_BASE_SECONDS = 300.0
WAF_BACKOFF_MAX_SECONDS = 1800.0
# 连续第 N 次进入冷却就放弃，让用户改用兜底方案（浏览器粘 cookie）。
MAX_WAF_STRIKES = 3

# 扫码确认后的风控宽限（boss 情报：手机点确认后约 5 秒登录才真正完成；
# 20 秒覆盖该窗口并留余量——期间禁止换码/重置参数阶梯）。
SCAN_CONFIRM_GRACE_SECONDS = 20.0

_PTUI_CALLBACK_RE = re.compile(
    r"ptuiCB\('(\d+)','[^']*','([^']*)','[^']*','([^']*)'"
)


class QrLoginError(RuntimeError):
    """扫码登录失败（只携带状态码/字段名等可打印信息，绝不带 cookie 值）。"""


class QrLoginHttpError(QrLoginError):
    """HTTP 层错误（区别于网络不可达）：携带可打印的状态码。"""

    def __init__(self, http_code: int):
        super().__init__(f"HTTP {http_code}")
        self.http_code = int(http_code)


# --------------------------------------------------------------------- utils
def hash33(text: str) -> int:
    """ptlogin2 的 ``getAuthorization`` 哈希（``ptqrtoken = hash33(qrsig)``）。

    与 ``client.g_tk_from_skey`` 同族但种子不同：这里从 0 开始累加
    ``hash*32 + ord(ch)``，最后截断到 31 位。
    """
    value = 0
    for char in str(text or ""):
        value += (value << 5) + ord(char)
    return value & 0x7FFFFFFF


def mask_uin(uin: str) -> str:
    """uin 掩码（前 3 + **** + 后 4）；过短或缺失时整体掩码。"""
    text = str(uin or "").strip()
    if len(text) > 7:
        return f"{text[:3]}****{text[-4:]}"
    return "(masked)"


_PERSON_QQ_ID_RE = re.compile(r"(person:qq:)(\d{4,})")


def mask_uin_text(text: str) -> str:
    """把 ``person:qq:<uin>`` 形态的实体 id 里的 uin 一并掩掉。

    人物卡的主键本身就含 uin，仅掩 ``uin`` 字段挡不住 id 这条路；
    合并来源（``merged_from``）等字段同样可能带别的卡的 id。
    """
    return _PERSON_QQ_ID_RE.sub(lambda m: m.group(1) + mask_uin(m.group(2)), str(text or ""))


def parse_ptui_callback(body: str) -> tuple[str, str, str] | None:
    """解析 ``ptuiCB('code','x','url','x','message',...)``。

    返回 ``(code, url, message)``；无法解析时返回 ``None``。``url`` 在
    code=0 时是 check_sig 地址（含 uin，属敏感信息，调用方不得打印）。
    """
    match = _PTUI_CALLBACK_RE.search(str(body or ""))
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def normalize_uin(uin: str) -> str:
    """ptlogin2 的 uin cookie 形如 ``o1234567890``，去掉前缀 ``o``。"""
    text = str(uin or "").strip()
    if text.startswith("o") and text[1:].isdigit():
        return text[1:]
    return text


def _action_value(style: str, wall_clock: Callable[[], float]) -> str:
    """``action`` 参数：经典 ``0-0-<ms>`` 或二次请求的 ``2-9-<ms>-<rand>``。"""
    millis = int(wall_clock() * 1000)
    if style == "long":
        return f"2-9-{millis}-{random.randint(100000, 999999)}"
    return f"0-0-{millis}"


# --------------------------------------------------- ptqrlogin 参数候选表
# 每组给出 name / params（不含 u1、ptqrtoken、action）/ referer / action_style
# 与来源注释。顺序即降级顺序。全部【待实测】，今晚 pre-flight 回填。
PARAM_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "name": "xui-qrcode-modern",
        # 来源：当前 xui_2/pc/qrcode.html（appid=549000912, daid=5）网络面板
        # 组合，2023-2024 多个开源 Qzone 登录 gist 沿用。注意不含
        # pt_qzone_sig —— 主对话原型带该参数时被 QQ 拒 STATE 7，疑似诱因。
        # 【待实测】
        "params": {
            "ptredirect": "1",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "js_ver": "22121111",
            "js_type": "1",
            "login_sig": "",
            "daid": "5",
            "pt_3rd_aid": "0",
            "pt_uin": "",
        },
        "referer": _xui_referer(DEFAULT_APPID, DEFAULT_DAID),
        "action_style": "short",
    },
    {
        "name": "xui-modern-no-ptuin",
        # 来源：同上页面另一批抓包不带 pt_uin。【待实测】
        "params": {
            "ptredirect": "1",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "js_ver": "22121111",
            "js_type": "1",
            "login_sig": "",
            "daid": "5",
            "pt_3rd_aid": "0",
        },
        "referer": _xui_referer(DEFAULT_APPID, DEFAULT_DAID),
        "action_style": "short",
    },
    {
        "name": "classic-qzone",
        # 来源：xui 之前 qzone 网页登录的经典组合（旧 js_ver，无
        # pt_3rd_aid/pt_uin），长期被开源 qzone cookie 工具使用；即主对话
        # 第二次尝试的那组（Referer 用 xui qrcode 页）。【待实测】
        "params": {
            "ptredirect": "1",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "js_ver": "10145",
            "js_type": "1",
            "login_sig": "",
            "daid": "5",
        },
        "referer": _xui_referer(DEFAULT_APPID, DEFAULT_DAID),
        "action_style": "short",
    },
    {
        "name": "minimal-smartqq",
        # 来源：SmartQQ/web-QQ 系开源客户端的最小参数集（无 daid/js_ver），
        # ptqrlogin 历史上对缺省参数宽容。【待实测】
        "params": {
            "ptredirect": "1",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "login_sig": "",
        },
        "referer": "https://xui.ptlogin2.qq.com/",
        "action_style": "short",
    },
    {
        "name": "xui-action-long",
        # 来源：xui 页面第二次登录请求使用长 action（2-9-<ms>-<rand>）的
        # 抓包样例（社区讨论）。【待实测】
        "params": {
            "ptredirect": "1",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "js_ver": "22121111",
            "js_type": "1",
            "login_sig": "",
            "daid": "5",
            "pt_3rd_aid": "0",
            "pt_uin": "",
        },
        "referer": _xui_referer(DEFAULT_APPID, DEFAULT_DAID),
        "action_style": "long",
    },
)


class QrLogin:
    """一次扫码登录会话：generate → poll_step → accept_success（run 串起来）。

    Parameters
    ----------
    client:
        具有 ``cookies: dict`` 与 ``save_cookies()`` 的客户端
        （生产环境是 :class:`fatfish.qzone.client.QzoneClient`；测试可注入假体）。
    config:
        未给 ``client`` 时用它构建 ``QzoneClient.from_config(config)``。
    opener / jar:
        测试注入；生产环境默认内存 CookieJar + HTTPCookieProcessor，
        会话隔离在实例内部，qrsig 不落盘。
    """

    def __init__(
        self,
        client: Any = None,
        config: Any = None,
        *,
        png_path: str | Path | None = None,
        opener: Any = None,
        jar: CookieJar | None = None,
        u1: str = DEFAULT_U1,
        user_agent: str = DEFAULT_USER_AGENT,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        candidates: tuple[dict[str, Any], ...] = PARAM_CANDIDATES,
        wall_clock: Callable[[], float] = time.time,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if client is None:
            if config is None:
                raise QrLoginError("QrLogin 需要 client 或 config 之一")
            from .client import QzoneClient

            client = QzoneClient.from_config(config)
        self.client = client
        self.u1 = u1
        self.user_agent = user_agent
        self.poll_interval = max(0.5, float(poll_interval))
        self.request_timeout = max(1.0, float(request_timeout))
        self._candidates = tuple(candidates) or PARAM_CANDIDATES
        self._candidate_index = 0
        self._wall_clock = wall_clock
        self._clock = clock
        self._sleep = sleep
        if opener is None:
            self._jar = jar if jar is not None else CookieJar()
            self._opener: Any = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._jar)
            )
        else:
            self._opener = opener
            self._jar = jar if jar is not None else getattr(opener, "cookiejar", None)
        if png_path is not None:
            self.png_path = Path(png_path)
        else:
            data_dir = getattr(client, "data_dir", None) or Path("data")
            self.png_path = Path(data_dir) / PNG_FILE_NAME
        # 一次成功登录截获的字段（含值，仅供内部/测试断言；不得打印）。
        self.last_captured: dict[str, str] = {}
        # T-0021 加固状态：WAF 退避窗 + 扫码确认宽限。
        self._waf_strikes = 0
        self._waf_until: float | None = None  # monotonic 截止；None = 无冷却
        self._scanned_at: float | None = None  # 首见 67/68 的 monotonic 时刻
        self._base_headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    # ---------------------------------------------------------------- session
    @property
    def candidate_name(self) -> str:
        return str(self._candidates[self._candidate_index].get("name"))

    def qrsig(self) -> str | None:
        if self._jar is None:
            return None
        return next(
            (c.value for c in self._jar if c.name == "qrsig" and c.value),
            None,
        )

    def generate(self) -> Path:
        """取新二维码落盘并重置会话；返回 PNG 路径。"""
        last_error: Exception | None = None
        for appid, daid in APPID_CANDIDATES:
            try:
                return self._generate_with_appid(appid, daid)
            except QrLoginError as exc:  # 换下一个 appid 再试
                last_error = exc
        raise QrLoginError(f"获取二维码失败：{last_error}")

    def _generate_with_appid(self, appid: str, daid: str) -> Path:
        query = urllib.parse.urlencode(
            {
                "appid": appid,
                "e": "0",
                "l": "M",
                "s": "3",
                "d": "72",
                "v": "4",
                "daid": daid,
                "t": str(random.random()),
            }
        )
        request = urllib.request.Request(
            f"{PTQRSHOW_URL}?{query}",
            headers={**self._base_headers, "Referer": _xui_referer(appid, daid)},
        )
        payload = self._fetch_bytes(request)
        if not (payload.startswith(b"\x89PNG") or payload.startswith(b"\xff\xd8")):
            raise QrLoginError(
                f"ptqrshow 未返回图片（{len(payload)} 字节），appid {appid} 可能不可用"
            )
        self.png_path.parent.mkdir(parents=True, exist_ok=True)
        self.png_path.write_bytes(payload)
        # 新会话从头开始参数阶梯（STATE 7 降级是会话内的，换码重置）。
        self._candidate_index = 0
        # 换码即新扫码会话：宽限窗随之作废。
        self._scanned_at = None
        return self.png_path

    # ------------------------------------------------- T-0021 hardening helpers
    def in_scan_grace(self) -> bool:
        """扫码确认宽限期内：禁止换码、禁止重置参数阶梯（手机确认后约 5s
        登录才真正完成，此窗口里的 65/7 多为风控噪声而非真实状态）。"""
        if self._scanned_at is None:
            return False
        return (self._clock() - self._scanned_at) < SCAN_CONFIRM_GRACE_SECONDS

    def waf_cooldown_remaining(self) -> float:
        """距冷却结束还有多少秒；0 = 不在冷却。"""
        if self._waf_until is None:
            return 0.0
        return max(0.0, self._waf_until - self._clock())

    def _register_waf_strike(self) -> float:
        """记一次 WAF 拦截并计算指数退避窗（5min → 10min → 20min 封顶）。"""
        self._waf_strikes += 1
        backoff = min(
            WAF_BACKOFF_BASE_SECONDS * (2 ** (self._waf_strikes - 1)),
            WAF_BACKOFF_MAX_SECONDS,
        )
        self._waf_until = self._clock() + backoff
        return backoff

    def _waf_result(self, *, retry_after: float) -> dict[str, Any]:
        return {
            "status": "waf_cooldown",
            "code": 403,
            "message": "腾讯风控冷却中，稍后自动重试；急用可先用浏览器粘 cookie 兜底",
            "candidate": self.candidate_name,
            "retry_after": float(retry_after),
            "strikes": self._waf_strikes,
            "give_up": self._waf_strikes >= MAX_WAF_STRIKES,
        }

    # ------------------------------------------------------------------ poll
    def poll_step(self) -> dict[str, Any]:
        """单步轮询；STATE 7 等参数错误自动降级到下一候选组合。

        返回 dict：
        * ``status``: success / expired / waiting / scanned / param_error /
          waf_cooldown / error / net_retry / unparseable
        * ``code`` / ``message`` / ``candidate``
        * waf_cooldown 额外带 ``retry_after``（秒）、``strikes``、``give_up``
        * 仅 success：``check_url``（敏感，不得打印）与 ``captured``
          （cookie 值，敏感，不得打印）。
        """
        qrsig = self.qrsig()
        if not qrsig:
            raise QrLoginError("尚无二维码会话：请先调用 generate()")
        # 纵深防御：冷却期内无论谁调 poll_step 都不再打 ptqrlogin。
        cooldown = self.waf_cooldown_remaining()
        if cooldown > 0:
            return self._waf_result(retry_after=cooldown)
        token = hash33(qrsig)
        for _ in range(len(self._candidates)):
            candidate = self._candidates[self._candidate_index]
            params = dict(candidate.get("params") or {})
            params["u1"] = self.u1
            params["ptqrtoken"] = str(token)
            params["action"] = _action_value(
                str(candidate.get("action_style") or "short"), self._wall_clock
            )
            request = urllib.request.Request(
                f"{PTQRLOGIN_URL}?{urllib.parse.urlencode(params)}",
                headers={
                    **self._base_headers,
                    "Referer": str(candidate.get("referer") or "https://xui.ptlogin2.qq.com/"),
                },
            )
            try:
                body = self._fetch_text(request)
            except QrLoginHttpError as exc:
                # T-0021: HTTP 403/429 是 WAF 级拦截（实测与参数无关），
                # 换参数/换码只会加剧冷却——进入指数退避。
                if exc.http_code in WAF_STATUS_CODES:
                    backoff = self._register_waf_strike()
                    return self._waf_result(retry_after=backoff)
                return {
                    "status": "net_retry",
                    "code": exc.http_code,
                    "message": f"HTTP {exc.http_code}",
                    "candidate": self.candidate_name,
                }
            except QrLoginError as exc:
                # _fetch_bytes 已把 URLError/超时包装成 QrLoginError；
                # 轮询对网络错误按可重试处理，不降级参数。
                return {
                    "status": "net_retry",
                    "code": -1,
                    "message": type(exc).__name__,
                    "candidate": self.candidate_name,
                }
            parsed = parse_ptui_callback(body)
            if parsed is None:
                return {
                    "status": "unparseable",
                    "code": -1,
                    "message": "ptqrlogin 响应无法解析",
                    "candidate": self.candidate_name,
                }
            code, check_url, message = parsed
            status = PTQR_STATUS.get(code, "error")
            if status == "scanned" and self._scanned_at is None:
                self._scanned_at = self._clock()
            if status == "param_error":
                if self.in_scan_grace():
                    # 扫码确认窗口内的 STATE 7 多为风控噪声：换参数组合
                    # 会作废手机上正在确认的会话，宁可信扫码状态。
                    return {
                        "status": "param_error",
                        "code": int(code),
                        "message": "扫码确认宽限期内忽略参数错误",
                        "candidate": self.candidate_name,
                        "in_grace": True,
                    }
                # 参数错误 → 换下一候选组合，本轮最多把阶梯走完一遍。
                self._candidate_index = (self._candidate_index + 1) % len(self._candidates)
                continue
            if status == "success":
                self._waf_strikes = 0
                self._waf_until = None
                if not check_url:
                    return {
                        "status": "error",
                        "code": int(code),
                        "message": "ptqrlogin 返回成功但缺少 check_sig 地址",
                        "candidate": self.candidate_name,
                    }
                captured = self.accept_success(check_url)
                return {
                    "status": "success",
                    "code": 0,
                    "message": message,
                    "candidate": self.candidate_name,
                    "check_url": check_url,
                    "captured": captured,
                }
            if code in HARD_ERROR_CODES:
                status = "error"
            return {
                "status": status,
                "code": int(code),
                "message": message,
                "candidate": self.candidate_name,
            }
        return {
            "status": "param_error",
            "code": 7,
            "message": "全部候选参数组合均被 QQ 拒绝",
            "candidate": self.candidate_name,
        }

    # --------------------------------------------------------------- capture
    def accept_success(self, check_url: str) -> dict[str, str]:
        """跟随 check_sig 截获 cookie，过滤/合并后经 client.save_cookies 落盘。

        check_sig 链在 ``.qq.com`` 域下发 ``uin`` / ``skey``；补一跳
        ``user.qzone.qq.com`` 触发 ``p_skey``。任何一步网络失败都容忍
        （cookie 可能已由重定向链写入 jar），只有必需字段缺失才报错。
        """
        if not check_url:
            raise QrLoginError("缺少 check_sig 地址，无法截获 cookie")
        try:
            self._fetch_bytes(
                urllib.request.Request(
                    check_url,
                    headers={**self._base_headers, "Referer": PTQRLOGIN_URL},
                )
            )
        except QrLoginError:
            pass  # 容忍：重定向链可能已部分写入 cookie
        try:
            self._fetch_bytes(urllib.request.Request(CHECK_HOP_URL, headers=self._base_headers))
        except QrLoginError:
            pass  # p_skey 可能在 check_sig 阶段已下发

        captured: dict[str, str] = {}
        if self._jar is not None:
            for cookie in self._jar:
                if cookie.name in WANT_COOKIE_FIELDS and cookie.value:
                    captured[cookie.name] = cookie.value
        uin = normalize_uin(captured.get("uin", ""))
        if uin:
            captured["uin"] = uin
        missing = [name for name in REQUIRED_RESULT_FIELDS if not captured.get(name)]
        if missing:
            raise QrLoginError(
                "扫码成功但未截获必需字段：" + ", ".join(missing)
            )
        cookies = getattr(self.client, "cookies", None)
        if not isinstance(cookies, dict):
            raise QrLoginError("client 不支持 cookie 合并（缺少 cookies 字典）")
        # 截获字段覆盖旧值；旧文件里未被截获的字段（如历史 ptcz）保留。
        cookies.update(captured)
        self.client.save_cookies()
        self.last_captured = dict(captured)
        return captured

    # ------------------------------------------------------------------- run
    STATUS_LINES = {
        "waiting": "等待扫码…",
        "scanned": "已扫码，请在手机上确认",
        "net_retry": "网络波动，继续等待…",
        "unparseable": "登录服务响应异常，继续等待…",
        "param_error": "QQ 拒绝了登录参数（已轮完候选组合），继续重试…",
    }

    def _status_line(self, result: dict[str, Any]) -> str:
        """白名单文案；waf_cooldown 按剩余分钟动态生成。"""
        status = str(result.get("status") or "error")
        if status == "waf_cooldown":
            minutes = max(1, int(float(result.get("retry_after") or 0) // 60))
            suffix = "；多次拦截，建议改天再试或用浏览器粘 cookie 兜底" if result.get("give_up") else "；急用可先用浏览器粘 cookie 兜底"
            return f"腾讯风控冷却中（第 {result.get('strikes')} 次拦截），{minutes} 分钟后自动重试{suffix}"
        line = self.STATUS_LINES.get(status)
        if line is not None:
            return line
        code = result.get("code")
        message = str(result.get("message") or "").strip()
        return f"登录状态 {code}：{message}" if message else f"登录状态 {code}"

    def run(
        self,
        *,
        timeout: float = 600.0,
        max_regenerations: int = MAX_QR_REGENERATIONS,
        emit: Callable[[str], None] = print,
        open_image: bool = False,
    ) -> int:
        """完整扫码流程（CLI 兜底路径）。成功返回 0，失败/超时返回 1。

        只输出白名单文案：状态行、字段名、掩码 uin、PNG 路径。
        """
        png = self.generate()
        emit(f"二维码已生成：{png}")
        emit("请用手机 QQ 扫一扫，并在手机上点「登录确认」。")
        if open_image and os.name == "nt":
            try:
                os.startfile(str(png))  # type: ignore[attr-defined]  # noqa: S606
            except OSError:
                pass  # 打不开系统查看器不影响轮询
        deadline = self._clock() + max(0.0, float(timeout))
        regenerations = 0
        last_line = ""

        def note(line: str) -> None:
            nonlocal last_line
            if line != last_line:
                emit(line)
                last_line = line

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                note(f"等待超时（{int(timeout)} 秒），未完成扫码登录。")
                return 1
            try:
                result = self.poll_step()
            except QrLoginError as exc:
                emit(f"登录失败：{exc}")
                return 1
            status = str(result.get("status") or "error")
            if status == "success":
                captured = dict(result.get("captured") or {})
                uin_masked = mask_uin(captured.get("uin", ""))
                note(
                    "登录成功：已保存字段 "
                    + ", ".join(sorted(captured))
                    + f"（uin: {uin_masked}）"
                )
                return 0
            if status == "waf_cooldown":
                note(self._status_line(result))
                if result.get("give_up"):
                    note("风控冷却未解除，已停止轮询；建议换个网络环境再试，或用浏览器粘 cookie 兜底。")
                    return 1
                # 退避窗内绝不再打 ptqrlogin：等满 retry_after（不超过总剩余时间）。
                self._sleep(
                    min(
                        float(result.get("retry_after") or 0.0),
                        max(0.0, deadline - self._clock()),
                    )
                )
                continue
            if status == "expired":
                if self.in_scan_grace():
                    # 手机已确认、~5s 风控延迟未过：换码会作废这次登录。
                    note("二维码显示过期但扫码确认进行中，宽限等待…")
                    self._sleep(self._bounded_interval(deadline))
                    continue
                if regenerations >= max_regenerations:
                    note(f"二维码已连续换新 {regenerations} 张仍未扫码，已停止。")
                    return 1
                regenerations += 1
                png = self.generate()
                note(
                    f"二维码已过期，自动换新码（第 {regenerations}/{max_regenerations} 张）：{png}"
                )
                self._sleep(self._bounded_interval(deadline))
                continue
            note(self._status_line(result))
            self._sleep(self._bounded_interval(deadline))

    def _bounded_interval(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        return min(self.poll_interval, max(0.0, remaining))

    # ------------------------------------------------------------------- io
    def _fetch_bytes(self, request: urllib.request.Request) -> bytes:
        try:
            with self._opener.open(request, timeout=self.request_timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 的子类，必须先接：403/429 的风控语义
            # 与网络不可达完全不同（T-0021）。
            raise QrLoginHttpError(exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise QrLoginError(f"网络错误：{type(exc).__name__}") from exc

    def _fetch_text(self, request: urllib.request.Request) -> str:
        return self._fetch_bytes(request).decode("utf-8", "replace")


def dump_captured_safely(captured: dict[str, str]) -> str:
    """调试用：只输出字段名与掩码 uin 的 JSON（绝不带 cookie 值）。"""
    return json.dumps(
        {"fields": sorted(captured), "uin": mask_uin(captured.get("uin", ""))},
        ensure_ascii=False,
    )
