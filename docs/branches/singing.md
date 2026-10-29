---
title: 唱歌
type: branch
status: in-progress
tags: [singing, soulx, melody, prosody, lyrics, svs, gpu, remote]
updated: 2026-10-30
---

# 唱歌

> 上级：[系统架构](../overview/architecture.md) · 相关：[语音](voice.md) · [记忆](memory.md) · [运维](ops.md) · 决策：[ADR-0002](../adr/0002-singing-soulx.md)

## 结论

离线链路已跑通，v17（默认 `soft`）已定版到 `data/soulx/song_final/`（16.4s，boss 验收通过，采用默认版本）；**产品接入已完成**：`sing` 动作 + `sing_jobs`（schema v10）+ QQ `record` + Web 播放，显存与 TTS 串行，见 [ADR-0010](../adr/0010-sing-action.md)。

v5 相对 v4 修掉了三件可测量的事：末句收束（音域 4→9 半音、句尾下沉 2→7、落主音）、句间衔接（`[-3,0,-7]`→`[-3,-2,+2]`，不再有同音起与七度断崖）、句内音域（最小 4→7 半音）。词义也从“句级姿态”下探到字级时值（暖/呼唤/等待/具体名词）。mimo 音频评审在“贴合字调”上从 3 升到 4，其余三维持平（3/2/3）。

## 产品接入（2026-10-26）

| 层 | 位置 | 行为 |
|---|---|---|
| 动作 | `security/policy.py` `ACTION_SPECS["sing"]` | 仅私聊、每轮 1 次、群聊拒绝 |
| 执行 | `runtime.py` `execute_actions` → `fatfish/sing/service.py` | 同步渲染；失败追加一句 `friendly_sing_error`，不吞文字回复 |
| 台账 | `sing_jobs`（schema v10） | 一行一次点歌：情绪、四句歌词、音频路径、耗时、是否卸过 TTS |
| 显存 | `data/run/sing.lock` + `data/run/vram.lock` + TTS `/unload`→`/load` | 与拍照共用租约；`finally` 必还 TTS |
| 交付 | QQ `_queue_media_result` → `record`（index=2）；Web `/api/audio` | 与 TTS 语音同轮可并存 |
| 歌词 | 动作 `lyrics` 可选，四句、每句 4–14 字 | 不写/写坏都唱定版，坏词记 `lyrics_error` |
| 记忆 | `runtime.execute_actions` → `memory_items(source_type='sing')` | 成功后写一条「给…唱了《title》（emotion）」；问上次唱了什么用 `[最近唱过的歌]` 读最近 5 条 |

配置在 `config/low.toml [sing]`：`enabled`、`python`（`.venv-soulx`）、`script`（`soulx/song_v2.py`）、`default_src`、`output_dir`、`emotion`、`timeout_seconds`、`min_free_vram_mb` 等。应用本体仍零第三方依赖，SoulX 只作为 subprocess 运行。

**2026-10-26 记忆接线**：sing 成功后写一条 `memory_items(source_type='sing')`，gist 形如「给主人哥哥唱了《等你回家》（soft）」；自定义歌词会把四句一并写入。`_recent_sing_evidence` 在问「上次/最近唱了什么歌」时读最近 5 条，提示词用 `[最近唱过的歌]`。`sing_jobs` 仍是音频/参数台账，不承担记忆检索。**2026-10-26 端到端实测**（整合分支）：真实 SoulX 渲染 49.9s（v17，`soft`）→ `sing_jobs` done → 记忆「给主人哥哥唱了《等你回家》（`soft`）」→ 真实 LLM 回答「《等你回家》，soft 那一版」。同一配置两次渲染波形不同，这条只是本次 take。

## 云端唱歌（T-0067）

云端只有 2C2G、没有显卡（[ADR-0012](../adr/0012-online-tier.md)），但「点歌」不能因为搬家就没了。
所以唱歌拆成两半：**云上编排、家里渲染**。协议只有一条 POST，显存交接仍然 100% 在家里那台机器上。

| 位置 | 做什么 |
|---|---|
| 云 `fatfish/sing/service.py` `sing.mode="remote"` | 组 `{emotion, lyrics}` → POST 家里 `/sing` → 收 wav 存自己的 `data/songs/<job>/full.wav` → 写自己的 `sing_jobs` 行 |
| 家 `scripts/sing_server.py` | 走同一套 `SingService`：抢 `data/run/vram.lock` → `POST 9880/unload` → `song_v2.py` → `finally` `POST 9880/load` → 回 `audio/wav` |

云上**从不**碰自己的 TTS：`/unload`、`/load` 全在家里做，`finally` 也一样（`service.py` 里 remote 模式显式跳过本地 reload）。
本机 profile 不写 `sing.mode`，默认 `local`，行为与 T-0003 完全一致。

### 协议（`scripts/sing_server.py`）

| 请求 | 返回 |
|---|---|
| `GET /health`（**不要 token**） | `{"ok": true, "tts_loaded": bool\|null, "vram_locked": bool, "busy": bool}` |
| `POST /sing`，body `{"emotion": "soft", "lyrics": [...]?}`，头 `X-Token` | `200 audio/wav` + 头 `X-Job-Id` / `X-Tts-Unloaded` / `X-Tts-Reloaded` / `X-Elapsed-Ms` |
| 同上，token 不对 | `401 {"error": "unauthorized"}` |
| 同上，正在唱上一首 | `409 {"error": "busy"}` |
| 同上，家里渲染超时 | `504 {"error": "render_timeout"}` |
| 同上，本机没启用 / 资产缺失 | `503` |
| 同上，其它渲染失败 | `500`，`error` 是 `SingService` 的错误码，`message` 是给用户的那一句 |

