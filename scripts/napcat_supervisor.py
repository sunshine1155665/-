"""NapCat 掉线监管：指数退避重启 + 「凭据失效就停手」。

为什么需要它
------------
2026-10-25 的实测日志（`data/logs/napcat-direct.log`）显示两种完全不同的
"离线"：

1. **会话被服务端踢下线**（`[KickedOffLine] 你的账号当前登录已失效`）。
   紧接着 NapCat 用保存的快速登录凭据重试，得到
   `自动快速登录失败: 你的用户身份已失效`——**令牌和会话一起作废了**。
   这种情况下重启多少次都没用，只会让客户端一直挂在登录页上反复请求二维码。
2. **进程/网络抖动**，快速登录仍有效 —— 重启有价值。

本脚本把这两种情况分开处理：

* 能快速登录 → 按指数退避重启（默认 5 / 15 / 30 / 60 分钟，最多 4 次）；
* 需要人工扫码 → **不重启**，等 `--scan-timeout`（默认 15 分钟）；
  等不到就停掉 NapCat 退出（退出码 2），避免客户端整小时挂在登录页。

它只做监管，不改 NapCat 的任何配置，也不碰 `fatfish/`。

退出码
------
0 正常退出（Ctrl+C / 主动停止）
2 需要人工扫码，且等待超时（扫码后重跑本脚本）
3 重启次数用尽（服务端持续踢，建议先查环境/账号风险）

用法
----
    python scripts/napcat_supervisor.py --uin 9999000003
    python scripts/napcat_supervisor.py --uin 9999000003 --kill-desktop

日志：`data/logs/napcat-supervisor.jsonl`（每行一个事件，含在线时长）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = ROOT / "data" / "logs"
SUPERVISOR_LOG = "napcat-supervisor.jsonl"
NAPCAT_CONSOLE_LOG = "napcat-console.log"
WORKSPACE = ROOT
START_SCRIPT = "scripts\\start_napcat.bat"

#: 指数退避（秒）。用完之后一直用最后一个值。
DEFAULT_BACKOFF = (300.0, 900.0, 1800.0, 3600.0)
DEFAULT_MAX_RESTARTS = 4
DEFAULT_SCAN_TIMEOUT = 900.0
DEFAULT_LOGIN_TIMEOUT = 300.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 状态机
STATE_STARTING = "starting"
STATE_ONLINE = "online"
STATE_NEEDS_SCAN = "needs_scan"
STATE_OFFLINE = "offline"
STATE_GAVE_UP = "gave_up"

# 退出码
EXIT_OK = 0
EXIT_NEEDS_SCAN = 2
EXIT_EXHAUSTED = 3


def strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line or "")


# --------------------------------------------------------------------- 解析
#: 一行日志 → 事件名。顺序有意义：越具体的越靠前。
LINE_EVENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("kicked", re.compile(r"KickedOffLine|下线通知.*登录已失效")),
    ("token_dead", re.compile(r"自动快速登录失败|用户身份已失效")),
    ("online", re.compile(r"账号状态变更为在线|登录成功")),
    ("offline", re.compile(r"账号状态变更为离线")),
    ("scanned", re.compile(r"二维码已被扫描")),
    ("qr_refresh", re.compile(r"ErrCode:\s*3|请求刷新二维码")),
    ("quick_login", re.compile(r"正在快速登录")),
    ("protocol_up", re.compile(r"OneBot11 适配器初始化完成|OneBot.*服务启动")),
)


def parse_line(line: str) -> str | None:
    """把一行 NapCat 控制台输出归类成事件名；认不出来返回 ``None``。"""
    text = strip_ansi(line)
    for name, pattern in LINE_EVENTS:
        if pattern.search(text):
            return name
    return None


def onebot_port_open(
    port: int,
    *,
    timeout: float = 1.5,
    connect: Callable[[str, int, float], int] | None = None,
) -> bool:
    """探测本机 OneBot 端口是否可连（可连 = 已登录，见 protocol_up 注释）。

    ``connect`` 供测试注入；生产走 ``socket.create_connection`` 的
    ``connect_ex``（返回 0 = 成功，其余任何值都算失败）。
    """
    if connect is None:
        import socket

        def connect(host: str, port: int, timeout: float) -> int:
            with socket.create_connection((host, port), timeout=timeout):
                return 0

    try:
        return connect("127.0.0.1", int(port), timeout) == 0
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------- 监控
@dataclass
class Session:
    started_at: float
    ended_at: float | None = None
    end_reason: str = ""

    @property
    def duration(self) -> float | None:
        if self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)


@dataclass
class Monitor:
    """纯状态机：喂日志行/探针结果，输出「下一步该干什么」。

    所有时间都走注入的 ``clock``，所以测试里不需要真的等 15 分钟。
    """

    clock: Callable[[], float] = time.monotonic
    backoff: tuple[float, ...] = DEFAULT_BACKOFF
    max_restarts: int = DEFAULT_MAX_RESTARTS
    scan_timeout: float = DEFAULT_SCAN_TIMEOUT
    login_timeout: float = DEFAULT_LOGIN_TIMEOUT

    state: str = STATE_STARTING
    restarts: int = 0
    sessions: list[Session] = field(default_factory=list)
    _started_at: float = field(default_factory=lambda: 0.0)
    _state_since: float = field(default_factory=lambda: 0.0)
    _last_event_at: float = field(default_factory=lambda: 0.0)
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        now = self.clock()
        self._started_at = now
        self._state_since = now
        self._last_event_at = now

    # -- 查询
    @property
    def current_session(self) -> Session | None:
        for session in reversed(self.sessions):
            if session.ended_at is None:
                return session
        return None

    def online_seconds(self) -> float:
        return sum(s.duration or 0.0 for s in self.sessions)

    def backoff_seconds(self) -> float:
        if not self.backoff:
            return 0.0
        index = min(self.restarts, len(self.backoff) - 1)
        return float(self.backoff[index])

    def summary(self) -> dict[str, Any]:
        durations = [round(s.duration or 0.0, 1) for s in self.sessions]
        return {
            "state": self.state,
            "restarts": self.restarts,
            "sessions": durations,
            "online_seconds": round(self.online_seconds(), 1),
            "avg_session_seconds": round(sum(durations) / len(durations), 1)
            if durations
            else 0.0,
        }

    # -- 状态迁移
    def _enter(self, state: str, reason: str) -> dict[str, Any]:
        now = self.clock()
        if state != self.state:
            self.state = state
            self._state_since = now
        event = {"at": now, "event": "state", "state": state, "reason": reason}
        self.events.append(event)
        self._last_event_at = now
        return event

    def on_line(self, line: str) -> dict[str, Any] | None:
        name = parse_line(line)
        if name is None:
            return None
        return self.on_event(name)

    def on_event(self, name: str) -> dict[str, Any] | None:
        now = self.clock()
        if name == "online":
            if self.state == STATE_ONLINE:
                return None
            session = Session(started_at=now)
            self.sessions.append(session)
            return self._enter(STATE_ONLINE, "login_ok")
        if name in {"kicked", "offline"}:
            if self.state != STATE_ONLINE:
                return None
            session = self.current_session
            if session is not None:
                session.ended_at = now
                session.end_reason = name
            return self._enter(STATE_OFFLINE, name)
        if name == "token_dead":
            # 令牌失效：重启没用，必须人工扫码。
            if self.state == STATE_ONLINE:
                session = self.current_session
                if session is not None and session.ended_at is None:
                    session.ended_at = now
                    session.end_reason = "token_dead"
            return self._enter(STATE_NEEDS_SCAN, "token_dead")
        if name == "scanned":
            # 已经扫上了，等确认；状态不动，只记事件。
            self.events.append({"at": now, "event": "scanned"})
            return self.events[-1]
        if name == "protocol_up":
            # OneBot 适配器只在登录成功后才初始化——2026-10-31 夜间事故实证：
            # 3001 可连 ⟺ 已登录，挂在登录页的容器整夜都连不上。而 WebUI
            # 密码登录路径不会打「账号状态变更为在线|登录成功」，只认那两句
            # 的结果是 supervisor 把刚登录的活会话当 needs_scan 杀掉，20 分钟
            # 一次的强制重登还触发了腾讯风控（密码回退要验证码）。
            # 所以 protocol_up 本身就是登录成功，必须推进状态、取消 give-up。
            if self.state in {STATE_STARTING, STATE_NEEDS_SCAN}:
                self.events.append({"at": now, "event": "protocol_up"})
                self.sessions.append(Session(started_at=now))
                return self._enter(STATE_ONLINE, "protocol_up")
            if self.state == STATE_ONLINE:
                return None
            self.events.append({"at": now, "event": "protocol_up"})
            return self.events[-1]
        return None

    def on_protocol_lost(self) -> dict[str, Any] | None:
        """3001 端口的探针失败：只有在线时才当事。"""
        if self.state != STATE_ONLINE:
            return None
        return self.on_event("offline")

    # -- 决策
    def next_action(self) -> tuple[str, float]:
        """返回 ``(动作, 等待秒数)``。

        动作：``wait`` / ``restart`` / ``give_up_scan`` / ``give_up_restarts``。
        """
        now = self.clock()
        state_age = now - self._state_since
        if self.state == STATE_STARTING:
            if state_age >= self.login_timeout:
                # 启动后迟迟没有登录态：大概率在等扫码，转人工态。
                self._enter(STATE_NEEDS_SCAN, "login_timeout")
                return "wait", 0.0
            return "wait", max(1.0, self.login_timeout - state_age)
        if self.state == STATE_NEEDS_SCAN:
            if state_age >= self.scan_timeout:
                return "give_up_scan", 0.0
            return "wait", max(1.0, self.scan_timeout - state_age)
        if self.state == STATE_OFFLINE:
            if self.restarts >= self.max_restarts:
                return "give_up_restarts", 0.0
            delay = self.backoff_seconds()
            if state_age >= delay:
                return "restart", 0.0
            return "wait", max(1.0, delay - state_age)
        # online：等下一行日志/探针。
        return "wait", 5.0

    def record_restart(self) -> None:
        self.restarts += 1
        self._enter(STATE_STARTING, "restart")


# --------------------------------------------------------------------- 进程
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        record = {"ts": _now_iso(), **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def desktop_qq_running() -> list[int]:
    """返回正在运行的**桌面版** QQ 进程（排除 NapCat 便携目录里的）。"""
    if sys.platform != "win32":
        return []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
                "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    pids: list[int] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = str(item.get("ExecutablePath") or "").lower()
        if "vendor\\napcat" in path or "vendor/napcat" in path:
            continue
        pid = item.get("ProcessId")
        if isinstance(pid, int):
            pids.append(pid)
    return pids


def kill_pids(pids: Iterable[int]) -> None:
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue


class Supervisor:
    """把 ``Monitor`` 接到真实进程上。

    两种运行形态：

    * **本机进程**（Windows 默认）：启动 ``start_napcat.bat``，读它的 stdout
      拿日志，停止时 terminate 自己。
    * **容器**（Linux）：``start_command`` = ``docker start napcat``，
      ``stop_command`` = ``docker stop napcat``，日志另起一条
      ``docker logs -f --tail 0 napcat``。容器不是我们拉起来的，所以不能
      terminate，也不能靠 ``poll()`` 判断在线（``docker start`` 立刻返回）。
    """

    def __init__(
        self,
        *,
        uin: str,
        monitor: Monitor | None = None,
        log_dir: Path = DEFAULT_LOG_DIR,
        start_command: list[str] | None = None,
        stop_command: list[str] | None = None,
        log_command: list[str] | None = None,
        onebot_port: int = 3001,
        popen: Callable[..., Any] = subprocess.Popen,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.uin = str(uin)
        self.onebot_port = int(onebot_port)
        self.monitor = monitor or Monitor(clock=clock)
        self.log_dir = log_dir
        self.logger = JsonlLogger(log_dir / SUPERVISOR_LOG)
        self.console_log = log_dir / NAPCAT_CONSOLE_LOG
        self.start_command = start_command or [
            "cmd",
            "/c",
            START_SCRIPT,
            "-q",
            self.uin,
        ]
        self.stop_command = stop_command
        self.log_command = log_command
        self._popen = popen
        self._run = runner
        self._clock = clock
        self._sleep = sleep
        self.process: Any | None = None
        self._log_process: Any | None = None
        self._reader: threading.Thread | None = None
        #: 只有自己拉起的进程才 terminate / poll。
        self._owns_process = log_command is None

    # -- 进程生命周期
    def start(self) -> None:
        self.console_log.parent.mkdir(parents=True, exist_ok=True)
        if self.log_command:
            # 容器：start 只要拉起即可，日志另起一条跟随。
            self._run(self.start_command, capture_output=True, text=True, timeout=120)
            self.process = None
            self._log_process = self._popen(
                self.log_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()
        else:
            self.process = self._popen(
                self.start_command,
                cwd=str(WORKSPACE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()
        self.logger.write({"event": "launched", "uin": self.uin})

    def _pump(self) -> None:
        source = self._log_process or self.process
        stream = getattr(source, "stdout", None)
        if stream is None:
            return
        with self.console_log.open("a", encoding="utf-8", errors="replace") as sink:
            for raw in stream:
                line = strip_ansi(raw.rstrip("\n"))
                if not line:
                    continue
                sink.write(line + "\n")
                sink.flush()
                event = self.monitor.on_line(line)
                if event is not None:
                    self.logger.write({"event": event["event"], **{
                        k: v for k, v in event.items() if k != "event"
                    }})

    def stop(self, reason: str) -> None:
        self.logger.write({"event": "stopping", "reason": reason})
        log_process = self._log_process
        self._log_process = None
        if log_process is not None:
            try:
                log_process.terminate()
            except Exception:
                pass
        if self.stop_command:
            try:
                self._run(self.stop_command, capture_output=True, text=True, timeout=120)
            except Exception:
                pass
            self.process = None
            return
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    # -- 主循环
    def run(self, *, stop_when=lambda: False) -> int:
        self.logger.write({"event": "start", "uin": self.uin})
        self.start()
        # 启动探针：supervisor 可能比会话晚启动（人工在 WebUI 登录之后才
        # 拉起监管），日志跟随是 --tail 0，看不到已经发生过的 protocol_up。
        # 探一次端口，可连就当作已登录，否则会把活会话误判成 needs_scan
        # 再杀一次（2026-10-31 事故的第二半个坑）。
        if onebot_port_open(self.onebot_port):
            self.monitor.on_event("protocol_up")
            self.logger.write({"event": "startup_probe", "ok": True, "port": self.onebot_port})
        try:
            while True:
                if stop_when():
                    self.stop("user_stop")
                    return EXIT_OK
                action, delay = self.monitor.next_action()
                if action == "restart":
                    self.logger.write(
                        {
                            "event": "restart",
                            "after_seconds": self.monitor.backoff_seconds(),
                            "restarts": self.monitor.restarts,
                        }
                    )
                    self.stop("restart")
                    self.monitor.record_restart()
                    self.start()
                    continue
                if action == "give_up_scan":
                    self.logger.write(
                        {"event": "give_up", "reason": "needs_scan", **self.monitor.summary()}
                    )
                    self.stop("needs_scan")
                    return EXIT_NEEDS_SCAN
                if action == "give_up_restarts":
                    self.logger.write(
                        {"event": "give_up", "reason": "restarts", **self.monitor.summary()}
                    )
                    self.stop("restarts")
                    return EXIT_EXHAUSTED
                request_stop = stop_when()
                if request_stop:
                    self.stop("user_stop")
                    return EXIT_OK
                self._sleep(min(delay, 5.0))
                # 进程自己退出 = 也算离线；交给退避逻辑。容器模式下不靠
                # poll（`docker start` 立刻就返回了）。
                if (
                    self._owns_process
                    and self.process is not None
                    and self.process.poll() is not None
                ):
                    self.monitor.on_event("offline")
        finally:
            self.stop("exit")


# --------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NapCat 掉线监管：指数退避重启，凭据失效就停手等人工扫码。"
    )
    parser.add_argument("--uin", required=True, help="机器人 QQ 号（快速登录用）")
    parser.add_argument(
        "--backoff",
        default=",".join(str(int(v)) for v in DEFAULT_BACKOFF),
        help="重启退避秒数，逗号分隔（默认 300,900,1800,3600）",
    )
    parser.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    parser.add_argument(
        "--scan-timeout",
        type=int,
        default=int(DEFAULT_SCAN_TIMEOUT),
        help="需要人工扫码时最多等多久（秒）",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=int(DEFAULT_LOGIN_TIMEOUT),
        help="启动后多久还没登录态就视为在等扫码（秒）",
    )
    parser.add_argument(
        "--kill-desktop",
        action="store_true",
        help="启动前杀掉桌面版 QQ（NapCat 便携 QQ 与它互斥；仅 Windows）",
    )
    parser.add_argument(
        "--start-command",
        default=None,
        help='启动命令（默认 "cmd /c scripts\\start_napcat.bat -q <uin>"；'
        'Linux 容器用 "docker start napcat"）',
    )
    parser.add_argument(
        "--stop-command",
        default=None,
        help='停止命令（容器模式用 "docker stop napcat"）',
    )
    parser.add_argument(
        "--log-command",
        default=None,
        help='日志跟随命令（容器模式用 "docker logs -f --tail 0 napcat"）；'
        "设了它就不再从启动进程读 stdout",
    )
    parser.add_argument(
        "--onebot-port",
        type=int,
        default=3001,
        help="OneBot 正向 WebSocket 端口，启动探针用（默认 3001）",
    )
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    return parser


def split_command(value: str | None) -> list[str] | None:
    """把命令行字符串拆成 argv；``None``/空串返回 ``None``。"""
    text = str(value or "").strip()
    if not text:
        return None
    # Windows 上 posix=False 会保留引号，交给 Popen 自己处理更稳。
    return shlex.split(text, posix=(os.name != "nt"))


def parse_backoff(value: str) -> tuple[float, ...]:
    out: list[float] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            seconds = float(part)
        except ValueError:
            continue
        if seconds > 0:
            out.append(seconds)
    return tuple(out) or DEFAULT_BACKOFF


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.kill_desktop:
        pids = desktop_qq_running()
        if pids:
            print(f"[supervisor] 关闭桌面 QQ：{pids}")
            kill_pids(pids)
            time.sleep(3)
    monitor = Monitor(
        backoff=parse_backoff(args.backoff),
        max_restarts=max(0, args.max_restarts),
        scan_timeout=max(30.0, float(args.scan_timeout)),
        login_timeout=max(30.0, float(args.login_timeout)),
    )
    supervisor = Supervisor(
        uin=args.uin,
        monitor=monitor,
        log_dir=Path(args.log_dir),
        start_command=split_command(args.start_command),
        stop_command=split_command(args.stop_command),
        log_command=split_command(args.log_command),
        onebot_port=max(1, int(args.onebot_port)),
    )
    print(f"[supervisor] 启动；日志 {Path(args.log_dir) / SUPERVISOR_LOG}")
    try:
        code = supervisor.run()
    except KeyboardInterrupt:
        supervisor.stop("interrupt")
        code = EXIT_OK
    print(json.dumps(monitor.summary(), ensure_ascii=False))
    if code == EXIT_NEEDS_SCAN:
        print("[supervisor] 需要人工扫码；扫码后重跑本脚本。")
    elif code == EXIT_EXHAUSTED:
        print("[supervisor] 重启次数用尽；先查桌面 QQ 冲突/账号风控，别继续硬试。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
