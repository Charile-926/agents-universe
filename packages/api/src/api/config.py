"""Application settings loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py is installed at /app/src/api in Docker and lives four levels below
# the repository root during local development. Prefer a directory containing
# the bundled agent definitions, then fall back to the package parent.
def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "agents").is_dir():
            return candidate
    return here.parents[2]


_PROJECT_ROOT = _find_project_root()
_ENV_FILES = (str(_PROJECT_ROOT / ".env"), ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    # Database — PostgreSQL 是默认存储（账号、项目、会话、工具调用记录）。
    # 完整的 DATABASE_URL 优先级最高；未设置时用下面这些字段拼 postgresql+asyncpg。
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "agentsuniverse"
    db_user: str = "agents"
    db_password: str = "agents"

    # DB connection pool (tune for expected concurrent users)
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30

    # 兼容旧部署的整串连接（例如迁移前的 SQL Server 实例）。仅在 DATABASE_URL
    # 未设置时作为回退，值可以是任意 SQLAlchemy URL。
    mssql_connection_string: str = ""

    # 完整连接 URL，优先级最高。默认形态：postgresql+asyncpg://user:pass@host:5432/db
    database_url: str = ""

    # Security
    secret_key: str = "change-me-32-char-secret-key-here!"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""

    # App URLs
    app_base_url: str = "http://localhost:8000"
    app_root_path: str = ""
    web_base_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173"

    # Test override
    test_database_url: str = ""

    # Project workspaces — required outside tests. Each project lives under this root.
    projects_root: str = ""

    # Uploads. This is the enforced ceiling; the web composer keeps a matching
    # advisory constant (Composer.vue MAX_UPLOAD_MB) for the pre-upload check —
    # keep the two in sync when changing the limit.
    max_upload_size_mb: int = 10

    # Scheduled tasks. The scheduler is an in-process asyncio loop — one
    # replica only (see services/scheduler.py). Timezone is the default for
    # newly created tasks; each task stores its own.
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    # Agent-to-agent delegation: an agent that lacks a capability hands the
    # subtask to another agent (see services/delegation.py). Delegation is a
    # privilege-escalation surface — a narrow agent can borrow a broad one's
    # tools — so it has a kill switch, and any target agent may narrow its own
    # targets with `delegates_to:` frontmatter.
    agent_delegation_enabled: bool = True
    # Depth counts nested turns: 2 allows root -> child -> grandchild, 0 disables.
    agent_delegation_max_depth: int = 2
    agent_delegation_timeout_seconds: int = 1800


    # Atlassian (base URL is system-level; tokens are per-user in DB)
    # Cloud:  https://example.atlassian.net  → basic auth, no sub-paths
    # Server: https://jira.example.com       → bearer auth, /jira + /confluence sub-paths
    atlassian_base_url: str = ""

    # Git (base URL is system-level; token is per-user in DB)
    # All other git settings are derived from git_base_url — see properties below.
    git_base_url: str = "https://github.com"
    git_ssl_verify: bool = False
    atlassian_ssl_verify: bool = False

    # TLS / SSL for outbound LLM provider requests.
    llm_ssl_verify: bool = False

    # TLS / SSL for Playwright browser requests (set False to ignore self-signed certs).
    browser_ssl_verify: bool = True

    # Network policy for Python executed by agents (code_executor):
    # "" (default) -> allow all network (Playwright downloads, screen recording, ...);
    # "localhost"  -> loopback only; "none" -> all sockets blocked.
    sandbox_network: str = ""

    # System default model (OpenAI-compatible fallback when user has no configs)
    system_default_model_id: str = ""
    system_default_base_url: str = ""
    system_default_api_key: str = ""

    # Outbound HTTP proxy (applied to all LLM provider calls at startup)
    https_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1"
    # Extra NO_PROXY entries from .env (won't be overridden by system env NO_PROXY)
    app_no_proxy: str = ""

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "human"

    # OpenAPI 文档（/docs、/redoc、/openapi.json）。默认关闭：接口清单与
    # 内部路径不应暴露给普通用户；本地调试时显式设为 true。
    api_docs_enabled: bool = False

    # Auth bypass (local dev only — never enable in production)
    auth_bypass_enabled: bool = False
    auth_bypass_user_id: str = "SYSTEM"

    # Cookie 安全属性：None → 由 web_base_url 推导（https ⇒ Secure cookie）。
    # 显式 true/false 覆盖推导结果。会话是 Redis + HttpOnly 不透明 Cookie，
    # 没有 JWT / OAuth 状态需要签名。
    cookie_secure_override: bool | None = None

    @property
    def cookie_secure(self) -> bool:
        if self.cookie_secure_override is not None:
            return self.cookie_secure_override
        return self.web_base_url.startswith("https://")

    # Session (Redis-backed)
    session_ttl: int = 86400       # 24 hours
    auth_cookie_name: str = "x-auth-token"
    active_users_window: int = 300  # 5-minute active-user window

    @property
    def effective_redis_url(self) -> str:
        if not self.redis_password:
            return self.redis_url
        from urllib.parse import urlparse, urlunparse
        p = urlparse(self.redis_url)
        host_part = f":{quote_plus(self.redis_password)}@{p.hostname}"
        if p.port:
            host_part += f":{p.port}"
        return urlunparse(p._replace(netloc=host_part))

    @property
    def _atlassian_is_cloud(self) -> bool:
        return self.atlassian_base_url.rstrip("/").endswith(".atlassian.net")

    @property
    def atlassian_auth_type(self) -> str:
        return "basic" if self._atlassian_is_cloud else "bearer"

    @property
    def atlassian_jira_base_path(self) -> str:
        return "" if self._atlassian_is_cloud else "/jira"

    @property
    def atlassian_confluence_base_path(self) -> str:
        return "" if self._atlassian_is_cloud else "/confluence"

    @property
    def git_provider(self) -> str:
        return "github"

    @property
    def git_api_base_path(self) -> str:
        # Public GitHub uses https://api.github.com directly (no sub-path needed).
        # GitHub Enterprise Server exposes the REST API under /api/v3.
        if self.git_base_url.rstrip("/") == "https://github.com":
            return ""
        return "/api/v3"

    @property
    def git_web_base_path(self) -> str:
        return ""

    @property
    def git_commit_search_mode(self) -> str:
        return "by-jira-key"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_db_url(self) -> str:
        if self.test_database_url:
            return self.test_database_url
        if self.database_url:
            return self.database_url
        # DB_* 字段拼 PostgreSQL URL（默认存储形态）。
        # mssql_connection_string 是旧部署的回退覆盖。
        if self.mssql_connection_string:
            return self.mssql_connection_string
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
