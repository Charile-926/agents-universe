# Agents Universe

**自托管的多智能体工作平台。** 在浏览器里把一群智能体组织成一个团队：Product Owner 拆需求、Tech Lead 写代码、QA 验结果，其余角色（数据分析、渗透测试、办公助手、客服……）按需加入。每个智能体有独立的定义、技能、工作流与知识库；它们共享同一份项目工作区、对话历史和调度系统。

- **单体可部署**：一个 Docker 镜像 = nginx（前端）+ FastAPI（API/WS）+ 进程内调度器，外加 PostgreSQL 与 Redis。
- **纯自托管**：模型 API Key 由用户自带（Anthropic / OpenAI / Azure OpenAI / Gemini / 任意 OpenAI 兼容端点），经 AES-256-GCM 加密只存在你自己的数据库里。
- **代码可审计**：全部提示词、技能、工作流、知识都是仓库里的 Markdown / YAML，不藏在服务端数据库。

本仓库是上游 [agents-universe](https://github.com/agents-universe/agents-universe)（[agents-universe.com](https://agents-universe.com)）的精简分支，保留单页炼化工作台、账号密码登录与 PostgreSQL 默认部署，移除了 OAuth SSO、界面多语言与 workspace 层。

---

## 目录

- [能力](#能力)
- [架构](#架构)
- [快速开始](#快速开始)
- [配置](#配置)
- [开发与测试](#开发与测试)
- [部署、升级与回滚](#部署升级与回滚)
- [架构边界与已知限制](#架构边界与已知限制)
- [自研组件 vs 成熟开源方案](#自研组件-vs-成熟开源方案)
- [目录结构](#目录结构)
- [文档与许可](#文档与许可)

---

## 能力

| 能力 | 说明 | 代码锚点 |
|---|---|---|
| 多智能体协作 | 10 个内置角色；`delegate_agent` 让智能体把子任务交给别的角色（深度上限 + 目标白名单 + 全局开关） | `agents/*.agent.md`、`services/delegation.py` |
| 对话与任务计划 | WebSocket 流式输出、可中断、断线可恢复（`conversation_runs` + 快照）；复杂任务先出计划再逐项执行 | `agent_core/agent.py`、`services/agent_turn.py` |
| 工具集 | 文件读写、shell/python 执行、Git、浏览器（Playwright）、HTTP、MCP、图表、媒体、委派等 | `agent_core/tools/` |
| 项目工作区 | 每个项目一份磁盘目录；文件树、预览、上传、ZIP；项目隔离（私有项目白名单） | `routers/workspace_files.py`、`api/paths.py` |
| 知识库 | Markdown + frontmatter，按 slug/标签/交叉引用检索；项目级与全局两级；知识装载审计 | `agent_core/knowledge/` |
| 仓库知识图谱 | tree-sitter 解析 Python / TS / TSX / JS / Vue / Java / C#，`impact` 类查询回答"谁调用我、改这里会炸哪"；`.sql` 走区域正则抽取表/视图/过程 | `agent_core/knowledge/graph/` |
| 技能与工作流 | 四类技能（guidance / template / executable / composite）+ 11 个工作流 | `agents/skills/`、`workflows/` |
| 记忆 | 个人长期记忆（跨会话、按用户/项目）+ 情节记忆（每轮摘要） | `routers/memories.py`、`services/episodic_service.py` |
| 自动化脚本 | workflow / python / bash / playwright 四种脚本，实时日志，运行记录 | `agent_core/scripts/runner.py`、`routers/script_runs.py` |
| 定时任务 | 五字段 cron（时区感知），可触发脚本、Playwright 用例或无头智能体回合 | `services/scheduler.py`、`scheduling/cron.py` |
| 模型路由 | 用户可配多套模型；`auto` 模式先用最便宜的模型判复杂度再路由 | `services/complexity.py`、`services/model_tier.py` |
| 凭据管理 | 用户级集成令牌、项目级密钥、LLM Key；分层加密，明文不落库、不进日志 | `services/token_vault.py` |

## 架构

### 运行时拓扑

```
                          ┌─────────────────────────────────────────────┐
   浏览器 (Vue 3 SPA) ──► │ nginx :8000                                  │
   同源 /api、/ws          │  ├─ /             → dist/ 静态文件（SPA）      │
                          │  ├─ /api、/health → uvicorn 127.0.0.1:8001   │
                          │  └─ /ws           → uvicorn（WebSocket）     │
                          └─────────────────────────────────────────────┘
                                             │
                          ┌──────────────────▼──────────────────────────┐
                          │ FastAPI (uvicorn, 单进程)                    │
                          │  routers/   参数校验、鉴权、限流             │
                          │  services/  agent_turn、scheduler、delegation│
                          │  agent-core Agent.run() 循环 + 工具执行      │
                          │  sandbox    审计钩子 + 路径白名单             │
                          └───────┬───────────────────────┬─────────────┘
                                  │                       │
                     PostgreSQL / MySQL /            Redis
                     SQLite / SQL Server             会话、活跃用户、取消标记
```

镜像内部由 `docker-entrypoint.sh` 作为 PID 1 监督两个子进程：nginx（对外 8000）与 uvicorn（仅回环 8001）。nginx 挂掉会被自动重启，uvicorn 挂掉则退出容器交给 compose 的 `restart` 策略——"容器 Up 但整站不可用"是这套 supervisor 要避免的状态。

### 一次对话的路径

1. 前端通过 `/ws` 建立连接，发送用户消息。
2. `services/agent_turn.py` 组装这一轮的全部上下文：解密该用户的模型凭据 → 选择模型（显式 / `auto` 路由）→ 装载项目知识、记忆、技能、工作流、活动计划 → 构造 `ToolContext`。
3. `agent_core.Agent.run()` 进入循环：调用 provider → 解析 `tool_calls` → 执行工具（需要隔离的走 `sandbox.py`）→ 结果截断/落盘 → 必要时压缩上下文 → 继续，直到模型不再调用工具或触发中断。
4. 每个事件（token、工具开始/结束、计划更新、错误）实时推回 WebSocket，同时把本轮落成 `conversation_runs` 记录与 `messages` 行；中断后用 `streaming_snapshot` 恢复。
5. 轮次结束后写情节记忆、更新 token 计数、触发知识装载审计。

### 三个包的职责边界

| 包 | 是 | 不是 |
|---|---|---|
| `packages/agent-core` | 纯 Python 库：Agent 循环、provider 适配、工具、知识、沙箱、cron 求值 | 不知道 HTTP、数据库、用户——所有 IO 通过注入的 `ToolContext` |
| `packages/api` | FastAPI 应用：路由、鉴权、会话、持久化、调度循环、把两者粘起来 | 不实现智能体推理循环，不直接写 provider SDK 调用 |
| `packages/web` | Vue 3 SPA：状态、渲染、交互 | 不含业务规则；所有判定以 API 返回为准 |

这条边界是仓库最重要的架构约束：`agent-core` 可以被单独装进别的服务，`api` 可以为同一套核心换前端，`web` 可以只换主题。改动时若需要打破它（例如让 core 读数据库），先考虑把数据作为参数注入。

## 快速开始

前置：Docker 与 Docker Compose v2；至少 4 GB 内存（镜像内含 Chromium 与 JDK）。

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml

# 生成 SECRET_KEY 并写入 .env（占位值 change-me 会被拒绝启动）
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env
```

Windows（PowerShell）等价写法：

```powershell
Copy-Item .env.example .env
Copy-Item docker-compose.example.yml docker-compose.yml
"SECRET_KEY=$([Convert]::ToHexString((1..32 | ForEach-Object { Get-Random -Max 256 })).ToLower())" | Out-File -Append -Encoding utf8 .env
```

然后：

```bash
docker compose up --build
```

打开 <http://localhost:8000>，在 `/login` 页注册第一个账号（本地用户名 + 密码，数据库中只存密文与哈希）。

| 场景 | 命令 | 入口 |
|---|---|---|
| 生产形态（nginx + API 同容器） | `docker compose up --build` | <http://localhost:8000> |
| 前端热重载（vite dev server） | `docker compose --profile dev up` | <http://localhost:5173> |
| 只用独立前端镜像验证生产构建 | `docker compose --profile prod-web up --build` | <http://localhost:8080> |

Linux 主机首次启动前要先建好数据目录并交给容器运行用户，否则项目创建会失败（`PROJECT_FS_INIT_FAILED`）：

```bash
mkdir -p ./data/projects
# 镜像里的 appuser 由 `useradd -r` 创建，uid 取决于基础镜像（当前镜像通常是 997）；
# 先启动一次再核对：docker compose exec api id -u
sudo chown 997:997 ./data/projects
```

### 本地开发（不用 Docker）

```bash
docker compose up -d postgres redis          # 只起依赖服务

pip install -e "packages/agent-core[dev]" -e "packages/api[dev]"

cd packages/api && PYTHONPATH=src python -m uvicorn api.main:app --port 8000 --reload
cd packages/web && npm ci && npm run dev      # 5173，代理 /api、/ws 到 8000
```

Python ≥ 3.12、Node ≥ 22。数据库迁移由 API 启动时自动执行，也可手动 `cd packages/api && alembic upgrade head`。

## 配置

全部变量见 [`.env.example`](.env.example)（逐项带注释）。必须理解的只有这些：

| 变量 | 默认 | 作用 |
|---|---|---|
| `SECRET_KEY` | 无（占位值拒绝启动） | 派生令牌加密、用户名 HMAC、字段加密。**更换即等于丢失已加密数据**，务必备份 |
| `APP_BASE_URL` / `WEB_BASE_URL` | `http://localhost:8000` | 对外地址；CORS、Cookie 作用域、重定向都据此推导（https ⇒ Secure Cookie） |
| `DB_PASSWORD` | `agents` | compose 里 PostgreSQL 的密码；生产必须替换 |
| `DATABASE_URL` | 由 `DB_*` 拼 PostgreSQL | 也可直接给完整 URL；支持 PostgreSQL / MySQL / SQLite / SQL Server |
| `REDIS_URL` | `redis://redis:6379/0` | 会话与短期状态；缺失则无法登录 |
| `PROJECTS_ROOT` | `/app/projects` | 项目工作区根目录，必须在持久卷里且运行用户可写 |
| `AUTH_BYPASS_ENABLED` | `false` | 免登录旁路，**只在单人本地调试时开**；多用户环境开启等于所有人共用身份 |
| `API_DOCS_ENABLED` | `false` | 是否暴露 `/docs` 与 `/openapi.json` |
| `COOKIE_SECURE_OVERRIDE` | 空（按 URL 推导） | 强制会话 Cookie 的 `Secure` 属性 |

构建期参数（`docker build --build-arg`）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `NPM_REGISTRY` | `https://registry.npmmirror.com` | 前端依赖源；锁文件解析到 npmjs，构建时按此重写 |
| `APT_MIRROR_HOST` | `mirrors.tuna.tsinghua.edu.cn` | Debian 源镜像 |
| `INSECURE_TLS` | `auto` | apt / curl 的 TLS 校验。默认仅在企业代理构建参数存在时才关闭；显式 `false` 强制校验、`true` 强制关闭 |

## 开发与测试

```bash
# 后端
pytest packages/agent-core/tests -q            # -m "not browser" 跳过 Playwright 用例
pytest packages/api/tests -q                   # 每次会话跑真实 alembic 链（SQLite）

# 前端
cd packages/web && npm run build               # vue-tsc 类型检查 + 产物构建
cd packages/web && npm test -- --run           # vitest（含组件测试）

# 提交前
python scripts/check_commit_hygiene.py urls --all-files   # URL 主机白名单
python scripts/check_commit_hygiene.py identity    # 提交身份邮箱策略
pre-commit run --all-files                         # gitleaks、空白、YAML/TOML 等
```

CI（GitHub Actions，`.github/workflows/ci.yml`）：`agent-core` 与 `api` 两个 pytest job（后者另有 PostgreSQL 16 job 跑同一套用例，`TEST_DATABASE_URL` 指向容器）、前端 lint + 类型检查 + 构建 + vitest、`hygiene` 策略检查。前端安装固定 `npm ci`；锁文件的解析地址已归一为 npmjs 官方源（构建期仍可用 `NPM_REGISTRY` 重写）。

两个值得知道的测试约定：`packages/api/tests` 每个会话用真实迁移链建库（不是 `create_all`），所以"迁移能跑通"本身就是被测试的契约；`tests/test_cron_parity.py` 用成熟实现 croniter 做差分对照，防止自研 cron 求值悄悄偏离标准语义。

## 部署、升级与回滚

生产推荐一台 Linux 主机 + 自己的反向代理 / TLS 终止：

```bash
# 服务器上，仓库根目录
bash scripts/deploy_server.sh --dry-run     # 先看会做什么
bash scripts/deploy_server.sh               # 备份 → 拉代码 → 重建镜像 → 重启 → 健康检查 → 失败自动回滚
```

脚本会备份数据库（SQLite 拷文件 / PostgreSQL 走 pg_dump）与上一版镜像，健康检查失败自动切回旧镜像并保留备份（默认留 5 份）。可用 `APP_DIR`、`BRANCH`、`IMAGE`、`HEALTH_URL` 等环境变量覆盖。

**回滚注意**：升级会执行 alembic 迁移（可能删列、重建表）。

- SQLite 部署：必须"旧镜像 + 旧库文件"一起恢复，只回滚镜像会因缺列而 500。
- PostgreSQL 部署：回滚镜像前先确认数据库已有快照，删列类迁移不可逆。
- 升级前务必备份 `SECRET_KEY`：丢了它，已加密的 API Key、项目密钥、用户名全部无法解密。

对外只暴露 nginx 的 8000（或你自己的 443），不要直接暴露 8001 / Redis / 数据库。

## 架构边界与已知限制

诚实说明这套系统的边界，避免在生产里踩到：

1. **沙箱不是安全边界。** 智能体生成的 Python / shell 在审计钩子、路径白名单、命令校验器下运行（`agent_core/sandbox.py`、`sandbox_guard/sitecustomize.py`）。它防误操作，不防刻意逃逸。多租户或要跑不可信代码时，把执行层换成独立容器 / microVM（见下节）。
2. **调度器是进程内的。** `services/scheduler.py` 在 API 进程里跑一个 asyncio 循环，任务通过数据库行"抢占"防重。单副本部署成立；多副本需要外部调度器或分布式锁，否则会重复触发。
3. **排队任务不持久化。** 脚本/定时任务并发上限 64，超出的留内存队列，重启即丢。需要可靠队列时接外部 broker。
4. **智能体权限 = 当前用户权限。** 它用你的令牌做事，能做的就是你授权它做的；`delegate_agent` 会放宽到目标智能体的工具集，因此该开关默认保守。
5. **单进程 Python。** 长任务（浏览器、大文件）会占用 worker；水平扩展靠多容器 + 外部调度，不靠进程内线程。
6. **知识检索是关键词 + 结构打分，不是向量检索。** 设计上不引入 embedding 模型（离线可部署、零额外成本），代价是同义改写命中率不如向量库。
7. **界面固定简体中文。** 数据结构保留 `zh-CN` / `en-US` 覆盖字段（`category_i18n` 等），但运行时只有一套 UI 文案；`en-US.ts` 文案包已删除，重新引入多语言应当接 vue-i18n，而不是再手抄一份字典。
8. **发布（Agent-as-a-Service）只剩数据模型。** `models/publish.py` 与相关迁移还在，但本分支没有对应的路由与前端入口（测试 `SidebarFooter.test.ts` 明确断言该入口已移除）。需要对外发布能力时，应参照 `models/publish.py` 的注释补齐 `routers/publish.py` 与前端页面，而不是在会话路由里临时拼。

## 自研组件 vs 成熟开源方案

审查时对每个"自己写"的组件都做了同类开源项目调研：只看 **维护活跃 + 许可证明确 + 与现有抽象兼容** 的候选，许可证一律限定 MIT / Apache-2.0 / BSD。结论分三类：**已借鉴**、**评估后保留自研**、**建议后续替换**。

| 组件 | 自研现状 | 候选方案（许可证，维护状态） | 结论 |
|---|---|---|---|
| cron 求值 `scheduling/cron.py` | 262 行标准库实现，钉住 DST 折返与 POSIX "日/周三选一"语义 | [croniter](https://github.com/pallets-eco/croniter)（MIT，Pallets 维护，2026-11 仍在发版） | **已借鉴**：保留自研（无需新运行时依赖、DST 语义已被测试钉住），新增 `tests/test_cron_parity.py` 用 croniter 做差分对照，语义漂移会被测出来 |
| 进程内调度 `services/scheduler.py` | asyncio 循环 + DB 行抢占 | [APScheduler](https://github.com/agronholm/apscheduler)（MIT，3.x 稳定 / 4.x 仍 alpha）、Temporal、arq | **保留**：调度与 `run_turn`、任务抢占、中断恢复深度耦合，替换是一次重写；多副本诉求用外部调度器解决（限制第 2 条） |
| 执行沙箱 `sandbox.py` | 审计钩子 + 路径白名单 + 命令校验 | [E2B](https://github.com/e2b-dev/infra)（Apache-2.0，可单机自托管）、[nsjail](https://github.com/google/nsjail)（Apache-2.0，Google 维护）、[OpenHands runtime](https://github.com/All-Hands-AI/OpenHands)（MIT，Docker 执行器） | **建议替换执行层**：审计钩子适合单租户纵深防御；多租户应把 `code_executor` 换成 nsjail / E2B 容器，core 的工具接口已按 `ToolContext` 抽象，替换面收敛在一个工具内 |
| 代码图谱 `knowledge/graph/` | tree-sitter 语法 + 自建符号/边表 | [tree-sitter-language-pack](https://github.com/xberg-io/tree-sitter-language-pack)（MIT；打包的语法全部 MIT/Apache-2.0/BSD/ISC） | **已采用**：语法包就是成熟方案；本仓库只把所需 7 个语法预编译进镜像（`docker/ts-grammars/`），附 `SHA256SUMS` 与来源说明，避免构建期联网与来源不明二进制 |
| 上下文压缩 `compressor.py` | 丢旧工具结果 → 摘要 → 中断 | [LLMLingua](https://github.com/microsoft/LLMLingua)（MIT，Microsoft） | **保留**：LLMLingua 需本地小模型常驻，与本项目"只调远端 API"的部署形态冲突；现有策略是确定性降级，成本可预测 |
| 记忆 `episodic_service.py` / `memory_rw.py` | 表 + 摘要 + 标签检索 | [mem0](https://github.com/mem0ai/mem0)（Apache-2.0，活跃） | **保留，可选用**：mem0 自带向量库与抽取管线，引入即增加外部服务；当前设计把记忆当"可审计的 Markdown/表数据"，与知识库同构 |
| 口令哈希 `user_crypto.py` | PBKDF2-HMAC-SHA256，20 万次迭代 | [argon2-cffi](https://github.com/hynek/argon2-cffi)（MIT，活跃） | **建议后续迁移**：argon2id 抗 GPU 更强；迁移可做"登录时按旧哈希校验通过后透明重写"，不影响存量账号 |
| 前端文案 `i18n/` | 自建静态字典 + `t()` 插值 | vue-i18n（MIT） | **保留**：单语言下自建实现更小、无语言竞态；重新引入多语言时应改用 vue-i18n，而不是复制两份字典 |

被否掉的候选（记录原因，避免重复调研）：把整个 agent 运行时换成 LangChain / LlamaIndex——本项目的价值在"知识、技能、工作流都是文件 + 可审计的编排"，框架会引入大量隐式状态且与现有 `ToolRegistry` 抽象重复；用 Celery 替换调度器——引入 broker 与 worker 运维面，单机部署反而更重。

## 目录结构

```
packages/
  agent-core/          纯 Python 库：Agent 循环、provider、工具、知识、图谱、沙箱、cron
  api/                 FastAPI：routers / services / models / alembic
  web/                 Vue 3 + Vite + Pinia：pages / components / stores / composables
agents/                10 个 *.agent.md + skills/（47 个技能）
workflows/             11 个 *.workflow.md
knowledge/             全局知识库（system / technical / _template）
scaffold/              新建项目的模板
docker/                健康检查脚本、预编译 tree-sitter 语法（含 SHA256SUMS）
scripts/               deploy_server.sh、fetch_ts_grammars.py、check_commit_hygiene.py
docs/                  实现指南与架构审查报告
```

## 文档与许可

- 实现细节与设计取舍：[`docs/agents-universe-implementation-guide.zh-CN.md`](docs/agents-universe-implementation-guide.zh-CN.md)
- 本次架构审查与开源方案对比：[`docs/architecture-review-2026-11.zh-CN.md`](docs/architecture-review-2026-11.zh-CN.md)
- 给编码智能体的约定：[`CLAUDE.md`](CLAUDE.md)　贡献流程：[`CONTRIBUTING.md`](CONTRIBUTING.md)　安全策略：[`SECURITY.md`](SECURITY.md)　变更记录：[`CHANGELOG.md`](CHANGELOG.md)

Apache License 2.0，见 [`LICENSE`](LICENSE)。上游项目同样为 Apache-2.0；本分支的删改（移除 OAuth SSO、界面多语言、workspace 层）记录在 `CHANGELOG.md`。
