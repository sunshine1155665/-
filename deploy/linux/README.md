# 在线层部署（Linux 云主机）

对应决策：[ADR-0012](../../docs/adr/0012-online-tier.md)。目标只有一句话：

> **让 QQ / Web / 记忆 24h 在线；TTS、唱歌、拍照留在家里。**

家里开机 → 全功能；家里没开 → 纯文字（这是设计，不是故障）。

## 0. 买什么

| 项 | 选择 | 备注 |
|---|---|---|
| 规格 | **2C2G** 起步 | 1G 能跑但没余量；bge 想本地跑要 4G |
| 系统 | **Debian 12**（或 Ubuntu 24.04） | Python 3.11 / 3.12；**不要宝塔/1Panel/WordPress** |
| 带宽 | 按使用流量 | 实测月流量 1–3GB |
| 入站 | 只开 22（限来源 IP） | 8765 / 3001 / 6099 一律不开，走 Tailscale |

### 如果试用页只有四个系统可选

阿里云部分活动页只给 **Windows Server / Alibaba Cloud Linux / Ubuntu 22.04 / CentOS 7.9**：

| 选项 | 结论 |
|---|---|
| **Ubuntu 22.04** | ✅ **选这个**。先补 Python 3.11（见下），其余照本流程走 |
| Alibaba Cloud Linux 3 | ⚠️ 能跑，但它是 dnf，本脚本的 apt 分支不适用，要手改 |
| Windows Server 2022 | ❌ 2C2G 跑不动，还有授权费 |
| CentOS 7.9 | ❌ 2024-06 已 EOL，Python 3.6 |

Ubuntu 22.04 上先补 3.11（它默认是 3.10，没有 `tomllib`）：

```bash
apt-get update && apt-get -y install software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update && apt-get -y install python3.11 python3.11-venv
```

`bootstrap.sh` 会自动挑 `python3.13/3.12/3.11/3` 里第一个 ≥3.11 的，不用改脚本。
**优先还是点 Ubuntu 的下拉看有没有 24.04**（3.12，省这一步）。

## 1. 准备代码包（本机 Windows）

```powershell
cd <PROJECT_ROOT>
python scripts/make_app.py --dest dist/fatfish-app --verify --skip-tests
Compress-Archive -Path dist\fatfish-app\* -DestinationPath dist\fatfish-app.zip -Force
scp dist\fatfish-app.zip root@<IP>:/tmp/
scp -r deploy root@<IP>:/opt/fatfish-deploy
```

`models/`、`vendor/`、`.venv-*` 不传。想把记忆带过去就单独 `scp data/fatfish.db` 到 `/opt/fatfish/data/`，否则首次 `fatfish init` 新建。

> `scripts/make_app.py` 的打包目录不含 `deploy/`，所以部署文件单独走一条 `scp`。

## 2. 一键准备（云主机）

```bash
bash /opt/fatfish-deploy/linux/bootstrap.sh /tmp/fatfish-app.zip
cp /opt/fatfish-deploy/linux/env.example /opt/fatfish/.env
# 填 FATFISH_OPENCODE_KEY / QQ_ACCESS_TOKEN / FATFISH_WEB_TOKEN
chown fatfish:fatfish /opt/fatfish/.env && chmod 600 /opt/fatfish/.env

sudo -u fatfish /opt/fatfish/.venv/bin/python -m fatfish doctor
```

## 3. NapCat

```bash
WEBUI_TOKEN=$(openssl rand -base64 18) bash /opt/fatfish-deploy/linux/napcat.sh
docker logs -f napcat     # 扫码
```

OneBot 用 SSH 隧道配（**不要**把 6099 写进安全组）：

```bash
ssh -L 6099:127.0.0.1:6099 root@<IP>
# 浏览器 http://127.0.0.1:6099 → 新建 WebSocket 服务器
#   host 127.0.0.1 / port 3001 / token = .env 的 QQ_ACCESS_TOKEN / 格式 array
```

## 4. 启动与外网访问

```bash
systemctl start fatfish-web fatfish-qq
curl -s http://127.0.0.1:8765/api/health

curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --hostname fatfish-cloud
tailscale serve --bg --https=9443 http://127.0.0.1:8765
tailscale serve status
```

手机（同一 tailnet）访问 `https://fatfish-cloud.<tailnet>.ts.net:9443/`。

## 5. 验收

