---
title: ADR-0017 唱歌统一走 YuE2→RVC，SoulX 链路删除
type: adr
status: accepted
tags: [adr, singing, yue2, rvc, soulx, config]
updated: 2026-11-20
---

# ADR-0017：唱歌统一走 YuE2→RVC，SoulX 链路删除

## 背景

ADR-0002 把产线唱歌定为 SoulX-Singer + 规则旋律；ADR-0010 接入了 `sing` 动作；
ADR-0016 又新增了 YuE2 全曲 → Demucs → RVC 的通用链，但当时仍保留 SoulX 作为
短句/score 路径。2026-11-10 boss 明确：**唱歌全部用 YuE2+RVC**，不再让 SoulX
作为默认点歌路径，避免两套产线并存、音色和参数口径分裂。

## 决策

1. `config/low.toml [sing]` 默认切换到 YuE2→RVC：
   - `script = "soulx/song_yue2_rvc.py"`
   - `semitones = 0`（保留 YuE2 旋律；要升降调再改）
   - `timeout_seconds = 720`（YuE2 约 5–6 分钟）
   - `soulx_root` / `deps_path` / `prompt_wav` / `prompt_meta` 置空（仅 SoulX 用）
   - `default_src` 保留四句默认歌，renderer 读 `phrases[].text`
2. `fatfish/sing/service.py` 的默认值与提示改为 renderer 无关：
   - `sing.semitones` 默认 0（原 3）
   - `sing.timeout_seconds` 默认 720（原 300）
   - `script_missing` 提示指向 `soulx/song_yue2_rvc.py`
3. 远程唱歌超时同步放宽：
   - `DEFAULT_REMOTE_TIMEOUT` 600s（原 420s）
   - `scripts/sing_server.py --timeout` 默认 600s（原 420s）
   - `config/cloud.toml` 的 `sing.remote_timeout_seconds` 600s
4. **2026-11-20 清理**：旧 SoulX 的 `song_v2.py`、`vendor/SoulX-Singer`、旧 melody/prosody/
   legato/reverb 代码与测试、一次性 demo 脚本**从主仓删除**；`ADR-0002` 标记 superseded。
   只有 ADR 保留决策记录，不再保留代码/资产回退路径。
5. 文档同步：README、唱歌分支、项目总览、路线图、架构、ADR 索引。

## 备选与放弃理由

| 方案 | 结论 |
|---|---|
| 保留 SoulX 作为短句路径 | 不采用；两套产线并存，音色/参数口径分裂 |
| 按歌词长度自动选 SoulX/YuE2 | 不采用；行为不可预期，调试成本高 |
| 删除 SoulX 代码与资产 | **已采用（2026-11-20）**；旧链路不再有产线价值，代码/资产从主仓删除，只留 ADR 记录 |

## 后果

**正面**

- 所有唱歌（点歌、默认歌、多情境）只有一条链，音色和参数口径统一。
- 配置、默认值和文档一次对齐，减少“代码切了但配置没切”的漂移。
- 旧 SoulX 代码/资产已删除，主仓不再背两套产线。

**负面 / 风险**

- 每首歌约 5–6 分钟，比 SoulX 短句慢；同步长回合更明显。
- 默认歌也改走 YuE2→RVC，首次点歌会明显变慢；`timeout_seconds=720` 给足余量。
- YuE2 随机性导致同词不同 take；不以 byte 级复现为目标。
- RVC 对未见歌手/极端音域的迁移仍有上限，后续按场景补数据重训。

## 证据

- `config/low.toml [sing]` 已切换；`SingService` E2E 344.7s（YuE2 279.4 / Demucs 10.4 / RVC 40.2）。
- 6 情境与长版生日成品：`/transitions/multiscene/`、`/transitions/e2e_sing/`。
- 最佳音频包：`<archive>/fatfish-best-audio-2026-11-10.zip`。

## 关联

- [唱歌分支](../branches/singing.md)
- [ADR-0002](0002-singing-soulx.md)（SoulX 历史路线；本条把产线默认切走）
- [ADR-0010](0010-sing-action.md)（`sing` 动作与显存串行）
- [ADR-0016](0016-yue2-rvc-singing-chain.md)（YuE2→RVC 链细节；本条取消其“SoulX 仍保留为默认短句路径”的部分）
