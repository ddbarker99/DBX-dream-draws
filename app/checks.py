"""Automatic checks: can this competition go live, is it ready to draw, and is the data consistent?

Each check is a dict: {"label", "ok", "detail", "blocking"}. Blocking checks stop the action; the rest are
warnings shown to staff.
"""
import json

from flask import current_app

from .db import parse_iso, utcnow


def _c(label, ok, detail="", blocking=True):
    return {"label": label, "ok": bool(ok), "detail": detail, "blocking": blocking}


def launch_checks(db, comp):
    """Everything a competition needs before customers can see it."""
    cfg = current_app.config
    draw = not comp["game_type"]
    free_daily = bool(comp["free_daily"])
    out = [
        _c("Title", bool((comp["title"] or "").strip()), "Give it a clear name."),
        _c("Prize description", not draw or len((comp["description"] or "").strip()) >= 20,
           "Describe the prize (at least a sentence): what it is, condition, delivery."),
        _c("Prize image", not draw or comp["image"] or not cfg.get("REQUIRE_COMP_IMAGE", True),
           "Upload a photo of the prize."),
        _c("Entry price", free_daily or comp["ticket_price"] >= 1, "Set a price per entry."),
        _c("Maximum entries", comp["max_tickets"] >= 1, "Set the total number of entries."),
        _c("Per-person limit", comp["max_per_user"] >= 1, "Set how many entries one person may have (at least 1)."),
        _c("Closing / draw time in the future", parse_iso(comp["ends_at"]) > utcnow(), "Edit the closing date."),
    ]
    if comp["starts_at"]:
        out.append(_c("Go-live time before closing", comp["starts_at"] < comp["ends_at"], "The go-live time must be before closing."))
    if draw:
        out.append(_c("Prize value", comp["prize_value"] > 0, "Enter the prize's value (used for reporting and odds).", blocking=False))
    if comp["question_mode"] != "none" and not free_daily:
        out.append(_c("Entry question", all((comp[k] or "").strip() not in ("", "-") for k in ("question", "answer_a", "answer_b", "answer_c"))
                      and comp["correct"] in ("a", "b", "c"), "Fill in the question, three answers and the correct one."))
    n_prizes = db.execute("SELECT COUNT(*) FROM instant_prizes WHERE competition_id=?", (comp["id"],)).fetchone()[0]
    if comp["game_type"]:
        out.append(_c("Game prizes", n_prizes > 0, "Add the prize table for this game."))
    if n_prizes:
        out.append(_c("Instant prizes fit the entries", n_prizes <= comp["max_tickets"], "More instant prizes than entries."))
        out.append(_c("Instant prizes sealed", bool(comp["instant_hash"]), "Prize numbers aren't sealed — re-add them."))
    if not free_daily:
        out.append(_c("Free postal entry address", bool(cfg.get("POSTAL_ADDRESS")),
                      "Set POSTAL_ADDRESS in .env — every paid competition must offer a free entry route."))
        out.append(_c("Payments configured", bool(cfg.get("STRIPE_SECRET_KEY")) or cfg.get("DEMO_PAYMENTS"),
                      "Add your Stripe keys (or test mode) before taking entries."))
    from .mechanics import name as mech_name, unsigned
    missing = unsigned(db, comp)
    out.append(_c("Mechanic signed off for compliance", not missing,
                  "Not signed off yet: " + "; ".join(mech_name(m) for m in missing) + ". Someone responsible for the legal side must "
                  "sign it off in Admin → More → Compliance sign-off."))
    out.append(_c("Company details for the terms", bool(cfg.get("COMPANY_DETAILS")), "Set COMPANY_DETAILS in .env.", blocking=False))
    out.append(_c("Support email", bool(cfg.get("SUPPORT_EMAIL")), "Set SUPPORT_EMAIL in .env.", blocking=False))
    return out


def blocking_problem(checks):
    bad = next((c for c in checks if c["blocking"] and not c["ok"]), None)
    return f"{bad['label']}: {bad['detail']}" if bad else None


