---
title: 媒体能力（表情包 / 拍照 / 词库）
type: branch
status: active
tags: [sticker, photo, comfyui, lexicon, media, vram]
updated: 2026-11-18
---

# 媒体能力（表情包 / 拍照 / 词库）

> 上级：[项目总览](../overview/project-status.md) · 相关：[语音](voice.md) · [运维](ops.md) · [资产](assets.md)

## 表情包

| 项 | 事实 |
|---|---|
| 代码 | `sticker/service.py`（prompt 组装 + `sticker_jobs` 提交/轮询）、`Runtime.tick`（出图后补发） |
| 数据 | `sticker_jobs`（schema v16）；旧 `stickers` 表与 `data/stickers/` 资产不再被运行时读取 |
| 状态 | ✅ 产线：`send_sticker` → 魔搭 LoRA 现生成 → `qq_send_image` 补发 |
| 闸门 | `generation_max_per_10m=8` + `generation_max_pending=2`；失败静默（少一张图，不打断对话） |
| 冒烟 | `python -m fatfish sticker generate "挥手打招呼"`；`python -m fatfish sticker jobs` |
| 旧路线 | 2026-11-17 移除并从主仓删除：库内检索/意图/情绪闸门/底图加字/ComfyUI 兜底/9881 向量服务 |

无 `MODELSCOPE_API_KEY` 时 `StickerService.available=False`，`send_sticker` 返回 `generation_unavailable`，**不回落旧库检索**。

### 表情包 LoRA 与训练数据授权（2026-11-17）

魔搭已训练 `sunny1155665/whalegirl-sticker-v1`（Qwen-Image-2.1 底模，67 张、6000 步），
测试调用：`model="Qwen/Qwen-Image-2.1-Turbo"` + `loras=["sunny1155665/whalegirl-sticker-v1"]`
（`steps=8, guidance=1.0`，6–174s/张）。模型仓协议为 **CC BY-NC-SA 4.0**。

角色形象谱系（**溟月是角色名，不是作者**）：

1. 角色「溟月」——B站 UP「上善无形」原创，2025.06（蓝渐变发、呆毛、鲸鳍耳、鲸尾）；
2. 女仆鲸鱼娘（DeepSeek 元素二设）——B站 UP「ZipZipPipe」，2026.04；
3. 参考：`Iphsiibl/deepseek-whale-girl-gallery`（CC BY-NC-SA 4.0，含逐图署名）、
   `EDMOK/blue-fish-archive`（二创依 CC BY-NC-SA 4.0）。

训练数据上游仓库（MIT 仅覆盖各自贡献，不改变角色形象层协议）：

| 上游仓库 | commit | 贡献 | 许可 |
|---|---|---|---|
| `PC2005-cloud/dsh-pet` | 572cf0f | 27 张表情 | MIT（Copyright (c) 2026 PC2005-cloud） |
| `aliccc1315/dafeiyu-stickers` | 717d0cb | 33 静态 + 16 GIF 首帧 | MIT（排版配文；角色素材 PC2005-cloud） |

完整归属与 MIT 全文：[lora-sticker-attribution](../reports/evidence/lora-sticker-attribution.md)
（原 `work/lora-sticker-train/ATTRIBUTION.md` 已归档，工作区不再保留）。模型仓同名文件与
`LICENSE` 已同步。使用时须署名「溟月 / 上善无形」「女仆鲸鱼娘 / ZipZipPipe」并保持非商业、相同方式共享。

## 拍照

| 项 | 事实 |
|---|---|
| 代码 | `photo/service.py`，`runtime.py` 关键词触发 + 主动媒体路径 |
| 外部 | ComfyUI 8188（路径走 `config.local.toml [photo]`，不写进仓库）；或魔搭 API-Inference |
| 状态 | ✅ 产线默认魔搭 `mode="modelscope"`；本机 ComfyUI 保留 `mode="local"` |
| 问题 | 本机 ComfyUI 与 TTS 抢 8GB 显存；魔搭不占本机显存但受额度/排队影响 |

### 云端拍照（T-0068）

云端没有 GPU，也没有 ComfyUI。`[photo] mode = "remote"`（只在 `config/cloud.toml`）
让云上只做编排：`POST` 到家里那台机器的 `/photo`，拿回 png 存自己的 `data/photos`
并写自己的 `photo_jobs`，QQ / Web 的发送链路一行不用改。

