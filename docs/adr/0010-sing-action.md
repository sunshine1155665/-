---
title: ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行
type: adr
status: accepted
tags: [adr, singing, soulx, action, vram, tts, schema]
updated: 2026-10-26
---

# ADR-0010：唱歌接入——`sing` 动作、`sing_jobs` 与显存串行

> 承接 [ADR-0002](0002-singing-soulx.md)（SoulX-Singer 离线链路）与 [唱歌分支](../branches/singing.md)（v17 定版）。决定把「点歌」作为产品动作接进 `fatfish`，而不是继续停在 `staging_soulx/` 脚本层。

## 背景

离线链路已定版：`data/soulx/song_final/`（v17，16.4s，默认情绪 `soft`）经 boss 验收通过（采用默认版本）。但 `fatfish/` 里没有任何 `sing` 引用，QQ/Web 都无法点歌。接入必须同时解决三件事：

1. **8 GB 显卡互斥**：TTS（9880）与 SoulX 不能同时驻留；`data/run/vram.lock` 是产品侧（拍照）和 agent 共用的租约，唱歌必须走同一把锁。
2. **零第三方依赖**：SoulX 依赖 `torch`/`numpy`/`soundfile`，只能跑在 `.venv-soulx` 里；应用本体只允许 subprocess。
3. **回合阻塞的诚实交代**：一条歌需要 1–3 分钟，渲染期间用户收不到任何消息。要么异步任务队列，要么像拍照一样同步阻塞并在同一轮交付。当前选择后者（见「决策」）。

## 决策

### 1. `sing` 是动作，不是关键词旁路

`ACTION_SPECS["sing"]`：私聊、每轮最多 1 次、群聊拒绝。LLM 只在对方明确点歌时使用；`emotion` 取 `soft`（默认）/`genki`/`sad`；可选 `lyrics` 为四句、每句 4–14 字。没有歌词就唱定版《等你回家》。群聊、陌生人、公开场合永不唱歌。

### 2. 同步执行，同一轮交付

`execute_actions` 里同步调用 `SingService.sing()`，成功后 QQ 把 `result["sing"]["path"]` 作为 `record` 媒体入 outbox（与 TTS 语音用 `index=2` 区分），Web 通过 `/api/audio` 映射 `audio_url`。失败不吞文字回复：追加一句 `friendly_sing_error()`。

照片（T-0016）已经证明「同步等模型 + 同一轮交付」在这套本地应用里可用；唱歌沿用同一交互，不引入后台 worker、不做跨回合推送。代价是这一轮会等 1–3 分钟，收益是没有孤儿任务、不需要新的投递协议、TTS 交接在 `finally` 里必然执行。

### 3. `sing_jobs` 是台账，不是队列

Schema v10 新增 `sing_jobs`：`id / session_id / created_at / finished_at / status / emotion / lyrics_json / audio_path / error / tts_unloaded / elapsed_ms`。一次点歌一行，状态 `running → done|failed`；歌词以 JSON 数组保存，音频落在 `data/songs/<job_id>/full.wav`。「她唱了什么」在重启后仍可核对。

### 4. 显存串行与 TTS 交接

`SingService` 同时持有：

- 进程内 `threading.Lock` + 跨进程文件锁 `data/run/sing.lock`（一次只唱一首）；
- 共享租约 `data/run/vram.lock`（与拍照、`gpu.py` 闸门互斥）。

拿到 vram 租约后先看空闲显存：达到 `sing.min_free_vram_mb`（默认 6800）就直接开跑；不够则 `POST /unload` 让 TTS 释放权重并等待 `/health.loaded=false` 或显存达标。**无论成功、失败还是超时，`finally` 都先释放租约、再 `POST /load` 把 TTS 还回去**。若 TTS 无法释放显存，任务以 `vram_conflict_tts` 失败，不会在低显存下硬启动第三个模型。

### 5. 歌词兜底优先于点歌失败

`lyrics` 缺省/为空 → 唱定版；给了但格式不对（不是四句、单句长度越界）→ 记录 `lyrics_error`，仍唱定版，不让一句坏词毁掉整个回合。歌词落盘为临时 `lyrics.json`，字段只有 `title` + `phrases[].text`，与 `song_v2.py --src` 的既有格式一致。

## 配置（`config/low.toml [sing]`）

`enabled` / `python` / `script` / `soulx_root` / `deps_path` / `default_src` / `prompt_wav` / `prompt_meta` / `output_dir` / `emotion` / `semitones` / `timeout_seconds` / `min_free_vram_mb` / `unload_tts` / `reload_tts` / `vram_wait_seconds`。`standard`/`remote` 不覆盖，沿用 low 的值（profile 漂移守卫会在复制粘贴时红测）。

## 失败面

| 错误码 | 含义 | 用户看到 |
|---|---|---|
| `disabled` | `sing.enabled=false` | 「嗓子还没开」 |
| `busy` / `sing_busy` | 已有任务持锁 | 「正在唱上一首」 |
| `vram_busy` | vram 租约被拍照/闸门占用 | 「显卡正忙」 |
| `vram_conflict_tts` | 卸了 TTS 仍不够显存 | 「先卸载 TTS」 |
| `python_missing` / `script_missing` / `missing_assets` | 运行环境/资产缺失 | 对应说明 |
| `render_timeout` | 超时被杀进程 | 「录到一半超时了」 |
| `render_failed:*` | 非零退出/无输出 | 「这次没唱好」 |

## 后果

- **正**：QQ/Web 说「唱首歌」即可收到定版歌曲；显存互斥、TTS 回载、任务台账、失败话术全部有测试；应用本体仍零第三方依赖。
- **负**：一轮聊天会被渲染阻塞 1–3 分钟（与拍照同量级）；`sing_jobs` 只记录不重试，失败要用户再点一次；自定义歌词依赖 LLM 写四句短词，质量不做保证。
- **未做**：多 take 挑选、`--n-steps` 等唱法旋钮不暴露给 LLM；异步后台队列、跨轮推送、并发点歌排队均明确不做。

## 2026-10-26 补记：唱歌进入记忆

sing 成功后，runtime 在 `memory_items` 写一条 `source_type='sing'` 的记忆（gist「给…唱了《title》（emotion）」，自定义歌词附四句），`SingResult.title` 从定版 `default_src` 的 `title` 读取（默认《等你回家》），自定义歌词标为「你点的歌」。问「上次/最近唱了什么歌」时 `_recent_sing_evidence` 注入最近 5 条，提示词用 `[最近唱过的歌]`。`sing_jobs` 仍是音频/参数台账，不承担记忆检索；写记忆失败不影响歌曲交付。
