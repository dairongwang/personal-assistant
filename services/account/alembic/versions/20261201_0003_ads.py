"""sponsored content: ad campaigns + anonymous daily counters

Revision ID: 0003_ads
Revises: 0002_membership
Create Date: 2026-12-01 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_ads"
down_revision = "0002_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_campaigns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sponsor", sa.String(length=80), nullable=False),
        sa.Column("headline", sa.String(length=90), nullable=False),
        sa.Column("blurb", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("cta_label", sa.String(length=30), nullable=False, server_default="Learn more"),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("image_url", sa.String(length=2048), nullable=True),
        sa.Column("regions", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("languages", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("max_impressions", sa.Integer(), nullable=True),
        sa.Column("impressions_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("clicks_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ad_daily_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ad_id", sa.String(length=36), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("region", sa.String(length=2), nullable=False, server_default="ZZ"),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["ad_id"], ["ad_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ad_id", "day", "region", name="uq_ad_daily_stat"),
    )
    op.create_index("ix_ad_daily_stats_day", "ad_daily_stats", ["day"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ad_daily_stats_day", table_name="ad_daily_stats")
    op.drop_table("ad_daily_stats")
    op.drop_table("ad_campaigns")
