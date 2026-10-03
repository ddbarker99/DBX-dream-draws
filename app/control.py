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
    return render_template("admin/finance.html", r=finance_report(get_db(), start, end),
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
    rows = db.execute("SELECT f.*, u.email AS reviewer FROM flags f LEFT JOIN users u ON u.id=f.reviewed_by WHERE f.status=? "
                      "ORDER BY f.id DESC LIMIT 200", (show,)).fetchall()
    return render_template("admin/flags.html", rows=rows, show=show)


# ---------------- support cases ----------------

def open_case(name, email, topic, message, user_id=None, competition_id=None, checkout_id=None):
    db = get_db()
    now = iso(utcnow())
    cur = db.execute("INSERT INTO cases (user_id, name, email, topic, competition_id, checkout_id, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (user_id, name, email, topic, competition_id, checkout_id, now, now))
    db.execute("INSERT INTO case_notes (case_id, created_at, kind, body) VALUES (?,?, 'customer', ?)", (cur.lastrowid, now, message))
    return cur.lastrowid


@bp.route("/cases")
@require("cases")
def cases():
    show = request.args.get("show", "open")
    rows = get_db().execute(
        "SELECT k.*, a.name AS assignee, (SELECT COUNT(*) FROM case_notes n WHERE n.case_id=k.id) AS notes FROM cases k "
        "LEFT JOIN users a ON a.id=k.assigned_to WHERE k.status=? ORDER BY k.updated_at DESC LIMIT 200", (show,)).fetchall()
    return render_template("admin/cases.html", rows=rows, show=show)


@bp.route("/cases/<int:case_id>", methods=["GET", "POST"])
@require("cases")
def case_detail(case_id):
    db = get_db()
    k = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if k is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        now = iso(utcnow())
        body = f.get("body", "").strip()[:5000]
        if f.get("action") in ("reply", "internal"):
            if not body:
                flash("Write something first.", "error")
                return redirect(url_for("control.case_detail", case_id=case_id))
            db.execute("INSERT INTO case_notes (case_id, created_at, actor_id, kind, body) VALUES (?,?,?,?,?)",
                       (case_id, now, g.user["id"], f["action"], body))
            if f["action"] == "reply":
                mailer.send(k["email"], f"Re: {k['topic']} [case #{case_id}]",
                            f"Hi {k['name'].split()[0]},\n\n{body}\n\nReply to this email to continue the conversation.",
                            heading="A reply from our team")
                db.execute("UPDATE cases SET status='waiting', updated_at=? WHERE id=?", (now, case_id))
        sets = {}
        if f.get("status") in ("open", "waiting", "resolved"):
            sets["status"] = f["status"]
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
        audit(db, "case.update", f"case:{case_id}", f"{f.get('action') or 'update'} {sets or ''}")
        flash("Saved.")
        return redirect(url_for("control.case_detail", case_id=case_id))
    notes = db.execute("SELECT n.*, u.name AS actor FROM case_notes n LEFT JOIN users u ON u.id=n.actor_id WHERE case_id=? ORDER BY n.id",
                       (case_id,)).fetchall()
    customer = db.execute("SELECT * FROM users WHERE id=?", (k["user_id"],)).fetchone() if k["user_id"] else \
        db.execute("SELECT * FROM users WHERE email=?", (k["email"],)).fetchone()
    orders = db.execute("SELECT id, status, cash_due, created_at FROM checkouts WHERE user_id=? ORDER BY id DESC LIMIT 10",
                        (customer["id"],)).fetchall() if customer else []
    comp = db.execute("SELECT id, title FROM competitions WHERE id=?", (k["competition_id"],)).fetchone() if k["competition_id"] else None
    return render_template("admin/case.html", k=k, notes=notes, customer=customer, orders=orders, comp=comp)


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
