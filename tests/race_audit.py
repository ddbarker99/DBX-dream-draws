"""Race-condition audit (OWASP: limited inventory, financial transactions, one-time benefits).

    python tests/race_audit.py [--rounds 5] [--only S1,S3a] [--workers 4] [--threads 8]

How it works (same idea as tests/stress_concurrency.py, but over real HTTP):
  - Each scenario gets a fresh data directory and its own real server: gunicorn with several worker
    processes x threads (like production), all sharing one SQLite (WAL) database.
  - Setup (users, competitions, wallet ledger lines) is done in this process, straight into the database
    or over HTTP.
  - The racing requests are fired from separate OS processes (one per "browser tab"), released together by a
    barrier. Where a race is between a web request and a background job / service call (draws, settling
    unrevealed games) the job runs in its own OS process with its own app instance on the same database.
  - "Stripe mode" scenarios run the server with a fake secret key so payments.enabled() is true; the Stripe
    HTTP client (create_checkout / card_funding / refund) is replaced in each server worker by a stub that
    sleeps like a network call and appends every call to a log file, so we can count real-money side effects.
  - After every round: scenario assertions + app.checks.integrity_problems (new problems only are reported).

Nothing under app/ is modified. Exit code 0 = every round of every scenario passed.
"""
import argparse
import hashlib
import hmac
import json
import multiprocessing as mp
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import timedelta

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)

PW = "supersecret123"
WHSEC = "whsec_test"
BASE_CFG = {"TESTING": True, "DEMO_PAYMENTS": True, "ADMIN_MFA": False, "POSTAL_ADDRESS": "PO Box 1",
            "REQUIRE_COMP_IMAGE": False, "SIGNUP_RATE_LIMIT": 100000, "JOBS_ON_REQUESTS": False,
            "REFERRAL_BONUS": 100, "STRIPE_WEBHOOK_SECRET": WHSEC}
STRIPE_CFG = {"DEMO_PAYMENTS": False, "STRIPE_SECRET_KEY": "sk_test_race_audit_fake"}


# =====================================================================================================
# Server side: gunicorn imports this module and calls server_app() in every worker.
# =====================================================================================================

def _stub_log(kind, **kw):
    path = os.environ.get("RACE_STUB_LOG")
    line = json.dumps({"kind": kind, "pid": os.getpid(), "t": time.time(), **kw}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _install_stripe_stub():
    from app import payments

    def create_checkout(checkout_id, amount, description, user_email, success_url, cancel_url, kind="checkout"):
        time.sleep(0.02)
        _stub_log("create_checkout", ref=f"{kind}-{checkout_id}", amount=amount)
        return {"id": f"cs_race_{kind}_{checkout_id}", "url": f"https://checkout.stripe.test/{kind}/{checkout_id}"}

    def card_funding(pi):
        time.sleep(0.05)                       # a real call to Stripe's API
        _stub_log("card_funding", pi=pi)
        return "credit" if str(pi).startswith("pi_credit") else "debit"

    def refund(pi, reason="requested_by_customer", amount=None, why="", key=None):
        """Models Stripe's Idempotency-Key: the first request with a key refunds; repeats return that refund unchanged."""
        time.sleep(0.05)
        if key:
            import hashlib
            kdir = os.path.join(os.environ.get("RACE_STUB_LOG", "/tmp") + ".keys")
            os.makedirs(kdir, exist_ok=True)
            try:
                os.close(os.open(os.path.join(kdir, hashlib.sha256(key.encode()).hexdigest()), os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                _stub_log("refund_replayed", pi=pi, amount=amount, why=why)
                return {"id": f"re_race_replay"}
        _stub_log("refund", pi=pi, amount=amount, why=why)
        return {"id": f"re_race_{time.time_ns()}"}

    payments.create_checkout = create_checkout
    payments.card_funding = card_funding
    payments.refund = refund


def server_app():
    from app import create_app
    cfg = json.loads(os.environ["RACE_CFG"])
    app = create_app(cfg)
    if os.environ.get("RACE_STUB_LOG"):
        _install_stripe_stub()
    return app


# =====================================================================================================
# Harness
# =====================================================================================================

_PW_HASH = None
_COUNTER = [0]


def uniq(prefix):
    _COUNTER[0] += 1
    return f"{prefix}{os.getpid()}x{_COUNTER[0]}"


def pw_hash():
    global _PW_HASH
    if _PW_HASH is None:
        from werkzeug.security import generate_password_hash
        _PW_HASH = generate_password_hash(PW)
    return _PW_HASH


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Env:
    """One scenario: its own data directory, parent-side app (setup + checks) and a real gunicorn server."""

    def __init__(self, name, stripe=False, workers=4, threads=8, extra=None):
        import requests  # noqa: F401  (fail early if missing)
        self.name = name
        self.dir = tempfile.mkdtemp(prefix=f"race_{name}_")
        self.cfg = dict(BASE_CFG)
        if stripe:
            self.cfg.update(STRIPE_CFG)
        self.cfg.update(extra or {})
        self.stripe = stripe
        os.environ["DATA_DIR"] = self.dir
        from app import create_app
        self.app = create_app(self.cfg)
        self.db_path = self.app.config["DATABASE"]
        self.stub_path = os.path.join(self.dir, "stub_calls.jsonl")
        self.log_path = os.path.join(self.dir, "server.log")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, DATA_DIR=self.dir, RACE_CFG=json.dumps(self.cfg), SITE_URL=self.base,
                   SECRET_KEY="race-audit-secret", SECURE_COOKIES="0", PYTHONPATH=ROOT)
        if stripe:
            env["RACE_STUB_LOG"] = self.stub_path
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "--workers", str(workers), "--worker-class", "gthread",
             "--threads", str(threads), "--bind", f"127.0.0.1:{self.port}", "--chdir", TESTS_DIR, "--timeout", "120",
             "--log-level", "warning", "race_audit:server_app()"], env=env, stdout=self.log, stderr=self.log)
        import requests
        for _ in range(300):
            try:
                if requests.get(self.base + "/healthz", timeout=2).status_code < 500:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"server for {name} did not start; see {self.log_path}")
        time.sleep(0.5)            # let every worker boot

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    # ---- database helpers (fresh connection each time, closed straight away) ----
    def conn(self):
        from app.db import _connect
        return _connect(self.db_path)

    def all(self, sql, *a):
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(sql, a).fetchall()]
        finally:
            c.close()

    def val(self, sql, *a):
        c = self.conn()
        try:
            r = c.execute(sql, a).fetchone()
            return r[0] if r else None
        finally:
            c.close()

    def run(self, sql, *a):
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(sql, a)
            c.execute("COMMIT")
            return cur.lastrowid
        finally:
            c.close()

    def service(self, fn, *a, **kw):
        with self.app.app_context():
            return fn(*a, **kw)

    def integrity(self):
        from app.checks import integrity_problems
        from app.db import get_db
        with self.app.app_context():
            return integrity_problems(get_db())

    def log_tracebacks(self):
        try:
            with open(self.log_path, "rb") as f:
                return f.read().decode(errors="replace").count("Traceback")
        except OSError:
            return 0

    def stub_calls(self, kind=None):
        if not os.path.exists(self.stub_path):
            return []
        with open(self.stub_path) as f:
            rows = [json.loads(x) for x in f if x.strip()]
        return [r for r in rows if kind is None or r["kind"] == kind]

    # ---- fixtures ----
    def user(self, prefix="u", verified=1, admin_role=None, referred_by=None, points=0):
        from app.db import iso, utcnow
        import secrets
        email = f"{uniq(prefix)}@example.com"
        uid = self.run("INSERT INTO users (email, name, password_hash, dob, is_admin, admin_role, referral_code, referred_by, "
                       "created_at, email_verified) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       email, "Race Tester", pw_hash(), "1990-01-01", 1 if admin_role else 0, admin_role or "admin",
                       secrets.token_hex(5).upper(), referred_by, iso(utcnow()), verified)
        if points:
            self.add_points(uid, points)
        return uid, email

    def add_points(self, uid, pts):
        from app.db import iso, utcnow
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE users SET points=points+? WHERE id=?", (pts, uid))
            c.execute("INSERT INTO points_ledger (user_id, points, reason, created_at) VALUES (?,?,?,?)",
                      (uid, pts, "race audit opening points", iso(utcnow())))
            c.execute("COMMIT")
        finally:
            c.close()

    def ledger(self, uid, amount, kind, ref=None, reason="race audit opening balance"):
        from app.db import iso, utcnow
        return self.run("INSERT INTO credit_ledger (user_id, amount, reason, ref, created_at, kind) VALUES (?,?,?,?,?,?)",
                        uid, amount, reason, ref or f"admin-race-{uniq('')}", iso(utcnow()), kind)

    def paid_deposit(self, uid, amount, pi=None):
        from app.db import iso, utcnow
        did = self.run("INSERT INTO deposits (user_id, amount, status, payment_intent, created_at, paid_at) "
                       "VALUES (?,?, 'paid', ?, ?, ?)", uid, amount, pi, iso(utcnow()), iso(utcnow()))
        self.ledger(uid, amount, "deposit", ref=f"d{did}", reason=f"Deposit by card #{did}")
        return did

    def comp(self, price=100, max_tickets=100, max_per_user=50, game_type="", free_daily=0, auto_draw=1, days=30):
        from app.db import iso, utcnow
        from app.services import new_seed
        seed, seed_hash = new_seed()
        slug = uniq("race-comp-")
        cid = self.run(
            "INSERT INTO competitions (slug, title, description, ticket_price, max_tickets, max_per_user, ends_at, question, "
            "answer_a, answer_b, answer_c, correct, status, seed, seed_hash, created_at, category, game_type, prize_value, "
            "free_daily, auto_draw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'live', ?,?,?, 'tech', ?, 1000, ?, ?)",
            slug, f"Race {slug}", "A prize used by the race-condition audit.", price, max_tickets, max_per_user,
            iso(utcnow() + timedelta(days=days)), "2+2?", "3", "4", "5", "b", seed, seed_hash, iso(utcnow()),
            game_type, free_daily, auto_draw)
        return cid, slug

    def instant(self, cid, title, value, credit, qty, kind):
        from app.services import add_instant_prizes
        self.service(add_instant_prizes, cid, title, value, credit, qty, kind)


