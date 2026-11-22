"""add user_accounts (local username/password auth)

Revision ID: a9b1c2d3e4f5
Revises: f4a8b2c9d517
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9b1c2d3e4f5'
down_revision = 'f4a8b2c9d517'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_accounts',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('username', sa.String(length=100), nullable=False),
        sa.Column('password_hash', sa.String(length=500), nullable=False),
        sa.Column('display_name', sa.Unicode(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('username', name='uq_user_account_username'),
    )


def downgrade() -> None:
    op.drop_table('user_accounts')