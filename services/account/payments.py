"""Payment methods — the *strategy* pattern for "how does this account pay?" (the counterpart of
membership.py, which answers "what may this user do?").

Every way of paying is one strategy object with the same responsibilities; the billing routes
and ``billing.apply_webhook`` never branch on a provider name — they ask the strategy:

    method = payments.get("paypal")                    # or payments.default()
    method.create_checkout(account, plan, return_url)  # → URL of the provider's hosted page
    method.verify_signature(body, headers)             # is this webhook really from them?
    method.parse_webhook(body)                         # provider payload → schemas.PaymentEvent
    method.cancel(sub)                                 # end the subscription at the provider now
    method.refund(payment, cents)                      # give (part of) a charge back
    method.manage_url(sub, return_url)                 # the provider's self-service page
    method.describe()                                  # what the client's "Subscribe" UI shows

| Method | Hosted checkout                                   | Webhook authenticity                 |
|--------|---------------------------------------------------|--------------------------------------|
| stripe | Stripe Checkout Session (subscription / payment)  | Stripe-Signature (t + HMAC v1)       |
| paypal | PayPal Subscriptions API; Orders API for Lifetime | PayPal verify-webhook-signature API  |

Prices come from config (``PRICE_PRO_MONTHLY`` …, ``plan_catalog()``): Stripe is charged those
amounts through ad-hoc price data at checkout; PayPal subscriptions bill the configured Plan
ids, which must carry the same amounts at PayPal. Charges, failures and refunds are reported
back through webhooks as ``PaymentEvent.payment`` and kept in the payments ledger (billing.py),
which is what a prorated refund on cancellation is computed from.

Subscriptions are identified on the provider side by our *account id* (Stripe
``client_reference_id``/metadata, PayPal ``custom_id``) so a webhook maps back to an account
without any lookup; renewals and failures that only name the provider's subscription id are
resolved in ``billing.apply_webhook`` via the stored ``provider_subscription_id``.

Only the providers' REST APIs are used (standard library HTTP — no SDK to pin per Python
version); ``HttpTransport`` is the single seam, replaced by a fake in the tests. Card data never
touches this service: both providers host the payment page themselves.

Both providers have a test mode (Stripe test keys ``sk_test_…``, PayPal ``PAYPAL_ENV=sandbox``).
Without credentials, and outside prod, the built-in fakes (``fakes.py``) answer instead: same
strategy code, same REST calls and signed webhooks, but the "provider" lives in this process
and its hosted pages are served by this API — every billing state can be produced locally.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException

import config
import models
import schemas

log = logging.getLogger("account.payments")

BRAND = "Personal Assistant"
PLANS: tuple[str, ...] = ("pro_monthly", "pro_yearly", "lifetime")
PLAN_TITLES = {"pro_monthly": "Pro — monthly", "pro_yearly": "Pro — yearly", "lifetime": "Lifetime"}
PLAN_INTERVAL = {"pro_monthly": "month", "pro_yearly": "year", "lifetime": None}
PLAN_PERIOD_DAYS = {"pro_monthly": 30, "pro_yearly": 365, "lifetime": None}


class NotSupported(HTTPException):
    """The provider cannot do this."""

    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail)


class PaymentError(HTTPException):
    """The provider refused or could not be reached (502 — the client shows the message)."""

    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(status_code=status_code, detail=detail)


# ---------------------------------------------------------------------------- money & plans


def to_cents(amount: Decimal | str | int | float) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int) -> str:
    """Minor units → decimal string ("1.00")."""
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def plan_price_cents(plan: str) -> int:
    prices = {
        "pro_monthly": config.PRICE_PRO_MONTHLY,
        "pro_yearly": config.PRICE_PRO_YEARLY,
        "lifetime": config.PRICE_LIFETIME,
    }
    return to_cents(prices[plan])


def plan_for_cents(cents: int) -> str | None:
    """Which plan costs exactly this (used when a provider payload names no plan)."""
    return next((p for p in PLANS if plan_price_cents(p) == cents), None)


def refund_rule(plan: str) -> str:
    if plan == "lifetime":
        return f"Cancel any time: {money(to_cents(config.REFUND_LIFETIME))} {config.BILLING_CURRENCY} back."
    return "Cancel any time: the unused days of the current period are refunded."


def plan_info(plan: str) -> schemas.PlanInfo:
    cents = plan_price_cents(plan)
    return schemas.PlanInfo(
        id=plan,
        title=PLAN_TITLES[plan],
        amount=money(cents),
        amount_cents=cents,
        currency=config.BILLING_CURRENCY,
        interval=PLAN_INTERVAL[plan],
        period_days=PLAN_PERIOD_DAYS[plan],
        refund_rule=refund_rule(plan),
    )


def plan_catalog() -> list[schemas.PlanInfo]:
    return [plan_info(p) for p in PLANS]


# ---------------------------------------------------------------------------- helpers


def with_query(url: str, **params: str) -> str:
    """`url` with these query parameters set (replacing existing ones of the same name)."""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (Starlette's Headers already are; plain dicts are not)."""
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def from_unix(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def from_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class HttpTransport:
    """Minimal HTTPS client for the providers' REST APIs (standard library). Returns
    ``(status, json_body)`` for every answer — 4xx/5xx included — and raises ``PaymentError``
    only when the provider cannot be reached at all."""

    timeout_seconds = 20

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None, data: bytes | None = None
    ) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return resp.status, _json_or_empty(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json_or_empty(exc.read())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise PaymentError(f"The payment provider could not be reached ({exc}). Please try again.") from exc


def _json_or_empty(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}


# ---------------------------------------------------------------------------- the strategy


class PaymentMethod:
    """One way of paying (the strategy interface). Subclasses implement the provider
    specifics; everything the rest of the service needs is this shape."""

    name: str = "base"
    label: str = ""
    description: str = ""
    sandbox: bool = False  # nothing is charged

    # ---- configuration -----------------------------------------------------------------

    @property
    def plans(self) -> tuple[str, ...]:
        """Which plans this method sells (a plan without a configured price/plan id is not offered)."""
        return PLANS

    def is_configured(self) -> bool:
        return False

    @property
    def fake(self) -> Any:
        """The built-in fake of this provider when it stands in for missing credentials
        (config.BILLING_FAKE_PROVIDERS), else None."""
        return None

    # ---- the operations ------------------------------------------------------------------

    def create_checkout(
        self, account: models.Account, plan: str, return_url: str, trial_until: datetime | None = None
    ) -> str:
        """URL of the provider's hosted page for `plan`; the buyer comes back to `return_url`.
        `trial_until`: the account is mid-trial — paid billing must start then, not now."""
        raise NotImplementedError

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Authenticity of a webhook delivery (never trust the payload before this says True)."""
        raise NotImplementedError

    def parse_webhook(self, body: bytes) -> schemas.PaymentEvent | None:
        """Provider payload → neutral event; None = a kind of event we do not act on (still
        answered 200 so the provider stops retrying). Raises ValueError for garbage."""
        raise NotImplementedError

    def event_id(self, body: bytes) -> str | None:
        """The provider's id of this delivery, read before translating it — so a re-delivered
        event is recognised without side effects (parse_webhook may call the provider)."""
        event = json.loads(body)
        value = event.get("id") if isinstance(event, dict) else None
        return str(value) if value else None

    def cancel(self, sub: models.Subscription) -> None:
        """End `sub` at the provider **now** (no further charges; the refund is separate).
        The provider confirms through its webhook."""
        raise NotImplementedError

    def refund(self, payment: models.Payment, cents: int) -> str:
        """Give `cents` of `payment` back. Returns the provider's refund id."""
        raise NotImplementedError

    def can_manage(self, sub: models.Subscription) -> bool:
        """Is there a self-service page (payment method, invoices) for this subscription?"""
        return False

    def manage_url(self, sub: models.Subscription, return_url: str) -> str | None:
        """The provider's self-service page for this subscriber — None when there is none."""
        return None

    # ---- shared behaviour ----------------------------------------------------------------

    def require_plan(self, plan: str) -> None:
        if plan not in self.plans:
            raise HTTPException(status_code=400, detail=f"'{plan}' cannot be bought via {self.label}.")

    def describe(self) -> schemas.PaymentMethodInfo:
        """Always describes the method, configured or not: the client shows every method's
        button and an unconfigured one explains what is missing (`available`)."""
        configured = self.is_configured()
        return schemas.PaymentMethodInfo(
            name=self.name,
            label=self.label,
            description=self.description,
            plans=list(self.plans) if configured else list(PLANS),
            sandbox=configured and self.sandbox,
            fake=self.fake is not None,
            available=configured,
        )

    def unavailable_detail(self) -> str:
        return f"{self.label} payments are not set up on this server yet (see docs/DEPLOYMENT.md → Payments)."

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PaymentMethod {self.name}>"


# ---------------------------------------------------------------------------- Stripe


class StripePayment(PaymentMethod):
    """Stripe Checkout (hosted page): `mode=subscription` for Pro, `mode=payment` for Lifetime,
    both with ad-hoc price data from config. Webhooks: checkout.session.{completed,
    async_payment_succeeded, async_payment_failed} · customer.subscription.{created, updated,
    deleted} · invoice.{paid, payment_failed} · charge.refunded.
    Signatures: https://docs.stripe.com/webhooks#verify-manually."""

    name = "stripe"
    label = "Card (Stripe)"
    description = "Credit or debit card, Apple Pay and Google Pay — checkout hosted by Stripe."
    signature_tolerance_seconds = 300

    # Stripe subscription status → ours. Missing = the first payment never succeeded
    # (incomplete / incomplete_expired): not an entitlement change, ignored.
    STATUS = {
        "trialing": "trialing",
        "active": "active",
        "past_due": "past_due",
        "unpaid": "canceled",  # dunning exhausted
        "canceled": "canceled",
        "paused": "paused",
    }

    def __init__(self, http: HttpTransport | None = None):
        self.http = http or HttpTransport()

    # ---- configuration: real credentials, or the built-in fake standing in for them --------

    def _real(self) -> bool:
        return bool(config.STRIPE_SECRET_KEY and config.STRIPE_WEBHOOK_SECRET)

    @property
    def fake(self) -> Any:
        if self._real() or not config.BILLING_FAKE_PROVIDERS:
            return None
        import fakes

        return fakes.STRIPE

    @property
    def secret_key(self) -> str:
        return self.fake.secret_key if self.fake else config.STRIPE_SECRET_KEY

    @property
    def webhook_secret(self) -> str:
        return self.fake.webhook_secret if self.fake else config.STRIPE_WEBHOOK_SECRET

    @property
    def sandbox(self) -> bool:  # type: ignore[override]
        return self.secret_key.startswith("sk_test_")

    def is_configured(self) -> bool:
        return bool(self.fake) or self._real()

    # ---- API ---------------------------------------------------------------------------

    def _call(self, method: str, path: str, fields: Mapping[str, str] | None = None, *, doing: str) -> dict[str, Any]:
        headers = {
            "Authorization": "Bearer " + self.secret_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        transport = self.fake.transport if self.fake else self.http
        status, body = transport.request(method, config.STRIPE_API_BASE + path, headers=headers, data=data)
        if status >= 400:
            message = (body.get("error") or {}).get("message") or f"HTTP {status}"
            log.warning("Stripe %s %s failed: %s %s", method, path, status, message)
            raise PaymentError(f"Stripe could not {doing}: {message}")
        return body

    def create_checkout(
        self, account: models.Account, plan: str, return_url: str, trial_until: datetime | None = None
    ) -> str:
        self.require_plan(plan)
        subscription = plan != "lifetime"
        fields = {
            "mode": "subscription" if subscription else "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": config.BILLING_CURRENCY.lower(),
            "line_items[0][price_data][unit_amount]": str(plan_price_cents(plan)),
            "line_items[0][price_data][product_data][name]": f"{BRAND} — {PLAN_TITLES[plan]}",
            "success_url": return_url,
            "cancel_url": with_query(return_url, billing="canceled"),
            "client_reference_id": account.id,
            "customer_email": account.email,
            "metadata[account_id]": account.id,
            "metadata[plan]": plan,
        }
        if subscription:
            fields["line_items[0][price_data][recurring][interval]"] = PLAN_INTERVAL[plan] or "month"
            fields["subscription_data[metadata][account_id]"] = account.id
            fields["subscription_data[metadata][plan]"] = plan
            if trial_until:  # keep the running trial: Stripe starts charging when it ends
                fields["subscription_data[trial_end]"] = str(int(trial_until.timestamp()))
        else:
            fields["payment_intent_data[metadata][account_id]"] = account.id
            fields["payment_intent_data[metadata][plan]"] = plan
        session = self._call("POST", "/v1/checkout/sessions", fields, doing="start the checkout")
        url = session.get("url")
        if not url:
            raise PaymentError("Stripe did not return a checkout page.")
        return url

    def cancel(self, sub: models.Subscription) -> None:
        if not sub.provider_subscription_id:
            return  # a one-off payment (Lifetime): nothing renews at Stripe
        self._call("DELETE", f"/v1/subscriptions/{sub.provider_subscription_id}", doing="cancel the subscription")

    def refund(self, payment: models.Payment, cents: int) -> str:
        key = "payment_intent" if payment.provider_payment_id.startswith("pi_") else "charge"
        refund = self._call(
            "POST",
            "/v1/refunds",
            {key: payment.provider_payment_id, "amount": str(cents)},
            doing="refund the payment",
        )
        refund_id = refund.get("id")
        if not refund_id:
            raise PaymentError("Stripe did not confirm the refund.")
        return str(refund_id)

    def can_manage(self, sub: models.Subscription) -> bool:
        return bool(sub.provider_customer_id)  # Customer Portal: subscriptions and invoices

    def manage_url(self, sub: models.Subscription, return_url: str) -> str | None:
        if not sub.provider_customer_id:
            return None
        portal = self._call(
            "POST",
            "/v1/billing_portal/sessions",
            {"customer": sub.provider_customer_id, "return_url": return_url},
            doing="open the billing portal",
        )
        return portal.get("url") or None

    # ---- webhooks ----------------------------------------------------------------------

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        timestamp, signatures = None, []
        for item in (header(headers, "Stripe-Signature") or "").split(","):
            key, _, value = item.strip().partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                signatures.append(value)
        if not timestamp or not signatures or not timestamp.isdigit():
            return False
        if abs(time.time() - int(timestamp)) > self.signature_tolerance_seconds:
            return False  # replayed delivery
        expected = self.sign(body, secret=self.webhook_secret, timestamp=int(timestamp)).split("v1=", 1)[1]
        return any(hmac.compare_digest(expected, s) for s in signatures)

    @staticmethod
    def sign(body: bytes, *, secret: str | None = None, timestamp: int | None = None) -> str:
        """A `Stripe-Signature` header value for `body` (what Stripe's CLI/test mode sends)."""
        ts = timestamp if timestamp is not None else int(time.time())
        key = (secret if secret is not None else config.STRIPE_WEBHOOK_SECRET).encode()
        digest = hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return f"t={ts},v1={digest}"

    @staticmethod
    def _invoice_payment(obj: dict[str, Any], *, failed: bool = False) -> schemas.PaymentDetails | None:
        """The charge behind an invoice (paid or failed) with the period it bought."""
        payment_id = obj.get("payment_intent") or obj.get("charge")
        if isinstance(payment_id, dict):
            payment_id = payment_id.get("id")
        if not payment_id:
            payment_id = obj.get("id")  # no charge object yet (e.g. first attempt failed before one existed)
        if not payment_id:
            return None
        lines = ((obj.get("lines") or {}).get("data") or [{}])[0]
        period = lines.get("period") or {}
        parent = obj.get("parent") or {}
        details = obj.get("subscription_details") or parent.get("subscription_details") or {}
        plan = (details.get("metadata") or {}).get("plan") or (obj.get("metadata") or {}).get("plan")
        cents = int(obj.get("amount_due" if failed else "amount_paid") or obj.get("amount_due") or 0)
        return schemas.PaymentDetails(
            provider_payment_id=str(payment_id),
            amount_cents=cents,
            currency=str(obj.get("currency") or config.BILLING_CURRENCY).upper(),
            plan=plan or plan_for_cents(cents),
            period_start=from_unix(period.get("start")),
            period_end=from_unix(period.get("end")),
            paid_at=from_unix(obj.get("status_transitions", {}).get("paid_at")) or from_unix(obj.get("created")),
            failure_reason=(obj.get("last_payment_error") or {}).get("message") if failed else None,
        )

    def parse_webhook(self, body: bytes) -> schemas.PaymentEvent | None:
        event = json.loads(body)
        event_id, kind = event.get("id"), event.get("type", "")
        if not event_id or not kind:
            raise ValueError("not a Stripe event")
        obj: dict[str, Any] = (event.get("data") or {}).get("object") or {}
        meta: dict[str, Any] = obj.get("metadata") or {}

        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            if obj.get("mode") != "payment":
                # Subscription mode: status, period, trial and charges come with
                # customer.subscription.* / invoice.* (they carry our account id) — nothing to add.
                return None
            if obj.get("payment_status") not in ("paid", "no_payment_required"):
                return None  # delayed payment method: wait for checkout.session.async_payment_succeeded
            payment_id = obj.get("payment_intent")
            return schemas.PaymentEvent(
                event_id=event_id,
                type="transaction.completed",
                account_id=obj.get("client_reference_id") or meta.get("account_id"),
                plan="lifetime",
                status="active",
                customer_id=obj.get("customer"),
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(payment_id),
                    amount_cents=int(obj.get("amount_total") or 0),
                    currency=str(obj.get("currency") or config.BILLING_CURRENCY).upper(),
                    plan="lifetime",
                    paid_at=from_unix(event.get("created")),
                )
                if payment_id
                else None,
            )
        if kind == "checkout.session.async_payment_failed":
            payment_id = obj.get("payment_intent")
            if not payment_id:
                return None
            return schemas.PaymentEvent(
                event_id=event_id,
                type="payment_failed",
                account_id=obj.get("client_reference_id") or meta.get("account_id"),
                plan="lifetime" if obj.get("mode") == "payment" else "pro",
                customer_id=obj.get("customer"),
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(payment_id),
                    amount_cents=int(obj.get("amount_total") or 0),
                    currency=str(obj.get("currency") or config.BILLING_CURRENCY).upper(),
                    plan=meta.get("plan") or ("lifetime" if obj.get("mode") == "payment" else None),
                    paid_at=from_unix(event.get("created")),
                    failure_reason="The payment was declined or never completed.",
                ),
            )
        if kind in ("customer.subscription.created", "customer.subscription.updated"):
            status = self.STATUS.get(obj.get("status", ""))
            if status is None:
                return None
            items = (obj.get("items") or {}).get("data") or [{}]
            period = obj.get("current_period_end") or items[0].get("current_period_end")
            return schemas.PaymentEvent(
                event_id=event_id,
                type="subscription.updated",
                account_id=meta.get("account_id"),
                plan="pro",
                status=status,
                current_period_end=from_unix(period),
                cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
                customer_id=obj.get("customer"),
                subscription_id=obj.get("id"),
            )
        if kind == "customer.subscription.deleted":
            # Fires when the subscription really ends — at the period end after a
            # cancel_at_period_end, or right away for an immediate cancellation: access stops now.
            return schemas.PaymentEvent(
                event_id=event_id,
                type="subscription.canceled",
                account_id=meta.get("account_id"),
                current_period_end=from_unix(obj.get("ended_at")) or datetime.now(timezone.utc),
                customer_id=obj.get("customer"),
                subscription_id=obj.get("id"),
            )
        if kind in ("invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"):
            parent = obj.get("parent") or {}
            subscription_id = obj.get("subscription") or (parent.get("subscription_details") or {}).get("subscription")
            if isinstance(subscription_id, dict):
                subscription_id = subscription_id.get("id")
            if not subscription_id:
                return None  # a one-off invoice, not a subscription charge
            failed = kind == "invoice.payment_failed"
            details = obj.get("subscription_details") or parent.get("subscription_details") or {}
            return schemas.PaymentEvent(
                event_id=event_id,
                type="payment_failed" if failed else "payment.succeeded",
                account_id=(details.get("metadata") or {}).get("account_id"),
                plan="pro",
                customer_id=obj.get("customer"),
                subscription_id=str(subscription_id),
                payment=self._invoice_payment(obj, failed=failed),
            )
        if kind == "charge.refunded":
            payment_id = obj.get("payment_intent") or obj.get("id")
            refunds = ((obj.get("refunds") or {}).get("data") or [{}])[0]
            return schemas.PaymentEvent(
                event_id=event_id,
                type="payment.refunded",
                account_id=meta.get("account_id"),
                customer_id=obj.get("customer"),
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(payment_id),
                    amount_cents=int(obj.get("amount") or 0),
                    currency=str(obj.get("currency") or config.BILLING_CURRENCY).upper(),
                    refunded_cents=int(obj.get("amount_refunded") or 0),
                    refund_id=refunds.get("id"),
                ),
            )
        return None


