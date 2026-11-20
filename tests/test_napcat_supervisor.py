"""NapCat 掉线监管：全离线测试（假时钟，不启动任何进程）。"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    import sys

    path = ROOT / "scripts" / "napcat_supervisor.py"
    spec = importlib.util.spec_from_file_location("napcat_supervisor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 需要模块在 sys.modules 里（否则解析注解时拿不到命名空间）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sup = _load()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def make_monitor(clock, **kwargs):
    return sup.Monitor(clock=clock, **kwargs)


# ------------------------------------------------------------------ 日志解析
class ParseTests(unittest.TestCase):
    def test_ansi_is_stripped(self) -> None:
        self.assertEqual("abc", sup.strip_ansi("\x1b[31mabc\x1b[39m"))

    def test_known_lines(self) -> None:
        cases = {
            "10-25 14:50:06 [error] 从前有只大肥鱼 | [KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。": "kicked",
            "[NapCat] [WebUi] 自动快速登录失败: 你的用户身份已失效，为保证账号安全，请你重新登录。": "token_dead",
            "10-25 14:11:10 [info] 从前有只大肥鱼 | 账号状态变更为在线": "online",
            "10-25 14:50:07 [info] 从前有只大肥鱼 | 账号状态变更为离线": "offline",
            "10-25 15:33:14 [info] [Core] [Login] 二维码已被扫描，等待确认...": "scanned",
            "10-25 15:30:46 [error] [Core] [Login] Login Error,ErrType:  1  ErrCode: 5": None,
            "10-25 15:28:44 [error] [Core] [Login] Login Error,ErrType:  1  ErrCode: 3": "qr_refresh",
            "10-25 16:43:05 [info] [AdapterManager] OneBot11 适配器初始化完成": "protocol_up",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(expected, sup.parse_line(line))

    def test_unknown_line_is_none(self) -> None:
        self.assertIsNone(sup.parse_line("10-25 14:42:08 [info] 发送 -> 私聊 (1) [语音 6s]"))


# ------------------------------------------------------------------ 状态机
class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def test_online_then_kick_records_duration(self) -> None:
        monitor = make_monitor(self.clock)
        monitor.on_event("online")
        self.assertEqual(sup.STATE_ONLINE, monitor.state)
        self.clock.advance(39 * 60)
        monitor.on_event("kicked")
        self.assertEqual(sup.STATE_OFFLINE, monitor.state)
        self.assertEqual([39 * 60], [s.duration for s in monitor.sessions])
        self.assertEqual(39 * 60, monitor.online_seconds())

    def test_duplicate_online_is_ignored(self) -> None:
        monitor = make_monitor(self.clock)
        monitor.on_event("online")
        monitor.on_event("online")
        self.assertEqual(1, len(monitor.sessions))

    def test_backoff_grows_and_then_repeats_last(self) -> None:
        monitor = make_monitor(self.clock, backoff=(300, 900, 1800), max_restarts=10)
        seen = []
        for _ in range(4):
            monitor.on_event("online")
            monitor.on_event("kicked")
            seen.append(monitor.backoff_seconds())
            monitor.record_restart()
        self.assertEqual([300, 900, 1800, 1800], seen)

    def test_waits_before_restart(self) -> None:
        monitor = make_monitor(self.clock, backoff=(300,))
        monitor.on_event("online")
        monitor.on_event("kicked")
        action, delay = monitor.next_action()
        self.assertEqual("wait", action)
        self.assertAlmostEqual(300, delay)
        self.clock.advance(301)
        self.assertEqual(("restart", 0.0), monitor.next_action())

    def test_gives_up_after_max_restarts(self) -> None:
        monitor = make_monitor(self.clock, backoff=(1,), max_restarts=2)
        for _ in range(2):
            monitor.on_event("online")
            monitor.on_event("kicked")
            monitor.record_restart()
        monitor.on_event("online")
        monitor.on_event("kicked")
        self.clock.advance(5)
        self.assertEqual(("give_up_restarts", 0.0), monitor.next_action())

    def test_token_dead_needs_scan_and_does_not_restart(self) -> None:
        monitor = make_monitor(self.clock, scan_timeout=900)
        monitor.on_event("online")
        monitor.on_event("kicked")
        monitor.on_event("token_dead")
        self.assertEqual(sup.STATE_NEEDS_SCAN, monitor.state)
        action, _delay = monitor.next_action()
        self.assertEqual("wait", action)
        self.clock.advance(901)
        self.assertEqual(("give_up_scan", 0.0), monitor.next_action())
        self.assertEqual(0, monitor.restarts, "令牌失效时重启没有意义")

    def test_scan_within_timeout_still_gives_time(self) -> None:
        monitor = make_monitor(self.clock, scan_timeout=900)
        monitor.on_event("token_dead")
        self.clock.advance(600)
        self.assertEqual("wait", monitor.next_action()[0])
        monitor.on_event("online")
        self.assertEqual(sup.STATE_ONLINE, monitor.state)

    def test_starting_without_login_becomes_needs_scan(self) -> None:
        monitor = make_monitor(self.clock, login_timeout=300)
        self.assertEqual("wait", monitor.next_action()[0])
        self.clock.advance(301)
        monitor.next_action()
        self.assertEqual(sup.STATE_NEEDS_SCAN, monitor.state)

    def test_protocol_lost_only_matters_online(self) -> None:
        monitor = make_monitor(self.clock)
        self.assertIsNone(monitor.on_protocol_lost())
        monitor.on_event("online")
        self.assertIsNotNone(monitor.on_protocol_lost())
        self.assertEqual(sup.STATE_OFFLINE, monitor.state)

    def test_protocol_up_means_login(self) -> None:
        # WebUI 密码登录路径不打「登录成功」，protocol_up（OneBot 起来）
        # 就是登录成功的唯一可靠信号：必须进 online、开 session。
        monitor = make_monitor(self.clock, login_timeout=300)
        monitor.on_event("protocol_up")
        self.assertEqual(sup.STATE_ONLINE, monitor.state)
        self.assertEqual(1, len(monitor.sessions))
        # 登录超时不再把它拉回 needs_scan——这就是 2026-10-31 杀会话的路径。
        self.clock.advance(400)
        monitor.next_action()
        self.assertEqual(sup.STATE_ONLINE, monitor.state)

    def test_protocol_up_rescues_needs_scan_before_give_up(self) -> None:
        # 事故重放：needs_scan 等扫码等到第 13 分钟，用户在 WebUI 登录成功。
        # 修复前：give_up -> docker stop 杀掉活会话；修复后：留在 online。
        monitor = make_monitor(self.clock, scan_timeout=900)
        monitor.on_event("token_dead")
        self.clock.advance(800)
        monitor.on_event("protocol_up")
        self.assertEqual(sup.STATE_ONLINE, monitor.state)
        self.clock.advance(200)
        self.assertEqual("wait", monitor.next_action()[0])

    def test_protocol_up_while_online_is_noop(self) -> None:
        monitor = make_monitor(self.clock)
        monitor.on_event("online")
        monitor.on_event("protocol_up")
        self.assertEqual(1, len(monitor.sessions))
        self.assertEqual(sup.STATE_ONLINE, monitor.state)

    def test_summary_has_no_secrets_and_counts_sessions(self) -> None:
        monitor = make_monitor(self.clock)
        monitor.on_event("online")
        self.clock.advance(600)
        monitor.on_event("kicked")
        summary = monitor.summary()
        self.assertEqual(1, len(summary["sessions"]))
        self.assertEqual(600, summary["online_seconds"])
        self.assertEqual(600, summary["avg_session_seconds"])
        self.assertNotIn("9999000003", json.dumps(summary))


# ------------------------------------------------------------------ 启动探针
class PortProbeTests(unittest.TestCase):
    def test_open_port_is_online(self) -> None:
        self.assertTrue(sup.onebot_port_open(3001, connect=lambda h, p, t: 0))

    def test_refused_port_is_offline(self) -> None:
        self.assertFalse(sup.onebot_port_open(3001, connect=lambda h, p, t: 111))

    def test_probe_never_raises(self) -> None:
        def boom(host: str, port: int, timeout: float) -> int:
            raise OSError("refused")

        self.assertFalse(sup.onebot_port_open(3001, connect=boom))


# ------------------------------------------------------------------ CLI 契约
class CliTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = sup.build_parser().parse_args(["--uin", "123"])
        self.assertEqual("123", args.uin)
        self.assertFalse(args.kill_desktop)
        self.assertEqual(4, args.max_restarts)
        self.assertEqual(900, args.scan_timeout)

    def test_backoff_parsing(self) -> None:
        self.assertEqual((300.0, 900.0), sup.parse_backoff("300, 900"))
        self.assertEqual((5.0,), sup.parse_backoff("5"))
        self.assertEqual(sup.DEFAULT_BACKOFF, sup.parse_backoff(""))
        self.assertEqual(sup.DEFAULT_BACKOFF, sup.parse_backoff("abc,-3"))

    def test_start_script_contract(self) -> None:
        # 监管器只能用现成的启动脚本，不能自己拼 NapCat 参数。
        self.assertTrue(sup.START_SCRIPT.endswith("start_napcat.bat"))
        self.assertTrue((ROOT / sup.START_SCRIPT).is_file())

    def test_container_commands_parse(self) -> None:
        args = sup.build_parser().parse_args(
            [
                "--uin",
                "123",
                "--start-command",
                "docker start napcat",
                "--stop-command",
                "docker stop napcat",
                "--log-command",
                "docker logs -f --tail 0 napcat",
            ]
        )
        self.assertEqual(["docker", "start", "napcat"], sup.split_command(args.start_command))
        self.assertEqual(["docker", "stop", "napcat"], sup.split_command(args.stop_command))
        self.assertEqual(
            ["docker", "logs", "-f", "--tail", "0", "napcat"],
            sup.split_command(args.log_command),
        )

    def test_split_command_empty(self) -> None:
        self.assertIsNone(sup.split_command(""))
        self.assertIsNone(sup.split_command(None))

    def test_container_mode_does_not_own_process(self) -> None:
        monitor = make_monitor(FakeClock())
        supervisor = sup.Supervisor(
            uin="123",
            monitor=monitor,
            log_command=["docker", "logs", "-f", "napcat"],
            start_command=["docker", "start", "napcat"],
            stop_command=["docker", "stop", "napcat"],
        )
        self.assertFalse(supervisor._owns_process)
        supervisor = sup.Supervisor(uin="123", monitor=monitor)
        self.assertTrue(supervisor._owns_process)


if __name__ == "__main__":
    unittest.main()
