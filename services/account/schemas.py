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
    status: str  # subscription status: none | trialing | active | past_due | canceled | paused
    features: list[str]
    current_period_end: datetime | None
    cancel_at_period_end: bool
    trial_available: bool
    # Which payment strategy holds the subscription (stripe | paypal | seed | admin | …).
    provider: str = ""
    # Until when the paid membership lasts as things stand: trial end, next renewal, the end of
    # a canceled period, or the end of the dunning grace. None = Lifetime / nothing paid.
    access_until: datetime | None = None
    # Renews automatically at access_until (active Pro without a pending cancellation).
    renews: bool = False
    # POST /billing/cancel ends the membership now and refunds (GET /billing/cancel/preview says how much).
    can_cancel: bool = False
    # POST /billing/manage opens the provider's self-service page (payment method, invoices).
    can_manage: bool = False
    # The most recent charge (paid, failed or refunded) — what the account screen reports.
    last_payment: PaymentOut | None = None
    # Membership type the strategy resolved to (membership.py): free | paid | admin.
    membership: str = "free"
    membership_label: str = "Free"
    max_active_tasks: int | None = None  # None = unlimited


class PaymentOut(BaseModel):
    """One charge from the ledger (models.Payment) as the account screen lists it."""

    id: int
    provider: str
    plan: str  # pro_monthly | pro_yearly | lifetime
    amount: str  # decimal string, e.g. "10.00"
    currency: str
    status: str  # succeeded | failed | refunded | partially_refunded
    refunded: str  # decimal string
    refund_id: str | None
    refunded_at: datetime | None
    failure_reason: str | None
    period_start: datetime | None
    period_end: datetime | None
    paid_at: datetime


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
    # Advertising is managed in the separate ads admin console (apps/ads-admin); "" = not configured.
    ads_admin_url: str = ""


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
    # Payment method (payments.py strategy key): stripe | paypal. None = the default one.
    method: str | None = Field(default=None, pattern="^[a-z_]{1,32}$")


class CheckoutResponse(BaseModel):
    checkout_url: str
    provider: str


class PaymentMethodInfo(BaseModel):
    """One payment method as the client's "Subscribe" UI shows it (payments.py strategy)."""

    name: str  # stripe | paypal
    label: str
    description: str
    plans: list[str]  # which of pro_monthly | pro_yearly | lifetime this method sells
    sandbox: bool = False  # nothing is charged (the provider's test mode or the built-in fake)
    fake: bool = False  # the built-in fake of this provider (fakes.py) answers instead of the real one
    available: bool = True  # False = credentials missing on this server; checkout answers 503


class ManageRequest(BaseModel):
    return_url: str = Field(default="/", max_length=2048)


class ManageResponse(BaseModel):
    url: str
    provider: str


class PlanInfo(BaseModel):
    """One paid membership as sold: price (config PRICE_*) and refund rule."""

    id: str  # pro_monthly | pro_yearly | lifetime
    title: str
    amount: str  # decimal string in `currency`, e.g. "1.00"
    amount_cents: int
    currency: str
    interval: str | None  # month | year | None = one-off
    period_days: int | None  # nominal length of one paid period (None = Lifetime)
    refund_rule: str  # human-readable: what a cancellation gives back


class PaymentMethods(BaseModel):
    methods: list[PaymentMethodInfo]
    default: str | None  # pre-selected method, None when no method is configured yet
    plans: list[PlanInfo] = []
    currency: str = "USD"


class RefundPreview(BaseModel):
    """What cancelling right now gives back (GET /billing/cancel/preview)."""

    amount: str  # decimal string
    amount_cents: int
    currency: str
    basis: str  # prorated | flat | none
    days_left: float | None = None  # prorated: unused days …
    days_total: float | None = None  # … of the paid period
    paid: str | None = None  # the payment the refund comes out of
    note: str = ""


class CancelResult(BaseModel):
    entitlements: Entitlements
    refund: RefundPreview
    refund_id: str | None = None  # the provider's refund id (None = nothing to refund)
    refunded_via: str = "none"  # provider | recorded (no usable provider: ledger only) | none


