"""T-0069：云端 NapCat 掉线看门狗（`scripts/napcat_watch.py`）。

全部离线：真 socket 只绑 127.0.0.1 的临时端口；`docker inspect` 用假 runner；
邮件用假 SMTP 工厂。不碰真容器、不连外网、不写仓库里的 data/。
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `from scripts.napcat_watch import ...`
    sys.path.insert(0, str(ROOT))

from scripts.napcat_watch import (  # noqa: E402
    EXIT_CONFIG,
    EXIT_DOWN,
    EXIT_OK,
    Notifier,
    Sample,
    SystemProbe,
    Watchdog,
    main,
)

logging.getLogger("napcat_watch").setLevel(logging.CRITICAL)


class FakeClock:
    def __init__(self, now: float = 1_790_000_000.0):  # 2026-09 左右，避开 Windows 的负时间戳
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNotifier:
    """只记「喊了几次、喊了什么」。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> str:
        self.sent.append((subject, body))
        return "file"


def docker_runner(stdout: str = "", stderr: str = "", *, raises: BaseException | None = None):
    def run(*_args: Any, **_kwargs: Any):
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0 if stdout else 1)

    return run


class SystemProbeTests(unittest.TestCase):
    def test_port_probe_sees_a_listening_socket(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            probe = SystemProbe("127.0.0.1", port)
            self.assertTrue(probe.port_open())

    def test_port_probe_is_false_on_a_closed_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            port = srv.getsockname()[1]
        # 端口已释放；连接应立刻被拒绝（不会等满 2s）。
        self.assertFalse(SystemProbe("127.0.0.1", port, connect_timeout=0.5).port_open())

    def test_container_state_parses_status_and_started_at(self) -> None:
        probe = SystemProbe(runner=docker_runner("running|2026-10-30T10:45:57Z\n"))
        self.assertEqual(
            ("running", "2026-10-30T10:45:57Z", ""), probe.container_state()
        )

    def test_container_state_reports_empty_output(self) -> None:
        probe = SystemProbe(runner=docker_runner("", "Error: No such object: napcat"))
        self.assertEqual(("", "", "Error: No such object: napcat"), probe.container_state())

    def test_container_state_survives_a_missing_docker(self) -> None:
        probe = SystemProbe(runner=docker_runner(raises=FileNotFoundError("docker")))
        status, started, error = probe.container_state()
        self.assertEqual("", status)
        self.assertEqual("", started)
        self.assertIn("FileNotFoundError", error)


class SampleTests(unittest.TestCase):
    """端口与容器状态组合出来的原因码。"""

    def _sample(self, *, port_open: bool, inspect: str) -> Sample:
        probe = SystemProbe(runner=docker_runner(inspect))
        probe.port_open = lambda: port_open  # type: ignore[method-assign]
        return probe.sample()

    def test_port_open_is_healthy_even_if_inspect_is_odd(self) -> None:
        self.assertTrue(self._sample(port_open=True, inspect="running|t0").ok)

    def test_running_but_port_closed_is_the_kicked_offline_case(self) -> None:
        sample = self._sample(port_open=False, inspect="running|t0")
        self.assertFalse(sample.ok)
        self.assertEqual("port_closed", sample.reason)
        self.assertIn("没登录", sample.summary)

    def test_exited_container(self) -> None:
        sample = self._sample(port_open=False, inspect="exited|t0")
        self.assertEqual("container_exited", sample.reason)

    def test_restarting_container(self) -> None:
        sample = self._sample(port_open=False, inspect="restarting|t0")
        self.assertEqual("container_restarting", sample.reason)

    def test_missing_container(self) -> None:
        sample = self._sample(port_open=False, inspect="")
        self.assertEqual("container_missing", sample.reason)


class WatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.notifier = FakeNotifier()
        self.watchdog = self._watchdog()

    def _watchdog(self, *, grace: float = 300.0) -> Watchdog:
        return Watchdog(
            state_path=self.root / "run" / "napcat-watch.json",
            marker_path=self.root / "run" / "napcat_down",
            notifier=self.notifier,
            grace=grace,
            clock=self.clock,
            hostname="<cloud-host>",
        )

    def _down(self) -> Sample:
        return Sample(False, "port_closed", "started_at=t0", "running", "t0")

    @property
    def marker(self) -> Path:
        return self.root / "run" / "napcat_down"

    def test_healthy_is_zero_and_never_alerts(self) -> None:
        self.assertEqual(EXIT_OK, self.watchdog.observe(Sample(True, container_status="running")))
        self.assertEqual([], self.notifier.sent)
        self.assertFalse(self.marker.exists())

    def test_short_blip_returns_down_but_does_not_alert(self) -> None:
        # 退出码反映「这一轮不健康」，告警仍按 grace 去抖：两件事分开。
        self.assertEqual(EXIT_DOWN, self.watchdog.observe(self._down()))
        self.clock.advance(60)
        self.assertEqual(EXIT_DOWN, self.watchdog.observe(self._down()))
        self.assertEqual([], self.notifier.sent)
        self.assertFalse(self.marker.exists())
        self.assertEqual("pending", self.watchdog.last_event["event"])

    def test_blip_that_recovers_before_grace_stays_silent(self) -> None:
        self.watchdog.observe(self._down())
        self.clock.advance(120)
        self.assertEqual(EXIT_OK, self.watchdog.observe(Sample(True)))
        self.assertEqual([], self.notifier.sent)
        self.assertFalse(self.marker.exists())

    def test_sustained_outage_alerts_once_and_writes_the_marker(self) -> None:
        self.watchdog.observe(self._down())
        self.clock.advance(301)

        self.assertEqual(EXIT_DOWN, self.watchdog.observe(self._down()))
        self.assertEqual(1, len(self.notifier.sent))
        self.assertIn("掉线", self.notifier.sent[0][0])
        self.assertEqual("alert", self.watchdog.last_event["event"])
        self.assertTrue(self.marker.exists())
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual("port_closed", marker["reason"])
        self.assertTrue(marker["first_down_at"])

    def test_still_down_does_not_repeat_the_alert(self) -> None:
        self.watchdog.observe(self._down())
        self.clock.advance(301)
        self.watchdog.observe(self._down())
        for _ in range(5):
            self.clock.advance(120)
            self.assertEqual(EXIT_DOWN, self.watchdog.observe(self._down()))
        self.assertEqual(1, len(self.notifier.sent))

    def test_recovery_alerts_once_and_removes_the_marker(self) -> None:
        self.watchdog.observe(self._down())
        self.clock.advance(301)
        self.watchdog.observe(self._down())

        self.clock.advance(600)
        self.assertEqual(EXIT_OK, self.watchdog.observe(Sample(True)))
        self.assertEqual(2, len(self.notifier.sent))
        self.assertIn("已恢复", self.notifier.sent[1][0])
        self.assertIn("15 分钟", self.notifier.sent[1][0])
        self.assertEqual("recovered", self.watchdog.last_event["event"])
        self.assertFalse(self.marker.exists())

    def test_grace_timer_survives_a_new_process(self) -> None:
        # systemd timer 每 2 分钟起一个新进程：首次故障时间必须从状态文件续上。
        self.watchdog.observe(self._down())
        self.clock.advance(200)

        second = self._watchdog()
        self.clock.advance(110)  # 累计 310s
        self.assertEqual(EXIT_DOWN, second.observe(self._down()))
        self.assertEqual(1, len(self.notifier.sent))

    def test_healthy_state_is_persisted(self) -> None:
        self.watchdog.observe(Sample(True))
        state = json.loads((self.root / "run" / "napcat-watch.json").read_text(encoding="utf-8"))
        self.assertEqual("ok", state["state"])
        self.assertIsNone(state["first_down_at"])


class NotifierTests(unittest.TestCase):
    def test_without_smtp_it_falls_back_to_the_log_channel(self) -> None:
        notifier = Notifier(hostname="cloud", env={})
        self.assertFalse(notifier.mail_ready)
        with self.assertLogs("napcat_watch", level="ERROR") as captured:
            self.assertEqual("file", notifier.send("[napcat-watch] t", "body"))
        self.assertTrue(any("body" in line for line in captured.output))

    def test_with_smtp_it_sends_mail(self) -> None:
        sent: list[Any] = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                sent.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self):
                sent.append("ehlo")

            def starttls(self):
                sent.append("starttls")

            def login(self, user, password):
                sent.append(("login", user, password == "secret"))

            def send_message(self, message):
                sent.append(("send", message["Subject"], message["To"]))

        notifier = Notifier(
            hostname="cloud",
            env={
                "SMTP_HOST": "smtp.example",
                "SMTP_PORT": "587",
                "SMTP_USER": "bot@example",
                "SMTP_PASS": "secret",
                "SMTP_TO": "me@example",
            },
            smtp_factory=FakeSMTP,
        )
        self.assertTrue(notifier.mail_ready)
        self.assertEqual("smtp", notifier.send("[napcat-watch] 掉线", "body"))
        self.assertIn("starttls", sent)
        self.assertIn(("login", "bot@example", True), sent)
        self.assertTrue(any(entry[0] == "send" and entry[2] == "me@example" for entry in sent if isinstance(entry, tuple)))

    def test_smtp_failure_degrades_without_raising(self) -> None:
        def boom(*_args, **_kwargs):
            raise OSError("network down")

        notifier = Notifier(
            hostname="cloud",
            env={
                "SMTP_HOST": "smtp.example",
                "SMTP_TO": "me@example",
            },
            smtp_factory=boom,
        )
        with self.assertLogs("napcat_watch", level="WARNING"):
            self.assertEqual("file", notifier.send("s", "b"))

    def test_no_email_flag_forces_the_file_channel(self) -> None:
        notifier = Notifier(
            hostname="cloud",
            env={"SMTP_HOST": "smtp.example", "SMTP_TO": "me@example"},
            allow_email=False,
        )
        self.assertFalse(notifier.mail_ready)


