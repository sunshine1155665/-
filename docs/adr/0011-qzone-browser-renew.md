---
title: ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态
type: adr
status: accepted
tags: [adr, qzone, cookie, browser, cdp, risk-control]
updated: 2026-10-29
---

# ADR-0011：QQ 空间免扫码续期——借本机浏览器的登录态

> 承接 [ADR-0004](0004-qzone-feed.md)（QQ 空间走独立 cookie 客户端）与[平台接入](../branches/platforms.md)里的扫码登录（T-0020/T-0021）。决定给 cookie 过期补一条**不扫码**的路径。

## 背景

2026-10-25 拿到的 cookie 到 10-29 已经死了：`feeds3_html_more` 和 `emotion_cgi_msglist_v6` 都回 `-3000`。也就是说读/写路径当时已经断了，只是没有告警。

现有两条补齐路径都不理想：

1. **`qzone login --qr`**：`ptlogin2` 扫码是风控最敏感的一段，T-0021 实测会进冷却（首拦 ≥5 分钟、三拦放弃），手机端确认还有 1–2 分钟时效。每次过期都要赌一次。
2. **`qzone login --cookie`**：要人去 DevTools 里粘 `p_skey`。`p_skey` 是 HttpOnly，复制体验差，也容易贴错。

而 QQ 空间的 web 端从来不需要用户"每四天扫一次"——浏览器登录一次就能待很久。我们缺的正是这一层：**让浏览器登录，我们只搬结果**。

## 决策

### 1. 免扫码 = 本机浏览器登录一次，我们搬 cookie

`fatfish/qzone/browser_login.py`：

1. 起一个**独立 profile** 的 Edge/Chrome（`--user-data-dir=data/run/qzone-browser`、`--remote-debugging-port=0`）；
2. `Storage.clearCookies` → `Storage.setCookies`，把本地已有 cookie 灌进去；
3. `Target.createTarget` 打开 `https://user.qzone.qq.com/<uin>`，**让浏览器自己**按 qq.com 的正常逻辑走；
4. `Storage.getCookies` 把字段搬回来落盘。

浏览器是原装正版，不打补丁、不注入、不改文件；网络侧看到的就是一个普通浏览器打开了自己的空间。

### 2. 真无头不行：无人值守 = 窗口挪到屏幕外

**实测（2026-10-29）**：同一份有效 cookie，`--headless=new` 会被 qzone 打回登录页，headful 能进空间。所以 `refresh` 的"无人值守"是 `--window-position=-32000,-32000`，**不是** `--headless`。真无头只留 `--true-headless` 供调试，文档里写明会被拒。

云服务器（Linux）上的对应做法：候选路径已包含 `chromium` / `google-chrome` / `microsoft-edge`（`/usr/bin` 与 `/snap/bin`），root 运行时自动加 `--no-sandbox`；因为没有图形环境，**必须用虚拟显示**：`xvfb-run -a python -m fatfish qzone refresh`。缺 `DISPLAY` 时会直接报这条可执行的提示，而不是让 Chromium 在后台起不来。

### 3. 判据以服务端为准，页面状态只作诊断

**踩过的坑**：cookie 失效时浏览器照样把我们灌进去的 `uin`/`skey` 原样回显，第一版按"有 skey 就算成功"直接把死 cookie 写了回去。随后又发现 `g_qzonetoken` 在当前的 `user.qzone.qq.com` 页面上根本不出现（登录成功也没有），而页面停在哪里会抖——同一份有效 cookie，有时进空间、有时停 `i.qq.com`。

所以现在的顺序是：

1. **服务端探测 ``ok``** → 会话有效，收工（唯一稳定信号）；
2. 页面 `g_qzonetoken` 有值 → 页面确证登录（关掉探测时的唯一信号）；
3. `login_required` 且无人值守 → 立即失败，不耗超时。

探测端点用 `emotion_cgi_msglist_v6`（自己的说说，单端点无降级）。不用 `fetch_feeds`：它的 mobile 兜底会把端点错误（`-4003`）盖成"最终失败"，而 `-4003` 与登录态无关。

| 服务端回包 | 含义 | 动作 |
|---|---|---|
| `code:0`（0 条也算） | 会话有效 | 落盘 |
| `-3000 need login` / `请先登录空间` | 会话是死的 | **不落盘**，报 `session_expired` |
| `-4003` / 解析失败 | 端点坏了，与登录无关 | 有 `g_qzonetoken` 时落盘，附 `warning` |

### 4. 灌之前先清：残留同名 cookie 会盖掉新的

profile 里可能留着上一轮的 `.qzone.qq.com` cookie；我们灌的在 `.qq.com` 上，浏览器按"越具体越优先"会先用残留的旧值，实测能把页面打回登录页。所以 `_inject` 先 `Storage.clearCookies`。

### 5. `skey` 是 session cookie，profile 不是登录态仓库

关掉浏览器后 profile 里只剩 `p_skey` / `pt4_token` / `ptcz` / `qlogin_uid`，`skey` 和 `uin` 都没了。持久化只靠 `data/qzone_cookies.json`。

因此这条路的适用面要说清楚：

* **能免扫码**：已保存的 cookie 还没被服务端判死时，`refresh` 起浏览器、验证、收回，无需人工；
* **不能免扫码**：cookie 已经被判死（`-3000`）时，只能人工登录一次。但那一次登录是在**普通浏览器窗口**里完成的（不用我们的 ptlogin2 扫码代码），工具在登录成功当刻收走 cookie。

（`--keep-open` 让浏览器一直活着的话，那份 session cookie 也会一直留在浏览器里——见第 8 条。）

