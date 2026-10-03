"""Site status: payments paused (by an admin, or automatically when the payment provider keeps failing) and
maintenance mode. Customers get a clear message instead of failing transactions."""
import json
from datetime import timedelta

from flask import g, render_template, request

from .db import iso, parse_iso, utcnow
from .services import audit, get_setting, set_setting

# Pages that keep working during maintenance: admin, signing in, your account, legal pages, payment callbacks.
MAINTENANCE_OPEN = ("static", "public.uploads", "public.login", "public.logout", "public.stripe_webhook", "public.health",
                    "public.page", "public.account", "public.robots", "public.manifest", "public.service_worker",
                    "public.forgot", "public.reset", "public.verify_email", "public.withdrawal_detail", "public.order_detail",
                    "public.notifications", "public.contact")


def state():
    mode = get_setting("site_status", "ok")
    msg = get_setting("site_status_message", "")
    auto = get_setting("payments_auto_paused_until")
    if mode == "ok" and auto and parse_iso(auto) > utcnow():
        return "payments_paused", "Card payments are temporarily unavailable while our payment provider recovers. Please try again in a few minutes — nothing has been charged."
    return mode, msg


def payments_paused():
    mode, msg = state()
    if mode in ("payments_paused", "maintenance"):
        return msg or "Payments are paused for a short while. Please try again soon — you haven't been charged."
    return None


def record_provider_error():
    """Three payment-provider errors within ten minutes pause card payments for 15 minutes."""
    now = utcnow()
    hits = [t for t in json.loads(get_setting("stripe_errors", "[]")) if parse_iso(t) > now - timedelta(minutes=10)]
    hits.append(iso(now))
    set_setting("stripe_errors", json.dumps(hits[-10:]))
    if len(hits) >= 3 and not (get_setting("payments_auto_paused_until") and parse_iso(get_setting("payments_auto_paused_until")) > now):
        set_setting("payments_auto_paused_until", iso(now + timedelta(minutes=15)))
        from .db import get_db
        audit(get_db(), "site.payments_auto_paused", None, "Payment provider failed 3 times in 10 minutes — card payments paused for 15 minutes",
              actor=False)


def maintenance_gate():
    if request.endpoint in MAINTENANCE_OPEN or (request.blueprint in ("admin", "control", "security")):
        return None
    mode, msg = state()
    if mode != "maintenance" or (g.get("user") is not None and g.user["is_admin"]):
        return None
    return render_template("maintenance.html", msg=msg), 503