# ---- HTTP helpers (requests) ----

def _session(cookies=None):
    import requests
    s = requests.Session()
    if cookies:
        s.cookies.update(cookies)
    return s


def csrf_of(s, base):
    html = s.get(base + "/login", timeout=60).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(base, email):
    s = _session()
    tok = csrf_of(s, base)
    r = s.post(base + "/login", data={"email": email, "password": PW, "csrf": tok}, allow_redirects=False, timeout=60)
    if r.status_code != 302:
        raise RuntimeError(f"login failed for {email}: {r.status_code}")
    return s, csrf_of(s, base)


def admin_login(base, email):
    s, tok = login(base, email)
    r = s.post(base + "/admin/mfa/confirm?next=/admin/", data={"password": PW, "csrf": tok}, allow_redirects=False, timeout=60)
    if r.status_code != 302:
        raise RuntimeError(f"admin confirm failed for {email}: {r.status_code}")
    return s, csrf_of(s, base)


def post(s, base, path, data, tok, **kw):
    d = dict(data)
    d["csrf"] = tok
    return s.post(base + path, data=d, allow_redirects=False, timeout=90, **kw)


def clone(s, base, tok):
    """A second browser tab: same session cookie, its own copy of it from now on."""
    t = _session(s.cookies.get_dict())
    return t, tok


def basket_add(s, base, tok, slug, qty=1, numbers="", answer="b"):
    r = post(s, base, "/basket/add", {"slug": slug, "quantity": str(qty), "numbers": numbers, "answer": answer}, tok)
    return r


def buy_now(env, email, slug, qty, use_credit=False):
    """Sequential purchase (setup only): login, add, checkout, demo-pay. Returns checkout id."""
    s, tok = login(env.base, email)
    basket_add(s, env.base, tok, slug, qty)
    r = post(s, env.base, "/basket/checkout", {"use_credit": "1"} if use_credit else {}, tok)
    m = re.search(r"/checkout/(\d+)/(demo-pay|done)", r.headers.get("Location", ""))
    if not m:
        raise RuntimeError(f"setup purchase failed: {r.status_code} {r.headers.get('Location')}")
    if m.group(2) == "demo-pay":
        post(s, env.base, f"/checkout/{m.group(1)}/demo-pay", {}, tok)
    return int(m.group(1)), s, tok


def stripe_event(etype, obj, secret=WHSEC.encode()):
    body = json.dumps({"type": etype, "data": {"object": obj}}).encode()
    ts = int(time.time())
    sig = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


# ---- child processes (each one is a separate OS process) ----

