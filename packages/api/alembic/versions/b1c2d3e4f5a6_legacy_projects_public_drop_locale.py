"""legacy projects to public + drop user_preferences.locale

Revision ID: b1c2d3e4f5a6
Revises: a9b1c2d3e4f5
Create Date: 2026-11-22

Two changes that go with the multi-user isolation work:

1. Rows created while ``AUTH_BYPASS_ENABLED`` was on are owned by the shared
   ``dev-user`` / ``SYSTEM`` identity. Once real accounts are enabled those
   rows would vanish from every user's sidebar, so they are promoted to
   ``visibility='public'`` — the "公共文件夹" semantics: visible to all,
   while each user's conversations inside them stay private.

2. The UI no longer has a language switch, so the per-user ``locale``
   preference has no consumer.

Offline (``--sql``) compilation skips the data backfill — it needs a live
database — and drops the column with plain DDL so every dialect (including
the offline-compile test) renders valid SQL.
"""
from alembic import context, op
import sqlalchemy as sa


revision = 'b1c2d3e4f5a6'
down_revision = 'a9b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode():
        # Literal statements: portable across MSSQL / SQLite without expanding
        # bindparam support differences in the Alembic op layer.
        op.execute("UPDATE projects SET visibility = 'public' WHERE created_by IS NULL")
        op.execute(
            "UPDATE projects SET visibility = 'public' "
            "WHERE created_by IN ('dev-user', 'SYSTEM')"
        )
    # Offline ``--sql`` mode cannot reflect a table, so batch mode is out;
    # the column carries no index/constraint, so plain DDL is valid there.
    # Live runs use batch mode so SQLite (which cannot ALTER a column away in
    # older builds) still gets a correct table rebuild.
    if context.is_offline_mode():
        op.drop_column('user_preferences', 'locale')
    else:
        with op.batch_alter_table('user_preferences') as batch:
            batch.drop_column('locale')


def downgrade() -> None:
    op.add_column(
        'user_preferences',
        sa.Column('locale', sa.String(length=16), nullable=True),
    )
