"""membership: account role + admin audit log

Revision ID: 0002_membership
Revises: 0001_initial
Create Date: 2026-11-01 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_membership"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:  # batch: SQLite cannot ALTER in place
        batch.add_column(sa.Column("role", sa.String(length=16), nullable=False, server_default="user"))
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("actor_account_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_account_id", sa.String(length=36), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_target_account_id"), "audit_log", ["target_account_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_target_account_id"), table_name="audit_log")
    op.drop_table("audit_log")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("role")