def _safe(fn):
    def wrapped(i, barrier, q, *a):
        try:
            res = fn(i, barrier, q, *a)
        except Exception as e:
            res = {"i": i, "exc": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-800:]}
            try:
                barrier.abort()
            except Exception:
                pass
        res = res or {}
        res.setdefault("i", i)
        q.put(res)
    return wrapped


@_safe
def ch_checkout(i, barrier, q, base, cookies, tok, form, pay):
    s = _session(cookies)
    barrier.wait(60)
    r = s.post(base + "/basket/checkout", data={**form, "csrf": tok}, allow_redirects=False, timeout=90)
    loc = r.headers.get("Location", "")
    res = {"status": r.status_code, "loc": loc, "statuses": [r.status_code]}
    m = re.search(r"/checkout/(\d+)/(demo-pay|done)", loc)
    if m:
        res["cid"] = int(m.group(1))
        if m.group(2) == "demo-pay" and pay:
            r2 = s.post(base + f"/checkout/{m.group(1)}/demo-pay", data={"csrf": tok}, allow_redirects=False, timeout=90)
            res["statuses"].append(r2.status_code)
    return res


@_safe
def ch_post(i, barrier, q, base, cookies, path, data):
    s = _session(cookies)
    barrier.wait(60)
    r = s.post(base + path, data=data, allow_redirects=False, timeout=90)
    return {"status": r.status_code, "statuses": [r.status_code], "loc": r.headers.get("Location", ""),
            "body": r.text[:300] if "json" in r.headers.get("Content-Type", "") else ""}


@_safe
def ch_raw(i, barrier, q, base, path, body, headers):
    s = _session()
    barrier.wait(60)
    r = s.post(base + path, data=body, headers=headers, allow_redirects=False, timeout=90)
    return {"status": r.status_code, "statuses": [r.status_code]}


@_safe
def ch_service(i, barrier, q, data_dir, cfg, what, args):
    os.environ["DATA_DIR"] = data_dir
    from app import create_app
    from app import services
    app = create_app(cfg)
    with app.app_context():
        barrier.wait(60)
        try:
            if what == "run_draw":
                out = services.run_draw(*args)
            elif what == "due_auto_draws":
                out = services.due_auto_draws(force=True)
            elif what == "settle_unrevealed":
                out = services.settle_unrevealed(*args)
            else:
                raise ValueError(what)
            return {"ok": True, "out": repr(out), "statuses": []}
        except services.PurchaseError as e:
            return {"ok": False, "err": f"PurchaseError: {e}", "statuses": []}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {e}", "crash": True, "statuses": []}


def fire(jobs, timeout=180):
    """jobs: [(child_fn, args)]. Starts one OS process per job, releases them together, returns their results."""
    ctx = mp.get_context("fork")
    barrier, q = ctx.Barrier(len(jobs)), ctx.Queue()
    procs = [ctx.Process(target=fn, args=(i, barrier, q, *a)) for i, (fn, a) in enumerate(jobs)]
    for p in procs:
        p.start()
    out, deadline = [], time.time() + timeout
    while len(out) < len(procs) and time.time() < deadline:
        try:
            out.append(q.get(timeout=max(0.1, deadline - time.time())))
        except Exception:
            break
    for p in procs:
        p.join(10)
        if p.is_alive():
            p.kill()
    return sorted(out, key=lambda r: r["i"])


def summarise_http(results):
    codes = [c for r in results for c in r.get("statuses", [])]
    return {"5xx": sum(1 for c in codes if c >= 500), "exc": [r["exc"] for r in results if r.get("exc")],
            "crash": [r["err"] for r in results if r.get("crash")]}


# =====================================================================================================
# Scenarios. Each returns (problems:list[str], counts:dict). Setup is done fresh every round.
# =====================================================================================================

def s1_final_tickets(env, N):
    fillers, buyers = 3, 22
    M = N + fillers
    cid, slug = env.comp(price=100, max_tickets=M, max_per_user=N)
    for _ in range(fillers):
        buy_now(env, env.user("fill")[1], slug, 1)
    taken = {r["number"] for r in env.all("SELECT number FROM tickets WHERE competition_id=?", cid)}
    free = sorted(set(range(1, M + 1)) - taken)
    jobs, users, picked = [], [], 0
    for k in range(buyers):
        uid, email = env.user("buy")
        users.append(uid)
        s, tok = login(env.base, email)
        if k % 6 == 1:                       # a few pick the same specific remaining number
            basket_add(s, env.base, tok, slug, 1, numbers=str(free[0]))
            picked += 1
        elif k % 2 == 0:                     # half want the maximum (all that's left)
            basket_add(s, env.base, tok, slug, N)
        else:
            basket_add(s, env.base, tok, slug, 1)
        tabs = 2 if k % 5 == 0 else 1        # double-press / second tab with the same basket
        for _ in range(tabs):
            jobs.append((ch_checkout, (env.base, s.cookies.get_dict(), tok, {}, True)))
    res = fire(jobs)
    p = []
    sold = env.val("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='issued'", cid)
    allt = env.val("SELECT COUNT(*) FROM tickets WHERE competition_id=?", cid)
    if sold != M:
        p.append(f"sold {sold} != max_tickets {M}")
    if allt > M:
        p.append(f"oversold: {allt} ticket rows > {M}")
    dup = env.val("SELECT COUNT(*) FROM (SELECT number FROM tickets WHERE competition_id=? GROUP BY number HAVING COUNT(*)>1)", cid)
    if dup:
        p.append(f"{dup} duplicate ticket numbers")
    over = env.all("SELECT user_id, COUNT(*) n FROM tickets WHERE competition_id=? GROUP BY user_id HAVING n>?", cid, N)
    if over:
        p.append(f"per-person limit {N} exceeded: {over}")
    held = env.val("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='held'", cid)
    if held:
        p.append(f"{held} held tickets left behind")
    bad = env.val("SELECT COUNT(*) FROM orders o WHERE o.competition_id=? AND o.status='paid' AND o.quantity != "
                  "(SELECT COUNT(*) FROM tickets t WHERE t.order_id=o.id AND t.status='issued')", cid)
    if bad:
        p.append(f"{bad} paid orders without exactly their tickets")
    pend = env.val("SELECT COUNT(*) FROM orders WHERE competition_id=? AND status='pending'", cid)
    if pend:
        p.append(f"{pend} orders left pending")
    wrong = env.val("SELECT COUNT(*) FROM checkouts k WHERE k.status='paid' AND k.id IN (SELECT checkout_id FROM orders WHERE "
                    "competition_id=?) AND k.cash_due + k.credit_used + k.cash_used + k.deposit_used + k.promo_discount != k.subtotal",
                    cid)
    if wrong:
        p.append(f"{wrong} paid checkouts whose payment doesn't add up")
    ph = ",".join("?" * len(users))
    failed = env.val(f"SELECT COUNT(*) FROM users u WHERE u.id IN ({ph}) AND NOT EXISTS (SELECT 1 FROM checkouts k WHERE "
                     f"k.user_id=u.id AND k.status='paid') AND (EXISTS (SELECT 1 FROM tickets t WHERE t.user_id=u.id) OR "
                     f"EXISTS (SELECT 1 FROM credit_ledger l WHERE l.user_id=u.id))", *users)
    if failed:
        p.append(f"{failed} unsuccessful buyers still have tickets or wallet movements")
    twice = env.val(f"SELECT COUNT(*) FROM (SELECT user_id FROM checkouts WHERE status='paid' AND user_id IN ({ph}) "
                    f"GROUP BY user_id HAVING COUNT(*)>1)", *users)
    paid = env.val(f"SELECT COUNT(*) FROM checkouts WHERE status='paid' AND user_id IN ({ph})", *users)
    contested = env.val("SELECT COUNT(DISTINCT user_id) FROM tickets WHERE competition_id=? AND number=?", cid, free[0])
    if contested > 1:
        p.append("contested number owned twice")
    return p, {"N": N, "procs": len(jobs), "sold": f"{sold}/{M}", "paid_checkouts": paid, "users_paid_twice": twice,
               **summarise_http(res)}


