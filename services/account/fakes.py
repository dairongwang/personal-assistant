"""Built-in fakes of Stripe and PayPal — stand-ins for the real providers while their
credentials are missing (config.BILLING_FAKE_PROVIDERS: on outside prod, never in prod).

Each fake is three things:

1. a tiny "provider account" in memory (customers, subscriptions, orders, charges, refunds,
   issued webhooks), persisted to ``DATA_DIR/billing_fakes.json`` so it survives API restarts;
2. a *transport* answering the REST calls the strategies make — ``payments.py`` talks to it
   exactly as it talks to the real API (same paths, same form/JSON payloads, same answers), so
   the strategy code under test is the production code;
3. hosted pages served by this API under ``/api/v1/billing/fake/…`` that stand in for the
   provider's checkout / approval / portal pages. Buttons there do what a buyer (or the
   provider's scheduler, or an operator in the provider's dashboard) would do: pay, pay with a
   card that is declined, approve, cancel, renew, fail a payment, recover it, refund.

Whatever happens on the provider side is reported through **webhooks delivered in-process**:
the fake signs them the way the provider does (Stripe ``Stripe-Signature``; PayPal transmission
headers that its ``verify-webhook-signature`` endpoint — also faked — confirms) and hands them
to ``billing.receive_webhook``, i.e. the same path a real delivery takes minus the network.

Amounts are the configured prices (config.PRICE_*): Stripe is charged the ad-hoc price data
the strategy sends; the fake PayPal plans carry the configured amounts.

Only the handful of endpoints the strategies use are implemented; anything else answers 404.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

import config

log = logging.getLogger("account.fakes")

PERIOD_DAYS = {"pro_monthly": 30, "pro_yearly": 365}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _form(data: bytes | None) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs((data or b"").decode(), keep_blank_values=True).items()}


def _json(data: bytes | None) -> dict[str, Any]:
    try:
        body = json.loads(data or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _price_cents(plan: str) -> int:
    import payments

    return payments.plan_price_cents(plan)


def _money(cents: int) -> str:
    import payments

    return payments.money(cents)


def _label(plan: str) -> str:
    return plan.replace("_", " ").title()


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>
<body style="font-family:system-ui;max-width:560px;margin:48px auto;line-height:1.55;padding:0 16px">
<p style="background:#fff3cd;border:1px solid #ffe69c;padding:8px 12px;border-radius:6px;font-size:14px">
<strong>Built-in fake.</strong> This page stands in for {title.split(" (")[0]}'s hosted page while no
credentials are configured (<code>BILLING_FAKE_PROVIDERS</code>). Nothing is charged.</p>
{body}</body>"""


def _button(label: str, action: str, *, primary: bool = False, confirm: str | None = None) -> str:
    style = "padding:10px 18px;font-size:15px;margin:4px 6px 4px 0;border-radius:6px;cursor:pointer;" + (
        "background:#5469d4;color:#fff;border:0" if primary else "background:#fff;border:1px solid #999"
    )
    onsubmit = f' onsubmit="return confirm({json.dumps(confirm)})"' if confirm else ""
    return (
        f'<form method="post" action="{action}" style="display:inline"{onsubmit}>'
        f'<button style="{style}">{label}</button></form>'
    )


# ---------------------------------------------------------------------------- shared plumbing


