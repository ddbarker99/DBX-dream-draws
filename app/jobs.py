"""Background jobs and system health.

Every automated process runs through run_job(), which answers: did it run, when, did it succeed, what did
it change, and if it failed, why. Jobs run from the `worker` container (`flask run-jobs --loop`) and, as a
backup, piggy-back on web requests (throttled), so draws still happen if the worker is down.
"""
import os
import shutil
import traceback
from datetime import timedelta

from flask import current_app, url_for

from . import mailer
from .db import get_db, iso, parse_iso, utcnow

# name -> (interval seconds, description)
JOBS = {
    "publish_scheduled": (30, "Put scheduled competitions live"),
    "expire_checkouts": (60, "Release reservations whose 45 minutes are up"),
    "close_competitions": (30, "Freeze final entry lists at closing time"),
    "auto_draws": (60, "Run automatic draws"),
    "settle_unrevealed": (300, "Pay instant-win prizes left unrevealed"),
    "email_outbox": (60, "Send queued and retry failed emails"),
    "flag_rules": (900, "Look for patterns worth reviewing"),
    "health_alerts": (600, "Email staff when a health check fails"),
    "watch_reminders": (1800, "Remind customers about saved competitions closing soon"),
    "integrity": (21600, "Check the data for impossible conditions"),
    "reconcile": (86400, "Match payments and refunds against Stripe"),
    "reports": (1800, "Email the daily operations summary (7am) and weekly management report (Monday 8am)"),
    "prune": (86400, "Tidy old job history"),
}
_last = {}


def run_job(name, fn, force=False):
    """Run fn() if it's due. fn returns a short description of what changed (or None/'' for nothing)."""
    interval = JOBS[name][0]
    now = utcnow()
    if not force and _last.get(name) and (now - _last[name]).total_seconds() < interval:
        return None
    _last[name] = now
    db = get_db()
    row = db.execute("SELECT last_started FROM job_status WHERE job=?", (name,)).fetchone()
    if not force and row and row["last_started"] and (now - parse_iso(row["last_started"])).total_seconds() < interval:
        return None                                    # another worker ran it recently
    db.execute("INSERT INTO job_status (job, last_started) VALUES (?,?) ON CONFLICT(job) DO UPDATE SET last_started=excluded.last_started",
               (name, iso(now)))
    try:
        changed = fn()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        current_app.logger.exception("job %s failed", name)
        db.execute("INSERT INTO job_runs (job, started_at, finished_at, ok, error) VALUES (?,?,?,0,?)",
                   (name, iso(now), iso(utcnow()), (err + "\n" + traceback.format_exc()[-1500:])[:2000]))
        db.execute("UPDATE job_status SET last_error_at=?, last_error=? WHERE job=?", (iso(utcnow()), err[:300], name))
        return False
    db.execute("UPDATE job_status SET last_ok=? WHERE job=?", (iso(utcnow()), name))
    if changed:
        db.execute("INSERT INTO job_runs (job, started_at, finished_at, ok, changed) VALUES (?,?,?,1,?)",
                   (name, iso(now), iso(utcnow()), str(changed)[:2000]))
        db.execute("UPDATE job_status SET last_changed=? WHERE job=?", (str(changed)[:300], name))
    return changed or True


# ---------------- the jobs ----------------

def _publish_scheduled():
    from .services import due_scheduled
    from .admin import announce_live
    launched = due_scheduled(force=True)
    for c in launched:
        announce_live(c)
    return ", ".join(f"#{c['id']} live" for c in launched)


def _expire_checkouts():
    from .db import write_txn
    from .services import cleanup_expired
    db = get_db()
    before = db.execute("SELECT COUNT(*) FROM checkouts WHERE status='expired'").fetchone()[0]
    with write_txn() as w:
        cleanup_expired(w)
    n = db.execute("SELECT COUNT(*) FROM checkouts WHERE status='expired'").fetchone()[0] - before
    return f"{n} reservation(s) released" if n else ""


def _close():
    from .services import due_closures
    ids = due_closures(force=True)
    return ", ".join(f"#{i} closed" for i in ids)


def _draws():
    from .services import due_auto_draws
    from .admin import announce_draw
    drawn = due_auto_draws(force=True)
    for cid in drawn:
        announce_draw(cid)
    return ", ".join(f"#{c} drawn" for c in drawn)


def _settle():
    from .services import settle_unrevealed
    n = settle_unrevealed()
    return f"{n} play(s) settled" if n else ""


