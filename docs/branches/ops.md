---
title: 运维手册
type: branch
status: active
tags: [ops, runbook, ports, vram, backup, nightly, tests, git, night-test]
updated: 2026-10-30
---

# 运维手册

> 上级：[系统架构](../overview/architecture.md) · 相关：[语音](voice.md) · [唱歌](singing.md) · [记忆](memory.md) · [平台](platforms.md)

## 一键启动

```bat
启动大肥鱼.bat
```

内部是 `scripts/start_all.py`：拉起 TTS、向量、控制面、Web、QQ；Ctrl+C 全停。

## 云端在线层（试用中，ADR-0012）

把「接入 + 编排 + 记忆」搬到一台 Linux 云主机，GPU 能力（TTS / 唱歌 / 拍照）留在家里：

- 配置：`config/cloud.toml`（`low.toml` 之上的最小覆盖层，只关 GPU 能力）
- 部署：`deploy/linux/README.md`（`bootstrap.sh` / `napcat.sh` / 两个 systemd unit）
- 决策与验收判据：`docs/adr/0012-online-tier.md`（status: proposed，结论待回填）
- 降级：家里没开机时纯文字；`sing` / `photo` 不可用；表情包无 `MODELSCOPE_API_KEY` 时不发图（不回落旧库）
- SQLite 只能有一个写者：云为主库时，家里那份只读/备份，不许两边同时写

## 云端独立性与降级矩阵（T-0069，2026-10-30）

**用户口径**：「云服务器必须能在家里电脑关机时独立完成基础聊天服务。」
结论：NapCat / QQ 收发 / Web / 记忆**必须**由云端独立承担；家里只做 GPU 增强，
关机时降级纯文字可以接受，但**不允许卡住、也不允许抛异常**。

| 配置点 | 家里关机 / 断网时的行为 | 代码证据 |
|---|---|---|
| `[tts] base_url`（`cloud.local.toml`） | 探针判离线 → `home_offline` → 这句改发文字；**已合成的缓存句子照发** | `fatfish/tts/client.py:160-165`（缓存检查在探针之前） |
| `[remote] enabled`（`cloud.toml` 默认 `true`） | 每 30s 探一次、单次 3s 上限，失败只影响该窗口 | `fatfish/remote.py::build_probe` / `HealthProbe.check`（TTL 缓存） |
| `[sing] remote_url` | 2s TCP 探活失败 → `home_offline` → 一句人话，**不会**等满 900s | `fatfish/sing/service.py::_render_remote`（`reachable()` 在 `post()` 之前） |
| `[photo] remote_url` | 同上；180s 画图预算根本不会被动用 | `tests/test_photo_remote.py::RealTransportTests`（真 socket，整轮 < 5s） |
| `[sticker] generation_*` + `MODELSCOPE_API_KEY` | 无 key → `generation_unavailable`，这句不发图；**不回落旧库检索** | `fatfish/sticker/service.py::StickerService.available` |
| `.env` 的 `SING_SERVER_TOKEN` / `PHOTO_SERVER_TOKEN` | 缺失 → 家里回 401，云端一句「token 对不上」，不阻塞 | `scripts/sing_server.py::_authorized` / `scripts/photo_server.py::_authorized` |
| `[qzone] enabled = false` | 直接关掉 | `config/cloud.toml` |
| NapCat `127.0.0.1:3001` | **纯本机**，与家里无关；它才是「关机也能聊天」的关键路径 | `deploy/linux/napcat*.service` |
| 本地服务控制面 `9882` | 云端运行路径不引用 `fatfish/services.py`，不会去起停本地进程/ComfyUI | `grep -rn "import services\|ServiceManager" fatfish/ scripts/` 无命中 |

注：表中 `sing_server.py` / `photo_server.py` / `test_photo_remote.py` 为研发侧文件，发布版未分发。

兜底：`fatfish/runtime.py:1152` 的 `except Exception` 上写着「TTS must never block the text reply」——
任何能力层异常都只变成一条 `tts_error` 指标 + 文字回复，不会中断这一轮。

### 修掉的两个硬依赖（本轮重点）

