---
title: 平台接入
type: branch
status: active
tags: [qq, napcat, onebot, bilibili, qzone, comments, web, cli, permissions, prompt, desktop]
updated: 2026-11-15
---

# 平台接入

> 上级：[项目总览](../overview/project-status.md) · 相关：[运行时](runtime.md) · [运营/活动](activity.md) · [运维](ops.md) · [ADR-0018](../adr/0018-platform-prompt-variants.md)

## 提示词平台变体（2026-11-15）

不同入口不再共用一个固定前缀：`Runtime.handle_user_message(platform=...)` → `fatfish/prompt_variants.py` 选变体 → `stable_prefix(capabilities)` 只拼该入口能执行的动作块与平台规则。

| 入口 | platform | 变体 | 动作集 |
|---|---|---|---|
| QQ 私聊（NapCat） | `qq_private` | `full` | 全部 19 个动作 |
| QQ 群 | `qq_group` | `qq_group` | 聊天 + 表情包（群规则进前缀；不再带主人私聊工具清单） |
| 桌宠 `desktop:owner` | `desktop` | `desktop` | 全能力 **− send_voice**（气泡由壳子自动念）+ 桌面规则 |
| 漫游目标房间 | `roam_room` | `roam_room` | 全能力 + 漫游规则 |

运行时过滤：`qq.read` / `qq.talk` 只在 adapter 实现 `fetch_friend_history` / `fetch_group_history` / `fetch_forward_messages` / `fetch_group_members` 时出现（即 NapCat/OneBot）；`photo` / `sing` 按服务 enabled。**官方腾讯 QQ Bot 接口（`platforms/qqbot.py`）没有历史消息端点，因此官方 Bot 下 `read_chat` / `talk_to` / 上线补回都不可用，提示词里也不会出现它们**；要用读聊天必须走 NapCat。

越权动作由 `PolicyEngine.validate(allowed=...)` 返回 `unsupported_on_platform`（在 trust/安全校验之后，群聊仍报 `group_not_allowed`）。完整决策见 [ADR-0018](../adr/0018-platform-prompt-variants.md)。

## QQ

| 项 | 事实 |
|---|---|
| 协议 | NapCat + OneBot v11 正向 WebSocket（3001） |
| 代码 | `platforms/onebot.py`、`platforms/qq.py`、`platforms/catchup.py` |
| 能力 | 私聊、群（@ 触发）、文本、语音 `record`、图片、表情包；outbox handler：`qq_send` / `proactive_message` / `friend_share`（T-0012 v3，主动私聊好友位姐姐，原话投递，payload 缺 `friend_uin` 报错不猜测）/ `qq_catchup`（T-0028，安静时段顺延的补回重扫）/ `roam_message` + `roam_notice`（ADR-0014：目标房间开场与主人归位通知，目标必须来自 payload session，群 session 直接拒绝） |
| 状态 | ✅ 已实机收发（10-22 多次连接成功；10-23 最后一次 502 是 NapCat 未运行） |
| 依赖 | `.env` 的 `QQ_ACCESS_TOKEN`；NapCat 需本人扫码 |
| 缺口 | 引用回复未实现；`web/server.py:827` 硬编码 `features.group_chat=False`；掉线自动重连无证据 |

### 上线扫未回复（T-0028，ADR-0008）

