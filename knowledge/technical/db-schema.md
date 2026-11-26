---
slug: "technical/db-schema"
title: "Database Schema 与迁移约定"
category: "technical"
tags: ["database", "sqlalchemy", "alembic", "schema"]
---

# Database Schema 与迁移约定

**权威来源**：物理结构以 `packages/api/src/api/models/*.py` 与
`packages/api/alembic/versions/` 迁移链为准。本文只保留两类信息——跨方言的
**类型映射 / 迁移规则**，以及**表的职责清单**（帮你判断该改哪张表）。
逐列字段不再抄写：抄一遍就会过期一遍。

## 支持的方言

全部一等公民，按 `DATABASE_URL` 切换（优先级链见
`packages/api/src/api/config.py`）：

| 方言 | 应用驱动（异步） | Alembic 驱动（同步） | 说明 |
|---|---|---|---|
| PostgreSQL | `postgresql+asyncpg` | `postgresql+psycopg2` | **默认生产方言**：`DB_*` 字段拼 URL；CI 的 live 测试也是它 |
| SQL Server | `mssql+aioodbc` | `mssql+pyodbc` | 遗留部署（`MSSQL_CONNECTION_STRING` 回退） |
| MySQL | `mysql+aiomysql` | `mysql+pymysql` | 建库用 `utf8mb4` + `utf8mb4_0900_bin`（slug 唯一性区分大小写） |
| SQLite | `sqlite+aiosqlite` | `sqlite` | 单机部署与测试；测试会话每次跑真实 alembic 链 |

## 类型映射（模型 → 物理列）

| 模型（可移植） | SQL Server | PostgreSQL | MySQL | SQLite |
|---|---|---|---|---|
| `String(36)` 主键，客户端 `uuid4()` | UNIQUEIDENTIFIER（不用 NEWID） | VARCHAR(36) | VARCHAR(36) | VARCHAR(36) |
| `String(n)` | VARCHAR(n) | VARCHAR(n) | VARCHAR(n) | VARCHAR(n) |
| `Unicode(n)` | NVARCHAR(n) | VARCHAR(n) | VARCHAR(n) | VARCHAR(n) |
| `UnicodeText` | NTEXT* | TEXT | LONGTEXT | TEXT |
| `UTCDateTime`（naive UTC） | DATETIME2 | TIMESTAMP WITHOUT TIME ZONE | DATETIME | DATETIME |
| `Boolean` | BIT | BOOLEAN | TINYINT(1) | BOOLEAN |
| `Integer` | INT | INTEGER | INTEGER | INTEGER |

\* SQLAlchemy 的 mssql 方言把 `UnicodeText` 映射成 NTEXT；需要 MAX 语义时用
`NVARCHAR(MAX)` 变体。时间一律存 **naive UTC**（`models/_compat.py` 的
`UTCDateTime` / `now_utc`），显示层再转本地时区——不要写 `datetime.now()`。

## 迁移规则（保证四方言可编译）

- T-SQL 专属 DDL（`ALTER COLUMN`、`NEWID()`、`ADD CONSTRAINT ... DEFAULT`）放在
  `if op.get_bind().dialect.name == "mssql":` 分支内。
- SQLite 没有 `ALTER COLUMN` / 命名约束 DDL —— 用 `op.batch_alter_table`，
  唯一约束改用命名唯一**索引**。
- 触碰存量数据的回填用 `context.is_offline_mode()` 守卫，`alembic upgrade --sql` 才能编译。
- MySQL：匿名约束会被引擎改名（`projects_ibfk_2` …），删列/删索引前先用
  `sa.inspect(bind).get_foreign_keys()` 解析（错误 1553/1828，只在没有别的索引
  服务该 FK 时触发）；TEXT/BLOB 不接受字面 `DEFAULT`（错误 1101）——ORM 侧有
  客户端默认值时就跳过 server default。离线反射守卫用
  `op.get_context().as_sql` 跳过 FK 解析。
- MySQL 引擎跑在 READ COMMITTED（`api/database.py`）：其默认 REPEATABLE READ 会在
  事务首读时快照，看不见其他会话的提交（PG / SQL Server 默认 READ COMMITTED）。
- 迁移文件头部的 Revision ID / Create Date 必须由 alembic 生成；`down_revision`
  不要手改，冲突会让 `upgrade head` 变成多 head。

## 表清单（职责视角）

