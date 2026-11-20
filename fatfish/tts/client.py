"""Pluggable local TTS client.

Two modes:
  http_json: POST {"text", "voice", "format", "emotion"} to a local endpoint.
  command:   run a local command with {text}, {out}, {voice}, {format} placeholders.

The client only produces an audio file; sending it to QQ/Bilibili is a separate
adapter. Default is disabled so the core stays dependency-free.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config, env_secret
from ..db import Database
from ..textfmt import strip_stage_directions, strip_timestamps
from ..remote import build_probe
from ..voice.segments import split_short_sentences
from .instruct import instruct_for, normalize_tone, speed_for
from .segmentation import plan_segments


@dataclass(slots=True)
class TTSResult:
    ok: bool
    audio_path: str | None = None
    format: str = "wav"
    provider: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    error: str | None = None


class TTSClient:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.enabled = bool(config.get("tts.enabled", False))
        self.mode = str(config.get("tts.mode", "http_json")).strip().lower()
        # Which local backend owns ``base_url``.  The HTTP contract is the same,
        # but the cache must not reuse a CosyVoice WAV after switching engines.
        self.engine = (
            str(config.get("tts.engine", "cosyvoice") or "cosyvoice").strip().lower()
            or "cosyvoice"
        )
        # Bumped whenever the engine/reference changes.  Engine alone is not
        # enough: a 2.5 service can keep the old config name.  The pitch
        # overlay is NOT baked in here; it enters the key on its own below.
        self.cache_version = str(config.get("tts.cache_version", "") or "").strip()
        # 叠加升调（半音），与服务端 ``tts.pitch_semitones`` 同源同默认（0 =
        # 不叠加；参考音 cand_home_up1.wav 已含 +2）。改这个值会改变服务端
        # 返回的音频，所以必须进缓存键 —— 缓存键漏项 = 改了参数还放旧音频。
        try:
            self.pitch_semitones = max(0, int(config.get("tts.pitch_semitones", 0)))
        except (TypeError, ValueError):
            self.pitch_semitones = 0
        self.base_url = str(config.get("tts.base_url", "") or "").strip()
        self.voice = str(config.get("tts.voice", "") or "")
        self.format = str(config.get("tts.format", "wav") or "wav").lstrip(".")
        self.timeout = float(config.get("tts.timeout_seconds", 30))
        self.output_dir = config.data_dir / "audio"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = env_secret(str(config.get("tts.api_key_env") or "")) if config.get("tts.api_key_env") else None
        self.extra_headers = config.get("tts.headers", {}) or {}
        self.command = config.get("tts.command", []) or []
        # 云上跑时，TTS 其实在家里。合成前先看「家里还在不在」：不在就立刻
        # 返回 home_offline（调用方发文字），而不是每句都等满 30s 超时。
        # 本机 profile 不配 [remote] → 探针为 None，行为完全不变。
        self.capability_probe = build_probe(config)
        self.text_lang = str(config.get("tts.text_lang", "zh") or "zh")
        self.prompt_lang = str(config.get("tts.prompt_lang", "zh") or "zh")
        self.ref_audio_path = str(config.get("tts.ref_audio_path", "") or "")
        self.prompt_text = str(config.get("tts.prompt_text", "") or "")
        self.text_split_method = str(config.get("tts.text_split_method", "cut5") or "cut5")
        self.media_type = str(config.get("tts.media_type", self.format) or self.format)
        self.streaming_mode = bool(config.get("tts.streaming_mode", False))
        self.batch_size = int(config.get("tts.batch_size", 1) or 1)
        # ``（小声）`` is part of the chat bubble, not part of the audio.
        self.strip_stage = bool(config.get("tts.strip_stage_directions", True))
        self.stage_max_chars = int(config.get("tts.stage_direction_max_chars", 24) or 24)
        # 语气走 instruct，断句交给文本前端 —— 这两件是「听起来自不自然」的
        # 全部来源，和参考音无关。关掉任何一个都会退回改动前的行为。
        #
        # ⚠️ instruct 现在是**空表**（六个 tone 全发 None），因为在这台机器的
        # Fun-CosyVoice3-0.5B 上它是坏的：缺 <|endofprompt|> 会在服务端 LLM 线程
        # 里断言失败（503），补齐之后模型又会把指令当台词念出来。详见
        # fatfish/tts/instruct.py 与 docs/branches/voice.md 的说明。
        # 因此 `tts.instruct_enabled` 退化成一个纯粹的"回退开关"：关掉它 = 速度
        # 回到 1.00、去掉一切语气处理，用来在真机上对比改动前后的听感。
        # text_frontend: 由模型自己的文本前端处理「～」「……」「、」和断句，
        #   而不是我们按 20 字硬切完再拼。实测长句更连贯（4.60s vs 5.02s），
        #   而且它管发音 —— 关掉会把「陪着」念成「背着」（mimo 听写确认）。
        #   首次使用会加载 wetext（<HF_CACHE>/modelscope/hub/pengzhendong/wetext）。
        self.instruct_enabled = bool(config.get("tts.instruct_enabled", True))
        self.text_frontend = bool(config.get("tts.text_frontend", True))
        raw_template = config.get("tts.payload_template")
        if isinstance(raw_template, str) and raw_template.strip():
            try:
                raw_template = json.loads(raw_template)
            except json.JSONDecodeError:
                raw_template = None
        self.payload_template = raw_template if isinstance(raw_template, dict) else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "engine": self.engine,
            "base_url": self.base_url,
            "voice": self.voice,
            "format": self.format,
            "output_dir": str(self.output_dir),
            "configured": bool(
                (self.mode == "http_json" and self.base_url)
                or (self.mode == "gpt_sovits" and self.base_url and self.ref_audio_path)
                or (self.mode == "command" and self.command)
            ),
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
        }

    def spoken_text(self, text: str) -> str:
        """The text as it should be *heard*.

        The reply keeps ``（小声）`` so the bubble still shows the character
        doing something; only this view drops it, because a synthesiser would
        otherwise read the stage direction out loud. Transcript timestamps go
        too: ``send_voice`` synthesises the model's own text, which never passed
        through :func:`normalize_reply_segments`.
        """
        value = strip_timestamps(text or "")
        if not self.strip_stage:
            return value
        return strip_stage_directions(value, max_chars=self.stage_max_chars)

    def synthesize(self, text: str, *, emotion: str | None = None) -> TTSResult:
        text = self.spoken_text(text).strip()
        if not text:
            return TTSResult(False, error="empty_text")
        if not self.enabled:
            return TTSResult(False, error="disabled")
        out_path = self._cache_path(text, emotion)
        if out_path.exists() and out_path.stat().st_size > 0:
            return TTSResult(
                True,
                audio_path=str(out_path),
                format=self.format,
                provider=self.mode,
                cached=True,
            )
        # 缓存命中放在探测之前：家里关机时，**已经合成过的句子照样能发**。
        if self.capability_probe is not None and not self.capability_probe.home_online():
            return TTSResult(False, error="home_offline")
        started = time.time()
        if self.mode == "http_json":
            result = self._http_json(text, emotion, out_path)
        elif self.mode == "gpt_sovits":
            result = self._gpt_sovits(text, emotion, out_path)
        elif self.mode == "command":
            result = self._command(text, emotion, out_path)
        else:
            return TTSResult(False, error=f"unknown_tts_mode:{self.mode}")
        result.latency_ms = (time.time() - started) * 1000
        self._metric(
            "tts_generated" if result.ok else "tts_error",
            1.0,
            {"mode": self.mode, "error": result.error or ""},
        )
        return result

    def iter_synthesize(
        self, text: str, *, emotion: str | None = None
    ) -> Iterator[tuple[str, TTSResult]]:
        """Yield ``(segment, result)`` as each short segment is synthesized.

        Segments come from :func:`split_short_sentences` using
        ``tts.max_chars_per_sentence`` and ``tts.min_chars_per_sentence``.
        Stage directions are removed *before* the split, so a yielded segment
        is exactly what the listener hears. A failed or raised synthesis never
        stops the remaining segments.
        """
        max_raw = self.config.get("tts.max_chars_per_sentence", 20)
        min_raw = self.config.get("tts.min_chars_per_sentence", 4)
        max_chars = int(max_raw) if max_raw is not None else 20
        min_chars = int(min_raw) if min_raw is not None else 4
        spoken = self.spoken_text(text)
        policy = str(self.config.get("tts.segment_policy", "chars") or "chars").lower()
        if policy == "sentence":
            # Never cut inside a sentence; see fatfish/tts/segmentation.py.
            segments = [item.text for item in plan_segments(spoken, max_chars=max_chars)]
        else:
            segments = split_short_sentences(
                spoken, max_chars=max_chars, min_chars=min_chars
            )
        for segment in segments:
            try:
                result = self.synthesize(segment, emotion=emotion)
            except Exception as exc:  # one bad segment must not stop playback
                result = TTSResult(
                    False,
                    provider=self.mode,
                    error=f"tts_exception:{type(exc).__name__}:{exc}",
                )
            yield segment, result

    def synthesize_segments(
        self, text: str, *, emotion: str | None = None
    ) -> list[tuple[str, TTSResult]]:
        """Eager list form of :meth:`iter_synthesize` for simple callers."""
        return list(self.iter_synthesize(text, emotion=emotion))

    def _http_json(self, text: str, emotion: str | None, out_path: Path) -> TTSResult:
        if not self.base_url:
            return TTSResult(False, error="missing_tts_base_url")
        # ``emotion`` 现在是**类别名**而不是自由文本：模型只负责选类别，
        # 速度和（历史遗留的）措辞由 fatfish/tts/instruct.py 映射。
        tone = normalize_tone(emotion)
        instruct = instruct_for(tone) if self.instruct_enabled else None
        # 速度是**唯一**还在工作的语气旋钮（六档 1.00~1.10，"元气地板"不许低于
        # 1.00）。instruct_enabled 关掉时速度也回到 1.00 —— 那才是"改动之前"。
        rate = speed_for(tone) if self.instruct_enabled else 1.0
        variables = {
            "text": text,
            "voice": self.voice,
            "format": self.format,
            "emotion": "" if tone == "neutral" else tone,
            "instruct": instruct or "",
            "speed": "%g" % rate,
            "engine": self.engine,
        }
        if self.payload_template:
            # 模板模式保持完全可控：要断句就自己在模板里写 text_frontend，
            # 因为把它塞进字符串替换会让 JSON 里出现 "false" 这种字符串真值。
            payload = _render_template(self.payload_template, variables)
        else:
            payload = {
                "text": text,
                "voice": self.voice,
                "format": self.format,
                "engine": self.engine,
                "emotion": variables["emotion"],
                # 断句和文本规范化交给模型自己的前端：我们按 20 字硬切再拼，
                # 正是「听着断句不对」的主要来源。
                "text_frontend": self.text_frontend,
            }
            if instruct:
                payload["instruct"] = instruct
            # 速度每次都明写：这张表里每一档都有定义值（sad 正好是 1.0），
            # "缺字段"和"写 1.0"是两种不同的语义，别让服务端去猜。
            payload["speed"] = rate
        headers = {"content-type": "application/json", **(self.extra_headers or {})}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get("content-type", "")
                body = resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            return TTSResult(False, error=f"tts_http_error:{type(exc).__name__}")

        if content_type.startswith("audio/"):
            out_path.write_bytes(body)
            return TTSResult(True, audio_path=str(out_path), format=self.format, provider="http_json")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return TTSResult(False, error="tts_invalid_response")

        if isinstance(data, dict):
            audio_path = data.get("audio_path") or data.get("path")
            if audio_path and Path(str(audio_path)).exists():
                return TTSResult(
                    True,
                    audio_path=str(audio_path),
                    format=str(data.get("format", self.format)),
                    provider="http_json",
                )
            audio_b64 = data.get("audio_base64") or data.get("audio")
            if isinstance(audio_b64, str):
                try:
                    raw = base64.b64decode(audio_b64)
                except Exception:
                    return TTSResult(False, error="tts_bad_base64")
                out_path.write_bytes(raw)
                return TTSResult(True, audio_path=str(out_path), format=self.format, provider="http_json")
            if data.get("url"):
                return TTSResult(
                    True,
                    audio_path=str(data["url"]),
                    format=str(data.get("format", self.format)),
                    provider="http_json",
                )
        return TTSResult(False, error="tts_no_audio_in_response")

    def _gpt_sovits(self, text: str, emotion: str | None, out_path: Path) -> TTSResult:
        if not self.base_url:
            return TTSResult(False, error="missing_tts_base_url")
        if not self.ref_audio_path:
            return TTSResult(False, error="missing_ref_audio_path")
        payload = {
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "text_split_method": self.text_split_method,
            "batch_size": self.batch_size,
            "media_type": self.media_type,
            "streaming_mode": self.streaming_mode,
        }
        headers = {"content-type": "application/json", **(self.extra_headers or {})}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get("content-type", "")
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                pass
            return TTSResult(False, error=f"tts_http_{exc.code}:{detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            return TTSResult(False, error=f"tts_http_error:{type(exc).__name__}")
        if content_type.startswith("audio/") or (body[:4] == b"RIFF"):
            out_path.write_bytes(body)
            return TTSResult(True, audio_path=str(out_path), format=self.media_type, provider="gpt_sovits")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return TTSResult(False, error="tts_invalid_response")
        if isinstance(data, dict) and data.get("message"):
            return TTSResult(False, error=str(data["message"])[:300])
        return TTSResult(False, error="tts_no_audio_in_response")

    def _command(self, text: str, emotion: str | None, out_path: Path) -> TTSResult:
        command = self.command
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            return TTSResult(False, error="missing_tts_command")
        args = [
            str(part)
            .replace("{text}", text)
            .replace("{out}", str(out_path))
            .replace("{voice}", self.voice)
            .replace("{format}", self.format)
            for part in command
        ]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return TTSResult(False, error=f"tts_command_error:{type(exc).__name__}")
        if out_path.exists() and out_path.stat().st_size > 0:
            return TTSResult(True, audio_path=str(out_path), format=self.format, provider="command")
        detail = (proc.stderr or b"").decode("utf-8", errors="ignore")[:300]
        return TTSResult(False, error=f"tts_command_no_output:{detail}")

    def _cache_path(self, text: str, emotion: str | None) -> Path:
        # Provider/reference are part of the identity: switching from an old
        # GPT-SoVITS voice to CosyVoice must never reuse a stale WAV.
        # speed 目前由 emotion 派生，理论上已被上面的 emotion 字段覆盖；仍然写进
        # 键里，是为了将来 speed 变成可独立配置时不必回头记得改这里 —— 缓存键漏项
        # 是最难查的一类 bug（改了参数，听到的还是旧音频）。pitch_semitones 同理：
        # 服务端按它叠加升调，旧缓存都是按旧 pitch 合成的，不能复用。
        rate = speed_for(emotion) if self.instruct_enabled else 1.0
        key = (
            f"{self.engine}|{self.cache_version}|{self.mode}|{self.base_url}|{self.voice}|"
            f"{self.ref_audio_path}|{self.format}|{text}|{emotion or ''}|{rate:g}|"
            f"{self.pitch_semitones}"
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        return self.output_dir / f"{digest}.{self.format}"

    def clear_cache(self) -> int:
        """Delete cached audio owned by this client format; return file count."""
        removed = 0
        for path in self.output_dir.glob(f"*.{self.format}"):
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _metric(self, key: str, value: float, tags: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (time.time(), key, float(value), json.dumps(tags, ensure_ascii=False)),
        )


def _render_template(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for key, replacement in variables.items():
            result = result.replace("{" + key + "}", str(replacement))
        return result
    if isinstance(value, list):
        return [_render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_template(item, variables) for key, item in value.items()}
    return value