| 项 | 事实 |
|---|---|
| 代码 | `platforms/catchup.py`（判定/水位/审计），接线在 `platforms/qq.py` |
| 触发 | `run_forever` 连接成功处武装 → 连接稳定 `qq.catchup_delay_seconds`（默认 10s）后的 tick 触发一次；`catchup_last_scan_at:<session>` meta 水位 + `qq.catchup_min_interval_seconds`（默认 300s）防 connect 风暴；水位先写后判（崩溃不循环） |
| 范围 | 主人（`owner_bindings role='owner'`，**无任何绑定**时才回落 `person:owner` 卡的 uin）→ 姐姐（`role='friend'`，无绑定时回落 `person:friend` 卡）→ **人物卡名册**（`entities kind='person'` 且带 QQ uin 的已知活卡：`relation` 非空或 `source in (cli,binding,manual)`；`qq.catchup_max_people` 默认 20，`catchup_include_unknown_cards` 默认 false）；主人先、姐姐后；会话重叠只扫一次。绑定存在但为 web 会话时按不可达处理，不复活旧卡 uin。`qzone.owner_uin`/`qzone_cookies.json` 是 QZone 发帖账号（机器人号），不参与补回 |
| 判定 | 主数据源=本地 `conversation_turns`：最后一条永久 user 回合新于最后一条 assistant（按流 id）即未回复；NapCat `get_friend_msg_history`（已实机验证）为增强路径，拉 ≤`qq.catchup_max_messages`（默认 20）条，按「晚于她最后回复 + 与本地 user 回合内容/±300s 去重」diff 出真正漏掉的；没有本地 assistant 回合的人物卡会话以历史里机器人最后一条回复为 cutoff。action 不可用/失败一律降级本地流，不是硬依赖 |
| 补回 | 每联系人一个 catch-up 回合（session 原样复用）：提示词「你掉线错过了一些消息，按顺序看完再回复，不要跳过任何一条」为系统指令（`ephemeral_input=True`，不进历史），漏掉的消息经 `wrap_untrusted(kind='catchup_messages')` 走 `extra_evidence`（同 qzone/bili 旁路信任面）；回复走普通 `qq_send` 原路送达，不算主动消息、不占 proactive 配额、不挂 self_continue |
| 安静时段 | `qq.catchup_respect_quiet_hours=true` 默认遵守（用 `memory.quiet_start/quiet_end`）：深夜扫描不回话，按日幂等键入 outbox `qq_catchup`（`available_at=安静结束 08:00`）；次日到点由在岗适配器重扫（届时已答的判 nothing_new）；适配器未连接时 handler 返回失败交回 outbox 重试，下次上线重扫兜底 |
| 审计 | `decision_log kind='catchup'`：每次全扫一条 `scanned`（晨报统计补了几条），每目标一条 `replied` / `nothing_new` / `skipped_quiet` / `skipped_offline` 等 |
| 登录态 | catch-up 回合与重试 handler 都要求当前连接存活（`client.connected`），NapCat 掉线不产生无法投递的回复；LLM 不可用按 self_continue 先例沉默（不产出兜底话术） |
| 状态 | ✅ 离线 fixture 全覆盖（80 条，`tests/test_catchup.py`：判定/交错/水位/顺序/去重/wrap 注入/安静顺延/范围外不可达/历史接口可用与降级/人物卡目标/陌生人卡过滤/web 绑定不复活旧卡/历史图片/call_action echo 关联）；真实 NapCat `get_friend_msg_history` 已实机验证（2026-10-31），历史图片在 `vision_enabled=true` 时随补回回合内联给 LLM |

### 掉线监管（2026-10-29）