`GET /health` 不鉴权是故意的：它只说「进程活着 / 显卡忙不忙」，不含歌词与路径，tailnet 内做探活要简单。
`vram_locked` 靠一次非阻塞 `acquire` 探测（OS 字节锁没有「只读查询」），但**锁文件不存在时直接返回 false、不会为了回答一次轮询把它创建出来**；
自己渲染时由 `busy` 短路，不探测。剩下的代价是 acquire/release 之间那几微秒——恰好落在窗口里的拍照会看到一次假的 `vram_busy`，用户重点一次即可。
（`photo_server` 的 `/health` 干脆不探，只报自己的 `busy`。）

**别高频轮询 `/health`**：`tts_loaded` 是一次对家里 9880 的 `GET /health`，超时 **2s**——TTS 没起来时这个端点就要慢两秒才答。
云端 `HealthProbe` 的 TTL 是 30s、`remote.timeout_seconds` 默认 3s，最坏情况每 30 秒在后台路径上卡 2s；把 TTL 调小或手工频繁 `fatfish remote` 会先在这里出问题。
并发保护有两层：`sing_server` 自己的 `busy` 闸（第二个请求直接 409，`ThreadingHTTPServer` 保证 `/health` 在唱的时候也能答），
以及家里原有的 `data/run/sing.lock` + `vram.lock`（Web 与 QQ 两个进程共用）。
每个渲染请求在**请求线程里**新建一条 SQLite 连接并用完就关（`sqlite3` 有线程亲和性，共享连接会让写入直接 `ProgrammingError`）。

### 暴露方式：tailscale serve（不要开公网）

```bash
# 家里（token 放环境变量，别写进命令行历史）
export SING_SERVER_TOKEN='...'
python scripts/sing_server.py --host 127.0.0.1 --port 9945 --timeout 420
# 另一个终端：把 tailnet 的 9445 反代到本机 9945（自动给 TLS 证书）
tailscale serve --bg --https=9445 http://127.0.0.1:9945
tailscale serve status
```

云上 `/opt/fatfish/config/cloud.local.toml`：

```toml
[sing]
mode = "remote"
remote_url = "https://fatfish-home.<tailnet>.ts.net:9445"
remote_token_env = "SING_SERVER_TOKEN"
```

要点：
* 只绑 `127.0.0.1`。`--host 0.0.0.0` + 无 token 会被拒绝启动（`--allow-anonymous` 也只允许 loopback）。
* 没有 token 时脚本直接 exit 2，不会静默裸奔。
* 仓库里的 `config/cloud.toml` 只放示例地址；真实 tailnet 名字写进 `cloud.local.toml`，别提交。
  同样的三行注释版也在 `deploy/linux/cloud.local.example.toml`（`bootstrap.sh` 复制它）。
* `remote_url` 用 https（`tailscale serve` 的证书），`http://100.x:9945` 只在临时排障时用。

### 超时与降级

* 家里渲染上限 `--timeout`，默认 **420s**；云上 `sing.remote_timeout_seconds`（默认 420）**再 +30s** 才放弃，
  这样「家里自己超时」的 `504 {"error": "render_timeout"}` 会先到，用户看到的是「这首歌录到一半超时了」，而不是云上一个裸 socket 超时。
* **家里关机/断网**：POST 之前先做 2s 的 TCP 探测（`RemoteSingerClient.reachable()`）。探测失败立刻返回 `home_offline`，
  用户只多等 2 秒就收到一句文字，不会等满 450s —— 和 TTS 的 `home_offline`（`fatfish/tts/client.py`）是同一套思路。
* 失败一律**只回文字**：`runtime.execute_actions` 把 `friendly_sing_error(code)` 追加到回复里，同时写一条
  `metrics(key='sing_error', tags={mode, error, friendly})`，云端 `data/logs` 里翻不到的错在库里能查。
* 错误码 → 文案：`home_offline`「家里那台唱歌的机器这会儿没连上…」/ `busy`「正在唱上一首」/ `remote_timeout`「唱太久没回音」
  / `remote_bad_audio`「传回来的歌声是坏的」/ `remote_not_configured`「云端还没配好地址」。
* `sing_jobs` 两边各写一份：家里那行 `session_id='sing-server'`（一眼看出这次来自云端），云上那行是真实的 QQ/Web session，
  记忆与「上次唱了什么」仍然读云上这份。
* 歌词只进 `sing_jobs.lyrics_json`；`sing_server` 的日志只有 job_id / 耗时 / 字节数 / 错误码。

### 状态

* 2026-10-30：代码 + mock 测试（`tests/test_sing_remote.py`，33 条）+ 桩服务 curl 证据完成。
* **未做真机端到端**（云 → tailscale serve → 家里真渲染）：真实渲染仍由主控串行阶段执行，本轮没有动 GPU、没有 POST 9880。
* 未做：远端重试/断点续传（超时就是超时，用户重点一次）、家里多卡路由、把 `sing_server` 注册成 Windows 服务。

## 链路

