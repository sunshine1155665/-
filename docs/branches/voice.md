---
title: 语音与 TTS
type: branch
status: active
tags: [voice, tts, indextts25, reference-audio, pitch, gpu, latency]
updated: 2026-10-25
---

# 语音与 TTS

> 上级：[系统架构](../overview/architecture.md) · 相关：[唱歌](singing.md) · [运维](ops.md) · [资产](assets.md) · 决策：[ADR-0001](../adr/0001-indextts25.md)

## 结论

生产引擎是 **IndexTTS-2.5**（9880），六档语气 + 统一参考音；参考音本身已含 **+2 半音**，管线叠加升调默认关闭（`tts.pitch_semitones = 0`）。音色已被用户认可；瓶颈是**延迟**（RTF≈3.24），不适合连续实时对话。

## 实现

| 环节 | 做法 | 位置 |
|---|---|---|
| 服务 | 常驻 HTTP，`POST /tts` 返回完整 WAV（非流式） | `scripts/indextts_server.py` |
| 参考音 | `cand_home_up1.wav`，22050Hz / 5.92s，六档共用 | `data/whale_voice_demo_v2/refs/` |
| 语气 | 文本 → 8 维情绪向量 | `fatfish/tts/indextts.py:vector_for` |
| 变调 | 默认不叠加；`tts.pitch_semitones` >0 时 `asetrate` + `atempo` | `fatfish/tts/indextts.py:post_filter_for` |
| 切段 | 句子级，20 字上限 | `fatfish/tts/segmentation.py` |
| 发送 | 逐句写入 outbox | `fatfish/outbox.py` |

六档向量（当前代码值）：

| tone | 向量 |
|---|---|
| neutral | `{}` |
| happy | happy .55 / surprised .12 / calm .08 |
| soft | happy .45 / melancholic .05 / calm .25 |
| sad | happy .12 / sad .38 / melancholic .07 / calm .18 |
| annoyed | happy .22 / sad .30 / surprised .03 / calm .12 |
| lively | happy .58 / surprised .25 / calm .05 |

## 音高（`tts.pitch_semitones`）

**参考音 `cand_home_up1.wav` 离线制作时已经 +2 半音**（v6 身份锚点的一部分）。
管线曾经再叠加 +2（`asetrate=22050×1.122, atempo=1/1.122`），两层相加是
**+4**，音高过头 —— 2026-10-25 用户反馈后，叠加改为配置且默认关闭：

- `tts.pitch_semitones = 0`（默认）：服务端只做 16bit/单声道归一，不改变音高。
- `tts.pitch_semitones = N`（N>0）：对模型输出再升 N 半音，机制仍是
  `asetrate` 提频 + `atempo` 时长还原（`fatfish/tts/indextts.py:post_filter_for`）。
- **只应在换回原调参考音时设 >0**；对 `cand_home_up1.wav` 保持 0。
- 传递链：`config/*.toml [tts].pitch_semitones` → `start_all.tts_pitch_semitones()`
  → `indextts_server.py --pitch-semitones`（服务启动横幅与 `/health` 的
  `pitch_semitones`/`post_filter` 字段可核对是否生效；**改完需重启 9880 服务**）。
- 该值在客户端缓存键里（`fatfish/tts/client.py:_cache_path`），改动后旧 pitch
  合成的缓存音频自动失效，无需手动 bump `cache_version`。
- 负值一律按 0 处理：该机制只做升调，不提供降调。

## 资产

| 路径 | 大小 |
|---|---|
| `models/IndexTTS-2.5` | 10,466MB |
| `.venv-indextts` | 1,034MB |
| `data/whale_voice_demo_v2/refs/cand_home_up1.wav` | 261,150B |
| `data/audio_8d_scenes_v6/` | 20 文件 / 1.2MB（离线原型，未接线） |

## 已验证

- 产线已切 2.5；真实库未被测试污染（历史检查）。
- 切段单测 11 条通过；相关单测 73 条通过。
- 六档向量与参考音策略已在实机使用。
- 2026-10-25 音高叠加改配置化（默认 0，见上节）；全量单测 1040 条通过
  （3 条环境跳过，与本改动无关）。

## 已知问题

1. **延迟**：12.21s 音频耗时 39.6s，首句 10–20s；连续播放会 5–20s 空白。
2. `pause_after_ms` 仍按 2.0 标定，2.5 上会叠加，需重标。
3. `soft` 档最弱（唯一没有稳定 5 分的档）。
4. 8D 原型（genki/moe/tsundere/…）未接进 `indextts.py`。
5. `config/tts25-live.toml` 把 TTS 指向 9882（控制面），是遗留冲突。
6. 显存文档自相矛盾（5.5GB vs 7.2GB），未实测峰值；8GB 卡上属高危。

## 下一步

- 首句优先 + 短句缓存，验收线 < 5s。
- 重新标定 `pause_after_ms`。
- 决定 8D 是否接线（写 ADR）。
- 修/删 `tts25-live.toml` 端口冲突。

## 历史资料（已归档）

研发期的 voice 系列交接文档（`handover-tts25-2026-10-24.md`（最详细）、`handover-voice-current.md`、`voice-emotion.md`、`voice-consistency.md`、`voice-streaming.md`、`tts-service.md`、`memory-budget.md`、`whale-persona-voice-map.md`、`whale-voice-8d-plan.md`）已归档到本机外部归档盘（路径不入库），未随发布版分发。