| 项 | 事实 |
|---|---|
| 现象（10-25 实测） | `[KickedOffLine] 你的账号当前登录已失效` 共 5 次，在线 39 分钟 / 1h43 / 57 分钟 / 1h13 / 1h48；**紧跟着 `自动快速登录失败: 你的用户身份已失效`——会话和快速登录令牌一起作废**，所以每次都要人工扫码 |
| 噪音 | 掉线后 NapCat 在登录页反复取二维码：`Login Error ErrType:1 ErrCode:3` 每 ~2 分钟一次，持续约 40 分钟（不是"登录风暴"，是二维码自动刷新，但客户端整小时挂在登录页本身也是异常特征） |
| 代码 | `scripts/napcat_supervisor.py`（只监管，不改 NapCat 配置、不碰 `fatfish/`） |
| 策略 | 两种离线分开处理：① **令牌仍有效**（进程/网络抖动）→ 指数退避重启 5/15/30/60 分钟，最多 4 次；② **令牌失效**（`自动快速登录失败`）→ **不重启**，等人工扫码 `--scan-timeout`（默认 15 分钟），等不到就停掉 NapCat 退出码 2，避免整小时挂在登录页 |
| 台账 | `data/logs/napcat-supervisor.jsonl`：每次状态迁移 + 每段在线时长（`sessions`/`online_seconds`），改完能对比；NapCat 控制台输出落 `data/logs/napcat-console.log` |
| 用法 | `python scripts/napcat_supervisor.py --uin 9999000003 --kill-desktop` |
| 环境前提 | 桌面 QQ 与便携 QQ 互斥（`start_napcat.bat` 有守卫）；`--kill-desktop` 会先杀桌面 QQ，看门狗若把它拉回来仍会互踢 |

## QQ 空间（读 + 分享欲写路径）

