#!/usr/bin/env python
"""云端 NapCat / OneBot 掉线看门狗（T-0069）。

为什么需要
----------
2026-10-30 那次：10:45:57 账号被服务端 ``KickedOffLine``，``fatfish-qq`` 一直
在刷 ``connection refused``，但 systemd 眼里它还是 ``active``——**两个多小时
没人发现**。用户的原话是「我用云服务器就是为了在电脑关机时也能聊天」，所以
NapCat 必须留在云端，而且**掉线不能静默**。

这个脚本只做一件事：每 2 分钟看一眼「OneBot 还能不能连」，状态翻转时喊一声。
它**绝不重启容器**——重启是 ``napcat_supervisor`` 的职责，两个东西抢着拉只会
互相打架。

判定
----
1. ``127.0.0.1:3001`` 能不能连上（``socket.create_connection``，2s）——
   这是最直接的那条：OneBot 断了，端口就不通；
2. ``docker inspect napcat`` 的 ``.State.Status``——用来区分「容器没了」
   和「容器在、只是没登录/掉线」；
3. 两者合成一个结论。``fatfish-qq`` 的日志不再解析：它刷的每一行都是同一个
   根因，解析日志格式只会多一个会坏的地方。

状态机与去抖
------------
::

    ok ──不健康──> pending ──持续 >= grace──> down ──恢复──> ok
                     │                         │
                     └─ 未满 grace 就恢复 ─────┘（只记 info，不告警）

* ``grace`` 默认 300s：短于它的抖动是正常重启，不算故障；
* **只在翻转时告警**：``ok → down`` 一次、``down → ok`` 一次；``down`` 期间
  不再重复刷；
* 状态写在 ``data/run/napcat-watch.json``，所以 ``--once`` 由 systemd timer
  每隔 2 分钟拉起一个新进程时，计时不会从头开始。

告警通道
--------
① ``SMTP_HOST`` / ``SMTP_USER`` / ``SMTP_PASS`` / ``SMTP_TO`` 都在 → 发邮件，
   标题带机器名与已掉线时长；
② 否则/发送失败 → 写 ``data/logs/napcat-watch.log``，并落一个
   ``data/run/napcat_down``（内容是首次故障时间 + 原因码），恢复时删掉。
   远程排障时 ``cat data/run/napcat_down`` 就知道发生了什么。

退出码：``0`` 健康（或抖动未满 grace）/ ``1`` 已判定故障 / ``2`` 配置错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("napcat_watch")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3001
DEFAULT_CONTAINER = "napcat"
DEFAULT_INTERVAL = 120.0
DEFAULT_GRACE = 300.0
CONNECT_TIMEOUT = 2.0

EXIT_OK = 0
EXIT_DOWN = 1
EXIT_CONFIG = 2

#: 原因码 → 一句话。邮件/日志/marker 共用，免得三处各写一份措辞。
REASON_TEXT = {
    "ok": "正常",
    "port_closed": "OneBot 端口不通（容器在跑，但没登录 / 已掉线）",
    "container_exited": "容器 napcat 已退出",
    "container_restarting": "容器 napcat 在重启循环里",
    "container_missing": "找不到容器 napcat",
    "docker_missing": "docker 命令不可用",
}


def _iso(ts: float | None) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        # Windows 对「1970 之前」的时间戳会抛 OSError；告警不能因为这个崩。
        return f"epoch {float(ts):.0f}"


def _human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60.0:.1f} 小时"


@dataclass
class Sample:
    """一轮探测的结论。``ok=False`` 时 ``reason`` 是 :data:`REASON_TEXT` 的键。"""

    ok: bool
    reason: str = "ok"
    detail: str = ""
    container_status: str = ""
    container_started_at: str = ""

    @property
    def summary(self) -> str:
        text = REASON_TEXT.get(self.reason, self.reason)
        bits = [text]
        if self.container_status:
            bits.append(f"container={self.container_status}")
        if self.detail:
            bits.append(self.detail)
        return "；".join(bits)


class SystemProbe:
    """真实探测：TCP 端口 + ``docker inspect``。两条都可注入，便于离线测试。"""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        container: str = DEFAULT_CONTAINER,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        runner: Callable[..., Any] | None = None,
    ):
        self.host = host
        self.port = int(port)
        self.container = container
        self.connect_timeout = float(connect_timeout)
        self._run = runner or subprocess.run

    # -- 单个探测
    def port_open(self) -> bool:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            ):
                return True
        except OSError:
            return False

    def container_state(self) -> tuple[str, str, str]:
        """``(status, started_at, error)``；容器不存在时 status 为空串。"""
        fmt = "{{.State.Status}}|{{.State.StartedAt}}"
        try:
            proc = self._run(
                ["docker", "inspect", "--format", fmt, self.container],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return "", "", f"{type(exc).__name__}: {exc}"
        out = str(getattr(proc, "stdout", "") or "").strip()
        if not out:
            err = str(getattr(proc, "stderr", "") or "").strip()
            return "", "", (err.splitlines()[0] if err else "")[:160]
        status, _, started = out.partition("|")
        return status.strip(), started.strip(), ""

    def sample(self) -> Sample:
        status, started, error = self.container_state()
        if self.port_open():
            return Sample(
                True,
                container_status=status,
                container_started_at=started,
            )
        if error:
            # docker 本身不可用时，端口不通的结论仍然有效，只是说不清原因。
            reason = "container_missing" if "No such object" in error else "docker_missing"
            return Sample(False, reason, error)
        if status in {"exited", "dead"}:
            return Sample(False, "container_exited", f"started_at={started}", status, started)
        if status in {"restarting", "created", "paused"}:
            return Sample(False, "container_restarting", f"started_at={started}", status, started)
        if not status:
            return Sample(False, "container_missing", "docker inspect 无输出")
        # 容器在跑、端口不通 = 最常见的那个：账号掉线 / 还没扫码登录。
        return Sample(False, "port_closed", f"started_at={started}", status, started)


class Notifier:
    """邮件优先、文件兜底。两条通道都失败也不抛——告警器自己不能崩。"""

    MAIL_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_TO")

    def __init__(
        self,
        *,
        hostname: str,
        env: dict[str, str] | None = None,
        allow_email: bool = True,
        smtp_factory: Callable[..., Any] | None = None,
    ):
        source = os.environ if env is None else env
        self.smtp = {key: str(source.get(key, "") or "") for key in self.MAIL_KEYS}
        self.hostname = hostname or "cloud"
        self.allow_email = bool(allow_email)
        self._smtp = smtp_factory or smtplib.SMTP

    @property
    def mail_ready(self) -> bool:
        return bool(self.allow_email and self.smtp["SMTP_HOST"] and self.smtp["SMTP_TO"])

    def send(self, subject: str, body: str) -> str:
        """返回实际用的通道：``smtp`` 或 ``file``。"""
        if self.mail_ready:
            try:
                self._send_mail(subject, body)
                return "smtp"
            except Exception as exc:  # 网络 / 认证 / 证书，一律降级
                log.warning("smtp 发送失败（降级到日志）：%s", exc)
        log.error("%s | %s", subject, body.replace("\n", " "))
        return "file"

    def _send_mail(self, subject: str, body: str) -> None:
        try:
            port = int(self.smtp["SMTP_PORT"] or 587)
        except ValueError:
            port = 587
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.smtp["SMTP_USER"] or f"napcat-watch@{self.hostname}"
        message["To"] = self.smtp["SMTP_TO"]
        message.set_content(body)
        with self._smtp(self.smtp["SMTP_HOST"], port, timeout=15) as client:
            client.ehlo()
            if port != 25:
                client.starttls()
            if self.smtp["SMTP_USER"]:
                client.login(self.smtp["SMTP_USER"], self.smtp["SMTP_PASS"])
            client.send_message(message)


class Watchdog:
    """状态机 + 去抖 + 告警。不碰容器，只观察。"""

    def __init__(
        self,
        *,
        state_path: Path,
        marker_path: Path,
        notifier: Notifier,
        grace: float = DEFAULT_GRACE,
        clock: Callable[[], float] = time.time,
        hostname: str = "",
    ):
        self.state_path = Path(state_path)
        self.marker_path = Path(marker_path)
        self.notifier = notifier
        self.grace = float(grace)
        self.clock = clock
        self.hostname = hostname or socket.gethostname()
        self.state = self._load()
        #: 本轮的结论，``--json`` 直接打这一条，cron/journal 都能读。
        self.last_event: dict[str, Any] = {"event": "ok"}

    # -- 状态文件
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "ok", "first_down_at": None, "last_alert_at": None}
        if not isinstance(data, dict):
            return {"state": "ok", "first_down_at": None, "last_alert_at": None}
        data.setdefault("state", "ok")
        data.setdefault("first_down_at", None)
        data.setdefault("last_alert_at", None)
        return data

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.state_path)
        except OSError as exc:
            # 状态写不下去只影响「跨进程去抖」，不能因此停止监控。
            log.warning("状态文件写入失败：%s", exc)

    # -- 主判定
    def observe(self, sample: Sample) -> int:
        now = float(self.clock())
        state = self.state
        if sample.ok:
            return self._on_healthy(state, now)
        return self._on_unhealthy(state, sample, now)

    def _on_healthy(self, state: dict[str, Any], now: float) -> int:
        event = "ok"
        if state.get("state") == "down":
            started = state.get("first_down_at")
            duration = max(0.0, now - float(started)) if started else 0.0
            self._remove_marker()
            self.notifier.send(
                f"[napcat-watch] {self.hostname} NapCat 已恢复（掉线 {_human_duration(duration)}）",
                f"OneBot 端口重新可连。\n"
                f"首次故障：{_iso(started)}\n"
                f"恢复时间：{_iso(now)}\n"
                f"掉线时长：{_human_duration(duration)}\n",
            )
            log.info("RECOVERED after %s", _human_duration(duration))
            state.update({"state": "ok", "first_down_at": None, "last_alert_at": None})
            event = "recovered"
        elif state.get("first_down_at") is not None:
            level = now - float(state["first_down_at"])
            log.info("抖动已恢复（%.0fs，未满 grace）", level)
            state["first_down_at"] = None
        self.last_event = {
            "event": event,
            "reason": "ok",
            "down_for_s": 0.0,
            "checked_at": _iso(now),
        }
        self._save()
        return EXIT_OK

    def _on_unhealthy(self, state: dict[str, Any], sample: Sample, now: float) -> int:
        if state.get("first_down_at") is None:
            state["first_down_at"] = now
            log.warning("不健康：%s（开始计时，grace %.0fs）", sample.summary, self.grace)
        missing_for = now - float(state["first_down_at"])
        event = "pending"
        if state.get("state") != "down" and missing_for >= self.grace:
            state.update({"state": "down", "last_alert_at": now})
            self._write_marker(state, sample, now)
            self.notifier.send(
                f"[napcat-watch] {self.hostname} NapCat 掉线 {_human_duration(missing_for)}",
                f"{sample.summary}\n"
                f"首次故障：{_iso(state['first_down_at'])}\n"
                f"检查时间：{_iso(now)}\n"
                f"容器状态：{sample.container_status or '未知'}\n"
                f"容器启动于：{sample.container_started_at or '未知'}\n\n"
                f"处理：先看 `docker logs --tail 50 napcat` 有没有二维码 / KickedOffLine，\n"
                f"需要时 `docker exec` 或者去 WebUI 6099（走 SSH 隧道）重新扫码。\n"
                f"看门狗不会重启容器——那是 napcat-supervisor 的事。\n",
            )
            log.error("ALERT %s（%s）", sample.summary, _human_duration(missing_for))
            event = "alert"
        elif state.get("state") == "down":
            event = "down"
        self.last_event = {
            "event": event,
            "reason": sample.reason,
            "reason_text": REASON_TEXT.get(sample.reason, sample.reason),
            "detail": sample.detail,
            "container_status": sample.container_status,
            "container_started_at": sample.container_started_at,
            "down_for_s": round(missing_for, 1),
            "checked_at": _iso(now),
        }
        self._save()
        # 退出码反映「这一轮探测到的事实」，不是「是否已经告警」：
        # 未满 grace 也算不健康，systemd 那边 failed 一眼可见（告警仍然去抖）。
        return EXIT_DOWN

    # -- 标记文件
    def _write_marker(self, state: dict[str, Any], sample: Sample, now: float) -> None:
        payload = {
            "first_down_at": _iso(state.get("first_down_at")),
            "checked_at": _iso(now),
            "reason": sample.reason,
            "reason_text": REASON_TEXT.get(sample.reason, sample.reason),
            "detail": sample.detail,
            "container_status": sample.container_status,
        }
        try:
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            self.marker_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("标记文件写入失败：%s", exc)

    def _remove_marker(self) -> None:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("标记文件删除失败：%s", exc)


def build_logger(log_path: Path, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("napcat_watch")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("[napcat-watch] %(levelname)s %(message)s"))
        logger.addHandler(stream)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            logger.addHandler(handler)
        except OSError as exc:
            logger.warning("日志文件不可写（只走 stderr）：%s", exc)
    return logger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="云端 NapCat / OneBot 掉线看门狗（只告警，不重启）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="循环间隔（秒）")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE, help="连续不健康多久才算故障（秒）")
    parser.add_argument("--data-dir", default=None, help="默认 FATFISH_DATA_DIR 或 ./data")
    parser.add_argument("--once", action="store_true", help="只跑一轮（systemd timer / 测试）")
    parser.add_argument("--json", action="store_true", help="把本轮结论打一行 JSON 到 stdout")
    parser.add_argument("--no-email", action="store_true", help="强制走文件通道（排障用）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interval <= 0:
        print("配置错误：--interval 必须 > 0", file=sys.stderr)
        return EXIT_CONFIG
    if args.grace < 0:
        print("配置错误：--grace 不能为负", file=sys.stderr)
        return EXIT_CONFIG
    if not str(args.container or "").strip():
        print("配置错误：--container 不能为空", file=sys.stderr)
        return EXIT_CONFIG

    data_dir = Path(
        args.data_dir or os.environ.get("FATFISH_DATA_DIR") or "data"
    )
    run_dir = data_dir / "run"
    build_logger(data_dir / "logs" / "napcat-watch.log", verbose=args.verbose)

    probe = SystemProbe(args.host, args.port, args.container)
    notifier = Notifier(
        hostname=socket.gethostname(),
        allow_email=not args.no_email,
    )
    watchdog = Watchdog(
        state_path=run_dir / "napcat-watch.json",
        marker_path=run_dir / "napcat_down",
        notifier=notifier,
        grace=args.grace,
    )

    def one_round() -> int:
        sample = probe.sample()
        code = watchdog.observe(sample)
        log.debug("sample ok=%s reason=%s detail=%s", sample.ok, sample.reason, sample.detail)
        if args.json:
            print(json.dumps(watchdog.last_event, ensure_ascii=False), flush=True)
        return code

    if args.once:
        return one_round()

    log.info(
        "看门狗启动：%s:%d / %s，每 %.0fs 一轮，grace %.0fs，邮件=%s",
        args.host,
        args.port,
        args.container,
        args.interval,
        args.grace,
        "on" if notifier.mail_ready else "off",
    )
    try:
        while True:
            one_round()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("bye")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
