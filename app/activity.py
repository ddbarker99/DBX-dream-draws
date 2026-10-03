"""One chronological history of a customer's account, and a complete copy of their data.

Both are built from the permanent records (orders, tickets, draws, ledgers, claims, withdrawals, audit log) rather than a
separate events table, so they can never disagree with what actually happened."""
import json

from flask import url_for

SECURITY_LABELS = {"account.password_changed": "Password changed", "account.password_reset": "Password reset by email link",
                   "account.sessions_revoked": "Signed out of other devices", "account.email_verified": "Email confirmed",
                   "safer.limits": "Spending limits changed", "safer.break": "Break started", "safer.exclude": "Self-exclusion started"}


def account_activity(db, uid, limit=300):
    """Newest first: (when, icon, text, link or None). Only meaningful events — no page views or marketing."""
    from .services import CLAIM_NAMES
    ev = []
    for k in db.execute("SELECT k.id, k.paid_at, k.created_at, k.status, (SELECT GROUP_CONCAT(c.title, ', ') FROM orders o JOIN "
                        "competitions c ON c.id=o.competition_id WHERE o.checkout_id=k.id) AS titles, (SELECT SUM(quantity) FROM orders o "
                        "WHERE o.checkout_id=k.id) AS n FROM checkouts k WHERE k.user_id=? AND k.status IN ('paid','refunded','needs_refund',"
                        "'credit_refused')", (uid,)):
        if k["status"] == "paid":
            ev.append((k["paid_at"] or k["created_at"], "🎟️", f"Entry confirmed — {k['n'] or 0} ticket{'s' if k['n'] != 1 else ''} in "
                       f"{k['titles'] or 'a competition'}", url_for("public.order_detail", cid=k["id"])))
        else:
            ev.append((k["paid_at"] or k["created_at"], "↩️", f"Order #{k['id']} not completed — payment refunded",
                       url_for("public.order_detail", cid=k["id"])))
    for p in db.execute("SELECT t.created_at, c.title, COUNT(*) AS n FROM tickets t JOIN competitions c ON c.id=t.competition_id "
                        "WHERE t.user_id=? AND t.postal_entry_id IS NOT NULL AND t.status='issued' GROUP BY t.postal_entry_id", (uid,)):
        ev.append((p["created_at"], "✉️", f"Free postal entry accepted — {p['title']}", None))
    for d in db.execute("SELECT c.title, c.slug, c.drawn_at, c.winner_ticket_id, (SELECT number FROM tickets w WHERE w.id=c.winner_ticket_id) "
                        "AS win, (SELECT COUNT(*) FROM tickets x WHERE x.competition_id=c.id AND x.user_id=? AND x.id=c.winner_ticket_id) AS mine "
                        "FROM competitions c WHERE c.status='drawn' AND c.game_type='' AND c.id IN (SELECT competition_id FROM tickets "
                        "WHERE user_id=? AND status='issued')", (uid, uid)):
        ev.append((d["drawn_at"], "🏆" if d["mine"] else "🎲", f"Draw complete — {d['title']}: " +
                   ("you won!" if d["mine"] else f"winning ticket #{d['win']}, not one of yours"),
                   url_for("public.draw_verify", slug=d["slug"])))
    for i in db.execute("SELECT ip.title, ip.won_at, c.title AS comp FROM instant_prizes ip JOIN tickets t ON t.id=ip.ticket_id "
                        "JOIN competitions c ON c.id=ip.competition_id WHERE t.user_id=? AND ip.won_at IS NOT NULL", (uid,)):
        ev.append((i["won_at"], "⚡", f"Instant win — {i['title']} ({i['comp']})", None))
    for e in db.execute("SELECT e.created_at, e.status, pc.id AS claim_id, c.title FROM claim_events e JOIN prize_claims pc ON pc.id=e.claim_id "
                        "JOIN competitions c ON c.id=pc.competition_id WHERE pc.user_id=? AND e.status IS NOT NULL", (uid,)):
        ev.append((e["created_at"], "📦", f"Prize update — {c_name(CLAIM_NAMES, e['status'])} ({e['title']})",
                   url_for("public.prize_claim", claim_id=e["claim_id"])))
    for r in db.execute("SELECT created_at, card_amount, wallet_amount, checkout_id FROM refunds WHERE user_id=?", (uid,)):
        where = " and ".join(x for x in (f"£{r['card_amount'] / 100:.2f} to your card" if r["card_amount"] else "",
                                         f"£{r['wallet_amount'] / 100:.2f} to your wallet" if r["wallet_amount"] else "") if x)
        ev.append((r["created_at"], "↩️", f"Refund — {where or 'processed'}",
                   url_for("public.order_detail", cid=r["checkout_id"]) if r["checkout_id"] else None))
    for w in db.execute("SELECT id, amount, status, created_at, done_at FROM withdrawals WHERE user_id=?", (uid,)):
        link = url_for("public.withdrawal_detail", wid=w["id"])
        ev.append((w["created_at"], "🏦", f"Withdrawal requested — £{w['amount'] / 100:.2f} (DBX{w['id']})", link))
        if w["done_at"]:
            ev.append((w["done_at"], "🏦", f"Withdrawal {'paid' if w['status'] == 'paid' else 'returned to your balance'} — "
                       f"£{w['amount'] / 100:.2f}", link))
    for d in db.execute("SELECT amount, paid_at FROM deposits WHERE user_id=? AND paid_at IS NOT NULL", (uid,)):
        ev.append((d["paid_at"], "💳", f"Funds added — £{d['amount'] / 100:.2f}", None))
    for a in db.execute(f"SELECT created_at, action FROM audit_log WHERE target=? AND action IN ({','.join('?' * len(SECURITY_LABELS))})",
                        (f"user:{uid}", *SECURITY_LABELS)):
        ev.append((a["created_at"], "🔒", SECURITY_LABELS[a["action"]], url_for("public.account", tab="profile")))
    u = db.execute("SELECT created_at FROM users WHERE id=?", (uid,)).fetchone()
    if u:
        ev.append((u["created_at"], "👋", "Account opened", None))
    ev.sort(key=lambda e: e[0] or "", reverse=True)
    return ev[:limit]