def draw_checks(db, comp):
    """Is it safe to draw? Run before every draw (manual or automatic)."""
    cid = comp["id"]
    held = db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='held'", (cid,)).fetchone()[0]
    pending = db.execute("SELECT COUNT(DISTINCT checkout_id) FROM orders WHERE competition_id=? AND status='pending'", (cid,)).fetchone()[0]
    postal = db.execute("SELECT COUNT(*) FROM postal_entries WHERE competition_id=? AND status='received'", (cid,)).fetchone()[0]
    snap = db.execute("SELECT * FROM entry_snapshots WHERE competition_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    live = [r[0] for r in db.execute("SELECT number FROM tickets WHERE competition_id=? AND status='issued' ORDER BY number", (cid,))]
    from .services import entries_digest
    unpaid = db.execute("SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id WHERE t.competition_id=? AND t.status='issued' "
                        "AND o.status!='paid'", (cid,)).fetchone()[0]
    ownerless = db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='issued' AND user_id IS NULL "
                           "AND postal_entry_id IS NULL AND order_id IS NULL", (cid,)).fetchone()[0]
    accepted_postal = db.execute("SELECT COUNT(*) FROM postal_entries WHERE competition_id=? AND status='accepted'", (cid,)).fetchone()[0]
    postal_tickets = db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND postal_entry_id IS NOT NULL AND status='issued'",
                                (cid,)).fetchone()[0]
    over = db.execute("SELECT COUNT(*) FROM (SELECT user_id FROM tickets WHERE competition_id=? AND status='issued' AND user_id IS NOT NULL "
                      "GROUP BY user_id HAVING COUNT(*)>?)", (cid, comp["max_per_user"])).fetchone()[0]
    refund_due = db.execute("SELECT COUNT(DISTINCT k.id) FROM checkouts k JOIN orders o ON o.checkout_id=k.id WHERE o.competition_id=? "
                            "AND k.status='needs_refund'", (cid,)).fetchone()[0]
    return [
        _c("Closing time has passed", parse_iso(comp["ends_at"]) <= utcnow(), "The draw can't run before the advertised time."),
        _c("No reservations still open", held == 0, f"{held} reserved ticket(s) in {pending} checkout(s) still being paid."),
        _c("Every postal entry processed", postal == 0, f"{postal} envelope(s) waiting in the postal queue."),
        _c("Valid free entries included", accepted_postal == postal_tickets,
           f"{accepted_postal} accepted postal entries but {postal_tickets} postal tickets."),
        _c("Final entry list frozen", bool(snap), "The entry list freezes automatically once the checks above pass."),
        _c("Frozen list matches the tickets", bool(snap) and entries_digest(live) == snap["entries_hash"],
           "The tickets differ from the frozen list — do not draw; see the runbook."),
        _c("Every paid ticket has a paid order", unpaid == 0, f"{unpaid} ticket(s) belong to orders that aren't paid."),
        _c("Every ticket has an owner", ownerless == 0, f"{ownerless} ticket(s) with no customer, order or postal entry."),
        _c("At least one entry", len(live) > 0, "Nobody entered — cancel instead of drawing."),
        _c("Per-person limit respected", over == 0, f"{over} customer(s) hold more than {comp['max_per_user']} tickets — review before drawing.",
           blocking=False),
        _c("No payments awaiting refund", refund_due == 0, f"{refund_due} payment(s) for this competition need refunding (their tickets "
           "were never issued, so they're not in the draw).", blocking=False),
    ]


