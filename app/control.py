"""DBX Control Centre and operations tools: dashboard, reconciliation, customer timelines, review flags, support cases."""
import csv
import io
from datetime import datetime, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for)

from . import UK, mailer
from .db import get_db, iso, parse_iso, utcnow, write_txn
from .perms import can, require
from .services import (CLAIM_NAMES, LIFECYCLE_NAMES, audit, balances, lifecycle_stage, public_name, sold_count, uk_midnight)

bp = Blueprint("control", __name__, url_prefix="/admin")


def _sum(db, sql, *a):
    return db.execute(sql, a).fetchone()[0] or 0


# ---------------- Control Centre ----------------

def attention_items(db):
    """Everything that needs a person, most urgent first. (label, count, link, level)"""
    now = iso(utcnow())
    items = []

    def add(label, n, link, level="warn"):
        if n:
            items.append((label, n, link, level))

    stuck = db.execute("SELECT id, title FROM competitions WHERE status='live' AND locked_at IS NULL AND ends_at<?",
                       (iso(utcnow() - timedelta(minutes=50)),)).fetchall()
    add("Competitions past closing that haven't closed (postal entries or payments outstanding)", len(stuck),
        url_for("admin.postal_queue"), "bad")
    add("Draws due — closed and waiting for a draw", _sum(db, "SELECT COUNT(*) FROM competitions WHERE status='live' "
        "AND game_type='' AND locked_at IS NOT NULL"), url_for("admin.dashboard"), "bad")
    add("Postal entries waiting to be processed", _sum(db, "SELECT COUNT(*) FROM postal_entries WHERE status='received'"),
        url_for("admin.postal_queue"))
    add("Payments needing a refund", _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status='needs_refund'")
        + _sum(db, "SELECT COUNT(*) FROM deposits WHERE status='needs_refund'"), url_for("admin.payouts"), "bad")
    add("Withdrawals to pay", _sum(db, "SELECT COUNT(*) FROM withdrawals WHERE status IN ('requested','processing')"),
        url_for("admin.payouts"))
    add("Withdrawals waiting more than 24 hours", _sum(db, "SELECT COUNT(*) FROM withdrawals WHERE status IN ('requested','processing') "
        "AND created_at<?", iso(utcnow() - timedelta(hours=24))), url_for("admin.payouts"), "bad")
    add("Winners awaiting fulfilment", _sum(db, "SELECT COUNT(*) FROM prize_claims WHERE status NOT IN ('delivered','forfeited')"),
        url_for("admin.prizes"))
    add("Forfeited prizes needing a redraw", _sum(db, "SELECT COUNT(*) FROM prize_claims pc WHERE status='forfeited' AND id="
        "(SELECT MAX(id) FROM prize_claims WHERE competition_id=pc.competition_id)"), url_for("admin.prizes"), "bad")
    add("Instant prizes to send", _sum(db, "SELECT COUNT(*) FROM instant_prizes WHERE ticket_id IS NOT NULL AND fulfilled=0"),
        url_for("admin.prizes"))
    add("Open support cases", _sum(db, "SELECT COUNT(*) FROM cases WHERE status='open'"), url_for("control.cases"))
    add("Flags to review", _sum(db, "SELECT COUNT(*) FROM flags WHERE status='open'"), url_for("control.flags"))
    add("Background job failures in the last 24 hours", _sum(db, "SELECT COUNT(*) FROM job_runs WHERE ok=0 AND started_at>?",
        iso(utcnow() - timedelta(hours=24))), url_for("control.health"), "bad")
    add("Emails that failed to send", _sum(db, "SELECT COUNT(*) FROM notifications WHERE email_status='failed'"),
        url_for("control.health"), "bad")
    order = {"bad": 0, "warn": 1}
    return sorted(items, key=lambda x: order[x[3]])


@bp.route("/")
@require("comps.view")
def centre():
    db = get_db()
    today = iso(uk_midnight())
    week = iso(uk_midnight(7))
    money = {
        "card_today": _sum(db, "SELECT SUM(cash_due) FROM checkouts WHERE status='paid' AND paid_at>=?", today)
                      + _sum(db, "SELECT SUM(amount) FROM deposits WHERE status='paid' AND paid_at>=?", today),
        "card_week": _sum(db, "SELECT SUM(cash_due) FROM checkouts WHERE status='paid' AND paid_at>=?", week)
                     + _sum(db, "SELECT SUM(amount) FROM deposits WHERE status='paid' AND paid_at>=?", week),
        "entries_today": _sum(db, "SELECT COUNT(*) FROM tickets WHERE status='issued' AND created_at>=?", today),
        "postal_today": _sum(db, "SELECT COUNT(*) FROM tickets WHERE status='issued' AND postal_entry_id IS NOT NULL AND created_at>=?", today),
        "signups_today": _sum(db, "SELECT COUNT(*) FROM users WHERE created_at>=?", today),
        "orders_today": _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status='paid' AND paid_at>=?", today),
        "failed_today": _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status IN ('expired','credit_refused','needs_refund') "
                                 "AND stripe_session_id IS NOT NULL AND created_at>=?", today),
        "refunded_today": _sum(db, "SELECT SUM(amount) FROM credit_ledger WHERE ref LIKE 'refund-o%' AND created_at>=?", today),
    }
    live = []
    for c in db.execute("SELECT * FROM competitions WHERE status='live' ORDER BY ends_at").fetchall():
        sold = sold_count(db, c["id"])
        live.append({"c": c, "sold": sold, "stage": lifecycle_stage(db, c),
                     "revenue": _sum(db, "SELECT SUM(amount) FROM orders WHERE competition_id=? AND status='paid'", c["id"]),
                     "instant_left": _sum(db, "SELECT COUNT(*) FROM instant_prizes WHERE competition_id=? AND ticket_id IS NULL", c["id"]),
                     "postal": _sum(db, "SELECT COUNT(*) FROM postal_entries WHERE competition_id=? AND status='received'", c["id"]),
                     "hours": (parse_iso(c["ends_at"]) - utcnow()).total_seconds() / 3600})
    from .jobs import health_checks
    return render_template("admin/centre.html", money=money, attention=attention_items(db), live=live,
                           ending=[x for x in live if 0 < x["hours"] <= 48], stage_names=LIFECYCLE_NAMES,
                           instant_left=sum(x["instant_left"] for x in live),
                           instant_value=_sum(db, "SELECT SUM(ip.value) FROM instant_prizes ip JOIN competitions c ON c.id=ip.competition_id "
                                                  "WHERE c.status='live' AND ip.ticket_id IS NULL"),
                           health=health_checks(db))


# ---------------- financial reconciliation ----------------

