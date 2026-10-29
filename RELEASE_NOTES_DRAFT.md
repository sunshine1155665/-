# RELEASE_NOTES_DRAFT —— 公开发布树组装报告（worker-release，2026-10-30）

> 面向 boss 的验收稿。任务 T-0059；方案见主仓 `github-publish-plan-2026-10-30.md`。
> 本文件是组装元文档，正式发布前可删除或改写为正式 Release Notes。

## 1. 交付物概览

| 项 | 值 |
|---|---|
| 目录 | `D:/fatfish-release`（全新 git 仓库，单初始提交，`main` 分支） |
| 入库文件数 | 314 个（git ls-files；工作区 ≈5.3 MB；**git size-pack 1.45 MiB**） |
| 全量测试 | 1,945 条 OK（skipped=3，串行 ~6.5 分钟，cwd=发布树） |
| 主树改动 | **零**（只读复制；主仓未动任何文件） |

## 2. 组装动作清单

- **快照复制**：主树工作区（含未跟踪干净产品件）→ 排除 .git、data/、models/、vendor/、checkpoints/、dist/、.venv-*/、.agent-board/、.zcode/、.workbuddy/、staging/、__pycache__、.idea/、.vscode/、.env、headers.txt、served.html、*.log、memory_v6_review_*、scripts/send_continue.*、node_modules、.opencode/、opencode.jsonc。
- **删除研发脚手架（77 项）**：根目录 52 个 `staging_*.py` + `staging_sticker_spec.txt`；`staging_soulx/` 32 个研发脚本（保留 song_v2 的 import 闭包 14 文件 + 评测伴侣 melody_eval.py，共 **15 个 → `soulx/`**，song.py 经 AST 实算不在闭包内，未保留）；`scripts/` 14 个一次性脚本（resing_world、sing_demo、verify_sing、tag_stickers、memory_recall_eval、memory_report、migrate_embedding_blob、import_llm_details、export_agent_pack、run_injection_eval、security_smoke、night_test、dev_web.bat、make_copy.py）；`tasks/`、`skills/`、`scripts/agent_board/`、`README_APP.md`、旧 `AGENTS.md`、根目录研发报告 md ×3（含发布方案本身）；连带删 `tests/test_night_test.py`、`config/night-test.toml`。
- **保留被引用脚本**：make_app.py（test_packaging/test_bili_login_ui 引用）、text2melody.py（唱歌运行时 import）、finalize_song.py（test_finalize_song 引用）、preflight/serve_audio/upload_audio/各服务脚本（测试或 start_all 引用）。
- **联动代码编辑**：`scripts/make_app.py`（PACKAGE_DIRS 去 skills；ROOT_FILES/CLEAN_ROOT_FILES 以 README.md/AGENTS.md 取代 README_APP.md；SCRIPT_FILES 换成 install_assets.py/fetch_stickers.py）；`tests/test_packaging.py`（REQUIRED_FILES 同步）。
- **接线 staging_soulx → soulx**：`config/low.toml [sing].script`、`fatfish/sing/service.py`（docstring+报错文案）、`soulx/song_v2.py` 与 `soulx/sing_batch.py` 的 `sys.path.insert`、6 个测试文件的 sys.path/docstring、`scripts/finalize_song.py` 命令模板。全树 `staging_soulx` 残留 = 0（ADR 历史叙述保留 2 处）。
- **资产播种**：`assets/voice/cand_home_up1.wav`（255 KB 自制录音）、`assets/song/prompt_v6.json`、`assets/song/persona_song.json`；新增 `scripts/install_assets.py`（零依赖、幂等、--dry-run）与 `scripts/fetch_stickers.py`（URL 待补，见 §6）。
- **新增文档**：`README.md`（重写）、`AGENTS.md`（AI 部署手册，给老师同学的 AI 助手读）、`CONTRIBUTING.md`（由主仓 AGENTS.md 通用化）、`THIRD_PARTY_NOTICES.md`、`config/example.toml`。

## 3. 脱敏执行清单（file:line 级）

