"""payments: when a refund happened (the admin payment analytics list refunds by date)

Revision ID: 0006_payment_refunded_at
Revises: 0005_payments
Create Date: 2027-01-10 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_payment_refunded_at"
down_revision = "0005_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("refunded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("refunded_at")
