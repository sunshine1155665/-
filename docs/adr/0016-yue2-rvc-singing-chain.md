---
title: ADR-0016 唱歌通用链：YuE2 全曲生成 + RVC 鲸鱼娘换声
type: adr
status: accepted
tags: [adr, singing, yue2, rvc, timbre, gpu]
updated: 2026-11-08
---

# ADR-0016：唱歌通用链——YuE2 全曲生成 + RVC 鲸鱼娘换声

## 背景

ADR-0002 的 SoulX 链路是「先有旋律/score，再唱」：适合定版短句，但换情境时要先有谱，
词曲一体的生成能力弱。2026-11-08 的实测目标从「一首定版」扩大到「多个日常情境都能唱」，
需要一条能从一句情境直接生成完整歌、再换成鲸鱼娘音色的通用链。

已有资产：

- YuE2-3B 可以 `cot=full` 直接生成 4 句歌（人声+伴奏），4 句约 32–53 秒；
- RVC `rvc_loli_mixed_c_e60` 是鲸鱼娘微调的单级模型（TTS-heavy），
  早期「c_e60 + light_e40 级联」有电音/金属感，已放弃；
- Demucs `htdemucs` 可将 YuE2 混音拆成人声/伴奏；
- `sing` 动作（ADR-0010）已经能通过 `SingService` 调外部 renderer，
  只需一个兼容 `--emotion/--out/--src/--semitones`、输出 `full.wav` 的脚本。

## 决策

1. **YuE2 声音约束用 `young female vocal`**：soft / clear diction / narrow range /
   no high notes。童声/女童声（`8-10 year old girl voice`）B 版只留档，不进入正式链；
   实测它对 F0 的控制因风格而异（雨天/生病/海边升高，早安/想念/赖床不变或下降），
   且人耳 A/B 判定 A 已足够。
2. **RVC 单级 c_e60**：`index_rate=0.25`、`protect=0.50`、`f0_method=rmvpe`。
   index 矩阵 0.0–0.75 实测：0.2–0.4 为甜区，0.3 相似度最高但 0.25 听感更稳；
   ≥0.5 flatness 抬头、金属感风险增加。锁定 0.25。
3. **pitch 按歌定**：默认 0（保留 YuE2 旋律）；偏高再 −2/−3。RVC 只换音色，
   不把不同歌强行拉到同一 F0。
4. **首尾静音门**：RVC 会在纯静音段写低电平噪声；混音前按源人声 RMS 做 0.02s
   渐变门限，避免伴奏一停就听见嘶声。
5. **混音**：vocal 0.85 / instrumental 0.55，输出 48kHz stereo PCM16。
6. **产品 renderer**：`soulx/song_yue2_rvc.py`，兼容 `SingService`；
   正式接线要求 `sing.timeout_seconds >= 600`、`sing.semitones = 0`、
   `sing.script` 指向该脚本、`sing.python` 用 `.venv-soulx`。
7. **应用本体仍零第三方依赖**：YuE2/Demucs/RVC 全部在 `.venv-*` 里以 subprocess
   运行，`fatfish/` 不新增依赖。

## 备选与放弃理由

| 方案 | 结论 |
|---|---|
| 童声/女童声 prompt B 版 | 留档，不采用；F0 控制不稳定，A 版已够 |
| RVC 级联（c_e60 + light_e40） | 电音/金属感明显，放弃 |
| index 0.5–0.75 | 音色更近但 flatness 抬头、金属感风险；不采用 |
| 把每首歌都 auto-pitch 到 295Hz | 会改旋律/调性；改为 pitch 0，按歌再调 |
| SongComposer 7B 原生填音 | 质量可用但 16.7GB、约 10 分钟/13 秒，只作离线质量模式；模型已归档 E |
| ACE-Step 旧链 | 已归档 E，不再作为唱歌主路径 |

## 后果

**正面**

- 6 个情境（早安/雨天/生病/想念/赖床/海边）全部走通，换歌、换人、换音高测试
  有声帧旋律吻合 86.9–96.2%，没有跑调式崩溃。
- `SingService` E2E 实测：job done，总 344.7s（YuE2 279.4 / Demucs 10.4 / RVC 40.2），
  输出 42.86s / 48kHz stereo；源 F0 311→312.8Hz。
- 音色由人耳 A/B 定版；工程上 index/pitch/混音参数可复现。

**负面 / 风险**

- 生成式链路比 SoulX 慢：一首约 5–6 分钟，必须同步长回合；`timeout_seconds`
  默认 300 不够，正式配置建议 720。
- YuE2 每次随机，同一歌词不同 seed 会得到不同旋律；不是 byte 级复现。
- RVC 对未见歌手的迁移仍有上限；若后续某情境明显不像，优先补童声/鲸鱼娘歌唱数据
  重训单级 RVC，而不是继续调 prompt。
- 本次 E2E 用 `vram_free=8000` 绕过 TTS 交接；TTS 常驻时的 unload/reload 还需实跑。

## 证据与入口

- 6 情境 A 版：`/transitions/multiscene/`；童声 A/B 留档：`/transitions/child_voice/`
- 换歌/换人/音高：`/transitions/generalization/`；index 矩阵：`/transitions/index_sweep/`
- E2E：`/transitions/e2e_sing/`；报告：
  `<YUE2_ROOT>/work/longform/e2e_sing/E2E_REPORT.md`
- 成品包：`<archive>/fatfish-singing-finals-2026-11-08.zip`
- 归档清单：`<archive>/MANIFEST.md`

## 关联

- [唱歌分支](../branches/singing.md)（新增「YuE2 → RVC 通用链」节）
- [ADR-0002](0002-singing-soulx.md)（SoulX 短句/score 链路仍保留）
- [ADR-0010](0010-sing-action.md)（`sing` 动作与显存串行）
- [ADR-0015](0015-singing-eval-determinism.md)（确定性/双层评测；本条不依赖 byte 级复现）
