"""Daily reconciliation: compare what Stripe says was paid and refunded with what our database recorded.

Mismatches become review flags (kind "Reconciliation") — nothing is changed automatically, because the fix
always needs a person to look at both sides.
"""
import json
from datetime import timedelta

from flask import current_app

from .db import iso, parse_iso, utcnow

WINDOW_DAYS = 3        # look back this far each run (overlapping runs are harmless: flags are raised once)
SETTLE_MINUTES = 60    # ignore anything newer than this — the webhook may simply not have arrived yet


def _ref(sess):
    kind, _, n = (sess.get("client_reference_id") or "").partition("-")
    return (kind, int(n)) if kind in ("checkout", "deposit") and n.isdigit() else (None, None)


def compare(db, sessions, refunds, now=None, since=None):
    """sessions/refunds: Stripe objects. Returns a list of (subject, detail) mismatches."""
    now = now or utcnow()
    since = since or now - timedelta(days=WINDOW_DAYS)
    settled = now - timedelta(minutes=SETTLE_MINUTES)
    refunded = {}
    for r in refunds:
        if r.get("status") in ("succeeded", "pending") and r.get("payment_intent"):
            refunded[r["payment_intent"]] = refunded.get(r["payment_intent"], 0) + int(r.get("amount") or 0)
    out, seen = [], set()
    for s in sessions:
        if s.get("payment_status") != "paid":
            continue
        kind, n = _ref(s)
        amount, pi, sid = int(s.get("amount_total") or 0), s.get("payment_intent"), s.get("id")
        back = refunded.get(pi, 0)
        if kind is None:
            out.append((f"stripe:{sid}", f"Stripe payment {sid} ({amount}p) has no checkout or deposit reference"))
            continue
        seen.add(sid)
        table = "checkouts" if kind == "checkout" else "deposits"
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (n,)).fetchone()
        due = (row["cash_due"] if kind == "checkout" else row["amount"]) if row else None
        label = f"{'Checkout' if kind == 'checkout' else 'Deposit'} #{n}"
        if row is None:
            out.append((f"{kind}:{n}:missing", f"Stripe took {amount}p for {label}, which doesn't exist here (session {sid})"))
            continue
        if amount != due:
            out.append((f"{kind}:{n}:amount", f"{label}: Stripe charged {amount}p but we expected {due}p"))
        status = row["status"]
        if status in ("pending", "expired"):
            if parse_iso(row["created_at"]) < settled:
                out.append((f"{kind}:{n}:unfulfilled", f"{label} was paid at Stripe ({amount}p) but is “{status}” here — "
                                                       f"nothing was issued. Fulfil or refund it."))
        elif status == "credit_refused" and back < amount:
            out.append((f"{kind}:{n}:refund", f"{label} was refused (credit card) but Stripe shows only {back}p of {amount}p refunded"))
        elif status == "paid":
            expected = row["refunded"] if kind == "deposit" else 0
            if back != expected:
                out.append((f"{kind}:{n}:refunded", f"{label}: Stripe shows {back}p refunded but our records show {expected}p"
                            + (" — the entries are still valid here" if kind == "checkout" else "")))
    lo, hi = iso(since + timedelta(minutes=10)), iso(settled)
    for table, kind, amt in (("checkouts", "checkout", "cash_due"), ("deposits", "deposit", "amount")):
        for r in db.execute(f"SELECT id, stripe_session_id, {amt} a FROM {table} WHERE status='paid' AND {amt}>0 "
                            f"AND stripe_session_id IS NOT NULL AND created_at>=? AND created_at<?", (lo, hi)):
            if r["stripe_session_id"] not in seen:
                out.append((f"{kind}:{r['id']}:notpaid", f"{kind.title()} #{r['id']} ({r['a']}p) is paid here but Stripe has no "
                                                         f"completed payment for session {r['stripe_session_id']}"))
    return out


def run(db):
    """Fetch from Stripe, compare, raise flags, record the result. Returns a summary string."""
    from .control import _flag
    from .services import set_setting
    from . import payments
    if not payments.enabled() or current_app.config.get("DEMO_PAYMENTS"):
        return ""
    now = utcnow()
    since = now - timedelta(days=WINDOW_DAYS)
    sessions = payments.list_sessions(since.timestamp())
    refunds = payments.list_refunds(since.timestamp())
    problems = compare(db, sessions, refunds, now, since)
    new = sum(_flag(db, "Reconciliation", subject, detail) for subject, detail in problems)
    set_setting("last_reconciliation", json.dumps({"at": iso(now), "sessions": len(sessions), "refunds": len(refunds),
                                                   "problems": len(problems), "new": new}))
    return f"{len(sessions)} payments, {len(refunds)} refunds checked; {len(problems)} mismatch(es), {new} new"
