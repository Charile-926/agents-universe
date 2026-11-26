# Agents Universe

自托管的多智能体工作平台。在浏览器里把一群智能体组织成一个团队：Product Owner 拆需求、Tech Lead 写代码、QA 验结果，其余角色（数据分析、渗透测试、办公助手、客服……）按需加入。每个智能体有独立的定义、技能、工作流与知识库；它们共享同一份项目工作区、对话历史和调度系统。

- **单体可部署**：一个 Docker 镜像 = nginx（前端）+ FastAPI（API/WS）+ 进程内调度器，外加 PostgreSQL 与 Redis
- **纯自托管**：模型 API Key 由用户自带（Anthropic / OpenAI / Azure OpenAI / Gemini / 任意 OpenAI 兼容端点），经 AES-256-GCM 加密只存在你自己的数据库里
- **代码可审计**：全部提示词、技能、工作流、知识都是仓库里的 Markdown / YAML，不藏在服务端数据库

---

## 功能特性

| 能力 | 说明 |
|---|---|
| 多智能体协作 | 10 个内置角色；支持智能体间委派子任务 |
| 对话与任务计划 | WebSocket 流式输出、可中断、断线可恢复；复杂任务先出计划再逐项执行 |
| 工具集 | 文件读写、shell/python 执行、Git、浏览器（Playwright）、HTTP、MCP、图表、媒体、委派等 |
| 项目工作区 | 每个项目一份磁盘目录；文件树、预览、上传、ZIP；项目隔离 |
| 知识库 | Markdown + frontmatter，按 slug/标签/交叉引用检索；项目级与全局两级 |
| 仓库知识图谱 | tree-sitter 解析多种编程语言，支持代码依赖查询 |
| 技能与工作流 | 四类技能 + 11 个工作流 |
| 记忆 | 个人长期记忆（跨会话）+ 情节记忆（每轮摘要） |
| 自动化脚本 | workflow / python / bash / playwright 四种脚本，实时日志 |
| 定时任务 | 五字段 cron（时区感知），可触发脚本或智能体回合 |
| 模型路由 | 用户可配多套模型；支持 auto 模式根据复杂度自动路由 |
| 凭据管理 | 用户级集成令牌、项目级密钥、LLM Key；分层加密 |

---

## 快速开始

前置：Docker 与 Docker Compose v2；至少 4 GB 内存。

```bash
# 1. 复制配置示例
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml

# 2. 生成 SECRET_KEY（占位值 change-me 会被拒绝启动）
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env
```

Windows（PowerShell）：
```powershell
Copy-Item .env.example .env
Copy-Item docker-compose.example.yml docker-compose.yml
"SECRET_KEY=$([Convert]::ToHexString((1..32 | ForEach-Object { Get-Random -Max 256 })).ToLower())" | Out-File -Append -Encoding utf8 .env
```

```bash
# 3. 启动服务
docker compose up --build
```

打开 <http://localhost:8000>，在 `/login` 页注册第一个账号。

| 场景 | 命令 | 入口 |
|---|---|---|
| 生产形态 | `docker compose up --build` | <http://localhost:8000> |
| 前端热重载 | `docker compose --profile dev up` | <http://localhost:5173> |

Linux 主机首次启动前需要建好数据目录：
```bash
mkdir -p ./data/projects
sudo chown 997:997 ./data/projects
```

### 本地开发（不用 Docker）

```bash
# 只起依赖服务
docker compose up -d postgres redis

# 安装依赖
pip install -e "packages/agent-core[dev]" -e "packages/api[dev]"

# 启动后端
cd packages/api && PYTHONPATH=src python -m uvicorn api.main:app --port 8000 --reload

# 启动前端
cd packages/web && npm ci && npm run dev
```

Python ≥ 3.12、Node ≥ 22。

---

## 配置