def _outbox():
    from .notify import flush
    db = get_db()
    db.execute("UPDATE notifications SET email_status='failed', email_error='Interrupted while sending' "
               "WHERE email_status='sending' AND sent_at IS NULL AND created_at<?", (iso(utcnow() - timedelta(minutes=10)),))
    res = flush()
    sent = sum(1 for v in res.values() if v == "sent")
    failed = sum(1 for v in res.values() if v == "failed")
    return f"{sent} sent, {failed} failed" if res else ""


def _flags():
    from .control import run_flag_rules
    n = run_flag_rules(get_db())
    return f"{n} new flag(s)" if n else ""


def _alerts():
    from .services import get_setting, set_setting
    db = get_db()
    bad = [c for c in health_checks(db) if not c["ok"]]
    sent = []
    for c in bad:
        key = f"alert:{c['key']}"
        last = get_setting(key)
        if last and (utcnow() - parse_iso(last)).total_seconds() < 6 * 3600:
            continue
        set_setting(key, iso(utcnow()))
        to = current_app.config.get("SUPPORT_EMAIL")
        if to:
            mailer.send(to, f"⚠ {current_app.config['SITE_NAME']}: {c['name']}",
                        f"A health check is failing.\n\n{c['name']}: {c['detail']}\n\nSee Admin → System health.",
                        heading="System alert", button=("Open system health", current_app.config["SITE_URL"] + "/admin/health"))
        sent.append(c["name"])
    return ("alerted: " + ", ".join(sent)) if sent else ""


def _watch():
    """Closing-soon reminders, only for competitions a customer saved and asked to be reminded about. Always in
    their account; by email only if they keep reminder emails switched on."""
    from .notify import notify, send_one
    from .routes import unsubscribe_link
    from .flags import state
    if state("watchlist") == "off":
        return ""
    staff_only = " AND u.is_admin=1" if state("watchlist") == "staff" else ""
    db = get_db()
    n = 0
    for r in db.execute("SELECT w.user_id, c.id, c.title, c.slug, c.ends_at, u.email, u.reminder_emails FROM watchlist w "
                        "JOIN competitions c ON c.id=w.competition_id JOIN users u ON u.id=w.user_id WHERE c.status='live' "
                        "AND w.remind_close=1 AND c.ends_at>? AND c.ends_at<?" + staff_only,
                        (iso(utcnow()), iso(utcnow() + timedelta(hours=24)))).fetchall():
        link = url_for("public.competition", slug=r["slug"])
        from . import UK
        closes = parse_iso(r["ends_at"]).astimezone(UK).strftime("%a %d %b at %H:%M")
        nid = notify(r["user_id"], "reminder", f"Closing soon: {r['title']}", f"A competition you asked us to remind you about closes {closes}.",
                     link=link, dedupe_key=f"watch:{r['id']}:{r['user_id']}", email=r["email"] if r["reminder_emails"] else None,
                     mail={"button": ("Take a look", current_app.config["SITE_URL"] + link), "heading": "Closing soon",
                           "subject": f"Closing soon: {r['title']}"})
        if nid:
            n += 1
            if r["reminder_emails"]:
                db.execute("UPDATE notifications SET body=body || ? WHERE id=?",
                           (f"\n\nDon't want reminders by email? Unsubscribe: {unsubscribe_link(r['user_id'])}", nid))
                send_one(nid)
    return f"{n} reminder(s)" if n else ""


def _prune():
    db = get_db()
    n = db.execute("DELETE FROM job_runs WHERE started_at<?", (iso(utcnow() - timedelta(days=90)),)).rowcount
    # Retention (see docs/PRIVACY-DATA-AUDIT.md): device records 90 days after last use, read notifications after a year.
    s = db.execute("DELETE FROM user_sessions WHERE last_seen<?", (iso(utcnow() - timedelta(days=90)),)).rowcount
    m = db.execute("DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at<? AND email_status IS NOT 'failed'",
                   (iso(utcnow() - timedelta(days=365)),)).rowcount
    e = db.execute("DELETE FROM error_log WHERE resolved_at IS NOT NULL AND resolved_at<?", (iso(utcnow() - timedelta(days=90)),)).rowcount
    parts = [f"{n} job record(s)" if n else "", f"{e} resolved error(s)" if e else "", f"{s} old device record(s)" if s else "", f"{m} old notification(s)" if m else ""]
    return ", ".join(p for p in parts if p) and "removed " + ", ".join(p for p in parts if p)


