# CONTRIBUTING —— 参与开发约定

欢迎参与「鲸鱼娘·大肥鱼」。本文件是给改代码的人（人或 AI 助手）的硬约定；部署请看 [AGENTS.md](AGENTS.md)，架构与决策见 `docs/overview/` 与 `docs/adr/`。

## 硬约束

1. **应用本体零第三方依赖**：`fatfish/` 只用 Python 标准库 + SQLite。需要新依赖时先开 issue/ADR 讨论并落 `requirements.txt` 或引擎 venv，不要直接 `pip install` 进应用本体。
2. **不要碰本地资产**：`data/`（真实库与音频）、`models/`、`vendor/`、`.venv-*/`。测试一律用临时目录，不读真实 `data/fatfish.db`。
3. **schema 变更流程**：改 `fatfish/schema.sql` 时必须同步——
   - `fatfish/db.py` 的 `SCHEMA_VERSION` +1；
   - 在 `Database` 的迁移逻辑里保证旧库可升级且幂等；
   - 更新 `tests/test_p0.py`（及迁移相关测试）的版本断言；
   - 提供回填/兼容读路径。
4. **测试纪律**（提交前必须全绿）：

   ```bash
   python -m unittest discover -s tests -p "test_*.py"
   ```

   全量离线、不联网、不读真实库，串行约 6–7 分钟。新增功能必须带测试；先写/改测试，再做实现。
5. **文档**：`docs/` 下文档带 YAML front matter（`title/type/status/tags/updated`）；增删文档后跑：

   ```bash
   python scripts/docs_index.py --write   # 重建索引
   python scripts/docs_index.py --check   # 必须绿
   ```

   里程碑只更新总览与对应分支文档，不要新开散页。
6. **秘密**：`.env`、cookie、token、真实聊天内容、真实 QQ 号绝不入库、不进日志、**不进 issue / PR / 截图**（用 `<uin>`、`<PROJECT_ROOT>` 占位）。
7. **破坏性命令需人工确认**：删除/移动资产、`git reset --hard`、重写数据库之前必须先征得维护者同意。

## GPU / 显存约定（8 GB 卡）

TTS(9880) / 唱歌(SoulX) / ComfyUI(8188) / 向量(9881) **不能同时驻留显存**，租约文件是 `data/run/vram.lock`，产品侧与脚本共用同一把锁。

1. 任何会加载模型的重活之前先过闸门：

   ```bash
   python scripts/preflight.py --for tts     # 或 --for svs；不够时加 --unload-tts
   ```

   退出码 0 = 可以跑；非 0 = 先别跑。
2. **唱歌 ↔ TTS 串行**：唱歌起跑前卸 TTS 腾显存；唱歌一结束（成功、失败、挂起）立刻把资源还给 TTS。产品代码里这段交接在 `fatfish/sing/service.py`（`finally` 里回载）。
3. 渲染、评审、全量测试一律**串行**，不并发。
4. TTS 常驻不会自己缩（分配器只涨不还）：怀疑内存只增不减时走控制口重启服务（`POST http://127.0.0.1:9882/services/tts/stop` 再 start；同一服务同时只允许一个 job，连发 409 `busy` 要等）。

## 工作流

1. 从 `main` 拉分支或 worktree（推荐 worktree，避免和主工作区抢文件）：

   ```bash
   git worktree add ../fatfish-task-<name> -b task/<name>
   ```

2. 动手前在 issue/任务描述里写清：目标、涉及文件、验收命令、禁止事项。
3. 先写/改测试，再做实现；提交信息用 `type(scope): summary`（如 `fix(memory): ...`、`feat(sing): ...`）。
4. 完成后在任务描述里写：改了什么、如何验证、已知风险。
5. **PR 流程**：`task/<名字>` 分支 → PR → 至少 1 人 approve → 合并；`main` 设分支保护，不直接 push。PR 描述附验收命令的输出结论。

## 代码地图（速查）

| 层 | 位置 |
|---|---|
| 单轮编排 | `fatfish/runtime.py`、`fatfish/context.py`、`fatfish/llm.py` |
| 记忆 | `fatfish/memory/`（`service.py` 检索、`details.py` 细节、`events.py` 事件、`gate.py` 召回闸门） |
| 语音 | `fatfish/tts/`（引擎在 `.venv-indextts`，9880） |
| 唱歌 | `soulx/`（SoulX-Singer 子进程链路）+ `fatfish/sing/service.py` |
| 平台 | `fatfish/platforms/`、`fatfish/bilibili/`、`fatfish/qzone/`、`fatfish/web/server.py` |
| 数据 | SQLite schema（`fatfish/schema.sql` + 幂等迁移） |

## 提交前自查

- [ ] 全量测试绿（串行跑完）
- [ ] 新功能带测试；测试不联网、不碰真实 `data/`
- [ ] schema 变更走了第 3 条流程
- [ ] 文档改动后 `docs_index.py --check` 绿
- [ ] diff 里没有秘密、真实 QQ 号、本机绝对路径
