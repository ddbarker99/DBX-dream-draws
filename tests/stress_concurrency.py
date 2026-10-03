"""Concurrency stress test with separate processes (like gunicorn workers in production), all on one database.

    python tests/stress_concurrency.py [--buyers 24] [--tickets 30]

Each process is its own copy of the app with its own database connection. All buyers wait at a barrier, then
at the same instant:
  - a third try to buy the SAME three numbers (only one may get them),
  - the rest buy lucky dips until the competition is sold out,
  - some double-press Pay.
Afterwards it checks: no ticket number owned twice, never more tickets than the maximum, every paid checkout
has exactly its tickets, no duplicate orders, wallet balances match their history, integrity checks pass.
Exit code 0 = PASS.
"""
import argparse
import multiprocessing as mp
import os
import re
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
CONTESTED = "1,2,3"


def make_app(data_dir):
    os.environ["DATA_DIR"] = data_dir
    from app import create_app
    return create_app({"TESTING": True, "DEMO_PAYMENTS": True, "ADMIN_MFA": False, "POSTAL_ADDRESS": "PO Box 1",
                       "REQUIRE_COMP_IMAGE": False, "SIGNUP_RATE_LIMIT": 100000, "JOBS_ON_REQUESTS": False})


def csrf(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)


def post(c, url, data):
    d = dict(data)
    d["csrf"] = csrf(c)
    return c.post(url, data=d)


def buyer(i, data_dir, slug, barrier, results):
    app = make_app(data_dir)
    c = app.test_client()
    post(c, "/login", {"email": f"buyer{i}@example.com", "password": "supersecret123"})
    if i % 3 == 0:
        post(c, "/basket/add", {"slug": slug, "quantity": "3", "numbers": CONTESTED, "answer": "b"})
    else:
        post(c, "/basket/add", {"slug": slug, "quantity": str(1 + i % 4), "answer": "b"})
    token = csrf(c)
    barrier.wait()
    paid = 0
    for _ in range(2 if i % 5 == 0 else 1):            # some people double-press Pay
        r = c.post("/basket/checkout", data={"csrf": token})
        m = re.search(r"/checkout/(\d+)/demo-pay", r.headers.get("Location", ""))
        if m:
            c.post(f"/checkout/{m.group(1)}/demo-pay", data={"csrf": token})
            paid += 1
    results.put((i, paid))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--buyers", type=int, default=24)
    ap.add_argument("--tickets", type=int, default=30)
    args = ap.parse_args(argv)
    data_dir = tempfile.mkdtemp()
    app = make_app(data_dir)
    admin = app.test_client()
    post(admin, "/signup", {"name": "Admin Person", "email": "admin@example.com", "dob": "1990-01-01",
                            "password": "supersecret123", "agree": "1"})
    app.test_cli_runner().invoke(args=["make-admin", "admin@example.com"])
    post(admin, "/admin/competitions/new", {
        "title": "Stress Prize", "description": "A prize used to test simultaneous buyers.", "ends_at": "2099-01-01T20:00",
        "category": "tech", "ticket_price": "1", "max_tickets": str(args.tickets), "max_per_user": "10", "prize_value": "100",
        "question": "2+2?", "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b"})
    from app.db import _connect
    db = _connect(app.config["DATABASE"])
    cid, slug = db.execute("SELECT id, slug FROM competitions").fetchone()
    post(admin, f"/admin/competitions/{cid}/status", {"action": "publish"})
    for i in range(args.buyers):
        post(app.test_client(), "/signup", {"name": f"Buyer {i}", "email": f"buyer{i}@example.com", "dob": "1990-01-01",
                                            "password": "supersecret123", "agree": "1"})
    ctx = mp.get_context("fork")
    barrier, results = ctx.Barrier(args.buyers), ctx.Queue()
    procs = [ctx.Process(target=buyer, args=(i, data_dir, slug, barrier, results)) for i in range(args.buyers)]
    [p.start() for p in procs]
    [p.join(120) for p in procs]
    crashed = [p.exitcode for p in procs if p.exitcode != 0]
    db = _connect(app.config["DATABASE"])
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]       # noqa: E731
    problems = []
    if crashed:
        problems.append(f"{len(crashed)} buyer process(es) crashed")
    if q("SELECT COUNT(*) FROM (SELECT number FROM tickets WHERE competition_id=? GROUP BY number HAVING COUNT(*)>1)", cid):
        problems.append("a ticket number is owned twice")
    sold = q("SELECT COUNT(*) FROM tickets WHERE competition_id=?", cid)
    if sold > args.tickets:
        problems.append(f"oversold: {sold} > {args.tickets}")
    askers = [f"buyer{i}@example.com" for i in range(args.buyers) if i % 3 == 0]
    contested = db.execute(f"SELECT COUNT(DISTINCT t.user_id) FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.competition_id=? "
                           f"AND t.number IN ({CONTESTED}) AND u.email IN ({','.join('?' * len(askers))})", (cid, *askers)).fetchone()[0]
    if contested > 1:
        problems.append(f"the contested numbers went to {contested} of the people who asked for them")
    if q("SELECT COUNT(*) FROM tickets WHERE status='held'"):
        problems.append("reserved tickets left behind")
    bad = q("SELECT COUNT(*) FROM orders o WHERE o.status='paid' AND o.quantity != (SELECT COUNT(*) FROM tickets t WHERE t.order_id=o.id)")
    if bad:
        problems.append(f"{bad} paid order(s) without exactly their tickets")
    dups = q("SELECT COUNT(*) FROM (SELECT user_id FROM checkouts WHERE status='paid' GROUP BY user_id HAVING COUNT(*)>1)")
    if dups:
        problems.append(f"{dups} customer(s) charged twice for one basket")
    with app.app_context():
        from app.checks import integrity_problems
        from app.db import get_db
        problems += integrity_problems(get_db())
    paid = q("SELECT COUNT(*) FROM checkouts WHERE status='paid'")
    print(f"{args.buyers} buyers in {args.buyers} processes · {sold}/{args.tickets} tickets sold · {paid} paid checkouts · "
          f"contested numbers owned by {contested} buyer")
    print("\n".join(problems) if problems else "PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