class CliTests(unittest.TestCase):
    def test_bad_grace_is_a_config_error(self) -> None:
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(EXIT_CONFIG, main(["--once", "--grace", "-1"]))
            self.assertEqual(EXIT_CONFIG, main(["--once", "--interval", "0"]))
            self.assertEqual(EXIT_CONFIG, main(["--once", "--container", " "]))
        self.assertIn("配置错误", stderr.getvalue())

    def test_once_json_prints_one_parseable_line(self) -> None:
        import contextlib
        import io

        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            dead_port = srv.getsockname()[1]
        # main() 会挂一个 FileHandler 到模块 logger；Windows 上不关掉就删不掉目录。
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.addCleanup(self._reset_logger)
        tmp = tmpdir.name

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "--once",
                    "--json",
                    "--data-dir",
                    tmp,
                    "--port",
                    str(dead_port),
                    "--grace",
                    "300",
                ]
            )
        self.assertEqual(EXIT_DOWN, code)
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(1, len(lines))
        payload = json.loads(lines[0])
        self.assertIn(payload["event"], {"pending", "alert", "down"})
        self.assertIn(
            payload["reason"],
            {"port_closed", "container_missing", "docker_missing"},
        )
        self.assertTrue((Path(tmp) / "run" / "napcat-watch.json").exists())

    @staticmethod
    def _reset_logger() -> None:
        logger = logging.getLogger("napcat_watch")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        # main() 会把级别调到 INFO；恢复静默，别让后面的用例往 stderr 刷。
        logger.setLevel(logging.CRITICAL)


if __name__ == "__main__":
    unittest.main()