| 步 | 做什么 | 入口 |
|---|---|---|
| 1 | 写词（模型给 4 句，非法回落定版） | `fatfish/sing/service.py` |
| 2 | 语义姿态（ask/boast/invite/soft/cheer/plain） | `prosody.py:analyze` |
| 3 | 逐句 Spec（动机/轮转/终止式/节奏/字级时值） | `song_v2.py:phrase_spec` |
| 4 | 生成音符（13 动机 + 9 节奏 + 声调约束） | `melody_v2.py:generate` |
| 5 | 后置守卫（音域/句尾/跳进/三连/字调）+ 句间重调 | `melody_v2.py:_post_guards` / `retune_join` |
| 6 | 转 SVS 元数据 | `sing_batch.py:notes_to_target` |
| 7 | SoulX-Singer 合成（score 模式，fp16） | `sing_batch.py:Singer` |
| 8 | +2 半音（重采样 + 时长还原） | `transpose_post.py:transpose` |
| 9 | 拨弦伴奏 + 混音 | `accomp.py:render` + `song_v2.main` |
| 10 | 符号指标 / 结构化音频评审 | `melody_eval.py` / `melody_review.py` |

标准命令：

```bash
.venv-soulx\Scripts\python.exe soulx\song_v2.py \
  --semitones 2 --rate-scale 1.0 --out data/soulx/song_final
```

worktree 里没有 `vendor/` 时，用 `SOULX_ROOT` 指向主仓模型；`pypinyin` 靠
`PYTHONPATH` 指向主仓 `vendor/singdeps`：

```bash
$env:SOULX_ROOT='<PROJECT_ROOT>\vendor\SoulX-Singer'
$env:PYTHONPATH='<PROJECT_ROOT>\vendor\singdeps'
```

## 当前候选（2026-10-25）

| 目录 | 半音 | 时长 | 状态 |
|---|---|---|---|
| `data/soulx/melody_v6` | +3 | 12.8s | **本轮新，等试听**（弧线生成，`melody_ab.html` 可 A/B） |
| `data/soulx/melody_v5` | +3 | 11.8s | 上一轮，mimo 判“像念经”，保留对照 |
| `data/soulx/song_slow` | +2 | 11.8s | 旧推荐，等裁决 |
| `data/soulx/cand_b2_slow` | +2 | 13.25s | 更慢，等裁决 |
| `data/soulx/cand_c3_mid` | +3 | 11.8s | 更元气，等裁决 |
| `data/soulx/cand_d1_mid` | +1 | 11.8s | 更低沉，等裁决 |

## 旋律规则（代码里真实存在的）

- 五声音阶 `0 2 4 7 9`，主音 A3（57），音域 55–69。
- 13 个童谣动机：star/tiger/arc/rock/climb/swing/call/step/descend/hover/run/echo/swell。
- 9 种节奏：even/dotted/swing/shortlong/pickup/bounce/longshort/lilt/nursery；末音固定 2 拍。
- 声调约束：一声 −1~+2、二声 +2~+7、三声 −4~+2、轻声 −2~+1；同音保持永远允许。
- **两条生成路径**（`Spec.style`）：
  - `pattern`（旧）：把童谣动机铺满整句再修倒字。v5 走这条，四个九字句＝同一个动机跑四遍。
  - `arc`（v6 起，`song_v2` 默认）：一句一条弧线，DP 求最短路径。状态是 `(音高, 上一个音程方向)`，
    句中级进、跳进只留句缘、反向级进回补（counter-leap）加分；有调音节**禁止同音重复**，
    第 2 个音必须动；目标曲线按字调方向夹紧（四声不许比前一个音高、二声不许低）。
    终止式由 `_cadence_pairs` 按末字声调选：二声末字只能上行/保持，四声只能下行，
    末句（`cadence=0`）强制落主音；请求的终止级（4/2/0）优先，但换成能合法进入的级。
- 字级时值：`base × 词类系数 × (1 + 0.15·w)`，虚词 0.62 / 实词 1.0 / 叠字 0.78 / 倒数第二字 1.35，整体 clamp [0.5, 1.8]。
- `rotate` 只用 0/2；4/6 会被声调修复压平。
- **后置守卫**（v5 起，按顺序）：`_ensure_range`（每句 ≥5 半音、末句 ≥6，只接受成为新峰/新谷的候选）→ `_repair_leaps`（超过 `max_leap`（默认 7）的音程，把后一个音移到最近的合法五声音阶音，仍过字调）→ `_repair_tail`（末句句尾三音下沉 ≥3，末音不动、改前两音）→ 3×（`_break_triples` → `_repair_tones` → `_repair_tail`）。所有守卫都不得制造倒字，宁可窄也不破字调。
  - arc 路径调用守卫时带 `lock_tail=True`：终止音是承诺，`_break_triples` 不许动最后一个音。
  - `_repair_tones` 末音倒字时先改倒数第二音；`_repair_leaps` 跳过最后一个音程。
  - arc 路径用 `relax` 兜底（0 严格 → 1 允许终止前同音 → 2 允许任意同音）：二声末字落主音时只能 `0 0` 收，宁可重复也不丢终止式。
- **句间重调** `retune_join`：同音起（0）或 ≥7 半音会被改前两音，改完仍要过字调检查；不会改成与第二音同音（不制造“敲门”起句）。
- **呼唤起句** `open_with_vocative`：仅当句子以称呼语（主人/哥哥/宝宝…，代词不算）开头时，首音改成与第二音同音或比它低一个大二度。
- **动机回忆**：第 1 句的前 3 个音程作为 `head` 传给后面三句，只作软加分（`−0.9`），字调优先。
- **语义词表** `melody_lexicon.py`：手写 warm / vocative / wait / concrete，权重 clamp；只影响时值与动机选择，不参与音高生成。动机覆盖是保守的：默认沿用 PLAN，仅“具体名词 ≥2 且默认动机过窄”或“等待类动词 + 悬停动机”时换，末句强制宽动机。

## 代码边界（容易误解）