### 6. `doctor` 的 `ok` 只回答"cookie 还能不能登录"

`doctor(online=True)` 改成探测 `fetch_emotions`，好友动态 `fetch_feeds` 单独报在 `feeds` 字段里，不影响 `ok`。原因见「后果」第一条：端点抖动会被误读成"需要重新登录"，也会让免扫码续期自我否定。

### 7. CDP 用 60 行标准库 WebSocket，不引依赖

`websocket-client` 在 QQ 适配器里是可选依赖；QQ 空间这条链路必须能在裸标准库里跑（[AGENTS.md](../../AGENTS.md) 硬约束 1）。CDP 只需要文本帧，掩码/分片/ping 三个细节十来行就够，已用假 socket 全覆盖。

### 8. 常驻实例可复用：`--keep-open` + 自动接管

`refresh --keep-open` / `login --browser --keep-open` 成功后不关浏览器。下一次任何一条命令会先读 `DevToolsActivePort` 并**试连**：连得上就直接接管（`reused: true`），连不上（残留端口文件）才重新启动。接管时不杀那个进程（`owned=False`），只收掉自己开的标签页。

收益是省一次启动（5–10 秒），**以及**那个浏览器里还留着 `skey` 这类 session cookie——等于多一份与 `data/qzone_cookies.json` 独立的活凭据，JSON 丢了还能从浏览器里捞回来。

要说清的两点：

* **它不会"多次产生新 cookie"**。同一份活会话反复收，值是一样的（实测 `rotated: false`）。QQ 空间没有滑动续期可刷，会话本来就是按周计的。
* **成本是内存**：本机实测一个 offscreen Edge 10 个进程、约 775 MB working set。8 GB 机器上跑 TTS/SoulX 前应该先关掉（与 [AGENTS.md](../../AGENTS.md) 的"显存/内存串行"约定同一道理）。

### 9. 输出红线不变

返回字典只有：掩码 uin、字段名列表、布尔量、`probe` 里的错误码。cookie 值不打印、不写日志、不进 `decision_log`。

## 备选与放弃

| 方案 | 为什么不用 |
|---|---|
| 自己按 `ptlogin2 check_sig` 静默续期 | 参数（`pt4_token`/`ptsigx`/`daid`）不可控，且无法验证；错了只能看到 `-3000` |
| 读浏览器 cookie 数据库（SQLite） | Windows 上要 DPAPI + AES-256-GCM；标准库没有 AES，违反零依赖硬约束 |
| Playwright / Selenium | 第三方依赖，违反硬约束；能力上和我们这 60 行 CDP 完全重叠 |
| 只靠 `--qr` 扫码 | 风控面最大，2026-10 起冷却频繁；降级为兜底 |
| 从 QQ 客户端内置浏览器抠 cookie | 依赖 QQ 安装位置与版本，稳定性最差 |

## 后果

**收益**：cookie 过期不再需要扫码；`login --browser` 一次之后 `refresh` 全自动；读/写路径的可用性从"看运气"变成"一条命令 + 一个可读的判定"。

**代价与已知边界**：

* `data/run/qzone-browser` 是一个真实浏览器 profile（首次约 27 MB），落在运行时目录，不进备份、不进仓；
* 每次续期要起一次浏览器（约 5–10 秒），比纯 HTTP 重；
* 服务端探测会多发 1–2 个请求；
* 首次仍需要一次人工登录（只发生在账号层面，不再发生在每次 cookie 过期）；
* `--no-verify` 只用于离线调试；生产关掉它等于把"死 cookie 当活凭据"的风险放回来；
* Edge/Chrome 只覆盖标准安装位置，非常规安装用 `--browser-path`；
* **仍然违反 QQ 用户协议**。这条路降低的是我们这一侧的异常特征，不是这件事的性质；风控依旧可能封号。

**顺带查清的两件事（2026-10-29 实测，影响 ADR-0004 的读路径）**：

1. **好友动态端点当前不可用**：同一份有效 cookie，`emotion_cgi_msglist_v6` 稳定 200（0 条说说），但 `feeds3_html_more` 会间歇性回 `-3000 need login`（几分钟前同一请求返回过 `code:0` + 312 KB 数据，随后持续 `-3000`），mobile 兜底稳定 `-4003`。已排除：`uin` 的 `o0` 前缀、`X-Requested-With`、`Referer`、`Origin`、`Set-Cookie` 轮换。结论是**平台侧会话/端点状态**，不是我们少带了什么。所以 `qzone fetch` 现在拿不到好友动态，`doctor` 会同时报 `probe.ok=true` 和 `feeds.ok=false`——这正是"cookie 没问题、端点坏了"的区分。
2. **她的账号当前 0 条说说**：`emotion_cgi_msglist_v6` 正常返回空列表，说明该端点的鉴权与请求都通。
3. **回退路径已落地**（同一份数据、同一套解析）：`QZONE_FRIEND_UINS`（.env，逗号分隔）里的好友逐个拉 `emotion_cgi_msglist_v6?uin=<friend>`，item 形状与聚合 feed 完全一致（`external_id/author_uin/content/media/published_at/url/comments`），直接喂现有 collector。实测 2 个好友 = 10 条 fetched / 5 条 new，`author_scope=friend`，`qzone show --friend` 可见。一条都没拿到才记失败，避免把"端点挂了"误判成"cookie 挂了"。
   **为什么不用 `owner_bindings` 自动推导**：那只有姐姐一个，而主人（`owner_uin` 留空时等于她的号）的动态也只能靠这张表。用显式列表 + `.env` 是为了不把真实 QQ 号写进受版本管理的 `config/*.toml`。