| 项 | 事实 |
|---|---|
| 代码 | `qzone/client.py`（cookie + g_tk，读 + 发布）、`qzone/collector.py`（读路径）、`qzone/posts.py`（写路径）、`qzone/qr_login.py`（扫码登录） |
| 读能力 | 好友动态：feeds3_html_more（ic2 代理，2026-10-25 实测鉴权通过）优先，响应为 `_Callback` JSONP + JS 字面量 + 内嵌 HTML 块，`feeds_html.py` 宽松解析（裸 key/单引号/尾逗号容忍；广告与 null 占位跳过），mobile list 为降级候选（2026-10-25 实测被拒 -4003）；自己说说 emotion_cgi_msglist_v6（2026-10-25 实测通过，0 条说说时正确返回空）。feeds 单端点失败可 `qzone.feeds_optional=true` 降级 partial 不计熔断。**2026-10-29 起新增加一层回退**：feeds3/feeds2 被 ic2 判未登录（`-3000/-4001`，`tips 0103-87`）时，对 `QZONE_FRIEND_UINS`（.env，逗号分隔）里的好友逐个拉 `emotion_cgi_msglist_v6?uin=<friend>`——实测好友 uin 也返回 `code:0`、字段与聚合 feed 一致，每人 1 请求；一条都没拿到才记失败（不误判成 cookie 失效）。→ `qzone_feed_items` → 记忆（schema v7） |
| 实体层（T-0013） | ingest 时作者同步进实体层：`person:qq:<uin>`（无 uin 时按昵称键）、self→`person:owner`、好友位→`person:friend`，昵称变更按别名归并；mention 落 `entity_mentions(source_type='qzone', source_id=external_id)`；tags（QQ空间/动态/作者名）走 `tags.sync_tags` 到 `memory_item`；回填 `fatfish qzone backfill-entities`（幂等、只增不删）。召回验证口径：`entities_for_query` + mention 回链（event_details 不含动态原文） |
| 写能力 | `post_qzone` 动作（可选 `desire` 0-1 自评）→ 阈值 ≥0.7 且 `require_approval=false` 时直入 outbox（`decided_by='desire-threshold'`），否则草稿 → 主人审批（Web/CLI）→ outbox `qzone_post` → `publish_text`（schema v9，`desire`/`decided_by` 列） |
| 扫码登录 | ptlogin2 `ptqrshow`（取码落 `data/qq_login_qr.png`）+ `ptqrlogin` 轮询（`ptqrtoken=hash33(qrsig)`）+ `check_sig` 重定向截获 cookie → 合并经 `QzoneClient.save_cookies()` 落盘。参数候选表 5 组运行时降级（STATE 7 自动换下一组）；**候选组合与备用 appid 均待 10-25 02:00 pre-flight 实测回填** |
| 免扫码续期（ADR-0011） | `qzone/browser_login.py`：起独立 profile 的 Edge/Chrome（`data/run/qzone-browser`，`--remote-debugging-port=0`，**窗口挪屏幕外**——实测 qzone 拒绝 `--headless=new`）→ `Storage.clearCookies` + `setCookies` 灌旧 cookie → 浏览器自己打开 `user.qzone.qq.com/<uin>` → `Storage.getCookies` 取回。**判据以服务端为准**：`emotion_cgi_msglist_v6` 回 `code:0` 才算有效（`-3000` 判死且**绝不覆盖**文件；`-4003` 属端点问题照存并附 warning）；页面 `g_qzonetoken` 只作辅助（当前 qzone 登录成功也不下发）。没登录态时 20s 内快速失败并提示 `qzone login --browser` 开窗口人工登录一次（登录成功当刻收走 cookie，之后静默）。CDP 用 60 行标准库 WebSocket，不引依赖。**已知**：`skey` 是 session cookie，profile 不是登录态仓库，持久化只靠 `data/qzone_cookies.json`；`--keep-open` 常驻实例可被后续命令自动接管（`reused:true`，省启动且多留一份活凭据，代价约 775 MB 内存） |
| 端点实况（2026-10-29） | `doctor` 的 `ok` 现在只回答"cookie 还能不能登录"（探测 `fetch_emotions`），好友动态 `fetch_feeds` 单独报在 `feeds` 字段。实测：同一份有效 cookie，`emotion_cgi_msglist_v6` 稳定 200（她的号 0 条说说），`feeds3_html_more` 间歇 `-3000`（几分钟前同一请求还返回 `code:0` + 312 KB）、mobile 兜底稳定 `-4003`；已排除 `o0` 前缀 / `X-Requested-With` / `Referer` / `Origin` / `Set-Cookie` 轮换。**好友动态读取当前不可用，属平台侧状态** |
| 入口 | **首选** `fatfish qzone refresh`（headless 免扫码续期）/ `fatfish qzone login --browser`（开窗口人工登录一次）；一键 `QQ空间登录.bat` → `scripts/qzone_login_ui.py`（本地 HTML 页，浏览器每 2s 自动刷新图，过期自动换新码，无过期赛跑）；兜底 CLI `fatfish qzone login --qr`（生成 PNG + 状态行轮询，过期自动换码 ≤3 次）；`--cookie` 粘贴路径保留 |
| 安全 | qrsig / cookie 值 / check_sig URL 绝不打印不落日志；输出只有状态码、字段名、掩码 uin（前 3 + **** + 后 4）；qrsig 只存内存 CookieJar（实例隔离，不落盘）；测试全离线 fixture（57 条，`tests/test_qzone_qr_login.py`；含 T-0021 WAF 退避/扫码宽限） |
| 隐私 | 好友动态默认不进 LLM（`qzone.friends_content_to_llm=false`）；草稿命中 cookie 模式 / secret_notes → `blocked`（存脱敏文本） |
| 配额 | `max_posts_per_day=0` = 不限制（>0 才生效，批准即占名额）、安静时段 23:30–08:00 顺延、发布失败重试 ≤3 次后 `failed`；`share_with_owner` 走 wakeup 主动消息（每日 ≤12 次，安静时段顺延）；`share_to_friend` 走 outbox 直投（`share.max_to_friend_per_day=2`，0=不限，计数走 decision_log；安静时段顺延；同日同文幂等；ADR-0006） |
| 状态 | ✅ 全离线测试通过；免扫码续期上线（ADR-0011，2026-10-29 实机验证：浏览器登录 → 收 cookie → `refresh` 无人值守 → `doctor` `probe.ok=true`）。**当前 cookie 有效但好友动态端点不可用**：`feeds3_html_more` 间歇 `-3000`、mobile 兜底 `-4003`（同一份 cookie 下 `emotion_cgi` 一直 200），见下方「端点实况」；发布/登录仍待 `doctor --online` + pre-flight 回填（ADR-0004） |
| CLI | `qzone status/login(--qr|--browser|--cookie)/refresh/fetch/show/backfill-entities/doctor/forget` + 写路径 `posts/approve/reject` |
| Web | `/api/qzone/feed`、`/api/qzone/posts`、`/api/qzone/review`；「QQ动态」视图（草稿审批卡 + 最近动态） |
| 边界 | 图片上传不做（`media_json` 预留）；QQ 评论只做响应内嵌解析（见下节）；**动态下评论/点赞明确不做**（2026-10-25 boss 拍板，ADR-0006；对好友的表达走 `share_to_friend` 私聊，收件人仅绑定行解析、无配置绕过） |