- 指定 `pattern` 时走模板路径：`contour/span/weights` **不参与选音**（docstring 已写明）；`max_leap` 仍由后置 `_repair_leaps` 生效。
- `style="arc"` 时 `pattern`/`rotate`/`end_mode` 都不参与选音，只在 arc 求解失败（返回 `None`）时作为兜底；`song_v2` 现在默认走 arc。
- `end_mode` 的 fifth/third 只在模板路径生效。
- `prosody` 的 `register` 未生效（`song_v2` 把 TONIC 写死 57）。
- SVS 非确定：同一配置两次输出字节不同，不能靠音频哈希做回归。
- 后置守卫可能改变动机本身的音：简谱里的数字是**守卫之后**的结果，不是模板原样。
- 老 `report.json`（v4 及以前）没有 `tones`/`join_in`，指标里字调合规显示 `—`、句间由 degrees 回推，不要当成 0。

## 已知问题

| # | 问题 | 状态 |
|---|---|---|
| 1 | 第 4 句音域只有 4 个半音、收句弱 | **v5 已处理**：`descend` 动机 + 守卫，音域 9、句尾下沉 7、落主音 |
| 2 | 旋律与词义关联停在句级姿态 | **v5 部分处理**：字级时值 + 保守动机覆盖；仍未做句调/重音建模 |
| 3 | 字级时值只有固定系数 | **v5 部分处理**：`base × 词类 × (1+0.15w)`，但词表是手写小样本 |
| 4 | 句中跳进仍有 7 半音（五度），mimo 认为应压到 ≤4–5 | **v6 已处理**：DP 对句中 ≥5 半音加罚 1.6，实测最大跳 3–5，五度只出现在句缘 |
| 5 | 非末句句尾停在属音/三音（p0 落 7、p1 落 4、p2 落 7），句间缺“落—起”关系 | **v6 已处理**：非末句落 4/2（半终止），末句落 0；句间 +3 / −2 / +5 |
| 6 | 同音重复 25–38%（`1 1 5 5` 来自小星星动机本身） | **v6 已解决**：有调音节禁止同音，实测四句 0% |
| 7 | 四句节奏密度接近，mimo 评“像念歌/数豆子” | **v6 部分处理**：不再平铺同一动机，每句独立弧线 + 不同起音/落点；节奏型仍由句姿态决定 |

## 评估与审核（P3，2026-10-25）

- `melody_eval.py`：符号指标，离线、无依赖。逐句给音域/级进/跳进/最大跳进/同音重复/三连/字调合规/句尾下沉/句间；汇总给 `min_phrase_range`、`final_tail_drop`、`joins_ok`。没有 `tones` 的老报告字调返回 `None`，不伪造分数。
- `melody_review.py`：结构化音频评审，**五维** 1–5 分（pleasant / natural / fits_words / memorable / singable）+ `problem`/`feel`/`suggest`/`systematic`；3 票取中位并报极差。`pleasant`（好听）与 `natural`（自然，不机械）是 v6 起加的——原四维里没有「愿不愿意再听」这一项，评不出用户说的「不好听」。
- **模型限制**：`mimo-v2.6-flash` 在该网关**不支持音频输入**（400 `unsupported_content_type`：“this model does not read audio”）。实测 `mimo-v2.5` 与 `mimo-v2.6-pro` 可读，评审默认用 **`mimo-v2.6-pro`**，可用 `MELODY_REVIEW_MODEL` 覆盖。
- `build_melody_ab.py`：单文件 A/B 页，音频 base64 内嵌，离线可开。

v4 vs v5 评审结果（3 票中位）：

| 维度 | v4 | v5 |
|---|---|---|
| natural | 3 | 3 |
| fits_words | 3 | **4** |
| memorable | 2 | 2 |
| singable | 3 | 3 |

记忆点仍是 2，是下一轮的主要目标。原文与取舍见 [旋律-歌词适配建议](singing-advice.md)。

## v6：弧线生成（2026-10-25）

v5 用户听感未通过（不够自然）；mimo 五维审核给 v5 pleasant 3 / natural 2 / memorable 2，`problem` 统一是「同音重复太多，像念经」。根因不在守卫，在**生成方式**：7 音童谣动机铺满 9 个字，四个句子同一个动机跑四遍，有调音节只能反复落在同一个音上。

v6 换路径，不动旧的 pattern 路径（原 37 条测试全部保留）：

- `Spec.style="arc"`：一句一条弧线，DP 求最短路径。状态 `(音高, 到达该音的真实音程)`；句中级进 0 罚、句中 ≥5 半音跳进 +1.6、反向级进回补 −0.5、A-B-A-B 等幅反向摆动 +1.1。
- **有调音节禁止同音重复**（轻声可以），第 2 个音必须动，终止前一个音必须动。
- **目标曲线按字调方向夹紧**：四声不许比前一个音高、二声不许低。少了这一步，曲线要求上行而字调不许上行，DP 唯一合法动作就是重复前一个音——这就是「念经」的直接来源。
- **终止式按末字声调选**（`_cadence_pairs`）：二声末字只能上行/保持、四声只能下行；非末句落 4/2，落主音额外 +0.8（免得中段就收束），末句（`cadence=0`）强制落主音。
- **每句一个呼吸点**：句内最高音时值 ×1.6（上限 2 拍，末音仍最长）。DP 状态从「方向 ±1」改成「真实音程」后，counter-leap 与 A-B-A-B 摆动罚分才真正生效（之前是死代码）。
- **每句的弧线形状必须不同**：`ARC_PLAN` 里 p0/p2 原本同为 `amp=7, cadence=4`，目标曲线就是同一条，DP 交出的是同一句移高两度（`0 2 4 7 9 7 9 7 4` vs `2 4 7 9 7 9 7 4 2`），评审判定为整首同一句循环。p2 改成 `peak_at=0.30`（早峰、下行、落 re），形状才真的不一样。
- 句间停顿 0.30/0.30/0.45 → 0.45/0.40/0.55 秒，像换气而不是断句。

