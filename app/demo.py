"""`flask demo-lifecycle`: run a complete competition on staging with test money — creation, entries, a free postal
entry, closing, the draw, the winner's choice and delivery details, fulfilment — through the real pages and rules.

Refuses to run against a live site: it needs STAGING=1, or no live Stripe key and test payments on.
Everything it creates is named [DEMO] so it's easy to spot and remove (Admin → Start fresh).
"""
import re
import secrets

import click

from . import db as dbmod


def _csrf(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)


def _post(c, url, data=None, **kw):
    d = dict(data or {})
    d["csrf"] = _csrf(c)
    return c.post(url, data=d, **kw)


def run(app, say=click.echo):
    cfg = app.config
    live_key = (cfg.get("STRIPE_SECRET_KEY") or "").startswith("sk_live_")
    if not cfg.get("STAGING") and (live_key or not cfg.get("DEMO_PAYMENTS")):
        raise click.ClickException("This only runs on staging (STAGING=1) or a local copy with DEMO_PAYMENTS=1 — never on the live site.")
    saved = {k: cfg.get(k) for k in ("DEMO_PAYMENTS", "STRIPE_SECRET_KEY", "ADMIN_MFA", "REQUIRE_COMP_IMAGE", "TESTING",
                                       "SIGNUP_RATE_LIMIT", "JOBS_ON_REQUESTS")}
    cfg.update(DEMO_PAYMENTS=True, STRIPE_SECRET_KEY="", ADMIN_MFA=False, REQUIRE_COMP_IMAGE=False, TESTING=True,
               SIGNUP_RATE_LIMIT=10 ** 6, JOBS_ON_REQUESTS=True)
    if not cfg.get("POSTAL_ADDRESS"):
        cfg["POSTAL_ADDRESS"] = "DBX Demo, PO Box 1, Testtown"
    tag = secrets.token_hex(3)
    conn = dbmod._connect(cfg["DATABASE"])
    try:
        staff = app.test_client()
        staff_email = f"demo-staff-{tag}@example.com"
        pw = secrets.token_urlsafe(12)
        _post(staff, "/signup", {"name": "Demo Staff", "email": staff_email, "dob": "1985-01-01", "password": pw, "agree": "1"})
        conn.execute("UPDATE users SET is_admin=1, admin_role='admin', email_verified=1 WHERE email=?", (staff_email,))
        say(f"1. Staff account {staff_email} (password {pw}) — admin for this demo")
        _post(staff, "/admin/competitions/new", {
            "title": f"[DEMO] Games Console Bundle {tag}", "description": "Demonstration prize: a games console with two controllers, delivered free.",
            "ends_at": "2099-01-01T20:00", "category": "tech", "ticket_price": "1.50", "max_tickets": "50", "max_per_user": "10",
            "prize_value": "500", "cash_alternative": "£400", "auto_draw": "1",
            "question": "What is 2 + 2?", "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b",
            "comp_terms": "- Delivery to UK mainland only\n- Cash alternative of £400 available"})
        cid, slug = conn.execute("SELECT id, slug FROM competitions ORDER BY id DESC LIMIT 1").fetchone()
        _post(staff, f"/admin/competitions/{cid}/status", {"action": "publish"})
        say(f"2. Competition created, checked and published: /c/{slug}")
        players = []
        for i, (name, qty) in enumerate((("Alex Demo", 3), ("Sam Demo", 2), ("Jo Demo", 1))):
            c = app.test_client()
            email = f"demo-{i}-{tag}@example.com"
            _post(c, "/signup", {"name": name, "email": email, "dob": "1990-05-05", "password": pw, "agree": "1"})
            conn.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))
            _post(c, "/basket/add", {"slug": slug, "quantity": str(qty), "answer": "b"})
            r = _post(c, "/basket/checkout", {})
            m = re.search(r"/checkout/(\d+)/demo-pay", r.headers.get("Location", ""))
            if m:
                _post(c, f"/checkout/{m.group(1)}/demo-pay")
            players.append((c, email))
        n = conn.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='issued'", (cid,)).fetchone()[0]
        say(f"3. Three customers entered with test payments — {n} tickets issued")
        _post(staff, f"/admin/competitions/{cid}/postal", {"name": "Pat Postal", "email": f"postal-{tag}@example.com",
                                                          "address": "1 Demo Street, Testtown, TE1 1ST", "dob": "1980-02-02",
                                                          "answer_correct": "1", "received": dbmod.utcnow().strftime("%Y-%m-%d")})
        ok = conn.execute("SELECT COUNT(*) FROM postal_entries WHERE competition_id=? AND status='accepted'", (cid,)).fetchone()[0]
        say(f"4. A free postal entry logged and checked against the rules — {'accepted' if ok else 'not accepted'} "
            "(same chance as a paid entry)")
        from datetime import timedelta
        conn.execute("UPDATE competitions SET ends_at=? WHERE id=?", (dbmod.iso(dbmod.utcnow() - timedelta(minutes=2)), cid))
        from . import jobs
        jobs._last.clear()
        conn.execute("DELETE FROM job_status")
        staff.get("/")
        jobs._last.clear()
        conn.execute("DELETE FROM job_status")
        staff.get("/")
        row = conn.execute("SELECT status, winner_ticket_id FROM competitions WHERE id=?", (cid,)).fetchone()
        if row[0] != "drawn":
            raise click.ClickException(f"The draw didn't run (status {row[0]}) — check Admin → Health.")
        win = conn.execute("SELECT t.number, u.email FROM tickets t LEFT JOIN users u ON u.id=t.user_id WHERE t.id=?", (row[1],)).fetchone()
        say(f"5. Closed, entry list frozen and drawn — winning ticket #{win[0]} ({win[1] or 'postal entrant'})")
        claim = conn.execute("SELECT id FROM prize_claims WHERE competition_id=?", (cid,)).fetchone()[0]
        winner_client = next((c for c, e in players if e == win[1]), None)
        if winner_client:
            _post(winner_client, f"/account/prizes/{claim}", {"choice": "prize"})
            _post(winner_client, f"/account/prizes/{claim}", {"name": "Demo Winner", "address": "2 Demo Road, Testtown, TE1 2ST", "phone": ""})
            say("6. The winner chose the prize and gave delivery details on their prize page")
        for st in ("verification", "verified", "chosen", "fulfilment", "delivered"):
            _post(staff, f"/admin/prizes/{claim}", {"status": st, "note": f"Demo: {st}"})
        say("7. Staff moved the prize through verification → chosen → on its way → delivered; the winner was told at each step")
        say(f"\nDone. Look at: /c/{slug} · /results · Admin → Control Centre · the winner's account. Remove with Admin → Start fresh.")
        return cid
    finally:
        conn.close()
        cfg.update(saved)