### 账号与凭据
| 表 | 职责 | 关键点 |
|---|---|---|
| `user_accounts` | 本地账号 | 只存 `username_hash`（HMAC，唯一）+ `username_enc` / `display_name_enc`（Fernet）+ `password_hash`（PBKDF2）；库里没有明文 |
| `user_tokens` | 集成服务令牌（git / jira …） | `(user_id, service_key)` 唯一；`encrypted_value` AES-256-GCM；`key_hint` 仅末 4 位 |
| `user_api_keys` | LLM 供应商 Key | `(user_id, provider)` 唯一；可带 `base_url`（自定义端点） |
| `user_model_configs` | 用户模型配置（多条目 / 供应商） | `complexity_tier` 驱动 auto 路由；`context_window` / `thinking_enabled` / `reasoning_effort` 为覆盖项 |
| `user_tier_models` | 旧版"档位 → 模型"映射 | **已废弃**，仅向后兼容读取；新代码写 `user_model_configs` |
| `user_preferences` | 用户级 UI 状态 | `onboarding_completed` 等 |

### 项目与协作
| 表 | 职责 | 关键点 |
|---|---|---|
| `projects` | 项目树 | 全局唯一 `slug`；`parent_id` 自引用；`visibility=public/private` |
| `project_members` | 私有项目白名单 | 创建者有隐式权限，不落此表；复合主键 `(project_id, user_id)` |
| `project_secrets` | 项目级密钥 | `(project_id, service_key, environment, secret_name)` 唯一；AES-256-GCM |
| `project_deletion_jobs` | 工作区删除 / 回收状态机 | 落盘路径 + `status` + `attempt_count`，重启后可恢复 |
| `agents` | 智能体登记（全局或项目内） | `definition_path` 指向 `agents/*.agent.md`；`model_*` 列已不参与运行 |

### 对话与运行
| 表 | 职责 | 关键点 |
|---|---|---|
| `conversations` | 会话 | 带 `publish_id` / 列表轮询的索引；`token_budget` / `tokens_used` |
| `messages` | 消息（含 tool_calls、知识引用） | 完成态以 `Message` 行为准 |
| `conversation_runs` | 每轮对话的持久记录 | `running → completed / failed / interrupted`；`streaming_snapshot` 用于中断恢复 |
| `agent_tasks` | 计划中的子任务 | `parent_task_id` 自引用；`depends_on` / `tools_needed` 是 JSON 字符串（受列宽限制，写入时截断） |
| `task_events` | 任务时间线事件 | `(conversation_id, sequence)` 唯一 |

### 知识、记忆与脚本
| 表 | 职责 | 关键点 |
|---|---|---|
| `knowledge_metadata` | 知识条目元数据 | `(project_id, category, slug)` 语义唯一；`content_hash` 去重；`knowledge_level` / `parent_slug` / `depth` 表达层级；**不存 embedding** |
| `knowledge_versions` | 知识内容版本 | `knowledge_id` 级联删除 |
| `knowledge_load_events` | 知识装载审计 | `load / unload`、`turn_number`、触发原因 |
| `personal_memories` | 个人长期记忆 | `project_id` 为 NULL = 全局；关键字标签 |
| `episodic_memories` | 会话级情节记忆 | 摘要 + key_findings + open_questions |
| `automation_scripts` | 项目脚本 | `script_type`：workflow / python / bash / playwright |
| `script_runs` | 脚本运行记录与日志 | 实时日志 WebSocket 与"有运行中脚本禁止删项目"都读它 |

### 调度、集成与发布
| 表 | 职责 | 关键点 |
|---|---|---|
| `scheduled_tasks` | cron 任务 | `target_type`：script / playwright / agent；`conversation_id` 故意不做外键（任务要活过会话删除） |
| `scheduled_task_runs` | 每次触发的记录 | script / playwright 运行额外复用 `script_runs` |
| `mcp_servers` | MCP 服务器注册表 | 全局行（`project_id` NULL）+ 项目行（从项目知识文件懒同步）；`(project_id, slug)` 唯一性在应用层保证 |
| `agent_publishes` | Agent-as-a-Service 发布 | 绑定发布者的 `model_config_id`；对外 SSE API |
| `publish_keys` | 发布专用 API Key | **只存 SHA-256 哈希**；明文只在签发时展示一次 |

## 写入约定

- 主键一律 `String(36)` + 客户端 `uuid4()`；不用自增 / `IDENTITY` / `NEWID()`。
- 新建表必须带 `created_at`（`UTCDateTime`）；需要审计的加 `created_by`。
- 敏感字段只能经 `services/user_crypto.py`（用户名类）或
  `services/token_vault.py`（令牌类）加解密，不要在业务代码里直接调 `cryptography`。
- 新增用户数据表：所有查询路径都要带 `user_id` 过滤，跨用户访问返回 404。

## Related Knowledge

- [[system/framework-overview]]