LEDGER_BUCKETS = [
    ("Prizes won", "ref LIKE 'ip%'"),
    ("Refunds (cancelled competitions)", "ref LIKE 'refund-o%'"),
    ("Spent on entries", "ref LIKE 'c%' AND amount<0"),
    ("Returned from unpaid checkouts", "ref LIKE 'c%' AND amount>0"),
    ("Withdrawals requested", "ref LIKE 'w%' AND amount<0"),
    ("Withdrawals returned", "ref LIKE 'w%' AND amount>0"),
    ("Deposits by card", "ref LIKE 'd%' AND ref NOT LIKE 'dr%'"),
    ("Deposits refunded to card", "ref LIKE 'dr%'"),
    ("Referral bonuses", "ref LIKE 'u%'"),
    ("DBX Points redeemed", "reason LIKE 'Redeemed%'"),
    ("Admin adjustments", "ref LIKE 'admin%'"),
]


def _period():
    def parse(v, default):
        try:
            return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=UK)
        except (TypeError, ValueError):
            return default
    end_default = uk_midnight() + timedelta(days=1)
    start = parse(request.args.get("from"), uk_midnight().replace(day=1))
    end = parse(request.args.get("to"), end_default - timedelta(days=1)) + timedelta(days=1)
    return start, end


def finance_report(db, start, end):
    a, b = iso(start), iso(end)
    paid = "status='paid' AND paid_at>=? AND paid_at<?"
    r = {
        "card_entries": _sum(db, f"SELECT SUM(cash_due) FROM checkouts WHERE {paid}", a, b),
        "card_deposits": _sum(db, f"SELECT SUM(amount) FROM deposits WHERE {paid}", a, b),
        "card_count": _sum(db, f"SELECT COUNT(*) FROM checkouts WHERE {paid} AND cash_due>0", a, b)
                      + _sum(db, f"SELECT COUNT(*) FROM deposits WHERE {paid}", a, b),
        "refused": _sum(db, "SELECT SUM(cash_due) FROM checkouts WHERE status='credit_refused' AND created_at>=? AND created_at<?", a, b)
                   + _sum(db, "SELECT SUM(amount) FROM deposits WHERE status='credit_refused' AND created_at>=? AND created_at<?", a, b),
        "refund_due": _sum(db, "SELECT SUM(cash_due) FROM checkouts WHERE status='needs_refund'")
                      + _sum(db, "SELECT SUM(amount) FROM deposits WHERE status='needs_refund'"),
        "gross": _sum(db, "SELECT SUM(o.amount + o.discount) FROM orders o JOIN checkouts k ON k.id=o.checkout_id "
                          "WHERE k.status='paid' AND k.paid_at>=? AND k.paid_at<?", a, b),
        "multibuy": _sum(db, "SELECT SUM(o.discount) FROM orders o JOIN checkouts k ON k.id=o.checkout_id "
                             "WHERE k.status='paid' AND k.paid_at>=? AND k.paid_at<?", a, b),
        "promo": _sum(db, f"SELECT SUM(promo_discount) FROM checkouts WHERE {paid}", a, b),
        "credit_used": _sum(db, f"SELECT SUM(credit_used) FROM checkouts WHERE {paid}", a, b),
        "deposit_used": _sum(db, f"SELECT SUM(deposit_used) FROM checkouts WHERE {paid}", a, b),
        "cash_used": _sum(db, f"SELECT SUM(cash_used) FROM checkouts WHERE {paid}", a, b),
        "withdrawn": _sum(db, "SELECT SUM(amount) FROM withdrawals WHERE status='paid' AND done_at>=? AND done_at<?", a, b),
        "prizes_cash": _sum(db, "SELECT SUM(amount) FROM credit_ledger WHERE ref LIKE 'ip%' AND kind='cash' AND created_at>=? AND created_at<?", a, b),
        "prizes_credit": _sum(db, "SELECT SUM(amount) FROM credit_ledger WHERE ref LIKE 'ip%' AND kind='credit' AND created_at>=? AND created_at<?", a, b),
        "prizes_physical": _sum(db, "SELECT SUM(value) FROM instant_prizes WHERE won_at>=? AND won_at<? AND credit_amount=0", a, b),
        "draw_prizes": _sum(db, "SELECT SUM(prize_value) FROM competitions WHERE drawn_at>=? AND drawn_at<?", a, b),
    }
    r["net_entries"] = r["gross"] - r["multibuy"]
    # Every pound of entries must be accounted for by exactly one source.
    r["sources"] = r["promo"] + r["credit_used"] + r["deposit_used"] + r["cash_used"] + r["card_entries"]
    r["balanced"] = r["sources"] == r["net_entries"]
    buckets = []
    for label, where in LEDGER_BUCKETS:
        row = {k: 0 for k in ("cash", "credit", "deposit")}
        for kind, total in db.execute(f"SELECT kind, SUM(amount) FROM credit_ledger WHERE {where} AND created_at>=? AND created_at<? "
                                      "GROUP BY kind", (a, b)):
            row[kind] = total
        if any(row.values()):
            buckets.append((label, row))
    r["buckets"] = buckets
    r["owed"] = {k: _sum(db, "SELECT SUM(amount) FROM credit_ledger WHERE kind=?", k) for k in ("cash", "credit", "deposit")}
    return r


@bp.route("/finance")
@require("reports")
def finance():
    start, end = _period()
    import json
    from .services import get_setting
    db = get_db()
    rec = json.loads(get_setting("last_reconciliation") or "null")
    rec_open = db.execute("SELECT COUNT(*) FROM flags WHERE kind='Reconciliation' AND status='open'").fetchone()[0]
    return render_template("admin/finance.html", r=finance_report(db, start, end), rec=rec, rec_open=rec_open,
                           start=start.strftime("%Y-%m-%d"), end=(end - timedelta(days=1)).strftime("%Y-%m-%d"))


# ---------------- business reporting (definitions: docs/REPORTING-DEFINITIONS.md) ----------------

