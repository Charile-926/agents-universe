# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本仓库工作时提供指导。它只描述**稳定的、读代码一眼看不出来的**约定；会变的事实（文件清单、功能列表、运行命令）放 README，不在这里重复。

## 项目一句话

Agents Universe 是一个多智能体平台：后端 Python（FastAPI + SQLAlchemy 2 + Alembic），前端 Vue 3 + TypeScript，单仓库 `packages/` 下三个包（`agent-core`、`api`、`web`）。生产形态是一个 Docker 镜像（nginx 同时托管前端静态文件和 `/api`、`/ws` 反向代理）。

## 命令

```bash
# 后端
pip install -e "packages/agent-core[dev]" -e "packages/api[dev]"   # 或用 .venv
pytest packages/agent-core/tests -q        # agent-core 单测（含 browser_* 需 Playwright）
pytest packages/api/tests -q               # api 单测（SQLite 跑完整 alembic 链）
cd packages/api && alembic upgrade head    # 本地迁移（DATABASE_URL 指向目标库）

# 前端
cd packages/web && npm ci && npm run dev    # http://localhost:5173，代理 /api、/ws 到 8000
cd packages/web && npm run build            # vue-tsc 类型检查 + vite 构建（dist/）
cd packages/web && npm test -- --run        # vitest

# 提交前
python scripts/check_commit_hygiene.py urls --all-files   # URL 主机白名单（.commit-hygiene.toml）
python scripts/check_commit_hygiene.py identity    # 提交身份邮箱策略
pre-commit run --all-files                         # 全套本地钩子（gitleaks、空白、YAML/TOML 等）
```

`pytest -m "not browser"` 跳过需要 Playwright/Chromium 的用例。

## 架构速览

```
packages/
  agent-core/   纯 Python 库，无 FastAPI 依赖
    agent.py          Agent 循环：system prompt 组装 → provider 调用 → tool_calls → 压缩 → 循环
    providers/        anthropic / openai / gemini 三家 SDK 适配 + registry
    tools/            ~40 个工具，统一 ToolRegistry；工具必须声明 side_effect / degrades_context
    knowledge/        loader（md+frontmatter → KnowledgeEntry）+ index（SQLite 缓存）+ graph（tree-sitter 代码图谱）
    skills/ workflows/ 内容注册表（agents/skills/*.md、workflows/*.yaml）
    scheduling/cron.py 五字段 cron 求值（标准库；tests/test_cron_parity.py 与 croniter 交叉校验）
    sandbox.py        审计钩子 + 路径白名单；sandbox_guard/sitecustomize.py 是子进程侧 guard
  api/          FastAPI 应用
    routers/          HTTP / WebSocket 路由，只做参数校验与鉴权，业务在 services/
    services/         agent_turn.py（一轮对话的编排）、scheduler.py（进程内调度循环）等
    models/           SQLAlchemy 模型（_compat.py 提供跨方言 UTCDateTime / uuid）
    alembic/          迁移链；SQLite/PG/MySQL/MSSQL 四方言都要能跑
  web/          Vue 3 SPA
    src/api/          fetch 封装与 SSE/WS 客户端
    src/stores/       Pinia：conversation（消息流）、auth、settings 等
    src/composables/  useWebSocket 等
agents/          *.agent.md 智能体定义 + skills/ 子目录
workflows/       YAML 工作流
knowledge/       Markdown 知识库（frontmatter: slug/title/category/tags）
scaffold/        项目脚手架模板
docker/          ts-grammars 预编译语法（含 SHA256SUMS）、healthcheck
scripts/         deploy_server.sh、fetch_ts_grammars.py、check_commit_hygiene.py
```

一次对话的路径：浏览器 `POST /ws`（`api/websocket.py`）→ `services/agent_turn.py` 组装上下文（知识检索、记忆、技能、计划、模型凭据解密）→ `agent_core.Agent.run()` 循环 → 工具执行（可能落到 `sandbox.py`）→ 事件流回 WebSocket → 前端 store 渲染。

## 硬性约定