| 位置 | 做什么 |
|---|---|
| 云 `fatfish/photo/service.py` `photo.mode="remote"` | 组 `{prompt, negative, job_id, width, height, seed?}` → POST 家里 `/photo` → 收 png 存自己的 `data/photos/<job>.png` → 写自己的 `photo_jobs` 行 |
| 家 `scripts/photo_server.py` | 走同一套 `PhotoService`：抢 `data/run/vram.lock` → 必要时卸 TTS / 自启 ComfyUI → 画图 → `finally` 放锁 → 回 `image/png` |

云上从不碰 ComfyUI，也不碰 9882 控制面；本机 profile 不写 `photo.mode`，默认 `local`，行为与之前完全一致。

### 协议（`scripts/photo_server.py`）

| 请求 | 返回 |
|---|---|
| `GET /health`（**不要 token**） | `{"ok": true, "busy": bool, "mode": "local", "comfyui_url": "..."}` |
| `POST /photo`，body `{"prompt": "…", "negative"?, "caption"?, "job_id"?, "width"?, "height"?, "seed"?}`，头 `X-Token` | `200 image/png` + 头 `X-Job-Id` / `X-Elapsed-Ms` |
| 同上，token 不对 | `401 {"error": "unauthorized"}` |
| 同上，正在拍上一张（或显卡被 TTS / 唱歌占着） | `409 {"error": "busy"\|"vram_busy"}` |
| 同上，家里画图超时 | `504 {"error": "comfyui_timeout"}` |
| 同上，本机没启用 / ComfyUI 起不来 / 模型缺失 | `503` |
| 同上，其它渲染失败 | `500`，`error` 是 `PhotoService` 的错误码，`message` 是给用户的那一句 |

`GET /health` 不鉴权且**不碰 `data/run/vram.lock`**：健康检查只是探针，一 acquire 就等于偷偷占显卡。这也正是云端 `fatfish remote` 探的地址——没有它，`photo_online()` 永远是红的。
并发保护两层：`photo_server` 自己的 `busy` 闸（第二个请求直接 409，`ThreadingHTTPServer` 保证 `/health` 在画图时也能答），以及家里原有的 `data/run/vram.lock`（与 TTS、sing 共用）。
每个请求在**请求线程里**新建一条 SQLite 连接并用完就关（`sqlite3` 有线程亲和性）。

### 暴露方式：tailscale serve（不要开公网）

```bash
# 家里（token 放环境变量，别写进命令行历史）
export PHOTO_SERVER_TOKEN='...'
python scripts/photo_server.py --host 127.0.0.1 --port 9946 --timeout 150 --emit-json
# 另一个终端：把 tailnet 的 9446 反代到本机 9946
tailscale serve --bg --https=9446 http://127.0.0.1:9946
tailscale serve status
```

每张图结束（成功、失败、被 409 拒）都打一行摘要：默认是
`job=<id> ok=True bytes=70234 elapsed=41200ms error=-`，加 `--emit-json` 换成
`summary {"event":"photo_job","ok":true,"job_id":"…","bytes":…,"elapsed_ms":…,"error":""}`，
云端把家里日志拽下来可以直接 `jq 'select(.event=="photo_job")'`。

云上 `/opt/fatfish/config/cloud.local.toml`：

```toml
[photo]
remote_url = "https://fatfish-home.<tailnet>.ts.net:9446"
remote_token_env = "PHOTO_SERVER_TOKEN"
remote_timeout_seconds = 180
```

要点：
* 只绑 `127.0.0.1`；没有 token 直接 exit 2，`--allow-anonymous` 也只允许 loopback。
* 端口与唱歌错开：sing 本地 9945 / serve 9445，photo 本地 9946 / serve 9446。
* `photo.mode = "remote"` 的配置**不能**用来启动 `photo_server`（会变成云端→家里→云端），`main()` 里直接拒绝。
* 仓库里的 `config/cloud.toml` 只放占位地址；真实 tailnet 名字写进 `cloud.local.toml`，别提交。`deploy/linux/cloud.local.example.toml` 里有同样的注释版。

### 超时与降级