1. **`[remote] enabled` 原来是注释掉的。** 云端按示例开了语音却没开探针时
   `build_probe()` 返回 `None`，每句话都要等满 `tts.timeout_seconds`（20s）才失败——
   家里关机时表现就是「半天不回」。已改为 `config/cloud.toml` 里**默认 `enabled = true`**，
   由 `tests/test_config_profiles.py` 的 `test_cloud_overrides` / `test_gpu_features_are_off` 钉住。
2. **只配 `[tts] base_url`、忘了 `[remote] home_url` 时探针仍然空转。** 已让
   `build_probe()` 从 `base_url` 自动推导 `scheme://host:port` 再拼 `/health`；
   loopback 不推导（本机行为零变化），显式 `home_url` 优先。
   见 `fatfish/remote.py::home_url_from_tts` 与 `tests/test_remote.py::BuildProbeTests` 新增的三条。

审计方式：逐项读代码 + 跑定向测试（`test_remote` / `test_config_profiles` / `test_sing_remote` /
`test_photo_remote`），**没有**在云端做破坏性实验。

### 看门狗：掉线不再静默

`scripts/napcat_watch.py`（标准库；**绝不重启容器**）由 `napcat-watch.timer` 每 2 分钟跑一次 `--once`：

- **判定**：`127.0.0.1:3001` 能否连（2s TCP）+ `docker inspect napcat` 的 `.State.Status`；
  合成 `port_closed` / `container_exited` / `container_restarting` / `container_missing`。
  不解析 `fatfish-qq` 的日志——它刷的每行都是同一根因，解析格式只会多一个会坏的地方。
- **去抖**：连续不健康 **≥ 300s** 才算一次故障；短抖动只记一行 info。
- **只在翻转时告警**：`ok→down` 一次、`down→ok` 一次，`down` 期间不重复刷。
  有 `SMTP_*` 就发邮件（标题带机器名 + 时长）；否则写 `data/logs/napcat-watch.log`
  并落 `data/run/napcat_down`（首次故障时间 + 原因码），恢复时删除。
- 退出码 `0` 本轮健康 / `1` 本轮不健康（**含未满 grace 的抖动**，告警仍按 grace 去抖）
  / `2` 配置错误。所以掉线期间 timer 会持续 `failed`，这是有意的第二重提醒：
  `systemctl --failed` 一眼可见，不用等邮件。
- `--json` 每轮打一行结论（`event` = `ok` / `pending` / `alert` / `down` / `recovered`），
  日志里同时有 `ALERT` / `RECOVERED` 字样，`grep` 就能翻。
- 状态存 `data/run/napcat-watch.json`，timer 每次起新进程也能续上 grace 计时。

安装：

```bash
install -m 644 deploy/linux/napcat-watch.service /etc/systemd/system/
install -m 644 deploy/linux/napcat-watch.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now napcat-watch.timer
systemctl list-timers | grep napcat
```

排障：`journalctl -u napcat-watch -n 50`、`cat /opt/fatfish/data/run/napcat_down`。

### napcat-supervisor 与容器 `--restart always` 会不会打架（只读结论）

容器是 `--restart always`，supervisor 用 `docker start/stop napcat`。**现在不冲突**：

- `docker stop` 是显式停止，`--restart always` 不会把它拉回来；supervisor 的
  `stop → backoff → start` 完全掌控节奏。
- `docker start` 对已在运行的容器是 no-op，重复调用不报错。
- **唯一重叠点**：Docker daemon 重启或主机重启时，`--restart always` 会自己把容器拉起来；
  此时若 supervisor 已因「需要扫码」退出（exit 2），就没有人接管这一轮登录。
  表现正是「容器在跑、3001 不监听」——看门狗报的就是这个状态。

所以：**登录恢复、`3001` 稳定之前不要 `systemctl start napcat-supervisor`**；
恢复后启动时也不需要改容器的 `--restart always`（保留它，daemon 重启能自愈）。
将来若要消掉上面那个重叠，可把容器改成 `--restart unless-stopped`（人工 stop 过的不再被拉起）——
这是**登录恢复之后**的事，现在不动。

## schema 版本 13 溯源与结论（T-0063，2026-10-30）

**现象**：`fatfish/db.py` 的 `SCHEMA_VERSION = "12"`，本机 `data/fatfish.db` 的
`schema_meta.schema_version` 是 **13**，云端 `/opt/fatfish/data/fatfish.db` 是 12。

**13 是谁写的**