def integrity_problems(db):
    """Conditions that should be impossible. Returns a list of strings (empty = all good)."""
    out = []

    def q(sql, *a):
        return db.execute(sql, a).fetchall()

    for r in q("SELECT competition_id, number, COUNT(*) n FROM tickets GROUP BY competition_id, number HAVING n>1"):
        out.append(f"Ticket #{r['number']} in competition {r['competition_id']} is allocated {r['n']} times")
    for r in q("SELECT t.competition_id, COUNT(*) n FROM tickets t JOIN orders o ON o.id=t.order_id "
               "WHERE t.status='issued' AND o.status!='paid' GROUP BY t.competition_id"):
        out.append(f"{r['n']} issued ticket(s) in competition {r['competition_id']} belong to unpaid orders")
    for r in q("SELECT o.id, o.quantity, (SELECT COUNT(*) FROM tickets t WHERE t.order_id=o.id AND t.status='issued') n FROM orders o "
               "WHERE o.status='paid' AND o.id NOT IN (SELECT order_id FROM refunds) "
               "AND o.quantity != (SELECT COUNT(*) FROM tickets t WHERE t.order_id=o.id AND t.status='issued')"):
        out.append(f"Paid order {r['id']} should have {r['quantity']} tickets but has {r['n']}")
    for r in q("SELECT c.id FROM competitions c LEFT JOIN tickets t ON t.id=c.winner_ticket_id "
               "WHERE c.status='drawn' AND (t.id IS NULL OR t.competition_id!=c.id OR t.status!='issued')"):
        out.append(f"Competition {r['id']} has a winner without a valid entry")
    for r in q("SELECT c.id FROM competitions c WHERE c.status='drawn' AND c.winner_ticket_id IS NOT "
               "(SELECT winning_ticket_id FROM draws d WHERE d.competition_id=c.id ORDER BY d.id DESC LIMIT 1)"):
        out.append(f"Competition {r['id']}'s winner doesn't match its latest draw record")
    for r in q("SELECT user_id, kind, SUM(amount) s FROM credit_ledger GROUP BY user_id, kind HAVING s<0"):
        out.append(f"Customer {r['user_id']} has a negative {r['kind']} balance ({r['s']}p)")
    for r in q("SELECT ref, COUNT(*) n FROM credit_ledger WHERE ref LIKE 'ip%' GROUP BY ref HAVING n>1"):
        out.append(f"Instant prize {r['ref'][2:]} was paid {r['n']} times")
    for r in q("SELECT ref, COUNT(*) n FROM credit_ledger WHERE ref LIKE 'd%' AND ref NOT LIKE 'dr%' GROUP BY ref HAVING n>1"):
        out.append(f"Deposit {r['ref'][1:]} was credited {r['n']} times")
    for r in q("SELECT stripe_session_id, COUNT(*) n FROM checkouts WHERE stripe_session_id IS NOT NULL AND status='paid' "
               "GROUP BY stripe_session_id HAVING n>1"):
        out.append(f"Stripe session {r['stripe_session_id']} fulfilled {r['n']} times")
    for r in q("SELECT t.competition_id, COUNT(*) n FROM tickets t JOIN competitions c ON c.id=t.competition_id "
               "WHERE c.locked_at IS NOT NULL AND t.created_at > c.locked_at GROUP BY t.competition_id"):
        out.append(f"{r['n']} ticket(s) created in competition {r['competition_id']} after it closed")
    for r in q("SELECT ip.id FROM instant_prizes ip JOIN tickets t ON t.id=ip.ticket_id WHERE t.competition_id!=ip.competition_id"):
        out.append(f"Instant prize {r['id']} is linked to a ticket from another competition")
    skipped = db.execute("SELECT value FROM settings WHERE key='constraints_skipped'").fetchone()
    if skipped and skipped[0]:
        out.append(f"Database safety rules not active because existing data breaks them: {skipped[0]} — see docs/DEPLOY.md")
    for r in q("SELECT u.id, u.points, COALESCE(SUM(p.points),0) s FROM users u LEFT JOIN points_ledger p ON p.user_id=u.id "
               "GROUP BY u.id HAVING u.points != s"):
        out.append(f"Customer {r['id']}'s points balance ({r['points']}) doesn't match their points history ({r['s']})")
    for r in q("SELECT r.id FROM refunds r WHERE EXISTS (SELECT 1 FROM tickets t WHERE t.order_id=r.order_id)"):
        out.append(f"Refund {r['id']}: the refunded order still has tickets")
    for r in q("SELECT id FROM refunds WHERE status='card_failed'"):
        out.append(f"Refund {r['id']}: the card refund failed — refund it in Stripe by hand")
    for r in q("SELECT w.id FROM withdrawals w WHERE NOT EXISTS (SELECT 1 FROM credit_ledger l WHERE l.ref='w' || w.id AND l.amount<0)"):
        out.append(f"Withdrawal {r['id']} has no matching wallet entry")
    for r in q("SELECT w.id FROM withdrawals w WHERE w.status='rejected' AND NOT EXISTS "
               "(SELECT 1 FROM credit_ledger l WHERE l.ref='w' || w.id AND l.amount>0)"):
        out.append(f"Returned withdrawal {r['id']} wasn't credited back")
    for r in q("SELECT d.id FROM deposits d WHERE d.status='paid' AND NOT EXISTS (SELECT 1 FROM credit_ledger l WHERE l.ref='d' || d.id)"):
        out.append(f"Paid deposit {r['id']} has no wallet entry")
    for r in q("SELECT ip.id FROM instant_prizes ip WHERE ip.fulfilled=1 AND ip.credit_amount>0 AND NOT EXISTS "
               "(SELECT 1 FROM credit_ledger l WHERE l.ref='ip' || ip.id)"):
        out.append(f"Instant prize {r['id']} is marked paid but no wallet entry exists")
    for r in q("SELECT c.id FROM competitions c WHERE c.status='drawn' AND NOT EXISTS (SELECT 1 FROM entry_snapshots s WHERE s.competition_id=c.id)"):
        out.append(f"Drawn competition {r['id']} has no frozen entry list")
    from .services import verify_audit_chain
    broken = verify_audit_chain(db)
    if broken:
        out.append(f"Audit log chain broken at entry {broken} — the log may have been tampered with")
    return out