def _integrity():
    import json
    from .checks import integrity_problems
    from .services import set_setting
    problems = integrity_problems(get_db())
    set_setting("last_integrity", json.dumps({"at": iso(utcnow()), "problems": problems[:50]}))
    return f"{len(problems)} problem(s): " + "; ".join(problems[:3]) if problems else ""


def daily_summary(db, now=None):
    """Yesterday and right now, in plain text — the morning email and the copy kept on the Reports page."""
    from .control import work_queues, _sum
    from . import UK
    now = now or utcnow()
    midnight = now.astimezone(UK).replace(hour=0, minute=0, second=0, microsecond=0)
    a, b = iso(midnight - timedelta(days=1)), iso(midnight)
    lines = [f"DBX daily summary — {(midnight - timedelta(days=1)).strftime('%A %d %B %Y')}", ""]
    lines += ["YESTERDAY",
              f"  Paid orders: {_sum(db, 'SELECT COUNT(*) FROM checkouts WHERE status=? AND paid_at>=? AND paid_at<?', 'paid', a, b)}"
              f" · card £{_sum(db, 'SELECT SUM(cash_due) FROM checkouts WHERE status=? AND paid_at>=? AND paid_at<?', 'paid', a, b) / 100:,.2f}",
              f"  Failed / abandoned card checkouts: {_sum(db, 'SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND status IN (?,?,?) AND created_at>=? AND created_at<?', 'expired', 'credit_refused', 'needs_refund', a, b)}",
              f"  Competitions closed: {_sum(db, 'SELECT COUNT(*) FROM competitions WHERE locked_at>=? AND locked_at<?', a, b)}"
              f" · draws completed: {_sum(db, 'SELECT COUNT(*) FROM draws WHERE drawn_at>=? AND drawn_at<?', a, b)}",
              f"  Refunds: {_sum(db, 'SELECT COUNT(*) FROM refunds WHERE created_at>=? AND created_at<?', a, b)} single orders"
              f" · £{_sum(db, 'SELECT SUM(amount) FROM credit_ledger WHERE ref LIKE ? AND created_at>=? AND created_at<?', 'refund-o%', a, b) / 100:,.2f} to wallets",
              f"  New customers: {_sum(db, 'SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?', a, b)}"
              f" · support cases opened: {_sum(db, 'SELECT COUNT(*) FROM cases WHERE created_at>=? AND created_at<?', a, b)}", ""]
    lines.append("WAITING NOW")
    for qu in work_queues(db):
        lines.append(f"  {qu['title']}: {qu['total']}")
        for text, _link, meta, level in qu["rows"]:
            if level != "info":
                lines.append(f"    - {text}" + (f" ({meta})" if meta else ""))
    return "\n".join(lines)


def weekly_summary(db, now=None):
    from .control import business_report, target_rows
    from . import UK
    now = now or utcnow()
    end = now.astimezone(UK).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    cur, prev = business_report(db, start, end), business_report(db, start - timedelta(days=7), start)

    def gbp(p):
        return f"£{p / 100:,.2f}"
    rows = [("Net entry value", "net_entries", gbp), ("Paid with customers' own money", "customer_money", gbp),
            ("Prize costs", "prize_costs", gbp), ("Estimated contribution", "contribution", gbp), ("Refunds of entries", "refunds_entries", gbp),
            ("Paid orders", "orders", str), ("Unique paying customers", "customers", str), ("— new", "new", str),
            ("— returning", "returning", str), ("Sign-ups", "signups", str)]
    lines = [f"DBX weekly report — week ending {(end - timedelta(days=1)).strftime('%d %B %Y')}", "",
             "BUSINESS (this week · previous week; definitions: docs/REPORTING-DEFINITIONS.md)"]
    lines += [f"  {label}: {fmt(cur[k])} · {fmt(prev[k])}" for label, k, fmt in rows]
    lines += ["", "TARGETS"]
    for t in target_rows(db):
        status = "no data" if t["ok"] is None else ("on target" if t["ok"] else "OFF TARGET")
        lines.append(f"  {t['label']}: {t['value'] if t['value'] is not None else '—'}{t['unit']} (goal {t['op']} {t['target']}) — {status}")
    return "\n".join(lines)