```bash
systemctl is-active fatfish-web fatfish-qq
journalctl -u fatfish-web -n 50 --no-pager
free -h
docker logs napcat --tail 20
```

| 检查 | 期望 |
|---|---|
| `/api/health` | 200 |
| Web 登录 + 一轮聊天 | 有回复 |
| QQ 私聊 | 有文字回复 |
| 重启自愈 | `reboot` 后 2 分钟内回来 |
| 内存 | `free -h` used < 1.4G（2G 机器） |

## 6. 备份

```bash
cat >/etc/cron.d/fatfish-backup <<'EOF'
0 4 * * * fatfish cd /opt/fatfish && .venv/bin/python -m fatfish backup && find data/backups -name '*.db' -mtime +7 -delete
EOF
```

家里在线时建议反向拉取（由家里发起，云主机不持有家里的密钥）：

```bash
# 家里（Windows PowerShell / WSL）
scp root@fatfish-cloud:/opt/fatfish/data/backups/*.db <PROJECT_ROOT>/data/backups/
```

## 7. 降级矩阵

| 家里 GPU | 云端行为 |
|---|---|
| 在线 | 文字正常；贴纸 `sticker.embedding_backend` 改回 `http` 并指向家里 9881 后恢复语义选图 |
| 在线 | 语音：`tts.enabled=true` + `tts.base_url` 指家里 9880 后恢复 |
| 离线 | 纯文字；`sing` / `photo` 不可用 |
| 离线 | 贴纸只剩「关键词 + 意图」打分（余弦恒 0）：自发基本不出图，明确点图仍可用 |
| 离线 | 语音请求若 `tts.enabled=true` 会失败——**第一阶段保持 false** |

## 8. 掉线监管（强烈建议）

容器带 `--restart always`，但它只保证**进程**活着——账号被踢之后容器会一直挂在登录页反复取二维码
（2026-10-25 本机实测：每 2 分钟一次 `ErrCode:3`，持续 40 分钟），既没有用又增加异常特征。

`napcat_supervisor.py` 把两种离线分开：

| 情况 | 日志特征 | 处理 |
|---|---|---|
| 快速登录凭据仍有效 | `正在快速登录` 成功 | 指数退避重启容器：5 / 15 / 30 / 60 分钟，最多 4 次 |
| 凭据已失效 | `自动快速登录失败: 你的用户身份已失效` | **不重启**，等人工扫码 15 分钟，等不到就 `docker stop` 并退出 |

```bash
install -m 644 /opt/fatfish-deploy/linux/napcat-supervisor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now napcat-supervisor
systemctl status napcat-supervisor --no-pager
```

- 台账：`/opt/fatfish/data/logs/napcat-supervisor.jsonl`（每段在线时长）
- 控制台：`/opt/fatfish/data/logs/napcat-console.log`
- 退出码 2 = 需要人工扫码、3 = 重启用尽；unit 已用 `SuccessExitStatus=0 2 3` 把它们
  当成正常停手，**服务会停在 `inactive`**（不是 failed，也不会 60 秒后自动拉起）。
  **不要把 `Restart=always` 加回去**，否则就变成无限撞登录。
- 2026-10-31 事故：这条 unit 原来只写了 `SuccessExitStatus=0`，exit 2 被 `Restart=on-failure`
  当成失败，整夜每 20 分钟撞一轮登录（systemd 重启计数 59 次、一天 60+ 次登录尝试）。
  被踢后要重新扫码时，手动 `systemctl start napcat-supervisor`，15 分钟内扫完即可。
- 手机取码：RikkaHub 通过 MCP 调 `napcat_qr`（`napcat-qr-mcp.service` + tailscale 9451），
  标准流程见 [手机端 RikkaHub 取 NapCat 二维码标准流程](../../docs/runbooks/napcat-qr-phone.md)。

## 9. QQ 空间在云端（第二阶段）

第一阶段 `config/cloud.toml` 里 `qzone.enabled = false`。要开的话，先说清楚一件事：

> **云端只做「续期 + 采集」，不做「登录」。**

qzone 会拒绝 `--headless=new`（2026-10-29 实测），云主机上没有可交互的屏幕，所以第一次登录/彻底失效后的
重新登录，仍在**家里**用 `python -m fatfish qzone login --browser` 完成，然后把
`data/qzone_cookies.json` 拷过来。云端只是拿这份有效 cookie 定时续期。

