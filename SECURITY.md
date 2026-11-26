# 安全策略

## 安全设计

- **账号与口令** — 唯一的登录方式是用户名 + 密码（`/api/auth/register`、`/api/auth/login`）。口令以 PBKDF2-HMAC-SHA256（每用户独立盐，20 万次迭代）单向哈希存储；用户名与显示名以 Fernet 密文落库，另存 HMAC-SHA256 摘要用于唯一约束与登录查找，数据库中不出现明文（`services/user_crypto.py`）。
- **会话** — Redis + HttpOnly Cookie（`SameSite=Lax`，`WEB_BASE_URL` 为 https 时自动加 `Secure`），REST 与 WebSocket 共用，TTL 24 小时随活跃请求滑动续期。会话 id 是不透明随机值，服务端可随时吊销；没有 JWT，也没有 OAuth 状态。
- **密钥加密** — 用户的 LLM API Key、集成 Token 与项目密钥均以 AES-256-GCM 加密存储（`services/token_vault.py`），密钥由 `SECRET_KEY` 派生、不落库；明文永不进入日志、错误信息或模型上下文。
- **启动自检** — `SECRET_KEY` 仍为 `change-me` 占位值时 API 拒绝启动；`AUTH_BYPASS_ENABLED` 在多用户环境必须保持 `false`。
- **服务端隔离** — 所有查询以当前登录用户为前提；跨用户访问返回 404（不确认资源是否存在），私有项目按 id 直接访问返回 403（`tests/test_user_isolation.py`）。
- **出站防护** — 智能体发起的 HTTP 请求经 SSRF 校验（私有/环回/元数据地址、重定向逐跳复检），MCP 服务器带 SSRF 校验、头部黑名单与破坏性工具确认门。
- **沙箱** — 智能体生成的 Python / shell 在审计钩子 + 路径白名单 + 命令校验器下运行（`agent_core/sandbox.py`、`sandbox_guard/sitecustomize.py`）。这是纵深防御而非安全边界：多租户或运行不可信代码时，应把执行层放到独立容器 / microVM（见 README「架构边界」）。
- **最小权限** — 智能体以当前用户身份与该用户的令牌执行，操作可追责；委派（`delegate_agent`）会放宽权限，因此有全局开关、深度上限与目标白名单。

## 报告漏洞

请使用代码托管平台的**私有漏洞报告**功能（仓库页 → Security → Report a vulnerability），不要公开发布未修复漏洞的细节。

我们承诺：

- 收到报告后 **72 小时内**确认
- 确认有效后尽快修复，并在修复后发布安全通告
- 报告期间保持沟通透明

## 建议的部署基线

- 生产环境务必关闭 `AUTH_BYPASS_ENABLED`，使用账号密码登录并经 HTTPS 访问
- 替换默认的 `DB_PASSWORD`，生成随机 `SECRET_KEY`（`openssl rand -hex 32`）且不要泄露、不要随意更换（已加密字段依赖它解密）
- 构建镜像时保持 TLS 校验开启（仅在企业 SSL 拦截代理环境下显式放行，见 Dockerfile 的 `INSECURE_TLS`）
- API 端口不要直接暴露公网，经反向代理 / 网关统一入口
- 升级前备份数据库与 `PROJECTS_ROOT`
