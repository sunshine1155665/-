---
title: 媒体能力（表情包 / 拍照 / 词库）
type: branch
status: active
tags: [sticker, photo, comfyui, lexicon, media, vram]
updated: 2026-10-30
---

# 媒体能力（表情包 / 拍照 / 词库）

> 上级：[系统架构](../overview/architecture.md) · 相关：[语音](voice.md) · [运维](ops.md) · [资产](assets.md)

## 表情包

| 项 | 事实 |
|---|---|
| 代码 | `sticker/service.py`（检索/决策）、`importer.py`（导入打标）、`intent.py`（意图分类）、`generator.py`（加字生成）、`lexicon.py`（查询侧同义词扩展） |
| 数据 | 索引 101 张（`data/stickers/index.json`），DB `stickers` 114 行，生成图 15 张 |
| 状态 | ✅ 可用；`sticker_usage` 有真实使用记录 |
| 闸门 | `intent_threshold`（bge 标定 0.50）+ `min_emotion_intensity=2.0` 双闸门 |
| 评测 | `scripts/sticker_recall_eval.py`（`--demo` 确定性 13 题 / `--db` 真库 20 题）；2026-10-31 hash 基线 hit@1 18/20、hit@3 20/20 |
| 问题 | `tests/test_core.py:277` 曾红：测试环境用 hash 向量，现已显式固定为 hash + 0.38 |

### 云端贴纸检索（task/cloud2，2026-10-31）

2C2G 云主机跑不动 bge（torch RSS 1GB+），`config/cloud.toml` 用 hash（256 维、零依赖）。
让相似度通道活起来要三件事，缺一退化成纯关键词+意图：

1. **库向量 hash 重算**：`python staging_reembed.py --db data/fatfish.db --backend hash --apply`
   （自带备份）。库里若是 768 维旧向量，宽度不符 → 余弦恒 0。
2. **查询侧同义词扩展**：`sticker/lexicon.py`，把「惊到」桥到「惊讶」这类库里的说法；
   只扩查询侧，调词表不重算库。相似度取 max(原句向量, 扩展向量)——别名不稀释
   本来匹配得好的查询（实测晚安查询 0.545，单用扩展向量会掉到 0.453）。
3. **surprise 规则词表**补「惊到/吓到/惊呆」：意图分占 0.30 权重，是短板时的最大杠杆。
4. **相似度拉伸**（2026-10-31 定版）：`similarity_floor=0.15`、`similarity_ceil=0.60`
   （写在云端 cloud.local.toml）。hash 非零余弦 p50 0.26 / p90 0.45 / max 0.88，
   不拉伸时中等匹配够不着 0.45；拉伸后晚安 0.58->0.72、开心 0.27->0.50、事务句 0.16 仍拒发。

评测基线（hash，真库副本）：hit@1 18/20、hit@3 20/20。提频配置（0.45 阈值 + 情绪闸 1.0 +
拉伸）在云端 smoke：晚安/开心自发发送，事务句拒发。第二阶段家里在线时改回 http 指向
9881（同模型同宽度，向量可比）。

## 拍照

| 项 | 事实 |
|---|---|
| 代码 | `photo/service.py`，`runtime.py:463` 关键词触发 |
| 外部 | ComfyUI 8188，模型目录由 `photo.models_root` 配置（anima-turbo 等） |
| 数据 | 6 次作业，3 成功 3 失败（显存冲突 ×2、启动超时 ×1） |
| 状态 | ✅ 可出图并发送；不稳定 |
| 问题 | 与 TTS 抢 8GB 显存 |

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

### 状态

* 2026-10-30：两端代码 + mock 测试（`tests/test_photo_remote.py`，38 条）完成，含真 socket 的死端口用例。
* **未做真机端到端**（云 → tailscale serve → 家里真画图）：ComfyUI 与显存仍由主控串行阶段验证，本轮没有动 GPU、没有起 ComfyUI、没有连云端。
* 未做：远端重试 / 断点续传、多卡路由、把 `photo_server` 注册成 Windows 服务、把生成的图顺手同步回记忆库。

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

### 状态

* 2026-10-30：两端代码 + mock 测试（`tests/test_photo_remote.py`，38 条）完成，含真 socket 的死端口用例。
* **未做真机端到端**（云 → tailscale serve → 家里真画图）：ComfyUI 与显存仍由主控串行阶段验证，本轮没有动 GPU、没有起 ComfyUI、没有连云端。
* 未做：远端重试 / 断点续传、多卡路由、把 `photo_server` 注册成 Windows 服务、把生成的图顺手同步回记忆库。

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

### 状态

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

`stickers`、`sticker_usage`、`photo_jobs`、`lexicon_entries`。

## 历史资料（已归档）

`sticker-index.md`、`sticker-caption-guide.md`、`photo.md`、`lexicon.md`。
