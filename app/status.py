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
                    "public.notifications", "public.contact", "public.status_page")


def state():
    mode = get_setting("site_status", "ok")
    msg = get_setting("site_status_message", "")
    auto = get_setting("payments_auto_paused_until")
    if mode == "ok" and emergency():
        return "payments_paused", EMERGENCY_MSG
    if mode == "ok" and auto and parse_iso(auto) > utcnow():
        return "payments_paused", "Card payments are temporarily unavailable while our payment provider recovers. Please try again in a few minutes — nothing has been charged."
    return mode, msg


def emergency():
    """The emergency purchase lock, or None. Set by a senior admin when payments, ticket allocation or a competition's
    integrity is in doubt: nobody can buy, but accounts, withdrawals, results and support keep working."""
    raw = get_setting("emergency_lock")
    return json.loads(raw) if raw else None


EMERGENCY_MSG = ("Entries are paused for a short while as a precaution while we check something. Your account, tickets and "
                 "balances are safe and you haven't been charged.")


def set_emergency(on, staff=None, reason="", hold_draws=False):
    from .db import get_db
    if on:
        set_setting("emergency_lock", json.dumps({"reason": reason[:300], "by": staff["email"] if staff else "system",
                                                  "at": iso(utcnow()), "hold_draws": bool(hold_draws)}))
        audit(get_db(), "site.emergency_lock", None, f"Emergency purchase lock ON{' (automatic draws held)' if hold_draws else ''}: "
              f"{reason[:200]}")
    else:
        set_setting("emergency_lock", "")
        audit(get_db(), "site.emergency_unlock", None, "Emergency purchase lock lifted")


def draws_held():
    e = emergency()
    return bool(e and e.get("hold_draws"))


def public_status(db):
    """What customers see on /status: each service, plainly, with no technical detail."""
    mode, msg = state()
    lock = emergency()
    since = iso(utcnow() - timedelta(hours=1))
    mail_failed = db.execute("SELECT COUNT(*) FROM notifications WHERE email_status='failed' AND created_at>?", (since,)).fetchone()[0]
    mail_sent = db.execute("SELECT COUNT(*) FROM notifications WHERE email_status='sent' AND created_at>?", (since,)).fetchone()[0]
    rows = [("Website and accounts", "bad" if mode == "maintenance" else "ok",
             (msg or "Planned maintenance — back shortly.") if mode == "maintenance" else "Working normally")]
    if lock:
        rows.append(("Entering competitions and payments", "warn", EMERGENCY_MSG))
    elif mode in ("payments_paused", "maintenance"):
        rows.append(("Entering competitions and payments", "warn", msg or "Paused for a short while — you won't be charged."))
    else:
        rows.append(("Entering competitions and payments", "ok", "Working normally"))
    rows.append(("Withdrawals", "ok", "Requests are accepted as normal and paid within 24 hours"))
    rows.append(("Draws", "warn" if lock and lock.get("hold_draws") else "ok",
                 "Draws due now will run as soon as our checks finish — no entries are lost" if lock and lock.get("hold_draws")
                 else "Running on schedule"))
    rows.append(("Email notifications", "warn" if mail_failed >= 5 and mail_failed > mail_sent else "ok",
                 "Some emails are delayed — everything is also in your account notifications"
                 if mail_failed >= 5 and mail_failed > mail_sent else "Working normally"))
    worst = "bad" if any(r[1] == "bad" for r in rows) else ("warn" if any(r[1] == "warn" for r in rows) else "ok")
    return worst, rows


def payments_paused():
    if emergency():
        return EMERGENCY_MSG
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