def _reports():
    """Send the daily summary after 7am UK and the weekly report on Monday after 8am, once each."""
    from .services import get_setting, set_setting
    from . import UK
    db = get_db()
    local = utcnow().astimezone(UK)
    to = [a.strip() for a in (current_app.config.get("REPORT_EMAILS") or current_app.config.get("SUPPORT_EMAIL") or "").split(",") if a.strip()]
    sent = []
    if local.hour >= 7 and get_setting("daily_report_date") != local.strftime("%Y-%m-%d"):
        text = daily_summary(db)
        set_setting("daily_report_date", local.strftime("%Y-%m-%d"))
        set_setting("daily_report_text", text)
        for addr in to:
            mailer.send(addr, f"{current_app.config['SITE_NAME']} daily summary", text, heading="Daily summary")
        sent.append("daily")
    week = local.strftime("%G-W%V")
    if local.weekday() == 0 and local.hour >= 8 and get_setting("weekly_report_week") != week:
        text = weekly_summary(db)
        set_setting("weekly_report_week", week)
        set_setting("weekly_report_text", text)
        for addr in to:
            mailer.send(addr, f"{current_app.config['SITE_NAME']} weekly report", text, heading="Weekly report")
        sent.append("weekly")
    return ", ".join(sent) + (" sent" if sent else "") if sent else ""


def _reconcile():
    from .reconcile import run
    return run(get_db())


FUNCS = {"publish_scheduled": _publish_scheduled, "expire_checkouts": _expire_checkouts, "close_competitions": _close,
         "auto_draws": _draws, "settle_unrevealed": _settle, "email_outbox": _outbox, "flag_rules": _flags,
         "health_alerts": _alerts, "watch_reminders": _watch, "prune": _prune,
         "integrity": _integrity, "reconcile": _reconcile, "reports": _reports}


def run_all_jobs(force=False, only=None):
    out = {}
    for name in JOBS:
        if only and name not in only:
            continue
        out[name] = run_job(name, FUNCS[name], force=force)
    return out


# ---------------- health ----------------