### 评论区读取（T-0017，只读）

| 项 | 事实 |
|---|---|
| 原则 | 评论区是公开 UGC、注入风险最高：只经由 wrap_untrusted 的记忆/召回通道进提示词，不进「看看空间动态」旁路证据块；不抬 importance；条数/长度全走 config 上限 |
| B站 | `bilibili/client.py::video_comments(bvid, limit)`：`/x/v2/reply/wbi/main`（WBI 签名 + 复用节流），oid 由 bvid 本地换算（公开 BV↔AV 算法，省一次 view 请求）；单页 `ps≤20` 不翻页；每条 `「用户: 内容」` ≤150 字；`comments_per_video=10`，0=完全关闭不请求 |
| B站接线 | `browse_once` ingest 时传 `ingest_video(comments=...)`（已有参数：rag_meta `comments[:2000]` + rag_text 拼接，零 schema 改动）；评论拉取失败不挡视频入库 |
| QQ空间 | 零额外请求路线：feeds/emotions 响应内嵌 `commentlist` 容错解析（含 feedinfo 外层回退），每条 ≤100 字带昵称、每动态 ≤5 条（`comments_per_item=5`，0=不提取）；写入 rag metadata `comments` 与 memory evidence；继承条目 `origin_scope`（好友→`qzone_friend` 默认不进提示词） |
| 禁止 | 不新增 per-item 评论拉取（每动态+1 请求的风控面不值得）；不做评论回复/点赞/写操作 |
| 状态 | ✅ 离线 fixture 全覆盖（reply 解析/截断/开关、内嵌评论提取/截断/上限/scope）；真实 reply 端点与内嵌字段形状待实测回填 |

## B 站

| 项 | 事实 |
|---|---|
| 代码 | `bilibili/client.py`（WBI 签名 + 二维码登录）、`bilibili/collector.py` |
| 能力 | 只读：热门/搜索/视频信息/评论区（见上「评论区读取」）→ finding 卡 → RAG |
| 状态 | 🟡 手动 `bilibili browse` 可跑（`rag_chunks=2`）；`low.toml` 里 `bilibili.enabled=false` |
| 资产 | `data/bilibili_cookies.json`（455B） |
| 边界 | 不做评论发布/私信/刷量；评论只读（`comments_per_video`）；后台定时采集未做 |

## Web

| 项 | 事实 |
|---|---|
| 服务 | `fatfish/web/server.py`，默认 8765（`start_all` 用随机空闲端口） |
| 鉴权 | token → Cookie；POST 需 `X-Fatfish-Request: 1` + Origin 校验 |
| API | `/api/chat`、`/api/chat/stream`（NDJSON 逐句 TTS）、`/api/memory*`、`/api/services/*`、`/api/qzone/*`、`/api/audio` 等 |
| 前端 | `web/app.js`（181KB+）+ `style.css`（100KB+），8 个视图；另有 `stream.html` |
| 状态 | ✅ |

## CLI

`fatfish/cli.py`，55 个叶子命令。常用：

