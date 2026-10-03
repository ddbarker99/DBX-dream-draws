"""Role-based admin permissions.

Each admin account has one role. A role is a set of permissions; every admin view declares the
permission it needs. Keep this table as the single source of truth — templates ask `can('...')`.
"""
from functools import wraps

from flask import abort, flash, g, redirect, url_for

PERMISSIONS = {
    "comps.view": "See competitions, entries and draw records",
    "comps": "Create, edit, publish, cancel and delete competitions and games",
    "draws": "Run draws and redraws",
    "postal": "Receive and process postal entries",
    "prizes": "Manage winners, prize claims and physical prize fulfilment",
    "money": "Withdrawals, refunds, wallet adjustments and promo codes",
    "money.large": "Wallet adjustments above the large-adjustment limit",
    "reports": "Revenue, reconciliation and performance reports",
    "users.view": "Look up customers and their timelines",
    "users.verify": "Mark a customer's email as verified",
    "users.manage": "Give or remove admin access",
    "cases": "Support cases",
    "flags": "Review fraud and abuse flags",
    "settings": "Site settings, status and maintenance mode",
    "reset": "Wipe test data",
    "audit": "Read the audit log and system health",
}

ROLES = {
    "support": ("Support", {"comps.view", "postal", "users.view", "users.verify", "cases", "flags"}),
    "competitions": ("Competition manager", {"comps.view", "comps", "draws", "postal", "prizes", "cases", "audit"}),
    "finance": ("Finance", {"comps.view", "money", "reports", "users.view", "flags", "audit"}),
    "admin": ("Administrator", set(PERMISSIONS)),
}
ROLE_ALIASES = {"owner": "admin", "staff": "competitions"}   # roles used before v9


def role_of(user):
    if not user or not user["is_admin"]:
        return None
    r = user["admin_role"] or "admin"
    return ROLE_ALIASES.get(r, r) if ROLE_ALIASES.get(r, r) in ROLES else "support"


def can(user, perm):
    r = role_of(user)
    return bool(r) and perm in ROLES[r][1]


def require(perm):
    """Admins without the permission are told so; everyone else gets a 404 (admin pages stay hidden)."""
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if g.user is None or not g.user["is_admin"]:
                abort(404)
            if not can(g.user, perm):
                flash(f"Your role ({ROLES[role_of(g.user)][0]}) can't do that. Ask an administrator.", "error")
                return redirect(url_for("control.centre"))
            return view(*a, **kw)
        return wrapped
    return deco