| 类别 | 位置 | 处理 |
|---|---|---|
| 机器人 QQ | docs/branches/platforms.md:47、scripts/napcat_supervisor.py:30-31 | → `<uin>` |
| 机器人 QQ（测试） | tests/test_napcat_supervisor.py:163、tests/test_qzone_browser_login.py:46 | → 合成值 9999000003；掩码断言 :624 同步 → `999****0003` |
| 主人 QQ | tests/test_memory_pollution.py:34 | → `qq:private:9999000001` |
| 好友 QQ | tests/test_friend_slot.py:34 | → `qq:private:9999000002`（memory_recall_eval.py 已删，跳过） |
| Tailscale IP | scripts/serve_audio.py:4 | → `100.x.y.z` |
| 本机路径 | docs/runbooks/startup.md:18-21,39-43、deploy/linux/README.md:43,123、docs/branches/singing.md:61-62、singing-advice.md:165,239、tests/test_qzone_browser_login.py:311,313、scripts/finalize_song.py:37 | → `<PROJECT_ROOT>`/相对路径/合成路径 |
| 凭据状态 | docs/runbooks/startup.md:21 | 删掩码账号与 quick-login 描述 |
| 本机 ComfyUI 安装目录名 | config/low|standard|remote.toml（photo 四件套+注释，共 13 处）、docs/branches/media.md:28,31、docs/adr/0012:88、docs/overview/project-status.md:122,296,297,553 | → 置空/通用描述；photo 四件套全部置空、`comfyui_autostart=false`；standard.toml ref_audio_path/prompt_text 置空（test_config_profiles 快照同步） |
| 真实库统计/画像 | docs/overview/project-status.md:21,23,43,176,209,569,600、docs/branches/memory.md:114-121 | → 通用化/合成（数字改「量级见本机库」） |
| 真实聊天原话 | docs/branches/memory.md:37-41,192-223、docs/adr/0003:314,330-341,373-374、fatfish/context.py:1220、fatfish/memory/details.py:390、fatfish/memory/service.py:793、tests（test_past_timeline/test_memory_recall/test_details/test_memory_pollution/test_memory_entities/test_nightly_archive/test_recall_prefilter/test_voice_tone） | 全部替换为合成例句（「赶稿很累」「跟主人哥哥聊了中午吃什么」「跟我说声晚安，明天见啦」；测试 seed 与断言成对替换，语义不变） |
| 已删工具引用 | docs/branches/ops.md（导出/回导/BLOB 迁移/夜测四节）、memory.md、adr/0002/0003/0005、security.md:47、singing.md、singing-advice.md、project-status.md | → 标注「研发期内部工具，未随发布版分发」或改指 `soulx/` |