```bash
python -m fatfish init
python -m fatfish doctor
python -m fatfish serve --port 0
python -m fatfish chat "在吗"
python -m fatfish nightly --date 2026-10-22
python -m fatfish memory search "鲸鲸"
python -m fatfish qq status
python -m fatfish bilibili browse
```

## 待授予的新权限（规划）

见 [活动规划](activity.md)：QQ 动态发布、B 站观看/收藏。原则：只读优先、频率闸门、内容可审计、失败不影响聊天。

## 云端只读接入（第二阶段，2026-10-30）

B 站与 QQ 空间在云端**只开读**，写路径保持关闭。它们走 cookie HTTP，**不经过 NapCat**，
所以 NapCat 掉线不影响这一节（反之亦然）。

**数据**：只把两份文件放到云端 `/opt/fatfish/data/`，不要覆盖 data 下其他内容：

```bash
scp data/bilibili_cookies.json data/qzone_cookies.json fatfish-cloud:/tmp/
ssh fatfish-cloud 'install -o fatfish -g fatfish -m 600 /tmp/bilibili_cookies.json /opt/fatfish/data/;
                    install -o fatfish -g fatfish -m 600 /tmp/qzone_cookies.json /opt/fatfish/data/;
                    rm -f /tmp/*_cookies.json'
```

**开关**只写在云端私有配置 `/opt/fatfish/config/cloud.local.toml`（不提交仓库，`cloud.toml` 保持 `enabled = false`）：

```toml
[bilibili]
enabled = true      # 才跑后台 browse；CLI `bilibili browse` 本身不受限
read_only = true

[qzone]
enabled = true      # 才跑 30 分钟轮询；CLI `qzone fetch` 本身不受限
read_only = true    # post_qzone 直接返回 qzone_read_only，写路径仍需审批
```

改完 `systemctl restart fatfish-qq`（CLI 每次自己读配置，不用重启）。

**验收**：

```bash
cd /opt/fatfish
sudo -u fatfish .venv/bin/python -m fatfish --profile cloud --config config/cloud.local.toml bilibili status
sudo -u fatfish .venv/bin/python -m fatfish --profile cloud --config config/cloud.local.toml bilibili browse --limit 3
sudo -u fatfish .venv/bin/python -m fatfish --profile cloud --config config/cloud.local.toml qzone status
sudo -u fatfish .venv/bin/python -m fatfish --profile cloud --config config/cloud.local.toml qzone fetch
```

- `bilibili status` → `logged_in: true`；`browse` 返回带 `memory_id` / `rag_id` 的卡片（= 已入库）。
- `qzone status` → `cookie.fields_present.p_skey: true`、`g_tk_ready: true`、`write_enabled: false`；
  `qzone fetch` → `"ok": true` 且 `new` 有增长。
- **失败特征**：`qzone api error -3000: 请先登录空间` = cookie 死了（本机 `qzone doctor` 会给出同样结论，
  与 IP 无关）；`-4003: 非法操作` 多半是 feeds 端点与 cookie 不匹配，先看 `qzone doctor` 的 probe。

**续期（过期时，在家里做）**：

1. 家里 `python -m fatfish qzone login --browser`（或刷新：`python -m fatfish qzone refresh`，免扫码）/
   `python -m fatfish bilibili login --qr`；
2. 家里 `python -m fatfish qzone doctor` 确认 `online_checked.ok = true`；
3. 把 `data/qzone_cookies.json`（或 `bilibili_cookies.json`）按上面的 `install` 覆盖云端，`chown fatfish:fatfish` + `chmod 600`；
4. `systemctl restart fatfish-qq`，再跑一次验收命令。

> 云端**不做登录**：机房 IP 扫码风险高，且 qzone 会拒绝 `--headless=new`。
> 后续可用 NapCat 的 `get_cookies`（`qzone.qq.com` 走 p_skey 分支）替代手工粘贴，见
> [NapCat Qzone pskey 可用性调研](napcat-pskey.md)；本轮**不实现**，只记一笔。
