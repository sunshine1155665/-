"""能力层探测（云 → 家里 GPU）与 TTS 前置开关：全离线测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fatfish.config import Config
from fatfish.remote import HealthProbe, build_probe
from fatfish.tts.client import TTSClient, TTSResult


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """记录被探测的 URL；按脚本决定成功/失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        if self.fail:
            raise OSError("connection refused")
        return b"ok"


def make_config(tmp: Path, *, tts_base_url: str = "", **remote) -> Config:
    return Config(
        {
            "app": {"data_dir": str(tmp / "data")},
            "tts": {
                "enabled": True,
                "mode": "command",
                "command": ["true"],
                "base_url": tts_base_url,
            },
            "remote": {"enabled": True, **remote},
        },
        profile="test",
    )


class BuildProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_returns_none(self) -> None:
        config = Config({"app": {"data_dir": str(self.tmp / "d")}}, profile="test")
        self.assertIsNone(build_probe(config))
        config = Config(
            {"app": {"data_dir": str(self.tmp / "d")}, "remote": {"enabled": False}},
            profile="test",
        )
        self.assertIsNone(build_probe(config))

    def test_explicit_endpoints(self) -> None:
        config = make_config(
            self.tmp,
            endpoints={"tts": "http://home:9880/health", "embedding": "http://home:9881/health"},
        )
        probe = build_probe(config)
        assert probe is not None
        self.assertEqual(
            {"tts", "embedding"}, set(probe.endpoints)
        )

    def test_home_url_convenience_builds_tts_endpoint(self) -> None:
        config = make_config(self.tmp, home_url="http://home:9880/")
        probe = build_probe(config)
        assert probe is not None
        self.assertEqual("http://home:9880/health", probe.endpoints["tts"])

    def test_empty_endpoints_returns_none(self) -> None:
        config = make_config(self.tmp, endpoints={})
        self.assertIsNone(build_probe(config))

    def test_home_url_is_derived_from_a_remote_tts_base_url(self) -> None:
        # 云端最容易漏配的一行：只写 [tts] base_url、忘了 [remote] home_url。
        # 不推导的话，家里关机时每句都要等满 tts.timeout_seconds 才失败。
        config = make_config(
            self.tmp, tts_base_url="https://home.example-tailnet.ts.net:9443/tts"
        )
        probe = build_probe(config)
        assert probe is not None
        self.assertEqual(
            "https://home.example-tailnet.ts.net:9443/health", probe.endpoints["tts"]
        )

    def test_loopback_tts_base_url_never_creates_a_probe(self) -> None:
        # 本机 profile：base_url 是 127.0.0.1，不该凭空多出一个远程探针。
        config = make_config(self.tmp, tts_base_url="http://127.0.0.1:9880/tts")
        self.assertIsNone(build_probe(config))

    def test_explicit_home_url_wins_over_the_tts_guess(self) -> None:
        config = make_config(
            self.tmp,
            home_url="http://probe-host:9880",
            tts_base_url="https://home.example-tailnet.ts.net:9443/tts",
        )
        probe = build_probe(config)
        assert probe is not None
        self.assertEqual("http://probe-host:9880/health", probe.endpoints["tts"])


class ProbeBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.recorder = Recorder()
        self.probe = HealthProbe(
            endpoints={"tts": "http://home:9880/health"},
            ttl=30.0,
            clock=self.clock,
            opener=self.recorder,
        )

    def test_ok(self) -> None:
        self.assertTrue(self.probe.home_online())
        self.assertEqual(["http://home:9880/health"], self.recorder.urls)

    def test_failure_is_reported_not_raised(self) -> None:
        self.recorder.fail = True
        result = self.probe.check("tts")
        self.assertFalse(result.ok)
        self.assertEqual("OSError", result.error)
        self.assertFalse(self.probe.home_online())

    def test_cache_avoids_repeated_probes(self) -> None:
        self.probe.check("tts")
        self.clock.now += 10
        self.probe.check("tts")
        self.assertEqual(1, len(self.recorder.urls), "TTL 内不该重复探测")
        self.clock.now += 25  # 超过 TTL
        self.probe.check("tts")
        self.assertEqual(2, len(self.recorder.urls))

    def test_force_bypasses_cache(self) -> None:
        self.probe.check("tts")
        self.probe.check("tts", force=True)
        self.assertEqual(2, len(self.recorder.urls))

    def test_home_online_requires_all_configured(self) -> None:
        probe = HealthProbe(
            endpoints={"tts": "http://home:9880/health", "embedding": "http://home:9881/health"},
            ttl=30.0,
            clock=self.clock,
            opener=self.recorder,
        )
        self.assertTrue(probe.home_online())
        self.recorder.fail = True
        self.clock.now += 60
        self.assertFalse(probe.home_online())

    def test_not_configured_name(self) -> None:
        result = self.probe.check("sing")
        self.assertFalse(result.ok)
        self.assertEqual("not_configured", result.error)

    def test_status_shape(self) -> None:
        status = self.probe.status()
        self.assertTrue(status["home_online"])
        self.assertEqual(30.0, status["ttl_seconds"])
        self.assertEqual("http://home:9880/health", status["endpoints"]["tts"]["url"])
        self.assertTrue(status["endpoints"]["tts"]["ok"])


class TtsGateTests(unittest.TestCase):
    """家里离线时：未合成的句子立刻失败，已合成的缓存照常可用。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    class _Db:
        def __getattr__(self, name):
            return lambda *a, **k: None

    def _client(self, *, online: bool, **remote) -> TTSClient:
        config = make_config(self.tmp, **remote)
        client = TTSClient(config, self._Db())  # type: ignore[arg-type]
        client.capability_probe = _FakeProbe(online)  # type: ignore[assignment]
        return client

    def test_offline_fails_fast_without_calling_backend(self) -> None:
        client = self._client(online=False)
        client._http_json = lambda *a, **k: self.fail("离线时不该发起合成")  # type: ignore[assignment]
        result = client.synthesize("你好呀")
        self.assertFalse(result.ok)
        self.assertEqual("home_offline", result.error)

    def test_online_uses_backend(self) -> None:
        client = self._client(online=True)

        def fake_backend(text, emotion, out_path):
            out_path.write_bytes(b"RIFFfake")
            return TTSResult(True, audio_path=str(out_path), provider="http_json")

        client.mode = "http_json"
        client._http_json = fake_backend  # type: ignore[assignment]
        result = client.synthesize("你好呀")
        self.assertTrue(result.ok, result.error)

    def test_cached_audio_survives_offline(self) -> None:
        client = self._client(online=False)
        cached = client._cache_path("你好呀", None)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"RIFFcached")
        result = client.synthesize("你好呀")
        self.assertTrue(result.ok)
        self.assertTrue(result.cached)

    def test_no_remote_config_means_no_gate(self) -> None:
        config = Config(
            {
                "app": {"data_dir": str(self.tmp / "data")},
                "tts": {"enabled": True, "mode": "command", "command": ["true"]},
            },
            profile="test",
        )
        client = TTSClient(config, self._Db())  # type: ignore[arg-type]
        self.assertIsNone(client.capability_probe)


class _FakeProbe:
    def __init__(self, online: bool) -> None:
        self.online = online

    def home_online(self) -> bool:
        return self.online

    def status(self):
        return {"home_online": self.online}


class CliTests(unittest.TestCase):
    def test_remote_status_parses(self) -> None:
        from fatfish import cli

        args = cli.build_parser().parse_args(["remote", "status", "--force"])
        self.assertIs(cli.cmd_remote_status, args.func)
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()