实测（`data/soulx/melody_v6/report.json`，`melody_eval`）：

| 句 | 简谱（相对主音） | 音域 | 同音 | 最大跳 | 时值比 | 落点 |
|---|---|---|---|---|---|---|
| 1 本鲸鱼娘今天好想你 | `0 2 4 7 9 7 9 7 4` | 9 | 0% | 3 | 2.0× | 4（半终止） |
| 2 我数着海浪等你回来 | `7 2 4 7 4 2 0 2 4` | 7 | 0% | 5 | 3.0× | 4（半终止） |
| 3 热汤和拥抱都准备好 | `2 4 9 7 4 2 4 0 2` | 9 | 0% | 5 | 2.6× | 2（半终止，早峰下行） |
| 4 主人哥哥今天辛苦了 | `7 9 7 9 7 9 7 2 0` | 9 | 0% | 5 | 2.6× | 0（主音） |

句间 `[+3, −2, +5]`；字调合规 100%；三连 0；最小句音域 7；末句句尾下沉 7；总时长 12.8s。时值比 = 最长音 / 最短音，v5 是 2.0–3.2× 但分布均匀，v6 的长音集中在每句最高音（呼吸点）和末音上。

第 2 句末字「来」是二声，落主音会倒字，所以终止式落在 4、由 2 上行进入（`tail_drop` 显示 −4 是上升，不是缺陷）。

### v6 五维审核（mimo-v2.6-pro，3 票中位）

| 维度 | v4 | v5 | v6 | Δ v5→v6 |
|---|---|---|---|---|
| pleasant（好听） | 2 | 3 (±1) | 3 (±1) | = |
| natural（自然） | 2 | 2 | 2 | = |
| fits_words（贴合字调） | 2 (±1) | 4 (±1) | 4 (±1) | = |
| memorable（记忆点） | 2 | 2 (±1) | **3 (±1)** | **+1** |
| singable（好唱） | 3 (±1) | 4 (±1) | 4 | = |

v6 的 `problem`：「7、9 两音来回摆，四句都像念经」。也就是说同音重复、句间落点、四句同形（p0/p2 曾只差一个字的平移）都解决了，**剩下唯一的硬伤是两音摆动**。逐条建议与取舍见 [旋律-歌词适配建议](singing-advice.md)。

注意评分的噪声：同一版 v6 前后评过两次，`natural` 3→2、`memorable` 2→3，而 `problem` 两次都指向两音摆动。1 分的差不要当成改进或退步。

## v7：偏音经过音 + 禁止两音摆动（2026-10-25）

v6 的 `problem` 只剩一条：「7、9 两音来回摆，四句都像念经」。根因不是守卫，是**音阶的空隙**：五声里 7（sol）到 4（mi）隔 3 个半音，而一声只允许 −2~+3，第 4 句「哥哥今天辛」5 个连续一声从 7 往下没有任何合法落点，DP 只能 7↔9 摆。v7 走 mimo 的第一条候选（偏音），节奏链路不动：

- **五声外经过音**：`_PASSING_OFFSET = 5`（fa）进 DP 候选池，`7 → 5 → 4` 成为合法下行。它是调料不是主音：必须级进进出（`|Δ| > 2` 直接剪枝）、不许落在终止式、每个音罚 `_PASSING_COST = 3.4`——只有换来的东西比罚分多时 DP 才会买。
- **交替 run 长度进状态**：DP 状态从 `(音高, 音程)` 扩到 `(音高, 音程, 交替 run 长度)`，等幅反向罚分改成 `1.1 × run`（1.1 / 2.2 / 3.3）。`7 9 7` 是装饰，`7 9 7 9 7` 是念经，代价必须不同。
- **大跳只落实词**：≥4 半音的跳进落在虚字（的/了/着/和/都…）上直接剪枝。mimo 两轮都提「和」承载全句最大跳进却落在虚字上，这次是硬约束。
- **终止音不许同音**：轻声末字（了）原本可以重复前一音，收尾会变成 `0 0`；现在末音重复也剪掉（只有 relax 兜底才允许）。
- **开口不再同音**：`open_with_vocative` 只允许从下方级进进入（−2/−4），去掉 `7 7` 起句。

实测（`data/soulx/melody_v7/report.json`，`melody_eval`）：

| 句 | 简谱（相对主音） | 音域 | 同音 | 最大跳 | 时值比 | 落点 | 偏音位置 |
|---|---|---|---|---|---|---|---|
| 1 本鲸鱼娘今天好想你 | `0 2 4 7 9 7 5 4 2` | 9 | 0% | 3 | 2.0× | 2（半终止） | 第 7 字 `5`（sol→fa→mi） |
| 2 我数着海浪等你回来 | `4 5 7 9 7 4 0 2 4` | 9 | 0% | 4 | 3.0× | 4（半终止） | 第 2 字 `5`（mi→fa→sol） |
| 3 热汤和拥抱都准备好 | `2 4 7 9 4 2 4 0 2` | 9 | 0% | 5 | 3.2× | 2（半终止，早峰） | 无 |
| 4 主人哥哥今天辛苦了 | `4 5 7 9 7 5 4 2 0` | 9 | 0% | 2 | 2.6× | 0（主音） | 第 2、6 字 `5` |

