"""ad sources: creative kind (internal/sponsored/affiliate) + runtime operator settings

Revision ID: 0004_ad_sources
Revises: 0003_ads
Create Date: 2026-12-15 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_ad_sources"
down_revision = "0003_ads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ad_campaigns") as batch:
        batch.add_column(sa.Column("kind", sa.String(length=16), nullable=False, server_default="sponsored"))
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    with op.batch_alter_table("ad_campaigns") as batch:
        batch.drop_column("kind")