def s2_two_tabs_wallet(env):
    uid, email = env.user("wallet")
    env.ledger(uid, 200, "credit")
    env.ledger(uid, 200, "cash")
    env.paid_deposit(uid, 100)
    start = 500
    cid, slug = env.comp(price=100, max_tickets=1000, max_per_user=100)
    s, tok = login(env.base, email)
    jobs = []
    for _ in range(6):
        t, _ = clone(s, env.base, tok)
        basket_add(t, env.base, tok, slug, 3)       # £3 each; balance £5 — at most one can be paid fully from the wallet
        jobs.append((ch_checkout, (env.base, t.cookies.get_dict(), tok, {"use_credit": "1"}, True)))
    res = fire(jobs)
    p = []
    bal = {r["kind"]: r["s"] for r in env.all("SELECT kind, SUM(amount) s FROM credit_ledger WHERE user_id=? GROUP BY kind", uid)}
    for k, v in bal.items():
        if v < 0:
            p.append(f"{k} balance negative: {v}")
    used = env.val("SELECT COALESCE(SUM(credit_used + cash_used + deposit_used),0) FROM checkouts WHERE user_id=? AND status IN "
                   "('paid','pending')", uid)
    if used > start:
        p.append(f"wallet spent {used}p > balance {start}p")
    total = sum(bal.values())
    if total != start - used:
        p.append(f"ledger total {total} != {start} - used {used}")
    bad = env.val("SELECT COUNT(*) FROM checkouts WHERE user_id=? AND status='paid' AND "
                  "credit_used + cash_used + deposit_used + cash_due + promo_discount != subtotal", uid)
    if bad:
        p.append(f"{bad} checkouts don't add up")
    paid = env.val("SELECT COUNT(*) FROM checkouts WHERE user_id=? AND status='paid'", uid)
    return p, {"tabs": 6, "paid": paid, "wallet_used": used, "balance_after": bal, **summarise_http(res)}


WITHDRAW = {"step": "confirm", "method": "paypal", "paypal_email": "race@example.com"}