句间 `[+2, −2, +2]`；字调合规 100%；三连 0；最小句音域 9；末句句尾下沉 4；总时长 12.8s。

与 v6 的差别：**7↔9 摆动消失**（四句都不再出现等幅三连交替）；第 3 句原来落在「和」上的 5 半音跳进没有了（和 +3、拥 +2），全句最大跳是「拥→抱」的纯四度，落在实词；第 4 句从 `7 9 7 9 7 9 7` 变成一次完整上行 + 一次完整下行，最大跳从 5 降到 2。

取舍说明：

- 第 2 句仍然收在 4（`tail_drop = −4` 是上行）。末字「来」是二声，二声只能上行或保持；五声池里 0 以下没有可用的邻音，`0 0` 又被禁止，所以 `0 2 4` 是唯一不倒也字、不念经的收法。它是半终止，本来就不该「说完」。
- 第 3 句 9→4 是 5 半音（纯四度）跳进，落在「抱」（实词）。纯四度是最好唱的音程之一，`melody_eval` 记 `max_leap = 5`，不是缺陷。

## v8–v11：拆拱形、封顶、句尾拖音（2026-10-25）

v7 的六维评分里 `persona` 第一次被量出来（3/5），`problem` 从「两音摆动」换成了
**「四句全是同一拱形，听感单调」**。后面四版都在打这一条，两版被否：

| 版本 | 改法 | 结果（好听/自然/贴合/记忆/好唱/角色） | 结论 |
|---|---|---|---|
| v8 | 四句峰位、峰高全拆开；第 3 句把「拥抱」顶到高八度 `12` | 3/2/4/2/3/3 | **否**：「第三句一下子蹦到楼顶…像通用儿歌，出戏」；第 4 句开口两个同音「像在敲门」 |
| v9 | 峰值锁回 9；第 2 句压成只到 5 的平句；第 4 句 `0 4 7 9` 抬起再一路落下 | 3/3/4/3/5/**4** | 采纳：角色一致 3→4、好唱 4→5、记忆点 2→3 |
| v10 | 第 3 句改早峰（`2 4 9 7 4 2 4 0 2`），想打掉「每句都从低爬到 9」 | 3/2/4/3/4/3 | **否**：`2→4→9` 五度跳，「时值均匀无呼吸，像摆音盒」 |
| v11 | 旋律同 v9，只把末字 2 拍放到 2.5 拍（末句 3 拍） | **4/4**/4/3/4/3 | 采纳：好听 3→4、自然 3→4；代价是好唱 5→4、角色一致 4→3 |

v11 的四句（`data/soulx/melody_v11/report.json`）：

| 句 | 简谱 | 峰 | 落点 | 末字时值 |
|---|---|---|---|---|
| 1 本鲸鱼娘今天好想你 | `0 2 4 7 9 7 5 4 2` | 9 @ 第 5 字 | 2 | 2.5 拍 |
| 2 我数着海浪等你回来 | `4 0 2 4 5 4 0 2 4` | 5 @ 第 5 字 | 4 | 2.5 拍 |
| 3 热汤和拥抱都准备好 | `2 4 7 9 7 5 4 0 2` | 9 @ 第 4 字 | 2 | 2.5 拍 |
| 4 主人哥哥今天辛苦了 | `0 4 7 9 7 5 4 2 0` | 9 @ 第 4 字 | 0 | 3.0 拍 |

字调 100%、同音 0%、三连 0、句间 `[+2, −2, −2]`、末句句尾下沉 4、12.8s。

这一轮学到的三件事：

- **峰高封顶**：v8 的八度跳被评审判为儿歌嗓。对这个音色（参考音 F0 55–73），
  主音上方五度（`9`）就是上限，想做出高潮只能改**形状**，不能加**音高**。
- **峰位不够，轮廓才算数**：v9/v10 的峰位已经分散，评审仍然说「同一句型」。
  改成序列化升降方向（`++++----` / `-+++--++` / `+++----+` / `+++-----`）后，
  测试能守住「四句不同轮廓」，但这只保证不重复，不保证好听。
- **动机召回在弧线路径上是死代码**：`head` 的 −0.9 加分从来没改变过 DP 的最优解
  （实测 27 种 same/none/invert 组合输出完全相同）。开场的变化只能来自
  `ARC_PLAN` 的 `start/amp/peak_at`，已写进代码注释。
- **句尾拖音是唯一能「不改音高就改气质」的旋钮**：`Spec.final_beats`（默认 2.0）
  在 `song_v2.FINAL_BEATS` 里设成 2.5/2.5/2.5/3.0，`melody_eval` 改成读报表里的
  末音时值（旧报表没有这一项时仍按 2 拍算）。

## 定版与混音（2026-10-25）

`scripts/finalize_song.py` 把「听得对的那一条」冻结成 `data/soulx/song_final/`：复制
`full.wav` / `vocal.wav` / `report.json`，把当时的全部参数写进 `SETTINGS.json`，
再由 report 反推出可复现的渲染命令，附 sha256 前 16 位。**渲染是随机的**（同一配置两次
波形相关 0.05），所以 take 本身不可复现——配置必须跟着音频走，否则"这条好的"就等于丢了。

**最终定版（2026-10-25）**：`melody_v17`（默认情绪 `soft`）→ `data/soulx/song_final/`。
16.4 s、2.4 字/秒、原生调性、换气 1.1/1.0/1.4 拍、房间感 14%、伴奏 -7 dB、颤音 18 音分、
滑音 25 音分、气口 0.08、气声 0.05、连音 0.55。boss 验收通过（听感明显改善，采用默认版本）。`--emotion genki/sad` 保留为可选，不进定版。

