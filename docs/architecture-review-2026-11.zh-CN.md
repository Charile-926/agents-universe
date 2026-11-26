# 架构审查报告（2026-11）

审查对象：本仓库 `agents-universe`（上游 `agents-universe/agents-universe` 的精简分支，Apache-2.0）。
范围：仓库全部包（`agent-core`、`api`、`web`）、内容层（`agents/`、`workflows/`、`knowledge/`）、构建与部署（`Dockerfile`、compose、nginx、脚本）、CI 与文档。
方法：逐包读代码 → 跑测试与构建 → 用"来源可查、许可证明确、维护活跃"三条标准检索同类开源项目 → 能安全替换/借鉴的当场落地，不能的写清理由与替换路径。

---

## 一、结论摘要

1. **架构本身是健康的**：三层包边界（纯库 / HTTP 应用 / SPA）清晰，`ToolContext` 注入让 agent-core 与数据库、HTTP 解耦；知识、技能、工作流都是文件，可审计。没有发现需要推倒重来的结构问题。
2. **真正的问题集中在"删减后的残留"**：这个分支移除了 OAuth SSO、界面多语言与 workspace 层，但依赖、配置项、注释、环境变量文档、安全策略文档、知识库文档、前端文案都留着上一版的东西。这类残留比代码 bug 更危险——它会让下一个维护者（或智能体）按错误的前提改代码。
3. **构建链路有三个实质缺陷**：前端构建不读锁文件（`rm -f package-lock.json && npm install`）、Dockerfile 默认 target 是 vite 开发服务器、镜像构建全局关闭 TLS 校验。三者都已修正。
4. **供应链上唯一"来源不透明"的资产是预编译的 tree-sitter 语法**（9.7 MB `.so`）。已补 `SHA256SUMS`、来源说明，并把下载顺序改为官方源优先。
5. **自研组件整体合理**，只有一个值得立即借鉴：cron 求值。已引入 croniter 做差分测试（不增加运行时依赖）。其余（调度器、沙箱、压缩、记忆）评估后保留，理由与替换路径见第五节。

## 二、架构评价

**做得好的地方**

- `agent-core` 不 import `api`：所有 IO（文件、数据库、用户令牌、取消信号）通过 `ToolContext` 传入。这让核心可被别的服务复用，也让工具测试不需要起数据库。
- 迁移链是"被测试的契约"：`api` 测试每次会话跑真实 alembic 链（SQLite），CI 另跑 PostgreSQL。四方言兼容不是口头约定。
- 会话是 Redis + 不透明 Cookie，而不是 JWT：吊销即时、无签名密钥轮换问题；用户名等敏感字段在库里只有 HMAC 摘要 + Fernet 密文。
- 前端单一主题（白底黑字、无渐变阴影）、单一文案目录：视觉与文案都不会发散。
- 部署脚本把"备份 → 构建 → 健康检查 → 回滚"做成一条命令，且明确写清 SQLite 回滚必须连库文件一起恢复。

**结构性问题（本次记录，未重构）**

| 问题 | 证据 | 影响 |
|---|---|---|
| 巨型模块 | `services/agent_turn.py` 2999 行、`agent_core/agent.py` 2364 行、`styles/layout.css` 5562 行、`TokenConfigModal.vue` 1469 行 | 改动冲突面大、review 成本高、单测只能覆盖片段 |
| 调度器绑定单进程 | `services/scheduler.py` 在 API 进程内跑 asyncio 循环，靠 DB 行抢占 | 多副本会重复触发；水平扩展必须先外置调度 |
| 沙箱是"防误操作"级 | 审计钩子 + 路径白名单（`sandbox_guard/sitecustomize.py`） | 不能作为多租户安全边界 |
| 关键词检索而非向量 | `knowledge/index.py` + `scorer.py`，无 embedding | 同义改写召回弱；换来零外部依赖 |
| 发布功能只剩模型 | `models/publish.py` 存在，无 router、无前端入口 | 能力清单与实际不符（已在 README 标注） |
| lint 未纳门禁 | `[tool.ruff]` 有配置，CI 不跑；实测 1022 条告警 | 风格漂移；贸然开启会红一片，需先分批清理 |

