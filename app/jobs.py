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
    db = get_db()
    n = 0
    for r in db.execute("SELECT w.user_id, c.id, c.title, c.slug, c.ends_at, u.email, u.reminder_emails FROM watchlist w "
                        "JOIN competitions c ON c.id=w.competition_id JOIN users u ON u.id=w.user_id WHERE c.status='live' "
                        "AND w.remind_close=1 AND c.ends_at>? AND c.ends_at<?",
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
    parts = [f"{n} job record(s)" if n else "", f"{s} old device record(s)" if s else "", f"{m} old notification(s)" if m else ""]
    return ", ".join(p for p in parts if p) and "removed " + ", ".join(p for p in parts if p)


FUNCS = {"publish_scheduled": _publish_scheduled, "expire_checkouts": _expire_checkouts, "close_competitions": _close,
         "auto_draws": _draws, "settle_unrevealed": _settle, "email_outbox": _outbox, "flag_rules": _flags,
         "health_alerts": _alerts, "watch_reminders": _watch, "prune": _prune}


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
            if name not in ("prune", "flag_rules", "health_alerts"):
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
    return out
