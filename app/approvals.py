"""Four-eye approval for the highest-impact admin actions.

One compromised or mistaken admin account shouldn't be able to move a large sum, change a published result or hand out
admin access on its own. These actions are recorded as a request; a *different* admin with the right permission approves
(which carries the action out) or rejects it. Requests expire after 48 hours.

Setting `four_eyes`: "auto" (default) — required whenever another admin exists who could approve; "on" — always required
(blocks if nobody else can approve); "off" — never (not recommended)."""
import json
from datetime import timedelta

from .db import get_db, iso, parse_iso, utcnow, write_txn
from .perms import ROLES, can, role_of

KINDS = {
    "wallet_adjust": ("Large wallet adjustment", "money.large"),
    "redraw": ("Redraw a published result", "draws"),
    "admin_access": ("Change admin access", "users.manage"),
}
EXPIRY_HOURS = 48


def _approvers(db, kind, requester_id):
    perm = KINDS[kind][1]
    return [u for u in db.execute("SELECT * FROM users WHERE is_admin=1 AND id!=?", (requester_id,)) if can(u, perm)]


def required(db, kind, requester):
    from .services import get_setting
    mode = get_setting("four_eyes", "auto")
    if mode == "off":
        return False
    if mode == "on":
        return True
    return bool(_approvers(db, kind, requester["id"]))


def request_approval(db, kind, target, payload, summary, reason, staff):
    from .services import audit
    rid = db.execute("INSERT INTO approvals (kind, target, payload, summary, reason, requested_by, requested_at) VALUES (?,?,?,?,?,?,?)",
                     (kind, target, json.dumps(payload), summary[:300], (reason or "")[:500], staff["id"], iso(utcnow()))).lastrowid
    audit(db, "approval.requested", f"approval:{rid}", f"{KINDS[kind][0]}: {summary[:200]}")
    try:
        from flask import current_app, url_for
        from . import mailer
        if not current_app.testing:
            for u in _approvers(db, kind, staff["id"]):
                mailer.send(u["email"], f"Approval needed: {KINDS[kind][0]}",
                            f"{staff['email']} asked for: {summary}\nReason: {reason}\n\nApprove or reject it here: "
                            f"{current_app.config['SITE_URL']}{url_for('control.approvals')}", heading="Approval needed")
    except Exception:
        pass
    return rid


def expire_old(db):
    db.execute("UPDATE approvals SET status='expired', decided_at=? WHERE status='pending' AND requested_at<?",
               (iso(utcnow()), iso(utcnow() - timedelta(hours=EXPIRY_HOURS))))


class ApprovalError(Exception):
    pass


def decide(rid, approve, staff, note=""):
    """Approve (and carry out) or reject a request. The approver must be a different admin with the permission."""
    from .services import audit
    db = get_db()
    expire_old(db)
    a = db.execute("SELECT * FROM approvals WHERE id=?", (rid,)).fetchone()
    if a is None or a["status"] != "pending":
        raise ApprovalError("That request has already been decided or has expired.")
    if a["requested_by"] == staff["id"]:
        raise ApprovalError("You can't approve your own request — another administrator has to.")
    if not can(staff, KINDS[a["kind"]][1]):
        raise ApprovalError(f"Your role ({ROLES[role_of(staff)][0]}) can't approve this.")
    now = iso(utcnow())
    if not approve:
        db.execute("UPDATE approvals SET status='rejected', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                   (staff["id"], now, (note or "")[:300], rid))
        audit(db, "approval.rejected", f"approval:{rid}", f"{a['summary'][:200]} — {note[:100]}")
        return None
    requester = db.execute("SELECT * FROM users WHERE id=?", (a["requested_by"],)).fetchone()
    result = EXECUTORS[a["kind"]](json.loads(a["payload"]), requester, staff)
    db.execute("UPDATE approvals SET status='approved', decided_by=?, decided_at=?, decision_note=?, result=? WHERE id=?",
               (staff["id"], now, (note or "")[:300], str(result)[:300], rid))
    audit(db, "approval.approved", f"approval:{rid}", f"{a['summary'][:200]} — approved by {staff['email']}")
    return result


def _wallet_adjust(p, requester, approver):
    from .services import add_credit, audit, balance
    with write_txn() as db:
        if p["amount"] < 0 and balance(db, p["uid"], p["kind"]) + p["amount"] < 0:
            raise ApprovalError("That would now make their balance negative — reject it and ask for a new request.")
        add_credit(db, p["uid"], p["amount"], p["reason"], f"admin{requester['id']}+{approver['id']}", kind=p["kind"])
        audit(db, "wallet.adjust", f"user:{p['uid']}", f"{p['amount']:+}p {p['kind']}: {p['reason']} (requested by "
              f"{requester['email']}, approved by {approver['email']})")
    return f"{p['amount']:+}p {p['kind']} applied"


def _redraw(p, requester, approver):
    from .services import PurchaseError, redraw
    try:
        n = redraw(p["cid"], f"{p['reason']} (approved by a second administrator)", requester)
    except PurchaseError as e:
        raise ApprovalError(str(e))
    try:
        from .admin import announce_draw
        announce_draw(p["cid"])
    except Exception:
        pass
    return f"new winning ticket #{n}"


def _admin_access(p, requester, approver):
    from .services import audit
    db = get_db()
    if p["role"]:
        db.execute("UPDATE users SET is_admin=1, admin_role=? WHERE id=?", (p["role"], p["uid"]))
    else:
        db.execute("UPDATE users SET is_admin=0 WHERE id=?", (p["uid"],))
    audit(db, "user.admin_access", f"user:{p['uid']}", f"Admin access set to {p['role'] or 'none'} (requested by "
          f"{requester['email']}, approved by {approver['email']})")
    return f"access set to {p['role'] or 'none'}"


EXECUTORS = {"wallet_adjust": _wallet_adjust, "redraw": _redraw, "admin_access": _admin_access}


def pending(db):
    expire_old(db)
    return db.execute("SELECT a.*, u.email AS requester FROM approvals a LEFT JOIN users u ON u.id=a.requested_by "
                      "WHERE a.status='pending' ORDER BY a.id").fetchall()


def age_hours(a):
    return (utcnow() - parse_iso(a["requested_at"])).total_seconds() / 3600