## 三、发现与处理

| # | 严重度 | 位置 | 问题 | 处理 |
|---|---|---|---|---|
| F1 | 高 | `pyproject.toml`、`config.py`、`redis_client.py`、`.env.example`、`SECURITY.md`、`CLAUDE.md`、`nginx-combined.conf`、`main.py` | OAuth 移除后的死代码与错误文档：`authlib`、`python-jose` 已无任何引用；8 个 `oauth_*` 配置项；3 个 OAuth Redis 辅助函数；nginx 里还有 Casdoor :8003 反代；安全文档仍写 JWT/OAuth | 删除依赖、配置、辅助函数、Casdoor server 块；重写 `SECURITY.md`；修正注释与文档 |
| F2 | 高 | `Dockerfile`（web-build / web-dev） | `rm -f package-lock.json && npm install`：构建不读锁文件，依赖版本随上游漂移，且与 CI 的 `npm ci` 行为不一致 | 锁文件 110 条 npmmirror URL 归一为 npmjs；两个阶段都改 `npm ci` + `NPM_REGISTRY` 重写 |
| F3 | 高 | `Dockerfile` 末尾 | 最后一个 stage 是 `web-dev`，裸 `docker build .` 产出 vite 开发服务器（历史事故点） | 把 `web-dev` 移到 `final` 之前；`final` 成为默认 target，脚本仍显式 `--target final` 双保险 |
| F4 | 中 | `Dockerfile` | apt `Verify-Peer false` 与 `curl -k` 无条件生效，等于所有构建都关校验 | 改为按 `http_proxy` 构建参数或 `INSECURE_TLS=true` 才关闭；默认校验开启 |
| F5 | 中 | `docker/ts-grammars/*.so`、`scripts/fetch_ts_grammars.py` | 9.7 MB 二进制入库但无校验清单；manifest 优先走第三方代理 | 生成 `SHA256SUMS` + `README.md`（版本、来源、校验方式）；脚本改为官方 URL 优先、镜像兜底；构建时 `sha256sum -c` |
| F6 | 中 | `knowledge/technical/db-schema.md` | 描述已不存在的表（`users`、`oauth_sessions`、`jwt_sessions`、`workspaces`、`workspace_members`）与不存在的 `embedding` 列；这份文档会被索引进智能体上下文 | 重写为"方言映射 + 迁移规则 + 表职责清单"，并声明源码为唯一权威 |
| F7 | 低 | `CLAUDE.md` | 首行是错位的变更日志；MSSQL/`UNIQUEIDENTIFIER`/OAuth 陈述与代码不符；`complexity.py` 被标为 legacy 但 `agent_turn.py` 正在用 | 重写为简洁的稳定约定清单 |
| F8 | 低 | `packages/web/src/i18n/locales/en-US.ts` | 961 行英文文案在"移除多语言"后无人引用（`i18n/index.ts` 只加载 zh-CN） | 删除；`AppLocale` 类型保留（`category_i18n` 数据仍在用） |
| F9 | 低 | `packages/api/tests/conftest.py` | `mkdir(parents=True)` 不幂等：一次被中断的测试会让后续每次会话都 `WinError 183` 全红 | 改 `exist_ok=True` |
| F10 | 低 | `entrypoint`、compose、`deploy_server.sh` 注释 | 残留 8003（Casdoor）、`/auth` 代理、`--target final 不能省`等过时说明 | 逐条修正 |
| F11 | 中 | `README.md` | 描述的功能与当前分支不符（发布页、Tour、i18n、docker target 陷阱） | 重写（见交付物） |
| F12 | 低 | `.env.example` | `osemgrep` 笔误；OAuth 段落与 `OAUTH_SECURE_COOKIE` 仍在 | 修正为 `so semgrep`；删除 OAuth 段；改为 `COOKIE_SECURE_OVERRIDE` |
| F13 | 低 | `packages/api/tests/test_user_isolation.py` | `test_distill_sources_only_own_conversations` 断言 `user-a` 恰好 1 条可炼化会话；同一会话库里其它用例也会给 `user-a` 建会话，整套跑必红（单跑通过）。已用 `HEAD` 的干净 worktree 复现，确认与本次改动无关 | 改为断言「自己那条可见」并按 id 取工具调用数，与库中其它数据解耦 |