def c_name(names, status):
    return names.get(status, status.replace("_", " ").capitalize())


def _rows(db, sql, *args, drop=()):
    return [{k: r[k] for k in r.keys() if k not in drop} for r in db.execute(sql, args)]


def data_export(db, uid):
    """Everything we hold about the customer that they can be given, in one machine-readable file. Passwords, security
    tokens and internal staff notes are left out; bank details are masked."""
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    keep = ("id", "name", "email", "phone", "dob", "created_at", "email_verified", "marketing", "marketing_sms", "reminder_emails",
            "referral_code", "points", "points_lifetime", "daily_limit", "weekly_limit", "monthly_limit", "excluded_until")
    profile = {k: u[k] for k in keep if k in u.keys()}
    withdrawals = _rows(db, "SELECT * FROM withdrawals WHERE user_id=? ORDER BY id", uid)
    for w in withdrawals:
        for k in ("account_number", "sort_code"):
            if w.get(k):
                w[k] = "••••" + str(w[k])[-4:]
    return {
        "generated_note": "Your DBX Dream Draws data. Amounts are in pence; times are UTC.",
        "profile": profile,
        "orders": _rows(db, "SELECT id, status, subtotal, promo_discount, credit_used, cash_used, deposit_used, cash_due, created_at, paid_at "
                            "FROM checkouts WHERE user_id=? ORDER BY id", uid),
        "order_lines": _rows(db, "SELECT o.id, o.checkout_id, c.title AS competition, o.quantity, o.amount, o.status, o.terms_version "
                                 "FROM orders o JOIN competitions c ON c.id=o.competition_id JOIN checkouts k ON k.id=o.checkout_id "
                                 "WHERE k.user_id=? ORDER BY o.id", uid),
        "tickets": _rows(db, "SELECT t.number, c.title AS competition, t.created_at, t.order_id, t.postal_entry_id IS NOT NULL AS free_postal, "
                             "t.revealed_at FROM tickets t JOIN competitions c ON c.id=t.competition_id WHERE t.user_id=? AND t.status='issued' "
                             "ORDER BY t.id", uid),
        "wallet_transactions": _rows(db, "SELECT created_at, kind, amount, reason, ref FROM credit_ledger WHERE user_id=? ORDER BY id", uid),
        "points": _rows(db, "SELECT * FROM points_ledger WHERE user_id=? ORDER BY id", uid, drop=("user_id",)),
        "withdrawals": withdrawals,
        "deposits": _rows(db, "SELECT id, amount, status, refunded, created_at, paid_at FROM deposits WHERE user_id=? ORDER BY id", uid),
        "refunds": _rows(db, "SELECT order_id, checkout_id, method, card_amount, wallet_amount, status, reason, created_at FROM refunds "
                             "WHERE user_id=? ORDER BY id", uid),
        "prizes": _rows(db, "SELECT pc.id, c.title AS competition, pc.status, pc.created_at, pc.updated_at FROM prize_claims pc JOIN "
                            "competitions c ON c.id=pc.competition_id WHERE pc.user_id=? ORDER BY pc.id", uid),
        "support_requests": [dict(k, messages=_rows(db, "SELECT created_at, kind, body FROM case_notes WHERE case_id=? AND kind!='internal' "
                                                        "ORDER BY id", k["id"]))
                             for k in _rows(db, "SELECT id, topic, status, created_at, updated_at FROM cases WHERE user_id=? ORDER BY id", uid)],
        "notifications": _rows(db, "SELECT created_at, kind, title, body, email_status FROM notifications WHERE user_id=? ORDER BY id", uid),
        "sign_ins": _rows(db, "SELECT created_at, last_seen, ip, agent, revoked_at FROM user_sessions WHERE user_id=? ORDER BY created_at", uid),
        "consent_history": _rows(db, "SELECT * FROM consent_log WHERE user_id=? ORDER BY id", uid, drop=("user_id",)),
        "activity": [{"at": a[0], "event": a[2]} for a in account_activity(db, uid, limit=10000)],
    }


def data_export_json(db, uid):
    return json.dumps(data_export(db, uid), indent=2, default=str)