def s3a_withdrawals(env):
    uid, email = env.user("wd")
    env.ledger(uid, 3000, "cash")
    s, tok = login(env.base, email)
    jobs = [(ch_post, (env.base, s.cookies.get_dict(), "/account/withdraw", {**WITHDRAW, "amount": "20", "csrf": tok}))
            for _ in range(8)]
    res = fire(jobs)
    p = []
    n = env.val("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid)
    tot = env.val("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=?", uid)
    cash = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='cash'", uid)
    if tot > 3000:
        p.append(f"withdrew {tot}p > balance 3000p")
    if cash < 0:
        p.append(f"cash balance negative {cash}")
    if cash != 3000 - tot:
        p.append(f"cash {cash} != 3000 - {tot}")
    if n != 1:
        p.append(f"{n} withdrawals of £20 from £30 (expected exactly 1)")
    return p, {"procs": 8, "withdrawals": n, "withdrawn": tot, "cash_after": cash, **summarise_http(res)}


def s3b_withdraw_and_buy(env):
    uid, email = env.user("wdb")
    env.ledger(uid, 1000, "cash")
    cid, slug = env.comp(price=100, max_tickets=1000, max_per_user=100)
    s, tok = login(env.base, email)
    jobs = []
    for k in range(8):
        if k % 2 == 0:
            jobs.append((ch_post, (env.base, s.cookies.get_dict(), "/account/withdraw", {**WITHDRAW, "amount": "8", "csrf": tok})))
        else:
            t, _ = clone(s, env.base, tok)
            basket_add(t, env.base, tok, slug, 8)
            jobs.append((ch_checkout, (env.base, t.cookies.get_dict(), tok, {"use_credit": "1"}, True)))
    res = fire(jobs)
    p = []
    wd = env.val("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=?", uid)
    spent = env.val("SELECT COALESCE(SUM(cash_used),0) FROM checkouts WHERE user_id=? AND status IN ('paid','pending')", uid)
    cash = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='cash'", uid)
    if wd + spent > 1000:
        p.append(f"withdrawn {wd} + spent {spent} > 1000")
    if cash < 0:
        p.append(f"cash negative {cash}")
    if cash != 1000 - wd - spent:
        p.append(f"cash {cash} != 1000 - {wd} - {spent}")
    return p, {"procs": 8, "withdrawn": wd, "cash_spent_on_entries": spent, "cash_after": cash, **summarise_http(res)}


def s4_points(env):
    uid, email = env.user("pts", points=500)
    s, tok = login(env.base, email)
    jobs = [(ch_post, (env.base, s.cookies.get_dict(), "/account/redeem", {"blocks": "3", "csrf": tok})) for _ in range(8)]
    res = fire(jobs)
    p = []
    pts = env.val("SELECT points FROM users WHERE id=?", uid)
    led = env.val("SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE user_id=?", uid)
    credit = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='credit'", uid)
    redeemed = 500 - pts
    if pts < 0:
        p.append(f"points negative: {pts}")
    if pts != led:
        p.append(f"points {pts} != ledger {led}")
    if redeemed > 500:
        p.append(f"redeemed {redeemed} > 500 owned")
    if credit != redeemed:
        p.append(f"credit {credit}p doesn't match {redeemed} points redeemed")
    if redeemed != 300:
        p.append(f"redeemed {redeemed} points (expected exactly one redemption of 300)")
    return p, {"procs": 8, "redeemed_points": redeemed, "credit_given": credit, "points_after": pts, **summarise_http(res)}


def _promo(env, max_uses, per_user, percent=50):
    from app.db import iso, utcnow
    code = uniq("RACE").upper()
    pid = env.run("INSERT INTO promo_codes (code, percent, max_uses, per_user, created_at) VALUES (?,?,?,?,?)",
                  code, percent, max_uses, per_user, iso(utcnow()))
    return pid, code


def s5a_promo_many_users(env):
    pid, code = _promo(env, max_uses=1, per_user=1)
    cid, slug = env.comp(price=200, max_tickets=1000, max_per_user=10)
    jobs = []
    for _ in range(20):
        _, email = env.user("promo")
        s, tok = login(env.base, email)
        basket_add(s, env.base, tok, slug, 1)
        jobs.append((ch_checkout, (env.base, s.cookies.get_dict(), tok, {"promo": code}, True)))
    res = fire(jobs)
    p = []
    used = env.val("SELECT COUNT(*) FROM checkouts WHERE promo_id=? AND status IN ('paid','pending')", pid)
    uses = env.val("SELECT uses FROM promo_codes WHERE id=?", pid)
    if used != 1:
        p.append(f"single-use code used by {used} checkouts")
    if uses != 1:
        p.append(f"promo uses counter = {uses}")
    return p, {"procs": 20, "checkouts_with_code": used, "uses": uses, **summarise_http(res)}


def s5b_promo_same_user(env):
    pid, code = _promo(env, max_uses=100, per_user=1)
    cid, slug = env.comp(price=200, max_tickets=1000, max_per_user=100)
    uid, email = env.user("promo1")
    s, tok = login(env.base, email)
    jobs = []
    for _ in range(6):
        t, _ = clone(s, env.base, tok)
        basket_add(t, env.base, tok, slug, 1)
        jobs.append((ch_checkout, (env.base, t.cookies.get_dict(), tok, {"promo": code}, True)))
    res = fire(jobs)
    used = env.val("SELECT COUNT(*) FROM checkouts WHERE promo_id=? AND user_id=? AND status IN ('paid','pending')", pid, uid)
    p = [] if used == 1 else [f"per_user=1 code used {used} times by one customer"]
    return p, {"tabs": 6, "checkouts_with_code": used, **summarise_http(res)}


def _reserve(env, email, slug, qty, form=None):
    s, tok = login(env.base, email)
    basket_add(s, env.base, tok, slug, qty)
    r = post(s, env.base, "/basket/checkout", form or {}, tok)
    loc = r.headers.get("Location", "")
    m = re.search(r"/checkout/(\d+)/", loc)
    if m:
        return int(m.group(1)), s, tok
    k = env.val("SELECT id FROM checkouts c WHERE c.user_id=(SELECT id FROM users WHERE email=?) ORDER BY id DESC", email)
    if k is None:
        raise RuntimeError(f"reserve failed: {r.status_code} {loc}")
    return k, s, tok


def _fulfil_checks(env, k, uid, expect_tickets, expect_prizes):
    p = []
    st = env.val("SELECT status FROM checkouts WHERE id=?", k)
    if st != "paid":
        p.append(f"checkout status {st}")
    nt = env.val("SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id WHERE o.checkout_id=? AND t.status='issued'", k)
    if nt != expect_tickets:
        p.append(f"{nt} tickets issued (expected {expect_tickets})")
    ip = env.val("SELECT COUNT(*) FROM credit_ledger WHERE user_id=? AND ref LIKE 'ip%'", uid)
    if ip != expect_prizes:
        p.append(f"{ip} instant-prize ledger rows (expected {expect_prizes})")
    pts = env.val("SELECT COUNT(*) FROM points_ledger WHERE ref=?", f"c{k}")
    if pts != 1:
        p.append(f"{pts} points awards for the order (expected 1)")
    orders = env.val("SELECT COUNT(*) FROM orders WHERE checkout_id=? AND status='paid'", k)
    return p, {"tickets": nt, "ip_rows": ip, "points_rows": pts, "paid_orders": orders}


def s6a_duplicate_demo_pay(env):
    cid, slug = env.comp(price=150, max_tickets=20, max_per_user=10)
    env.instant(cid, "50p credit", 50, 50, 20, "credit")       # every number wins → paid at fulfilment
    uid, email = env.user("dup")
    k, s, tok = _reserve(env, email, slug, 2)
    jobs = [(ch_post, (env.base, s.cookies.get_dict(), f"/checkout/{k}/demo-pay", {"csrf": tok})) for _ in range(10)]
    res = fire(jobs)
    p, c = _fulfil_checks(env, k, uid, 2, 2)
    return p, {"procs": 10, **c, **summarise_http(res)}


def s6b_duplicate_webhooks(env):
    cid, slug = env.comp(price=150, max_tickets=20, max_per_user=10)
    env.instant(cid, "50p credit", 50, 50, 20, "credit")
    uid, email = env.user("hook")
    k, s, tok = _reserve(env, email, slug, 2)
    due = env.val("SELECT cash_due FROM checkouts WHERE id=?", k)
    sess = env.val("SELECT stripe_session_id FROM checkouts WHERE id=?", k)
    body, headers = stripe_event("checkout.session.completed", {
        "id": sess, "payment_status": "paid", "amount_total": due, "payment_intent": f"pi_debit_{k}",
        "metadata": {"checkout_id": str(k)}})
    res = fire([(ch_raw, (env.base, "/stripe/webhook", body, headers)) for _ in range(10)])
    p, c = _fulfil_checks(env, k, uid, 2, 2)
    return p, {"procs": 10, "session": sess, **c, **summarise_http(res)}


def s6c_credit_card_webhooks(env):
    cid, slug = env.comp(price=150, max_tickets=100, max_per_user=10)
    uid, email = env.user("ccard")
    k, s, tok = _reserve(env, email, slug, 2)
    due = env.val("SELECT cash_due FROM checkouts WHERE id=?", k)
    sess = env.val("SELECT stripe_session_id FROM checkouts WHERE id=?", k)
    pi = f"pi_credit_{k}_{uniq('')}"
    body, headers = stripe_event("checkout.session.completed", {
        "id": sess, "payment_status": "paid", "amount_total": due, "payment_intent": pi, "metadata": {"checkout_id": str(k)}})
    res = fire([(ch_raw, (env.base, "/stripe/webhook", body, headers)) for _ in range(10)])
    refunds = [r for r in env.stub_calls("refund") if r["pi"] == pi]
    st = env.val("SELECT status FROM checkouts WHERE id=?", k)
    issued = env.val("SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id WHERE o.checkout_id=? AND t.status='issued'", k)
    p = []
    if len(refunds) != 1:
        p.append(f"Stripe refund called {len(refunds)} times for one credit-card payment of {due}p "
                 f"(customer refunded {len(refunds) * due}p of {due}p requested)")
    if st != "credit_refused":
        p.append(f"checkout status {st}")
    if issued:
        p.append(f"{issued} tickets issued for a refused card")
    return p, {"procs": 10, "refund_calls": len(refunds), "status": st, **summarise_http(res)}


def _closed_comp_with_entries(env):
    from app.db import iso, utcnow
    cid, slug = env.comp(price=100, max_tickets=30, max_per_user=10)
    for _ in range(3):
        buy_now(env, env.user("ent")[1], slug, 2)
    env.run("UPDATE competitions SET ends_at=? WHERE id=?", iso(utcnow() - timedelta(minutes=1)), cid)
    return cid, slug


def _admin(env, role="admin"):
    uid, email = env.user("admin", admin_role=role)
    s, tok = admin_login(env.base, email)
    return uid, s, tok


def s7a_concurrent_draw(env):
    cid, slug = _closed_comp_with_entries(env)
    _, s, tok = _admin(env)
    jobs = [(ch_post, (env.base, s.cookies.get_dict(), f"/admin/competitions/{cid}/draw", {"csrf": tok})) for _ in range(6)]
    jobs += [(ch_service, (env.dir, env.cfg, "run_draw", (cid,))) for _ in range(3)]
    jobs += [(ch_service, (env.dir, env.cfg, "due_auto_draws", ())) for _ in range(3)]
    res = fire(jobs)
    p = []
    draws = env.val("SELECT COUNT(*) FROM draws WHERE competition_id=?", cid)
    claims = env.val("SELECT COUNT(*) FROM prize_claims WHERE competition_id=?", cid)
    snaps = env.val("SELECT COUNT(*) FROM entry_snapshots WHERE competition_id=?", cid)
    st = env.val("SELECT status FROM competitions WHERE id=?", cid)
    if draws != 1:
        p.append(f"{draws} draw records")
    if claims != 1:
        p.append(f"{claims} prize claims")
    if snaps != 1:
        p.append(f"{snaps} entry snapshots")
    if st != "drawn":
        p.append(f"status {st}")
    errs = sorted({r.get("err", "")[:70] for r in res if r.get("err")})
    return p, {"procs": len(jobs), "draws": draws, "claims": claims, "snapshots": snaps, "service_errors": errs,
               **summarise_http(res)}


def s7b_double_redraw(env):
    cid, slug = _closed_comp_with_entries(env)
    from app.services import run_draw
    env.service(run_draw, cid, None)
    _, s, tok = _admin(env)
    current = env.val("SELECT MAX(id) FROM draws WHERE competition_id=?", cid)      # what the redraw form carries
    data = {"csrf": tok, "confirm": "REDRAW", "reason": "Winner failed the age verification check", "replaces": str(current)}
    res = fire([(ch_post, (env.base, s.cookies.get_dict(), f"/admin/competitions/{cid}/redraw", data)) for _ in range(6)])
    draws = env.val("SELECT COUNT(*) FROM draws WHERE competition_id=?", cid)
    redraws = draws - 1
    active = env.val("SELECT COUNT(*) FROM prize_claims WHERE competition_id=? AND status!='forfeited'", cid)
    forfeited = env.val("SELECT COUNT(*) FROM prize_claims WHERE competition_id=? AND status='forfeited'", cid)
    p = []
    if redraws != 1:
        p.append(f"one redraw request (double-submitted x6) produced {redraws} redraws; {forfeited} winners forfeited")
    if active != 1:
        p.append(f"{active} active winners")
    return p, {"procs": 6, "redraws": redraws, "forfeited_claims": forfeited, "active_claims": active, **summarise_http(res)}


def s8_free_play(env):
    cid, slug = env.comp(price=0, max_tickets=20000, max_per_user=366, game_type="spin", free_daily=1)
    env.instant(cid, "50p Site Credit", 50, 50, 400, "credit")
    uid, email = env.user("free")
    s, tok = login(env.base, email)
    res = fire([(ch_post, (env.base, s.cookies.get_dict(), f"/free-play/{slug}", {"csrf": tok})) for _ in range(10)])
    n = env.val("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND user_id=?", cid, uid)
    return ([] if n == 1 else [f"{n} free plays claimed today (expected 1)"]), {"procs": 10, "plays": n, **summarise_http(res)}


def _game_play(env):
    cid, slug = env.comp(price=50, max_tickets=10, max_per_user=10, game_type="scratch")
    env.instant(cid, "£1 credit", 100, 100, 10, "credit")     # every play wins £1 credit, paid on reveal
    uid, email = env.user("game")
    k, s, tok = buy_now(env, email, slug, 1)
    tid = env.val("SELECT t.id FROM tickets t JOIN orders o ON o.id=t.order_id WHERE o.checkout_id=?", k)
    pid = env.val("SELECT id FROM instant_prizes WHERE ticket_id=?", tid)
    return cid, uid, s, tok, tid, pid


def _reveal_checks(env, uid, tid, pid):
    paid = env.val("SELECT COUNT(*) FROM credit_ledger WHERE ref=?", f"ip{pid}")
    credit = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='credit'", uid)
    ful = env.val("SELECT fulfilled FROM instant_prizes WHERE id=?", pid)
    rev = env.val("SELECT revealed_at FROM tickets WHERE id=?", tid)
    p = []
    if paid != 1:
        p.append(f"prize paid {paid} times")
    if credit != 100:
        p.append(f"credit balance {credit}p (expected 100p)")
    if not ful or not rev:
        p.append(f"fulfilled={ful} revealed_at={rev}")
    return p, {"prize_ledger_rows": paid, "credit": credit}


def s9a_concurrent_reveal(env):
    cid, uid, s, tok, tid, pid = _game_play(env)
    res = fire([(ch_post, (env.base, s.cookies.get_dict(), f"/play/reveal/{tid}", {"csrf": tok})) for _ in range(10)])
    p, c = _reveal_checks(env, uid, tid, pid)
    firsts = sum(1 for r in res if '"first":true' in r.get("body", "").replace(" ", ""))
    wins = sum(1 for r in res if '"win":true' in r.get("body", "").replace(" ", ""))
    if firsts != 1:
        p.append(f"{firsts} responses said 'first reveal'")
    return p, {"procs": 10, **c, "first_flags": firsts, "win_responses": wins, **summarise_http(res)}


def s9b_reveal_vs_settle(env):
    cid, uid, s, tok, tid, pid = _game_play(env)
    jobs = [(ch_post, (env.base, s.cookies.get_dict(), f"/play/reveal/{tid}", {"csrf": tok})) for _ in range(5)]
    jobs += [(ch_service, (env.dir, env.cfg, "settle_unrevealed", (None, 0))) for _ in range(5)]
    res = fire(jobs)
    p, c = _reveal_checks(env, uid, tid, pid)
    return p, {"procs": 10, **c, **summarise_http(res)}


def _referred_pair(env):
    from app.db import iso, utcnow
    rid, _ = env.user("referrer")
    fid, femail = env.user("friend", referred_by=rid)
    env.run("INSERT INTO referrals (referrer_id, referred_id, created_at, status) VALUES (?,?,?, 'joined')", rid, fid, iso(utcnow()))
    return rid, fid, femail


def _referral_checks(env, rid, fid):
    n = env.val("SELECT COUNT(*) FROM credit_ledger WHERE user_id=? AND ref=?", rid, f"u{fid}")
    total = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", rid)
    st = env.val("SELECT status FROM referrals WHERE referred_id=?", fid)
    p = []
    if n != 1 or total != 100:
        p.append(f"referrer credited {n} times ({total}p, expected one 100p bonus)")
    if st != "rewarded":
        p.append(f"referral status {st}")
    return p, n


def s10_referral(env):
    cid, slug = env.comp(price=200, max_tickets=1000, max_per_user=100)
    # (a) the friend's first purchase made in 4 tabs at once
    rid, fid, femail = _referred_pair(env)
    s, tok = login(env.base, femail)
    jobs = []
    for _ in range(4):
        t, _ = clone(s, env.base, tok)
        basket_add(t, env.base, tok, slug, 1)
        jobs.append((ch_checkout, (env.base, t.cookies.get_dict(), tok, {}, True)))
    # (b) another friend's single first checkout confirmed 6 times at once
    rid2, fid2, femail2 = _referred_pair(env)
    k, s2, tok2 = _reserve(env, femail2, slug, 1)
    jobs += [(ch_post, (env.base, s2.cookies.get_dict(), f"/checkout/{k}/demo-pay", {"csrf": tok2})) for _ in range(6)]
    res = fire(jobs)
    p1, n1 = _referral_checks(env, rid, fid)
    p2, n2 = _referral_checks(env, rid2, fid2)
    paid_a = env.val("SELECT COUNT(*) FROM checkouts WHERE user_id=? AND status='paid'", fid)
    return [f"(a) {x}" for x in p1] + [f"(b) {x}" for x in p2], {"bonus_rows_a": n1, "paid_checkouts_a": paid_a,
                                                                    "bonus_rows_b": n2, **summarise_http(res)}


def s11_approval_double_execute(env):
    """Four-eye approval of a large wallet adjustment, approved by two admins / double-clicked at the same time."""
    a_id, a_s, a_tok = _admin(env)
    target, _ = env.user("target")
    r = post(a_s, env.base, f"/admin/users/{target}", {"action": "credit", "amount": "150", "kind": "credit",
                                                       "reason": "Race audit large adjustment"}, a_tok)
    b_id, b_s, b_tok = _admin(env)
    c_id, c_s, c_tok = _admin(env)
    rid = env.val("SELECT id FROM approvals WHERE target=? AND status='pending' ORDER BY id DESC", f"user:{target}")
    if rid is None:
        # approvals only kick in if another approver existed when A asked: B and C exist now, so ask again
        r = post(a_s, env.base, f"/admin/users/{target}", {"action": "credit", "amount": "150", "kind": "credit",
                                                           "reason": "Race audit large adjustment"}, a_tok)
        rid = env.val("SELECT id FROM approvals WHERE target=? AND status='pending' ORDER BY id DESC", f"user:{target}")
    before = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", target)
    if rid is None or before != 0:
        return [f"setup: approval not created (status {r.status_code}, balance {before})"], {}
    jobs = []
    for s, tok in ((b_s, b_tok), (c_s, c_tok)):
        jobs += [(ch_post, (env.base, s.cookies.get_dict(), "/admin/approvals",
                            {"csrf": tok, "id": str(rid), "decision": "approve"})) for _ in range(4)]
    res = fire(jobs)
    n = env.val("SELECT COUNT(*) FROM credit_ledger WHERE user_id=? AND amount=15000", target)
    total = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", target)
    st = env.val("SELECT status FROM approvals WHERE id=?", rid)
    p = [] if n == 1 and total == 15000 else [f"one approved £150 adjustment applied {n} times (customer credited {total}p)"]
    return p, {"procs": 8, "ledger_rows": n, "credited": total, "approval_status": st, **summarise_http(res)}


def s12a_deposit_refund(env):
    uid, email = env.user("deprf")
    pi = f"pi_dep_{uniq('')}"
    did = env.paid_deposit(uid, 2000, pi=pi)
    s, tok = login(env.base, email)
    res = fire([(ch_post, (env.base, s.cookies.get_dict(), "/account/deposit/refund", {"csrf": tok})) for _ in range(6)])
    calls = [r for r in env.stub_calls("refund") if r["pi"] == pi]
    card = sum(r["amount"] or 0 for r in calls)
    refunded = env.val("SELECT refunded FROM deposits WHERE id=?", did)
    bal = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='deposit'", uid)
    p = []
    if card != 2000:
        p.append(f"£20 deposit: card refunds sent to Stripe total {card}p in {len(calls)} calls; DB records {refunded}p refunded")
    if bal < 0:
        p.append(f"deposit balance negative {bal}")
    return p, {"procs": 6, "stripe_refund_calls": len(calls), "card_refunded": card, "db_refunded": refunded,
               "deposit_balance": bal, **summarise_http(res)}


def s12b_deposit_refund_vs_purchase(env):
    uid, email = env.user("deprb")
    pi = f"pi_dep_{uniq('')}"
    env.paid_deposit(uid, 1000, pi=pi)
    cid, slug = env.comp(price=100, max_tickets=1000, max_per_user=100)
    s, tok = login(env.base, email)
    jobs = []
    for k in range(6):
        if k % 2 == 0:
            jobs.append((ch_post, (env.base, s.cookies.get_dict(), "/account/deposit/refund", {"csrf": tok})))
        else:
            t, _ = clone(s, env.base, tok)
            basket_add(t, env.base, tok, slug, 8)
            jobs.append((ch_checkout, (env.base, t.cookies.get_dict(), tok, {"use_credit": "1"}, False)))
    res = fire(jobs)
    card = sum(r["amount"] or 0 for r in env.stub_calls("refund") if r["pi"] == pi)
    spent = env.val("SELECT COALESCE(SUM(deposit_used),0) FROM checkouts WHERE user_id=? AND status IN ('paid','pending')", uid)
    bal = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='deposit'", uid)
    p = []
    if card + spent > 1000:
        p.append(f"£10 deposit: {card}p refunded to card + {spent}p spent on entries = {card + spent}p")
    if bal < 0:
        p.append(f"deposit balance negative {bal}")
    return p, {"procs": 6, "card_refunded": card, "deposit_spent": spent, "deposit_balance": bal, **summarise_http(res)}


def s13_goodwill_cap(env):
    _, s, tok = _admin(env, role="support")     # Support role: goodwill allowed, capped at £20 per customer per 30 days
    target, _ = env.user("gw")
    data = {"csrf": tok, "action": "goodwill", "amount": "15", "reason": "Race audit service problem"}
    res = fire([(ch_post, (env.base, s.cookies.get_dict(), f"/admin/users/{target}", data)) for _ in range(8)])
    total = env.val("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", target)
    p = [] if total <= 2000 else [f"goodwill cap £20 exceeded: {total}p given"]
    return p, {"procs": 8, "goodwill_given": total, **summarise_http(res)}


SCENARIOS = [
    # key, title, stripe-mode, fn, variants
    ("S1", "Final tickets (N left, 22 buyers + double-press tabs)", False, s1_final_tickets, [1, 2, 5, 10]),
    ("S2", "Same customer, 6 tabs paying from one wallet", False, s2_two_tabs_wallet, None),
    ("S3a", "Concurrent withdrawals from one cash balance", False, s3a_withdrawals, None),
    ("S3b", "Withdrawals + wallet purchases at once", False, s3b_withdraw_and_buy, None),
    ("S4", "Concurrent points redemption", False, s4_points, None),
    ("S5a", "Single-use promo (max_uses=1), 20 users", False, s5a_promo_many_users, None),
    ("S5b", "per_user=1 promo, one user 6 tabs", False, s5b_promo_same_user, None),
    ("S6a", "Same checkout confirmed by 10 concurrent demo-pay posts", False, s6a_duplicate_demo_pay, None),
    ("S6b", "Same checkout, 10 concurrent identical Stripe webhooks", True, s6b_duplicate_webhooks, None),
    ("S6c", "Credit-card payment, 10 concurrent identical webhooks", True, s6c_credit_card_webhooks, None),
    ("S7a", "Concurrent draw: 6 admin POSTs + 3 run_draw + 3 due_auto_draws", False, s7a_concurrent_draw, None),
    ("S7b", "Redraw double-submitted x6", False, s7b_double_redraw, None),
    ("S8", "Free daily play claimed 10x at once", False, s8_free_play, None),
    ("S9a", "Same instant-win play revealed 10x at once", False, s9a_concurrent_reveal, None),
    ("S9b", "Reveal x5 concurrent with settle_unrevealed x5", False, s9b_reveal_vs_settle, None),
    ("S10", "Referral reward: first purchase completing twice", False, s10_referral, None),
    ("S11", "Four-eye approval executed concurrently (large wallet adjustment)", False, s11_approval_double_execute, None),
    ("S12a", "Unspent-deposit refund requested 6x at once", True, s12a_deposit_refund, None),
    ("S12b", "Deposit refund concurrent with wallet purchases", True, s12b_deposit_refund_vs_purchase, None),
    ("S13", "Goodwill credit cap, 8 concurrent grants", False, s13_goodwill_cap, None),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--json", default="", help="write all results to this file")
    args = ap.parse_args(argv)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    rows, failed = [], 0
    for key, title, stripe, fn, variants in SCENARIOS:
        if only and key not in only:
            continue
        env = Env(key, stripe=stripe, workers=args.workers, threads=args.threads)
        try:
            for variant in (variants or [None]):
                label = key if variant is None else f"{key}[N={variant}]"
                for rnd in range(1, args.rounds + 1):
                    before = set(env.integrity())
                    tb_before = env.log_tracebacks()
                    t0 = time.time()
                    try:
                        problems, counts = fn(env) if variant is None else fn(env, variant)
                    except Exception as e:
                        problems, counts = [f"harness error: {type(e).__name__}: {e}"], {"tb": traceback.format_exc()[-600:]}
                    new_integrity = [x for x in env.integrity() if x not in before]
                    tbs = env.log_tracebacks() - tb_before
                    ok = not problems and not new_integrity
                    failed += not ok
                    row = {"scenario": label, "title": title, "round": rnd, "result": "PASS" if ok else "FAIL",
                           "problems": problems, "integrity": new_integrity, "server_tracebacks": tbs,
                           "secs": round(time.time() - t0, 1), **counts}
                    rows.append(row)
                    extra = "; ".join(problems + [f"integrity: {x}" for x in new_integrity])
                    shown = {k: v for k, v in counts.items() if k not in ("tb",) and v not in ([], 0, None)}
                    print(f"{label:<10} r{rnd} {row['result']}  {shown}  tracebacks={tbs}" + (f"\n           -> {extra}" if extra else ""),
                          flush=True)
        finally:
            env.stop()
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1, default=str)
    print("\nSUMMARY")
    by = {}
    for r in rows:
        by.setdefault(r["scenario"], []).append(r["result"])
    for k, v in by.items():
        print(f"  {k:<10} {v.count('PASS')}/{len(v)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