## 四、本次落地的改动

**删除（死代码 / 错误配置）**
`authlib`、`python-jose[cryptography]`、`config.oauth_*` 8 项与 `oauth_state_ttl`、`redis_client` 的 3 个 OAuth 函数、`en-US.ts`、nginx Casdoor server 块。

**构建与供应链**
Dockerfile 两个 npm 阶段改 `npm ci`；stage 调序；TLS 校验默认开启；`package-lock.json` 解析源归一；`docker/ts-grammars/` 增 `SHA256SUMS` 与来源说明、构建期校验；语法下载脚本官方源优先并自动重写校验清单。

**测试**
新增 `packages/agent-core/tests/test_cron_parity.py`：13 条表达式 × 2 个时区 × 6 次触发与 croniter 逐次比对，另断言非法表达式双方都拒绝；`croniter` 只进 `[test]` extra。`conftest.py` 夹具幂等化。修正 `test_user_isolation.py` 里与执行顺序耦合的炼化用例（F13）。

**文档**
重写 `README.md`（架构、快速开始、配置、部署、边界、开源方案对比）；重写 `SECURITY.md`（真实安全模型与部署基线）；重写 `CLAUDE.md`（稳定约定，去掉过时事实）；修正 `CONTRIBUTING.md` 的硬性准则（`String(36)` + `uuid4()`、`npm ci`）；修正 `docs/agents-universe-implementation-guide.zh-CN.md` 的 JWT/OAuth 描述；重写 `knowledge/technical/db-schema.md`。

## 五、自研组件 vs 成熟开源方案

筛选标准：许可证 MIT / Apache-2.0 / BSD；近 12 个月有提交或发版；抽象不冲突（不需要重写调用方）。

| 组件 | 候选 | 许可证 / 活跃度 | 决策与理由 |
|---|---|---|---|
| `scheduling/cron.py`（262 行，标准库） | croniter 7.0.0（Pallets 维护） | MIT，2026-11 发版 | **采用为测试基准**。运行时保留自研：无新依赖、DST 语义已被 `test_cron.py` 钉死；用差分测试兜住与标准语义的偏离 |
| `services/scheduler.py`（进程内 asyncio + DB 抢占） | APScheduler 3.11（4.x 仍 alpha）、Temporal、arq | MIT | **保留**。调度器与 `run_turn`、中断恢复、任务抢占耦合，替换是重写；单副本部署本来就成立。多副本再引入外部调度器 |
| `sandbox.py` + `sandbox_guard/`（审计钩子） | E2B（可单机自托管，需 KVM）、nsjail（Google）、OpenHands runtime（Docker） | Apache-2.0 / MIT | **建议替换执行层**。已把边界写进 README；`code_executor` 是唯一替换面，core 不需要改 |
| `knowledge/graph/`（tree-sitter + 自建图） | tree-sitter-language-pack | MIT，语法全部宽松许可 | **已采用**。只把 7 个语法预编译入库，避免构建期联网；补了校验与来源 |
| `compressor.py`（丢旧结果 → 摘要 → 中断） | LLMLingua | MIT | **保留**。LLMLingua 需本地模型常驻，与"只调远端 API"的部署形态冲突 |
| 记忆（表 + 摘要 + 标签） | mem0 | Apache-2.0，活跃 | **保留**。引入即多一个向量服务；现有记忆是可审计数据 |
| `user_crypto.py`（PBKDF2 20 万次） | argon2-cffi | MIT，活跃 | **建议后续迁移**。可做"旧哈希校验通过后透明重写"，无存量账号风险 |
| `i18n/`（自建 `t()`） | vue-i18n | MIT | **保留**。单语言下更小；重新多语言时改用它 |

