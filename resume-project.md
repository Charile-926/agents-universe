# 简历项目段（按 Smart-agent 模板句式熔炼，融合面试稿口径）

## 基于多智能体协同与工程确定性的企业级 AI Agent 平台

**GitHub：** https://github.com/XieZhiWen-926/agents-universe.git（按实际仓库地址替换）

**项目简介：**
针对企业落地大模型时"开发者框架门槛高、经验锁在个人会话无法沉淀，低代码平台又缺身份权限与可审计轨迹"的两极困局，独立设计并实现一套企业级开源 Agent 平台。针对模型在开放环境下"选错工具、参数幻觉、跨工具死循环、上下文变长即丢信息"的概率性问题，以工程确定性兜住模型不确定性：融合多 LLM 统一接入、Plan-and-Execute 任务树拆解、ReAct 工具调用循环与多智能体嵌套委派，覆盖研发协同（需求→编码→自动化测试）、业务知识问答、办公文档交付三大场景，全异步架构支撑 Docker 一键自托管。

**技术栈：**
Python 3.12、FastAPI、SQLAlchemy 2.x（异步）、Alembic、Redis 7、SQL Server / PostgreSQL / MySQL / SQLite、Vue 3.5 + TypeScript + Vite + Pinia、WebSocket、Playwright、Docker Compose、Nginx、MCP（Model Context Protocol）、Anthropic / OpenAI / Azure OpenAI / Gemini

**技术亮点：**

- **架构与全异步工程：** Monorepo 拆分 `agent-core`（纯 Python 编排引擎）/ `api`（FastAPI）/ `web`（Vue 3）三层；提供商适配器统一接入 4 家 LLM，按轻量/标准/旗舰三档做复杂度自动路由与故障回退；会话热数据入 Redis（TTL）、完整历史与任务持久化数据库，多实例部署不依赖粘性会话；WebSocket 全双工流式推送思考与工具轨迹，Nginx 反代 + Docker 多阶段构建一键拉起多服务。

- **Agent 推理与工具容错：** 主干用 Plan-and-Execute（`plan_task` 拆任务树，执行中可重规划、支持运行时用户插话注入），子任务内跑 ReAct 循环、关键产出经校验闭环；工具层做三层防护——参数 schema 校验失败即把错误字段回喂模型自我修正（限 1–2 次）、工具异常结构化回填让模型换方案而非中断、连续 A→B→A→B 调用模式识别即熔断；并行 tool_calls 异步并发、独立捕获异常、按 RPM 限并发；业务含义错误叠加业务规则校验与高风险操作关键实体回显确认。

- **知识上下文工程（差异化设计）：** 刻意不依赖向量召回，采用"项目选中即全量加载 + Markdown `[[slug]]` 双向交叉引用"的显式知识工程；primary 条目全文入上下文、detail 条目仅索引摘要并由 `knowledge_rw` 按需 load/unload，配合 L0 上下文→L7 全局知识分层记忆与阈值触发的滚动摘要压缩，在十万字级项目文档下既保上下文完整又控 token 预算；知识变更带版本留档、`[stale]` 显式遗忘，全程可审计。

- **多智能体协同与安全边界：** 内置 PO / Tech Lead / QA / 数据分析 / 办公助手等 10 个角色，通过 @提及单轮路由与 `delegate_agent` 同 turn 嵌套委派（事件 fail-closed 白名单、深度上限、会话锁串行化）串起专业化分工；30+ 工具经 Streamable HTTP + SSE 兜底接入外部 MCP Server；双层沙箱（命令白名单 + CPython audit hook 文件守卫）拦截路径逃逸，出站 SSRF 校验、破坏性工具 `user_confirm` 二次确认（定时任务非交互模式直接降级跳过）、密钥 AES-256-GCM 加密且明文不进 LLM 上下文；数据按 `project_id`/`user_id` 严格隔离。

**项目成果：**
上线后任务成功率从约 78% 提升并稳定在 92%，工具调用失败率从 12% 降至 4%，流式交互首 token 延迟控制在 1 秒级；成效主要来自参数回喂自我修正、结构化错误换方案、规划前置三项优化；一套代码支持 4 种数据库方言与 4 家 LLM 一键切换，单项目承载万级知识条目与十万字级文档全量上下文，10 个智能体、11 条工作流、30+ 工具开箱即用。