# ---------------------------------------------------------------------------- PayPal


class PayPalPayment(PaymentMethod):
    """PayPal REST: Subscriptions API (billing plans) for Pro, Orders API v2 for Lifetime.
    The buyer approves on PayPal's page; the subscription activates / the order is captured
    from the webhook (CHECKOUT.ORDER.APPROVED → capture), so a closed tab cannot lose a sale.
    Charges: PAYMENT.SALE.COMPLETED (subscriptions) / PAYMENT.CAPTURE.COMPLETED (orders);
    refunds through /v2/payments/captures/{id}/refund. Webhook authenticity is confirmed by
    PayPal's verify-webhook-signature endpoint."""

    name = "paypal"
    label = "PayPal"
    description = "PayPal balance, linked bank account or card — checkout hosted by PayPal."

    STATUS = {
        "ACTIVE": "active",
        "SUSPENDED": "past_due",  # payment failures (PayPal suspends, then retries)
        "CANCELLED": "canceled",
        "EXPIRED": "canceled",
    }
    EVENTS_SUBSCRIPTION = (
        "BILLING.SUBSCRIPTION.ACTIVATED",
        "BILLING.SUBSCRIPTION.UPDATED",
        "BILLING.SUBSCRIPTION.RE-ACTIVATED",
        "BILLING.SUBSCRIPTION.SUSPENDED",
    )

    def __init__(self, http: HttpTransport | None = None):
        self.http = http or HttpTransport()
        self._token: tuple[str, float] | None = None  # (access token, expires at)

    # ---- configuration: real credentials, or the built-in fake standing in for them --------

    def _real(self) -> bool:
        return bool(config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET and config.PAYPAL_WEBHOOK_ID)

    @property
    def fake(self) -> Any:
        if self._real() or not config.BILLING_FAKE_PROVIDERS:
            return None
        import fakes

        return fakes.PAYPAL

    @property
    def credentials(self) -> tuple[str, str, str]:
        """(client id, client secret, webhook id)"""
        if self.fake:
            return self.fake.client_id, self.fake.client_secret, self.fake.webhook_id
        return config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET, config.PAYPAL_WEBHOOK_ID

    @property
    def sandbox(self) -> bool:  # type: ignore[override]
        return bool(self.fake) or config.PAYPAL_ENV != "live"

    def _plan_ids(self) -> dict[str, str]:
        if self.fake:
            return dict(self.fake.plan_ids)
        return {"pro_monthly": config.PAYPAL_PLAN_PRO_MONTHLY, "pro_yearly": config.PAYPAL_PLAN_PRO_YEARLY}

    @property
    def plans(self) -> tuple[str, ...]:
        """Subscriptions need a PayPal Plan id; Lifetime is an order at the configured price."""
        return tuple(p for p in PLANS if p == "lifetime" or self._plan_ids().get(p))

    def is_configured(self) -> bool:
        return bool(self.fake) or self._real()

    # ---- API ---------------------------------------------------------------------------

    def _transport(self) -> Any:
        return self.fake.transport if self.fake else self.http

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time():
            return self._token[0]
        client_id, client_secret, _ = self.credentials
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        status, body = self._transport().request(
            "POST",
            config.PAYPAL_API_BASE + "/v1/oauth2/token",
            headers={
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=b"grant_type=client_credentials",
        )
        token = body.get("access_token")
        if status >= 400 or not token:
            log.warning("PayPal OAuth failed: %s %s", status, body.get("error_description") or body.get("error"))
            raise PaymentError("PayPal rejected this service's credentials.")
        self._token = (token, time.time() + max(60, int(body.get("expires_in", 3600)) - 60))
        return token

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        headers = {
            "Authorization": "Bearer " + self._access_token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        }
        data = json.dumps(payload).encode() if payload is not None else None
        return self._transport().request(method, config.PAYPAL_API_BASE + path, headers=headers, data=data)

    @staticmethod
    def _custom_id(account: models.Account, plan_key: str) -> str:
        return f"{account.id}:{plan_key}"  # ≤ 127 chars: uuid + ":lifetime"

    @staticmethod
    def _split_custom(value: Any) -> tuple[str | None, str | None]:
        """`custom_id` → (account id, plan key); a value without ':' is just an account id."""
        if not isinstance(value, str) or not value:
            return None, None
        account_id, _, plan_key = value.partition(":")
        return account_id or None, plan_key or None

    def create_checkout(
        self, account: models.Account, plan: str, return_url: str, trial_until: datetime | None = None
    ) -> str:
        self.require_plan(plan)
        context = {
            "brand_name": BRAND,
            "shipping_preference": "NO_SHIPPING",
            "return_url": return_url,
            "cancel_url": with_query(return_url, billing="canceled"),
        }
        if plan == "lifetime":
            status, body = self._call(
                "POST",
                "/v2/checkout/orders",
                {
                    "intent": "CAPTURE",
                    "purchase_units": [
                        {
                            "reference_id": "lifetime",
                            "custom_id": self._custom_id(account, "lifetime"),
                            "description": f"{BRAND} — {PLAN_TITLES['lifetime']}",
                            "amount": {
                                "currency_code": config.BILLING_CURRENCY,
                                "value": money(plan_price_cents("lifetime")),
                            },
                        }
                    ],
                    "application_context": {**context, "user_action": "PAY_NOW"},
                },
            )
        else:
            subscription: dict[str, Any] = {
                "plan_id": self._plan_ids()[plan],
                "custom_id": self._custom_id(account, plan),
                "subscriber": {"email_address": account.email},
                "application_context": {**context, "user_action": "SUBSCRIBE_NOW"},
            }
            if trial_until:  # keep the running trial: the first charge is due when it ends
                subscription["start_time"] = trial_until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            status, body = self._call("POST", "/v1/billing/subscriptions", subscription)
        if status >= 400:
            message = body.get("message") or body.get("error_description") or f"HTTP {status}"
            log.warning("PayPal checkout for %s failed: %s %s", plan, status, message)
            raise PaymentError(f"PayPal could not start the checkout: {message}")
        approve = next((link.get("href") for link in body.get("links", []) if link.get("rel") == "approve"), None)
        if not approve:
            raise PaymentError("PayPal did not return an approval page.")
        return approve

    def cancel(self, sub: models.Subscription) -> None:
        if not sub.provider_subscription_id:
            return  # a one-off order (Lifetime): nothing renews at PayPal
        status, body = self._call(
            "POST",
            f"/v1/billing/subscriptions/{sub.provider_subscription_id}/cancel",
            {"reason": "Canceled by the subscriber from the app."},
        )
        if status >= 400 and not (status == 422 and body.get("name") == "UNPROCESSABLE_ENTITY"):
            message = body.get("message") or f"HTTP {status}"
            log.warning("PayPal cancel of %s failed: %s %s", sub.provider_subscription_id, status, message)
            raise PaymentError(f"PayPal could not cancel the subscription: {message}")

    def refund(self, payment: models.Payment, cents: int) -> str:
        status, body = self._call(
            "POST",
            f"/v2/payments/captures/{payment.provider_payment_id}/refund",
            {"amount": {"value": money(cents), "currency_code": payment.currency}},
        )
        refund_id = body.get("id")
        if status >= 400 or not refund_id:
            message = body.get("message") or f"HTTP {status}"
            log.warning("PayPal refund of %s failed: %s %s", payment.provider_payment_id, status, message)
            raise PaymentError(f"PayPal could not refund the payment: {message}")
        return str(refund_id)

    def can_manage(self, sub: models.Subscription) -> bool:
        return bool(sub.provider_subscription_id)  # the buyer's "Automatic payments" page

    def manage_url(self, sub: models.Subscription, return_url: str) -> str | None:
        if not sub.provider_subscription_id:
            return None
        if self.fake:
            return self.fake.manage_url(sub.provider_subscription_id, return_url)
        host = "www.sandbox.paypal.com" if self.sandbox else "www.paypal.com"
        return f"https://{host}/myaccount/autopay/"

    # ---- webhooks ----------------------------------------------------------------------

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        fields = {
            "auth_algo": header(headers, "PAYPAL-AUTH-ALGO"),
            "cert_url": header(headers, "PAYPAL-CERT-URL"),
            "transmission_id": header(headers, "PAYPAL-TRANSMISSION-ID"),
            "transmission_sig": header(headers, "PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": header(headers, "PAYPAL-TRANSMISSION-TIME"),
        }
        if not all(fields.values()):
            return False
        try:
            event = json.loads(body)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        status, answer = self._call(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            {**fields, "webhook_id": self.credentials[2], "webhook_event": event},
        )
        return status == 200 and answer.get("verification_status") == "SUCCESS"

    @staticmethod
    def _amount(value: Any) -> tuple[int, str]:
        """PayPal money object → (cents, currency)."""
        if not isinstance(value, dict):
            return 0, config.BILLING_CURRENCY
        raw = value.get("value") or value.get("total") or "0"
        return to_cents(raw), str(value.get("currency_code") or value.get("currency") or config.BILLING_CURRENCY)

    def _plan_of(self, plan_id: Any, plan_key: str | None) -> str | None:
        if plan_key in PLANS:
            return plan_key
        return next((plan for plan, pid in self._plan_ids().items() if pid and pid == plan_id), None)

    def parse_webhook(self, body: bytes) -> schemas.PaymentEvent | None:
        event = json.loads(body)
        event_id, kind = event.get("id"), event.get("event_type", "")
        if not event_id or not kind:
            raise ValueError("not a PayPal event")
        res: dict[str, Any] = event.get("resource") or {}

        if kind.startswith("BILLING.SUBSCRIPTION."):
            account_id, plan_key = self._split_custom(res.get("custom_id"))
            common = {
                "event_id": event_id,
                "account_id": account_id,
                "customer_id": (res.get("subscriber") or {}).get("payer_id"),
                "subscription_id": res.get("id"),
            }
            if kind in self.EVENTS_SUBSCRIPTION:
                status = self.STATUS.get(res.get("status", ""))
                if status is None:
                    return None  # APPROVAL_PENDING / APPROVED: nothing paid yet
                return schemas.PaymentEvent(
                    type="subscription.updated",
                    plan="pro",
                    status=status,
                    current_period_end=from_iso((res.get("billing_info") or {}).get("next_billing_time")),
                    **common,
                )
            if kind in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"):
                # PayPal stops charging at once; what was paid for stays usable until the stored
                # period end (the event carries no period → billing keeps the known one).
                return schemas.PaymentEvent(type="subscription.canceled", **common)
            if kind == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
                cents, currency = self._amount((res.get("billing_info") or {}).get("outstanding_balance"))
                plan = self._plan_of(res.get("plan_id"), plan_key)
                if not cents and plan:
                    cents = plan_price_cents(plan)
                return schemas.PaymentEvent(
                    type="payment_failed",
                    payment=schemas.PaymentDetails(
                        provider_payment_id=f"{res.get('id')}:failed:{event_id}",
                        amount_cents=cents,
                        currency=currency,
                        plan=plan,
                        paid_at=from_iso(event.get("create_time")),
                        failure_reason="PayPal could not collect the payment.",
                    ),
                    **common,
                )
            return None

        if kind == "PAYMENT.SALE.COMPLETED" and res.get("billing_agreement_id"):
            # A subscription charge (first or renewal): the sale names the subscription; ask
            # PayPal for the subscription to learn the account, plan and the new period.
            subscription_id = res["billing_agreement_id"]
            status, sub = self._call("GET", f"/v1/billing/subscriptions/{subscription_id}")
            if status != 200:
                raise PaymentError(f"PayPal subscription {subscription_id} could not be read (HTTP {status}).")
            account_id, plan_key = self._split_custom(sub.get("custom_id"))
            cents, currency = self._amount(res.get("amount"))
            paid_at = from_iso(res.get("create_time") or res.get("update_time")) or datetime.now(timezone.utc)
            period_end = from_iso((sub.get("billing_info") or {}).get("next_billing_time"))
            return schemas.PaymentEvent(
                event_id=event_id,
                type="payment.succeeded",
                account_id=account_id,
                plan="pro",
                status=self.STATUS.get(sub.get("status", ""), "active"),
                current_period_end=period_end,
                customer_id=(sub.get("subscriber") or {}).get("payer_id"),
                subscription_id=subscription_id,
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(res.get("id")),
                    amount_cents=cents,
                    currency=currency,
                    plan=self._plan_of(sub.get("plan_id"), plan_key) or plan_for_cents(cents),
                    period_start=paid_at,
                    period_end=period_end,
                    paid_at=paid_at,
                ),
            )

        if kind == "CHECKOUT.ORDER.APPROVED":
            units = res.get("purchase_units") or [{}]
            account_id, plan_key = self._split_custom(units[0].get("custom_id"))
            if plan_key != "lifetime" or not account_id:
                return None
            status, order = self._call("POST", f"/v2/checkout/orders/{res.get('id')}/capture", {})
            already = status == 422 and any(
                issue.get("issue") == "ORDER_ALREADY_CAPTURED" for issue in order.get("details") or []
            )
            if not (status in (200, 201) and order.get("status") == "COMPLETED") and not already:
                raise PaymentError(f"PayPal order {res.get('id')} could not be captured (HTTP {status}).")
            captures = (((order.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures")) or [{}]
            capture = captures[0]
            cents, currency = self._amount(capture.get("amount") or units[0].get("amount"))
            return schemas.PaymentEvent(
                event_id=event_id,
                type="transaction.completed",
                account_id=account_id,
                plan="lifetime",
                status="active",
                customer_id=(order.get("payer") or {}).get("payer_id"),
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(capture["id"]),
                    amount_cents=cents,
                    currency=currency,
                    plan="lifetime",
                    paid_at=from_iso(capture.get("create_time")) or datetime.now(timezone.utc),
                )
                if capture.get("id")
                else None,
            )

        if kind in ("PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
            account_id, plan_key = self._split_custom(res.get("custom_id"))
            if plan_key != "lifetime" or not account_id:
                return None
            cents, currency = self._amount(res.get("amount"))
            denied = kind != "PAYMENT.CAPTURE.COMPLETED"
            return schemas.PaymentEvent(
                event_id=event_id,
                type="payment_failed" if denied else "transaction.completed",
                account_id=account_id,
                plan="lifetime",
                status="active",
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(res.get("id")),
                    amount_cents=cents,
                    currency=currency,
                    plan="lifetime",
                    paid_at=from_iso(res.get("create_time")) or datetime.now(timezone.utc),
                    failure_reason="PayPal declined the payment." if denied else None,
                ),
            )

        if kind in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.SALE.REFUNDED"):
            # The refund resource points at the capture/sale it belongs to ("up" link).
            up = next((link.get("href", "") for link in res.get("links") or [] if link.get("rel") == "up"), "")
            payment_id = up.rstrip("/").rsplit("/", 1)[-1] if up else (res.get("sale_id") or res.get("capture_id"))
            if not payment_id:
                return None
            cents, currency = self._amount(res.get("amount"))
            account_id, _ = self._split_custom(res.get("custom_id"))
            return schemas.PaymentEvent(
                event_id=event_id,
                type="payment.refunded",
                account_id=account_id,
                payment=schemas.PaymentDetails(
                    provider_payment_id=str(payment_id),
                    amount_cents=0,
                    currency=currency,
                    refunded_cents=cents,
                    refund_is_total=False,  # PayPal reports each refund on its own
                    refund_id=res.get("id"),
                ),
            )
        return None


# ---------------------------------------------------------------------------- registry

STRIPE = StripePayment()
PAYPAL = PayPalPayment()

METHODS: dict[str, PaymentMethod] = {m.name: m for m in (STRIPE, PAYPAL)}


def enabled() -> list[PaymentMethod]:
    """Every registered method, in BILLING_METHODS order when that is set (configured or not)."""
    names = config.BILLING_METHODS or list(METHODS)
    return [METHODS[n] for n in names if n in METHODS]


def available() -> list[PaymentMethod]:
    """The methods a buyer can actually pay with right now (enabled *and* configured)."""
    return [m for m in enabled() if m.is_configured()]


def default() -> PaymentMethod | None:
    methods = available()
    for m in methods:
        if m.name == config.BILLING_PROVIDER:
            return m
    return methods[0] if methods else None


def get(name: str | None) -> PaymentMethod:
    """The strategy for a checkout request (None = default). 404 for an unknown / disabled
    method, 503 for a known one whose credentials are missing (the detail says which)."""
    if name is None:
        method = default()
        if method is None:
            raise HTTPException(status_code=503, detail="No payment method is set up on this server yet.")
        return method
    for method in enabled():
        if method.name == name:
            if not method.is_configured():
                raise HTTPException(status_code=503, detail=method.unavailable_detail())
            return method
    raise HTTPException(status_code=404, detail=f"Payment method '{name}' is not available.")


def for_subscription(sub: models.Subscription) -> PaymentMethod | None:
    """The strategy holding this subscription, if it is one of ours and usable right now
    (seeded / admin-granted rows have none: their lifecycle is local only)."""
    method = METHODS.get(sub.provider)
    return method if method and method.is_configured() else None


def catalog() -> schemas.PaymentMethods:
    """Every enabled method (configured ones first in the client), the pre-selected one and
    the plans with their prices."""
    chosen = default()
    return schemas.PaymentMethods(
        methods=[m.describe() for m in enabled()],
        default=chosen.name if chosen else None,
        plans=plan_catalog(),
        currency=config.BILLING_CURRENCY,
    )
