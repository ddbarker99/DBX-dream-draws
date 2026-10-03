"""Customer notifications: one record per event (dedupe_key makes retries harmless), shown in the account's
notification centre and, where wanted, emailed through a tracked outbox that retries failures."""
import json

from flask import current_app

from . import mailer
from .db import get_db, iso, utcnow

MAX_TRIES = 5


def notify(user_id, kind, title, body, link=None, dedupe_key=None, email=None, mail=None, db=None):
    """Record a notification. email: address to email (None = in-app only). mail: extra mailer kwargs
    (button, heading, highlight, preheader, subject). Returns the id, or None if this event was already recorded."""
    db = db or get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO notifications (user_id, email, kind, title, body, link, dedupe_key, created_at, email_status, "
        "email_payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, email, kind, title, body, link, dedupe_key, iso(utcnow()), "queued" if email else None,
         json.dumps(mail or {}) if email else None))
    return cur.lastrowid if cur.rowcount else None


def send_one(nid):
    """Try to email one queued notification. Safe to call twice: it only sends rows still queued/failed."""
    db = get_db()
    n = db.execute("SELECT * FROM notifications WHERE id=? AND email_status IN ('queued','failed') AND email_tries<?",
                   (nid, MAX_TRIES)).fetchone()
    if n is None:
        return None
    # claim it, so a parallel worker doesn't send the same email
    if not db.execute("UPDATE notifications SET email_status='sending', email_tries=email_tries+1 WHERE id=? AND "
                      "email_status IN ('queued','failed')", (nid,)).rowcount:
        return None
    extra = json.loads(n["email_payload"] or "{}")
    subject = extra.pop("subject", None) or n["title"]
    if extra.get("button"):
        extra["button"] = tuple(extra["button"])
    try:
        status = mailer.deliver(n["email"], subject, n["body"], **extra)
        db.execute("UPDATE notifications SET email_status=?, sent_at=?, email_error=NULL WHERE id=?",
                   ("sent" if status == "sent" else "not_configured", iso(utcnow()), nid))
        return status
    except Exception as e:   # network, auth, refused — retried by the outbox job
        current_app.logger.exception("Email %s failed", nid)
        db.execute("UPDATE notifications SET email_status='failed', email_error=? WHERE id=?", (str(e)[:300], nid))
        return "failed"


def flush(ids=None):
    """Send queued emails now (after the request's work is saved). With no ids, retries the whole outbox."""
    db = get_db()
    if ids is None:
        ids = [r[0] for r in db.execute("SELECT id FROM notifications WHERE email_status IN ('queued','failed') AND email_tries<? "
                                        "ORDER BY id LIMIT 50", (MAX_TRIES,))]
    return {i: send_one(i) for i in ids if i}


def notify_and_send(*a, **kw):
    nid = notify(*a, **kw)
    if nid and kw.get("email"):
        send_one(nid)
    return nid


def unread_count(user_id):
    return get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user_id,)).fetchone()[0]


def tell(email, subject, body, *, kind, key, user_id=None, link=None, title=None, **mail):
    """Customer email + in-account notification, recorded once per key, sent and tracked (retried if it fails)."""
    from flask import current_app
    if link and link.startswith("/"):
        link_abs = current_app.config["SITE_URL"] + link
    else:
        link_abs = link
    if link_abs and "button" not in mail:
        mail["button"] = ("View in your account", link_abs)
    return notify_and_send(user_id, kind, title or subject, body, link=link, dedupe_key=key, email=email,
                           mail=dict(subject=subject, **mail))