def health_checks(db):
    """Each check: key, name, ok, detail. Shown in the Control Centre and System health; failures are emailed."""
    now = utcnow()
    out = []

    def add(key, name, ok, detail):
        out.append({"key": key, "name": name, "ok": ok, "detail": detail})

    since = iso(now - timedelta(hours=2))
    tried = db.execute("SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>? AND created_at<?",
                       (since, iso(now - timedelta(minutes=45)))).fetchone()[0]
    failed = db.execute("SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>? AND created_at<? "
                        "AND status!='paid'", (since, iso(now - timedelta(minutes=45)))).fetchone()[0]
    add("payments", "Card payments", not (tried >= 6 and failed / tried >= 0.7),
        f"{tried - failed} of {tried} card checkouts completed in the last 2 hours" if tried else "No card checkouts in the last 2 hours")
    bad_mail = db.execute("SELECT COUNT(*) FROM notifications WHERE email_status='failed' AND created_at>?",
                          (iso(now - timedelta(hours=1)),)).fetchone()[0]
    stuck_mail = db.execute("SELECT COUNT(*) FROM notifications WHERE email_status='queued' AND created_at<?",
                            (iso(now - timedelta(minutes=15)),)).fetchone()[0]
    smtp = bool(current_app.config.get("SMTP_HOST"))
    add("email", "Email sending", smtp and not bad_mail and not stuck_mail,
        "SMTP isn't configured — emails aren't being sent" if not smtp else
        f"{bad_mail} failed in the last hour, {stuck_mail} waiting over 15 minutes" if (bad_mail or stuck_mail) else "Sending normally")
    late = db.execute("SELECT COUNT(*) FROM competitions WHERE status='live' AND game_type='' AND auto_draw=1 AND ends_at<?",
                      (iso(now - timedelta(hours=1)),)).fetchone()[0]
    add("draws", "Automatic draws", not late,
        f"{late} automatic draw(s) more than an hour overdue — check postal entries and job errors" if late else "No draws overdue")
    stale = []
    for name, (interval, _) in JOBS.items():
        r = db.execute("SELECT * FROM job_status WHERE job=?", (name,)).fetchone()
        if r is None or not r["last_started"] or (now - parse_iso(r["last_started"])).total_seconds() > max(interval * 3, 600):
            if name not in ("prune", "flag_rules", "health_alerts", "integrity", "reconcile", "reports"):
                stale.append(name)
        elif r["last_error_at"] and (not r["last_ok"] or r["last_error_at"] > r["last_ok"]):
            stale.append(f"{name} (failing: {r['last_error']})")
    add("jobs", "Background jobs", not stale, ("Not running or failing: " + ", ".join(stale)) if stale else "All running")
    old_w = db.execute("SELECT COUNT(*) FROM withdrawals WHERE status IN ('requested','processing') AND created_at<?",
                       (iso(now - timedelta(hours=48)),)).fetchone()[0]
    add("withdrawals", "Withdrawals", not old_w, f"{old_w} withdrawal(s) waiting more than 48 hours" if old_w else "None stuck")
    path = os.path.dirname(current_app.config["DATABASE"])
    du = shutil.disk_usage(path)
    free_pct = 100 * du.free / du.total
    size = os.path.getsize(current_app.config["DATABASE"]) / 1e6
    add("storage", "Disk space", free_pct >= 10 and du.free > 1e9,
        f"{du.free / 1e9:.1f} GB free ({free_pct:.0f}%), database {size:.1f} MB")
    try:
        db.execute("SELECT 1").fetchone()
        ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except Exception as e:     # pragma: no cover
        ok, _ = False, e
    add("database", "Database", ok, "Integrity check passed" if ok else "Integrity check FAILED — restore from backup")
    from .services import get_setting
    last_backup = get_setting("last_backup_verified")
    add("backups", "Backups", bool(last_backup) and (now - parse_iso(last_backup)).total_seconds() < 2 * 86400,
        f"Last backup restored and verified {last_backup}" if last_backup else "No verified backup recorded — run ./backup.sh")
    pay_ok = bool(current_app.config.get("STRIPE_SECRET_KEY")) or current_app.config.get("DEMO_PAYMENTS")
    add("stripe", "Payment provider", bool(pay_ok), "Configured" if pay_ok else "No Stripe key — payments are switched off")
    import json
    integ = json.loads(get_setting("last_integrity") or "null")
    if integ is None:
        add("integrity", "Data integrity", True, "Not checked yet — runs every 6 hours")
    else:
        add("integrity", "Data integrity", not integ["problems"],
            ("; ".join(integ["problems"][:3]) + (" …" if len(integ["problems"]) > 3 else "")) if integ["problems"]
            else f"No problems found (checked {integ['at']})")
    if current_app.config.get("STRIPE_SECRET_KEY") and not current_app.config.get("DEMO_PAYMENTS"):
        rec = json.loads(get_setting("last_reconciliation") or "null")
        open_rec = db.execute("SELECT COUNT(*) FROM flags WHERE kind='Reconciliation' AND status='open'").fetchone()[0]
        fresh = rec and (now - parse_iso(rec["at"])).total_seconds() < 2 * 86400
        add("reconcile", "Stripe reconciliation", bool(fresh) and not open_rec,
            f"{open_rec} mismatch(es) waiting in Flags" if open_rec else
            (f"Matched {rec['sessions']} payments and {rec['refunds']} refunds ({rec['at']})" if fresh else "Hasn't run in 2 days"))
    drill = json.loads(get_setting("last_dr_drill") or "null")
    if drill is None:
        add("dr", "Disaster-recovery drill", True, "Not run yet — run `flask dr-drill` once a quarter (docs/DISASTER-RECOVERY.md)")
    else:
        age = (now - parse_iso(drill["at"])).days
        add("dr", "Disaster-recovery drill", drill["ok"] and age <= 100,
            f"Last drill {'passed' if drill['ok'] else 'FAILED'} {age} day(s) ago ({drill['backup']}, restored in {drill['seconds']} s)"
            + (" — due again" if age > 100 else ""))
    errs = db.execute("SELECT COUNT(*), COALESCE(SUM(count),0) FROM error_log WHERE resolved_at IS NULL AND last_at>?",
                      (iso(now - timedelta(hours=24)),)).fetchone()
    add("errors", "Server errors", not errs[0], f"{errs[0]} different error(s), {errs[1]} time(s) in 24 hours — see Admin → Targets"
        if errs[0] else "None in the last 24 hours")
    blocked = db.execute("SELECT COUNT(DISTINCT a.target) FROM audit_log a JOIN competitions c ON 'comp:' || c.id = a.target "
                         "WHERE a.action='draw.blocked' AND a.created_at>? AND c.status='live'", (iso(now - timedelta(hours=24)),)).fetchone()[0]
    add("draw_blocked", "Draw safety checks", not blocked,
        f"{blocked} draw(s) blocked by the readiness checks — see the competition page and RUNBOOK §9" if blocked else "No draws blocked")
    from .metrics import stats
    today = stats(db, 1)
    slow = today["p95_under_ms"] is not None and today["requests"] >= 200 and today["p95_under_ms"] > 1000
    erring = today["error_rate"] is not None and today["requests"] >= 200 and today["error_rate"] > 1
    add("speed", "Site speed & errors", not (slow or erring),
        (f"Slow: 95% of pages under {today['p95_under_ms']} ms today" if slow else "")
        + (f"{' · ' if slow else ''}{today['error_rate']:.1f}% of requests failing today" if erring else "")
        if (slow or erring) else (f"{today['requests']:,} requests today, 95% under {today['p95_under_ms']} ms" if today["requests"]
                                  else "No traffic measured yet today"))
    db_errs = db.execute("SELECT COALESCE(SUM(count),0) FROM error_log WHERE last_at>? AND (error LIKE 'OperationalError%' "
                         "OR error LIKE 'DatabaseError%' OR error LIKE 'IntegrityError%')", (iso(now - timedelta(hours=1)),)).fetchone()[0]
    add("db_errors", "Database errors", db_errs < 5, f"{db_errs} database error(s) in the last hour" if db_errs else "None in the last hour")
    pay_drop = _payment_drop(db, now)
    add("payment_rate", "Payment success rate", not pay_drop, pay_drop or "Normal compared with the last 7 days")
    odd = anomalies(db, now)
    add("anomalies", "Unusual activity", not odd, "; ".join(odd) if odd else "Nothing unusual in the last 24 hours")
    return out