class _Store:
    """Persistent dict-of-dicts state for one fake (JSON file, written after every change)."""

    def __init__(self, name: str, tables: tuple[str, ...]):
        self.name = name
        self.tables = tables
        self.data: dict[str, dict[str, Any]] = {t: {} for t in tables}
        self.loaded = False

    @property
    def path(self):
        return config.DATA_DIR / "billing_fakes.json"

    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")).get(self.name) or {}
        except (OSError, ValueError):
            raw = {}
        for table in self.tables:
            self.data[table] = dict(raw.get(table) or {})

    def save(self) -> None:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                whole = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                whole = {}
            whole[self.name] = self.data
            self.path.write_text(json.dumps(whole, indent=1, default=str), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - read-only data dir
            log.warning("could not persist %s fake state: %s", self.name, exc)

    def reset(self) -> None:
        self.data = {t: {} for t in self.tables}
        self.loaded = True
        self.save()


class _Webhooks:
    """Queue of outgoing webhooks; delivered in-process, one at a time, after the action that
    produced them — a delivery that itself triggers more (PayPal capture) just queues them."""

    def __init__(self):
        self.pending: list[tuple[str, bytes, dict[str, str]]] = []
        self.lock = threading.RLock()
        self.delivering = False

    def emit(self, provider: str, body: bytes, headers: dict[str, str]) -> None:
        with self.lock:
            self.pending.append((provider, body, headers))
            if not self.delivering:
                self.flush()

    def flush(self) -> None:
        import billing
        import payments
        from database import SessionLocal

        with self.lock:
            self.delivering = True
            try:
                while self.pending:
                    provider, body, headers = self.pending.pop(0)
                    method = payments.METHODS[provider]
                    try:
                        with SessionLocal() as db:
                            result = billing.receive_webhook(db, method, body, headers)
                        log.info("fake %s webhook delivered: %s", provider, result)
                    except HTTPException as exc:
                        log.warning("fake %s webhook rejected: %s %s", provider, exc.status_code, exc.detail)
            finally:
                self.delivering = False


WEBHOOKS = _Webhooks()


# ---------------------------------------------------------------------------- Stripe


class FakeStripe:
    """Stripe: Checkout Sessions, Subscriptions, Invoices/Charges, Refunds, Billing Portal,
    signed events."""

    name = "stripe"
    secret_key = "sk_test_fake_builtin"
    webhook_secret = "whsec_fake_builtin"
    base = "/api/v1/billing/fake/stripe"

    def __init__(self):
        self.store = _Store("stripe", ("customers", "subscriptions", "sessions", "charges"))
        self.transport = _StripeTransport(self)

    # ---- state -------------------------------------------------------------------------

    @property
    def customers(self) -> dict[str, dict[str, Any]]:
        self.store.load()
        return self.store.data["customers"]

    @property
    def subscriptions(self) -> dict[str, dict[str, Any]]:
        self.store.load()
        return self.store.data["subscriptions"]

    @property
    def sessions(self) -> dict[str, dict[str, Any]]:
        self.store.load()
        return self.store.data["sessions"]

    @property
    def charges(self) -> dict[str, dict[str, Any]]:
        """Keyed by payment intent id (pi_…): what a refund is issued against."""
        self.store.load()
        return self.store.data["charges"]

    def reset(self) -> None:
        self.store.reset()

    def customer_for(self, email: str, account_id: str | None, *, customer_id: str | None = None) -> str:
        for cid, c in self.customers.items():
            if c["email"] == email:
                return cid
        cid = customer_id or "cus_fake_" + secrets.token_hex(6)
        self.customers[cid] = {"id": cid, "email": email, "account_id": account_id, "created": int(time.time())}
        self.store.save()
        return cid

    def adopt(
        self,
        account_id: str,
        email: str,
        *,
        subscription_id: str,
        customer_id: str,
        plan: str,
        status: str,
        period_end: datetime | None,
    ) -> None:
        """Register a subscription that exists in our database (seeded accounts) so the portal,
        cancel and refund work for it too."""
        self.customers[customer_id] = {"id": customer_id, "email": email, "account_id": account_id}
        self.subscriptions[subscription_id] = self._subscription(
            subscription_id, customer_id, account_id, plan, status, period_end
        )
        self.store.save()

    def adopt_charge(
        self,
        payment_intent: str,
        *,
        customer_id: str,
        account_id: str,
        plan: str,
        amount_cents: int,
        subscription_id: str | None = None,
        paid: bool = True,
    ) -> None:
        """Register a charge that exists in our ledger (seeded accounts) so it can be refunded."""
        self.charges[payment_intent] = {
            "id": "ch_" + payment_intent[3:],
            "payment_intent": payment_intent,
            "customer": customer_id,
            "subscription": subscription_id,
            "amount": amount_cents,
            "amount_refunded": 0,
            "paid": paid,
            "refunds": [],
            "metadata": {"account_id": account_id, "plan": plan},
            "created": int(time.time()),
        }
        self.store.save()

    @staticmethod
    def _subscription(sid, cid, account_id, plan, status, period_end, cancel_at_period_end=False) -> dict[str, Any]:
        return {
            "id": sid,
            "object": "subscription",
            "customer": cid,
            "status": status,
            "plan": plan,
            "cancel_at_period_end": cancel_at_period_end,
            "current_period_end": int(period_end.timestamp()) if period_end else None,
            "ended_at": None,
            "metadata": {"account_id": account_id, "plan": plan},
        }

    def _object(self, sub: dict[str, Any]) -> dict[str, Any]:
        """The subscription as Stripe's current API returns it (period on the item)."""
        return {
            **sub,
            "items": {
                "data": [
                    {
                        "price": {
                            "unit_amount": _price_cents(sub["plan"]),
                            "currency": config.BILLING_CURRENCY.lower(),
                        },
                        "current_period_end": sub["current_period_end"],
                    }
                ]
            },
        }

    # ---- events ------------------------------------------------------------------------

    def emit(self, kind: str, obj: dict[str, Any]) -> None:
        import payments

        body = json.dumps(
            {
                "id": "evt_fake_" + secrets.token_hex(8),
                "type": kind,
                "created": int(time.time()),
                "data": {"object": obj},
            }
        ).encode()
        WEBHOOKS.emit(
            "stripe", body, {"Stripe-Signature": payments.StripePayment.sign(body, secret=self.webhook_secret)}
        )

    def _charge(
        self, cid: str, account_id: str, plan: str, amount: int, *, subscription_id: str | None, paid: bool
    ) -> dict[str, Any]:
        pi = "pi_fake_" + secrets.token_hex(8)
        self.adopt_charge(
            pi,
            customer_id=cid,
            account_id=account_id,
            plan=plan,
            amount_cents=amount,
            subscription_id=subscription_id,
            paid=paid,
        )
        return self.charges[pi]

    def _invoice(self, sub: dict[str, Any], charge: dict[str, Any], *, paid: bool, period_start: int) -> dict[str, Any]:
        """An invoice for one subscription period (what invoice.paid / payment_failed carry)."""
        return {
            "id": "in_fake_" + secrets.token_hex(6),
            "object": "invoice",
            "customer": sub["customer"],
            "subscription": sub["id"],
            "payment_intent": charge["payment_intent"],
            "charge": charge["id"] if paid else None,
            "amount_due": charge["amount"],
            "amount_paid": charge["amount"] if paid else 0,
            "currency": config.BILLING_CURRENCY.lower(),
            "created": int(time.time()),
            "status_transitions": {"paid_at": int(time.time()) if paid else None},
            "last_payment_error": None if paid else {"message": "Your card was declined."},
            "lines": {"data": [{"period": {"start": period_start, "end": sub["current_period_end"]}}]},
            "subscription_details": {"metadata": dict(sub["metadata"])},
        }

    # ---- what a buyer / Stripe's scheduler does ------------------------------------------

    def pay_session(self, session_id: str, *, declined: bool = False) -> str:
        """The buyer paid on the checkout page (or tried with a card that is declined).
        Returns the URL to send the browser to."""
        session = self.sessions.get(session_id)
        if not session or session["status"] != "open":
            raise HTTPException(status_code=404, detail="Unknown or already completed checkout session.")
        account_id = session["client_reference_id"]
        cid = self.customer_for(session["customer_email"], account_id)
        plan = session["plan"]
        if session["mode"] == "payment":
            charge = self._charge(cid, account_id, plan, session["amount"], subscription_id=None, paid=not declined)
            session.update(
                {
                    "status": "complete",
                    "payment_status": "unpaid" if declined else "paid",
                    "customer": cid,
                    "payment_intent": charge["payment_intent"],
                }
            )
            self.store.save()
            if declined:
                # A delayed payment method that did not go through: Stripe reports it later.
                self.emit("checkout.session.async_payment_failed", self._session_object(session))
                return session["cancel_url"]
            self.emit("checkout.session.completed", self._session_object(session))
            return session["success_url"]
        if declined:
            return f"{self.base}/checkout/{session_id}/declined"  # the session stays open, nothing happens
        sid = "sub_fake_" + secrets.token_hex(6)
        trial_end = session.get("trial_end")
        if trial_end and trial_end > time.time():
            status, period_end = "trialing", datetime.fromtimestamp(trial_end, tz=timezone.utc)
        else:
            status, period_end = "active", _now() + timedelta(days=PERIOD_DAYS.get(plan, 30))
        sub = self._subscription(sid, cid, account_id, plan, status, period_end)
        self.subscriptions[sid] = sub
        session.update({"status": "complete", "payment_status": "paid", "customer": cid, "subscription": sid})
        self.store.save()
        self.emit("customer.subscription.created", self._object(sub))
        if status == "active":  # the first period is charged right away (a trial is not)
            charge = self._charge(cid, account_id, plan, session["amount"], subscription_id=sid, paid=True)
            self.emit("invoice.paid", self._invoice(sub, charge, paid=True, period_start=int(time.time())))
        self.emit("checkout.session.completed", self._session_object(session))
        return session["success_url"]

    def _session_object(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session["id"],
            "object": "checkout.session",
            "mode": session["mode"],
            "payment_status": session["payment_status"],
            "client_reference_id": session["client_reference_id"],
            "customer": session.get("customer"),
            "subscription": session.get("subscription"),
            "payment_intent": session.get("payment_intent"),
            "amount_total": session["amount"],
            "currency": config.BILLING_CURRENCY.lower(),
            "metadata": {"account_id": session["client_reference_id"], "plan": session["plan"]},
        }

    def cancel_now(self, sid: str) -> None:
        sub = self._sub(sid)
        sub.update({"status": "canceled", "cancel_at_period_end": False, "ended_at": int(time.time())})
        self.store.save()
        self.emit("customer.subscription.deleted", self._object(sub))

    def renew(self, sid: str) -> None:
        """The period ended and Stripe collected the next payment."""
        sub = self._sub(sid)
        if sub["status"] == "canceled":
            raise HTTPException(status_code=409, detail="A canceled subscription does not renew.")
        start = max(_now(), datetime.fromtimestamp(sub["current_period_end"] or 0, tz=timezone.utc))
        sub["current_period_end"] = int((start + timedelta(days=PERIOD_DAYS.get(sub["plan"], 30))).timestamp())
        sub["status"] = "active"
        self.store.save()
        charge = self._charge(
            sub["customer"],
            sub["metadata"]["account_id"],
            sub["plan"],
            _price_cents(sub["plan"]),
            subscription_id=sid,
            paid=True,
        )
        self.emit("invoice.paid", self._invoice(sub, charge, paid=True, period_start=int(start.timestamp())))
        self.emit("customer.subscription.updated", self._object(sub))

    def fail_payment(self, sid: str) -> None:
        """The renewal charge failed: Stripe opens dunning (past_due)."""
        sub = self._sub(sid)
        if sub["status"] == "canceled":
            raise HTTPException(status_code=409, detail="A canceled subscription is not charged.")
        sub["status"] = "past_due"
        sub["current_period_end"] = int(time.time())  # the paid period is over
        self.store.save()
        charge = self._charge(
            sub["customer"],
            sub["metadata"]["account_id"],
            sub["plan"],
            _price_cents(sub["plan"]),
            subscription_id=sid,
            paid=False,
        )
        sub["open_invoice_intent"] = charge["payment_intent"]
        self.store.save()
        self.emit("invoice.payment_failed", self._invoice(sub, charge, paid=False, period_start=int(time.time())))
        self.emit("customer.subscription.updated", self._object(sub))

    def recover_payment(self, sid: str) -> None:
        """The buyer updated the card and the open invoice was paid (same payment intent)."""
        sub = self._sub(sid)
        if sub["status"] != "past_due":
            raise HTTPException(status_code=409, detail="There is no open invoice to pay.")
        start = _now()
        sub["status"] = "active"
        sub["current_period_end"] = int((start + timedelta(days=PERIOD_DAYS.get(sub["plan"], 30))).timestamp())
        pi = sub.pop("open_invoice_intent", None)
        charge = self.charges.get(pi) if pi else None
        if charge is None:
            charge = self._charge(
                sub["customer"],
                sub["metadata"]["account_id"],
                sub["plan"],
                _price_cents(sub["plan"]),
                subscription_id=sid,
                paid=True,
            )
        charge["paid"] = True
        self.store.save()
        self.emit("invoice.paid", self._invoice(sub, charge, paid=True, period_start=int(start.timestamp())))
        self.emit("customer.subscription.updated", self._object(sub))

    def refund(self, payment_intent: str, amount: int | None) -> dict[str, Any]:
        """POST /v1/refunds (from the API) or "Refund" in the dashboard (portal page)."""
        charge = self.charges.get(payment_intent)
        if charge is None or not charge["paid"]:
            raise HTTPException(status_code=404, detail=f"No such paid charge: {payment_intent}")
        left = charge["amount"] - charge["amount_refunded"]
        cents = left if amount is None else amount
        if cents <= 0 or cents > left:
            raise HTTPException(status_code=400, detail="Refund amount exceeds what is left to refund.")
        refund = {"id": "re_fake_" + secrets.token_hex(6), "amount": cents, "created": int(time.time())}
        charge["amount_refunded"] += cents
        charge["refunds"].insert(0, refund)
        self.store.save()
        self.emit(
            "charge.refunded",
            {
                "id": charge["id"],
                "object": "charge",
                "payment_intent": payment_intent,
                "customer": charge["customer"],
                "amount": charge["amount"],
                "amount_refunded": charge["amount_refunded"],
                "refunded": charge["amount_refunded"] >= charge["amount"],
                "currency": config.BILLING_CURRENCY.lower(),
                "metadata": dict(charge["metadata"]),
                "refunds": {"data": list(charge["refunds"])},
            },
        )
        return refund

    def _sub(self, sid: str) -> dict[str, Any]:
        sub = self.subscriptions.get(sid)
        if not sub:
            raise HTTPException(status_code=404, detail=f"No such subscription: {sid}")
        return sub


class _StripeTransport:
    """Answers the Stripe REST calls payments.StripePayment makes (form-encoded in, JSON out)."""

    def __init__(self, fake: FakeStripe):
        self.fake = fake

    def request(self, method: str, url: str, *, headers=None, data: bytes | None = None) -> tuple[int, dict]:
        if (headers or {}).get("Authorization") != "Bearer " + self.fake.secret_key:
            return 401, {"error": {"message": "Invalid API Key provided (fake Stripe)."}}
        path = urlsplit(url).path
        form = _form(data)
        f = self.fake
        if method == "POST" and path == "/v1/checkout/sessions":
            amount = form.get("line_items[0][price_data][unit_amount]")
            if not amount or not amount.isdigit():
                return 400, {"error": {"message": "Missing required param: line_items[0][price_data][unit_amount]."}}
            plan = (
                "lifetime"
                if form.get("mode") == "payment"
                else (
                    "pro_yearly"
                    if form.get("line_items[0][price_data][recurring][interval]") == "year"
                    else "pro_monthly"
                )
            )
            plan = form.get("metadata[plan]") or plan
            sid = "cs_fake_" + secrets.token_hex(8)
            f.sessions[sid] = {
                "id": sid,
                "status": "open",
                "payment_status": "unpaid",
                "mode": form.get("mode", "subscription"),
                "plan": plan,
                "amount": int(amount),
                "client_reference_id": form.get("client_reference_id"),
                "customer_email": form.get("customer_email", ""),
                "success_url": form.get("success_url", "/"),
                "cancel_url": form.get("cancel_url", "/"),
                "trial_end": (
                    int(form["subscription_data[trial_end]"]) if form.get("subscription_data[trial_end]") else None
                ),
            }
            f.store.save()
            return 200, {"id": sid, "object": "checkout.session", "url": f"{f.base}/checkout/{sid}"}
        if path.startswith("/v1/subscriptions/"):
            sid = path.rsplit("/", 1)[1]
            if sid not in f.subscriptions:
                return 404, {"error": {"message": f"No such subscription: '{sid}'"}}
            if method == "GET":
                return 200, f._object(f.subscriptions[sid])
            if method == "DELETE":
                f.cancel_now(sid)
                return 200, f._object(f.subscriptions[sid])
            if method == "POST":
                return 200, f._object(f.subscriptions[sid])
        if method == "POST" and path == "/v1/refunds":
            pi = form.get("payment_intent")
            if not pi and form.get("charge"):
                pi = next((p for p, c in f.charges.items() if c["id"] == form["charge"]), None)
            if not pi or pi not in f.charges:
                return 404, {"error": {"message": f"No such payment_intent: '{form.get('payment_intent')}'"}}
            try:
                refund = f.refund(pi, int(form["amount"]) if form.get("amount") else None)
            except HTTPException as exc:
                return exc.status_code, {"error": {"message": str(exc.detail)}}
            return 200, {"id": refund["id"], "object": "refund", "amount": refund["amount"], "status": "succeeded"}
        if method == "POST" and path == "/v1/billing_portal/sessions":
            cid = form.get("customer", "")
            if cid not in f.customers:
                return 400, {"error": {"message": f"No such customer: '{cid}'"}}
            query = urlencode({"return_url": form.get("return_url", "/")})
            return 200, {"id": "bps_fake", "url": f"{f.base}/portal/{cid}?{query}"}
        return 404, {"error": {"message": f"Unrecognized request URL ({method}: {path}) — fake Stripe"}}


# ---------------------------------------------------------------------------- PayPal


class FakePayPal:
    """PayPal: OAuth, Subscriptions API, Orders v2, captures + refunds, webhook-signature
    verification."""

    name = "paypal"
    client_id = "fake-paypal-client-id"
    client_secret = "fake-paypal-client-secret"
    webhook_id = "WH-FAKE-BUILTIN"
    plan_ids = {"pro_monthly": "P-FAKE-MONTHLY", "pro_yearly": "P-FAKE-YEARLY"}
    base = "/api/v1/billing/fake/paypal"

    def __init__(self):
        self.store = _Store("paypal", ("subscriptions", "orders", "captures", "signatures"))
        self.transport = _PayPalTransport(self)

    @property
    def subscriptions(self) -> dict[str, dict[str, Any]]:
        self.store.load()
        return self.store.data["subscriptions"]

    @property
    def orders(self) -> dict[str, dict[str, Any]]:
        self.store.load()
        return self.store.data["orders"]

    @property
    def captures(self) -> dict[str, dict[str, Any]]:
        """Sales (subscription charges) and captures (orders) — what a refund is issued against."""
        self.store.load()
        return self.store.data["captures"]

    @property
    def signatures(self) -> dict[str, dict[str, Any]]:
        self.store.load()
        return self.store.data["signatures"]

    def reset(self) -> None:
        self.store.reset()

    def plan_of(self, plan_id: str) -> str:
        return next((plan for plan, p in self.plan_ids.items() if p == plan_id), "pro_monthly")

    def manage_url(self, subscription_id: str | None, return_url: str) -> str | None:
        if not subscription_id or subscription_id not in self.subscriptions:
            return None
        return f"{self.base}/autopay/{subscription_id}?" + urlencode({"return_url": return_url})

    # ---- events ------------------------------------------------------------------------

    def emit(self, kind: str, resource: dict[str, Any]) -> None:
        event_id = "WH-FAKE-" + secrets.token_hex(8)
        body = json.dumps(
            {"id": event_id, "event_type": kind, "resource": resource, "create_time": _iso(_now())}
        ).encode()
        transmission = "fake-tx-" + secrets.token_hex(8)
        signature = secrets.token_hex(16)
        self.signatures[transmission] = {"sig": signature, "event_id": event_id}
        self.store.save()
        WEBHOOKS.emit(
            "paypal",
            body,
            {
                "PAYPAL-AUTH-ALGO": "SHA256withRSA",
                "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/fake",
                "PAYPAL-TRANSMISSION-ID": transmission,
                "PAYPAL-TRANSMISSION-SIG": signature,
                "PAYPAL-TRANSMISSION-TIME": _iso(_now()) or "",
            },
        )

    def _resource(self, sub: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in sub.items() if k not in ("return_url", "cancel_url", "plan")}

    def _money(self, cents: int) -> dict[str, str]:
        return {"currency_code": config.BILLING_CURRENCY, "value": _money(cents)}

    def _sale(self, sub: dict[str, Any]) -> dict[str, Any]:
        """One subscription charge (a "sale"): recorded as a capture so it can be refunded."""
        sale_id = "SALE" + secrets.token_hex(5).upper()
        cents = _price_cents(sub["plan"])
        self.captures[sale_id] = {
            "id": sale_id,
            "kind": "sale",
            "status": "COMPLETED",
            "amount": self._money(cents),
            "custom_id": sub["custom_id"],
            "billing_agreement_id": sub["id"],
            "create_time": _iso(_now()),
            "refunds": [],
        }
        sub["billing_info"]["last_payment"] = {"amount": self._money(cents), "time": _iso(_now())}
        self.store.save()
        return self.captures[sale_id]

    # ---- what a buyer / PayPal does ------------------------------------------------------

    def approve_subscription(self, sid: str) -> str:
        sub = self._sub(sid)
        if sub["status"] != "APPROVAL_PENDING":
            raise HTTPException(status_code=409, detail="This subscription was already approved.")
        start = _parse_iso(sub.get("start_time"))
        deferred = bool(start and start > _now())
        sub["status"] = "ACTIVE"
        sub["subscriber"]["payer_id"] = sub["subscriber"].get("payer_id") or "PAYER" + secrets.token_hex(4).upper()
        next_billing = start if deferred else _now() + timedelta(days=PERIOD_DAYS.get(sub["plan"], 30))
        sub["billing_info"] = {"next_billing_time": _iso(next_billing)}
        self.store.save()
        self.emit("BILLING.SUBSCRIPTION.ACTIVATED", self._resource(sub))
        if not deferred:  # the first period is charged right away
            sale = self._sale(sub)
            self.emit("PAYMENT.SALE.COMPLETED", {k: v for k, v in sale.items() if k not in ("kind", "refunds")})
        return sub["return_url"]

    def cancel_subscription(self, sid: str) -> None:
        sub = self._sub(sid)
        sub["status"] = "CANCELLED"
        sub["billing_info"] = {k: v for k, v in sub.get("billing_info", {}).items() if k != "next_billing_time"}
        self.store.save()
        self.emit("BILLING.SUBSCRIPTION.CANCELLED", self._resource(sub))

    def renew(self, sid: str) -> None:
        """PayPal collected the next payment: a sale naming the billing agreement."""
        sub = self._sub(sid)
        if sub["status"] != "ACTIVE":
            raise HTTPException(status_code=409, detail="Only an active subscription renews.")
        due = _parse_iso(sub["billing_info"].get("next_billing_time")) or _now()
        start = max(_now(), due)
        sub["billing_info"]["next_billing_time"] = _iso(start + timedelta(days=PERIOD_DAYS.get(sub["plan"], 30)))
        self.store.save()
        sale = self._sale(sub)
        self.emit("PAYMENT.SALE.COMPLETED", {k: v for k, v in sale.items() if k not in ("kind", "refunds")})

    def fail_payment(self, sid: str) -> None:
        sub = self._sub(sid)
        if sub["status"] != "ACTIVE":
            raise HTTPException(status_code=409, detail="Only an active subscription is charged.")
        sub["status"] = "SUSPENDED"  # PayPal suspends after failed retries
        sub["billing_info"]["next_billing_time"] = _iso(_now())
        sub["billing_info"]["outstanding_balance"] = self._money(_price_cents(sub["plan"]))
        sub["billing_info"]["failed_payments_count"] = 1
        self.store.save()
        self.emit("BILLING.SUBSCRIPTION.PAYMENT.FAILED", self._resource(sub))
        self.emit("BILLING.SUBSCRIPTION.SUSPENDED", self._resource(sub))

    def recover_payment(self, sid: str) -> None:
        sub = self._sub(sid)
        if sub["status"] != "SUSPENDED":
            raise HTTPException(status_code=409, detail="Only a suspended subscription is re-activated.")
        sub["status"] = "ACTIVE"
        sub["billing_info"]["next_billing_time"] = _iso(_now() + timedelta(days=PERIOD_DAYS.get(sub["plan"], 30)))
        sub["billing_info"].pop("outstanding_balance", None)
        sub["billing_info"]["failed_payments_count"] = 0
        self.store.save()
        self.emit("BILLING.SUBSCRIPTION.RE-ACTIVATED", self._resource(sub))
        sale = self._sale(sub)
        self.emit("PAYMENT.SALE.COMPLETED", {k: v for k, v in sale.items() if k not in ("kind", "refunds")})

    def approve_order(self, oid: str, *, declined: bool = False) -> str:
        order = self._order(oid)
        if order["status"] != "CREATED":
            raise HTTPException(status_code=409, detail="This order was already approved.")
        order["payer"] = {"payer_id": "PAYER" + secrets.token_hex(4).upper()}
        unit = order["purchase_units"][0]
        if declined:
            order["status"] = "VOIDED"
            capture = {
                "id": "CAP" + secrets.token_hex(4).upper(),
                "status": "DECLINED",
                "amount": unit.get("amount"),
                "custom_id": unit.get("custom_id"),
                "create_time": _iso(_now()),
            }
            self.store.save()
            self.emit("PAYMENT.CAPTURE.DENIED", capture)
            return order["cancel_url"]
        order["status"] = "APPROVED"
        self.store.save()
        # The API captures the order when this arrives (payments.PayPalPayment.parse_webhook).
        self.emit("CHECKOUT.ORDER.APPROVED", {k: v for k, v in order.items() if k not in ("return_url", "cancel_url")})
        return order["return_url"]

    def capture_order(self, oid: str) -> tuple[int, dict[str, Any]]:
        order = self._order(oid)
        if order["status"] == "COMPLETED":
            return 422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]}
        if order["status"] != "APPROVED":
            return 422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]}
        order["status"] = "COMPLETED"
        unit = order["purchase_units"][0]
        capture = {
            "id": "CAP" + secrets.token_hex(4).upper(),
            "kind": "capture",
            "status": "COMPLETED",
            "amount": unit.get("amount"),
            "custom_id": unit.get("custom_id"),
            "create_time": _iso(_now()),
            "refunds": [],
        }
        self.captures[capture["id"]] = capture
        public = {k: v for k, v in capture.items() if k not in ("kind", "refunds")}
        unit["payments"] = {"captures": [public]}
        self.store.save()
        self.emit("PAYMENT.CAPTURE.COMPLETED", public)
        return 201, {
            "id": oid,
            "status": "COMPLETED",
            "payer": order["payer"],
            "purchase_units": order["purchase_units"],
        }

    def refund(self, capture_id: str, amount_cents: int | None) -> tuple[int, dict[str, Any]]:
        """POST /v2/payments/captures/{id}/refund (from the API) or "Refund" at PayPal."""
        capture = self.captures.get(capture_id)
        if capture is None or capture["status"] != "COMPLETED":
            return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
        import payments

        total = payments.to_cents(capture["amount"]["value"])
        refunded = sum(payments.to_cents(r["amount"]["value"]) for r in capture["refunds"])
        cents = (total - refunded) if amount_cents is None else amount_cents
        if cents <= 0 or cents > total - refunded:
            return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "The refund amount exceeds the remaining amount."}
        refund = {
            "id": "REF" + secrets.token_hex(5).upper(),
            "status": "COMPLETED",
            "amount": self._money(cents),
            "custom_id": capture.get("custom_id"),
            "create_time": _iso(_now()),
            "links": [{"rel": "up", "href": f"{config.PAYPAL_API_BASE}/v2/payments/captures/{capture_id}"}],
        }
        capture["refunds"].insert(0, refund)
        self.store.save()
        self.emit("PAYMENT.CAPTURE.REFUNDED" if capture["kind"] == "capture" else "PAYMENT.SALE.REFUNDED", refund)
        return 201, refund

    def _sub(self, sid: str) -> dict[str, Any]:
        sub = self.subscriptions.get(sid)
        if not sub:
            raise HTTPException(status_code=404, detail=f"No such subscription: {sid}")
        return sub

    def _order(self, oid: str) -> dict[str, Any]:
        order = self.orders.get(oid)
        if not order:
            raise HTTPException(status_code=404, detail=f"No such order: {oid}")
        return order