```bash
# 1) 浏览器 + 虚拟显示（Debian 12）
apt-get -y install chromium xvfb
# Ubuntu 24.04 的 chromium 是 snap 包（不好用），改装官方 deb：
#   wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google.gpg
#   echo "deb [signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list
#   apt-get update && apt-get -y install google-chrome-stable xvfb

# 2) 从家里拷 cookie（和可选的浏览器 profile）
scp data/qzone_cookies.json root@<IP>:/opt/fatfish/data/
chown fatfish:fatfish /opt/fatfish/data/qzone_cookies.json

# 3) 打开采集（改部署副本，不是仓库里的文件）
sed -i '/^\[qzone\]/,/^\[/ s/^enabled = false/enabled = true/' /opt/fatfish/config/cloud.toml

# 4) 先手动跑一次，确认能读
sudo -u fatfish env DISPLAY=:99 xvfb-run -a /opt/fatfish/.venv/bin/python \
    -m fatfish --profile cloud qzone refresh --timeout 180

# 5) 定时续期（每天 06:10 / 18:10）
install -m 644 /opt/fatfish-deploy/linux/qzone-refresh.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now qzone-refresh.timer
```

验收：`journalctl -u qzone-refresh -n 30` 里出现 `"ok": true` 和 `"online_ok": true`；
`fatfish qzone fetch` 能入库（好友动态走 `QZONE_FRIEND_UINS` 的回退，见 ADR-0011）。

> 换 IP 之后 cookie 有可能被判失效（`-3000`）。这不是故障，按上面的方式在家里重新登一次再拷过来即可。

## 10. 能力层探针（家里在不在）

第二阶段打开语音之后，云上最怕的不是「家里关机」，而是「家里关机了云还不知道」——每句话都要等到
`tts.timeout_seconds`（默认 30 秒）才失败，体感就是「半天不回」。

`fatfish/remote.py` 给了个带缓存的开关：

```toml
# /opt/fatfish/config/cloud.toml
[remote]
enabled = true
home_url = "http://fatfish-home:9880"   # Tailscale MagicDNS 名或 100.x 地址都行
health_ttl_seconds = 30
timeout_seconds = 3
```

- 探测的是 `GET <home_url>/health`（TTS 软服务在模型未加载时也会回 200，适合当"进程还在"的信号）
- 结果缓存 30 秒：一次失败只影响一个窗口，不会每句都等超时
- 不在线时 `synthesize` 立刻返回 `home_offline`，上层照常发文字
- **已合成的缓存音频仍然可发**（缓存命中在探测之前）——家里关机时她还能说出以前说过的话
- 多个能力可以分开配：`[remote.endpoints] tts = "..."; embedding = "..."`，**全部**通才算在线

```bash
sudo -u fatfish /opt/fatfish/.venv/bin/python -m fatfish --profile cloud remote status --force
```

```json
{
  "home_online": true,
  "ttl_seconds": 30,
  "endpoints": { "tts": { "url": "http://fatfish-home:9880/health", "ok": true, "age_seconds": 0.0, "error": null } }
}
```


## 11. 到期 / 拆机

```bash
# 1. 拉走数据
scp -r root@<IP>:/opt/fatfish/data/backups ./cloud-backups/
# 2. 云控制台「释放实例」（按量付费：停机 ≠ 不收费）
```

## 文件

| 文件 | 作用 |
|---|---|
| `bootstrap.sh` | 系统包 / swap / 用户 / 解包 / venv / unit |
| `napcat.sh` | Docker 版 NapCat（host 网络，3001） |
| `fatfish-web.service` | Web（127.0.0.1:8765，token 走 env） |
| `fatfish-qq.service` | QQ 适配器（连 127.0.0.1:3001） |
| `napcat-supervisor.service` | 掉线监管：退避重启 / 凭据失效停手（容器模式） |
| `qzone-refresh.{service,timer}` | QZone 免扫码续期（xvfb + 有头 Chromium） |
| `env.example` | `.env` 模板 |

## 12. 代码同步（覆盖式 + 自动回滚）

改完代码要更新云端时，别再用 `bootstrap.sh` 重装（那会重建 venv、覆盖更多东西）。
用 `scripts/deploy/sync_cloud.sh`：**先备份 → 覆盖包内容 → 重启 → 健康检查 → 不行就自己滚回去**。

### 12.1 流程（家里发起）