（中间还定过一版 `melody_v12`：「这个不错」；后来加了颤音/混音/连音，被 v17 取代。）

**混音 bug（v14 修）**：`mixed = vocal + backing` 一直直接相加，实测伴奏只在人声下面
**17 dB**——等于清唱。现在 `match_backing()` 按 RMS 把人声压到伴奏上方 `--backing-db`
（默认 -7 dB，流行歌常见 4–8 dB）。这不是风格问题，是量纲问题：17 dB 的差距任何人一听
就是「干、像念经」。数字、代码、测试都在这一版里。

**颤音（v13，备选）**：长音在进入 120 ms 后渐入 5.2 Hz / 18 音分颤音 + 全曲 ±5 音分慢漂移
（WORLD 重合成，谱包络不动）。评审没分出差别（噪声大于效果），先留作备选，不并入定版。

**连音（v15）：「吐字太果断」的根因是结构性的**。SVS 的 data processor 把**每个音**包成
`<BOW> … <EOW>`（一个字 = 一个词），所以字缝天然带一个词边界。渲染结果上能测到：35 个
字缝里 **29% 掉到 -6 dB 以下、14% 掉到 -12 dB 以下、最深 -33 dB**（等于断开）。
`legato.fill_valleys()` 只在深谷处把前一个元音的尾巴用 8 ms 淡入淡出接过去（听不出循环），
句间换气不碰：深谷 **29% → 17%**、-12 dB 以下 **14% → 6%**、最深 **-33 → -17 dB**。

**情绪（v18）**：这套 SVS **没有 emotion embedding**，所以"情绪"只能是**唱法参数的一组取值**。
`song_v2.EMOTIONS` 给了三个完整预设，`--emotion{soft,genki,sad}` 选一个，
单独传参数还能只覆盖其中一项（`--emotion genki --vibrato 20`）：

| 预设 | 速度 | 颤音 | 滑音 | 气口 | 房间 | 伴奏 | 听感 |
|---|---|---|---|---|---|---|---|
| `soft`（默认） | 0.78 | 18 音分 @5.2 | 25 音分 | 0.08 | 0.14 | -7 dB | 撒娇、贴耳 |
| `genki` | 0.88 | 11 音分 @5.6 | 15 音分 | 0.05 | 0.10 | -6 dB | 明亮、跳跃 |
| `sad` | 0.70 | 24 音分 @4.7 | 38 音分 | 0.12 | 0.19 | -9 dB | 委屈、拖沓 |

方向是常识性的：**快 + 少颤音 + 干房间 = 元气**，**慢 + 深颤音 + 湿房间 + 大滑音 = 委屈**。
它改的是"怎么唱"，不是"音色"——音色只能换参考音（下一步）。

## 旋律-歌词适配建议（mimo，2026-10-24）

已让 mimo-v2.6-pro 针对当前成品给出专业建议，原文与取舍见 [旋律-歌词适配建议](singing-advice.md)。

优先采纳（按性价比，2026-10-25 状态）：

1. **收句落点模板**：soft/comfort 类句末三字强制下行 ≥3 度，尾字 ≥1.25× 平均时值。**v5 已做**（末句 `tail_drop` ≥3，实测 7）。
2. **字级情感权重**：`dur = base × 词类系数 × (1 + 0.15·w)`，配词表，clamp 到 [0.5, 1.8]。**v5 已做**（手写表，非 200 词）。
3. **句位时值**：首字 0.9 / 中部 1.0 / 尾二 1.35 / 尾字由终止式决定。**部分已做**（尾二 1.35、尾字固定 2 拍）。
4. **重读词**：只做软偏好（局部最高音），声调硬约束优先。未做。

暂缓：动机参数化（改动大）、句向量检索（缺标注数据）。

## 唱法（B 线，2026-10-25）

旋律层见顶之后，力气转到「唱法」：同一份旋律 v11，只换渲染方式。结论先写：

**SVS 采样不是确定性的。** 同一份配置连渲两次，波形相关系数 **0.05**。同一份旋律
（一个音都没改）五个 take 的好听分是 **4 / 2 / 3 / 3**、角色一致 **3 / 3 / 3 / 4**。
这解释了两件事：为什么「改旋律」的收益总和噪声一样大，以及为什么不能拿单个 take
的分数当结论——**同一个谱子，唱的人当天嗓子状态就值 1–2 分**。

| take | 变的是什么 | 好听 | 自然 | 好唱 | 角色 | 判词 |
|---|---|---|---|---|---|---|
| v11 | 基线（32 步，重采样 +3） | 4 | 4 | 4 | 3 | 太温太稳，像哼摇篮曲 |
| v11_t2 | 同配置重渲 | 2 | 2 | 4 | 3 | 偏扁偏童声，像通用儿歌 |
| v11_t3 | 同配置重渲 | 3 | 3 | 4 | 3 | 音区压在 A3–F#4，太平太低 |
| v11n | `--shift-mode model`（模型内变调） | 3 | 3 | 4 | 3 | 音色偏干，咬字硬 |
| **v11_s64** | `--n-steps 64` | 3 | 3 | **5** | **4** | 音色/气声/句尾拖音都对得上参考音 |

两条可复现的接口：

- `--n-steps`（默认 32 → 64）：flow-matching 步数，更多的步数 = 更干净的音色。
  64 步是目前角色一致性最高的一版（4/5），代价是好听/自然各降 1（样本只有一次，
  按上面的噪声看，需要多渲几个 take 再判）。