class _PayPalTransport:
    """Answers the PayPal REST calls payments.PayPalPayment makes (JSON in, JSON out)."""

    token = "A21AAFakeAccessToken"

    def __init__(self, fake: FakePayPal):
        self.fake = fake

    def request(self, method: str, url: str, *, headers=None, data: bytes | None = None) -> tuple[int, dict]:
        headers = headers or {}
        path = urlsplit(url).path
        f = self.fake
        if method == "POST" and path == "/v1/oauth2/token":
            expected = base64.b64encode(f"{f.client_id}:{f.client_secret}".encode()).decode()
            if headers.get("Authorization") != "Basic " + expected:
                return 401, {
                    "error": "invalid_client",
                    "error_description": "Client Authentication failed (fake PayPal)",
                }
            return 200, {"access_token": self.token, "token_type": "Bearer", "expires_in": 32400}
        if headers.get("Authorization") != "Bearer " + self.token:
            return 401, {"name": "AUTHENTICATION_FAILURE", "message": "Authentication failed (fake PayPal)."}
        body = _json(data)
        if method == "POST" and path == "/v1/billing/subscriptions":
            plan_id = body.get("plan_id", "")
            if plan_id not in f.plan_ids.values():
                return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "The requested plan is not active."}
            sid = "I-FAKE" + secrets.token_hex(5).upper()
            context = body.get("application_context") or {}
            f.subscriptions[sid] = {
                "id": sid,
                "plan_id": plan_id,
                "plan": f.plan_of(plan_id),
                "status": "APPROVAL_PENDING",
                "custom_id": body.get("custom_id"),
                "start_time": body.get("start_time"),
                "subscriber": {"email_address": (body.get("subscriber") or {}).get("email_address", "")},
                "billing_info": {},
                "return_url": context.get("return_url", "/"),
                "cancel_url": context.get("cancel_url", "/"),
            }
            f.store.save()
            return 201, {
                "id": sid,
                "status": "APPROVAL_PENDING",
                "links": [{"rel": "approve", "href": f"{f.base}/approve/subscription/{sid}"}],
            }
        if path.startswith("/v1/billing/subscriptions/"):
            parts = path.split("/")
            sid = parts[4]
            if sid not in f.subscriptions:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            if method == "GET" and len(parts) == 5:
                return 200, f._resource(f.subscriptions[sid])
            if method == "POST" and parts[-1] == "cancel":
                if f.subscriptions[sid]["status"] == "CANCELLED":
                    return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "Subscription already cancelled."}
                f.cancel_subscription(sid)
                return 204, {}
        if method == "POST" and path == "/v2/checkout/orders":
            oid = "ORD" + secrets.token_hex(6).upper()
            context = body.get("application_context") or {}
            f.orders[oid] = {
                "id": oid,
                "status": "CREATED",
                "intent": body.get("intent", "CAPTURE"),
                "purchase_units": body.get("purchase_units") or [{}],
                "return_url": context.get("return_url", "/"),
                "cancel_url": context.get("cancel_url", "/"),
            }
            f.store.save()
            return 201, {
                "id": oid,
                "status": "CREATED",
                "links": [{"rel": "approve", "href": f"{f.base}/approve/order/{oid}"}],
            }
        if method == "POST" and path.startswith("/v2/checkout/orders/") and path.endswith("/capture"):
            oid = path.split("/")[4]
            if oid not in f.orders:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            return f.capture_order(oid)
        if method == "POST" and path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
            import payments

            capture_id = path.split("/")[4]
            amount = body.get("amount") or {}
            cents = payments.to_cents(amount["value"]) if amount.get("value") else None
            return f.refund(capture_id, cents)
        if method == "POST" and path == "/v1/notifications/verify-webhook-signature":
            issued = f.signatures.get(body.get("transmission_id", ""))
            ok = (
                issued is not None
                and issued["sig"] == body.get("transmission_sig")
                and body.get("webhook_id") == f.webhook_id
                and (body.get("webhook_event") or {}).get("id") == issued["event_id"]
            )
            return 200, {"verification_status": "SUCCESS" if ok else "FAILURE"}
        return 404, {"name": "RESOURCE_NOT_FOUND", "message": f"{method} {path} — fake PayPal"}