**全树复扫结论（发布树内，排除 .git/__pycache__）**：机器人 QQ 前缀 0 命中；主人/好友 QQ 前缀 0 命中（bilibili 公开算法常量 2251799813685247 除外）；Tailscale 网段 0 命中；本机 ComfyUI 目录名 0 命中；主仓目录名 0 命中；`E:\`、`D:\develop` 0 命中；真实聊天短语 4 条（原文不列）0 命中；`363****` 0 命中。

## 4. 测试结果

- 全量：**1,945 tests, OK, skipped=3**（串行 380s，主仓 .venv-indextts 的 Python 3.11，cwd=发布树）。
- 首轮 1,880 条跑出 2 个失败，均已修复：
  1. `tests/test_melody.py` import 已删的 `melody_eval` → 该模块是 154 行纯符号评测件（melody_v2 伴侣），收编进 `soulx/`（song_v2 闭包 14 + melody_eval = 15 文件）；
  2. `tests/test_qzone_browser_login.py:624` 掩码断言含旧真实 QQ → 随合成值同步。
- 连带处置：`tests/test_staging_indextts_save.py` 原 import 已删研发脚本的 `_save_pcm_wav`；产品侧同函数在 `scripts/indextts_server.py`，测试重写为钉产品实现并改名 `tests/test_indextts_save.py`（2 用例，绿）。
- 随研发脚本删除的测试：`tests/test_night_test.py`（驱动脚本已删）。

## 5. 冒烟

- `python -m fatfish --help`：26 个子命令正常。
- `python scripts/install_assets.py --dry-run`：3 个文件计划正确；真跑后 `data/` 就绪。
- `python scripts/make_app.py --dest <tmp> --no-zip`：打包成功（见下方数字）。
- `python scripts/docs_index.py --write && --check`：28 docs，绿。

## 6. 遗留 TODO(boss)（URL/信息占位清单）

1. 表情包库来源 URL + 许可证确认 → `scripts/fetch_stickers.py` 的 `STICKER_URL`；
2. anima 鲸鱼娘 LoRA 来源 URL → README「ComfyUI」节、THIRD_PARTY_NOTICES；
3. IndexTTS-2.5 / bge-base-zh-v1.5 的 HF/ModelScope 页面链接核实 → README、THIRD_PARTY_NOTICES（fetch_models.py 落地时同步）；
4. SoulX-Singer 权重落位子目录核实 → README/AGENTS.md；
5. vendor 快照 revision（index-tts-main、SoulX-Singer）→ THIRD_PARTY_NOTICES；
6. NapCat、ComfyUI、anima-turbo 许可证核实 → THIRD_PARTY_NOTICES（index-tts=bilibili 协议、SoulX-Singer=Apache-2.0、pypinyin=MIT、g2pM=Apache-2.0 已按 vendor/元数据实读登记）；
7. GitHub 组织/仓库名与 `<REPO_URL>`（README 快速开始里是占位符）。

## 7. 已知风险

- `config/*.toml` 的 `[photo]` 四件套为空且 `comfyui_autostart=false`：首次点拍照会得到连接失败提示，属预期降级（需按 `config/example.toml` 填 `config.local.toml`）。
- 测试计数 1,945 比主仓基线 1,007 多：主基线数字是 2026-10-25 的旧值，本树按当前代码全量实跑。
- 发布树 `data/` 已被 install_assets 播种（gitignore 内，不入库）；如需「空 data」验收，删掉 `data/` 后重跑 `install_assets.py --dry-run` 应显示 3 个 copy。
- 主工作树在途任务（T-0056/T-0057）仍在改 config/low.toml、platforms.md 等：**main 收敛后应重跑一遍本组装流程**再定稿（方案 §四 时序注记）。

## 8. 主对话验收修订（2026-10-30）

组装后的独立验收发现并修复 4 类残留（修复后全树复扫通过、test_past_timeline 20 例复跑 OK）：

1. `docs/overview/project-status.md` 整篇移除——内部进度日志、真实用户反馈密度高，公开价值低；`docs/README.md` 手工行与 `activity.md` 上级链接改指 `architecture.md`，索引重建后 `--check` 绿（27 docs）。
2. 用户听感直接引语转述（去引号）：singing.md 3 处、roadmap.md 2 处（定版验收、v5 听感、八度高潮否决、句型重复判定）。
3. 示例时间线的真实日期/时刻整体位移为合成值（示例日取 2026-10-20 与五个新时刻，旧值不列）：`docs/branches/memory.md` 示例块与 `tests/test_past_timeline.py` 全部 seed/断言成对修改，真实时刻不再出现在发布树；`fatfish/context.py` docstring 示例同步。
4. 本报告第 3 节复扫结论中列举的 4 条真实短语原文改为「原文不列」。

以上修订已 amend 进同一初始提交。

## 9. v2 重同步（2026-11-03，sync worker）

主仓 9b25878..bc8ea0c（18 个提交 / 78 文件）收敛后的重跑。本次**用可重跑同步器**完成：
`fatfish-lab/scripts/build_release_tree.py`（分类引擎：IN_SYNC / SYNC / PAIRED_SYNC /
AUTO_MERGE / KEEP_HANDEDITED / MERGE；三方合并 + 行级替换对自动脱敏 + 敏感串门禁）。

| 项 | 值 |
|---|---|
| 同步写入 | 98 文件（SYNC 44 + PAIRED 8 + AUTO_MERGE 9 + ADD 37）；web/ 有人物卡等快照后更新一并带入 |
| 人工合并 | 6 文件（memory/ops/platforms/startup md、context.py、make_app.py——冲突点均为「组装脱敏 vs 主仓增量」交叠） |
| 测试快照修正 | `test_packaging.py` OPERATIONAL_SCRIPTS 改为发布树实际分发的运维脚本集；`test_config_profiles.py` cloud 覆盖快照更新为 16 键（sing/photo mode=remote 编排 + example-tailnet 占位端点） |
| 新增能力 | 多会话漫游 v14（`fatfish/roam/`、ADR-0013/0014）、talk_to / read_chat / watch_video 确定性旁路、entity_edges 1 跳关联召回、QQ 历史/图片直读、B 站看视频、NapCat 扫码 MCP（`napcat_qr_mcp.py`）、表情包 hash 检索提质 |
| schema | v13 → v14（`Database.migrate()` 幂等升级，旧库可升） |
| 测试 | 2,538 条 OK（skipped=3，串行 ~7.3 分钟，cwd=发布树） |

## 10. v2 验证（2026-11-03）

- 全量测试：**2,538 条 OK**（skipped=3）；首轮 3 失败已修（见 §9 测试快照修正）。
- 敏感串门禁：QQ 原值 / Tailscale 真实 IP / 主仓路径 / .workbuddy 全树 grep —— **0 新增命中**（基线 19 处均为通用路径、版本号、测试夹具等已核验良性项）；重同步过程实际抓到并修复 1 处真实 tailnet IP（云端复盘文档带入，已置换为 `100.x.y.z` 占位）。
- 组装知识沉淀：同步器与脱敏规则全部脚本化于主仓 `scripts/build_release_tree.py`，敏感值只存本地 `staging/release-sync/scrub.local.toml`（不入库）。

### 10.1 文档同步补充（2026-11-03 晚）

- 全景文档 `docs/overview/project-map.md`（存放规范/功能图/节点图/代码流程）与 architecture.md v14 校准一并入树。
- `scripts/napcat_qr_setup.py` 默认 URL 中的真实 tailnet 域名已置换为 `example-tailnet` 占位（门禁在文档复查中捕获）。
- `docs/README.md` 索引重生成（30 docs）。

### 10.2 CI（2026-11-03）

- 新增 `.github/workflows/ci.yml`：push/PR 自动跑全量离线测试（2,538 条）+ `fatfish --help` 冒烟。
- matrix：windows-latest 必过（开发/验收环境）；ubuntu-latest 为观察腿（continue-on-error，Linux 套件未验证，先攒信号）。
- Python 3.11（README 钉位）；pip 装 requirements.txt 四个可选包（纯 Python，任意平台可装）。
- 建仓推送后即生效；配套建议：main 设分支保护（禁直推 + 要求 1 approve，见 CONTRIBUTING.md）。
