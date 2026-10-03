"""Feature flags: switch a feature off, or show it to staff only, without a deploy.

States: "on" (everyone), "staff" (admins only — for testing on the live site), "off" (nobody).
Changes are made in Admin → Settings → Features and recorded in the audit log.
"""
from flask import abort, g

from .db import get_db, iso, utcnow

FEATURES = {
    "support_centre": ("Help & support centre", "Logged-in customers raise and follow support requests in their account. "
                                                "Off: they use the contact form instead."),
    "watchlist": ("Save & remind me", "Customers save competitions and ask for closing / result reminders."),
    "deposits": ("Wallet deposits", "Customers add money to their wallet by card. Off: no new deposits (refunds of unspent "
                                    "deposits still work)."),
    "referrals": ("Refer a friend", "Referral links and rewards. Off: links still sign people up but earn no reward."),
    "search": ("Site search", "The search page and header search button."),
}
STATES = ("on", "staff", "off")


def state(key):
    cache = g.setdefault("_flags", {})
    if key not in cache:
        row = get_db().execute("SELECT state FROM feature_flags WHERE key=?", (key,)).fetchone()
        cache[key] = row["state"] if row else "on"
    return cache[key]


def enabled(key, user=None):
    if key not in FEATURES:
        raise KeyError(key)
    s = state(key)
    if s == "staff":
        user = user if user is not None else g.get("user")
        return bool(user and user["is_admin"])
    return s == "on"


def require_feature(key):
    if not enabled(key):
        abort(404)


def set_state(db, key, new, actor):
    from .services import audit
    if key not in FEATURES or new not in STATES:
        abort(400)
    old = state(key)
    if old == new:
        return False
    db.execute("INSERT INTO feature_flags (key, state, updated_at, updated_by) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "state=excluded.state, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
               (key, new, iso(utcnow()), actor["id"]))
    g.setdefault("_flags", {})[key] = new
    audit(db, "feature.set", f"feature:{key}", f"{FEATURES[key][0]}: {old} → {new}", actor=actor)
    return True


def all_flags(db):
    rows = {r["key"]: r for r in db.execute("SELECT f.*, u.email FROM feature_flags f LEFT JOIN users u ON u.id=f.updated_by")}
    return [{"key": k, "label": v[0], "help": v[1], "state": rows[k]["state"] if k in rows else "on",
             "updated_at": rows[k]["updated_at"] if k in rows else None, "by": rows[k]["email"] if k in rows else None}
            for k, v in FEATURES.items()]