* **两段预算**：家里 `--timeout` 默认 **150s**，云上 `photo.remote_timeout_seconds = 180`——家里先到点，回一个明确的 `504 comfyui_timeout`；云上要是先断，就只剩一句含糊的 `remote_timeout`。
* **判「家里不在」是秒级**：POST 前先 2s TCP 探活（`RemotePhotoClient.reachable()`）+ 3s 健康探针，最坏 5s 出结论，180s 只留给「家里在、正在画」。`tests/test_photo_remote.py::RealTransportTests` 用真 socket 钉住了这条（死端口上整轮 `generate()` 必须 < 5s 回 `home_offline`）。
* **错误码优先取 body**：家里 JSON 里的 `error` 比 HTTP 码精确，所以 `409 {"error":"vram_busy"}` 会直接落成 `vram_busy`，用户看到「显卡正忙着呢」；只有拿不到 JSON 才退回 `remote_error:<status>`。与 `sing` 侧同一套约定。
* 失败一律**只回一句** `friendly_photo_error(code)` 的文字，不让用户等满冷启动；本机 ComfyUI 冷启动 30–120s 是家里那边的事，云端感知到的要么是图，要么是一句话。
* 探针：`fatfish/remote.py` 里 `photo` 是**可选能力位**（`HealthProbe.optional`）——`photo_online()` 单独报，`home_online()` 不看它，拍照服务挂了不能把语音判成离线。
* **不给云端开 9882 控制面**：那是本机「起停 ComfyUI / TTS」的本地控制面，跨公网暴露等于把「随便起进程」的开关交出去。云端只需要一个「帮我把这张画出来」的有界请求，`/photo` 一条就够；显存排队本就该发生在有显卡的那台机器上。

### 魔搭云端出图（`mode = "modelscope"`，2026-11-12；加速 2026-11-17）

不占本机显存、不启 ComfyUI，直接调魔搭 API-Inference 的异步生图接口；
`images/generations` 的 `model` 直接填 **LoRA 的 ModelScope model id**，平台会
自动加载它声明的底模再套 LoRA。当前指向本机训练的
`sunny1155665/whalegirl-moss-v2`（底模 `openmoss/MOSS-Image-SFT`，触发词
`whalegirl`，最新检查点 `c2-st2000`）。

加速：底模原生 **50 步 / CFG 3.0**；通过 API 的 `loras` 参数再叠一层
`DiffSynth-Studio/MOSS-Image-SFT-TurboLoRA`（OPD 蒸馏，8 步 / CFG 1.0），
角色 LoRA 仍按 `modelscope_model` 全强度加载，不替换角色权重。
8 步**不带**加速 LoRA 实测会糊成水彩噪点，所以 `modelscope_steps` /
`modelscope_guidance` 只在 `modelscope_accel_lora` 非空时才有意义。

| 项 | 事实 |
|---|---|
| 代码 | `fatfish/photo/modelscope.py`（stdlib urllib 客户端） + `service.py` 的 `_generate_modelscope` / `start_modelscope_job` |
| 接口 | `POST /v1/images/generations`（`X-ModelScope-Async-Mode: true`）→ 轮询 `GET /v1/tasks/{task_id}`（`X-ModelScope-Task-Type: image_generation`）→ 下载 `output_images[0]` |
| 配置 | `config/low.toml` 的 `[photo] modelscope_*`；profile `moss-photo` 只切拍照，profile `modelscope` 同时切聊天与拍照 |
| 加速 | `modelscope_accel_lora = "DiffSynth-Studio/MOSS-Image-SFT-TurboLoRA"`、`modelscope_steps = 8`、`modelscope_guidance = 1.0`；`modelscope_extra` 里同名键优先，便于临时回退 |
| 实测 | 2026-11-17：50 步基线 15.1s；叠 turbo 后 8 步 11.7s，角色/服装/画质肉眼无明显退化；魔搭算力池排队波动大（同一配置 12–237s 都见过，还有一次轮询 300s 超时），耗时以任务轮询为准 |
| 测试 | `tests/test_photo_modelscope.py`（13 条，mock transport，不联网） |
| 额度 | 魔搭 API-Inference 免费额度约 2000 次/天，单模型另有日限额；超限回 429，不扣费 |

要点：
* 失败一律映射成一句 `friendly_photo_error`：key 不对 / 额度用完 / 模型不可用 /
  超时 / 图坏了，各有独立文案；不会把 HTTP 错误抛给用户。
* 云端返回的图片保留真实后缀（png/jpg/webp/gif），仍写 `data/photos/<job_id>.*`
  和 `photo_jobs`，QQ / Web 发送链路不用改。
* 每次出图前把 LoRA 触发词 `whalegirl` 前置到正向提示词（已存在则不重复）；
  正向提示词只进 `photo_jobs`，不进对话/记忆。