- 13 由 T-0057「每轮发送者归属」引入：`36fd537 feat(runtime): attribute each turn to
  its sender (schema v13, T-0057)`（2026-10-30 00:42），补丁
  `4316f3e fix(db): create the v13 sender index only after the column migration`（00:59）。
- 两者随 `4ed2113 merge: task/context-roster (T-0057)` 进了 main，`main:fatfish/db.py`
  现在就是 13。
- 当前分支 `task/cloud2` 从 `2cacf57`（T-0056）拉出，早于那次合并，所以常量还是 12；
  本机库是当时用 main 跑过一次迁移留下的（db mtime `00:59:14` ≈ 4316f3e 提交时间）。

**13 相对 12 只多了三样**（本机库 `sqlite_master` / `PRAGMA table_info` 核对）

| 对象 | 形态 | 建在哪 |
|---|---|---|
| `conversation_turns.sender_id` | `TEXT` | `schema.sql` + `_COLUMN_MIGRATIONS`（ALTER） |
| `conversation_turns.sender_name` | `TEXT NOT NULL DEFAULT ''` | 同上 |
| `idx_turn_session_sender` | `(session_id, sender_id, id)` | 只在 `migrate()` 里建（4316f3e：旧库上 schema.sql 跑在 ALTER 之前，索引会引用不存在的列） |

本机库这两列与索引都在 → **库已经是 v13 目标态**。

另外库里有 23 个「schema.sql 里没有」的对象：`detail_fts*` / `memory_fts*` / `rag_fts*`
15 张 FTS5 影子表、`vocab`（`fts5vocab` 视图表）、`lexicon_entries`、`owner_bindings`、
`secret_notes`、`sqlite_sequence`。它们分别由 FTS5 或功能模块运行时自建
（`owner.py:42`、`privacy.py:26`、`lexicon/service.py:92`），**与 schema 版本无关，不是 v13 产物**。

**结论：选 A（代码到 13），由 `git merge main` 带进来，不手改常量。已执行。**

- `merge: main -> task/cloud2（取 v13 发送者归属，T-0063）`（`58855d0`）把 main 的 20 个提交并入本分支；
  合并后 `fatfish/db.py` 的 `SCHEMA_VERSION = "13"`（来自 main，本分支没有手工改过这个常量），
  与本机库的 13 一致。
- 唯一冲突是文档 `docs/README.md` 的索引表，按「两边都要」合并（保留 main 的 `people` 标签、
  假数据报告行与 `staging` 标签，同时保留本分支的 `napcat-pskey`、`cdp`、ADR-0012 的 `sqlite`、ops 的 10-30 日期），
  随后 `scripts/docs_index.py --write` 重生成（30 docs）。代码与测试零冲突。
- 合并后 `python -m fatfish doctor` → `status: OK`、库版本仍 13。（doctor 本身不校验 schema 版本，
  见文末；所以「版本不符告警」本来就不存在，真正的判据是 `fatfish status` 里的 `db.schema_version`
  与常量一致。）

理由：v13 迁移本来就在 main 上，且有 `tests/test_turn_sender.py`（405 行）覆盖
（`test_fresh_database_has_sender_columns_and_index`、
`test_v12_database_migrates_to_v13_idempotently`、
`test_v12_database_opens_through_ensure_schema`）；在 cloud2 手工抄一份 13 会在
`db.py`/`schema.sql` 制造第二份定义，等真要并 main 时反而更容易冲突 —— 而 cloud2 本来
就要先并 main 才谈部署。

**⚠ 合并前的反向危险（已在合并后消除，留档）**：代码还是 12 的分支上，任何一条 CLI 命令都会走
`_bootstrap → db.ensure_schema()`，把 13 的库标记写回 12（`migrate()` 结尾无条件
`set_meta`）。在副本上实测：13 → 12，`sender_id`/`sender_name` 和索引都不丢。
不丢数据，但版本标记会撒谎。合并到 13 之后代码与库同版，这条路径不再触发降级。

**合并预演**（`git merge-tree --write-tree --name-only HEAD main`，只算不写）：唯一冲突是
`docs/README.md`；`db.py` / `schema.sql` 无冲突（cloud2 那 3 个提交没碰 schema）—— 与实跑一致。

**升级演练**（取 main 的树跑）：v12 新库 → `ensure_schema()` → 13，两列 + 索引齐全，
再跑 `migrate()` 返回 `applied=[]`（幂等）。云端 12 库升 13 走的就是这条路径：部署后首次
启动自动升级，不需要手工动库。