STRIPE = FakeStripe()
PAYPAL = FakePayPal()


def reset() -> None:
    STRIPE.reset()
    PAYPAL.reset()


# ---------------------------------------------------------------------------- hosted pages

router = APIRouter(prefix="/billing/fake", include_in_schema=False)


def _guard() -> None:
    if not config.BILLING_FAKE_PROVIDERS or config.IS_PROD:
        raise HTTPException(status_code=404)


def _fmt_ts(value) -> str:
    if not value:
        return "—"
    dt = datetime.fromtimestamp(value, tz=timezone.utc) if isinstance(value, (int, float)) else _parse_iso(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _cur() -> str:
    return config.BILLING_CURRENCY


def _simulate(provider: str, buttons: list[str]) -> str:
    """The collapsible "what would the provider do next" block — only while there is something."""
    if not buttons:
        return ""
    return (
        f'<details style="margin-top:8px"><summary style="cursor:pointer;color:#666">'
        f"Simulate what {provider} would do next</summary>"
        f'<div style="margin-top:6px">{"".join(buttons)}</div></details>'
    )


# ---- Stripe


@router.get("/stripe/checkout/{session_id}", response_class=HTMLResponse)
def stripe_checkout(session_id: str):
    _guard()
    session = STRIPE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown checkout session.")
    if session["status"] != "open":
        back = session["success_url"] if session["payment_status"] == "paid" else session["cancel_url"]
        return _page(
            "Stripe (fake) — Checkout", f'<h2>This checkout is finished</h2><p><a href="{back}">Back to the app</a></p>'
        )
    plan = session["plan"]
    price = _money(session["amount"])
    what = (
        "one-off payment"
        if session["mode"] == "payment"
        else ("subscription — first charge when the trial ends" if session.get("trial_end") else "subscription")
    )
    return _page(
        "Stripe (fake) — Checkout",
        f"""<h2>Pay with card</h2>
<p><strong>{_label(plan)}</strong> · {price} {_cur()} · {what}<br>
<span style="color:#666">for {session["customer_email"]}</span></p>
<p><input placeholder="4242 4242 4242 4242" style="padding:8px;width:60%" disabled>
<input placeholder="MM/YY" style="padding:8px;width:16%" disabled>
<input placeholder="CVC" style="padding:8px;width:14%" disabled></p>
{_button(f"Pay {price} {_cur()}", f"{STRIPE.base}/checkout/{session_id}/pay", primary=True)}
{_button("Pay with a card that is declined", f"{STRIPE.base}/checkout/{session_id}/decline")}
<p><a href="{session["cancel_url"]}">Cancel and go back</a></p>""",
    )


@router.post("/stripe/checkout/{session_id}/pay")
def stripe_checkout_pay(session_id: str):
    _guard()
    return RedirectResponse(url=STRIPE.pay_session(session_id), status_code=303)


@router.post("/stripe/checkout/{session_id}/decline")
def stripe_checkout_decline(session_id: str):
    _guard()
    return RedirectResponse(url=STRIPE.pay_session(session_id, declined=True), status_code=303)


@router.get("/stripe/checkout/{session_id}/declined", response_class=HTMLResponse)
def stripe_checkout_declined(session_id: str):
    _guard()
    session = STRIPE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown checkout session.")
    return _page(
        "Stripe (fake) — Checkout",
        f"""<h2 style="color:#b00">Your card was declined</h2>
<p>Nothing was charged and no subscription was created. You can try another card or go back.</p>
<p><a href="{STRIPE.base}/checkout/{session_id}">Try again</a> ·
<a href="{session["cancel_url"]}">Back to the app</a></p>""",
    )


@router.get("/stripe/portal/{customer_id}", response_class=HTMLResponse)
def stripe_portal(customer_id: str, return_url: str = "/"):
    _guard()
    customer = STRIPE.customers.get(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Unknown customer.")
    back = "?" + urlencode({"return_url": return_url})
    rows = []
    for s in (s for s in STRIPE.subscriptions.values() if s["customer"] == customer_id):
        base = f"{STRIPE.base}/portal/{customer_id}/{s['id']}"
        actions = []
        if s["status"] in ("active", "trialing", "past_due"):
            actions.append(
                _button("Cancel immediately", f"{base}/cancel-now{back}", confirm="End the subscription now at Stripe?")
            )
        simulate = []
        if s["status"] in ("active", "trialing"):
            simulate.append(_button("Period ends → renewal collected", f"{base}/renew{back}"))
            simulate.append(_button("Renewal payment fails (past due)", f"{base}/fail{back}"))
        if s["status"] == "past_due":
            simulate.append(_button("Card updated → open invoice paid", f"{base}/recover{back}"))
        rows.append(
            f"""<li style="margin-bottom:16px"><strong>{_label(s["plan"])}</strong> ({s["id"]})<br>
status: {s["status"]}<br>current period ends: {_fmt_ts(s["current_period_end"])}<br>
<div style="margin-top:6px">{"".join(actions)}</div>{_simulate("Stripe", simulate)}</li>"""
        )
    charges = []
    mine = [c for c in STRIPE.charges.values() if c["customer"] == customer_id]
    for c in reversed(sorted(mine, key=lambda c: c["created"])):  # newest first, ties by insertion
        left = c["amount"] - c["amount_refunded"]
        state = (
            "failed"
            if not c["paid"]
            else (
                "refunded"
                if left == 0
                else (f"refunded {_money(c['amount_refunded'])}" if c["amount_refunded"] else "paid")
            )
        )
        refund = (
            _button(
                "Refund in full (dashboard)",
                f"{STRIPE.base}/portal/{customer_id}/charge/{c['payment_intent']}/refund{back}",
            )
            if c["paid"] and left > 0
            else ""
        )
        charges.append(
            f"""<li>{_fmt_ts(c["created"])} · {_label(c["metadata"].get("plan", ""))} · {_money(c["amount"])} {_cur()}
· <em>{state}</em> <span style="color:#888">({c["payment_intent"]})</span> {refund}</li>"""
        )
    return _page(
        "Stripe (fake) — Customer portal",
        f"""<h2>Billing for {customer["email"]}</h2>
<ul style="list-style:none;padding:0">{"".join(rows) or "<li>No subscriptions.</li>"}</ul>
<h3>Payments</h3>
<ul style="padding-left:18px">{"".join(charges) or "<li>No payments yet.</li>"}</ul>
<p><a href="{return_url}">← Return to Personal Assistant</a></p>""",
    )


@router.post("/stripe/portal/{customer_id}/charge/{payment_intent}/refund")
def stripe_portal_refund(customer_id: str, payment_intent: str, return_url: str = "/"):
    """An operator refunding from the Stripe dashboard: only the charge.refunded webhook tells us."""
    _guard()
    charge = STRIPE.charges.get(payment_intent)
    if not charge or charge["customer"] != customer_id:
        raise HTTPException(status_code=404, detail="Unknown charge.")
    STRIPE.refund(payment_intent, None)
    return RedirectResponse(
        url=f"{STRIPE.base}/portal/{customer_id}?" + urlencode({"return_url": return_url}), status_code=303
    )


@router.post("/stripe/portal/{customer_id}/{subscription_id}/{action}")
def stripe_portal_action(customer_id: str, subscription_id: str, action: str, return_url: str = "/"):
    _guard()
    sub = STRIPE.subscriptions.get(subscription_id)
    if not sub or sub["customer"] != customer_id:
        raise HTTPException(status_code=404, detail="Unknown subscription.")
    actions = {
        "cancel-now": lambda: STRIPE.cancel_now(subscription_id),
        "renew": lambda: STRIPE.renew(subscription_id),
        "fail": lambda: STRIPE.fail_payment(subscription_id),
        "recover": lambda: STRIPE.recover_payment(subscription_id),
    }
    if action not in actions:
        raise HTTPException(status_code=404)
    actions[action]()
    return RedirectResponse(
        url=f"{STRIPE.base}/portal/{customer_id}?" + urlencode({"return_url": return_url}), status_code=303
    )


# ---- PayPal


@router.get("/paypal/approve/subscription/{subscription_id}", response_class=HTMLResponse)
def paypal_approve_subscription(subscription_id: str):
    _guard()
    sub = PAYPAL.subscriptions.get(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Unknown subscription.")
    if sub["status"] != "APPROVAL_PENDING":
        return _page(
            "PayPal (fake)", f'<h2>Already approved</h2><p><a href="{sub["return_url"]}">Back to the app</a></p>'
        )
    starts = _parse_iso(sub.get("start_time"))
    when = (
        f"first charge on {starts.strftime('%Y-%m-%d')} (after your trial)"
        if starts and starts > _now()
        else "first charge today"
    )
    plan = sub["plan"]
    return _page(
        "PayPal (fake) — Approve subscription",
        f"""<h2>Agree to automatic payments</h2>
<p><strong>Personal Assistant — {_label(plan)}</strong> · {_money(_price_cents(plan))} {_cur()} · {when}<br>
<span style="color:#666">PayPal account: {sub["subscriber"].get("email_address") or "—"}</span></p>
{_button("Agree & Subscribe", f"{PAYPAL.base}/approve/subscription/{subscription_id}/approve", primary=True)}
<p><a href="{sub["cancel_url"]}">Cancel and return</a></p>""",
    )


@router.post("/paypal/approve/subscription/{subscription_id}/approve")
def paypal_approve_subscription_do(subscription_id: str):
    _guard()
    return RedirectResponse(url=PAYPAL.approve_subscription(subscription_id), status_code=303)


@router.get("/paypal/approve/order/{order_id}", response_class=HTMLResponse)
def paypal_approve_order(order_id: str):
    _guard()
    order = PAYPAL.orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Unknown order.")
    if order["status"] != "CREATED":
        back = order["return_url"] if order["status"] in ("APPROVED", "COMPLETED") else order["cancel_url"]
        return _page("PayPal (fake)", f'<h2>This order is finished</h2><p><a href="{back}">Back to the app</a></p>')
    unit = order["purchase_units"][0]
    amount = unit.get("amount") or {}
    return _page(
        "PayPal (fake) — Pay",
        f"""<h2>Pay now</h2>
<p><strong>{unit.get("description", "Purchase")}</strong> ·
{amount.get("currency_code", "")} {amount.get("value", "?")}</p>
{_button("Pay now", f"{PAYPAL.base}/approve/order/{order_id}/approve", primary=True)}
{_button("Pay with a funding source that is declined", f"{PAYPAL.base}/approve/order/{order_id}/decline")}
<p><a href="{order["cancel_url"]}">Cancel and return</a></p>""",
    )


@router.post("/paypal/approve/order/{order_id}/approve")
def paypal_approve_order_do(order_id: str):
    _guard()
    return RedirectResponse(url=PAYPAL.approve_order(order_id), status_code=303)


@router.post("/paypal/approve/order/{order_id}/decline")
def paypal_approve_order_decline(order_id: str):
    _guard()
    return RedirectResponse(url=PAYPAL.approve_order(order_id, declined=True), status_code=303)


@router.get("/paypal/autopay/{subscription_id}", response_class=HTMLResponse)
def paypal_autopay(subscription_id: str, return_url: str = "/"):
    _guard()
    sub = PAYPAL.subscriptions.get(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Unknown subscription.")
    base = f"{PAYPAL.base}/autopay/{subscription_id}"
    back = "?" + urlencode({"return_url": return_url})
    actions = []
    if sub["status"] in ("ACTIVE", "SUSPENDED"):
        actions.append(
            _button("Cancel automatic payments", f"{base}/cancel{back}", confirm="Cancel this subscription at PayPal?")
        )
    simulate = []
    if sub["status"] == "ACTIVE":
        simulate.append(_button("Next payment collected", f"{base}/renew{back}"))
        simulate.append(_button("Payment fails (suspended)", f"{base}/fail{back}"))
    if sub["status"] == "SUSPENDED":
        simulate.append(_button("Funding fixed → re-activated", f"{base}/recover{back}"))
    sales = []
    mine = [c for c in PAYPAL.captures.values() if c.get("billing_agreement_id") == subscription_id]
    for c in reversed(sorted(mine, key=lambda c: c["create_time"])):  # newest first, ties by insertion
        import payments

        total = payments.to_cents(c["amount"]["value"])
        refunded = sum(payments.to_cents(r["amount"]["value"]) for r in c["refunds"])
        state = "refunded" if refunded >= total else (f"refunded {_money(refunded)}" if refunded else "completed")
        refund = (
            _button(
                "Refund in full (PayPal)", f"{PAYPAL.base}/autopay/{subscription_id}/capture/{c['id']}/refund{back}"
            )
            if refunded < total
            else ""
        )
        sales.append(
            f"""<li>{_fmt_ts(c["create_time"])} · {c["amount"]["value"]} {c["amount"]["currency_code"]} ·
<em>{state}</em> <span style="color:#888">({c["id"]})</span> {refund}</li>"""
        )
    return _page(
        "PayPal (fake) — Automatic payments",
        f"""<h2>Personal Assistant — {_label(sub["plan"])}</h2>
<p>status: {sub["status"]}<br>next payment: {_fmt_ts(sub.get("billing_info", {}).get("next_billing_time"))}</p>
<div>{"".join(actions)}</div>{_simulate("PayPal", simulate)}
<h3>Payments</h3>
<ul style="padding-left:18px">{"".join(sales) or "<li>No payments yet.</li>"}</ul>
<p><a href="{return_url}">← Return to Personal Assistant</a></p>""",
    )


@router.post("/paypal/autopay/{subscription_id}/capture/{capture_id}/refund")
def paypal_autopay_refund(subscription_id: str, capture_id: str, return_url: str = "/"):
    """A refund issued at PayPal itself: only the PAYMENT.SALE.REFUNDED webhook tells us."""
    _guard()
    PAYPAL._sub(subscription_id)
    status, body = PAYPAL.refund(capture_id, None)
    if status >= 400:
        raise HTTPException(status_code=status, detail=body.get("message", "refund failed"))
    return RedirectResponse(
        url=f"{PAYPAL.base}/autopay/{subscription_id}?" + urlencode({"return_url": return_url}), status_code=303
    )


@router.post("/paypal/autopay/{subscription_id}/{action}")
def paypal_autopay_action(subscription_id: str, action: str, return_url: str = "/"):
    _guard()
    PAYPAL._sub(subscription_id)
    actions = {
        "cancel": lambda: PAYPAL.cancel_subscription(subscription_id),
        "renew": lambda: PAYPAL.renew(subscription_id),
        "fail": lambda: PAYPAL.fail_payment(subscription_id),
        "recover": lambda: PAYPAL.recover_payment(subscription_id),
    }
    if action not in actions:
        raise HTTPException(status_code=404)
    actions[action]()
    return RedirectResponse(
        url=f"{PAYPAL.base}/autopay/{subscription_id}?" + urlencode({"return_url": return_url}), status_code=303
    )