def business_report(db, start, end):
    """Headline numbers for a period. Every figure has one written definition; see docs/REPORTING-DEFINITIONS.md."""
    from .services import get_setting
    a, b = iso(start), iso(end)
    f = finance_report(db, start, end)
    fee_pct = float(get_setting("fee_percent", "1.5") or 0)
    fee_fixed = int(get_setting("fee_fixed_pence", "20") or 0)
    card_count = f["card_count"]
    card_total = f["card_entries"] + f["card_deposits"]
    buyers = [r[0] for r in db.execute("SELECT DISTINCT user_id FROM checkouts WHERE status='paid' AND paid_at>=? AND paid_at<?", (a, b))]
    returning = _sum(db, "SELECT COUNT(DISTINCT user_id) FROM checkouts WHERE status='paid' AND paid_at<? AND user_id IN "
                         "(SELECT user_id FROM checkouts WHERE status='paid' AND paid_at>=? AND paid_at<?)", a, a, b)
    refunds_entries = _sum(db, "SELECT SUM(amount) FROM credit_ledger WHERE ref LIKE 'refund-o%' AND created_at>=? AND created_at<?", a, b)
    refunds_card = f["refused"] + _sum(db, "SELECT SUM(-amount) FROM credit_ledger WHERE ref LIKE 'dr%' AND created_at>=? AND created_at<?", a, b)
    promo_credit = _sum(db, "SELECT SUM(amount) FROM credit_ledger WHERE kind='credit' AND amount>0 AND created_at>=? AND created_at<? "
                            "AND (ref LIKE 'u%' OR reason LIKE 'Redeemed%' OR ref LIKE 'admin%')", a, b)
    prize_costs = f["draw_prizes"] + f["prizes_cash"] + f["prizes_credit"] + f["prizes_physical"]
    customer_money = f["card_entries"] + f["deposit_used"] + f["cash_used"]
    fees = round(card_total * fee_pct / 100) + card_count * fee_fixed
    r = {
        "gross_entries": f["gross"], "multibuy": f["multibuy"], "net_entries": f["net_entries"],
        "promo_codes": f["promo"], "credit_used": f["credit_used"], "customer_money": customer_money,
        "card_entries": f["card_entries"], "deposit_used": f["deposit_used"], "cash_used": f["cash_used"],
        "refunds_entries": refunds_entries, "refunds_card": refunds_card, "promo_credit": promo_credit,
        "draw_prizes": f["draw_prizes"], "instant_cash": f["prizes_cash"], "instant_credit": f["prizes_credit"],
        "instant_physical": f["prizes_physical"], "prize_costs": prize_costs,
        "card_total": card_total, "card_count": card_count, "fees": fees, "fee_pct": fee_pct, "fee_fixed": fee_fixed,
        "customers": len(buyers), "returning": returning, "new": len(buyers) - returning,
        "orders": _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status='paid' AND paid_at>=? AND paid_at<?", a, b),
        "free_entries": _sum(db, "SELECT COUNT(*) FROM tickets WHERE postal_entry_id IS NOT NULL AND status='issued' "
                                 "AND created_at>=? AND created_at<?", a, b),
        "paid_entries": _sum(db, "SELECT SUM(o.quantity) FROM orders o WHERE o.status='paid' AND o.paid_at>=? AND o.paid_at<?", a, b),
        "signups": _sum(db, "SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?", a, b),
        "withdrawn": f["withdrawn"],
    }
    r["avg_order"] = r["net_entries"] // r["orders"] if r["orders"] else 0
    r["avg_customer"] = r["net_entries"] // r["customers"] if r["customers"] else 0
    r["contribution"] = customer_money - r["refunds_entries"] - prize_costs - fees
    return r


REPORT_ROWS = [
    ("gross_entries", "Gross paid entries"), ("multibuy", "Multi-buy discounts"), ("net_entries", "Net entry value"),
    ("promo_codes", "Promo-code discounts"), ("credit_used", "Paid with site credit (promotional)"),
    ("customer_money", "Paid with customers' own money"), ("card_entries", "— by card at checkout"),
    ("deposit_used", "— from deposited funds"), ("cash_used", "— from cash winnings"),
    ("refunds_entries", "Refunds of entries (cancelled competitions)"), ("refunds_card", "Refunds to cards"),
    ("promo_credit", "Promotional credit issued"), ("draw_prizes", "Draw prizes (value, by draw date)"),
    ("instant_cash", "Instant cash prizes"), ("instant_credit", "Instant site-credit prizes"),
    ("instant_physical", "Instant physical prizes (value)"), ("prize_costs", "Total prize costs"),
    ("card_total", "Card payments received"), ("fees", "Estimated card fees"), ("contribution", "Estimated contribution"),
    ("withdrawn", "Withdrawals paid out"),
]
COUNT_ROWS = [("orders", "Paid orders"), ("paid_entries", "Paid entries"), ("free_entries", "Free postal entries"),
              ("customers", "Unique paying customers"), ("new", "— new (first ever purchase)"), ("returning", "— returning"),
              ("signups", "Sign-ups"), ("card_count", "Card payments")]


@bp.route("/reports", methods=["GET", "POST"])
@require("reports")
def reports():
    from .services import set_setting
    db = get_db()
    if request.method == "POST":
        try:
            pct, fixed = float(request.form.get("fee_percent", "1.5")), int(request.form.get("fee_fixed_pence", "20"))
            if not (0 <= pct <= 10 and 0 <= fixed <= 200):
                raise ValueError
        except ValueError:
            flash("Fees must be a percentage (0–10) and pence (0–200).", "error")
            return redirect(url_for("control.reports"))
        set_setting("fee_percent", str(pct))
        set_setting("fee_fixed_pence", str(fixed))
        audit(db, "reports.fees", None, f"Card fee estimate set to {pct}% + {fixed}p")
        flash("Saved.")
        return redirect(url_for("control.reports"))
    start, end = _period()
    span = end - start
    cur, prev = business_report(db, start, end), business_report(db, start - span, start)
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["metric", "this period (pence or count)", "previous period", "period_start_uk", "period_end_uk"])
        for k, label in REPORT_ROWS + COUNT_ROWS:
            w.writerow([label, cur[k], prev[k], start.strftime("%Y-%m-%d"), (end - timedelta(days=1)).strftime("%Y-%m-%d")])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=business-report-{start:%Y%m%d}.csv"})
    return render_template("admin/reports.html", r=cur, p=prev, rows=REPORT_ROWS, counts=COUNT_ROWS,
                           start=start.strftime("%Y-%m-%d"), end=(end - timedelta(days=1)).strftime("%Y-%m-%d"))


@bp.route("/finance/payments.csv")
@require("reports")
def finance_csv():
    """Every card payment we asked Stripe for in the period, to match line-by-line against Stripe's report."""
    start, end = _period()
    db = get_db()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date_utc", "type", "dbx_ref", "stripe_session", "amount_gbp", "status", "customer_email"])
    for r in db.execute("SELECT k.*, u.email FROM checkouts k JOIN users u ON u.id=k.user_id WHERE k.cash_due>0 AND k.created_at>=? "
                        "AND k.created_at<? AND k.stripe_session_id IS NOT NULL ORDER BY k.id", (iso(start), iso(end))):
        w.writerow([r["paid_at"] or r["created_at"], "entries", f"checkout-{r['id']}", r["stripe_session_id"],
                    f"{r['cash_due'] / 100:.2f}", r["status"], r["email"]])
    for r in db.execute("SELECT d.*, u.email FROM deposits d JOIN users u ON u.id=d.user_id WHERE d.created_at>=? AND d.created_at<? "
                        "AND d.stripe_session_id IS NOT NULL ORDER BY d.id", (iso(start), iso(end))):
        w.writerow([r["paid_at"] or r["created_at"], "deposit", f"deposit-{r['id']}", r["stripe_session_id"],
                    f"{r['amount'] / 100:.2f}", r["status"] + (f" (refunded {r['refunded'] / 100:.2f})" if r["refunded"] else ""), r["email"]])
    audit(db, "finance.export", None, f"Payments CSV {start:%Y-%m-%d} to {end:%Y-%m-%d}")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=dbx-payments-{start:%Y%m%d}-{end:%Y%m%d}.csv"})