**验收**：`python -m fatfish doctor` 本来就不校验 schema 版本（`doctor.py` 里没有
`SCHEMA_VERSION` 引用，实跑 `status: OK`）；真正会对齐的是 `fatfish status` 里
`db.schema_version` 与常量一致。合并后跑全量测试 + `scripts/docs_index.py --check`。

## 端口

| 端口 | 服务 | 备注 |
|---|---|---|
| 8765 | Web | `start_all` 默认 `--port 0` 随机 |
| 9880 | TTS | IndexTTS-2.5 |
| 9881 | 向量 | bge-base-zh |
| 9882 | 控制面 | `services.py` |
| 8188 | ComfyUI | 外部 |
| 3001 | NapCat WS | 外部 |
| 8009 | KEV | 可选 |

## 显存纪律（8GB）

TTS、ComfyUI、SoulX **不能同时驻留**。顺序：

1. `POST http://127.0.0.1:9880/unload`
2. 跑拍照 / 唱歌
3. `POST http://127.0.0.1:9880/load`（或让服务按需加载）

控制面 `services.py` 用 `data/run/vram.lock` 租约协调；拿不到锁返回 503。

**动手前先跑 preflight**（2026-10-25 定的硬规矩：曾出现内存 99% 仍直接开跑，
出来的音频和人声都不可信，还白烧一轮渲染）：

```bash
python scripts/preflight.py --for svs            # 唱歌：要 5.5 GB 显存 / 6 GB 内存
python scripts/preflight.py --for tts            # 语音：要 6.5 GB 显存 / 6 GB 内存
python scripts/preflight.py --for svs --unload-tts   # 不够就顺手卸掉 9880 再复查
```

它打印显存/内存余量、9880/9881 是否常驻，判断"可以跑 / 先别跑"并以退出码返回
（0 = 可以）。**不够就不要开跑**，先卸服务或等别的任务结束；渲染、评审、全量
测试一律串行，不并发。

## 让外面的人听音频（tailnet，2026-10-25）

公网图床在这条网络里基本不可用（catbox reset / litterbox 打开很慢 / pixeldrain
reset / 0x0.st 连不上）。改成从本机起静态服务，用 Tailscale 的 HTTPS 入口给链接：

```bash
python scripts/serve_audio.py --dir data/soulx --host 127.0.0.1 --port 8899
tailscale serve --bg --https=8443 http://127.0.0.1:8899
tailscale serve status          # 看当前代理；443 上已有 OpenCode，不要覆盖
```

- 只绑 `127.0.0.1`，由 tailscaled 反代，Windows 防火墙不用开口子。
- 443 端口已被 OpenCode 的 serve 占用（根路径），**新增用 8443**，不要 `--set-path`
  动别人的配置。
- `serve_audio.py` 支持 `Range`（206），手机 Safari 靠它才肯播。
- 收工：`tailscale serve --https=8443 off`。

## 常用命令

```bash
python -m fatfish init
python -m fatfish doctor
python -m fatfish serve --port 0 --open
python -m fatfish nightly --date 2026-10-20 --dry-run
python -m fatfish nightly --date 2026-10-20
python -m fatfish backup
python -m fatfish memory search "鲸鲸"
python -m fatfish tts status
```

## 测试

