"""DATABASE_URL 优先级与默认 URL 的回归测试。

Settings 用显式参数构造（init 参数优先级高于环境变量），避免影响全局
get_settings() 缓存与测试库的 TEST_DATABASE_URL。
"""
from api.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        test_database_url="",
        database_url="",
        mssql_connection_string="",
        db_host="127.0.0.1",
        db_port=5432,
        db_name="agentsuniverse",
        db_user="agents",
        db_password="agents",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_default_url_is_postgresql_built_from_db_fields():
    """无覆盖时用 DB_* 字段拼 PostgreSQL URL。"""
    s = _settings()
    assert s.effective_db_url == (
        "postgresql+asyncpg://agents:agents@127.0.0.1:5432/agentsuniverse"
    )


def test_password_is_url_quoted():
    s = _settings(db_password="p@ss word")
    assert "p%40ss+word" in s.effective_db_url


def test_database_url_beats_legacy_string_and_db_fields():
    s = _settings(
        database_url="postgresql+asyncpg://u:p@dbhost:5432/appdb",
        mssql_connection_string="mssql+aioodbc://legacy:legacy@oldhost/db?driver=X",
    )
    assert s.effective_db_url == "postgresql+asyncpg://u:p@dbhost:5432/appdb"


def test_legacy_connection_string_is_last_resort():
    s = _settings(mssql_connection_string="mysql+aiomysql://u:p@h/db")
    assert s.effective_db_url == "mysql+aiomysql://u:p@h/db"


def test_test_database_url_still_wins_everything():
    s = _settings(
        test_database_url="sqlite+aiosqlite:///tmp/test.db",
        database_url="postgresql+asyncpg://u:p@h/db",
        mssql_connection_string="mssql+aioodbc://legacy",
    )
    assert s.effective_db_url == "sqlite+aiosqlite:///tmp/test.db"