def liability(db):
    """Instant-win prizes: what exists, what's been won, paid, still owed and still to be won."""
    rows = []
    for c in db.execute("SELECT * FROM competitions WHERE id IN (SELECT DISTINCT competition_id FROM instant_prizes) "
                        "AND status!='draft' ORDER BY status='live' DESC, ends_at DESC").fetchall():
        r = db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(ip.value),0) total_v, "
            "SUM(ip.ticket_id IS NOT NULL) won, COALESCE(SUM(CASE WHEN ip.ticket_id IS NOT NULL THEN ip.value END),0) won_v, "
            "SUM(ip.ticket_id IS NOT NULL AND ip.fulfilled=1) paid, COALESCE(SUM(CASE WHEN ip.ticket_id IS NOT NULL AND ip.fulfilled=1 THEN ip.value END),0) paid_v, "
            "SUM(ip.ticket_id IS NOT NULL AND ip.fulfilled=0) owed, COALESCE(SUM(CASE WHEN ip.ticket_id IS NOT NULL AND ip.fulfilled=0 THEN ip.value END),0) owed_v, "
            "SUM(ip.ticket_id IS NOT NULL AND t.revealed_at IS NULL) unrevealed, "
            "SUM(ip.ticket_id IS NOT NULL AND ip.fulfilled=0 AND ip.credit_amount=0) physical_owed "
            "FROM instant_prizes ip LEFT JOIN tickets t ON t.id=ip.ticket_id WHERE ip.competition_id=?", (c["id"],)).fetchone()
        rows.append({"c": c, **{k: r[k] or 0 for k in r.keys()}, "left": (r["total"] or 0) - (r["won"] or 0),
                     "left_v": (r["total_v"] or 0) - (r["won_v"] or 0),
                     "by_type": db.execute("SELECT COALESCE(NULLIF(prize_type,''), CASE WHEN credit_amount>0 THEN 'credit' ELSE 'physical' END) k, "
                                           "COUNT(*) n, SUM(value) v, SUM(ticket_id IS NOT NULL) w FROM instant_prizes WHERE competition_id=? "
                                           "GROUP BY k", (c["id"],)).fetchall()})
    return rows