### 数据库
- 主键一律 `String(36)` + 客户端 `uuid4()`（`models/_compat.py:new_uuid`），不用数据库自增或 `NEWID()`。迁移中 T-SQL 专属 DDL 放在 `if op.get_bind().dialect.name == "mssql":` 分支内；SQLite 用 `op.batch_alter_table`；数据回填用 `context.is_offline_mode()` 守卫。
- PostgreSQL 是默认生产方言（`DB_*` 拼 URL），SQL Server 是遗留部署（`MSSQL_CONNECTION_STRING`），MySQL/SQLite 同样一等公民。新迁移必须四方言可编译。
- 新模型必须写 UTC（`UTCDateTime` naive UTC），不要用 `datetime.now()`。
- 迁移文件头部的 Revision ID / Create Date 必须由 alembic 生成，不要手写。

### 认证与安全
- 会话是 **Redis + HttpOnly 不透明 Cookie**（`dependencies/auth.py`）；没有 JWT，没有 OAuth。登录方式只有用户名 + 密码（`routers/auth_local.py`）。
- 用户名 / 显示名在库里只有 HMAC 摘要 + Fernet 密文（`services/user_crypto.py`）；API Key 走 AES-256-GCM（`services/token_vault.py`）。新表存敏感字段必须复用这两个模块，不要自造加密。
- `SECRET_KEY` 为 `change-me` 占位值时 API 拒绝启动（`main.py`），不要绕过。
- 所有用户数据查询必须带 `user_id` 过滤；跨用户访问返回 404 而不是 403。
- 智能体发起的 HTTP 请求必须经 `tools/_ssrf.py` 校验（含重定向逐跳）。
- MCP 工具：头名黑名单、破坏性工具二次确认、`mcp_client.py` 连接超时。新增外部 IO 工具时同步更新这两处。

### 智能体工具
- 每个工具在 `ToolRegistry` 注册并声明 `side_effect`（read / write / external）与 `degrades_context`。
- 工具结果进入模型上下文前必须截断（`agent.py` 的 `_MAX_TOOL_RESULT_CHARS`），大输出落盘并只回传路径。
- 上下文压缩策略在 `compressor.py`：先丢最旧的工具结果，再摘要，再中断。
- 沙箱（`sandbox.py`）是**纵深防御而非安全边界**：多租户或不可信代码必须放到独立容器 / microVM 执行。
- 后台任务（脚本运行、cron 调度）并发上限 64，超出的排队；**排队中的任务不持久化**，重启会丢。

### 前端
- 界面文案只维护 `src/i18n/locales/zh-CN.ts`，运行时不存在第二种语言（`category_i18n` 是数据字段，不是 UI 多语言）。
- 所有 API 调用走 `src/api/` 的封装（统一错误、401 跳登录、request id）。
- 样式是单一主题：纯白底黑字、无渐变、无阴影特效；按钮字体字号全局统一。改动 `styles/` 时不要引入第二套视觉。
- 新组件必须带 `__tests__/*.spec.ts`（vitest + @vue/test-utils）。

### 内容文件
- `agents/*.agent.md` 必须有 `model` 字段；frontmatter 用 `python-frontmatter` 解析。
- `knowledge/*.md` 的 `slug` 与路径一致，`category` ∈ system / domain / technical / skills / workflows。这份知识库会被索引进智能体上下文，所以**过期的知识文档等同于给模型错误输入**——改了架构或表结构，同步改 `knowledge/technical/`。

## 不要做
- 不要在 `routers/` 里写业务逻辑；不要在 `agent-core` 里 import `api`。
- 不要提交 `node_modules`、`.tmp/`、`data/`、`.env`（已在 .gitignore / .dockerignore）。
- 不要在文档里写未在 `.commit-hygiene.toml` 白名单中的 URL 主机，CI 会拒绝。
- 不要给 `docker/ts-grammars/*.so` 换版本而不更新 `SHA256SUMS` 与 `fetch_ts_grammars.py` 的 `PACK_VERSION`。
- 不要为了让 Dockerfile 在某个网络环境跑通而默认关闭 TLS 校验——只在构建参数 `INSECURE_TLS` / 代理构建参数下放行。
