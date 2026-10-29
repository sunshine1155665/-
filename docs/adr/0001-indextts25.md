---
title: ADR-0001 产线 TTS 选用 IndexTTS-2.5
type: adr
status: accepted
tags: [adr, voice, tts, indextts25]
updated: 2026-10-25
---

# ADR-0001：产线 TTS 选用 IndexTTS-2.5

## 背景

2026-10-22 产线是 CosyVoice3-0.5B + 软声服务。声音可用，但情绪控制靠 instruct，档位不稳；用户要求更像“她本人”，并能做更细的语气。

## 决策

- 引擎：IndexTTS-2.5，`models/IndexTTS-2.5`，服务 9880。
- 参考音：统一 `cand_home_up1.wav`（五档共用一条真实录音）。
- 情绪：8 维向量，映射为六档（neutral/happy/soft/sad/annoyed/lively）。
- 后处理：**+2 半音**（重采样 1.122 + 时长还原），与生产感知一致。
- 切段：句子级，20 字上限，逐句入 outbox。

## 备选与放弃理由

| 方案 | 放弃原因 |
|---|---|
| CosyVoice3-0.5B | 情绪控制弱、音色不像本人；5.1GB 环境仍保留作 torch 供体 |
| Edge TTS | 只能定方向，不能私有部署 |
| GPT-SoVITS | 质量参考，不作为产线 |
| IndexTTS-2.0 | 已被 2.5 替代，权重已删 |

## 后果

**正面**：音色获用户认可；六档可控；与唱歌共用参考音。

**负面**：

- 模型 10.2GB，加载慢；8GB 卡上与 ComfyUI/SoulX 互斥。
- RTF≈3.24，首句 10–20s，不适合连续实时对话。
- 6 档之外的表情（8D 原型）尚未接线。

## 复核时间

延迟优化后（目标首句 < 5s）重新评估是否仍以 2.5 为唯一产线。

## 注记（2026-10-25，不改上方原决策）

「后处理 +2 半音」已废止为**默认关闭的配置项**：复盘发现参考音
`cand_home_up1.wav` 离线制作时**已经 +2 半音**，管线再叠 +2 实际是 +4，音高
过头（用户反馈）。现在叠加由 `tts.pitch_semitones` 控制、默认 **0**，只在
换回原调参考音时设 >0（机制不变：`asetrate` + `atempo`）。详见
[语音与 TTS](../branches/voice.md) 的「音高」一节。
