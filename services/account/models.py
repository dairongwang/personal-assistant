"""Database model of the Account & Sync Service.

Only what billing and multi-device sync strictly need (PLATFORM_DESIGN.md §1.4). No content:
``sync_ops.ciphertext`` / ``sync_snapshots.ciphertext`` are opaque bytes encrypted on the
user's device with a key the server never sees. ``tests/test_privacy.py`` guards this list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """Naive-UTC in the database, timezone-aware UTC in Python (portable SQLite/Postgres)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


ROLE_USER = "user"
ROLE_ADMIN = "admin"  # production support & management (membership.AdminMembership)
ROLES = (ROLE_USER, ROLE_ADMIN)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    # user | admin. Membership type (free/paid/admin) is derived: role first, then subscription.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_USER)
    # Argon2/PBKDF2 of the client-derived *auth key* (never the password). Salted again here.
    auth_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # The user's data key, wrapped on the device with a key derived from the password.
    # Opaque to us; lets a second device unwrap it after proving the password.
    wrapped_data_key: Mapped[str] = mapped_column(Text, nullable=False)
    kdf_params: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # pinned, versioned
    # Opt-in durable encrypted snapshot ("vault"); see sync_snapshots.
    vault_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Monotonic per-account sequence: the server is the master of op ordering.
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(40), nullable=False, default="web")
    client_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # last seq applied
    token_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # bump = sign out
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")  # free|pro|lifetime
    # trialing | active | past_due | canceled | none
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_billing_event"),)


class AuditLog(Base):
    """Who did what to which account from the admin panel (membership changes, sign-outs,
    deletions, role changes). Metadata only — never user content."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    actor_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # None = pipeline/CLI
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")  # JSON, small


class SyncOp(Base):
    """One encrypted operation relayed between a user's devices. Deleted once every active
    device has acknowledged it, or after SYNC_OP_TTL_DAYS — whichever comes first."""

    __tablename__ = "sync_ops"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False)
    op_id: Mapped[str] = mapped_column(String(64), nullable=False)  # client UUID → idempotent pushes
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "seq", name="uq_sync_seq"),
        UniqueConstraint("account_id", "op_id", name="uq_sync_op"),
        Index("ix_sync_ops_account_seq", "account_id", "seq"),
    )


class SyncSnapshot(Base):
    """Latest encrypted full snapshot per account (vault). Lets a new device — or a device that
    resets its local cache — rebuild from the server, which is the master copy."""

    __tablename__ = "sync_snapshots"

    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)  # ops with seq > this still apply
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Release(Base):
    """Version-controlled release manifest: what the deploy pipeline published last."""

    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="stable")
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    min_supported_version: Mapped[str] = mapped_column(String(40), nullable=False)
    min_supported_schema: Mapped[int] = mapped_column(Integer, nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("channel", "version", name="uq_release_channel_version"),)
