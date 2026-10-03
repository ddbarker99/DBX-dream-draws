"""Minimal Stripe Checkout client (no SDK needed) + webhook signature check."""
import hashlib
import hmac
import time

import requests
from flask import current_app

API = "https://api.stripe.com/v1"


def enabled():
    return bool(current_app.config.get("STRIPE_SECRET_KEY"))


def create_checkout(checkout_id, amount, description, user_email, success_url, cancel_url, kind="checkout"):
    """One Stripe line for the whole basket (after multi-buy, promo and wallet credit), or a wallet deposit."""
    key = "deposit_id" if kind == "deposit" else "checkout_id"
    data = {
        "mode": "payment",
        "customer_email": user_email,
        "client_reference_id": f"{kind}-{checkout_id}",
        f"metadata[{key}]": str(checkout_id),
        f"payment_intent_data[metadata][{key}]": str(checkout_id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "gbp",
        "line_items[0][price_data][unit_amount]": str(amount),
        "line_items[0][price_data][product_data][name]": (f"Wallet deposit #{checkout_id}" if kind == "deposit"
                                                           else f"Competition entries #{checkout_id}"),
        "line_items[0][price_data][product_data][description]": description[:500],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "expires_at": str(int(time.time()) + 30 * 60 + 5),
    }
    r = requests.post(f"{API}/checkout/sessions", data=data,
                      auth=(current_app.config["STRIPE_SECRET_KEY"], ""), timeout=20)
    r.raise_for_status()
    return r.json()


def verify_webhook(payload: bytes, sig_header: str, secret: str, tolerance=300):
    """Implements Stripe's documented v1 signature scheme."""
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        ts = int(parts["t"])
        sigs = [v for k, v in (p.split("=", 1) for p in sig_header.split(",")) if k == "v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def card_funding(payment_intent_id):
    """'credit' | 'debit' | 'prepaid' | 'unknown' — or None if it wasn't a card (e.g. Pay by Bank).
    Apple Pay / Google Pay are cards underneath, so they're checked too."""
    r = requests.get(f"{API}/payment_intents/{payment_intent_id}", params={"expand[]": "latest_charge"},
                     auth=(current_app.config["STRIPE_SECRET_KEY"], ""), timeout=20)
    r.raise_for_status()
    charge = r.json().get("latest_charge") or {}
    card = (charge.get("payment_method_details") or {}).get("card")
    return card.get("funding") if card else None


def refund(payment_intent_id, reason="requested_by_customer", amount=None, why="credit card not accepted"):
    data = {"payment_intent": payment_intent_id, "reason": reason, "metadata[why]": why}
    if amount:
        data["amount"] = str(amount)
    r = requests.post(f"{API}/refunds", data=data,
                      auth=(current_app.config["STRIPE_SECRET_KEY"], ""), timeout=20)
    r.raise_for_status()
    return r.json()