```bash
.venv-indextts\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

- 当前：2,680 条，约 289s，全部通过（2 skipped；2026-11-20 清理后实测）。
- 测试必须离线可跑；测试不依赖旧贴纸向量/旧 SoulX。
- 不要把 `data/fatfish.db` 指向真实库；测试用临时目录。

## 备份

```bash
python -m fatfish backup
```

关键资产另见 [资产](assets.md)。跑 `nightly` 前先备份 DB（`python -m fatfish backup`）。

> 以下两组研发期内部工具（导出数据包 / 回导 LLM 细节 / 向量 BLOB 迁移）未随公开发布版分发。
> 设计要点保留备查：导出包含 turns/events/details/memories 与抽取 prompt；回导只接受
> “turn 属于该 event + quote 是精确子串”的行，被拒只报告不修复，`source='llm'` 靠 `detail_hash` 去重；
> 向量迁移把 JSON 转 float32 BLOB（无需改表，`loads()` 同时读新旧格式，实测体积显著下降）。

## 日志与观测

`data/logs/`：`qq.log`、`web.log`、`tts.log` 等。`data/run/` 有 PID/锁文件；无对应进程的残留可清。

数据库里的 `metrics` 表是**遥测**，不是记忆：

| 列 | 含义 |
|---|---|
| `at` | 发生时间 |
| `key` | 指标名（`llm_latency_ms`、`llm_prompt_tokens`、`llm_cache_hit_rate`、`intent_classify`、`tts_generated`、`voice_sent`、`wakeup_*`、`life_interrupt`、`llm_error`…） |
| `value` | 数值 |
| `tags_json` | 维度（backend/error/session 等） |

- 每次 LLM 调用会写约 6 行（延迟、prompt/completion token、缓存命中/未命中、命中率），所以 390 次调用≈2,340 行。
- 每晚按 `nightly.metrics_retention_days=30` 自动清理；当前 2,621 行 / 475KB 是 30 天内的正常量。
- 用途：成本/延迟/缓存命中/意图分类/TTS 成功率。需要长期趋势时做日聚合，不要保留原始行。

## 30 分钟自主决策轮（T-0025）

她上线后每 30 分钟自己决定这半小时干什么（分享/看动态/聊B站/休息）。设计细节见
[activity.md](activity.md#自主决策轮已实施t-00252026-10-25)；运维要动的只有几处：

- **总开关**：`[activity].enabled`（low/standard/remote 默认 true；研发期夜测档曾显式 false，
  避免和夜测驱动双循环双记账）。急停就把 `enabled` 改 false 重启。
- **台账**：`data/logs/activity-YYYYMMDD.jsonl`，每轮/每次跳过一行
  （`ts/decision/session/menu/actions/tokens/...`）；晨报汇总读它。
  `decision=skipped` 是省电捷径或配额/LLM 不可用，属正常；连续 `error` 才需要看
  `decision_log`（kind=`activity_turn`）里的错误文本。
- **审计**：`decision_log` 里 kind=`activity_turn`——`action=decide`（act/rest/error）与
  `action=skip`（no_new_content / daily_quota / llm_unavailable）；白天硬顶
  `activity.max_llm_turns_per_day`（默认 48）按它计数。
- **B 站采集**：`[activity].bili_browse_*` 只管节奏，真正跑起来还需要
  `[bilibili].enabled=true`（cookie 就绪后手动打开）。
- **水位移位**：meta `last_activity_turn_at` / `last_activity_bili_browse_at`。调试时删掉
  对应 meta 即可让下一 tick 立即出轮（安静时段内仍会等 08:00）。

## 夜间实测驱动（历史）

2026-10-25 曾用研发期内部驱动脚本做过一次夜间实测窗口（03:00–08:00、每 30 分钟一轮、
夜间不发 QQ 消息、产物只有记忆/说说草稿/台账；测试档才放开 `qzone.friends_content_to_llm`）。
该脚本与 `config/night-test.toml` 属一次性研发脚手架，未随公开发布版分发；
如需复现同类实测，参照本分支「自主活动」与 `nightly` 的配置自行编排即可。

## 版本控制

本仓为单一初始提交的公开发布树。`data/`、`models/`、`vendor/`、`.venv-*` 已忽略。

## 排障

| 现象 | 先查 |
|---|---|
| QQ 不回 | NapCat 是否登录、`qq.log` 是否 502、`QQ_ACCESS_TOKEN` |
| QQ 频繁掉线 | 桌面 QQ 是否在跑（与便携 QQ 互斥，看门狗会拉回来）；`data/logs/napcat-supervisor.jsonl` 里的在线时长；`data/logs/napcat-console.log` 里有没有 `KickedOffLine` |
| 没声音 | 9880 是否在跑、`tts.log`、显存是否被 ComfyUI 占用 |
| 拍照失败 | `photo_jobs.error`：`vram_conflict_tts` / `comfyui_start_timeout` |
| 记忆不增长 | `episodes` / `memory_items` 行数；`schema_meta.last_nightly_date` |
| 表情包不生成 | `MODELSCOPE_API_KEY` 是否设置；`sticker.generation_enabled`；`sticker_jobs.error` |
| 唱歌失败 | `.venv-soulx` 是否可用；`YUE2_ROOT` / `RVC_ROOT` / `RVC_PTH` / `RVC_INDEX` 是否存在；`prompt_v6.json` 是否存在 |

## 云端 NapCat 掉线 2026-10-30（账号被踢，不是升级事故）

**现象**：11:51 起 `fatfish-qq` 每 30s 刷 `cannot connect to OneBot at ws://127.0.0.1:3001: ConnectionRefusedError`；
WebUI 6099 正常在听，3001 没有监听，容器 `Up`、QQ 进程也在。服务 `active` 但**空转**（连不上 = 不收不发）。

