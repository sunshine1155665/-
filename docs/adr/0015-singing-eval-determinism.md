---
title: ADR-0015 唱歌评测基座：确定性采样与双层自动评审
type: adr
status: superseded
tags: [adr, singing, eval, determinism, mimo]
updated: 2026-11-20
---

# ADR-0015：唱歌评测基座——确定性采样与双层自动评审

> **历史记录（2026-11-20）**：评测脚本/旧 SoulX 链路已从主仓删除；本文只保留当时的
> 评测设计，不再代表产线。

## 背景

ADR-0002 时代 SVS 输出非确定（同配置两次渲染波形相关 0.05），导致 A/B 不可控、
take 挑选纯人工、旋律层实验「v7→v11 五版在噪声内摆动」。外部项目
（Esmerubphire/feiyu_voice，无 License，仅借鉴思路不抄码）证明 SoulX 采样可用
「种子 + 确定性算法」钉死。评审方面，mimo 系音频模型可作听感评审
（singing-advice.md 四轮实测），但单模型单票噪声 ±1 分，且 flash 对局部音准
灾难失明（2026-11-04 实测：某 take 一个音差 2096 cents 仍得最高分）。

## 决策

1. **确定性采样**：`song_v2.py --seed N` → `apply_determinism()`（manual_seed +
   use_deterministic_algorithms(warn_only) + TF32 off + CUBLAS_WORKSPACE_CONFIG），
   不碰 vendor。实测进程内 / 跨进程 / 产品全链（含效果与混音）均 byte 级复现
   （`seed_probe.py` corr=1.0；`_repro_s8` 与 s8 `full.wav` cmp 一致）。
2. **seed 即 take**：同配置不同 seed = 不同 take；`pick_take.py` 按
   「F0 硬门（±50c ≥ 80% 且 mean|cents| ≤ 100）→ flash 六轴均值决胜」自动选。
3. **双层评测协议**：客观 F0（`eval_take.py`，可复现）做硬门，被淘汰的 take
   不进评审；`mimo-v2.6-flash`（网关已恢复音频输入）做 sweep 评审，
   `mimo-v2.6-pro` 只做终审；跨模型只比相对、不比绝对；flash 的转写与改谱
   处方不可信（误听实锤；其建议曾违反峰值封顶等既有守卫）。
4. **编排结论（待 boss 拍板「算不算歌」）**：念白+唱句混排（`spoken_intro.py`）
   让贴脸首获 5/5（flash），pro 终审同序（s8talk 3.83 > s8talk2 3.50 > s8 3.00）；
   简短文案（V1）优于长文案（V2），内容有敏感性。

## 备选与放弃理由

| 方案 | 结论 |
|---|---|
| SoulX SVC 换声（SVS→SVC） | 定量复测确认 ADR-0002：对已是参考音音色的唱段，MFCC 距参考 43.95 → 50.38（推离）；仅留翻唱场景 |
| genki 预设 / cfg 2.0 | 「表现力」换来 196–449 cents 音准劣化，F0 硬门淘汰 |
| SVS→RVC（Applio + HermitST 替身，CC BY-NC） | 评审证据显示音色已贴脸、扣分在唱法规整；RVC 只换音色不改唱法，期望值下调，管线验证进行中 |
| flash 转写做歌词核对 | 转写不可靠（probe2 与本次误听均证实）；CER 需专用 ASR（Paraformer），未做 |

## 后果

**正面**：A/B 从噪声比拼变成可控实验；take 挑选自动化（ADR-0010 的「多 take
挑选」未做项就此落地）；编排层（念白混排）成为角色一致性的最大杠杆
（+0.5 avg6，双评委同序）。

**负面**：byte 级复现绑定本机软硬件，跨机器不保证；flash 对音准的盲区依赖
客观硬门补位；编排「算不算歌」与念白文案定稿待人工裁决。

## 关联

- [唱歌分支](../branches/singing.md)（新增「确定性与自动评测」节）
- [ADR-0002](0002-singing-soulx.md)（补记：非确定后果已解除、SVC 定量复测）
- [ADR-0010](0010-sing-action.md)（多 take 挑选未做项 → `pick_take.py`）
- 试听页：`data/soulx/seed_sweep/index.html`（`serve_audio.py` + `tailscale serve`）

## 校准补记（2026-11-04 晚）

- **WavLM 音色相似度入列**（`voice_sim.py`，`microsoft/wavlm-base-plus-sv`，经
  hf-mirror 下载）：两个念白混排 take 相似度全场最高（s8talk2 0.962 /
  s8talk 0.950，纯唱 0.76–0.90）——音色贴脸自此有三条独立证据链
  （flash 贴脸轴 / pro 终审 / WavLM 余弦）同向。
- **5 组故意错配负例 5/5 平手**（`run_prosody.py`，fw4 vs fw4）：flash 的
  `fits_words` 轴无判别力（与 singing-advice 时代对照测试失败的记录一致）。
  常数轴不改排序，故 `pick_take` 不必剔除，但其数值不可单独解读；评审结论
  依赖贴脸/persona/自然等其余轴 + 客观硬门。
- 替身 RVC 管线（HermitST，CC BY-NC）跑通：16.4s 人声 6.3s 换声，但音色被
  推离自家参考（MFCC 43.95 → 103.5）——「自训才有意义」坐实，管线资产留档。
