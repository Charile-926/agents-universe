# Agents Universe（智能体工作台）

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Vue 3](https://img.shields.io/badge/Vue-3.5-42b883.svg)](https://vuejs.org/)

> 一个多用户的智能体工作台：登录后与内置智能体对话、管理项目与会话、配置模型密钥，并把聊天记录与工具调用炼化成可复用的 Skill。

界面固定简体中文。左侧是项目与会话，中间是对话/工作区/发布/定时任务，右侧是知识、记忆与 Skill 炼化面板。

## 功能范围

- **账号 + 密码登录**：注册/登录/登出。密码以 PBKDF2-HMAC-SHA256 单向哈希存储，用户名与显示名加密后落库（PostgreSQL）。
- **十个内置智能体**：Product Owner、Quality Assurance、Tech Lead、数据分析专家、办公助手、外部系统集成专家、智能体定制专家、项目定制专家、渗透测试专家、智能客服；顶栏选择器可切换并收藏到侧边栏。
- **对话**：流式输出、@ 提及其他智能体、附件上传、工具调用与任务计划卡片、上下文压缩与用量表。
- **项目与会话**：项目树、收藏、会话历史树、工作区文件树与脚本运行。
- **知识与记忆**：知识完整度、条目加载情况；用户/项目密钥、会话笔记、个人记忆与情节记忆。
- **密钥配置**：`/settings/tokens` 配置 LLM 模型与外部集成，数据加密存储、按用户隔离。
- **发布与定时任务**：把智能体能力发布成可访问的服务，或按计划运行。
- **Skill 炼化（核心功能之一）**：右栏「炼化 Skill」面板或独立页 `/distill`，把当前账号自己的聊天记录与工具调用汇总成一份可复用的 Skill，支持预览、复制、下载 `.md`。
- **新手引导**：首次登录的分步 Tour。
- **服务端权限隔离**：所有查询以当前登录用户为前提；跨用户读取返回 404，不确认资源是否存在。

已移除：OAuth/SSO 入口、中英文切换（界面固定简体中文，`vue-i18n` 已不参与构建）。

## 快速开始

### 环境要求

- Docker 与 Docker Compose（推荐）
- 本地开发另需：Node.js 20+、Python 3.12+、PostgreSQL 16

### Docker 一键启动

```bash
# 1. 复制环境变量与编排文件（docker-compose.yml 未入库，必须从示例复制）
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml

# 2. 生成 SECRET_KEY 写入 .env（用户字段加密与会话签名都由它派生）
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env

# 3. 启动（含 PostgreSQL、Redis、API 与前端）
docker compose up --build
```

启动后访问 http://localhost:8000 。首次打开会跳转到 `/login`，注册账号即可使用。

数据库迁移由容器启动脚本自动执行。`DATABASE_URL` 在编排文件里指向 `postgres:5432`；本地直连时改成 `127.0.0.1:5432`。

### 本地开发

```bash
# 0. 依赖服务（PostgreSQL + Redis）
docker compose up -d postgres redis

# 1. 环境变量：cp .env.example .env，生成 SECRET_KEY，
#    设置 DATABASE_URL=postgresql+asyncpg://agents:agents@127.0.0.1:5432/agentsuniverse
#    并把 PROJECTS_ROOT 设为已存在的绝对路径

# 2. 后端（packages/api/）
cd packages/api
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ../agent-core -e .
PYTHONPATH=src python -m uvicorn api.main:app --port 8000 --reload

# 3. 前端（packages/web/）
cd packages/web
npm install
npm run dev                          # http://localhost:5173，代理到 :8000
```

接口文档默认关闭（`/docs`、`/openapi.json` 不暴露）；本地调试需要时在 `.env` 设 `API_DOCS_ENABLED=true`。

## 账号与数据存储

| 数据 | 存储方式 |
| --- | --- |
| 登录口令 | PBKDF2-HMAC-SHA256，每用户独立盐，20 万次迭代，单向不可逆 |
| 用户名 / 显示名 | Fernet（AES-128-CBC + HMAC-SHA256）密文；密钥由 `SECRET_KEY` 经 HKDF 派生，不落库 |
| 用户名查找 | HMAC-SHA256 摘要（大小写归一化），仅用于唯一约束与登录匹配 |
| 会话 | Redis + HttpOnly Cookie，REST 与 WebSocket 共用，随活跃请求滑动续期 |
| 业务数据 | PostgreSQL（账号、项目、会话、消息、工具调用记录） |

平台不再提供 OAuth/SSO 入口：**每个用户都必须用账号与密码登录**。`AUTH_BYPASS_ENABLED` 默认关闭，多用户环境禁止开启——开启后所有请求都会解析成同一个内置用户，隔离失效。

## Skill 炼化

入口有两个：工作台右栏的「炼化 Skill」标签页，以及独立页 `/distill`（不依赖具体项目，适合离开会话后集中整理）。

1. **选择来源**：勾选具体会话，或勾选"汇总全部会话"（取最近 50 个，超出自动截断）。
2. **生成**：服务端读取当前用户的消息与工具调用记录，按"场景 / 典型需求 / 标准流程 / 工具规范 / 已知失败 / 质量清单"汇总成 Skill 草稿。
3. **预览 / 复制 / 下载**：草稿是标准 frontmatter + Markdown，可直接放进项目的 `skills/` 目录使用。

数据量保护：单次最多 50 个会话、每会话最近 80 条消息、总量 3000 条；触发上限时界面会明确提示"已分批"。生成前会脱敏密钥、Bearer 凭证与本机绝对路径。

## 权限隔离

隔离在服务端完成：

| 资源 | 规则 |
| --- | --- |
| 会话列表 / 消息 / 任务 | 一律以 `conversation.user_id = 当前用户` 为前提；跨用户访问返回 404 |
| 炼化接口 | 只聚合当前账号的会话与工具调用；传入他人会话 id 返回 404 |
| 项目 / 媒体 / 工作区文件 | 先校验项目访问权，再校验会话归属；私有项目按 id 直接访问返回 403 |
| 响应字段 | 不返回 `fs_path`、`created_by` 等内部标识与服务器路径 |

验证用例见 `packages/api/tests/test_user_isolation.py`：两个用户切换身份，覆盖私有项目、公共项目内的会话、按 id 猜测访问、炼化接口越权等路径。

## 系统架构

```
浏览器（Vue 3 + Pinia）
   │  REST /api/*（会话 Cookie）
   ▼
FastAPI（packages/api）── Redis（会话、活跃用户）
   │
   ├── agent_core（packages/agent-core）：LLM 编排、工具、知识索引
   └── SQLAlchemy → PostgreSQL
```

## 技术栈

- 前端：Vue 3、Vite、Pinia、Vue Router、Tailwind
- 后端：FastAPI、SQLAlchemy 2（async）、Alembic、Redis、Pydantic v2
- 存储：PostgreSQL 16、Redis 7
- 部署：Docker Compose + nginx

## 目录结构

```
packages/
  agent-core/   智能体编排、工具、知识索引（Python 包）
  api/          FastAPI 服务、路由、模型、迁移（alembic/）
  web/          Vue 3 前端（Vite）
agents/         内置智能体与技能定义
knowledge/      全局知识模板与索引
scaffold/       新建项目时复制的测试脚手架
```

## 关键配置

| 变量 | 说明 |
| --- | --- |
| `SECRET_KEY` | 必填，启动时校验；用户字段加密与会话签名都由它派生 |
| `DATABASE_URL` | PostgreSQL 连接串；不设时用 `DB_*` 拼 `postgresql+asyncpg` URL |
| `REDIS_URL` | 会话与事件存储 |
| `PROJECTS_ROOT` | 项目工作区根目录，必须是已存在的绝对路径，且位于仓库之外 |
| `AUTH_BYPASS_ENABLED` | 默认 `false`；多用户环境禁止开启 |
| `CORS_ORIGINS` | 允许的前端来源（本地开发含 5173） |
| `API_DOCS_ENABLED` | 默认 `false`；本地调试需要 Swagger 时设为 `true` |

## 测试

```bash
# 后端（SQLite 文件库跑真实 alembic 迁移链）
cd packages/api && PYTHONPATH=src pytest -q

# 前端（Vitest + Vue Test Utils）
cd packages/web && npm run test -- --run

# 类型检查
cd packages/web && npx vue-tsc --noEmit
```

## 部署上线（Gitee + 服务器）

代码托管在 Gitee（`git@gitee.com:ethan_926/agents-universe.git`），生产以 Docker 镜像运行在云服务器，源码位于服务器的 `/root/agents-universe`。**前端与后端都打进镜像，改代码必须重建镜像**，只传文件不会生效。

日常发布三步：

```bash
# 1. 本地：提交并推送
git add -A && git commit -m "..." && git push origin main

# 2. 服务器：拉取并重建镜像（<server> 换成服务器地址）
ssh root@<server> "cd /root/agents-universe && bash scripts/deploy_server.sh"

# 3. 验证
curl http://<server>:8000/health      # 返回 ok 后浏览器 Ctrl+F5 强刷
```

`scripts/deploy_server.sh` 会依次完成：前置检查（`SECRET_KEY` / `DATABASE_URL` / `AUTH_BYPASS_ENABLED`）→ 核对 SQLite 库文件是否在挂载卷内 → 备份库文件与当前镜像 → `git reset --hard origin/main` → `docker build --target final` → 重启服务 → 健康检查（超时自动回滚镜像与库文件）。

```bash
bash scripts/deploy_server.sh --dry-run   # 只打印将要执行的动作
bash scripts/deploy_server.sh --help
```

两个必须注意的点：

- `--target final` 不能省：`Dockerfile` 最后一个 stage 是 `web-dev` 开发镜像，漏了会建成跑 vite dev 的镜像，线上不可用。
- 服务器 `docker-compose.yml` 的 api 服务只有 `image` 没有 `build` 段，`docker compose build api` 会静默跳过；必须用上面的 `docker build`。

### 升级到本版本（破坏性变更）

1. **数据库**：本版本默认存储改为 PostgreSQL。服务器若仍用 SQLite，必须在服务器 `.env` 显式保留
   `DATABASE_URL=sqlite+aiosqlite:////app/data/<库文件>.db`，否则会去连不存在的 `postgres:5432`。
   要真正切到 PostgreSQL，需另行部署实例并迁移数据（跨库数据不会自动搬运）。
2. **登录**：OAuth/SSO 入口已移除，改为账号密码。`AUTH_BYPASS_ENABLED` 必须为 `false`，
   否则所有请求都归到同一个内置用户。`SECRET_KEY` 不要更换，否则已加密的字段无法解密。
3. **历史数据归属**：新注册账号的 `user_id` 是新的 UUID，与旧的 SSO `sub` / `dev-user` 不同，
   历史项目与会话不会自动出现在新账号下（数据仍在库里）。需要认领时按旧 id 更新
   `projects.created_by` 与 `conversations.user_id`。
4. **前端**：工作台是多面板布局，`main.ts` 依次引入 `index.css`、`styles/layout.css`、`styles/tour.css`、`styles/app.css`；改动后必须重建镜像再强刷，仅重启容器看不到新版。

生产构建会剔除 `console.*` 与 `debugger`；接口错误统一返回中文提示与稳定错误码，不返回堆栈、表名或服务器路径。升级前请备份数据库与 `PROJECTS_ROOT`。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