class PaymentDetails(BaseModel):
    """The money side of a provider event: one charge (or its failure / refund)."""

    provider_payment_id: str = Field(max_length=128)  # Stripe payment intent/charge, PayPal capture/sale
    amount_cents: int = 0
    currency: str = "USD"
    plan: str | None = None  # pro_monthly | pro_yearly | lifetime (None = work it out)
    period_start: datetime | None = None
    period_end: datetime | None = None
    paid_at: datetime | None = None
    failure_reason: str | None = None
    refunded_cents: int | None = None  # payment.refunded: the amount (see refund_is_total)
    refund_is_total: bool = True  # True = cumulative total refunded (Stripe); False = this refund only (PayPal)
    refund_id: str | None = None


class PaymentEvent(BaseModel):
    """Provider-neutral billing event: what every payment strategy turns its webhook into
    (billing.apply_webhook only ever sees this shape)."""

    event_id: str = Field(max_length=128)
    # subscription.created | subscription.updated | transaction.completed | subscription.canceled
    # | payment_failed | payment.succeeded | payment.refunded
    type: str = Field(max_length=64)
    # A charge / failure / refund this event reports (recorded in the payments ledger).
    payment: PaymentDetails | None = None
    # None = unknown to the provider's payload (renewals, failures): resolved via subscription_id.
    account_id: str | None = None
    plan: str = "pro"  # pro | lifetime
    status: str = "active"  # trialing | active | past_due | canceled | paused
    # None = the provider did not say (keeps the stored period; Lifetime never has one).
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    customer_id: str | None = Field(default=None, max_length=128)
    subscription_id: str | None = Field(default=None, max_length=128)


# ==================== Sponsored content ====================