- `--shift-mode model`：让模型自己升调，想避免重采样把共振峰一起抬上去（那正是
  「幼态/童声」的来源）。**实测失败**：模型没有真的升 3 个半音（中位 F0 327 → 320 Hz），
  音色反而更干。默认仍是 `resample`，参数留着是为了以后换模型时能再试。

由此定下的做法（写进 `AGENTS.md` 硬约束 8/9）：

1. **每次开跑前 preflight**：`python scripts/preflight.py --for svs`（显存/内存/
   服务是否常驻，退出码 0 才开跑；不够加 `--unload-tts`）。2026-10-25 出现过内存
   99% 仍直接渲染。
2. **take selection 是正式步骤**：同一配置渲 2–3 个 take，用同一套六维评审挑一个，
   像人声录音一样。不是"再调一版旋律"，而是"从这一版里挑一条最好的"。

远程试听（**tailnet 直连**，不过公网；手机在同一个 Tailscale 里就能开）：

```bash
python scripts/serve_audio.py --dir data/soulx --host 127.0.0.1 --port 8899
tailscale serve --bg --https=8443 http://127.0.0.1:8899
# https://<机器名>.<tailnet>.ts.net:8443/listen.html   ← 轻量页（32 KB）
# https://<机器名>.<tailnet>.ts.net:8443/melody_ab.html ← 自包含页（3 MB）
```

为什么不用图床：`catbox.moe` 连接被 reset、`litterbox` 上传成功但外网打开很慢/打不开、
`pixeldrain` 直接 reset、`0x0.st` 连不上。tailnet 是 WireGuard 直连（实测 ping 手机
48 ms），同一份 640 KB wav 走 HTTPS 只要 35 ms，而且不把角色声音传到公网。

两个 A/B 对比页由研发期内部脚本生成（未随发布版分发，此处保留设计说明）：

- `listen.html`（`--link-audio`）：`<audio src="melody_v11_s64/full.wav">`，页面
  只有 32 KB，手机秒开；需要有人在本机起 `serve_audio.py`。
- `melody_ab.html`（默认）：音频 base64 内嵌，单文件 3 MB，可以离线/发给别人。

`serve_audio.py` 自己实现了 `Range`（206）：手机 Safari 必须先拿 byte range 才肯播，
`http.server` 默认回 200 整文件，音频会卡住不放。

## 下一步

1. ~~人工试听定版~~：已完成，`song_final/` 为 v17（`soft`）。
2. 唱法线（B）仍可继续，按性价比排序：
   - **多 take 挑选**：同一配置多渲 2–3 条，评审挑最好的一条；按 0.05 的波形相关
     看，take 之间的差距比任何参数都大。渲染一条约 1 分钟，值得。
   - **`--n-steps`**：32 → 64 已见效（角色一致 4）；再往上（96）未测，边际可能递减。
   - **参考音切片**：换 `cand_home_up1` 的其他候选（`refs/cand_home_b`、`ref_base`）需要
     重新对齐 metadata（28 音节/5.54s 那种），是一整条对齐流程，未做。
   - **TTS 念白 + 唱句混排**：评审两轮都说「念白那句最像她」。先做一句开场白
     （主人哥哥…）再进唱，角色一致性大概率抬高，但要先定"这还算不算一首歌"。
   - **旋律层**：已见顶（v7→v11 五版在噪声内摆动），不再投入。
3. ~~接入 fatfish~~：已完成（`sing` 动作 + `sing_jobs` + QQ `record` + Web 播放；见上节与 ADR-0010）。**未做**：`/api/sing` 独立点歌端点（聊天动作已覆盖）、异步队列、多 take 自动挑选。
4. 详细听觉实验记录见归档 `handover-singing-2026-10-24.md`。

## 资产

| 路径 | 说明 |
|---|---|
| `data/soulx/prompt_v6.json` | 核心：参考音对齐 metadata（28 音节 / 5.54s / F0 55–73） |
| `data/soulx/*` | 339 文件 / 69.6MB，各版本 wav + report.json |
| `data/soulx/melody_v11/` | **本轮推荐**：v9 旋律 + 句尾拖音，13.4s，+3 半音 |
| `data/soulx/melody_v10/` | 被否：第 3 句早峰 + 五度跳 |
| `data/soulx/melody_v9/` | 角色一致性最高的一版（4/5），12.8s |
| `data/soulx/melody_v8/` | 被否：八度高潮 |
| `data/soulx/melody_v7/` | 偏音经过音，12.8s |
| `data/soulx/melody_v6/` | v6 产物（full/vocal/raw.wav + report.json + review.json），12.8s，+3 半音 |
| `data/soulx/melody_v5/` | v5 产物（full/vocal/raw.wav + report.json），11.8s，+3 半音 |
| `data/soulx/melody_ab.html` | v4…v11 单文件 A/B 页（音频内嵌） |
| `data/soulx/melody_ab_review8.json` | 最新一轮六维评分（v9 vs v11，带参考音，3 票） |
| `data/soulx/melody_ab_review6.json` | v7 vs v9（角色一致 4 的那次） |
| `data/soulx/melody_ab_review4.json` | 首轮六维评分（v4…v7） |
| `data/whale_voice_demo_v2/refs/cand_home_up1.wav` | 角色一致性的判定参考音（第二段音频块） |
| `vendor/SoulX-Singer` | 5,366MB（SVS 2.6GB + SVC 2.6GB） |
| `.venv-soulx` | 117MB（借 `.venv-cosyvoice` 的 torch） |
| `soulx/` | 14 个运行时文件（定版唱歌链路）；研发实验脚本未随发布版分发 |