* 本 LoRA 是「鲸鱼娘」二创，CC BY-NC-SA 4.0：署名「溟月 / 上善无形」与
  「鲸鱼娘 / ZipZipPipe」，非商业使用；商用需另行取得原作者许可。
* **异步补发**（`Runtime.tick` → `qq_send_image`）要同时满足两条适配器：
  - 官方 QQBot：异步图必须在**触发消息的被动回复窗口**内发，否则会被当成
    proactive 富媒体，配额/类型校验一挂就是静默失败。`photo_jobs.reply_to`
    （schema v17）保存入站 `msg_id`，出图后随 outbox 一路带到 `send_media`，
    按 `msg_id + msg_seq` 发被动回复。
  - NapCat（`platforms/qq.py`）：必须注册 `qq_send_image` / `qq_send_voice`
    handler，归一成 `qq_send` + media 段；无 QQ handler 的 web/CLI tick 只
    `defer`，不再烧掉重试次数。
  2026-11-17 之前两条都缺：官方侧图被当主动消息拒发，NapCat 侧直接
  `no handler` → `dead`；任务成功、用户收不到图、也没有报错。

### 主动发图 / 表情包（2026-11-18）

三条触发路都可用：活动轮（`activity-*` 沙盒）、self-continue 续聊轮、普通聊天轮。
用户明确说「拍照 / 发个表情包」时走原确定性路径，不受闸门限制；模型自己决定发的
走 `[proactive_media]` 闸门。

| 项 | 事实 |
|---|---|
| 配置 | `photo_enabled` / `sticker_enabled`（默认 true）、每日上限 `photo_max_per_day=10`、`sticker_max_per_day=20`、最小间隔 30/10 分钟、安静时段 23:30–08:00 |
| 闸门 | `_proactive_media_allowed()` 读 `decision_log` 的 `proactive_photo` / `proactive_sticker` 计数；睡觉/安静时段/超限/太近直接拒绝 |
| 重定向 | 活动轮的 job 原本落在 `activity-*`，QQ poller 会跳过；`_media_target_session()` 在提交前改到 owner 的 `qq:private:*` 会话 |
| 投递 | 官方 QQBot 支持主动富媒体（`msg_type=7` 不需要 `msg_id`）；此前的畸形点是「无 `msg_id` 却带 `msg_seq`」，已修 |
| 内容 | 照片 prompt 由活动/生活状态生成；表情包走 `sticker.decide`；失败静默，不影响文字 |

### 状态

* 2026-11-18：自发照片/表情包三路开通；活动轮图片重定向到 owner、带预算/间隔/安静时段；
  self-continue 与普通聊天轮同样受闸门约束。
* 2026-11-17：拍照接魔搭加速 LoRA（8 步 / CFG 1.0），50 步基线 15.1s → 8 步 11.7s；
  同时修复异步图片补发：官方 QQBot 用 `photo_jobs.reply_to` 发被动富媒体，
  NapCat 补齐 `qq_send_image` handler。
* 2026-11-17：`wants_photo` 扩到「发几张图 / 多发点照片 / 再发几张」；引用或评价
  已有图片（「你发的图」「这张照片真好看」）不误触发。
* 2026-10-30：两端代码 + mock 测试（`tests/test_photo_remote.py`，38 条）完成，含真 socket 的死端口用例。
* **未做真机端到端**（云 → tailscale serve → 家里真画图）：ComfyUI 与显存仍由主控串行阶段验证，本轮没有动 GPU、没有起 ComfyUI、没有连云端。
* 未做：远端重试 / 断点续传、多卡路由、把 `photo_server` 注册成 Windows 服务、把生成的图顺手同步回记忆库。

## 词库 / 黑话

| 项 | 事实 |
|---|---|
| 代码 | `lexicon/service.py`（CRUD/审核/挑选）、`miner.py`（挖掘） |
| 数据 | 138 条（78 approved / 60 candidate），累计引用 312 次 |
| 状态 | ✅ |
| 问题 | `lexicon/__main__.py` 与主 CLI 重复；审核依赖人工 |

## 相关表

`sticker_jobs`（产线生成）、`photo_jobs`（本机/魔搭/远程）、`lexicon_entries`；旧 `stickers` / `sticker_usage` 仅为兼容保留，运行时不读。

## 历史资料

旧检索时代的 `sticker-index.md`、`sticker-caption-guide.md`、`photo.md`、`lexicon.md` 已归档到项目归档区，不再随仓分发。