```bash
# 1) 本机打包（manifest.json 的 built_at 就是这次部署的版本指纹）
python scripts/make_app.py --dest dist/fatfish-app --no-zip
tar -C dist -czf dist/fatfish-app-sync.tar.gz fatfish-app

# 2) 传上去（脚本放 /opt/fatfish-deploy/linux/ 复用）
scp dist/fatfish-app-sync.tar.gz fatfish-cloud:/tmp/
scp scripts/deploy/sync_cloud.sh fatfish-cloud:/opt/fatfish-deploy/linux/

# 3) 先看会改什么（不写盘、不重启）
ssh fatfish-cloud 'sh /opt/fatfish-deploy/linux/sync_cloud.sh --pkg /tmp/fatfish-app-sync.tar.gz --dry-run'

# 4) 真跑
ssh fatfish-cloud 'sh /opt/fatfish-deploy/linux/sync_cloud.sh --pkg /tmp/fatfish-app-sync.tar.gz --label 20261030'
```

最后一行是结论：`RESULT: OK` / `NOCHANGE` / `ROLLED_BACK` / `ROLLBACK_FAILED`，退出码 0/0/2/3（1 = 预检失败，线上零改动）。

### 12.2 覆盖与排除清单

覆盖范围 = 包里有的文件（`fatfish/ web/ config/ docs/ skills/ tests/ scripts/` + 根文件）。
**永不覆盖、永不删除**：

| 路径 | 原因 |
|---|---|
| `data/` | 记忆与凭据，最不能丢 |
| `.venv/` | 云端独立虚拟环境，包里没有 |
| `.env` | 线上密钥 |
| `config/cloud.local.toml` | 线上私有覆盖配置 |

其余「上一版 manifest 里有、新版没有」的文件会被删掉（`tests/` 里被移除的用例就属于这种），
删除只按 manifest 清单执行，不会扫目录乱删。备份同样排除 `data/`、`.venv/`、`__pycache__`。

### 12.3 备份与回滚

- 备份：`/opt/fatfish-backups/fatfish-<时间戳>-<label>.tar.gz`，默认保留最近 **5** 份（`--keep N` 可调）；
- 回滚 = 解开最近一份备份 → 覆盖回去 → 删掉这一版新增的文件 → 重启 → 再等健康；
- 回滚失败（`RESULT: ROLLBACK_FAILED`，退出码 3）时日志会直接打印手工恢复命令：

```bash
tar -C /opt/fatfish -xzf /opt/fatfish-backups/fatfish-<时间戳>-<label>.tar.gz
systemctl restart fatfish-qq
journalctl -u fatfish-qq -n 80 --no-pager
```

### 12.4 幂等

同一份包跑第二次：逐文件 sha256 与运行目录一致且没有待清理文件 → 直接 `RESULT: NOCHANGE`，
**不备份、不覆盖、不重启**（`systemctl show -p ActiveEnterTimestamp` 不变）。
所以定时任务里反复跑是安全的。

### 12.5 已知限制

- **健康检查只证明"进程活着 + 新代码能 import"**，不证明 QQ 真的连上。`fatfish-qq` 连不上 NapCat 时
  `systemctl is-active` 仍是 active（它自己重试），日志里会是 `cannot connect to OneBot at ws://127.0.0.1:3001`。
  真要验到端到端，另跑 `sudo -u fatfish .venv/bin/python -m fatfish --profile cloud doctor`。
- `config/*.toml`（除 `cloud.local.toml`）会被仓库版本覆盖 —— 云端手改的部分记得搬进 `cloud.local.toml`。
- 包里的 `data/` 是空目录骨架（`.gitkeep`），同步时整目录排除，不会在云端造出多余结构。
- 只清理「上一版 manifest 里列过、新版没有」的文件。**manifest 之外的历史文件不会被动**——
  例如早先 Windows 上传留下的 8 个乱码名 `.bat` 副本仍在 `/opt/fatfish` 根下（Linux 上跑不到，
  确认无用可手工 `rm`）。
- 破坏性实验（故意发坏包）只在隔离目录做，见 12.6；**不要拿云上真目录试**。

### 12.6 自测

```bash
sh scripts/deploy/test_sync_cloud.sh        # Windows 上：MF_PYTHON=python sh scripts/deploy/test_sync_cloud.sh
```

在临时目录里造一棵假运行目录 + 假包，覆盖四个场景：正常同步、重复同步（幂等）、
坏包自动回滚（服务恢复健康）、缺 manifest 的残缺包被预检拦下。会输出 `SIM_RESULT: OK`。

