"""Stripe Checkout + webhook, TEST MODE ONLY.

Keys come from the environment (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_LAB). This module
refuses live keys and live-mode events outright: going live is a deliberate code change, not a config flip.
"""
from __future__ import annotations

import logging

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ServiceSettings
from .db import StripeEvent, User

log = logging.getLogger("receipts.service.billing")


class BillingError(Exception):
    pass


def require_test_key(key: str) -> str:
    if not key:
        raise BillingError("billing is not configured: set STRIPE_SECRET_KEY to a Stripe TEST key (sk_test_...)")
    if not (key.startswith("sk_test_") or key.startswith("rk_test_")):
        raise BillingError("refusing to use a non-test Stripe key: this MVP runs in Stripe test mode only")
    return key


def create_checkout(s: ServiceSettings, user: User) -> str:
    key = require_test_key(s.stripe_secret_key)
    if not s.stripe_price_lab:
        raise BillingError("set STRIPE_PRICE_LAB to the test-mode price id of the Lab plan (price_...)")
    session = stripe.checkout.Session.create(
        api_key=key,
        mode="subscription",
        line_items=[{"price": s.stripe_price_lab, "quantity": 1}],
        customer=user.stripe_customer_id or None,
        customer_email=None if user.stripe_customer_id else user.email,
        client_reference_id=user.id,
        metadata={"user_id": user.id, "plan": "lab"},
        subscription_data={"metadata": {"user_id": user.id, "plan": "lab"}},
        success_url=f"{s.public_base_url}/app?billing=success",
        cancel_url=f"{s.public_base_url}/app?billing=cancelled",
    )
    return session["url"]


def _get(obj, key, default=None):
    try:
        return obj.get(key, default) if hasattr(obj, "get") else getattr(obj, key, default)
    except Exception:
        return default


def handle_webhook(s: ServiceSettings, db: Session, payload: bytes, sig_header: str | None) -> dict:
    if not s.stripe_webhook_secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set")
    try:
        ev = stripe.Webhook.construct_event(payload, sig_header or "", s.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise BillingError(f"invalid webhook: {e}") from e
    if _get(ev, "livemode"):
        raise BillingError("live-mode events are rejected: this MVP runs in Stripe test mode only")
    ev_id, ev_type = _get(ev, "id"), _get(ev, "type")
    if db.get(StripeEvent, ev_id):
        return {"ok": True, "duplicate": True, "type": ev_type}
    obj = _get(_get(ev, "data", {}), "object", {}) or {}
    applied = None
    if ev_type == "checkout.session.completed":
        uid = _get(obj, "client_reference_id") or _get(_get(obj, "metadata", {}) or {}, "user_id")
        user = db.get(User, uid) if uid else None
        if user:
            user.plan, user.plan_status = "lab", "active"
            user.stripe_customer_id = _get(obj, "customer") or user.stripe_customer_id
            user.stripe_subscription_id = _get(obj, "subscription") or user.stripe_subscription_id
            applied = user.id
    elif ev_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id, cust = _get(obj, "id"), _get(obj, "customer")
        user = db.scalar(select(User).where(User.stripe_subscription_id == sub_id)) if sub_id else None
        if user is None and cust:
            user = db.scalar(select(User).where(User.stripe_customer_id == cust))
        if user:
            status = "canceled" if ev_type.endswith("deleted") else (_get(obj, "status") or "active")
            user.plan_status = status
            if status in ("canceled", "unpaid", "incomplete_expired"):
                user.plan = "free"
            applied = user.id
    db.add(StripeEvent(id=ev_id, type=ev_type or ""))
    log.info("stripe event %s %s applied_to=%s", ev_id, ev_type, applied)
    return {"ok": True, "type": ev_type, "applied_to": applied}


def report_usage(user: User, gpu_minutes: float) -> None:
    """Stub: metered billing (Stripe usage records / meter events) is not wired up in the MVP.

    Usage is recorded in our own `usage_records` table; a lab's flat subscription covers its quota.
    """
    log.debug("usage for %s: %.3f GPU-minutes (not reported to Stripe in the MVP)", user.id, gpu_minutes)