def after_draw_checks(db, comp_id, draw_id):
    """Straight after a draw or redraw: did exactly what should have happened, happen? Returns [(check, ok, note)]."""
    import hashlib
    import hmac
    import json
    d = db.execute("SELECT * FROM draws WHERE id=?", (draw_id,)).fetchone()
    comp = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
    entries = json.loads(d["entries"])
    t = db.execute("SELECT * FROM tickets WHERE id=?", (d["winning_ticket_id"],)).fetchone()
    from .services import entries_digest
    attempt = db.execute("SELECT COUNT(*) FROM draws WHERE competition_id=? AND id<=?", (comp_id, draw_id)).fetchone()[0]
    if d["method"] == "redraw":
        idx = int(hmac.new(comp["seed"].encode(), f"{d['entries_hash']}:redraw:{attempt}".encode(), hashlib.sha256).hexdigest(), 16) \
            % len(entries)
    else:
        idx = int(hmac.new(comp["seed"].encode(), d["entries_hash"].encode(), hashlib.sha256).hexdigest(), 16) % len(entries)
    claims = db.execute("SELECT COUNT(*) FROM prize_claims WHERE draw_id=?", (draw_id,)).fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM prize_claims WHERE competition_id=? AND status!='forfeited'", (comp_id,)).fetchone()[0]
    snap = db.execute("SELECT entries_hash, entry_count FROM entry_snapshots WHERE id=?", (d["snapshot_id"],)).fetchone() \
        if d["snapshot_id"] else None
    seed_ok = hashlib.sha256(comp["seed"].encode()).hexdigest() == comp["seed_hash"]
    checks = [
        ("Competition marked as drawn", comp["status"] == "drawn", comp["status"]),
        ("Winner on the competition matches the draw", comp["winner_ticket_id"] == d["winning_ticket_id"], ""),
        ("Winning ticket is a valid ticket in this competition", bool(t) and t["competition_id"] == comp_id and t["status"] == "issued"
         and t["number"] == d["winning_number"], ""),
        ("Winning ticket was in the frozen entry list", d["winning_number"] in entries, ""),
        ("Entry count matches the list", d["entry_count"] == len(entries), f"{d['entry_count']} vs {len(entries)}"),
        ("Entry list fingerprint matches", entries_digest(entries) == d["entries_hash"], ""),
        ("Result reproduces from the published method", idx == d["winning_index"] and entries[idx] == d["winning_number"],
         f"position {idx}"),
        ("Secret matches the fingerprint published before sales", seed_ok, ""),
        ("Exactly one prize claim created for this draw", claims == 1, f"{claims} created"),
        ("Exactly one active winner for the main prize", active == 1, f"{active} active"),
    ]
    if d["method"] != "redraw":
        checks.append(("Draw used the closing snapshot", bool(snap) and snap["entries_hash"] == d["entries_hash"], ""))
    return checks


def record_draw_audit(db, comp_id, draw_id, phase, checks):
    """checks: draw_checks() dicts (before) or after_draw_checks() tuples (after). Returns True if nothing blocking failed."""
    import json
    from .db import iso
    if checks and isinstance(checks[0], dict):
        rows = [[c["label"], c["ok"], "" if c["ok"] else c["detail"]] for c in checks]
        ok = all(c["ok"] or not c["blocking"] for c in checks)
    else:
        rows = [[name, bool(passed), note] for name, passed, note in checks]
        ok = all(r[1] for r in rows)
    db.execute("INSERT INTO draw_audits (competition_id, draw_id, phase, at, ok, detail) VALUES (?,?,?,?,?,?)",
               (comp_id, draw_id, phase, iso(utcnow()), 1 if ok else 0, json.dumps(rows)))
    return ok