**根因**（`docker logs napcat`）：
```
11:51:13 [error] [KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。
11:51:14 [info] 账号状态变更为离线
11:51:17 [warn] [Core] [Login] 账号已离线，正在重启 Worker 以重新创建 QQ 登录服务
11:51:21 [info] [Process] 已清理残留进程 / 11:51:22 [NapCat] Core Version: 4.18.41
11:51:22 [info] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=...
```
重启 Worker 之后**没有任何「登录成功 / 快速登录」日志**——只有 WebUI 起来了。
OneBot11 的 WS 服务器是**登录成功后才启动**的，所以账号离线 ⇒ 3001 必然不监听。

**时间线**（宿主 CST，证据全部来自 `docker logs napcat` / `journalctl` / `stat`，只读排查）：

| 时间 | 事件 | 证据 |
|---|---|---|
| 10:04:40 | `fatfish-qq` 开始刷 `connection lost`（累计 152 条），说明 3001 那会儿已经没了 | `journalctl -u fatfish-qq` |
| 10:10:27 | `/opt/napcat/qq` 被改名为 `qq.bak-20261030-101027`（15M；mtime 停在 00:31:08，ctime 10:10:27）——旧登录态被人工移走 | `stat -c "%n birth=%w ctime=%z"` |
| 10:31:24 | 容器 `created`；`10:31:31` 打印「没有 -q 指令指定快速登录，将使用二维码登录方式」，二维码存 `/app/napcat/cache/qrcode.png` | `docker inspect` / `docker logs` |
| 10:33:33 / 10:35:35 / 10:37:37 | `[Core] [Login] Login Error, ErrType: 1 ErrCode: 3`（二维码 2 分钟过期一轮，共 3 次） | `docker logs napcat` |
| 10:39:18 | `Login Error, ErrType: 1 ErrCode: 5` | 同上 |
| 10:43:26 | 容器 `started`（restarts=0，仍是同一次 run 内的重启），仍然无 `-q` | `docker inspect` |
| 10:45:33 | 再一次 `ErrCode: 3` | `docker logs napcat` |
| 10:46:36 | `[Core] [Login] 二维码已被扫描，等待确认...` → 登录成功 | 同上 |
| 10:47:11 | `fatfish-qq` 首次 `[qq] connected: ws://127.0.0.1:3001` | `journalctl -u fatfish-qq` |
| 11:41:43 | 最后一条正常收到的消息 | 同上 |
| 11:46:42 | `[ServerTime] 时间同步完成，偏差 -679ms` | `docker logs napcat` |
| **11:51:13** | **`[KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。`** | 同上 |
| 11:51:14 | `账号状态变更为离线` | 同上 |
| 11:51:17 | `[Core] [Login] 账号已离线，正在重启 Worker 以重新创建 QQ 登录服务` | 同上 |
| 11:51:21 | 清理残留进程 PID 169(Utility) / 197(GPU) | 同上 |
| 11:51:22 | `NapCat.Core Version: 4.18.41` + WebUI URL，**之后没有任何登录日志，`qrcode.png` 仍是 10:45 那份** | `docker logs` + `ls -la` |
| 11:54:06 | （T-0064 验收时我重启了 `fatfish-qq`）仍然 refused | `journalctl -u fatfish-qq` |

**根因结论**：服务端把这个 QQ 的登录会话判为失效，属于**账号侧被踢**，不是本机/服务器故障。
最可能的触发是「机房固定 IP + 10:31–10:39 连续 6 次二维码登录失败（ErrCode 3 五次 + ErrCode 5 一次）」，
被腾讯账号安全策略记了一笔：10:46 扫码建立起来的会话在 65 分钟后的例行 token 续期时被判失效。
`ErrCode 5` 在 NTQQ 里通常对应「操作过于频繁 / 账号被限制」，与上面的连败记录吻合。

