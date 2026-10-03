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


def work_queues(db):
    """The Control Centre board: each area of the business with the specific items waiting, most urgent first.
    Each queue: {key, title, link, rows: [(text, link, meta, level)], total}."""
    now = utcnow()
    q = []

    def queue(key, title, link, items, total=None):
        q.append({"key": key, "title": title, "link": link, "rows": items[:5], "total": len(items) if total is None else total})

    items = []
    for c in db.execute("SELECT id, title, ends_at FROM competitions WHERE status='live' AND game_type='' AND locked_at IS NOT NULL"):
        items.append((c["title"], url_for("admin.entries", cid=c["id"]) + "#draw", "closed — ready to draw", "bad"))
    for c in db.execute("SELECT c.id, c.title, c.ends_at, (SELECT COUNT(*) FROM postal_entries p WHERE p.competition_id=c.id "
                        "AND p.status='received') AS postal FROM competitions c WHERE c.status='live' AND c.locked_at IS NULL AND c.ends_at<?",
                        (iso(now - timedelta(minutes=50)),)):
        items.append((c["title"], url_for("admin.entries", cid=c["id"]), f"past closing — {c['postal']} postal waiting" if c["postal"]
                      else "past closing — payments still finishing", "bad"))
    for c in db.execute("SELECT id, title, ends_at FROM competitions WHERE status='live' AND game_type='' AND ends_at>? AND ends_at<? "
                        "ORDER BY ends_at", (iso(now), iso(now + timedelta(hours=24)))):
        items.append((c["title"], url_for("admin.entries", cid=c["id"]),
                      "draws at " + parse_iso(c["ends_at"]).astimezone(UK).strftime("%H:%M") + " (next 24 h)", "info"))
    queue("draws", "Draws", url_for("admin.dashboard"), items, total=sum(1 for i in items if i[3] != "info"))

    items = []
    for r in db.execute("SELECT pc.id, pc.status, pc.prize_choice, pc.delivery_address, pc.updated_at, c.title, c.cash_alternative "
                        "FROM prize_claims pc JOIN competitions c ON c.id=pc.competition_id "
                        "WHERE pc.status NOT IN ('delivered','forfeited') ORDER BY pc.created_at"):
        waiting_on_winner = (r["cash_alternative"] and not r["prize_choice"]) or (r["prize_choice"] != "cash" and not r["delivery_address"])
        stale = parse_iso(r["updated_at"]) < now - timedelta(days=3)
        meta = ("waiting for the winner" + (" for 3+ days — chase them" if stale else "")) if waiting_on_winner else \
            ("winner ready — " + ("pay the cash alternative" if r["prize_choice"] == "cash" else "arrange delivery"))
        items.append((r["title"], url_for("admin.claim_detail", claim_id=r["id"]), meta,
                      "bad" if (stale or not waiting_on_winner) else "warn"))
    n_instant = _sum(db, "SELECT COUNT(*) FROM instant_prizes WHERE ticket_id IS NOT NULL AND fulfilled=0 AND credit_amount=0")
    if n_instant:
        items.append((f"{n_instant} physical instant prize{'s' if n_instant != 1 else ''} to send", url_for("admin.prizes"), "", "warn"))
    items.sort(key=lambda x: {"bad": 0, "warn": 1}.get(x[3], 2))
    queue("prizes", "Prizes & winners", url_for("admin.prizes"), items)

    items = []
    for w in db.execute("SELECT w.id, w.amount, w.created_at, w.status, u.name FROM withdrawals w JOIN users u ON u.id=w.user_id "
                        "WHERE w.status IN ('requested','processing') ORDER BY w.created_at"):
        old = parse_iso(w["created_at"]) < now - timedelta(hours=24)
        items.append((f"Withdrawal £{w['amount'] / 100:.2f} — {w['name']}", url_for("admin.payouts"),
                      f"{w['status']}, waiting {int((now - parse_iso(w['created_at'])).total_seconds() // 3600)} h", "bad" if old else "warn"))
    for k in db.execute("SELECT id, cash_due FROM checkouts WHERE status='needs_refund' ORDER BY id"):
        items.append((f"Refund due: order #{k['id']} (£{k['cash_due'] / 100:.2f})", url_for("admin.payouts"), "refund in Stripe, then mark done", "bad"))
    for d in db.execute("SELECT id, amount FROM deposits WHERE status='needs_refund'"):
        items.append((f"Deposit refund due #{d['id']} (£{d['amount'] / 100:.2f})", url_for("admin.payouts"), "", "bad"))
    n_flags = _sum(db, "SELECT COUNT(*) FROM flags WHERE status='open' AND kind IN ('Reconciliation','Payment reversal')")
    if n_flags:
        items.append((f"{n_flags} Stripe mismatch / chargeback flag{'s' if n_flags != 1 else ''}", url_for("control.flags"), "", "bad"))
    items.sort(key=lambda x: {"bad": 0, "warn": 1}.get(x[3], 2))
    queue("money", "Money", url_for("admin.payouts"), items)

    items = [(f"{r['title']}: {r['n']} envelope{'s' if r['n'] != 1 else ''}", url_for("admin.postal_queue"),
              "closes " + r["ends_at"][:10], "bad" if r["ends_at"] < iso(now) else "warn")
             for r in db.execute("SELECT c.title, c.ends_at, COUNT(*) n FROM postal_entries p JOIN competitions c ON c.id=p.competition_id "
                                 "WHERE p.status='received' GROUP BY c.id ORDER BY c.ends_at")]
    queue("postal", "Free postal entries", url_for("admin.postal_queue"), items)

    items = []
    for k in db.execute(f"SELECT k.id, k.topic, k.priority, k.updated_at, k.name FROM cases k WHERE k.status='open' "
                        f"ORDER BY {PRIORITY_ORDER}, k.updated_at"):
        hrs = int((now - parse_iso(k["updated_at"])).total_seconds() // 3600)
        items.append((f"#{k['id']} {k['topic']} — {k['name']}", url_for("control.case_detail", case_id=k["id"]),
                      f"{k['priority']} · waiting {hrs} h", "bad" if (k["priority"] == "high" or hrs >= 24) else "warn"))
    n_other = _sum(db, "SELECT COUNT(*) FROM flags WHERE status='open' AND kind NOT IN ('Reconciliation','Payment reversal')")
    if n_other:
        items.append((f"{n_other} fraud / abuse flag{'s' if n_other != 1 else ''} to review", url_for("control.flags"), "", "warn"))
    queue("customers", "Customers & support", url_for("control.cases"), items)

    from .jobs import health_checks
    items = [(c["name"], url_for("control.health"), c["detail"], "bad") for c in health_checks(db) if not c["ok"]]
    n_err = _sum(db, "SELECT COUNT(*) FROM error_log WHERE resolved_at IS NULL")
    if n_err:
        items.append((f"{n_err} unresolved server error{'s' if n_err != 1 else ''}", url_for("control.targets") + "#errors", "", "warn"))
    queue("tech", "Technical", url_for("control.health"), items)
    return q


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
    return render_template("admin/centre.html", money=money, attention=attention_items(db), live=live, queues=work_queues(db),
                           ending=[x for x in live if 0 < x["hours"] <= 48], stage_names=LIFECYCLE_NAMES,
                           instant_left=sum(x["instant_left"] for x in live),
                           instant_value=_sum(db, "SELECT SUM(ip.value) FROM instant_prizes ip JOIN competitions c ON c.id=ip.competition_id "
                                                  "WHERE c.status='live' AND ip.ticket_id IS NULL"),
                           health=health_checks(db))


# ---------------- checkout diagnostics & segments (understanding, not targeting) ----------------

@bp.route("/checkout-diagnostics")
@require("reports")
def checkout_diagnostics():
    """Where card checkouts fail and whether it looks technical or UX — measured before anyone thinks about reminder emails."""
    db = get_db()
    since = iso(utcnow() - timedelta(days=30))
    settled = iso(utcnow() - timedelta(minutes=45))
    base = "FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>=? AND created_at<?"
    outcome = dict(db.execute(f"SELECT status, COUNT(*) {base} GROUP BY status", (since, settled)).fetchall())
    started = sum(outcome.values())

    def rate(n):
        return round(100 * n / started, 1) if started else None
    by_device = db.execute(f"SELECT COALESCE(device,'unknown') d, COUNT(*) n, SUM(status='paid') ok {base} GROUP BY d ORDER BY n DESC",
                           (since, settled)).fetchall()
    bands = db.execute(f"SELECT CASE WHEN cash_due<500 THEN 'under £5' WHEN cash_due<2000 THEN '£5–£20' WHEN cash_due<5000 THEN '£20–£50' "
                       f"ELSE '£50+' END band, COUNT(*) n, SUM(status='paid') ok {base} GROUP BY band ORDER BY MIN(cash_due)", (since, settled)).fetchall()
    from . import UK
    hours = {}
    for r in db.execute(f"SELECT created_at, status {base}", (since, settled)):
        h = parse_iso(r["created_at"]).astimezone(UK).hour // 4 * 4
        t = hours.setdefault(h, [0, 0])
        t[0] += 1
        t[1] += r["status"] == "paid"
    retried = _sum(db, "SELECT COUNT(DISTINCT a.id) FROM checkouts a WHERE a.stripe_session_id IS NOT NULL AND a.status='expired' AND a.created_at>=? "
                       "AND EXISTS (SELECT 1 FROM checkouts b WHERE b.user_id=a.user_id AND b.id>a.id AND b.created_at<=datetime(a.created_at,'+2 hours'))", since)
    recovered = _sum(db, "SELECT COUNT(DISTINCT a.id) FROM checkouts a WHERE a.stripe_session_id IS NOT NULL AND a.status='expired' AND a.created_at>=? "
                         "AND EXISTS (SELECT 1 FROM checkouts b WHERE b.user_id=a.user_id AND b.id>a.id AND b.status='paid' "
                         "AND b.created_at<=datetime(a.created_at,'+1 day'))", since)
    errors = _sum(db, "SELECT COALESCE(SUM(count),0) FROM error_log WHERE last_at>=? AND (endpoint LIKE '%checkout%' OR endpoint LIKE '%basket%')", since)
    steps = dict(db.execute("SELECT step, SUM(n) FROM funnel_counts WHERE day>=? AND step IN ('basket','checkout','paid') GROUP BY step",
                            (since[:10],)).fetchall())
    hints = []
    if errors:
        hints.append(f"{errors} server error(s) on basket/checkout pages — fix these first (Targets → Server errors).")
    if outcome.get("credit_refused", 0) >= max(3, started * 0.05):
        hints.append("Many credit-card refusals: make 'debit cards only' clearer before the payment page.")
    mob = next((r for r in by_device if r["d"] == "mobile"), None)
    desk = next((r for r in by_device if r["d"] == "desktop"), None)
    if mob and desk and mob["n"] >= 10 and desk["n"] >= 10 and mob["ok"] / mob["n"] < desk["ok"] / desk["n"] - 0.15:
        hints.append("Mobile completes much less often than desktop — test checkout on a phone (USABILITY-TEST.md).")
    if started and retried > started * 0.1:
        hints.append("Lots of people try again within 2 hours of an abandoned payment — something on the payment step may be confusing or failing.")
    return render_template("admin/checkout_diagnostics.html", started=started, outcome=outcome, rate=rate, by_device=by_device,
                           bands=bands, hours=sorted(hours.items()), retried=retried, recovered=recovered, errors=errors, steps=steps,
                           hints=hints)


@bp.route("/segments")
@require("reports")
def segments():
    """Broad behavioural groups, as counts only — to understand how the product is doing, not to single people out.
    There's deliberately no export or messaging from here."""
    db = get_db()
    now = utcnow()
    d30, d60 = iso(now - timedelta(days=30)), iso(now - timedelta(days=60))
    first = "(SELECT MIN(paid_at) FROM checkouts k2 WHERE k2.user_id=u.id AND k2.status='paid')"
    last = "(SELECT MAX(paid_at) FROM checkouts k3 WHERE k3.user_id=u.id AND k3.status='paid')"
    rows = db.execute(f"SELECT u.id, u.created_at, {first} AS first_paid, {last} AS last_paid FROM users u WHERE u.is_admin=0").fetchall()
    seg = {"new": [], "returning": [], "lapsed": [], "never": [], "free_only": []}
    postal = {r[0] for r in db.execute("SELECT DISTINCT user_id FROM postal_entries WHERE status='accepted' AND user_id IS NOT NULL")}
    for r in rows:
        if not r["first_paid"]:
            seg["free_only" if r["id"] in postal else "never"].append(r["id"])
        elif r["first_paid"] >= d30:
            seg["new"].append(r["id"])
        elif r["last_paid"] >= d30:
            seg["returning"].append(r["id"])
        elif r["last_paid"] < d60:
            seg["lapsed"].append(r["id"])
        else:
            seg.setdefault("quiet", []).append(r["id"])
    out = []
    labels = {"new": ("New", "First purchase in the last 30 days"), "returning": ("Returning", "Bought before, and again in the last 30 days"),
              "quiet": ("Quiet", "Last bought 30–60 days ago"), "lapsed": ("Lapsed", "Haven't bought for 60+ days"),
              "never": ("Signed up, never bought", "Account but no paid order"), "free_only": ("Free entries only", "Entered by post, never paid")}
    total_rev = _sum(db, "SELECT SUM(cash_due + deposit_used + cash_used) FROM checkouts WHERE status='paid' AND paid_at>=?", d30)
    for key in ("new", "returning", "quiet", "lapsed", "never", "free_only"):
        ids = seg.get(key, [])
        rev = orders = 0
        if ids:
            q = ",".join("?" * len(ids))
            rev = _sum(db, f"SELECT SUM(cash_due + deposit_used + cash_used) FROM checkouts WHERE status='paid' AND paid_at>=? AND user_id IN ({q})", d30, *ids)
            orders = _sum(db, f"SELECT COUNT(*) FROM checkouts WHERE status='paid' AND paid_at>=? AND user_id IN ({q})", d30, *ids)
        out.append({"label": labels[key][0], "what": labels[key][1], "n": len(ids), "rev": rev, "orders": orders,
                    "share": round(100 * rev / total_rev) if total_rev else None, "avg": rev // orders if orders else 0})
    return render_template("admin/segments.html", segs=out, customers=len(rows))


@bp.route("/experiments", methods=["GET", "POST"])
@require("reports")
def experiments():
    from .experiments import EXPERIMENTS, results
    db = get_db()
    if request.method == "POST":
        if not can(g.user, "settings"):
            flash("Only administrators can start or stop experiments.", "error")
            return redirect(url_for("control.experiments"))
        key, action = request.form.get("key"), request.form.get("action")
        if key not in EXPERIMENTS or action not in ("start", "stop"):
            abort(400)
        now = iso(utcnow())
        db.execute("INSERT INTO experiments (key, status) VALUES (?, 'stopped') ON CONFLICT(key) DO NOTHING", (key,))
        if action == "start":
            db.execute("UPDATE experiments SET status='running', started_at=COALESCE(started_at, ?), stopped_at=NULL WHERE key=?", (now, key))
        else:
            db.execute("UPDATE experiments SET status='stopped', stopped_at=?, decision=? WHERE key=?",
                       (now, request.form.get("decision", "").strip()[:300] or None, key))
        audit(db, f"experiment.{action}", f"experiment:{key}", request.form.get("decision", "")[:200])
        flash("Experiment started." if action == "start" else "Experiment stopped — everyone sees the original design again.")
        return redirect(url_for("control.experiments"))
    state = {r["key"]: r for r in db.execute("SELECT * FROM experiments")}
    items = []
    for key, exp in EXPERIMENTS.items():
        res, verdict = results(db, key)
        items.append({"key": key, "exp": exp, "state": state.get(key), "results": res, "verdict": verdict})
    return render_template("admin/experiments.html", items=items)


# ---------------- feedback & development backlog ----------------

BACKLOG_KINDS = (("bug", "Bug"), ("improvement", "Improvement"), ("feature", "New feature"))
BACKLOG_STATUSES = (("open", "Open"), ("planned", "Planned"), ("done", "Done"), ("wont_do", "Won't do"))


def add_evidence(db, backlog_id, source, source_id, note=None):
    db.execute("INSERT OR IGNORE INTO backlog_evidence (backlog_id, source, source_id, note, added_by, created_at) VALUES (?,?,?,?,?,?)",
               (backlog_id, source, source_id, (note or "")[:300] or None, g.user["id"], iso(utcnow())))
    if source == "feedback":
        db.execute("UPDATE feedback SET status='reviewed', backlog_id=? WHERE id=?", (backlog_id, source_id))


@bp.route("/backlog", methods=["GET", "POST"])
@require("cases")
def backlog():
    """Support turns recurring customer problems into one structured ticket with the evidence attached, instead of
    sending developers scattered messages. Sorted by how much evidence each has."""
    db = get_db()
    if request.method == "POST":
        f = request.form
        source, sid = f.get("source"), int(f["source_id"]) if f.get("source_id", "").isdigit() else None
        if f.get("status") and f.get("bid", "").isdigit():
            if f["status"] in dict(BACKLOG_STATUSES):
                db.execute("UPDATE backlog SET status=?, updated_at=? WHERE id=?", (f["status"], iso(utcnow()), int(f["bid"])))
                audit(db, "backlog.status", f"backlog:{f['bid']}", f["status"])
            return redirect(url_for("control.backlog_item", bid=int(f["bid"])))
        if f.get("bid", "").isdigit():                       # attach evidence to an existing item
            bid = int(f["bid"])
        else:
            title = " ".join(f.get("title", "").split())[:150]
            if len(title) < 5:
                flash("Give the problem a short, clear title.", "error")
                return redirect(request.referrer or url_for("control.backlog"))
            now = iso(utcnow())
            bid = db.execute("INSERT INTO backlog (kind, title, detail, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                             (f.get("kind") if f.get("kind") in dict(BACKLOG_KINDS) else "improvement", title,
                              f.get("detail", "").strip()[:4000] or None, g.user["id"], now, now)).lastrowid
            audit(db, "backlog.create", f"backlog:{bid}", title)
        if source in ("case", "feedback", "error") and sid:
            add_evidence(db, bid, source, sid, f.get("note"))
        flash("Added to the backlog.")
        return redirect(url_for("control.backlog_item", bid=bid))
    show = request.args.get("show", "open")
    rows = db.execute("SELECT b.*, (SELECT COUNT(*) FROM backlog_evidence e WHERE e.backlog_id=b.id) AS evidence FROM backlog b "
                      "WHERE b.status=? ORDER BY evidence DESC, b.updated_at DESC", (show,)).fetchall()
    return render_template("admin/backlog.html", rows=rows, show=show, kinds=BACKLOG_KINDS, statuses=BACKLOG_STATUSES)


@bp.route("/backlog/<int:bid>")
@require("cases")
def backlog_item(bid):
    db = get_db()
    b = db.execute("SELECT b.*, u.email FROM backlog b LEFT JOIN users u ON u.id=b.created_by WHERE b.id=?", (bid,)).fetchone()
    if b is None:
        abort(404)
    ev = []
    for e in db.execute("SELECT * FROM backlog_evidence WHERE backlog_id=? ORDER BY id DESC", (bid,)):
        if e["source"] == "case":
            k = db.execute("SELECT topic, reason, created_at FROM cases WHERE id=?", (e["source_id"],)).fetchone()
            first = db.execute("SELECT body FROM case_notes WHERE case_id=? ORDER BY id LIMIT 1", (e["source_id"],)).fetchone()
            ev.append((e, f"Case #{e['source_id']}: {k['topic'] if k else ''}", (first["body"][:300] if first else ""),
                       url_for("control.case_detail", case_id=e["source_id"])))
        elif e["source"] == "feedback":
            fb = db.execute("SELECT * FROM feedback WHERE id=?", (e["source_id"],)).fetchone()
            ev.append((e, f"Feedback ({fb['context']}, {fb['rating'] or '–'}/5)" if fb else "Feedback", (fb["comment"] or "") if fb else "",
                       url_for("control.feedback_admin")))
        else:
            er = db.execute("SELECT error, count FROM error_log WHERE id=?", (e["source_id"],)).fetchone()
            ev.append((e, f"Server error ×{er['count']}" if er else "Server error", er["error"] if er else "", url_for("control.targets") + "#errors"))
    return render_template("admin/backlog_item.html", b=b, ev=ev, kinds=dict(BACKLOG_KINDS), statuses=BACKLOG_STATUSES)


@bp.route("/feedback")
@require("cases")
def feedback_admin():
    db = get_db()
    since = iso(utcnow() - timedelta(days=30))
    summary = db.execute("SELECT context, COUNT(*) n, ROUND(AVG(rating), 1) avg, SUM(rating<=2) low FROM feedback WHERE created_at>=? "
                         "GROUP BY context", (since,)).fetchall()
    rows = db.execute("SELECT f.*, u.email FROM feedback f LEFT JOIN users u ON u.id=f.user_id ORDER BY f.status='new' DESC, "
                      "f.rating IS NULL, f.rating, f.id DESC LIMIT 200").fetchall()
    items = db.execute("SELECT id, title FROM backlog WHERE status IN ('open','planned') ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("admin/feedback.html", summary=summary, rows=rows, items=items, kinds=BACKLOG_KINDS)


# ---------------- universal admin search ----------------

@bp.route("/search")
@require("comps.view")
def search():
    """One box for everything staff look up: customers, orders, tickets, competitions, withdrawals, deposits, cases and
    Stripe references. Results are grouped; a single exact match goes straight to the record."""
    import re as _re
    db = get_db()
    q = " ".join(request.args.get("q", "").split())[:80]
    groups = {}

    def hit(group, label, link, meta=""):
        groups.setdefault(group, []).append((label, link, meta))

    if q:
        low = q.lower()
        ref = _re.fullmatch(r"(?i)(order|checkout|c|w|withdrawal|d|deposit|case|t|ticket|#)\s*#?(\d+)", q)
        num = int(ref.group(2)) if ref else (int(q.lstrip("#")) if q.lstrip("#").isdigit() else None)
        kind = (ref.group(1).lower() if ref else "")
        if can(g.user, "users.view"):
            for u in db.execute("SELECT id, name, email, phone FROM users WHERE email LIKE ? OR name LIKE ? OR (phone IS NOT NULL AND "
                                "replace(phone,' ','') LIKE ?) ORDER BY id DESC LIMIT 20", (f"%{q}%", f"%{q}%",
                                                                                              f"%{q.replace(' ', '')}%" if len(q) >= 6 else "\x00")):
                hit("Customers", f"{u['name']} · {u['email']}", url_for("control.timeline", uid=u["id"]), f"#{u['id']}")
        if num is not None:
            if kind in ("", "#", "order", "checkout", "c"):
                for k in db.execute("SELECT k.id, k.status, k.cash_due, u.email, u.id AS uid FROM checkouts k JOIN users u ON u.id=k.user_id "
                                    "WHERE k.id=?", (num,)):
                    hit("Orders", f"Order #{k['id']} · {k['status']} · £{k['cash_due'] / 100:.2f}", url_for("admin.order_detail", cid=k["id"]),
                        k["email"])
            if kind in ("", "#", "t", "ticket"):
                for t in db.execute("SELECT t.number, t.status, c.title, c.id AS cid, COALESCE(u.email, p.email) AS who, u.id AS uid "
                                    "FROM tickets t JOIN competitions c ON c.id=t.competition_id LEFT JOIN users u ON u.id=t.user_id "
                                    "LEFT JOIN postal_entries p ON p.id=t.postal_entry_id WHERE t.number=? ORDER BY c.id DESC LIMIT 30", (num,)):
                    hit("Tickets", f"Ticket #{t['number']} in {t['title']}", url_for("admin.entries", cid=t["cid"]),
                        f"{t['who'] or 'unknown'} · {t['status']}")
            if kind in ("", "#", "w", "withdrawal") and can(g.user, "money"):
                for w in db.execute("SELECT w.id, w.amount, w.status, u.email FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.id=?", (num,)):
                    hit("Withdrawals", f"Withdrawal #{w['id']} · £{w['amount'] / 100:.2f} · {w['status']}", url_for("admin.payouts"), w["email"])
            if kind in ("", "#", "d", "deposit") and can(g.user, "money"):
                for d in db.execute("SELECT d.id, d.amount, d.status, u.email, u.id AS uid FROM deposits d JOIN users u ON u.id=d.user_id "
                                    "WHERE d.id=?", (num,)):
                    hit("Deposits", f"Deposit #{d['id']} · £{d['amount'] / 100:.2f} · {d['status']}", url_for("control.timeline", uid=d["uid"]),
                        d["email"])
            if kind in ("", "#", "case") and can(g.user, "cases"):
                for k in db.execute("SELECT id, topic, status, email FROM cases WHERE id=?", (num,)):
                    hit("Support cases", f"Case #{k['id']} · {k['topic']} · {k['status']}", url_for("control.case_detail", case_id=k["id"]), k["email"])
            for c in db.execute("SELECT id, title, status FROM competitions WHERE id=?", (num,)):
                hit("Competitions", f"{c['title']} (#{c['id']})", url_for("admin.entries", cid=c["id"]), c["status"])
        if low.startswith(("cs_", "pi_")):
            for k in db.execute("SELECT id, user_id, status FROM checkouts WHERE stripe_session_id=? OR payment_intent=?", (q, q)):
                hit("Orders", f"Order #{k['id']} · {k['status']}", url_for("admin.order_detail", cid=k["id"]), q)
            for d in db.execute("SELECT id, user_id, status FROM deposits WHERE stripe_session_id=? OR payment_intent=?", (q, q)):
                hit("Deposits", f"Deposit #{d['id']} · {d['status']}", url_for("control.timeline", uid=d["user_id"]), q)
        if len(q) >= 3 and not low.startswith(("cs_", "pi_")):
            for c in db.execute("SELECT id, title, status, ends_at FROM competitions WHERE title LIKE ? OR slug LIKE ? ORDER BY id DESC LIMIT 20",
                                (f"%{q}%", f"%{q}%")):
                hit("Competitions", c["title"], url_for("admin.entries", cid=c["id"]), f"{c['status']} · closes {c['ends_at'][:10]}")
            if can(g.user, "money") or can(g.user, "audit"):
                for l in db.execute("SELECT l.id, l.amount, l.kind, l.reason, l.ref, l.created_at, u.id AS uid, u.email FROM credit_ledger l "
                                    "JOIN users u ON u.id=l.user_id WHERE l.ref=? OR l.reason LIKE ? ORDER BY l.id DESC LIMIT 20", (q, f"%{q}%")):
                    hit("Wallet transactions", f"{'+' if l['amount'] > 0 else ''}£{l['amount'] / 100:.2f} {l['kind']} · {l['reason'][:60]}",
                        url_for("control.timeline", uid=l["uid"]), f"{l['email']} · {l['created_at'][:10]}")
        total = sum(len(v) for v in groups.values())
        if total == 1 and request.args.get("go", "1") == "1":
            audit(db, "admin.search", None, f"Search {q!r} → 1 result")
            return redirect(next(iter(groups.values()))[0][1])
        audit(db, "admin.search", None, f"Search {q!r} → {total} results")
    order = ["Customers", "Orders", "Tickets", "Competitions", "Withdrawals", "Deposits", "Support cases", "Wallet transactions"]
    return render_template("admin/search.html", q=q, groups=[(k, groups[k]) for k in order if k in groups])


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
    from .services import get_setting, set_setting
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
                           daily=get_setting("daily_report_text"), weekly=get_setting("weekly_report_text"),
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
                 "needs_refund": "Payment needs a refund", "refunded": "Payment refunded by staff", "pending": "Checkout in progress"}.get(k["status"], k["status"])
        ev.append((k["paid_at"] or k["created_at"], "order", f"{label} — order #{k['id']}",
                   f"Card £{k['cash_due'] / 100:.2f} · balance £{(k['credit_used'] + k['cash_used'] + k['deposit_used']) / 100:.2f}"
                   f"{' · promo £%.2f' % (k['promo_discount'] / 100) if k['promo_discount'] else ''} — " + "; ".join(nums),
                   url_for("admin.order_detail", cid=k["id"])))
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
    events = [e if len(e) == 5 else (*e, None) for e in timeline_events(db, uid)]
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


# What the customer actually needed — recorded by staff on every case, so the roadmap comes from evidence.
# key: (label, where the fix usually belongs)
CASE_REASONS = {
    "where_tickets": ("Where are my ticket numbers?", "My tickets page and the order confirmation (screen and email)"),
    "draw_when": ("When / how is the draw?", "Draw date and method on the competition page; result notifications"),
    "cash_credit": ("Cash vs site credit confusion", "Wallet wording and the checkout payment summary"),
    "paid_no_entry": ("Paid but no entries / charged twice", "Checkout confirmation, payment failure messages, reconciliation"),
    "withdrawal": ("Withdrawal timing or problem", "Withdrawal status page and expectations shown before requesting"),
    "prize": ("Prize claim / delivery", "Prize-claim updates and winner notifications"),
    "login": ("Can't log in / password / email confirmation", "Login, password reset and verification emails"),
    "free_entry": ("Free postal entry question", "Free-entry page clarity"),
    "limits": ("Limits, breaks or responsible play", "Responsible play page and limit-change messages"),
    "bug": ("Something broken on the site", "Error log on the Targets page — fix the bug"),
    "complaint": ("Complaint about a decision or result", "Terms, transparency centre, complaints process"),
    "other": ("Other", "Read these cases — maybe a new category is needed"),
}

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
        if f.get("reason") in CASE_REASONS:
            sets["reason"] = f["reason"]
        if sets.get("status") == "resolved" and not (sets.get("reason") or k["reason"]):
            flash("Choose what the customer needed before resolving — it's how we decide what to improve.", "error")
            return redirect(url_for("control.case_detail", case_id=case_id))
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
    backlog_items = db.execute("SELECT id, title FROM backlog WHERE status IN ('open','planned') ORDER BY id DESC LIMIT 100").fetchall()
    linked = db.execute("SELECT b.id, b.title, b.status FROM backlog b JOIN backlog_evidence e ON e.backlog_id=b.id "
                        "WHERE e.source='case' AND e.source_id=?", (case_id,)).fetchall()
    return render_template("admin/case.html", k=k, notes=notes, customer=customer, orders=orders, comp=comp,
                           holder=holder, history=history, lock_minutes=LOCK_MINUTES, reasons=CASE_REASONS,
                           backlog_items=backlog_items, linked=linked, kinds=BACKLOG_KINDS)


@bp.route("/support-insights")
@require("cases")
def support_insights():
    """Phase 5: what customers keep asking, compared with the previous 30 days, and where the fix belongs."""
    db = get_db()
    now = utcnow()
    a, b, c_ = iso(now - timedelta(days=60)), iso(now - timedelta(days=30)), iso(now)

    def counts(since, until):
        return dict(db.execute("SELECT COALESCE(reason, 'untagged'), COUNT(*) FROM cases WHERE created_at>=? AND created_at<? "
                               "GROUP BY 1", (since, until)).fetchall())
    cur, prev = counts(b, c_), counts(a, b)
    orders = _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status='paid' AND paid_at>=?", b)
    rows = sorted(({"key": k, "label": v[0], "fix": v[1], "n": cur.get(k, 0), "prev": prev.get(k, 0),
                    "per100": round(100 * cur.get(k, 0) / orders, 1) if orders else None}
                   for k, v in CASE_REASONS.items()), key=lambda r: -r["n"])
    steps = db.execute("SELECT step, SUM(n) FROM funnel_counts WHERE day>=? GROUP BY step", (b[:10],)).fetchall()
    from .analytics import STEPS
    got = dict(steps)
    funnel = [(label, got.get(key, 0)) for key, label in STEPS if key != "home"]
    return render_template("admin/support_insights.html", rows=rows, untagged=cur.get("untagged", 0), orders=orders,
                           funnel=funnel)


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
                    "AND status IN ('paid','credit_refused','needs_refund','refunded')", month)
    bad = _sum(db, "SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>=? "
                   "AND status IN ('credit_refused','needs_refund','refunded')", month)
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
    # reliability, month by month (docs/TARGETS.md)
    recon = _sum(db, "SELECT COUNT(*) FROM flags WHERE kind IN ('Reconciliation','Payment reversal','Refund failed') AND created_at>=?", month)
    add("Payment reconciliation discrepancies (new, 30 days)", recon, "", "<=", 0, "Stripe mismatches, chargebacks and failed refunds")
    add("Draws blocked or overdue (30 days)", _sum(db, "SELECT COUNT(DISTINCT target) FROM audit_log WHERE action='draw.blocked' AND created_at>=?", month),
        "", "<=", 0, "Draws stopped by the readiness checks")
    slow_w = _sum(db, "SELECT COUNT(*) FROM withdrawals WHERE created_at>=? AND ((done_at IS NOT NULL AND julianday(done_at)-julianday(created_at)>2) "
                      "OR (done_at IS NULL AND julianday('now')-julianday(created_at)>2))", month)
    add("Withdrawals taking more than 48 hours (30 days)", slow_w, "", "<=", 0, "Requested in the last 30 days")
    replies = [((parse_iso(r[1]) - parse_iso(r[0])).total_seconds() / 3600) for r in db.execute(
        "SELECT k.created_at, (SELECT MIN(n.created_at) FROM case_notes n WHERE n.case_id=k.id AND n.kind='reply') FROM cases k "
        "WHERE k.created_at>=?", (month,)) if r[1]]
    from statistics import median as _median
    add("Support first-reply time (median, 30 days)", round(_median(replies), 1) if replies else None, " h", "<=", 24,
        f"{len(replies)} cases answered")
    add("Checkout errors (30 days)", _sum(db, "SELECT COALESCE(SUM(count),0) FROM error_log WHERE last_at>=? AND (endpoint LIKE '%checkout%' "
                                              "OR endpoint LIKE '%basket%' OR endpoint LIKE '%webhook%')", month), "", "<=", 0,
        "Server errors on basket, checkout and payment pages")
    ext = json.loads(get_setting("external_measurements") or "{}")
    for key, label, unit, op, target in EXTERNAL_TARGETS:
        m = ext.get(key) or {}
        add(label, m.get("value"), unit, op, target, f"Recorded {m['at'][:10]} by {m.get('by', 'staff')}" if m else "Not recorded yet")
    return rows


def ops_metrics(db):
    """Business-system health at a glance: today against the last 7 days, plus anything stuck right now."""
    from statistics import median
    from .metrics import stats
    now = utcnow()
    day, week = iso(now - timedelta(hours=24)), iso(now - timedelta(days=7))

    def pay_rate(since, until):
        r = db.execute("SELECT COUNT(*), SUM(status='paid') FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>=? "
                       "AND created_at<?", (since, until)).fetchone()
        return (round(100 * (r[1] or 0) / r[0], 1) if r[0] else None), r[0] or 0

    settled = iso(now - timedelta(minutes=45))
    rate_d, n_d = pay_rate(day, settled)
    rate_w, n_w = pay_rate(week, settled)
    waits = [(parse_iso(r[1]) - parse_iso(r[0])).total_seconds() / 3600 for r in db.execute(
        "SELECT created_at, done_at FROM withdrawals WHERE status='paid' AND done_at>?", (iso(now - timedelta(days=30)),))]
    t, w = stats(db, 1), stats(db, 7)
    return [
        ("Card payments completed (24 h / 7 days)", f"{rate_d if rate_d is not None else '—'}% of {n_d} / {rate_w if rate_w is not None else '—'}% of {n_w}",
         rate_d is None or rate_w is None or rate_d >= rate_w * 0.6),
        ("Checkouts abandoned or failed (24 h)", _sum(db, "SELECT COUNT(*) FROM checkouts WHERE created_at>? AND created_at<? "
                                                      "AND status IN ('expired','credit_refused','needs_refund')", day, settled), True),
        ("Payments waiting for a refund", _sum(db, "SELECT COUNT(*) FROM checkouts WHERE status='needs_refund'")
         + _sum(db, "SELECT COUNT(*) FROM deposits WHERE status='needs_refund'"), None),
        ("Draws overdue (closed > 1 h ago, not drawn)", _sum(db, "SELECT COUNT(*) FROM competitions WHERE status='live' AND game_type='' "
                                                             "AND auto_draw=1 AND ends_at<?", iso(now - timedelta(hours=1))), None),
        ("Emails failed (24 h) / waiting", f"{_sum(db, 'SELECT COUNT(*) FROM notifications WHERE email_status=? AND created_at>?', 'failed', day)}"
         f" / {_sum(db, 'SELECT COUNT(*) FROM notifications WHERE email_status=?', 'queued')}", None),
        ("Withdrawals waiting / oldest", f"{_sum(db, 'SELECT COUNT(*) FROM withdrawals WHERE status IN (?,?)', 'requested', 'processing')}"
         + (lambda o: f" / {o[:10]}" if o else "")(db.execute("SELECT MIN(created_at) FROM withdrawals WHERE status IN ('requested','processing')").fetchone()[0]), None),
        ("Withdrawal time to pay (median, 30 days)", f"{median(waits):.1f} h" if waits else "—", not waits or median(waits) <= 24),
        ("Reconciliation / payment-reversal flags open", _sum(db, "SELECT COUNT(*) FROM flags WHERE status='open' AND kind IN "
                                                          "('Reconciliation','Payment reversal')"), None),
        ("Requests today / 95% faster than", f"{t['requests']:,} / {t['p95_under_ms'] or '—'} ms", t["p95_under_ms"] is None or t["p95_under_ms"] <= 1000),
        ("Server errors today / 7 days", f"{t['errors']} / {w['errors']}", not t["errors"]),
    ]


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
    ops = [(label, value, (value == 0) if ok is None and isinstance(value, int) else ok) for label, value, ok in ops_metrics(db)]
    return render_template("admin/targets.html", rows=target_rows(db), external=EXTERNAL_TARGETS, errors=errors, ops=ops)


@bp.route("/errors/<int:eid>/resolve", methods=["POST"])
@require("audit")
def resolve_error(eid):
    db = get_db()
    db.execute("UPDATE error_log SET resolved_at=? WHERE id=?", (iso(utcnow()), eid))
    audit(db, "error.resolved", f"error:{eid}", "Marked fixed")
    flash("Marked as fixed — it reappears if it happens again.")
    return redirect(url_for("control.targets") + "#errors")


@bp.route("/releases")
@require("audit")
def releases():
    import json
    from .services import get_setting
    db = get_db()
    rows = db.execute("SELECT * FROM releases ORDER BY first_seen DESC LIMIT 50").fetchall()
    current = current_app.config.get("RELEASE")
    integ = json.loads(get_setting("last_integrity") or "null")
    from .jobs import health_checks
    health = health_checks(db)
    return render_template("admin/releases.html", rows=rows, current=current, integ=integ,
                           failing=[c for c in health if not c["ok"]], skipped=get_setting("constraints_skipped"))


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
