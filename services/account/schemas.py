"""Request/response models. Note what is absent: nothing here carries user content."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from versioning import is_valid

# ==================== Auth ====================


class DeviceInfo(BaseModel):
    platform: str = Field(default="web", max_length=40)
    client_version: str = Field(default="", max_length=40)
    schema_version: int = Field(default=0, ge=0)
    name_ciphertext: str | None = Field(default=None, max_length=2048)


class RegisterRequest(BaseModel):
    email: EmailStr
    auth_key: str = Field(min_length=32, max_length=256, description="Client-derived, never the password")
    wrapped_data_key: str = Field(min_length=16, max_length=4096)
    kdf_params: dict = Field(default_factory=dict)
    device: DeviceInfo = Field(default_factory=DeviceInfo)


class LoginRequest(BaseModel):
    email: EmailStr
    auth_key: str = Field(min_length=32, max_length=256)
    device: DeviceInfo = Field(default_factory=DeviceInfo)


class Session(BaseModel):
    token: str
    account_id: str
    device_id: str
    email: str
    wrapped_data_key: str
    kdf_params: dict
    vault_enabled: bool


class DeviceOut(BaseModel):
    id: str
    platform: str
    client_version: str
    schema_version: int
    cursor: int
    name_ciphertext: str | None
    last_seen_at: datetime
    is_current: bool = False


class Entitlements(BaseModel):
    plan: str  # billing plan: free | pro | lifetime
    status: str  # subscription status: none | trialing | active | past_due | canceled
    features: list[str]
    current_period_end: datetime | None
    cancel_at_period_end: bool
    trial_available: bool
    # Membership type the strategy resolved to (membership.py): free | paid | admin.
    membership: str = "free"
    membership_label: str = "Free"
    max_active_tasks: int | None = None  # None = unlimited


class Me(BaseModel):
    account_id: str
    email: str
    role: str = "user"
    created_at: datetime
    vault_enabled: bool
    devices: list[DeviceOut]
    entitlements: Entitlements


class AccountSettings(BaseModel):
    vault_enabled: bool | None = None


# ==================== Membership ====================


class MembershipInfo(BaseModel):
    """Public description of one membership type (what the pricing/account screens show)."""

    type: str  # anonymous | free | paid | admin
    label: str
    description: str
    requires_account: bool
    max_active_tasks: int | None  # None = unlimited
    features: list[str]
    can_upgrade: bool


class MembershipCatalog(BaseModel):
    memberships: list[MembershipInfo]


# ==================== Admin ====================


class AdminStats(BaseModel):
    accounts_total: int
    accounts_deleted: int
    memberships: dict[str, int]  # free | paid | admin → count (live accounts)
    subscriptions: dict[str, int]  # status → count
    devices_active: int
    sync_ops: int
    sync_bytes: int
    snapshots: int
    snapshot_bytes: int
    latest_release: str | None
    service_version: str
    database: str


class AdminAccountSummary(BaseModel):
    id: str
    email: str
    role: str
    membership: str
    plan: str
    status: str
    current_period_end: datetime | None
    created_at: datetime
    deleted_at: datetime | None
    devices_active: int
    last_seen_at: datetime | None


class AdminAccountPage(BaseModel):
    items: list[AdminAccountSummary]
    total: int
    offset: int
    limit: int


class AdminAuditEntry(BaseModel):
    at: datetime
    actor_account_id: str | None
    actor_email: str | None
    action: str
    detail: dict


class AdminDeviceOut(BaseModel):
    """Device metadata for support — without the (encrypted) device name."""

    id: str
    platform: str
    client_version: str
    schema_version: int
    cursor: int
    created_at: datetime
    last_seen_at: datetime


class AdminAccountDetail(AdminAccountSummary):
    vault_enabled: bool
    provider: str
    cancel_at_period_end: bool
    features: list[str]
    max_active_tasks: int | None
    devices: list[AdminDeviceOut]
    sync_ops: int
    sync_bytes: int
    snapshot_bytes: int | None
    audit: list[AdminAuditEntry]


class AdminMembershipUpdate(BaseModel):
    """Grant/revoke a paid membership by hand (comps, refunds, support). Writes the same
    subscription row a billing webhook would, with provider "admin", and an audit entry."""

    plan: str = Field(pattern="^(free|pro|lifetime)$")
    # Defaults: pro/lifetime → active, free → canceled. "none" makes the trial available again.
    status: str | None = Field(default=None, pattern="^(none|trialing|active|past_due|canceled)$")
    days: int | None = Field(default=None, ge=1, le=3660, description="Period length for pro (default 30)")
    note: str = Field(default="", max_length=500)


class AdminRoleUpdate(BaseModel):
    role: str = Field(pattern="^(user|admin)$")


# ==================== Sync ====================


class PushOp(BaseModel):
    op_id: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(ge=1)
    ciphertext: str = Field(min_length=4, description="base64; opaque to the server")


class PushRequest(BaseModel):
    ops: list[PushOp] = Field(max_length=1000)


class PushedOp(BaseModel):
    op_id: str
    seq: int


class PushResponse(BaseModel):
    accepted: list[PushedOp]
    last_seq: int


class PulledOp(BaseModel):
    seq: int
    op_id: str
    device_id: str
    schema_version: int
    ciphertext: str


class PullResponse(BaseModel):
    ops: list[PulledOp]
    last_seq: int
    cursor: int
    has_more: bool
    snapshot_seq: int | None


class AckRequest(BaseModel):
    cursor: int = Field(ge=0)


class SnapshotIn(BaseModel):
    seq: int = Field(ge=0, description="Ops with seq > this must be applied on top of the snapshot")
    schema_version: int = Field(ge=1)
    ciphertext: str = Field(min_length=4)


class SnapshotOut(BaseModel):
    seq: int
    schema_version: int
    device_id: str
    ciphertext: str
    updated_at: datetime


class SyncStatus(BaseModel):
    last_seq: int
    cursor: int
    pending_for_device: int
    snapshot_seq: int | None
    vault_enabled: bool
    active_devices: int


# ==================== Billing ====================


class CheckoutRequest(BaseModel):
    plan: str = Field(pattern="^(pro_monthly|pro_yearly|lifetime)$")
    return_url: str = Field(default="/", max_length=2048)


class CheckoutResponse(BaseModel):
    checkout_url: str
    provider: str


class StubWebhook(BaseModel):
    """Shape of the built-in sandbox provider's webhook (mirrors real providers' essentials)."""

    event_id: str = Field(max_length=128)
    type: str = Field(max_length=64)  # subscription.created|updated|canceled|payment_failed
    account_id: str
    plan: str = "pro"
    status: str = "active"
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    customer_id: str | None = None
    subscription_id: str | None = None


# ==================== Releases ====================


class ReleaseManifest(BaseModel):
    channel: str = Field(default="stable", max_length=32)
    version: str = Field(max_length=40)
    schema_version: int = Field(ge=1)
    min_supported_version: str = Field(max_length=40)
    min_supported_schema: int = Field(ge=1)
    critical: bool = False
    notes: str = Field(default="", max_length=20000)
    published_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_camel_case(cls, data):
        """release.json (apps/web) uses camelCase; the API speaks snake_case. Accept both."""
        if isinstance(data, dict):
            import re

            data = {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): v for k, v in data.items() if k != "$schema"}
        return data

    @field_validator("version", "min_supported_version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not is_valid(value):
            raise ValueError("must be a semantic version like 1.2.3")
        return value.strip().lstrip("v")


class ClientCheck(BaseModel):
    """What a client gets when it asks 'am I still allowed?'."""

    latest: ReleaseManifest | None
    client_version: str
    client_schema: int
    update_available: bool
    update_required: bool
    reason: str | None