**明确否决**：整体换 LangChain / LlamaIndex（价值在文件化编排与可审计性，框架引入隐式状态且与 `ToolRegistry` 重复）；Celery 替换调度器（多一层 broker 运维，单机更重）；`python-jose`/`authlib` 保留（已无使用场景）。

## 六、未实施但建议排期

1. **拆分 `agent_turn.py` / `agent.py`**（P0，纯重构）：按"上下文组装 / 模型选择 / 工具执行回调 / 事件推送"切四个模块，行为不变，测试兜底。
2. **沙箱执行层容器化**（P1）：`code_executor` 在 Linux 上优先走 nsjail（单机、无 KVM 依赖），Windows/macOS 开发环境保留审计钩子路径。
3. **调度器外置**（P1，仅多副本需求）：先加 PostgreSQL advisory lock 作为最小改动；需要更强语义再上 Temporal。
4. **口令哈希迁移 argon2id**（P2）：登录成功时透明重写，双算法共存一个版本周期。
5. **lint 分批清理**（P2）：先只对新增/改动文件跑 ruff，再逐步收敛历史告警，最后纳入 CI。
6. **发布能力补齐或删除**（P2）：`models/publish.py` 与 `conversations.publish_id` 目前无调用方。要么补 `routers/publish.py`，要么删模型与迁移（后者是不可逆迁移，需单独评审）。
7. **前端巨型文件拆分**（P3）：`TokenConfigModal.vue`、`layout.css`、`conversation` store。

## 七、验证记录

| 检查 | 结果 |
|---|---|
| `pytest packages/api/tests`（全量，SQLite + 真实 alembic 链） | **744 passed**（约 3 分钟） |
| `pytest packages/agent-core/tests/test_cron.py tests/test_cron_parity.py` | 87 passed（含新增差分用例） |
| `npm ci`（`packages/web`，归一后的锁文件） | 通过，473 个包 |
| `npm run build`（`vue-tsc --noEmit && vite build`） | 通过；仅有 chunk 体积警告，无类型错误 |
| `npm test -- --run` | 63 个测试文件 / 517 用例全绿 |
| `python scripts/check_commit_hygiene.py urls --all-files` | 退出码 0 |
| `docker build --check .` | **未执行**：本机没有 Docker CLI，只对 Dockerfile 做了静态审阅 |

**验证环境说明（重要，避免误判）**：本机 `packages/api/.venv` 里那个 uv 安装的 CPython 3.12 无法写文件型 SQLite——`sqlite3.connect(路径)` 之后 `CREATE TABLE`，同一条连接立刻查不到该表（`no such table: t`），换用同一台机器上的另一个 3.12 解释器跑同一段脚本则正常。最初 API 全量测试的 744 个 error 全部由此产生：alembic 新建的 `alembic_version` 在同一条连接里"消失"，最终报 `INSERT INTO alembic_version ... no such table`。这不是 alembic 版本回归——`.tmp/minrepro` 里的最小 alembic 工程在该解释器下同样失败，换解释器后整条迁移链通过；因此**没有**给 `alembic` 加版本上界。把上述 SQLite 缺陷绕过之后，744 个用例全部通过。

Windows 上另有一处环境细节：`mcp` 声明了 `pywin32>=311; sys_platform == 'win32'`，若用 `PYTHONPATH` 手工挂 `site-packages`（会绕过 `pywin32.pth`），还需要额外挂 `win32`、`win32\lib`、`pywin32_system32` 三个目录，否则 `test_mcp_servers_crud.py::test_test_connection_success` 会以 `ModuleNotFoundError: pywintypes` 报错。