**排除项**（都查过，别再重复怀疑）：
- 不是 QQ/镜像升级：容器 `created=10:31:24 started=10:43:26 restarts=0`；镜像仍是 4.18.41；
  QQ 主进程 `PID 132 lstart=Fri Oct 30 10:43:28`，到排查时（12:14）`etimes=5469` 一直没变；
  宿主机根本没有 `/opt/QQ`，`/var/log/dpkg.log*`、`/var/log/apt/history.log*` 里 grep `linuxqq|QQ` 全 0 命中；
  10:43–11:41 一直在正常收发消息。`/opt/napcat/qq` 的 10:46 mtime 只是首次登录写盘。
- 不是容器重启 / OOM：`restarts=0`，`journalctl 11:40–11:56` 里没有 docker、没有 oom-killer。
- 不是配置/token 问题：`onebot11_*.json` 合法，`websocketServers[0]` = `127.0.0.1:3001`、`enable: true`；
  `napcat_*.json` 只是日志开关。
- 不是「本机同号登录顶号」：本机 `Get-CimInstance Win32_Process` 只有 3 个 node.exe，全是 WorkBuddy
  （11:25:56 / 11:29:04 ×2），**没有 QQ.exe、没有 NapCat**；`netstat -ano` 里 3001/6099 都没有监听；
  `vendor/napcat/NapCat.Shell/config/` 的 mtime 还是 10-22（本次没活动）；`data/logs` 里没有 qq.log。
- 不是有人动服务器：10:00–11:56 所有成功 SSH 都来自 `100.x.y.z`（tailnet，我方），
  其余只是 零星外部地址的失败探测；没有 `docker restart`。
- 不是文件系统/权限问题：登录态目录在 10:46 正常建立，`nt_db` 10:47 落盘，`crash_files` 是空的。

**唯一无法证实的假设**：手机/另一台设备用同一个号登录（或点了「退出其他设备」）。
这发生在腾讯服务端，服务器侧日志看不到；QQ 自己的 `nt_data/log/qq-log_2026-10-30-11.qqxlog`
是加密/二进制格式，`grep -a` 对 `Kick|offline|下线|另一台|login` 全部 0 命中。**需要用户确认 11:51 前后有没有在手机/电脑上登这个号。**

**恢复**（必须人工登录，agent 不代扫）：大概率要**扫码**——
现行 QQ 数据目录是 10:46 全新建的（老的 10:10:27 已被改名成 `qq.bak-20261030-101027`），
容器启动时明确「没有 -q 指令指定快速登录」，被踢后 NapCat 也没有自动重登，说明本地 token 已被服务端作废。
`-q` 快速登录只能当场赌一次（失败会自动回落二维码），**没法先只读验证**，所以默认按扫码准备。
注意 `/app/napcat/cache/qrcode.png` 还是 10:45 的旧图（被踢后没重新生成），别拿它扫，走 WebUI。

1. 隧道进 WebUI 扫码：`ssh -L 6099:127.0.0.1:6099 <cloud-host>` → 打开 `http://127.0.0.1:6099/webui?token=<config/webui.json 里的 token>`；
2. 或先 `docker restart napcat` 赌一次快速登录（成功则 3001 直接回来；失败就退到登录页，等价走 1）；
3. 判据：`ss -ltn | grep 3001` 有监听 + `journalctl -u fatfish-qq -n 20` 出现 `connected`，不再刷 refused。
4. 扫码成功后**不要立刻删** `qq.bak-20261030-101027`，等快速登录凭据重新积累一轮再清理。

**待办**：
- `napcat-supervisor` **没装**（`systemctl cat napcat-supervisor` 为空、journal 无条目），掉线无人告警，
  只有 NapCat 内部重启 Worker。要装的话先确认它与 Docker 模式兼容，再 `install -m 644 deploy/linux/napcat-supervisor.service`。
- 机房 IP / 出口 IP 变化（tailnet 重连）会触发「登录已失效」，属**常态化风险**，不是代码 bug；
  云端长期运行要预留「扫码恢复」通道，别把 WebUI 端口对外开。
- `fatfish-qq` 的 `systemctl is-active` 不能证明 QQ 在线：NapCat 未登录时它照样 active 并一直重试。
  验收要额外看 3001 端口或 `fatfish doctor`。
