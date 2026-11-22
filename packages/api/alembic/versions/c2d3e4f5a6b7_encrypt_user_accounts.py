"""user_accounts: 明文用户名 -> 加密字段

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-22

账号表改为"密文 + 摘要"存储：

- ``username_hash``：HMAC-SHA256 摘要（唯一约束与登录查找）
- ``username_enc`` / ``display_name_enc``：Fernet 密文
- ``password_hash``：PBKDF2 单向哈希（未变）

实现方式为"读出 → 重建表 → 写回"而不是 ALTER：SQLite 无法可靠地删除带唯一
约束的列，MSSQL 又要求先删约束，重建表是唯一在三种方言上都成立的路径，同时
保留已有账号。离线 ``--sql`` 模式只输出 DDL，不做数据迁移。
"""
import sqlalchemy as sa
from alembic import context, op


revision = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def _create_new_table() -> None:
    op.create_table(
        'user_accounts',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('username_hash', sa.String(length=64), nullable=True),
        sa.Column('username_enc', sa.Text(), nullable=True),
        sa.Column('display_name_enc', sa.Text(), nullable=True),
        sa.Column('password_hash', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('username_hash', name='uq_user_account_username_hash'),
    )


def _create_legacy_table() -> None:
    op.create_table(
        'user_accounts',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('username', sa.String(length=100), nullable=False),
        sa.Column('password_hash', sa.String(length=500), nullable=False),
        sa.Column('display_name', sa.Unicode(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('username', name='uq_user_account_username'),
    )


def upgrade() -> None:
    if context.is_offline_mode():
        op.drop_table('user_accounts')
        _create_new_table()
        return

    from api.services.user_crypto import encrypt_text, username_digest

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT user_id, username, password_hash, display_name, created_at "
            "FROM user_accounts"
        )
    ).mappings().all()

    op.drop_table('user_accounts')
    _create_new_table()

    for row in rows:
        username = (row['username'] or '').strip()
        if not username:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO user_accounts "
                "(user_id, username_hash, username_enc, display_name_enc, password_hash, created_at) "
                "VALUES (:user_id, :username_hash, :username_enc, :display_name_enc, :password_hash, :created_at)"
            ),
            {
                'user_id': row['user_id'],
                'username_hash': username_digest(username),
                'username_enc': encrypt_text(username),
                'display_name_enc': encrypt_text(row['display_name'] or username),
                'password_hash': row['password_hash'],
                'created_at': row['created_at'],
            },
        )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_table('user_accounts')
        _create_legacy_table()
        return

    from api.services.user_crypto import decrypt_text

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT user_id, username_enc, password_hash, display_name_enc, created_at "
            "FROM user_accounts"
        )
    ).mappings().all()

    op.drop_table('user_accounts')
    _create_legacy_table()

    for row in rows:
        username = decrypt_text(row['username_enc'])
        if not username:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO user_accounts "
                "(user_id, username, password_hash, display_name, created_at) "
                "VALUES (:user_id, :username, :password_hash, :display_name, :created_at)"
            ),
            {
                'user_id': row['user_id'],
                'username': username,
                'password_hash': row['password_hash'],
                'display_name': decrypt_text(row['display_name_enc']) or username,
                'created_at': row['created_at'],
            },
        )
