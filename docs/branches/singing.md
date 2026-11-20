---
title: 唱歌
type: branch
status: active
tags: [singing, yue2, rvc, lyrics, svs, gpu, remote]
updated: 2026-11-20
---

# 唱歌

> 上级：[项目总览](../overview/project-status.md) · 相关：[语音](voice.md) · [记忆](memory.md) · [运维](ops.md) · 决策：[ADR-0016](../adr/0016-yue2-rvc-singing-chain.md) · [ADR-0017](../adr/0017-singing-all-yue2-rvc.md) · [ADR-0018](../adr/0018-singing-full-lyrics.md)

## 1. 结论

- 产线只有一条链路：**YuE2 全曲 → Demucs 分离 → RVC 鲸鱼娘换声 → 首尾静音门 → 混音**。
- renderer 是 `soulx/song_yue2_rvc.py`；`config/low.toml [sing].script` 指向它。
- `sing.semitones = 0`（保留 YuE2 旋律），`sing.timeout_seconds = 720`（YuE2 约 5–6 分钟）。
- 旧 SoulX `song_v2.py` 链路已从主仓删除，只在 ADR-0002/0010/0015 保留决策记录。
- 云端不渲染：`[sing] mode = "remote"`，POST 回家里 `scripts/sing_server.py`，家里没开就立刻回一句文字。

## 2. 链路

| # | 步骤 | 位置 |
|---|---|---|
| 1 | 收歌名/情绪/完整歌词（4–16 句，可带 `[Verse]/[Chorus]`） | `fatfish/sing/service.py`、`fatfish/runtime.py` |
| 2 | 歌词不合格先打回模型重写一次，再兜底默认歌 | `fatfish/sing/service.py`、ADR-0018 |
| 3 | 抢 `data/run/vram.lock`，`POST 9880/unload` 让 TTS 让出显卡 | `fatfish/sing/service.py`、`data/run/vram.lock` |
| 4 | 子进程 `soulx/song_yue2_rvc.py`：YuE2 生成 → Demucs → RVC `v10g_e30` | `.venv-soulx` / `.venv-train` |
| 5 | 首尾静音门 + 混音，写 `<out>/full.wav` | `soulx/song_yue2_rvc.py` |
| 6 | `finally` 里 `POST 9880/load` 还回 TTS；写 `sing_jobs` | `fatfish/sing/service.py` |
| 7 | QQ `record` / Web `/api/audio` 交付；成功写一条 `memory_items(source_type='sing')` | `fatfish/platforms/qq.py`、`web/server.py`、`fatfish/runtime.py` |

RVC 定版参数：`index 0.60 / protect 0.50 / RMVPE`，模型 `v10g_e30`；默认 `soft`，另有 `genki`、`sad`。pitch 默认 0，按歌再调。

## 3. 配置

`config/low.toml [sing]`：

| 键 | 产线值 | 说明 |
|---|---|---|
| `enabled` | `true` | |
| `python` | `.venv-soulx/Scripts/python.exe` | 渲染环境，应用本体仍零依赖 |
| `script` | `soulx/song_yue2_rvc.py` | 唯一产线 renderer |
| `default_src` | `data/soulx/persona_song/report.json` | 不写歌词时的默认歌 |
| `output_dir` | `songs`（相对 `data_dir`） | |
| `emotion` | `soft` | `soft` / `genki` / `sad` |
| `semitones` | `0` | 保留 YuE2 旋律 |
| `timeout_seconds` | `720` | 超时杀进程并还显卡 |
| `min_free_vram_mb` | `5600` | `preflight --for svs` 门槛 |
| `unload_tts` / `reload_tts` | `true` / `true` | 8GB 卡串行 |
| `vram_wait_seconds` | `20` | |

renderer 的外部路径通过环境变量覆盖，不写死在配置里：

| 变量 | 指向 |
|---|---|
| `YUE2_ROOT` | YuE2 安装目录（含 `.venv`、`models/YuE2-3B`、`models/YuE2-Vae`） |
| `RVC_ROOT` | RVC 工作目录（含 `.venv-train`、`convert_audio.py`） |
| `RVC_PTH` / `RVC_INDEX` | RVC 模型与索引 |

模型权重不随仓分发；按 README/THIRD_PARTY_NOTICES 的链接自行下载。

## 4. 云端唱歌（remote）

云端没有 `.venv-soulx` / 模型，只做编排：

```toml
[sing]
mode = "remote"
remote_url = "https://<home>.<tailnet>.ts.net:9445"
remote_token_env = "SING_SERVER_TOKEN"
remote_timeout_seconds = 900
```

- 家里 `scripts/sing_server.py` 走同一套 `SingService`：抢锁 → 卸 TTS → 渲染 → `finally` 回载 → 回 `audio/wav`。
- 云端 `reachable()` 先做 2s TCP 探活；家里关机/超时立刻返回 `home_offline` / `remote_timeout`，只回一句文字，不让用户等满 900s。
- `busy`（正在唱上一首）、`remote_bad_audio`、`remote_not_configured` 都有固定文案。
- 云端显式跳过本地 `/unload` `/load`，显存交接只发生在家里。

## 5. 歌词与资产

- 自定义歌词：`lyrics` 为 4–16 句正文，每句 4–16 字；`[Verse]`/`[Chorus]` 等 section tag 保留但不计入句数。
- 写词规则来自 `skills/whale-lyrics`（离线资产，不随发布树打包）；产物由 `fatfish/sing/service.py` 的 `clean_lyrics` 兜底。
- 默认歌与参考音资产：`assets/song/persona_song.json`、`assets/song/prompt_v6.json`、`assets/voice/cand_home_up1.wav`，由 `scripts/install_assets.py` 播种到 `data/`。
- `data/soulx/prompt_v6.json` 是参考音对齐 metadata；重建要重新对齐。
- 旧 `data/soulx/melody_*`、`song_final` 等历史 take 不参与产线；需要时从归档区取。

## 6. 测试

| 测试 | 覆盖 |
|---|---|
| `tests/test_song_yue2_rvc.py` | renderer 纯函数：歌词解析、请求体、参数、song id |
| `tests/test_sing_service.py` | 本地渲染编排、显存/TTS 交接、错误文案 |
| `tests/test_sing_remote.py` | 云端 remote 传输、探活、超时/坏音频 |
| `tests/test_tts_instruct.py` / `test_tts_indextts.py` | 语气映射（与唱歌共用 tone 归一化） |

不联网、不加载模型：renderer 的 `librosa/numpy/soundfile` 是可选导入，纯函数测试在标准库环境可跑。

## 7. 已知限制

- 一次只唱一首，与 TTS/拍照共用 8GB 卡；渲染期间 TTS 让位。
- YuE2 生成有随机性，同一配置两次结果不完全一致；定版 take 才写 `song_final`。
- 渲染 5–6 分钟是常态，长回合；云端靠 remote + 即时失败文案兜底。
- 模型/环境不随仓分发；缺 `.venv-soulx`、YuE2、RVC 时 `sing` 返回友好错误，不崩。

## 8. 历史

- SoulX-Singer + 规则旋律的旧链路：[ADR-0002](../adr/0002-singing-soulx.md)（历史）。
- `sing` 动作/sing_jobs/显存串行：[ADR-0010](../adr/0010-sing-action.md)（仍有效）。
- 确定性采样与评测：[ADR-0015](../adr/0015-singing-eval-determinism.md)（历史工具）。
- 统一到 YuE2→RVC：[ADR-0017](../adr/0017-singing-all-yue2-rvc.md)。