主要环境变量（详见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECRET_KEY` | 必填 | 派生令牌加密、字段加密。更换即丢失已加密数据 |
| `APP_BASE_URL` | `http://localhost:8000` | 对外地址，用于 CORS、Cookie |
| `DB_PASSWORD` | `agents` | PostgreSQL 密码，生产需替换 |
| `REDIS_URL` | `redis://redis:6379/0` | 会话与短期状态 |
| `PROJECTS_ROOT` | `/app/projects` | 项目工作区根目录 |
| `AUTH_BYPASS_ENABLED` | `false` | 免登录旁路，仅本地调试使用 |

---

## 技术架构

### 运行时拓扑

```
浏览器 (Vue 3 SPA)
    │
    ▼
nginx :8000
    ├─ /api、/health → uvicorn 127.0.0.1:8001
    └─ /ws → WebSocket
    │
    ▼
FastAPI (单进程)
    ├─ routers/   参数校验、鉴权、限流
    ├─ services/  agent_turn、scheduler、delegation
    └─ agent-core Agent.run() 循环 + 工具执行
    │
    ├─ PostgreSQL / MySQL / SQLite
    └─ Redis
```

### 三个包的职责

| 包 | 职责 |
|---|---|
| `packages/agent-core` | 纯 Python 库：Agent 循环、provider 适配、工具、知识、沙箱、cron。所有 IO 通过注入的 `ToolContext` |
| `packages/api` | FastAPI 应用：路由、鉴权、会话、持久化、调度循环。不实现智能体推理循环 |
| `packages/web` | Vue 3 SPA：状态、渲染、交互。所有判定以 API 返回为准 |

---

## 目录结构

```
packages/
  agent-core/          纯 Python 库
  api/                 FastAPI 应用
  web/                 Vue 3 前端
agents/                10 个内置智能体定义
workflows/             11 个工作流定义
knowledge/             全局知识库模板
scaffold/              新建项目模板
docker/                预编译 tree-sitter 语法
scripts/               部署脚本
docs/                  实现指南
```

---

## 开发与测试

```bash
# 后端测试
pytest packages/agent-core/tests -q
pytest packages/api/tests -q

# 前端构建与测试
cd packages/web && npm run build
cd packages/web && npm test -- --run
```

---

## 部署

生产推荐一台 Linux 主机：

```bash
# 服务器上
bash scripts/deploy_server.sh --dry-run     # 先看会做什么
bash scripts/deploy_server.sh               # 备份 → 重建镜像 → 重启 → 健康检查 → 失败自动回滚
```

回滚注意：升级会执行 alembic 迁移。SQLite 部署必须"旧镜像 + 旧库文件"一起恢复。

---

## 架构边界与已知限制

1. **沙箱不是安全边界**：智能体生成的代码在审计钩子、路径白名单下运行，防误操作不防刻意逃逸
2. **调度器是进程内的**：单副本部署成立；多副本需要外部调度器
3. **排队任务不持久化**：脚本/定时任务并发上限 64，超出内存队列，重启即丢
4. **智能体权限 = 当前用户权限**
5. **单进程 Python**：长任务会占用 worker
6. **知识检索是关键词 + 结构打分**：不是向量检索
7. **界面固定简体中文**

---

## 文档

- 实现细节：[`docs/agents-universe-implementation-guide.zh-CN.md`](docs/agents-universe-implementation-guide.zh-CN.md)
- 架构审查：[`docs/architecture-review-2026-11.zh-CN.md`](docs/architecture-review-2026-11.zh-CN.md)
- 给编码智能体的约定：[`CLAUDE.md`](CLAUDE.md)
- 贡献流程：[`CONTRIBUTING.md`](CONTRIBUTING.md)
- 安全策略：[`SECURITY.md`](SECURITY.md)
- 变更记录：[`CHANGELOG.md`](CHANGELOG.md)

---

## 许可

Apache License 2.0，见 [`LICENSE`](LICENSE)。