def _payment_drop(db, now):
    """Card checkouts completing in the last 6 hours vs the previous 7 days. Returns a message if it's unusually low."""
    def rate(a, b):
        r = db.execute("SELECT COUNT(*), SUM(status='paid') FROM checkouts WHERE stripe_session_id IS NOT NULL AND created_at>=? "
                       "AND created_at<?", (iso(a), iso(b))).fetchone()
        return (r[0] or 0), (r[1] or 0)
    settled = now - timedelta(minutes=45)                 # newer checkouts may still be paying
    n, ok = rate(now - timedelta(hours=6), settled)
    bn, bok = rate(now - timedelta(days=7), now - timedelta(hours=6))
    if n < 10 or bn < 30 or not bok:
        return None
    recent, base = ok / n, bok / bn
    if recent < base * 0.6:
        return f"Only {recent:.0%} of card checkouts completed in the last 6 hours (usually {base:.0%}) — check Stripe and the checkout page"
    return None


def anomalies(db, now=None):
    """Spikes worth a human look: refunds, withdrawals, sign-ups, prize wins."""
    now = now or utcnow()
    day, hour, month = iso(now - timedelta(hours=24)), iso(now - timedelta(hours=1)), iso(now - timedelta(days=30))
    out = []
    paid = db.execute("SELECT COUNT(*) FROM checkouts WHERE stripe_session_id IS NOT NULL AND status='paid' AND created_at>?", (day,)).fetchone()[0]
    refunded = db.execute("SELECT COUNT(*) FROM checkouts WHERE status IN ('credit_refused','needs_refund') AND created_at>?", (day,)).fetchone()[0]
    if refunded >= 5 and refunded >= 0.2 * (paid + refunded):
        out.append(f"{refunded} payments refunded or awaiting refund in 24 hours (vs {paid} kept)")
    w_day = db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE created_at>?", (day,)).fetchone()[0]
    w_avg = db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE created_at>? AND created_at<=?", (month, day)).fetchone()[0] / 29
    if w_day >= 50000 and w_day > 3 * w_avg:
        out.append(f"Withdrawal requests of £{w_day / 100:,.0f} in 24 hours (daily average £{w_avg / 100:,.0f})")
    signups = db.execute("SELECT COUNT(*) FROM users WHERE created_at>?", (hour,)).fetchone()[0]
    if signups >= 50:
        out.append(f"{signups} sign-ups in the last hour")
    wins = db.execute("SELECT COALESCE(SUM(value),0) FROM instant_prizes WHERE won_at>?", (day,)).fetchone()[0]
    sales = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND paid_at>?", (day,)).fetchone()[0]
    if wins >= 50000 and wins > 2 * sales:
        out.append(f"Instant prizes worth £{wins / 100:,.0f} won in 24 hours against £{sales / 100:,.0f} of sales")
    return out