# ---------------- customer timeline ----------------

def timeline_events(db, uid):
    ev = []
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    ev.append((u["created_at"], "account", "Account created", f"{u['email']} · born {u['dob']}"
               + (f" · referred by #{u['referred_by']}" if u["referred_by"] else "")))
    for s in db.execute("SELECT * FROM user_sessions WHERE user_id=?", (uid,)):
        ev.append((s["created_at"], "login", "Signed in", f"{s['ip'] or '?'} · {(s['agent'] or '')[:60]}"
                   + (" · signed out" if s["revoked_at"] else "")))
    for k in db.execute("SELECT * FROM checkouts WHERE user_id=?", (uid,)):
        lines = db.execute("SELECT o.id, o.quantity, c.title FROM orders o JOIN competitions c ON c.id=o.competition_id "
                           "WHERE o.checkout_id=?", (k["id"],)).fetchall()
        nums = []
        for ln in lines:
            ns = [r[0] for r in db.execute("SELECT number FROM tickets WHERE order_id=? ORDER BY number", (ln["id"],))]
            nums.append(f"{ln['title']}: {ln['quantity']}" + (f" (#{', #'.join(map(str, ns[:20]))}{'…' if len(ns) > 20 else ''})" if ns else ""))
        label = {"paid": "Order paid", "expired": "Checkout not completed", "credit_refused": "Credit card refused & refunded",
                 "needs_refund": "Payment needs a refund", "pending": "Checkout in progress"}.get(k["status"], k["status"])
        ev.append((k["paid_at"] or k["created_at"], "order", f"{label} — order #{k['id']}",
                   f"Card £{k['cash_due'] / 100:.2f} · balance £{(k['credit_used'] + k['cash_used'] + k['deposit_used']) / 100:.2f}"
                   f"{' · promo £%.2f' % (k['promo_discount'] / 100) if k['promo_discount'] else ''} — " + "; ".join(nums)))
    for p in db.execute("SELECT p.*, c.title, t.number FROM postal_entries p JOIN competitions c ON c.id=p.competition_id "
                        "LEFT JOIN tickets t ON t.postal_entry_id=p.id WHERE p.user_id=?", (uid,)):
        ev.append((p["received_at"] or p["created_at"], "postal", f"Postal entry {p['status']}",
                   f"{p['title']}" + (f" · ticket #{p['number']}" if p["number"] else "") + (f" · {p['reject_reason']}" if p["reject_reason"] else "")))
    for l in db.execute("SELECT * FROM credit_ledger WHERE user_id=?", (uid,)):
        ev.append((l["created_at"], "wallet", f"{'+' if l['amount'] > 0 else '−'}£{abs(l['amount']) / 100:.2f} {l['kind']}", l["reason"]))
    for w in db.execute("SELECT ip.title, ip.won_at, t.number, c.title AS comp FROM instant_prizes ip JOIN tickets t ON t.id=ip.ticket_id "
                        "JOIN competitions c ON c.id=ip.competition_id WHERE t.user_id=?", (uid,)):
        ev.append((w["won_at"], "prize", f"Instant win: {w['title']}", f"{w['comp']} · ticket #{w['number']}"))
    for d in db.execute("SELECT d.*, c.title FROM draws d JOIN competitions c ON c.id=d.competition_id WHERE d.winner_user_id=?", (uid,)):
        ev.append((d["drawn_at"], "prize", f"Won the draw: {d['title']}", f"Ticket #{d['winning_number']} ({d['method']})"))
    for w in db.execute("SELECT * FROM withdrawals WHERE user_id=?", (uid,)):
        ev.append((w["created_at"], "withdrawal", f"Withdrawal requested £{w['amount'] / 100:.2f}",
                   f"{w['method']} {w['account_name'] or w['paypal_email'] or ''} {w['sort_code'] or ''} {w['account_number'] or ''}".strip()))
        if w["done_at"]:
            ev.append((w["done_at"], "withdrawal", f"Withdrawal {w['status']} £{w['amount'] / 100:.2f}", w["note"] or ""))
    for d in db.execute("SELECT * FROM deposits WHERE user_id=? AND status!='expired'", (uid,)):
        ev.append((d["paid_at"] or d["created_at"], "wallet", f"Deposit £{d['amount'] / 100:.2f} ({d['status']})",
                   f"refunded £{d['refunded'] / 100:.2f}" if d["refunded"] else ""))
    for a in db.execute("SELECT * FROM audit_log WHERE target=?", (f"user:{uid}",)):
        kind = "safer" if a["action"].startswith("safer.") else ("security" if a["action"].startswith(("account.", "admin.")) else "admin")
        ev.append((a["created_at"], kind, a["action"], (a["detail"] or "") + (f" — by {a['actor_email']}" if a["actor_id"] and a["actor_id"] != uid else "")))
    for c in db.execute("SELECT * FROM cases WHERE user_id=?", (uid,)):
        ev.append((c["created_at"], "support", f"Support case #{c['id']}: {c['topic']}", c["status"]))
    for f in db.execute("SELECT * FROM flags WHERE subject=?", (f"user:{uid}",)):
        ev.append((f["created_at"], "flag", f"Flag for review: {f['kind']}", f"{f['detail']} ({f['status']})"))
    for n in db.execute("SELECT * FROM points_ledger WHERE user_id=?", (uid,)):
        ev.append((n["created_at"], "points", f"{n['points']:+} DBX Points", n["reason"]))
    return sorted((e for e in ev if e[0]), key=lambda e: e[0], reverse=True)