def https_url(value: str | None) -> str | None:
    """Creative links must be https (no javascript:/data:/http: — they open in a new tab)."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not value.lower().startswith("https://") or any(c.isspace() for c in value):
        raise ValueError("must be an https:// URL")
    return value


class AdCreative(BaseModel):
    """What the client renders. Note what is absent: nothing about the viewer."""

    id: str
    sponsor: str
    headline: str
    blurb: str
    cta_label: str
    target_url: str
    image_url: str | None = None


class AdSource(BaseModel):
    """The active ad source, as the client needs it to render (ad_sources.py)."""

    kind: str  # internal | sponsored | affiliate | adsense | microsoft | network
    label: str  # badge text, e.g. "Sponsored", "Ad · Google AdSense"
    title: str  # human name of the source
    mode: str  # fake | live | unavailable
    render: str  # creatives (first-party cards) | embed (provider script tag)
    third_party: bool
    consent_required: bool
    provider_name: str = ""
    privacy_url: str = ""
    disclosure: str = ""
    embed: dict | None = None  # script_url, container_*, push_global — only for render = embed


class AdServeResponse(BaseModel):
    ads: list[AdCreative]
    region: str  # the region the selection used ("ZZ" = none)
    rotation_seconds: int
    enabled: bool
    source: AdSource


class AdEventIn(BaseModel):
    ad_id: str = Field(min_length=1, max_length=36)
    kind: str = Field(pattern="^(impression|click)$")
    region: str | None = Field(default=None, max_length=2)


class AdEventsRequest(BaseModel):
    events: list[AdEventIn] = Field(max_length=20)


class AdEventsResponse(BaseModel):
    accepted: int


def https_or_app_url(value: str | None) -> str | None:
    """Creative links are https; internal (house) ads may also use ``app:upgrade`` (opens the account screen)."""
    if value is not None and value.strip() in APP_LINKS:
        return value.strip()
    return https_url(value)


APP_LINKS = ("app:upgrade",)


class AdCampaignIn(BaseModel):
    kind: str = Field(default="sponsored", pattern="^(internal|sponsored|affiliate)$")
    sponsor: str = Field(min_length=1, max_length=80)
    headline: str = Field(min_length=1, max_length=90)
    blurb: str = Field(default="", max_length=200)
    cta_label: str = Field(default="Learn more", min_length=1, max_length=30)
    target_url: str = Field(min_length=1, max_length=2048)
    image_url: str | None = Field(default=None, max_length=2048)
    regions: str = Field(
        default="", max_length=1000, description="Country codes / groups, comma-separated; empty = everywhere"
    )
    languages: str = Field(default="", max_length=200, description="Language codes, comma-separated; empty = any")
    status: str = Field(default="draft", pattern="^(draft|active|paused|archived)$")
    weight: int = Field(default=10, ge=1, le=100)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    max_impressions: int | None = Field(default=None, ge=1)

    @field_validator("target_url")
    @classmethod
    def _link(cls, v: str) -> str:
        return https_or_app_url(v) or v

    @field_validator("image_url")
    @classmethod
    def _https_only(cls, v: str | None) -> str | None:
        return https_url(v)

    @field_validator("sponsor", "headline", "blurb", "cta_label")
    @classmethod
    def _single_line(cls, v: str) -> str:
        return " ".join(v.split())

    @model_validator(mode="after")
    def _schedule(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class AdCampaignPatch(BaseModel):
    kind: str | None = Field(default=None, pattern="^(internal|sponsored|affiliate)$")
    sponsor: str | None = Field(default=None, min_length=1, max_length=80)
    headline: str | None = Field(default=None, min_length=1, max_length=90)
    blurb: str | None = Field(default=None, max_length=200)
    cta_label: str | None = Field(default=None, min_length=1, max_length=30)
    target_url: str | None = Field(default=None, min_length=1, max_length=2048)
    image_url: str | None = Field(default=None, max_length=2048)
    regions: str | None = Field(default=None, max_length=1000)
    languages: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, pattern="^(draft|active|paused|archived)$")
    weight: int | None = Field(default=None, ge=1, le=100)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    max_impressions: int | None = Field(default=None, ge=1)

    @field_validator("target_url")
    @classmethod
    def _link(cls, v: str | None) -> str | None:
        return https_or_app_url(v)

    @field_validator("image_url")
    @classmethod
    def _https_only(cls, v: str | None) -> str | None:
        return https_url(v)

    @field_validator("sponsor", "headline", "blurb", "cta_label")
    @classmethod
    def _single_line(cls, v: str | None) -> str | None:
        return None if v is None else " ".join(v.split())


class AdCampaignOut(AdCampaignIn):
    id: str
    impressions_total: int
    clicks_total: int
    live: bool
    created_at: datetime
    updated_at: datetime


class AdStatPoint(BaseModel):
    key: str  # ISO day or region code
    impressions: int
    clicks: int


class AdStats(BaseModel):
    ad_id: str
    days: int
    impressions: int
    clicks: int
    ctr: float
    by_day: list[AdStatPoint]
    by_region: list[AdStatPoint]


class AdSourceAdmin(AdSource):
    active: bool
    reason: str = ""  # why "unavailable"
    reporting: str = ""
    settings: dict[str, str] = {}  # ids/urls an administrator sees (no secrets exist here)


class AdSourcesOut(BaseModel):
    active: str
    fakes_enabled: bool
    sources: list[AdSourceAdmin]


class AdSourceUpdate(BaseModel):
    kind: str = Field(pattern="^(internal|sponsored|affiliate|adsense|microsoft|network)$")


# ---- analytics & compliance (ad-admin dashboard) ----


class AdKindPerformance(BaseModel):
    kind: str
    impressions: int
    clicks: int
    ctr: float
    campaigns: int


class AdCampaignPerformance(BaseModel):
    id: str
    kind: str
    sponsor: str
    headline: str
    status: str
    live: bool
    weight: int
    regions: str
    impressions: int
    clicks: int
    ctr: float
    share: float  # share of all impressions in the window
    impressions_total: int
    clicks_total: int


class AdAnalytics(BaseModel):
    days: int
    since: str
    until: str
    kind: str | None
    impressions: int
    clicks: int
    ctr: float
    previous_impressions: int
    previous_clicks: int
    impressions_change: float | None  # vs. the previous window (None = no baseline)
    clicks_change: float | None
    by_day: list[AdStatPoint]
    by_kind: list[AdKindPerformance]
    by_region: list[AdStatPoint]  # small counts folded into "other"
    regions_folded: int
    min_count: int
    campaigns: list[AdCampaignPerformance]
    active_source: AdSource
    third_party_note: str


# ==================== Payment analytics (administrators' console) ====================


class PaymentDayPoint(BaseModel):
    key: str  # YYYY-MM-DD
    transactions: int = 0
    succeeded: int = 0
    failed: int = 0
    refunds: int = 0  # refunds that happened that day (refunded_at)
    revenue_cents: int = 0  # gross, charges paid that day
    refunded_cents: int = 0
    net_cents: int = 0
    revenue: str = "0.00"
    refunded: str = "0.00"


class PaymentStatusShare(BaseModel):
    status: str
    count: int
    amount_cents: int  # paid amount; for refunded statuses the refunded amount
    amount: str
    share: float = 0.0  # of transactions


class PaymentMethodShare(BaseModel):
    provider: str
    label: str
    count: int = 0
    succeeded: int = 0
    failed: int = 0
    amount_cents: int = 0
    refunded_cents: int = 0
    amount: str = "0.00"
    refunded: str = "0.00"
    share: float = 0.0  # of gross revenue


class PaymentPlanShare(BaseModel):
    plan: str
    title: str
    count: int = 0
    amount_cents: int = 0
    refunded_cents: int = 0
    amount: str = "0.00"
    refunded: str = "0.00"
    share: float = 0.0


class TopCustomer(BaseModel):
    account_id: str
    email: str
    membership: str
    payments: int
    amount: str
    amount_cents: int
    refunded: str
    refunded_cents: int
    net_cents: int
    share: float
    last_paid_at: datetime


class AdminRefund(BaseModel):
    id: int
    account_id: str
    email: str
    provider: str
    plan: str
    amount: str
    refunded: str
    refunded_cents: int
    refund_id: str | None
    refunded_at: datetime | None
    paid_at: datetime
    full: bool


class KpiAlert(BaseModel):
    key: str
    level: str  # ok | warn | fail
    title: str
    detail: str
    value: float | None = None
    threshold: float | None = None


class PaymentAnalytics(BaseModel):
    since: str
    until: str
    days: int
    method: str | None
    status: str | None
    currency: str
    mixed_currencies: bool
    revenue: str  # gross
    revenue_cents: int
    refunded: str
    refunded_cents: int
    net_revenue: str
    net_revenue_cents: int
    transactions: int
    succeeded: int
    failed: int
    refund_count: int
    success_rate: float
    refund_rate: float
    average_transaction: str
    average_transaction_cents: int
    previous_revenue_cents: int
    previous_net_revenue_cents: int
    previous_transactions: int
    previous_succeeded: int
    previous_failed: int
    previous_refunded_cents: int
    revenue_change: float | None
    transactions_change: float | None
    failed_change: float | None
    by_day: list[PaymentDayPoint]
    by_status: list[PaymentStatusShare]
    by_method: list[PaymentMethodShare]
    by_plan: list[PaymentPlanShare]
    top_customers: list[TopCustomer]
    refunds: list[AdminRefund]
    alerts: list[KpiAlert]
    methods: list[str]  # every provider seen in the ledger (filter options)


class AdminTransaction(BaseModel):
    id: int
    account_id: str
    email: str
    provider: str
    provider_payment_id: str
    plan: str
    amount: str
    amount_cents: int
    currency: str
    status: str
    refunded: str
    refunded_cents: int
    refund_id: str | None
    refunded_at: datetime | None
    failure_reason: str | None
    period_start: datetime | None
    period_end: datetime | None
    paid_at: datetime


class AdminTransactionPage(BaseModel):
    items: list[AdminTransaction]
    total: int
    limit: int
    offset: int


class AdminTransactionAccount(BaseModel):
    id: str
    email: str
    role: str
    membership: str
    created_at: datetime
    deleted_at: datetime | None
    plan: str
    status: str
    provider: str
    provider_customer_id: str | None
    provider_subscription_id: str | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    lifetime_value_cents: int  # everything this account paid minus refunds


class AdminTransactionDetail(BaseModel):
    transaction: AdminTransaction
    account: AdminTransactionAccount
    history: list[AdminTransaction]  # the account's other charges, newest first


class ComplianceCheck(BaseModel):
    key: str
    title: str
    status: str  # ok | warn | fail
    detail: str


class ComplianceRegime(BaseModel):
    code: str
    name: str
    jurisdiction: str
    obligations: list[str]
    how_met: list[str]
    status: str  # ok | attention
    attention: list[str]


class DataInventoryItem(BaseModel):
    data: str
    location: str
    personal: bool
    retention: str
    rows: int


class AdCompliance(BaseModel):
    generated_at: datetime
    environment: str
    active_source: AdSource
    checks: list[ComplianceCheck]
    regimes: list[ComplianceRegime]
    inventory: list[DataInventoryItem]
    disclaimer: str


class AdOverview(BaseModel):
    source: AdSource
    enabled: bool
    campaigns: dict[str, int]  # status → count
    live: int
    impressions_7d: int
    clicks_7d: int
    rotation_seconds: int
    retention_days: int
    image_hosts: list[str]  # first-party hosts creative images may come from (empty = text only)
    region_groups: dict[str, list[str]]


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
