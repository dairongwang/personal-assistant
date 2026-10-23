"""payments ledger: charges, failures and refunds per account (prorated refund on cancellation)

Revision ID: 0005_payments
Revises: 0004_ad_sources
Create Date: 2027-01-05 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_payments"
down_revision = "0004_ad_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="succeeded"),
        sa.Column("refunded_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refund_id", sa.String(length=128), nullable=True),
        sa.Column("failure_reason", sa.String(length=256), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_id"),
    )
    op.create_index("ix_payments_account_id", "payments", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_payments_account_id", table_name="payments")
    op.drop_table("payments")