@bp.route("/customers/<int:uid>/timeline")
@require("users.view")
def timeline(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u is None:
        abort(404)
    audit(db, "customer.viewed", f"user:{uid}", "Opened customer timeline")
    kinds = request.args.getlist("kind")
    events = timeline_events(db, uid)
    if kinds:
        events = [e for e in events if e[1] in kinds]
    return render_template("admin/timeline.html", u=u, events=events, bal=balances(db, uid), kinds=kinds,
                           all_kinds=["order", "postal", "wallet", "prize", "withdrawal", "safer", "security", "support", "flag", "points", "login", "admin"])


# ---------------- review flags ----------------

def _flag(db, kind, subject, detail):
    """Raise a flag once per (kind, subject). Returns True if new."""
    return db.execute("INSERT OR IGNORE INTO flags (kind, subject, detail, created_at) VALUES (?,?,?,?)",
                      (kind, subject, detail[:500], iso(utcnow()))).rowcount > 0


def run_flag_rules(db):
    """Patterns worth a human look. Flags never block anyone by themselves."""
    new = 0
    day = iso(utcnow() - timedelta(hours=24))
    month = iso(utcnow() - timedelta(days=30))
    for phone, ids in db.execute("SELECT phone, GROUP_CONCAT(id) FROM users WHERE phone IS NOT NULL AND phone!='' "
                                 "GROUP BY phone HAVING COUNT(*)>1"):
        new += _flag(db, "Shared phone number", f"phone:{phone}", f"Accounts {ids} use the same phone number — possible duplicate accounts")
    for ip, n, ids in db.execute("SELECT ip, COUNT(DISTINCT user_id), GROUP_CONCAT(DISTINCT user_id) FROM user_sessions "
                                 "WHERE created_at>? AND ip IS NOT NULL AND ip!='' GROUP BY ip HAVING COUNT(DISTINCT user_id)>=4", (day,)):
        new += _flag(db, "Many accounts from one connection", f"ip:{ip}:{utcnow():%Y%m%d}",
                     f"{n} different accounts signed in from {ip} in 24 hours (accounts {ids}). Could be a shared network.")
    for uid, n in db.execute("SELECT user_id, COUNT(*) FROM checkouts WHERE created_at>? AND stripe_session_id IS NOT NULL "
                             "AND status IN ('expired','credit_refused','needs_refund') GROUP BY user_id HAVING COUNT(*)>=3", (day,)):
        new += _flag(db, "Repeated failed payments", f"user:{uid}", f"{n} card payments failed or were refused in 24 hours")
    for uid, n in db.execute("SELECT CAST(substr(target,6) AS INTEGER), COUNT(*) FROM audit_log WHERE action='safer.limit_block' "
                             "AND created_at>? GROUP BY target HAVING COUNT(*)>=3", (day,)):
        new += _flag(db, "Repeatedly hitting spending limits", f"user:{uid}", f"{n} purchases stopped by their own spending limit in 24 hours "
                     "— consider a responsible-play check-in")
    for uid, n, names in db.execute("SELECT user_id, COUNT(DISTINCT COALESCE(account_number, paypal_email)), "
                                    "GROUP_CONCAT(DISTINCT COALESCE(account_name, paypal_email)) FROM withdrawals WHERE created_at>? "
                                    "GROUP BY user_id HAVING COUNT(DISTINCT COALESCE(account_number, paypal_email))>=2", (month,)):
        new += _flag(db, "Withdrawal destination changed", f"user:{uid}", f"{n} different payout destinations in 30 days ({names})")
    for w in db.execute("SELECT w.id, w.user_id, w.account_name, u.name FROM withdrawals w JOIN users u ON u.id=w.user_id "
                        "WHERE w.method='bank' AND w.status IN ('requested','processing')"):
        if w["account_name"] and u_last(w["name"]) not in w["account_name"].lower():
            new += _flag(db, "Withdrawal to a name that doesn't match", f"withdrawal:{w['id']}",
                         f"Account holder “{w['account_name']}” vs customer “{w['name']}” (user {w['user_id']})")
    for u in db.execute("SELECT b.id, b.phone FROM users b WHERE b.excluded_until>? AND b.phone IS NOT NULL", (iso(utcnow()),)):
        others = [r[0] for r in db.execute("SELECT id FROM users WHERE phone=? AND id!=?", (u["phone"], u["id"]))]
        if others and db.execute(f"SELECT 1 FROM checkouts WHERE status='paid' AND paid_at>? AND user_id IN ({','.join('?' * len(others))})",
                                 (day, *others)).fetchone():
            new += _flag(db, "Possible break circumvention", f"user:{u['id']}",
                         f"Account on a break shares a phone number with account(s) {others}, which bought entries in the last 24 hours")
    for addr, n, comp in db.execute("SELECT lower(address), COUNT(DISTINCT lower(name)), competition_id FROM postal_entries "
                                    "GROUP BY lower(address), competition_id HAVING COUNT(DISTINCT lower(name))>=4"):
        new += _flag(db, "Many postal entrants at one address", f"postal:{comp}:{addr[:40]}",
                     f"{n} different names sent postal entries from the same address to competition {comp}")
    return new


def u_last(name):
    parts = (name or "").lower().split()
    return parts[-1] if parts else ""


@bp.route("/flags", methods=["GET", "POST"])
@require("flags")
def flags():
    db = get_db()
    if request.method == "POST":
        if request.form.get("run"):
            n = run_flag_rules(db)
            flash(f"Checked — {n} new flag{'s' if n != 1 else ''}.")
        else:
            status = request.form.get("status")
            if status not in ("reviewed", "dismissed", "open"):
                abort(400)
            db.execute("UPDATE flags SET status=?, reviewed_at=?, reviewed_by=?, note=? WHERE id=?",
                       (status, iso(utcnow()), g.user["id"], request.form.get("note", "").strip()[:500] or None,
                        int(request.form["fid"])))
            audit(db, "flag.review", f"flag:{request.form['fid']}", f"Marked {status}: {request.form.get('note', '').strip()[:200]}")
            flash("Saved.")
        return redirect(url_for("control.flags"))
    show = request.args.get("show", "open")
    kind = request.args.get("kind", "")
    rows = db.execute("SELECT f.*, u.email AS reviewer FROM flags f LEFT JOIN users u ON u.id=f.reviewed_by WHERE f.status=? "
                      "AND (?='' OR f.kind=?) ORDER BY f.id DESC LIMIT 200", (show, kind, kind)).fetchall()
    return render_template("admin/flags.html", rows=rows, show=show, kind=kind)


# ---------------- support cases ----------------

def open_case(name, email, topic, message, user_id=None, competition_id=None, checkout_id=None):
    db = get_db()
    now = iso(utcnow())
    cur = db.execute("INSERT INTO cases (user_id, name, email, topic, competition_id, checkout_id, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (user_id, name, email, topic, competition_id, checkout_id, now, now))
    db.execute("INSERT INTO case_notes (case_id, created_at, kind, body) VALUES (?,?, 'customer', ?)", (cur.lastrowid, now, message))
    return cur.lastrowid


LOCK_MINUTES = 20          # a case opened by a staff member stays theirs this long after their last action
PRIORITY_ORDER = "CASE k.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"


def _lock_holder(db, k):
    """The other staff member currently working on this case, or None."""
    if not k["locked_by"] or k["locked_by"] == g.user["id"] or not k["locked_at"]:
        return None
    if (utcnow() - parse_iso(k["locked_at"])).total_seconds() > LOCK_MINUTES * 60:
        return None
    return db.execute("SELECT id, name, email FROM users WHERE id=?", (k["locked_by"],)).fetchone()


def _take_lock(db, case_id):
    db.execute("UPDATE cases SET locked_by=?, locked_at=? WHERE id=?", (g.user["id"], iso(utcnow()), case_id))


@bp.route("/cases")
@require("cases")
def cases():
    show = request.args.get("show", "open")
    order = f"{PRIORITY_ORDER}, k.updated_at ASC" if show != "resolved" else "k.updated_at DESC"
    rows = get_db().execute(
        "SELECT k.*, a.name AS assignee, l.name AS locker, (SELECT COUNT(*) FROM case_notes n WHERE n.case_id=k.id) AS notes, "
        "(SELECT MAX(created_at) FROM case_notes n WHERE n.case_id=k.id AND n.kind='customer') AS last_customer FROM cases k "
        "LEFT JOIN users a ON a.id=k.assigned_to LEFT JOIN users l ON l.id=k.locked_by AND k.locked_at>? "
        f"WHERE k.status=? ORDER BY {order} LIMIT 200",
        (iso(utcnow() - timedelta(minutes=LOCK_MINUTES)), show)).fetchall()
    return render_template("admin/cases.html", rows=rows, show=show)


@bp.route("/cases/<int:case_id>", methods=["GET", "POST"])
@require("cases")
def case_detail(case_id):
    db = get_db()
    k = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if k is None:
        abort(404)
    holder = _lock_holder(db, k)
    if request.method == "POST":
        f = request.form
        now = iso(utcnow())
        if f.get("action") == "takeover":
            _take_lock(db, case_id)
            audit(db, "case.takeover", f"case:{case_id}", f"Took over from {holder['email'] if holder else 'nobody'}")
            flash("You're now handling this case.")
            return redirect(url_for("control.case_detail", case_id=case_id))
        if f.get("action") == "release":
            db.execute("UPDATE cases SET locked_by=NULL, locked_at=NULL WHERE id=? AND locked_by=?", (case_id, g.user["id"]))
            flash("Released — someone else can pick it up.")
            return redirect(url_for("control.cases"))
        if holder:
            flash(f"{holder['name']} is working on this case, so nothing was saved. Take it over first if you need to.", "error")
            return redirect(url_for("control.case_detail", case_id=case_id))
        body = f.get("body", "").strip()[:5000]
        if f.get("action") in ("reply", "internal"):
            if not body:
                flash("Write something first.", "error")
                return redirect(url_for("control.case_detail", case_id=case_id))
            db.execute("INSERT INTO case_notes (case_id, created_at, actor_id, kind, body) VALUES (?,?,?,?,?)",
                       (case_id, now, g.user["id"], f["action"], body))
            if f["action"] == "reply":
                mailer.send(k["email"], f"Re: {k['topic']} [case #{case_id}]",
                            f"Hi {k['name'].split()[0]},\n\n{body}\n\nReply to this email, or follow it in your account under "
                            f"Help & support, to continue the conversation.", heading="A reply from our team")
                db.execute("UPDATE cases SET status='waiting', updated_at=? WHERE id=?", (now, case_id))
                if k["user_id"]:
                    from .notify import notify
                    notify(k["user_id"], "support", f"We've replied: {k['topic']}", body[:300],
                           link=url_for("public.case_view", case_id=case_id), dedupe_key=f"case-reply:{case_id}:{now}")
        sets = {}
        if f.get("status") in ("open", "waiting", "resolved"):
            sets["status"] = f["status"]
        if f.get("priority") in ("high", "normal", "low"):
            sets["priority"] = f["priority"]
        if f.get("resolution"):
            sets["resolution"] = f["resolution"].strip()[:500]
        for col in ("competition_id", "checkout_id", "user_id"):
            if f.get(col, "").isdigit():
                sets[col] = int(f[col])
        if f.get("assign") == "me":
            sets["assigned_to"] = g.user["id"]
        if sets:
            sets["updated_at"] = now
            db.execute(f"UPDATE cases SET {', '.join(k_ + '=?' for k_ in sets)} WHERE id=?", [*sets.values(), case_id])
        if sets.get("status") == "resolved":
            db.execute("UPDATE cases SET locked_by=NULL, locked_at=NULL WHERE id=?", (case_id,))
        else:
            _take_lock(db, case_id)
        audit(db, "case.update", f"case:{case_id}", f"{f.get('action') or 'update'} {sets or ''}")
        flash("Saved.")
        return redirect(url_for("control.case_detail", case_id=case_id))
    if not holder and k["status"] != "resolved":
        _take_lock(db, case_id)
    notes = db.execute("SELECT n.*, u.name AS actor FROM case_notes n LEFT JOIN users u ON u.id=n.actor_id WHERE case_id=? ORDER BY n.id",
                       (case_id,)).fetchall()
    customer = db.execute("SELECT * FROM users WHERE id=?", (k["user_id"],)).fetchone() if k["user_id"] else \
        db.execute("SELECT * FROM users WHERE email=?", (k["email"],)).fetchone()
    orders = db.execute("SELECT id, status, cash_due, created_at FROM checkouts WHERE user_id=? ORDER BY id DESC LIMIT 10",
                        (customer["id"],)).fetchall() if customer else []
    comp = db.execute("SELECT id, title FROM competitions WHERE id=?", (k["competition_id"],)).fetchone() if k["competition_id"] else None
    history = None
    if customer:
        from .services import balances
        uid = customer["id"]
        history = {
            "cases": db.execute("SELECT id, topic, status, created_at FROM cases WHERE (user_id=? OR email=?) AND id!=? ORDER BY id DESC LIMIT 10",
                                (uid, customer["email"], case_id)).fetchall(),
            "balances": balances(db, uid),
            "spent": db.execute("SELECT COALESCE(SUM(cash_due + credit_used + deposit_used),0) FROM checkouts WHERE user_id=? AND status='paid'",
                                (uid,)).fetchone()[0],
            "withdrawals": db.execute("SELECT id, amount, status, created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 5",
                                      (uid,)).fetchall(),
            "flags": db.execute("SELECT kind, detail FROM flags WHERE status='open' AND subject=?", (f"user:{uid}",)).fetchall(),
            "wins": db.execute("SELECT COUNT(*) FROM competitions c JOIN tickets t ON t.id=c.winner_ticket_id WHERE t.user_id=?",
                               (uid,)).fetchone()[0],
            "on_break": bool(customer["excluded_until"] and customer["excluded_until"] > iso(utcnow())),
        }
    return render_template("admin/case.html", k=k, notes=notes, customer=customer, orders=orders, comp=comp,
                           holder=holder, history=history, lock_minutes=LOCK_MINUTES)


# ---------------- measurable targets (docs/TARGETS.md) ----------------

EXTERNAL_TARGETS = [   # measured outside the app; staff record the latest reading
    ("uptime", "Uptime (last 30 days, from your uptime monitor)", "%", ">=", 99.9),
    ("lcp_mobile", "Largest Contentful Paint on a phone (PageSpeed Insights, homepage)", "s", "<=", 2.5),
    ("mobile_usability", "Lighthouse accessibility score on a phone (competition page)", "", ">=", 95),
    ("usability_tasks", "Usability test: tasks completed without help (latest round)", "%", ">=", 90),
]


def target_rows(db):
    import json
    from .metrics import stats
    from .services import get_setting
    month = iso(utcnow() - timedelta(days=30))
    day30 = month[:10]

    def funnel(step, device=None):
        return _sum(db, "SELECT SUM(n) FROM funnel_counts WHERE step=? AND day>=?" + (" AND device=?" if device else ""),
                    *((step, day30, device) if device else (step, day30)))

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    rows = []

    def add(label, value, unit, op, target, how):
        ok = None if value is None else (value >= target if op == ">=" else value <= target)
        rows.append({"label": label, "value": value, "unit": unit, "op": op, "target": target, "ok": ok, "how": how})

    add("Checkout completion (started checkout → paid)", pct(funnel("paid"), funnel("checkout")), "%", ">=", 70,
        "Journey counts, last 30 days")
    mob = pct(funnel("paid", "mobile"), funnel("checkout", "mobile"))
    desk = pct(funnel("paid", "desktop"), funnel("checkout", "desktop"))
    add("Mobile checkout completion gap vs desktop", None if mob is None or desk is None else round(desk - mob, 1), " pts", "<=", 10,
        f"Mobile {mob if mob is not None else '—'}% vs desktop {desk if desk is not None else '—'}%, last 30 days")
    card = _sum(db, "SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>=? "
                    "AND status IN ('paid','credit_refused','needs_refund')", month)
    bad = _sum(db, "SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>=? "
                   "AND status IN ('credit_refused','needs_refund')", month)
    add("Payment problems (refused or needing a refund)", pct(bad, card), "%", "<=", 2, f"{bad} of {card} card payments, last 30 days")
    orders = _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status='paid' AND paid_at>=?", month)
    cases = _sum(db, "SELECT COUNT(*) FROM cases WHERE created_at>=? AND topic IN ('Entry','Account','Other','Payment')", month)
    add("“How do I / what happened?” support requests per 100 orders", round(100 * cases / orders, 1) if orders else None, "", "<=", 3,
        f"{cases} cases about entries, accounts, payments or other, {orders} paid orders, last 30 days")
    st = stats(db, 7)
    add("Server errors (5xx) per 100 requests", round(st["error_rate"], 3) if st["error_rate"] is not None else None, "%", "<=", 0.1,
        f"{st['errors']} of {st['requests']:,} requests, last 7 days")
    add("Server time, 95th percentile", st["p95_under_ms"], " ms", "<=", 500, "Time to build each page on the server, last 7 days "
        "(phone download time comes on top — see Largest Contentful Paint)")
    ext = json.loads(get_setting("external_measurements") or "{}")
    for key, label, unit, op, target in EXTERNAL_TARGETS:
        m = ext.get(key) or {}
        add(label, m.get("value"), unit, op, target, f"Recorded {m['at'][:10]} by {m.get('by', 'staff')}" if m else "Not recorded yet")
    return rows


@bp.route("/targets", methods=["GET", "POST"])
@require("reports")
def targets():
    import json
    from .services import get_setting, set_setting
    db = get_db()
    if request.method == "POST":
        ext = json.loads(get_setting("external_measurements") or "{}")
        for key, label, *_ in EXTERNAL_TARGETS:
            raw = request.form.get(key, "").strip()
            if raw:
                try:
                    ext[key] = {"value": float(raw), "at": iso(utcnow()), "by": g.user["email"]}
                except ValueError:
                    flash(f"{label}: enter a number.", "error")
                    return redirect(url_for("control.targets"))
        set_setting("external_measurements", json.dumps(ext))
        audit(db, "targets.record", None, json.dumps({k: v["value"] for k, v in ext.items()}))
        flash("Saved.")
        return redirect(url_for("control.targets"))
    errors = db.execute("SELECT * FROM error_log WHERE resolved_at IS NULL ORDER BY last_at DESC LIMIT 50").fetchall()
    return render_template("admin/targets.html", rows=target_rows(db), external=EXTERNAL_TARGETS, errors=errors)


@bp.route("/errors/<int:eid>/resolve", methods=["POST"])
@require("audit")
def resolve_error(eid):
    db = get_db()
    db.execute("UPDATE error_log SET resolved_at=? WHERE id=?", (iso(utcnow()), eid))
    audit(db, "error.resolved", f"error:{eid}", "Marked fixed")
    flash("Marked as fixed — it reappears if it happens again.")
    return redirect(url_for("control.targets") + "#errors")


# ---------------- prize liability ----------------

@bp.route("/liability")
@require("reports")
def liability():
    from .checks import liability as instant_liability
    db = get_db()
    games = instant_liability(db)
    draws = db.execute(
        "SELECT c.id, c.title, c.prize_value, c.drawn_at, p.id AS claim_id, p.status, p.prize_choice FROM competitions c "
        "LEFT JOIN prize_claims p ON p.id=(SELECT MAX(id) FROM prize_claims WHERE competition_id=c.id) "
        "WHERE c.game_type='' AND c.status='drawn' AND (p.id IS NULL OR p.status NOT IN ('delivered')) ORDER BY c.drawn_at").fetchall()
    upcoming = db.execute("SELECT id, title, prize_value, ends_at, status FROM competitions WHERE game_type='' AND status='live' "
                          "ORDER BY ends_at").fetchall()
    wallets = db.execute("SELECT COALESCE(SUM(CASE WHEN kind='cash' THEN amount END),0) cash, "
                         "COALESCE(SUM(CASE WHEN kind='credit' THEN amount END),0) credit, "
                         "COALESCE(SUM(CASE WHEN kind='deposit' THEN amount END),0) deposit FROM credit_ledger").fetchone()
    pending_w = db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status IN ('requested','processing')").fetchone()[0]
    totals = {
        "instant_owed": sum(g_["owed_v"] for g_ in games),
        "instant_left": sum(g_["left_v"] for g_ in games if g_["c"]["status"] == "live"),
        "draw_owed": sum(d["prize_value"] for d in draws),
        "draw_upcoming": sum(d["prize_value"] for d in upcoming),
    }
    return render_template("admin/liability.html", games=games, draws=draws, upcoming=upcoming, wallets=wallets,
                           pending_w=pending_w, totals=totals, claim_names=CLAIM_NAMES)


# ---------------- system health (see jobs.py) ----------------

@bp.route("/health", methods=["GET", "POST"])
@require("audit")
def health():
    from .jobs import health_checks, run_all_jobs
    db = get_db()
    if request.method == "POST" and request.form.get("run"):
        run_all_jobs(force=True)
        flash("Background jobs run now.")
        return redirect(url_for("control.health"))
    runs = db.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 100").fetchall()
    failed_mail = db.execute("SELECT * FROM notifications WHERE email_status='failed' ORDER BY id DESC LIMIT 20").fetchall()
    return render_template("admin/health.html", checks=health_checks(db), runs=runs, failed_mail=failed_mail)


# ---------------- journey analytics & competition performance ----------------

@bp.route("/analytics")
@require("reports")
def analytics():
    from .analytics import STEPS
    db = get_db()
    start, end = _period()
    device = request.args.get("device", "")
    comp = request.args.get("comp", type=int)
    where, args = "day>=? AND day<?", [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")]
    if device in ("mobile", "tablet", "desktop"):
        where += " AND device=?"
        args.append(device)
    rows = []
    for step, label in STEPS:
        w, a = where, list(args)
        if comp and step not in ("home", "basket"):
            w += " AND comp_id=?"
            a.append(comp)
        rows.append({"step": step, "label": label, "n": _sum(db, f"SELECT SUM(n) FROM funnel_counts WHERE {w} AND step=?", *a, step)})
    for i, r in enumerate(rows):
        prev = rows[i - 1]["n"] if i else None
        r["rate"] = round(100 * r["n"] / prev) if prev else None
    by_device = db.execute(f"SELECT device, step, SUM(n) FROM funnel_counts WHERE day>=? AND day<? GROUP BY device, step",
                           args[:2]).fetchall()
    dev = {}
    for d, st, n in by_device:
        dev.setdefault(d, {})[st] = n
    comps = db.execute("SELECT id, title FROM competitions WHERE status IN ('live','drawn') ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("admin/analytics.html", rows=rows, dev=dev, comps=comps, comp=comp, device=device,
                           start=start.strftime("%Y-%m-%d"), end=(end - timedelta(days=1)).strftime("%Y-%m-%d"))


def comp_metrics(db, cid, a, b):
    paid = "o.status='paid' AND o.paid_at>=? AND o.paid_at<?"
    m = {
        "paid_entries": _sum(db, f"SELECT SUM(o.quantity) FROM orders o WHERE o.competition_id=? AND {paid}", cid, a, b),
        "free_entries": _sum(db, "SELECT COUNT(*) FROM tickets WHERE competition_id=? AND postal_entry_id IS NOT NULL AND created_at>=? "
                                 "AND created_at<?", cid, a, b),
        "revenue": _sum(db, f"SELECT SUM(o.amount) FROM orders o WHERE o.competition_id=? AND {paid}", cid, a, b),
        "orders": _sum(db, f"SELECT COUNT(*) FROM orders o WHERE o.competition_id=? AND {paid}", cid, a, b),
        "entrants": _sum(db, "SELECT COUNT(DISTINCT COALESCE(user_id, -postal_entry_id)) FROM tickets WHERE competition_id=? "
                             "AND status='issued' AND created_at>=? AND created_at<?", cid, a, b),
        "refunds": _sum(db, "SELECT SUM(l.amount) FROM credit_ledger l JOIN orders o ON l.ref IN ('refund-o' || o.id, "
                            "'refund-o' || o.id || '-credit', 'refund-o' || o.id || '-deposit') WHERE o.competition_id=? "
                            "AND l.created_at>=? AND l.created_at<?", cid, a, b),
        "instant_cost": _sum(db, "SELECT SUM(value) FROM instant_prizes WHERE competition_id=? AND won_at>=? AND won_at<?", cid, a, b),
        "views": _sum(db, "SELECT SUM(n) FROM funnel_counts WHERE comp_id=? AND step='competition' AND day>=? AND day<?", cid, a[:10], b[:10]),
        "buyers_views": _sum(db, "SELECT SUM(n) FROM funnel_counts WHERE comp_id=? AND step='paid' AND day>=? AND day<?", cid, a[:10], b[:10]),
    }
    c = db.execute("SELECT * FROM competitions WHERE id=?", (cid,)).fetchone()
    m["draw_cost"] = c["prize_value"] if c["drawn_at"] and a <= c["drawn_at"] < b else 0
    m["avg_order"] = m["revenue"] // m["orders"] if m["orders"] else 0
    m["conversion"] = round(100 * m["buyers_views"] / m["views"], 1) if m["views"] else None
    m["margin"] = m["revenue"] - m["instant_cost"] - m["draw_cost"] - m["refunds"]
    return m


@bp.route("/performance")
@require("reports")
def performance():
    db = get_db()
    start, end = _period()
    span = end - start
    pstart, pend = start - span, start
    a, b, pa, pb = iso(start), iso(end), iso(pstart), iso(pend)
    rows = []
    for c in db.execute("SELECT * FROM competitions WHERE status!='draft' AND (ends_at>=? OR status='live') ORDER BY ends_at DESC "
                        "LIMIT 200", (pa,)).fetchall():
        m = comp_metrics(db, c["id"], a, b)
        if any(m[k] for k in ("paid_entries", "free_entries", "views", "refunds")) or c["status"] == "live":
            rows.append({"c": c, "m": m})
    keys = ("paid_entries", "free_entries", "revenue", "orders", "refunds", "instant_cost", "draw_cost", "margin")
    now = {k: sum(r["m"][k] for r in rows) for k in keys}
    prev = {k: 0 for k in keys}
    for c in db.execute("SELECT id FROM competitions WHERE status!='draft'").fetchall():
        pm = comp_metrics(db, c["id"], pa, pb)
        for k in keys:
            prev[k] += pm[k]
    now["avg_order"] = now["revenue"] // now["orders"] if now["orders"] else 0
    prev["avg_order"] = prev["revenue"] // prev["orders"] if prev["orders"] else 0
    now["players"] = _sum(db, "SELECT COUNT(DISTINCT user_id) FROM checkouts WHERE status='paid' AND paid_at>=? AND paid_at<?", a, b)
    prev["players"] = _sum(db, "SELECT COUNT(DISTINCT user_id) FROM checkouts WHERE status='paid' AND paid_at>=? AND paid_at<?", pa, pb)
    return render_template("admin/performance.html", rows=rows, now=now, prev=prev,
                           start=start.strftime("%Y-%m-%d"), end=(end - timedelta(days=1)).strftime("%Y-%m-%d"),
                           pstart=pstart.strftime("%d %b"), pend=(pend - timedelta(days=1)).strftime("%d %b"))
