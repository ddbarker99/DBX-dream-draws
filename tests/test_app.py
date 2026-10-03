import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ALLOW_DEV_KEY"] = "1"
warnings.simplefilter("ignore", ResourceWarning)

from app import create_app  # noqa: E402
from app.db import _connect  # noqa: E402
from app.services import instant_commitment  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.tmp
        self.app = create_app({"TESTING": True, "DEMO_PAYMENTS": True, "STRIPE_WEBHOOK_SECRET": "whsec_test", "ADMIN_MFA": False,
                               "REFERRAL_BONUS": 100, "POSTAL_ADDRESS": "DBX Dream Draws, PO Box 1, Testtown, TE1 1ST", "REQUIRE_COMP_IMAGE": False})
        self.client = self.app.test_client()
        from app import jobs, routes
        routes._FAILS.clear()          # rate limits are per process; each test starts clean
        jobs._last.clear()             # and background jobs are due straight away

    def tearDown(self):
        # Whatever a test did through the app, the data must still pass every integrity check.
        if getattr(self, "skip_integrity", False):
            return
        from app.checks import integrity_problems
        with self.app.app_context():
            from app.db import get_db
            self.assertEqual(integrity_problems(get_db()), [])

    def jobs_due(self):
        """Make every background job due on the next request (they're throttled per process and in the database)."""
        from app import jobs
        jobs._last.clear()
        db = self.db()
        db.execute("DELETE FROM job_status")
        db.commit()

    def cli(self, *args):
        return self.app.test_cli_runner().invoke(args=list(args))

    def db(self):
        return _connect(self.app.config["DATABASE"])

    def q(self, sql, *a):
        return self.db().execute(sql, a).fetchone()[0]

    def csrf(self, client=None):
        html = (client or self.client).get("/login").get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def post(self, url, data=None, client=None, **kw):
        c = client or self.client
        d = dict(data or {})
        d["csrf"] = self.csrf(c)
        return c.post(url, data=d, **kw)

    def signup(self, email, client=None, dob="1990-01-01", name="Test Person"):
        return self.post("/signup", {"name": name, "email": email, "dob": dob,
                                     "password": "supersecret123", "agree": "1"}, client=client)

    def make_comp(self, title="Test Prize", max_tickets=10, max_per_user=5, price="2.50", tiers="", publish=True,
                  instant=None):
        self.post("/admin/competitions/new", {
            "title": title, "description": "A brand new prize, delivered free to your door.", "ends_at": "2099-01-01T20:00", "category": "tech",
            "ticket_price": price, "max_tickets": str(max_tickets), "max_per_user": str(max_per_user),
            "discount_tiers": tiers, "prize_value": "500",
            "question": "2+2?", "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b"})
        cid = self.q("SELECT id FROM competitions ORDER BY id DESC")
        for spec in instant or []:
            self.post(f"/admin/competitions/{cid}/instant", spec)
        if publish:
            self.post(f"/admin/competitions/{cid}/status", {"action": "publish"})
        return cid

    def slug(self, cid):
        return self.q("SELECT slug FROM competitions WHERE id=?", cid)

    def add(self, cid, qty=1, numbers="", answer="b", client=None):
        return self.post("/basket/add", {"slug": self.slug(cid), "quantity": str(qty), "numbers": numbers,
                                         "answer": answer}, client=client)

    def checkout(self, client=None, promo="", use_credit=False, pay=True):
        d = {"promo": promo}
        if use_credit:
            d["use_credit"] = "1"
        r = self.post("/basket/checkout", d, client=client)
        loc = r.headers.get("Location", "")
        m = re.search(r"/checkout/(\d+)/(demo-pay|done)", loc)
        if not m:
            return None
        cid = int(m.group(1))
        if m.group(2) == "demo-pay" and pay:
            self.post(f"/checkout/{cid}/demo-pay", client=client)
        return cid


class Tests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.cid = self.make_comp()

    def test_no_automatic_admin_and_cli(self):
        self.assertEqual(self.q("SELECT is_admin FROM users WHERE email='admin@example.com'"), 1)
        c = self.app.test_client()
        self.signup("new@example.com", client=c)
        self.assertEqual(self.q("SELECT is_admin FROM users WHERE email='new@example.com'"), 0)
        self.assertEqual(c.get("/admin/").status_code, 404)
        self.assertIn("admin@example.com", self.cli("list-admins").output)
        self.assertNotIn("new@example.com", self.cli("list-admins").output)
        self.cli("make-admin", "new@example.com")
        self.assertEqual(c.get("/admin/").status_code, 200)
        self.cli("remove-admin", "new@example.com")
        self.assertEqual(c.get("/admin/").status_code, 404)
        self.assertEqual(self.cli("make-admin", "nobody@example.com").exit_code, 1)

    def test_first_signup_is_not_admin(self):
        fresh = Base()
        fresh.setUp()
        fresh.signup("first@example.com")
        self.assertEqual(fresh.q("SELECT is_admin FROM users"), 0)

    def test_csrf_and_404(self):
        self.assertEqual(self.client.post("/logout").status_code, 400)
        c = self.app.test_client()
        self.signup("plain@example.com", client=c)
        self.assertEqual(c.get("/admin/").status_code, 404)

    def test_under_18_rejected(self):
        r = self.signup("kid@example.com", client=self.app.test_client(), dob="2015-01-01")
        self.assertIn("18 or over", r.get_data(as_text=True))

    def test_wrong_answer_not_added(self):
        self.add(self.cid, 1, answer="a")
        self.assertIsNone(self.checkout())
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)

    def test_lucky_dip_and_per_user_cap(self):
        self.add(self.cid, 3)
        self.assertIsNotNone(self.checkout())
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 3)
        self.add(self.cid, 3)
        self.assertIsNone(self.checkout())  # 3 + 3 > 5
        self.add(self.cid, 2)
        self.assertIsNotNone(self.checkout())

    def test_pick_numbers_and_clash(self):
        self.add(self.cid, numbers="2,7")
        self.checkout()
        nums = [r[0] for r in self.db().execute("SELECT number FROM tickets ORDER BY number")]
        self.assertEqual(nums, [2, 7])
        c = self.app.test_client()
        self.signup("b@example.com", client=c)
        r = self.add(self.cid, numbers="7", client=c)
        r = c.get(r.headers["Location"])
        self.assertIn("#7 has just been taken", r.get_data(as_text=True))     # told straight away, not at checkout
        self.assertEqual(c.get("/basket").get_data(as_text=True).count("Remove"), 0)
        # a number that goes while it's sitting in someone's basket is flagged there, then refused at checkout
        self.add(self.cid, numbers="9", client=c)
        self.add(self.cid, numbers="9")
        self.checkout()
        self.assertIn("#9", c.get("/basket").get_data(as_text=True))
        r = self.post("/basket/checkout", {}, client=c, follow_redirects=True)
        self.assertIn("has just been taken by someone else", r.get_data(as_text=True))
        j = self.client.get(f"/c/{self.slug(self.cid)}/numbers?start=1").get_json()
        self.assertEqual(j["taken"], [2, 7, 9])

    def test_basket_multiple_comps_one_payment(self):
        c2 = self.make_comp("Second Prize", price="1.00")
        self.add(self.cid, 2)
        self.add(c2, 3)
        chk = self.checkout()
        row = self.db().execute("SELECT * FROM checkouts WHERE id=?", (chk,)).fetchone()
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["cash_due"], 500 + 300)
        self.assertEqual(self.q("SELECT COUNT(*) FROM orders WHERE checkout_id=?", chk), 2)

    def test_multibuy_discount(self):
        c2 = self.make_comp("Deal", max_tickets=100, max_per_user=50, price="1.00", tiers="10:10, 25:20")
        self.add(c2, 25)
        chk = self.checkout()
        self.assertEqual(self.q("SELECT cash_due FROM checkouts WHERE id=?", chk), 2000)

    def test_promo_code(self):
        self.post("/admin/promos", {"code": "half", "percent": "50", "fixed": "0", "min_spend": "0", "per_user": "1"})
        self.add(self.cid, 2)
        chk = self.checkout(promo="HALF")
        self.assertEqual(self.q("SELECT cash_due FROM checkouts WHERE id=?", chk), 250)
        self.assertEqual(self.q("SELECT uses FROM promo_codes"), 1)
        self.add(self.cid, 1)
        self.assertIsNone(self.checkout(promo="HALF"))  # one use per person

    def test_instant_win_credit_and_wallet_spend(self):
        c2 = self.make_comp("IW", max_tickets=5, max_per_user=5, price="1.00",
                            instant=[{"title": "£3 Credit", "value": "3", "type": "credit", "quantity": "5"}])
        # every number is an instant win, so buying 1 must win
        self.assertTrue(self.q("SELECT instant_hash FROM competitions WHERE id=?", c2))
        self.add(c2, 1)
        chk = self.checkout()
        page = self.client.get(f"/checkout/{chk}/done").get_data(as_text=True)
        self.assertIn("won instantly", page)
        uid = self.q("SELECT id FROM users WHERE email='admin@example.com'")
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=?", uid), 300)
        # spend £2.50 of credit fully -> no card payment
        self.add(self.cid, 1)
        chk2 = self.checkout(use_credit=True)
        row = self.db().execute("SELECT * FROM checkouts WHERE id=?", (chk2,)).fetchone()
        self.assertEqual((row["status"], row["cash_due"], row["credit_used"]), ("paid", 0, 250))
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=?", uid), 50)

    def test_instant_commitment_verifiable_and_locked(self):
        c2 = self.make_comp("IW2", max_tickets=50, price="1.00",
                            instant=[{"title": "Prize", "value": "10", "type": "physical", "quantity": "3"}])
        db = self.db()
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (c2,)).fetchone()
        prizes = db.execute("SELECT number, title FROM instant_prizes WHERE competition_id=?", (c2,)).fetchall()
        self.assertEqual(instant_commitment(comp["instant_salt"], prizes), comp["instant_hash"])
        self.add(c2, 1)
        self.checkout()
        r = self.post(f"/admin/competitions/{c2}/instant",
                      {"title": "Late", "value": "1", "type": "credit", "quantity": "1"}, follow_redirects=True)
        self.assertIn("locked", r.get_data(as_text=True))

    def test_cancelled_checkout_releases_tickets_and_credit(self):
        uid = self.q("SELECT id FROM users WHERE email='admin@example.com'")
        db = self.db()
        db.execute("INSERT INTO credit_ledger (user_id, amount, reason, created_at) VALUES (?, 100, 'gift', '2026-01-01T00:00:00Z')", (uid,))
        db.commit()
        self.add(self.cid, 2)
        chk = self.checkout(use_credit=True, pay=False)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='held'"), 2)
        self.client.get(f"/checkout/{chk}/cancel")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=?", uid), 100)

    def test_stale_holds_expire(self):
        self.add(self.cid, 5)
        self.checkout(pay=False)
        db = self.db()
        db.execute("UPDATE checkouts SET created_at='2000-01-01T00:00:00Z'")
        db.commit()
        c = self.app.test_client()
        self.signup("late@example.com", client=c)
        self.add(self.cid, 5, client=c)
        self.assertIsNotNone(self.checkout(client=c))

    def test_spend_limits(self):
        self.post("/account/limits", {"daily": "5", "weekly": "", "monthly": "250"})
        self.add(self.cid, 2)
        self.assertIsNotNone(self.checkout())    # £5
        self.add(self.cid, 1)
        self.assertIsNone(self.checkout())       # over daily £5
        r = self.post("/account/limits", {"daily": "100", "weekly": "", "monthly": "250"}, follow_redirects=True)
        self.assertIn("72 hours", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT daily_limit FROM users WHERE email='admin@example.com'"), 500)

    def test_self_exclusion(self):
        self.post("/account/exclude", {"days": "1"})
        self.add(self.cid, 1)
        self.assertIsNone(self.checkout())

    def test_referral_bonus_once(self):
        code = self.q("SELECT referral_code FROM users WHERE email='admin@example.com'")
        c = self.app.test_client()
        c.get(f"/r/{code}")
        self.signup("friend@example.com", client=c, name="Friend Person")
        self.add(self.cid, 1, client=c)
        self.checkout(client=c)
        self.add(self.cid, 1, client=c)
        self.checkout(client=c)
        uid = self.q("SELECT id FROM users WHERE email='admin@example.com'")
        self.assertEqual(self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", uid), 100)

    def test_withdrawal_flow(self):
        uid = self.q("SELECT id FROM users WHERE email='admin@example.com'")
        self.post(f"/admin/users/{uid}", {"action": "credit", "amount": "20", "reason": "test", "kind": "cash"})
        self.post(f"/admin/users/{uid}", {"action": "credit", "amount": "7", "reason": "bonus", "kind": "credit"})
        bank = {"amount": "15", "method": "bank", "account_name": "Admin Person", "sort_code": "12-34-56",
                "account_number": "12345678"}
        r = self.post("/account/withdraw", bank, follow_redirects=True)
        self.assertIn("verify your email", r.get_data(as_text=True))          # must verify first
        self.post(f"/admin/users/{uid}", {"action": "verify"})
        r = self.post("/account/withdraw", dict(bank, amount="25"), follow_redirects=True)
        self.assertIn("more than your cash balance", r.get_data(as_text=True))  # credit isn't withdrawable
        self.post("/account/withdraw", dict(bank, sort_code="123"), follow_redirects=True)
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 0)       # bad sort code rejected
        r = self.post("/account/withdraw", bank)                              # step 1: review, nothing taken yet
        self.assertIn("Check your withdrawal", r.get_data(as_text=True))
        self.assertIn("12-34-56", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 0)
        r = self.post("/account/withdraw", dict(bank, step="confirm"))      # step 2: request
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=? AND kind='cash'", uid), 500)
        detail = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Withdrawal requested", detail)
        other = self.app.test_client()
        self.signup("nosy@example.com", client=other)
        self.assertEqual(other.get(r.headers["Location"]).status_code, 404)   # nobody else can see it
        self.post("/admin/payouts", {"wid": str(self.q("SELECT id FROM withdrawals")), "action": "processing"})
        self.assertEqual(self.q("SELECT status FROM withdrawals"), "processing")
        csv_ = self.client.get("/admin/payouts/export.csv").get_data(as_text=True)
        self.assertIn("123456", csv_)
        self.assertIn("12345678", csv_)
        wid = self.q("SELECT id FROM withdrawals")
        self.post("/admin/payouts", {"wid": str(wid), "action": "reject"})
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=? AND kind='cash'", uid), 2000)
        self.assertEqual(self.q("SELECT account_number FROM withdrawals"), "****5678")   # masked afterwards

    def test_no_oversell_under_concurrency(self):
        clients = []
        for i in range(8):
            c = self.app.test_client()
            self.signup(f"u{i}@example.com", client=c)
            self.add(self.cid, 2, client=c)
            clients.append((c, self.csrf(c)))
        barrier = threading.Barrier(8)

        def go(c, token):
            barrier.wait()
            c.post("/basket/checkout", data={"csrf": token})

        threads = [threading.Thread(target=go, args=ct) for ct in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 10)
        self.assertEqual(self.q("SELECT COUNT(DISTINCT number) FROM tickets"), 10)

    def test_postal_entry_and_verifiable_draw(self):
        self.add(self.cid, 3)
        self.checkout()
        self.post(f"/admin/competitions/{self.cid}/postal",
                  {"name": "Post Person", "email": "p@example.com", "address": "1 Road", "answer_correct": "1",
                   "received": time.strftime("%Y-%m-%d")})
        self.post(f"/admin/competitions/{self.cid}/postal",
                  {"name": "Wrong", "email": "w@example.com", "address": "2 Road", "answer_correct": "0",
                   "received": time.strftime("%Y-%m-%d")})
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 4)
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z' WHERE id=?", (self.cid,))
        db.commit()
        self.post(f"/admin/competitions/{self.cid}/draw")
        c = db.execute("SELECT * FROM competitions WHERE id=?", (self.cid,)).fetchone()
        self.assertEqual(c["status"], "drawn")
        nums = sorted(r[0] for r in db.execute("SELECT number FROM tickets WHERE competition_id=?", (self.cid,)))
        self.assertEqual(hashlib.sha256(c["seed"].encode()).hexdigest(), c["seed_hash"])
        digest = hashlib.sha256(",".join(map(str, nums)).encode()).hexdigest()
        i = int(hmac.new(c["seed"].encode(), digest.encode(), hashlib.sha256).hexdigest(), 16) % len(nums)
        self.assertEqual(nums[i], self.q("SELECT number FROM tickets WHERE id=?", c["winner_ticket_id"]))
        self.assertIn(f"#{nums[i]}", self.client.get("/winners").get_data(as_text=True))
        self.assertIn(f"#{nums[i]}", self.client.get("/results").get_data(as_text=True))

    def test_stripe_webhook_signature_and_idempotency(self):
        self.add(self.cid, 2)
        chk = self.checkout(pay=False)
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "id": "cs_test_1", "payment_status": "paid", "metadata": {"checkout_id": str(chk)}}}}).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(self.client.post("/stripe/webhook", data=body,
                                          headers={"Stripe-Signature": f"t={ts},v1=deadbeef"}).status_code, 400)
        for _ in range(2):
            r = self.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 2)

    def test_password_reset(self):
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.post("/forgot", {"email": "admin@example.com"})
        link = re.search(r"(/reset/\S+)", "\n".join(logs.output)).group(1)
        self.post(link, {"password": "brandnewpass1"})
        c = self.app.test_client()
        r = self.post("/login", {"email": "admin@example.com", "password": "brandnewpass1"}, client=c)
        self.assertEqual(r.status_code, 302)

    def test_pages_render(self):
        self.add(self.cid, 1)
        self.checkout()
        uid = self.q("SELECT id FROM users LIMIT 1")
        s = self.slug(self.cid)
        for url in ["/", "/competitions", "/competitions?tab=instant", "/competitions?tab=ending", "/competitions?tab=tech", f"/c/{s}", f"/c/{s}/entries", "/basket",
                    "/winners", "/results", "/search?q=prize", "/winners?tab=live", "/how-it-works", "/contact", "/cookies", "/free-entry", "/terms", "/fair-draws", "/faq",
                    "/responsible-play", "/complaints", "/privacy", "/manifest.webmanifest", "/sw.js",
                    "/account", "/account?tab=entries", "/account?tab=wins", "/account?tab=wallet", "/account?tab=transactions", "/account?tab=points", "/account?tab=safer",
                    "/account?tab=profile", "/account?tab=entries&show=won", "/account?tab=entries&show=previous",
                    "/admin/", f"/admin/competitions/{self.cid}", f"/admin/competitions/{self.cid}/edit",
                    f"/admin/competitions/{self.cid}/export.csv", "/admin/promos", "/admin/users",
                    f"/admin/users/{uid}", "/admin/payouts", "/admin/settings", f"/admin/competitions/new?copy={self.cid}"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/nope").status_code, 404)

    def test_settings_live_banner(self):
        self.post("/admin/settings", {"announcement": "Big sale", "live_now_url": "https://youtube.com/x",
                                      "live_now_title": "Live now"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Big sale", page)
        self.assertIn("youtube.com/x", page)


class GameTests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.uid = self.q("SELECT id FROM users WHERE email='admin@example.com'")

    def game(self, kind="scratch", plays=4, win_all=True):
        self.post("/admin/competitions/new", {
            "title": f"Test {kind}", "description": "", "ends_at": "2099-01-01T20:00", "category": "cash",
            "game_type": kind, "ticket_price": "0.50", "max_tickets": str(plays), "max_per_user": str(plays),
            "question": "2+2?", "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b"})
        cid = self.q("SELECT id FROM competitions ORDER BY id DESC")
        self.post(f"/admin/competitions/{cid}/instant", {"title": "£2 Credit", "value": "2", "type": "credit",
                                                         "quantity": str(plays if win_all else 1)})
        self.post(f"/admin/competitions/{cid}/status", {"action": "publish"})
        return cid

    def wallet(self):
        return self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", self.uid)

    def test_starter_games(self):
        self.post("/admin/games/starter", {"publish": "1", "days": "30"})
        rows = self.db().execute("SELECT * FROM competitions WHERE game_type!='' ORDER BY ticket_price").fetchall()
        self.assertEqual([r["ticket_price"] for r in rows], [10, 40, 50, 100, 500])
        self.assertTrue(all(r["status"] == "live" and r["instant_hash"] for r in rows))
        self.assertEqual(self.q("SELECT COUNT(*) FROM instant_prizes WHERE competition_id=?", rows[0]["id"]), 288)
        for url in ["/instant-wins", "/instant-wins?price=10", "/instant-wins?type=spin", "/"] + [f"/c/{r['slug']}" for r in rows]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertIn("10p", self.client.get("/instant-wins").get_data(as_text=True))

    def test_random_games_live_and_sane(self):
        for _ in range(3):
            self.post("/admin/games/random", {"p10": ["0", "1"], "p40": ["0", "1"], "p50": ["0", "1"], "p100": ["0", "1"],
                                              "p500": ["0", "1"], "per_price": "1", "days": "30"})
        db = self.db()
        rows = db.execute("SELECT * FROM competitions WHERE game_type!=''").fetchall()
        self.assertEqual(len(rows), 15)
        self.assertEqual(sorted({r["ticket_price"] for r in rows}), [10, 40, 50, 100, 500])
        for r in rows:
            self.assertEqual(r["status"], "live")
            self.assertTrue(r["instant_hash"])
            n, pool = db.execute("SELECT COUNT(*), SUM(value) FROM instant_prizes WHERE competition_id=?", (r["id"],)).fetchone()
            rtp = pool / (r["ticket_price"] * r["max_tickets"])
            self.assertTrue(0.25 <= rtp <= 0.6, (r["title"], rtp))
            self.assertTrue(1 <= n <= r["max_tickets"] * 0.3, (r["title"], n))
        page = self.client.get("/instant-wins").get_data(as_text=True)
        self.assertEqual(page.count("gamecard"), 15)

    def test_only_selected_prices_and_drafts(self):
        self.post("/admin/games/random", {"p10": ["0", "1"], "p40": "0", "p50": "0", "p100": "0", "p500": "0",
                                          "per_price": "2", "draft": "1"})
        rows = self.db().execute("SELECT ticket_price, status FROM competitions WHERE game_type!=''").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(10, "draft"), (10, "draft")])
        self.assertNotIn("gamecard", self.client.get("/instant-wins").get_data(as_text=True))
        self.post("/admin/games/publish-drafts")
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE status='live'"), 2)

    def test_buy_play_reveal_credits_once(self):
        cid = self.game(plays=4)
        self.add(cid, 2)
        r = self.post("/basket/checkout", {})
        chk = int(re.search(r"/checkout/(\d+)/demo-pay", r.headers["Location"]).group(1))
        r = self.post(f"/checkout/{chk}/demo-pay")
        r = self.client.get(f"/checkout/{chk}/done")
        self.assertIn(f"/play/{self.slug(cid)}", r.headers["Location"])     # straight into the game
        self.assertEqual(self.wallet(), 0)                                    # not paid until revealed
        page = self.client.get(f"/play/{self.slug(cid)}").get_data(as_text=True)
        self.assertIn("SCRATCH", page.upper())
        tids = [r[0] for r in self.db().execute("SELECT id FROM tickets WHERE competition_id=? ORDER BY id", (cid,))]
        res = self.post(f"/play/reveal/{tids[0]}").get_json()
        self.assertTrue(res["win"])
        self.assertEqual(self.wallet(), 200)
        self.post(f"/play/reveal/{tids[0]}")                                  # revealing twice pays once
        self.assertEqual(self.wallet(), 200)
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self.assertEqual(self.post(f"/play/reveal/{tids[1]}", client=other).status_code, 404)
        self.post(f"/play/{self.slug(cid)}/reveal-all")
        self.assertEqual(self.wallet(), 400)

    def test_unrevealed_wins_paid_automatically(self):
        cid = self.game(plays=3)
        self.add(cid, 3)
        self.checkout()
        self.assertEqual(self.wallet(), 0)
        db = self.db()
        db.execute("UPDATE tickets SET created_at='2000-01-01T00:00:00Z'")
        db.commit()
        self.client.get("/account")                                          # any page load settles
        self.assertEqual(self.wallet(), 600)

    def test_game_has_no_draw_and_hides_spoilers(self):
        cid = self.game(plays=3, win_all=False)
        self.add(cid, 3)
        self.checkout()
        board = self.client.get(f"/c/{self.slug(cid)}").get_data(as_text=True)
        self.assertIn("1 of 1 left", board)                                  # win not shown until revealed
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z' WHERE id=?", (cid,))
        db.commit()
        r = self.post(f"/admin/competitions/{cid}/draw", follow_redirects=True)
        self.assertIn("don&#39;t have a main draw", r.get_data(as_text=True))


class V4Tests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.uid = self.q("SELECT id FROM users WHERE email='admin@example.com'")
        self.post(f"/admin/users/{self.uid}", {"action": "verify"})

    def bal(self, kind):
        return self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind=?", self.uid, kind)

    def comp(self, title="Draw", price="1.00", tickets=20, game="", prize=None, auto="1"):
        self.post("/admin/competitions/new", {
            "title": title, "description": "A brand new prize, delivered free to your door.", "ends_at": "2099-01-01T20:00", "category": "cash", "game_type": game,
            "ticket_price": price, "max_tickets": str(tickets), "max_per_user": str(tickets), "auto_draw": auto,
            "question": "2+2?", "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b"})
        cid = self.q("SELECT id FROM competitions ORDER BY id DESC")
        if prize:
            self.post(f"/admin/competitions/{cid}/instant", prize)
        self.post(f"/admin/competitions/{cid}/status", {"action": "publish"})
        return cid

    def test_cash_prize_goes_to_cash_and_is_spent_after_credit(self):
        cid = self.comp(prize={"title": "£5 Cash", "value": "5", "type": "cash", "quantity": "20"})
        self.add(cid, 1)
        self.checkout()
        self.assertEqual((self.bal("cash"), self.bal("credit")), (500, 0))
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "1", "kind": "credit"})
        self.add(cid, 3)                                   # £3: £1 credit first, then £2 cash
        chk = self.checkout(use_credit=True)
        row = self.db().execute("SELECT * FROM checkouts WHERE id=?", (chk,)).fetchone()
        self.assertEqual((row["credit_used"], row["cash_used"], row["cash_due"]), (100, 200, 0))
        self.assertEqual(self.bal("credit"), 0)
        self.assertEqual(self.bal("cash"), 300 + 1500)     # 3 more cash wins of £5

    def test_points_awarded_and_redeemed(self):
        cid = self.comp(price="10.00", tickets=50)
        self.add(cid, 15)                                  # £150 by card -> 150 points
        self.checkout()
        self.assertEqual(self.q("SELECT points FROM users WHERE id=?", self.uid), 150)
        self.post("/account/redeem", {"blocks": "1"})
        self.assertEqual(self.q("SELECT points FROM users WHERE id=?", self.uid), 50)
        self.assertEqual(self.bal("credit"), 100)
        self.post("/account/redeem", {"blocks": "1"})      # not enough points
        self.assertEqual(self.bal("credit"), 100)

    def test_free_daily_once_per_day_and_needs_verified_email(self):
        self.post("/admin/games/free-daily", {"kind": "spin"})
        c = self.db().execute("SELECT * FROM competitions WHERE free_daily=1").fetchone()
        self.assertEqual((c["status"], c["ticket_price"]), ("live", 0))
        r = self.post(f"/free-play/{c['slug']}")
        self.assertIn("/play/", r.headers["Location"])
        home = self.client.get("/instant-wins")
        self.assertIn("no-store", home.headers["Cache-Control"])
        self.assertIn("come back tomorrow", home.get_data(as_text=True))
        self.post(f"/free-play/{c['slug']}")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=?", c["id"]), 1)
        other = self.app.test_client()
        self.signup("new@example.com", client=other)
        r = self.post(f"/free-play/{c['slug']}", client=other, follow_redirects=True)
        self.assertIn("Verify your email", r.get_data(as_text=True))
        self.add(c["id"], 1)                               # can't buy the free game
        self.assertIsNone(self.checkout())
        self.assertIn("Play free now", self.client.get("/instant-wins").get_data(as_text=True).replace("today's free play", "Play free now"))

    def test_auto_draw_runs_and_manual_opt_out(self):
        auto = self.comp("Auto")
        manual = self.comp("Manual", auto="")
        for cid in (auto, manual):
            self.add(cid, 2)
            self.checkout()
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z'")
        db.commit()
        self.jobs_due()
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.client.get("/")
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", auto), "drawn")
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", manual), "live")
        self.assertIn("You've won Auto", "\n".join(logs.output))

    def test_cancel_refunds_cash_once(self):
        cid = self.comp(price="2.00")
        self.add(cid, 3)
        self.checkout()
        self.post(f"/admin/competitions/{cid}/status", {"action": "cancel"})
        self.assertEqual(self.bal("cash"), 600)
        from app.services import refund_competition
        with self.app.app_context():
            refund_competition(cid)
        self.assertEqual(self.bal("cash"), 600)

    def test_login_lockout(self):
        c = self.app.test_client()
        for _ in range(8):
            self.post("/login", {"email": "admin@example.com", "password": "wrong"}, client=c)
        r = self.post("/login", {"email": "admin@example.com", "password": "supersecret123"}, client=c)
        self.assertEqual(r.status_code, 429)

    def test_email_verification_link(self):
        c = self.app.test_client()
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.signup("v@example.com", client=c)
        link = re.search(r"(/verify/\S+)", "\n".join(logs.output)).group(1)
        self.assertEqual(self.q("SELECT email_verified FROM users WHERE email='v@example.com'"), 0)
        c.get(link)
        self.assertEqual(self.q("SELECT email_verified FROM users WHERE email='v@example.com'"), 1)
        self.assertEqual(c.get("/verify/garbage").status_code, 404)

    def test_seo_search_share_stats_and_referral_param(self):
        cid = self.comp("Golden Ticket Special")
        slug = self.slug(cid)
        self.assertIn(slug, self.client.get("/sitemap.xml").get_data(as_text=True))
        self.assertIn("Disallow: /admin/", self.client.get("/robots.txt").get_data(as_text=True))
        page = self.client.get(f"/c/{slug}").get_data(as_text=True)
        self.assertIn('og:title', page)
        self.assertIn("wa.me", page)
        self.assertIn("Golden Ticket", self.client.get("/competitions?q=golden").get_data(as_text=True))
        self.assertNotIn("Golden Ticket", self.client.get("/competitions?q=zzzz").get_data(as_text=True))
        self.assertEqual(self.client.get("/admin/stats").status_code, 200)
        code = self.q("SELECT referral_code FROM users WHERE id=?", self.uid)
        c = self.app.test_client()
        c.get(f"/c/{slug}?ref={code}")
        self.signup("friend@example.com", client=c)
        self.assertEqual(self.q("SELECT referred_by FROM users WHERE email='friend@example.com'"), self.uid)

    def test_image_upload_checked_and_converted(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (2400, 1200), (200, 0, 0)).save(buf, "PNG")
        buf.seek(0)
        d = {"title": "Pic", "description": "", "ends_at": "2099-01-01T20:00", "category": "tech", "ticket_price": "1",
             "max_tickets": "10", "max_per_user": "5", "question": "q", "answer_a": "a", "answer_b": "b", "answer_c": "c",
             "correct": "a", "csrf": self.csrf(), "image": (buf, "big.png")}
        self.client.post("/admin/competitions/new", data=d, content_type="multipart/form-data")
        name = self.q("SELECT image FROM competitions WHERE title='Pic'")
        self.assertTrue(name.endswith(".webp"))
        im = Image.open(os.path.join(self.app.config["UPLOAD_DIR"], name))
        self.assertEqual(max(im.size), 1600)
        d.update(title="Bad", csrf=self.csrf(), image=(io.BytesIO(b"<html>not an image</html>"), "evil.png"))
        r = self.client.post("/admin/competitions/new", data=d, content_type="multipart/form-data")
        self.assertIn("valid image", r.get_data(as_text=True))


class CreditCardTests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.cid = self.make_comp()
        self.uid = self.q("SELECT id FROM users")
        db = self.db()
        db.execute("INSERT INTO credit_ledger (user_id, amount, reason, created_at, kind) VALUES (?, 100, 'x', '2026-01-01T00:00:00Z', 'credit')",
                   (self.uid,))
        db.commit()

    def pay(self, funding, refund_ok=True):
        from unittest import mock
        from app import payments
        self.add(self.cid, 2)
        chk = self.checkout(use_credit=True, pay=False)
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "id": "cs_1", "payment_status": "paid", "payment_intent": "pi_1",
            "metadata": {"checkout_id": str(chk)}}}}).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        refund = mock.Mock(side_effect=None if refund_ok else RuntimeError("stripe down"))
        with mock.patch.object(payments, "card_funding", return_value=funding), mock.patch.object(payments, "refund", refund):
            for _ in range(2):   # webhook retried
                self.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
        return chk, refund

    def test_credit_card_refunded_tickets_released_balance_returned(self):
        chk, refund = self.pay("credit")
        refund.assert_called_once_with("pi_1")
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "credit_refused")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=?", self.uid), 100)
        self.assertIn("aren't accepted", self.client.get(f"/checkout/{chk}/done").get_data(as_text=True))

    def test_debit_card_accepted(self):
        chk, refund = self.pay("debit")
        refund.assert_not_called()
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 2)

    def test_non_card_payment_accepted(self):
        chk, _ = self.pay(None)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")

    def test_failed_refund_flagged_for_admin(self):
        chk, _ = self.pay("credit", refund_ok=False)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "needs_refund")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        self.assertIn("Refunds needed", self.client.get("/admin/payouts").get_data(as_text=True))



class OverhaulTests(Base):
    """Information architecture, basket/checkout journey, account and content checks from the v5 overhaul."""
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.cid = self.make_comp(tiers="3:10", max_per_user=8)

    def test_old_urls_redirect_permanently(self):
        for old, new in [("/games", "/instant-wins"), ("/games?price=10", "/instant-wins?price=10"),
                         ("/winners?tab=results", "/results"), ("/live", "/winners?tab=live"),
                         ("/?tab=ending", "/competitions?tab=ending"), ("/?q=abc", "/competitions?q=abc"),
                         ("/account?tab=tickets", "/account?tab=entries"), ("/account?tab=settings", "/account?tab=safer"),
                         ("/account?tab=rewards", "/account?tab=points"), ("/account?tab=orders", "/account?tab=transactions")]:
            r = self.client.get(old)
            self.assertEqual(r.status_code, 301, old)
            self.assertTrue(r.headers["Location"].endswith(new), (old, r.headers["Location"]))
        self.assertEqual(self.client.get("/winners?tab=bogus").status_code, 301)
        self.assertEqual(self.client.get("/competitions?tab=bogus").status_code, 301)

    def test_unique_titles_canonical_and_noindex(self):
        titles = {}
        for url in ["/", "/competitions", "/instant-wins", "/winners", "/results", "/how-it-works",
                    "/contact", "/faq", "/terms", "/privacy", "/cookies", "/free-entry", "/fair-draws",
                    f"/c/{self.slug(self.cid)}"]:
            html = self.app.test_client().get(url).get_data(as_text=True)
            t = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
            self.assertNotIn(t, titles, (url, titles.get(t)))
            titles[t] = url
            self.assertIn('rel="canonical"', html, url)
            self.assertNotIn('name="robots"', html, url)
            self.assertEqual(html.count("<h1"), 1, url)
        g = self.app.test_client()
        self.assertIn('content="noindex"', g.get(f"/c/{self.slug(self.cid)}/entries").get_data(as_text=True))
        self.assertIn('content="noindex"', g.get("/basket").get_data(as_text=True))

    def test_no_placeholders_or_draft_banners_for_visitors(self):
        for url in ["/free-entry", "/terms", "/privacy", "/faq", "/complaints", "/contact"]:
            html = self.app.test_client().get(url).get_data(as_text=True)
            for bad in ("example.com", "Example Street", "Template only", "Admin only"):
                self.assertNotIn(bad, html, (url, bad))
        self.assertIn("Admin only", self.client.get("/terms").get_data(as_text=True))   # admins still see it
        self.assertIn("Site setup", self.client.get("/admin/competitions").get_data(as_text=True))

    def test_basket_quantity_promo_preview_and_totals(self):
        self.post("/admin/promos", {"code": "SAVE10", "percent": "10", "fixed": "0", "min_spend": "0", "per_user": "1"})
        self.add(self.cid, 1)
        self.post("/basket/update", {"i": "0", "qty": "4"})          # 4 × £2.50 = £10, 10% multi-buy = £9
        page = self.client.get("/basket").get_data(as_text=True)
        self.assertIn("£9", page)
        self.assertIn("Multi-buy savings", page)
        r = self.post("/basket/promo", {"promo": "nope"}, follow_redirects=True)
        self.assertIn("isn&#39;t valid", r.get_data(as_text=True))
        self.post("/basket/promo", {"promo": "save10"})
        page = self.client.get("/basket").get_data(as_text=True)
        self.assertIn("Promo SAVE10", page)
        self.assertIn("£8.10", page)                                   # shown before payment
        self.post("/basket/update", {"i": "0", "qty": "99"})          # capped at the per-person limit
        self.assertIn('value="8"', self.client.get("/basket").get_data(as_text=True))
        chk = self.checkout()
        c = self.db().execute("SELECT * FROM checkouts WHERE id=?", (chk,)).fetchone()
        self.assertEqual((c["subtotal"], c["promo_discount"], c["status"]), (1800, 180, "paid"))
        self.post("/basket/update", {"i": "0", "qty": "0"})           # harmless on an empty basket

    def test_cancelled_payment_restores_basket(self):
        self.add(self.cid, 2)
        chk = self.checkout(pay=False)
        self.assertEqual(self.client.get("/basket").status_code, 200)
        r = self.client.get(f"/checkout/{chk}/cancel", follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("not been charged", page)
        self.assertIn("Test Prize", page)                              # basket is back
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "expired")

    def test_missing_answer_gives_clear_error(self):
        r = self.post("/basket/add", {"slug": self.slug(self.cid), "quantity": "1"}, follow_redirects=True)
        self.assertIn("Choose an answer", r.get_data(as_text=True))

    def test_order_detail_and_isolation(self):
        self.add(self.cid, 2)
        chk = self.checkout()
        page = self.client.get(f"/account/orders/{chk}").get_data(as_text=True)
        self.assertIn(f"Order #{chk}", page)
        self.assertIn("Test Prize", page)
        self.assertIn(f"/account/orders/{chk}", self.client.get("/account?tab=transactions").get_data(as_text=True))
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self.assertEqual(other.get(f"/account/orders/{chk}").status_code, 404)

    def test_contact_form_validates_and_rate_limits(self):
        c = self.app.test_client()
        r = self.post("/contact", {"name": "", "email": "bad", "topic": "x", "message": "hi"}, client=c)
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn("Enter your name", html)
        self.assertIn('aria-invalid="true"', html)
        good = {"name": "Jo Bloggs", "email": "jo@example.org", "topic": "A payment", "message": "Where is my order please?"}
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            r = self.post("/contact", good, client=c)
        self.assertIn("sent=1", r.headers["Location"])
        self.assertIn("Contact form from jo@example.org", "\n".join(logs.output))
        self.post("/contact", good, client=c)
        self.post("/contact", good, client=c)
        self.assertEqual(self.post("/contact", good, client=c).status_code, 429)
        r = self.post("/contact", dict(good, website="spam"), client=self.app.test_client())   # honeypot
        self.assertEqual(r.status_code, 302)

    def test_error_pages_have_a_way_out(self):
        html = self.client.get("/c/does-not-exist").get_data(as_text=True)
        self.assertIn("Page not found", html)
        self.assertIn("/competitions", html)
        c = self.app.test_client()
        c.get("/login")
        r = c.post("/basket/add", data={"slug": "x"})               # no CSRF token
        self.assertEqual(r.status_code, 400)
        self.assertIn("Form expired", r.get_data(as_text=True))

    def test_sitemap_keeps_recent_results_and_drops_cancelled(self):
        cancelled = self.make_comp("Gone Prize")
        self.post(f"/admin/competitions/{cancelled}/status", {"action": "cancel"})
        xml = self.client.get("/sitemap.xml").get_data(as_text=True)
        self.assertIn(self.slug(self.cid), xml)
        self.assertNotIn(self.slug(cancelled), xml)
        self.assertIn("/how-it-works", xml)
        page = self.app.test_client().get(f"/c/{self.slug(cancelled)}").get_data(as_text=True)
        self.assertIn('content="noindex"', page)
        self.assertIn("refunded in full", page)



class DeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.player = self.app.test_client()
        self.signup("p@example.com", client=self.player)

    def test_delete_unsold_and_refuse_unrefunded(self):
        empty = self.make_comp("Empty Prize")
        sold = self.make_comp("Sold Prize", instant=[{"title": "£1 Cash", "value": "1", "type": "cash", "quantity": "3"}])
        self.add(sold, 2, client=self.player)
        self.checkout(client=self.player)
        r = self.post(f"/admin/competitions/{empty}/delete", {"confirm": "nope"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE id=?", empty), 1)    # needs DELETE typed
        self.post(f"/admin/competitions/{empty}/delete", {"confirm": "DELETE"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE id=?", empty), 0)
        r = self.post(f"/admin/competitions/{sold}/delete", {"confirm": "DELETE"}, follow_redirects=True)
        self.assertIn("Cancel it first", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE id=?", sold), 1)
        self.post(f"/admin/competitions/{sold}/status", {"action": "cancel"})               # refunds to cash
        self.post(f"/admin/competitions/{sold}/delete", {"confirm": "DELETE"})
        db = self.db()
        for t in ("competitions", "tickets", "orders", "instant_prizes", "checkouts"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0, t)
        pid = self.q("SELECT id FROM users WHERE email='p@example.com'")
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=? AND reason LIKE 'Refund%'", pid), 500)
        self.assertIn("p@example.com", self.player.get("/account?tab=transactions").get_data(as_text=True))  # still works

    def test_bulk_delete_and_start_fresh(self):
        a, b = self.make_comp("A"), self.make_comp("B")
        self.post("/admin/games/starter", {"days": "30"})
        self.post("/admin/promos", {"code": "X1", "percent": "10", "fixed": "0", "min_spend": "0", "per_user": "1"})
        self.add(b, 1, client=self.player)
        self.checkout(client=self.player)
        r = self.post("/admin/competitions/delete-selected", {"cid": [str(a), str(b)]}, follow_redirects=True)
        self.assertIn("Not deleted", r.get_data(as_text=True))                             # B has paid entries
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE id IN (?,?)", a, b), 1)
        self.assertEqual(self.client.get("/admin/start-fresh").status_code, 200)
        self.post("/admin/start-fresh", {"competitions": "games", "confirm": "nope"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE game_type!=''"), 5)
        self.post("/admin/start-fresh", {"competitions": "games", "confirm": "RESET"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE game_type!=''"), 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions"), 1)
        backups = os.listdir(os.path.join(self.tmp, "backups"))
        self.assertTrue(any(f.startswith("before-reset-") for f in backups))
        self.post("/admin/start-fresh", {"accounts": "1", "promos": "1", "confirm": "reset"})
        for t in ("competitions", "orders", "checkouts", "credit_ledger", "promo_codes", "tickets"):
            self.assertEqual(self.q(f"SELECT COUNT(*) FROM {t}"), 0, t)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users"), 1)                         # only the admin
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.app.test_client().get("/admin/start-fresh").status_code, 404)  # admins only



class CreateTests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")

    def base(self, **kw):
        d = {"title": "Thing", "description": "A brand new prize, delivered free to your door.", "ends_at": "2099-01-01T20:00", "category": "cash",
             "ticket_price": "0.50", "max_tickets": "500", "max_per_user": "50", "question": "2+2?",
             "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b"}
        d.update(kw)
        return d

    def last(self):
        return self.db().execute("SELECT * FROM competitions ORDER BY id DESC").fetchone()

    def test_game_with_prize_table_in_one_go(self):
        for k in ("draw", "game", "free"):
            self.assertEqual(self.client.get(f"/admin/competitions/new?kind={k}").status_code, 200)
        r = self.post("/admin/competitions/new", self.base(kind="game", game_type="spin", title="Big Spin",
                      prize_table="1, £50 Cash, 50, cash\n10, £1 Site Credit, 1, credit\n2, AirPods, 129, physical"))
        c = self.last()
        self.assertEqual((c["game_type"], c["status"], c["free_daily"]), ("spin", "draft", 0))
        rows = self.db().execute("SELECT prize_type, COUNT(*), credit_amount FROM instant_prizes WHERE competition_id=? "
                                 "GROUP BY title ORDER BY value DESC", (c["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("physical", 2, 0), ("cash", 1, 5000), ("credit", 10, 100)])
        self.post(f"/admin/competitions/{c['id']}/status", {"action": "publish"})
        self.assertEqual(self.last()["status"], "live")

    def test_bad_prize_line_and_game_needs_prizes(self):
        r = self.post("/admin/competitions/new", self.base(kind="game", game_type="box", prize_table="lots, prize"),
                      follow_redirects=True)
        self.assertIn("Prize line 1", r.get_data(as_text=True))
        self.post("/admin/competitions/new", self.base(kind="game", game_type="box", title="Empty Box"))
        c = self.last()
        r = self.post(f"/admin/competitions/{c['id']}/status", {"action": "publish"}, follow_redirects=True)
        self.assertIn("Add prizes", r.get_data(as_text=True))
        self.assertEqual(self.last()["status"], "draft")
        self.post(f"/admin/competitions/{c['id']}/instant", {"random_table": "1"})
        self.assertGreater(self.q("SELECT COUNT(*) FROM instant_prizes WHERE competition_id=?", c["id"]), 1)
        self.post(f"/admin/competitions/{c['id']}/instant", {"prize_table": "3, Bonus Fiver, 5, cash"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM instant_prizes WHERE competition_id=? AND title='Bonus Fiver'", c["id"]), 3)

    def test_free_daily_from_form(self):
        self.post("/admin/competitions/new", self.base(kind="free", game_type="scratch", title="Free Scratch",
                  ticket_price="", question="", answer_a="", answer_b="", answer_c="", max_tickets="5000",
                  prize_table="1, £20 Cash, 20, cash"))
        c = self.last()
        self.assertEqual((c["free_daily"], c["ticket_price"], c["game_type"]), (1, 0, "scratch"))
        self.post(f"/admin/competitions/{c['id']}/status", {"action": "publish"})
        self.assertIn("Free Scratch", self.client.get("/instant-wins").get_data(as_text=True))

    def test_schedule_and_unpublish(self):
        from app import services
        self.post("/admin/competitions/new", self.base(kind="draw", title="Later Prize", starts_at="2098-06-01T10:00"))
        c = self.last()
        self.post(f"/admin/competitions/{c['id']}/status", {"action": "schedule"})
        c = self.last()
        self.assertEqual((c["status"], c["scheduled"]), ("draft", 1))
        self.assertEqual(self.client.get(f"/c/{c['slug']}").status_code, 404)              # not public yet
        db = self.db()
        db.execute("UPDATE competitions SET starts_at='2000-01-01T00:00:00Z' WHERE id=?", (c["id"],))
        self.jobs_due()
        self.client.get("/")                                                                  # any visit launches it
        self.assertEqual((self.last()["status"], self.last()["scheduled"]), ("live", 0))
        self.post(f"/admin/competitions/{c['id']}/status", {"action": "unpublish"})
        self.assertEqual(self.last()["status"], "draft")
        r = self.post("/admin/competitions/new", self.base(title="Bad", starts_at="2099-02-01T10:00"), follow_redirects=True)
        self.assertIn("before the closing time", r.get_data(as_text=True))

    def test_unpublish_refused_once_entered(self):
        cid = self.make_comp()
        self.add(cid, 1)
        self.checkout()
        r = self.post(f"/admin/competitions/{cid}/status", {"action": "unpublish"}, follow_redirects=True)
        self.assertIn("can&#39;t go back to draft", r.get_data(as_text=True))
        r = self.post(f"/admin/competitions/{cid}/edit", self.base(kind="draw", title="Renamed"), follow_redirects=True)
        self.assertEqual(self.q("SELECT title FROM competitions WHERE id=?", cid), "Renamed")
        self.assertIn("Ready to launch", self.client.get("/admin/competitions").get_data(as_text=True))



class DepositTests(Base):
    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.cid = self.make_comp(price="2.00", max_tickets=100, max_per_user=50)
        self.uid = self.q("SELECT id FROM users")
        db = self.db()
        db.execute("UPDATE users SET email_verified=1")
        db.commit()

    def deposit(self, amount):
        r = self.post("/account/deposit", {"amount": amount})
        m = re.search(r"/deposit/(\d+)/demo-pay", r.headers.get("Location", ""))
        if m:
            self.post(f"/deposit/{m.group(1)}/demo-pay")
            return int(m.group(1))
        return None

    def bal(self, kind):
        return self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind=?", self.uid, kind)

    def test_deposit_spend_and_limits_counted_once(self):
        self.assertIn("Add funds", self.client.get("/account?tab=wallet").get_data(as_text=True))
        did = self.deposit("20")
        self.assertEqual(self.bal("deposit"), 2000)
        self.assertIn("£20 added", self.client.get(f"/deposit/{did}/done").get_data(as_text=True))
        self.add(self.cid, 3)                                     # £6, paid from deposit
        chk = self.checkout(use_credit=True)
        c = self.db().execute("SELECT * FROM checkouts WHERE id=?", (chk,)).fetchone()
        self.assertEqual((c["deposit_used"], c["cash_due"], c["status"]), (600, 0, "paid"))
        self.assertEqual(self.bal("deposit"), 1400)
        from app.services import spend_summary
        with self.app.app_context():
            from app.db import get_db
            self.assertEqual(spend_summary(get_db(), self.uid)["monthly"], 2000)   # not 2600
        self.assertEqual(self.q("SELECT points FROM users WHERE id=?", self.uid), 6)
        self.post("/account/limits", {"daily": "25", "weekly": "", "monthly": "250"})
        r = self.post("/account/deposit", {"amount": "10"}, follow_redirects=True)
        self.assertIn("spending limit", r.get_data(as_text=True))
        self.assertIsNone(self.deposit("10"))

    def test_bad_amounts_unverified_and_break(self):
        for bad in ("2", "500", "abc"):
            self.assertIsNone(self.deposit(bad), bad)
        db = self.db()
        db.execute("UPDATE users SET email_verified=0")
        db.commit()
        r = self.post("/account/deposit", {"amount": "10"}, follow_redirects=True)
        self.assertIn("Confirm your email", r.get_data(as_text=True))
        db.execute("UPDATE users SET email_verified=1")
        db.commit()
        self.post("/account/exclude", {"days": "1"})
        self.assertIsNone(self.deposit("10"))

    def test_refund_unspent_and_not_withdrawable(self):
        self.deposit("10")
        self.deposit("5")
        self.add(self.cid, 4)                                     # £8 spent from deposits
        self.checkout(use_credit=True)
        r = self.post("/account/withdraw", {"amount": "5", "method": "paypal", "paypal_email": "a@b.c"}, follow_redirects=True)
        self.assertIn("more than your cash balance", r.get_data(as_text=True))
        r = self.post("/account/deposit/refund", follow_redirects=True)
        self.assertIn("£7.00 is on its way back", r.get_data(as_text=True))
        self.assertEqual(self.bal("deposit"), 0)
        self.assertEqual([tuple(x) for x in self.db().execute("SELECT amount, refunded FROM deposits ORDER BY id")],
                         [(1000, 200), (500, 500)])               # newest deposit refunded first
        r = self.post("/account/deposit/refund", follow_redirects=True)
        self.assertIn("don&#39;t have any unspent", r.get_data(as_text=True))

    def test_cancelled_checkout_returns_deposit(self):
        self.deposit("10")
        self.add(self.cid, 10)                                    # £20: £10 deposit + £10 card
        chk = self.checkout(use_credit=True, pay=False)
        self.assertEqual(self.bal("deposit"), 0)
        self.client.get(f"/checkout/{chk}/cancel")
        self.assertEqual(self.bal("deposit"), 1000)

    def test_stripe_webhook_deposit_and_credit_card(self):
        from unittest import mock
        from app import payments
        for funding, want in (("debit", "paid"), ("credit", "credit_refused")):
            with mock.patch.object(payments, "enabled", return_value=True), \
                 mock.patch.object(payments, "create_checkout", return_value={"id": f"cs_d_{funding}", "url": "https://stripe.test/x"}):
                r = self.post("/account/deposit", {"amount": "10"})
            self.assertEqual(r.headers["Location"], "https://stripe.test/x")
            did = self.q("SELECT MAX(id) FROM deposits")
            body = json.dumps({"type": "checkout.session.completed", "data": {"object": {
                "id": f"cs_d_{funding}", "payment_status": "paid", "payment_intent": f"pi_{did}",
                "metadata": {"deposit_id": str(did)}}}}).encode()
            ts = int(time.time())
            sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
            with mock.patch.object(payments, "card_funding", return_value=funding), mock.patch.object(payments, "refund") as rf:
                for _ in range(2):
                    self.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
            self.assertEqual(self.q("SELECT status FROM deposits WHERE id=?", did), want)
            self.assertEqual(rf.call_count, 1 if funding == "credit" else 0)
        self.assertEqual(self.bal("deposit"), 1000)               # only the debit one, once
        self.assertEqual(self.client.get("/admin/payouts").status_code, 200)


class IntegrityTests(Base):
    """Phase 1 of the rebuild: draws, payments, postal entries and the rules the legal pages promise."""

    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.cid = self.make_comp(max_tickets=20, max_per_user=5)
        self.p = self.app.test_client()
        self.signup("player@example.com", client=self.p, name="Pat Player")
        self.uid = self.q("SELECT id FROM users WHERE email='player@example.com'")

    def close(self, cid=None):
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z' WHERE id=?", (cid or self.cid,))
        db.commit()

    def postal(self, **kw):
        d = {"name": "Post Person", "email": "post@example.com", "address": "1 Road", "answer_correct": "1",
             "received": time.strftime("%Y-%m-%d")}
        d.update(kw)
        return self.post(f"/admin/competitions/{self.cid}/postal", d)

    def test_draw_snapshot_is_permanent(self):
        self.add(self.cid, 3, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        d = self.db().execute("SELECT * FROM draws WHERE competition_id=?", (self.cid,)).fetchone()
        self.assertEqual(d["entry_count"], 3)
        self.assertEqual(d["method"], "manual")
        self.assertEqual(sorted(json.loads(d["entries"])), json.loads(d["entries"]))
        self.assertEqual(d["winning_number"], json.loads(d["entries"])[d["winning_index"]])
        db = self.db()
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE draws SET winning_number=1")
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE competitions SET winner_ticket_id=NULL WHERE id=?", (self.cid,))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM audit_log")
        self.assertGreater(self.q("SELECT COUNT(*) FROM audit_log WHERE action='draw.run'"), 0)
        r = self.post(f"/admin/competitions/{self.cid}/delete", {"confirm": "DELETE"}, follow_redirects=True)
        self.assertIn("permanent record", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM competitions WHERE id=?", self.cid), 1)

    def test_sell_out_does_not_bring_draw_forward(self):
        cid = self.make_comp("Tiny", max_tickets=2, max_per_user=2)
        self.add(cid, 2, client=self.p)
        self.checkout(client=self.p)
        r = self.post(f"/admin/competitions/{cid}/draw", follow_redirects=True)
        self.assertIn("after the advertised closing time", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", cid), "live")

    def test_double_click_creates_one_checkout(self):
        self.add(self.cid, 2, client=self.p)
        html = self.p.get("/basket").get_data(as_text=True)
        idem = re.search(r'name="idem" value="([^"]+)"', html).group(1)
        r1 = self.post("/basket/checkout", {"idem": idem}, client=self.p)
        r2 = self.post("/basket/checkout", {"idem": idem}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts"), 1)
        self.assertIn("/checkout/", r2.headers["Location"])
        self.assertEqual(self.p.get(r2.headers["Location"]).status_code, 303)    # straight on to the one payment page
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 2)

    def _webhook(self, chk, amount):
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "id": "cs_x", "payment_status": "paid", "amount_total": amount, "metadata": {"checkout_id": str(chk)}}}}).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return self.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})

    def test_wrong_amount_is_never_fulfilled(self):
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        self._webhook(chk, 1)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "needs_refund")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        self.assertIn("couldn't complete this order", self.p.get(f"/checkout/{chk}/done").get_data(as_text=True))

    def test_paying_for_a_cancelled_competition_is_refunded(self):
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        self.post(f"/admin/competitions/{self.cid}/status", {"action": "cancel"})
        self._webhook(chk, 500)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "needs_refund")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 0)

    def test_cancellation_refunds_each_part_as_it_was_paid(self):
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "2", "kind": "credit", "reason": "Promo"})
        self.post("/admin/promos", {"code": "HALF", "percent": "50", "per_user": "1"})
        self.add(self.cid, 4, client=self.p)                     # 4 x £2.50 = £10, promo -> £5, credit £2, card £3
        self.checkout(client=self.p, promo="HALF", use_credit=True)
        self.post(f"/admin/competitions/{self.cid}/status", {"action": "cancel"})
        db = self.db()
        self.assertEqual(db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='credit' "
                                    "AND reason LIKE 'Refund%'", (self.uid,)).fetchone()[0], 200)
        self.assertEqual(db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='cash' "
                                    "AND reason LIKE 'Refund%'", (self.uid,)).fetchone()[0], 300)
        self.post(f"/admin/competitions/{self.cid}/status", {"action": "cancel"})        # can't double refund
        self.assertEqual(self.q("SELECT COUNT(*) FROM credit_ledger WHERE reason LIKE 'Refund%'"), 2)

    def test_postal_late_wrong_and_limits(self):
        self.close()
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2099-01-01T00:00:00Z' WHERE id=?", (self.cid,))
        db.commit()
        self.postal(answer_correct="0")
        self.postal(dob="2015-01-01")
        r = self.post(f"/admin/competitions/{self.cid}/postal", {"name": "N", "email": "n@example.com", "address": "A",
                                                                  "answer_correct": "1", "received": "2999-01-01"},
                      follow_redirects=True)
        self.assertIn("can&#39;t be in the future", r.get_data(as_text=True))
        reasons = [r[0] for r in self.db().execute("SELECT reject_reason FROM postal_entries ORDER BY id")]
        self.assertEqual(reasons, ["Wrong answer", "Under 18"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        # matched to an account by email: counts towards the same per-person limit as paid entries
        self.add(self.cid, 4, client=self.p)
        self.checkout(client=self.p)
        self.postal(email="PLAYER@example.com", name="Pat Player")
        self.postal(email="player@example.com", name="Pat Player")
        rows = self.db().execute("SELECT status, reject_reason, user_id FROM postal_entries WHERE email='player@example.com' "
                                 "ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("accepted", None, self.uid), ("rejected", "Per-person limit of 5 reached", self.uid)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE user_id=?", self.uid), 5)
        self.add(self.cid, 1, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE user_id=?", self.uid), 5)
        # no account: limit counted by email
        for _ in range(6):
            self.postal()
        self.assertEqual(self.q("SELECT COUNT(*) FROM postal_entries WHERE email='post@example.com' AND status='accepted'"), 5)
        self.assertIn("Per-person limit", self.client.get(f"/admin/competitions/{self.cid}").get_data(as_text=True))

    def test_postal_after_closing_is_rejected(self):
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2020-06-01T10:00:00Z' WHERE id=?", (self.cid,))
        db.commit()
        self.postal(received="2020-06-02")
        self.postal(received="2020-06-01")                     # closing day counts as on time
        rows = [tuple(r) for r in self.db().execute("SELECT status, reject_reason FROM postal_entries ORDER BY id")]
        self.assertEqual(rows, [("rejected", "Arrived after the competition closed"), ("accepted", None)])

    def test_lowering_a_limit_cancels_a_pending_raise(self):
        self.post("/account/limits", {"daily": "", "weekly": "", "monthly": "50"}, client=self.p)
        self.post("/account/limits", {"daily": "", "weekly": "", "monthly": "200"}, client=self.p)
        self.assertEqual(self.q("SELECT pending_limit FROM users WHERE id=?", self.uid), 20000)
        self.assertEqual(self.q("SELECT monthly_limit FROM users WHERE id=?", self.uid), 5000)
        self.post("/account/limits", {"daily": "", "weekly": "", "monthly": "40"}, client=self.p)
        self.assertIsNone(self.q("SELECT pending_limit FROM users WHERE id=?", self.uid))
        self.assertEqual(self.q("SELECT monthly_limit FROM users WHERE id=?", self.uid), 4000)

    def test_break_releases_checkouts_in_progress(self):
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        self.post("/account/exclude", {"days": "1"}, client=self.p)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "expired")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        self.post(f"/checkout/{chk}/demo-pay", client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 0)

    def test_password_change_signs_out_other_devices(self):
        other = self.app.test_client()
        self.post("/login", {"email": "player@example.com", "password": "supersecret123"}, client=other)
        self.assertEqual(other.get("/account").status_code, 200)
        self.post("/account/profile", {"current_password": "supersecret123", "new_password": "anothersecret1"}, client=self.p)
        self.assertEqual(self.p.get("/account").status_code, 200)       # this device stays in
        self.assertEqual(other.get("/account").status_code, 302)        # the other one is signed out

    def test_staff_cannot_touch_money(self):
        self.post(f"/admin/users/{self.uid}", {"action": "admin", "role": "competitions"})
        self.assertEqual(self.p.get("/admin/").status_code, 200)
        self.assertEqual(self.p.get(f"/admin/competitions/{self.cid}").status_code, 200)
        for url in ("/admin/payouts", "/admin/users", f"/admin/users/{self.uid}", "/admin/start-fresh", "/admin/promos"):
            self.assertEqual(self.p.get(url).status_code, 302, url)
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "50", "kind": "cash"}, client=self.p)
        self.assertEqual(self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", self.uid), 0)
        self.assertGreater(self.q("SELECT COUNT(*) FROM audit_log WHERE action='user.admin_access'"), 0)

    def test_winner_story_needs_permission(self):
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        self.post(f"/admin/competitions/{self.cid}/winner", {"winner_quote": "Amazing!"})
        self.assertNotIn("Amazing!", self.client.get("/winners").get_data(as_text=True))
        self.post(f"/admin/competitions/{self.cid}/winner", {"winner_quote": "Amazing!", "consent": "1"})
        self.assertIn("Amazing!", self.client.get("/winners").get_data(as_text=True))

    def test_basket_kept_through_sign_up(self):
        guest = self.app.test_client()
        self.add(self.cid, 3, client=guest)
        self.signup("newbie@example.com", client=guest, name="New Person")
        self.assertIn("3 × £2.50", guest.get("/basket").get_data(as_text=True))

    def test_payment_provider_down_keeps_basket(self):
        from unittest import mock
        from app import payments
        self.app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
        self.add(self.cid, 2, client=self.p)
        with mock.patch.object(payments, "create_checkout", side_effect=RuntimeError("down")):
            r = self.post("/basket/checkout", {}, client=self.p, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("you have not been charged", html)
        self.assertIn("2 × £2.50", html)                                   # basket still there
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)            # numbers released

    def test_draw_waits_for_checkouts_in_progress(self):
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p, pay=False)
        self.close()
        r = self.post(f"/admin/competitions/{self.cid}/draw", follow_redirects=True)
        self.assertIn("still in progress", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "live")

    def test_break_blocks_free_play_and_deposits(self):
        db = self.db()
        db.execute("UPDATE users SET email_verified=1")
        db.commit()
        self.post("/admin/games/free-daily", {"kind": "spin"})
        slug = self.q("SELECT slug FROM competitions WHERE free_daily=1")
        self.post("/account/exclude", {"days": "7"}, client=self.p)
        self.post(f"/free-play/{slug}", client=self.p)
        self.post("/account/deposit", {"amount": "10"}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE user_id=?", self.uid), 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM deposits WHERE user_id=?", self.uid), 0)

    def test_forgot_password_is_rate_limited(self):
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            for _ in range(5):
                self.post("/forgot", {"email": "player@example.com"})
        self.assertEqual(sum("Reset your password" in line for line in logs.output), 3)


class PlatformBase(Base):
    """Shared set-up: an admin, a live competition and a player."""

    def setUp(self):
        super().setUp()
        self.signup("admin@example.com", name="Admin Person")
        self.cli("make-admin", "admin@example.com")
        self.cid = self.make_comp(max_tickets=20, max_per_user=10)
        self.p = self.app.test_client()
        self.signup("player@example.com", client=self.p, name="Pat Player")
        self.uid = self.q("SELECT id FROM users WHERE email='player@example.com'")

    def close(self, cid=None):
        db = self.db()
        db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z' WHERE id=?", (cid or self.cid,))
        db.commit()

    def run_jobs(self):
        self.jobs_due()
        self.client.get("/")

    def role(self, r, client=None):
        self.post(f"/admin/users/{self.uid}", {"action": "admin", "role": r})


class PlatformTests(PlatformBase):
    """Phase 2 (platform): roles, MFA, sessions, lifecycle, redraws, claims, notifications, operations tools."""

    def test_mfa_required_for_admins(self):
        from app.security import totp
        self.app.config["ADMIN_MFA"] = True
        r = self.client.get("/admin/")
        self.assertIn("/admin/mfa/setup", r.headers["Location"])
        html = self.client.get("/admin/mfa/setup").get_data(as_text=True)
        secret = re.search(r'secret=([A-Z2-7]+)', html).group(1)
        self.post("/admin/mfa/setup", {"code": "000000"})
        self.assertEqual(self.q("SELECT mfa_enabled FROM users WHERE email='admin@example.com'"), 0)
        r = self.post("/admin/mfa/setup", {"code": totp(secret)})
        codes = re.findall(r"([0-9a-f]{6}-[0-9a-f]{6})", r.get_data(as_text=True))
        self.assertEqual(len(codes), 8)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        other = self.app.test_client()
        self.post("/login", {"email": "admin@example.com", "password": "supersecret123"}, client=other)
        self.assertIn("/admin/mfa/", other.get("/admin/payouts").headers["Location"])
        self.post("/admin/mfa/", {"code": "123456"}, client=other)
        self.assertEqual(other.get("/admin/payouts").status_code, 302)
        self.post("/admin/mfa/", {"code": codes[0]}, client=other)                  # recovery code, once
        self.assertEqual(other.get("/admin/payouts").status_code, 200)
        third = self.app.test_client()
        self.post("/login", {"email": "admin@example.com", "password": "supersecret123"}, client=third)
        self.post("/admin/mfa/", {"code": codes[0]}, client=third)                  # already used
        self.assertEqual(third.get("/admin/payouts").status_code, 302)
        self.assertGreater(self.q("SELECT COUNT(*) FROM audit_log WHERE action='admin.mfa_failed'"), 0)

    def test_roles_limit_what_staff_can_do(self):
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        self.close()
        for role, allowed, denied in (
                ("support", ["/admin/", "/admin/postal", "/admin/cases", f"/admin/customers/{self.uid}/timeline"],
                 ["/admin/payouts", "/admin/finance", "/admin/prizes", "/admin/start-fresh", "/admin/settings"]),
                ("finance", ["/admin/finance", "/admin/payouts", "/admin/flags"],
                 ["/admin/postal", "/admin/prizes", "/admin/competitions/new", "/admin/start-fresh"]),
                ("competitions", ["/admin/competitions/new", "/admin/prizes", "/admin/postal"],
                 ["/admin/payouts", "/admin/finance", "/admin/users", "/admin/start-fresh"])):
            self.role(role)
            for url in allowed:
                self.assertEqual(self.p.get(url).status_code, 200, (role, url))
            for url in denied:
                self.assertEqual(self.p.get(url).status_code, 302, (role, url))
        self.role("support")
        self.post(f"/admin/competitions/{self.cid}/draw", client=self.p)
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "live")    # support can't draw

    def test_sign_out_other_devices(self):
        other = self.app.test_client()
        self.post("/login", {"email": "player@example.com", "password": "supersecret123"}, client=other)
        self.assertIn("Where you", self.p.get("/account?tab=profile").get_data(as_text=True))
        self.post("/account/sessions/revoke", {"sid": "others"}, client=self.p)
        self.assertEqual(other.get("/account").status_code, 302)
        self.assertEqual(self.p.get("/account").status_code, 200)

    def test_closing_freezes_the_entry_list(self):
        self.add(self.cid, 3, client=self.p)
        self.checkout(client=self.p)
        self.post(f"/admin/competitions/{self.cid}/postal", {"name": "Post Person", "email": "post@example.com",
                  "address": "1 Road", "answer_correct": "1", "mode": "receive",
                  "received": time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))})
        db = self.db()
        db.execute("UPDATE competitions SET ends_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 minute') WHERE id=?", (self.cid,))
        db.commit()
        self.run_jobs()
        self.assertIsNone(self.q("SELECT locked_at FROM competitions WHERE id=?", self.cid))   # envelope still waiting
        r = self.post(f"/admin/competitions/{self.cid}/draw", follow_redirects=True)
        self.assertIn("Postal entries are still waiting", r.get_data(as_text=True))
        pid = self.q("SELECT id FROM postal_entries")
        self.post(f"/admin/postal/{pid}", {"action": "approve"})
        self.assertEqual(self.q("SELECT status FROM postal_entries"), "accepted")
        self.run_jobs()
        self.assertIsNotNone(self.q("SELECT locked_at FROM competitions WHERE id=?", self.cid))
        snap = self.db().execute("SELECT * FROM entry_snapshots").fetchone()
        self.assertEqual((snap["entry_count"], snap["paid_count"], snap["postal_count"]), (4, 3, 1))
        db = self.db()
        for sql in ("INSERT INTO tickets (competition_id, number, status, created_at) VALUES (%d, 19, 'issued', 'x')" % self.cid,
                    "DELETE FROM tickets WHERE competition_id=%d" % self.cid,
                    "UPDATE tickets SET user_id=NULL WHERE competition_id=%d" % self.cid,
                    "UPDATE competitions SET locked_at=NULL WHERE id=%d" % self.cid):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                db.execute(sql)
        self.assertIn("Entry list frozen", self.client.get(f"/admin/competitions/{self.cid}").get_data(as_text=True))

    def test_redraw_and_prize_claim(self):
        other = self.app.test_client()
        self.signup("second@example.com", client=other, name="Second Person")
        self.add(self.cid, 5, client=self.p)
        self.checkout(client=self.p)
        self.add(self.cid, 5, client=other)
        self.checkout(client=other)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        first = self.q("SELECT winning_number FROM draws")
        claim = self.q("SELECT id FROM prize_claims")
        self.post(f"/admin/prizes/{claim}", {"status": "contacted", "note": "Called, left voicemail"})
        self.assertEqual(self.q("SELECT status FROM prize_claims WHERE id=?", claim), "contacted")
        r = self.post(f"/admin/competitions/{self.cid}/redraw", {"reason": "no", "confirm": "REDRAW"}, follow_redirects=True)
        self.assertIn("Give the reason", r.get_data(as_text=True))
        self.post(f"/admin/competitions/{self.cid}/redraw", {"reason": "Winner failed age verification", "confirm": "REDRAW"})
        rows = self.db().execute("SELECT method, winning_number, reason FROM draws ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["method"], "redraw")
        self.assertNotEqual(rows[1]["winning_number"], first)
        self.assertEqual(self.q("SELECT status FROM prize_claims WHERE id=?", claim), "forfeited")
        # anyone can recompute the redraw with the published formula
        snap = json.loads(self.q("SELECT entries FROM entry_snapshots"))
        remaining = [n for n in snap if n != first]
        seed = self.q("SELECT seed FROM competitions WHERE id=?", self.cid)
        digest = hashlib.sha256(",".join(map(str, sorted(remaining))).encode()).hexdigest()
        i = int(hmac.new(seed.encode(), f"{digest}:redraw:2".encode(), hashlib.sha256).hexdigest(), 16) % len(remaining)
        self.assertEqual(rows[1]["winning_number"], sorted(remaining)[i])
        db = self.db()
        with self.assertRaises(sqlite3.DatabaseError):          # result only changes through a recorded redraw
            db.execute("UPDATE competitions SET winner_ticket_id=(SELECT MIN(t.id) FROM tickets t, competitions c "
                       "WHERE c.id=? AND t.id!=c.winner_ticket_id) WHERE id=?", (self.cid, self.cid))
        new_claim = self.q("SELECT MAX(id) FROM prize_claims")
        for st in ("contacted", "verification", "verified", "chosen", "fulfilment", "delivered"):
            self.post(f"/admin/prizes/{new_claim}", {"status": st})
        self.assertIsNotNone(self.q("SELECT completed_at FROM competitions WHERE id=?", self.cid))
        self.assertEqual(self.q("SELECT COUNT(*) FROM claim_events WHERE claim_id=?", new_claim), 7)
        self.assertIn("Redraw 1", self.client.get(f"/admin/competitions/{self.cid}").get_data(as_text=True))

    def test_notifications_are_recorded_once(self):
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "id": "cs_1", "payment_status": "paid", "amount_total": 500, "metadata": {"checkout_id": str(chk)}}}}).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        for _ in range(3):
            self.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM notifications WHERE dedupe_key=?", f"order:{chk}"), 1)
        self.assertIn("1 new", self.p.get("/").get_data(as_text=True))
        self.assertIn("Entries confirmed", self.p.get("/account/notifications").get_data(as_text=True))
        self.assertIn("1 new", self.p.get("/").get_data(as_text=True))                        # viewing isn't reading
        nid = self.q("SELECT id FROM notifications WHERE dedupe_key=?", f"order:{chk}")
        r = self.p.get(f"/account/notifications/{nid}/open")                                    # opening the link reads it
        self.assertIn(f"/account/orders/{chk}", r.headers["Location"])
        self.assertNotIn("1 new", self.p.get("/").get_data(as_text=True))
        other = self.app.test_client()
        self.signup("snoop@example.com", client=other)
        self.assertEqual(other.get(f"/account/notifications/{nid}/open").status_code, 404)

    def test_finance_reconciles(self):
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "1", "kind": "credit", "reason": "Promo"})
        self.post("/admin/promos", {"code": "TEN", "percent": "10", "per_user": "1"})
        self.add(self.cid, 4, client=self.p)
        self.checkout(client=self.p, promo="TEN", use_credit=True)
        html = self.client.get("/admin/finance").get_data(as_text=True)
        self.assertIn("✓ balances", html)
        csv_ = self.client.get("/admin/finance/payments.csv").get_data(as_text=True)
        self.assertTrue(csv_.startswith("date_utc,type,dbx_ref,stripe_session"))

    def test_timeline_flags_cases_health(self):
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        html = self.client.get(f"/admin/customers/{self.uid}/timeline").get_data(as_text=True)
        self.assertIn("Order paid", html)
        self.assertIn("Account created", html)
        self.assertEqual(self.q("SELECT COUNT(*) FROM audit_log WHERE action='customer.viewed'"), 1)
        db = self.db()
        db.execute("UPDATE users SET phone='07700900000'")
        db.commit()
        self.post("/admin/flags", {"run": "1"})
        self.assertIn("Shared phone number", self.client.get("/admin/flags").get_data(as_text=True))
        self.post("/contact", {"name": "Pat Player", "email": "player@example.com", "topic": "A payment",
                               "message": "Where is my refund please?"}, client=self.p)
        case = self.q("SELECT id FROM cases")
        self.assertEqual(self.q("SELECT user_id FROM cases"), self.uid)
        self.post(f"/admin/cases/{case}", {"action": "reply", "body": "Sorted — it's in your wallet."})
        self.assertEqual(self.q("SELECT status FROM cases"), "waiting")
        self.assertEqual(self.client.get("/admin/health").status_code, 200)
        self.assertEqual(self.client.get("/healthz").get_json()["ok"], True)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_payments_paused_and_maintenance(self):
        self.post("/admin/settings", {"site_status": "payments_paused", "site_status_message": "Back at 9pm"})
        self.add(self.cid, 1, client=self.p)
        r = self.post("/basket/checkout", {}, client=self.p, follow_redirects=True)
        self.assertIn("Back at 9pm", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts"), 0)
        self.post("/admin/settings", {"site_status": "maintenance", "site_status_message": "Upgrading"})
        self.assertEqual(self.p.get("/competitions").status_code, 503)
        self.assertEqual(self.p.get("/account").status_code, 200)
        self.assertEqual(self.client.get("/competitions").status_code, 200)                 # admins still see it
        self.post("/admin/settings", {"site_status": "ok"})
        self.assertEqual(self.p.get("/competitions").status_code, 200)

    def test_provider_failures_pause_payments(self):
        from unittest import mock
        from app import payments
        self.app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
        with mock.patch.object(payments, "create_checkout", side_effect=RuntimeError("down")):
            for _ in range(3):
                self.add(self.cid, 1, client=self.p)
                self.post("/basket/checkout", {}, client=self.p)
        self.add(self.cid, 1, client=self.p)
        r = self.post("/basket/checkout", {}, client=self.p, follow_redirects=True)
        self.assertIn("temporarily unavailable", r.get_data(as_text=True))


class CustomerFeatureTests(PlatformBase):
    """Results archive, search, watchlist, own-ticket search, points history and referrals."""

    def test_results_archive_is_permanent(self):
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        db = self.db()
        db.execute("UPDATE competitions SET purging=0")    # nothing; drawn_at is locked by trigger
        db.commit()
        html = self.client.get("/results").get_data(as_text=True)
        self.assertIn("Test Prize", html)
        self.assertIn(f"#{self.q('SELECT winning_number FROM draws')}", html)
        self.assertIn(f"/c/{self.slug(self.cid)}", self.client.get("/sitemap.xml").get_data(as_text=True))
        page = self.client.get(f"/c/{self.slug(self.cid)}").get_data(as_text=True)
        self.assertIn("Entry list frozen", page)
        self.assertEqual(self.client.get("/winners?tab=results").status_code, 301)

    def test_search_and_watchlist(self):
        html = self.client.get("/search?q=test").get_data(as_text=True)
        self.assertIn("Test Prize", html)
        self.assertIn("Nothing matches", self.client.get("/search?q=zzzz").get_data(as_text=True))
        self.post(f"/watch/{self.slug(self.cid)}", {"remind_close": "1"}, client=self.p)
        self.assertIn("Saved competitions", self.p.get("/account").get_data(as_text=True))
        db = self.db()
        db.execute("UPDATE competitions SET ends_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','+3 hours') WHERE id=?", (self.cid,))
        db.commit()
        self.run_jobs()
        self.run_jobs()
        self.assertEqual(self.q("SELECT COUNT(*) FROM notifications WHERE kind='reminder'"), 1)     # once, not every run
        self.assertIsNotNone(self.q("SELECT email_status FROM notifications WHERE kind='reminder'"))  # they asked for it
        self.assertIn("/unsubscribe/", self.q("SELECT body FROM notifications WHERE kind='reminder'"))
        self.post(f"/watch/{self.slug(self.cid)}", {"action": "remove"}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM watchlist"), 0)

    def test_find_my_ticket_and_public_list(self):
        self.add(self.cid, numbers="7,11", client=self.p)
        self.checkout(client=self.p)
        html = self.p.get("/account?tab=entries&tq=11").get_data(as_text=True)
        self.assertIn("1 competition with", html)
        self.assertIn("hold “99”", self.p.get("/account?tab=entries&tq=99").get_data(as_text=True).replace("don&#39;t ", ""))
        pub = self.p.get(f"/c/{self.slug(self.cid)}/entries").get_data(as_text=True)
        self.assertIn("Yours", pub)
        guest = self.app.test_client().get(f"/c/{self.slug(self.cid)}/entries").get_data(as_text=True)
        self.assertNotIn("player@example.com", guest)
        self.assertIn("Pat P.", guest)
        self.assertNotIn("Pat Player", guest)                                           # only first name + initial

    def test_points_history_adds_up(self):
        self.add(self.cid, 4, client=self.p)              # £10 by card -> 10 points
        self.checkout(client=self.p)
        rows = self.db().execute("SELECT points, reason FROM points_ledger WHERE user_id=?", (self.uid,)).fetchall()
        self.assertEqual([r[0] for r in rows], [10])
        self.assertIn("£10.00 paid", rows[0][1])
        self.assertEqual(self.q("SELECT SUM(points) FROM points_ledger WHERE user_id=?", self.uid), self.q("SELECT points FROM users WHERE id=?", self.uid))
        self.assertIn("Points history", self.p.get("/account?tab=points").get_data(as_text=True))

    def test_referral_rules(self):
        code = self.q("SELECT referral_code FROM users WHERE id=?", self.uid)
        friend = self.app.test_client()
        friend.get(f"/r/{code}")
        self.signup("friend@example.com", client=friend, name="Friend Person")
        self.assertEqual(self.q("SELECT status FROM referrals"), "joined")
        self.post(f"/admin/users/{self.q('SELECT id FROM users WHERE email=?', 'friend@example.com')}",
                  {"action": "credit", "amount": "10", "kind": "credit", "reason": "Gift"})
        self.add(self.cid, 1, client=friend)
        self.checkout(client=friend, use_credit=True)       # paid entirely with credit -> not eligible
        self.assertEqual(self.q("SELECT status FROM referrals"), "not_eligible")
        self.assertIn("Not eligible", self.p.get("/account?tab=points").get_data(as_text=True))
        # same phone as the referrer is never rewarded
        db = self.db()
        db.execute("UPDATE users SET phone='07700900111' WHERE id=?", (self.uid,))
        db.commit()
        twin = self.app.test_client()
        twin.get(f"/r/{code}")
        self.post("/signup", {"name": "Twin Person", "email": "twin@example.com", "dob": "1990-01-01", "phone": "07700900111",
                              "password": "supersecret123", "agree": "1"}, client=twin)
        self.assertEqual(self.q("SELECT reason FROM referrals ORDER BY id DESC"), "Same phone number as the referrer")


class OpsTests(PlatformBase):
    """Concurrency, backups, configurable mechanics, journey counts and staging."""

    def racers(self, n, numbers="", qty=1, cid=None):
        out = []
        from app import routes
        for i in range(n):
            routes._FAILS.clear()                      # sign-up rate limit: every test client shares one IP
            c = self.app.test_client()
            self.signup(f"racer{i}@example.com", client=c)
            r = self.add(cid or self.cid, qty, numbers=numbers, client=c)
            out.append((c, self.csrf(c)))
        return out

    def race(self, clients):
        barrier = threading.Barrier(len(clients))
        results = []

        def go(c, token):
            barrier.wait()
            r = c.post("/basket/checkout", data={"csrf": token})
            m = re.search(r"/checkout/(\d+)/demo-pay", r.headers.get("Location", ""))
            if m:
                c.post(f"/checkout/{m.group(1)}/demo-pay", data={"csrf": token})
            results.append(bool(m))

        threads = [threading.Thread(target=go, args=ct) for ct in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    def test_forty_people_racing_for_one_number(self):
        clients = self.racers(40, numbers="5")
        wins = self.race(clients)
        self.assertEqual(sum(wins), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE number=5"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts WHERE status='paid'"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='held'"), 0)

    def test_stampede_never_oversells(self):
        cid = self.make_comp("Stampede", max_tickets=25, max_per_user=5)
        clients = self.racers(30, cid=cid)
        self.race(clients)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=?", cid), 25)
        self.assertEqual(self.q("SELECT COUNT(DISTINCT number) FROM tickets WHERE competition_id=?", cid), 25)
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts WHERE status='paid'"), 25)

    def test_backup_is_restore_tested(self):
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        r = self.cli("backup")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Every stored draw still recomputes", r.output)
        self.assertIn("VERIFIED", r.output)
        self.assertIsNotNone(self.q("SELECT value FROM settings WHERE key='last_backup_verified'"))
        self.assertIn("Last backup restored and verified", self.client.get("/admin/health").get_data(as_text=True))

    def test_no_question_mechanic(self):
        self.post("/admin/competitions/new", {
            "title": "Free Draw", "description": "A brand new prize, delivered free to your door.", "ends_at": "2099-01-01T20:00", "category": "tech",
            "ticket_price": "1", "max_tickets": "10", "max_per_user": "5", "question_mode": "none"})
        cid = self.q("SELECT id FROM competitions ORDER BY id DESC")
        self.post(f"/admin/competitions/{cid}/status", {"action": "publish"})
        self.assertEqual(self.q("SELECT question_mode FROM competitions WHERE id=?", cid), "none")
        self.assertNotIn('name="answer"', self.p.get(f"/c/{self.slug(cid)}").get_data(as_text=True))
        self.post("/basket/add", {"slug": self.slug(cid), "quantity": "2"}, client=self.p)
        self.assertIsNotNone(self.checkout(client=self.p))
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=?", cid), 2)

    def test_journey_counts_have_no_identifiers(self):
        self.p.get("/")
        self.p.get(f"/c/{self.slug(self.cid)}")
        self.add(self.cid, 1, client=self.p)
        self.p.get("/basket")
        self.checkout(client=self.p)
        steps = dict(self.db().execute("SELECT step, SUM(n) FROM funnel_counts GROUP BY step").fetchall())
        for st in ("home", "competition", "add", "basket", "checkout", "paid"):
            self.assertGreaterEqual(steps.get(st, 0), 1, st)
        cols = [r[1] for r in self.db().execute("PRAGMA table_info(funnel_counts)")]
        self.assertEqual(cols, ["day", "step", "device", "comp_id", "n"])                 # nothing about the person
        self.client.get("/", headers={"User-Agent": "Googlebot/2.1"})
        self.assertIn("Customer journey", self.client.get("/admin/analytics").get_data(as_text=True))
        self.assertIn("Competition performance", self.client.get("/admin/performance").get_data(as_text=True))

    def test_staging_never_emails_customers(self):
        self.app.config["STAGING"] = True
        self.app.config["SUPPORT_EMAIL"] = "staff@example.com"
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.post("/forgot", {"email": "player@example.com"})
        out = "\n".join(logs.output)
        self.assertIn("to=staff@example.com", out)
        self.assertIn("[STAGING]", out)
        self.assertIn("STAGING", self.client.get("/").get_data(as_text=True))


class CustomerExperienceTests(PlatformBase):
    """Phase 3: dashboard, My tickets, preferences, transparency, reminders, sold-out, support centre."""

    def test_dashboard_shows_what_needs_action(self):
        html = self.p.get("/account").get_data(as_text=True)
        self.assertIn("Confirm your email", html)
        self.assertIn("When you enter a competition", html)                 # helpful empty state
        db = self.db()
        db.execute("UPDATE users SET email_verified=1")
        db.commit()
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "20", "kind": "cash", "reason": "Prize"})
        self.assertIn("£20.00 of cash winnings you can withdraw", self.p.get("/account").get_data(as_text=True))

    def test_my_tickets_filters_and_ticket_page(self):
        self.add(self.cid, numbers="3,4", client=self.p)
        chk = self.checkout(client=self.p)
        tid = self.q("SELECT id FROM tickets WHERE number=3")
        html = self.p.get("/account?tab=entries").get_data(as_text=True)
        self.assertIn(f"/account/tickets/{tid}", html)
        page = self.p.get(f"/account/tickets/{tid}").get_data(as_text=True)
        self.assertIn(f"Order #{chk}", page)
        self.assertIn("Draw pending", page)
        self.close()
        self.assertIn("Upcoming draw <span class=\"count\">1", self.p.get("/account?tab=entries").get_data(as_text=True))
        self.post(f"/admin/competitions/{self.cid}/draw")
        html = self.p.get("/account?tab=entries&show=completed").get_data(as_text=True)
        self.assertIn("Test Prize", html)
        self.assertIn("Winner", self.p.get(f"/account/tickets/{self.q('SELECT winner_ticket_id FROM competitions')}").get_data(as_text=True))
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self.assertEqual(other.get(f"/account/tickets/{tid}").status_code, 404)

    def test_preferences_record_consent_and_unsubscribe(self):
        self.post("/signup", {"name": "Mark Eting", "email": "m@example.com", "dob": "1990-01-01", "password": "supersecret123",
                              "agree": "1", "marketing": "1"}, client=self.app.test_client())
        mid = self.q("SELECT id FROM users WHERE email='m@example.com'")
        self.assertEqual((self.q("SELECT marketing FROM users WHERE id=?", mid), self.q("SELECT marketing_sms FROM users WHERE id=?", mid)), (1, 0))
        self.assertEqual(self.q("SELECT source FROM consent_log WHERE user_id=?", mid), "signup")
        self.assertEqual(self.q("SELECT marketing FROM users WHERE id=?", self.uid), 0)            # never opted in by default
        self.post("/account/preferences", {"marketing_sms": "1", "reminder_emails": "1"}, client=self.p)
        self.assertEqual(self.q("SELECT marketing_sms FROM users WHERE id=?", self.uid), 1)
        with self.app.test_request_context():
            from app.routes import unsubscribe_link
            link = unsubscribe_link(mid)
        path = link.split("localhost:5000")[-1]
        guest = self.app.test_client()
        self.assertIn("Unsubscribe", guest.get(path).get_data(as_text=True))
        self.post(path, {}, client=guest)
        self.assertEqual(self.q("SELECT marketing FROM users WHERE id=?", mid), 0)
        self.assertEqual(self.q("SELECT source FROM consent_log WHERE user_id=? ORDER BY id DESC", mid), "unsubscribe link")
        self.assertEqual(guest.get("/unsubscribe/forged").status_code, 404)

    def test_transparency_and_states(self):
        html = self.client.get("/transparency").get_data(as_text=True)
        for x in ("How draws work", "Free entry", "Results archive", "Responsible play", "Who we are"):
            self.assertIn(x, html)
        tiny = self.make_comp("Tiny", max_tickets=1, max_per_user=1)
        self.add(tiny, 1, client=self.p)
        self.checkout(client=self.p)
        page = self.client.get(f"/c/{self.slug(tiny)}").get_data(as_text=True)
        self.assertIn("Sold out — every ticket has gone", page)
        self.assertIn("Still open", page)
        self.assertIn("Test Prize", page)                                   # an alternative, never added to the basket
        self.close(tiny)
        page = self.client.get(f"/c/{self.slug(tiny)}").get_data(as_text=True)
        self.assertIn("draw pending", page)
        self.assertIn('http-equiv="refresh"', page)

    def test_support_centre(self):
        self.add(self.cid, 1, client=self.p)
        chk = self.checkout(client=self.p)
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self.add(self.cid, 1, client=other)
        other_chk = self.checkout(client=other)
        r = self.post("/support", {"topic": "Payment", "message": "I was charged twice I think", "checkout_id": str(chk),
                                   "competition_id": str(self.cid)}, client=self.p)
        case = self.q("SELECT id FROM cases ORDER BY id DESC")
        self.assertEqual((self.q("SELECT checkout_id FROM cases WHERE id=?", case), self.q("SELECT priority FROM cases WHERE id=?", case)), (chk, "high"))
        self.post("/support", {"topic": "Payment", "message": "Look at this other order", "checkout_id": str(other_chk)}, client=self.p)
        self.assertIsNone(self.q("SELECT checkout_id FROM cases ORDER BY id DESC"))          # can't attach someone else's order
        self.post(f"/admin/cases/{case}", {"action": "internal", "body": "SECRET staff note"})
        self.post(f"/admin/cases/{case}", {"action": "reply", "body": "Only one charge — the other was released."})
        view = self.p.get(f"/support/requests/{case}").get_data(as_text=True)
        self.assertIn("Only one charge", view)
        self.assertNotIn("SECRET", view)
        self.assertIn("Our support team has replied", self.p.get("/account").get_data(as_text=True))
        self.post(f"/support/requests/{case}", {"body": "Thanks!"}, client=self.p)
        self.assertEqual(self.q("SELECT status FROM cases WHERE id=?", case), "open")
        self.assertEqual(other.get(f"/support/requests/{case}").status_code, 404)
        r = self.post("/support", {"topic": "", "message": "x"}, client=self.p)
        self.assertEqual(r.status_code, 400)


class OperationsTests(PlatformBase):
    """Phase 3 operations: validators, draw readiness, tamper-evident audit, ledgers, integrity, reconciliation,
    feature flags, support desk, liability."""

    def test_prelaunch_validator_blocks_publish(self):
        self.post("/admin/competitions/new", {
            "title": "Thin", "description": "Short", "ends_at": "2099-01-01T20:00", "category": "tech",
            "ticket_price": "1", "max_tickets": "10", "max_per_user": "5", "prize_value": "100",
            "question": "2+2?", "answer_a": "3", "answer_b": "4", "answer_c": "5", "correct": "b"})
        cid = self.q("SELECT id FROM competitions ORDER BY id DESC")
        r = self.post(f"/admin/competitions/{cid}/status", {"action": "publish"}, follow_redirects=True)
        self.assertIn("Prize description", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", cid), "draft")
        html = self.client.get(f"/admin/competitions/{cid}").get_data(as_text=True)
        self.assertIn("Pre-launch checks", html)
        self.assertIn("must be fixed", html)
        self.app.config["POSTAL_ADDRESS"] = ""
        db = self.db()
        db.execute("UPDATE competitions SET description='A proper description of a lovely prize.' WHERE id=?", (cid,))
        db.commit()
        r = self.post(f"/admin/competitions/{cid}/status", {"action": "publish"}, follow_redirects=True)
        self.assertIn("Free postal entry address", r.get_data(as_text=True))
        self.app.config["POSTAL_ADDRESS"] = "PO Box 1"
        self.post(f"/admin/competitions/{cid}/status", {"action": "publish"})
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", cid), "live")

    def test_draw_readiness_blocks_and_logs_once(self):
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.run_jobs()                                         # freezes the entry list
        html = self.client.get(f"/admin/competitions/{self.cid}").get_data(as_text=True)
        self.assertIn("Draw-readiness checks", html)
        self.assertIn("all ", html)
        db = self.db()                                          # simulate someone deleting the frozen list by hand
        db.execute("DROP TRIGGER snapshots_no_delete")
        db.execute("DELETE FROM entry_snapshots")
        db.commit()
        for _ in range(2):
            r = self.post(f"/admin/competitions/{self.cid}/draw", follow_redirects=True)
        self.assertIn("Draw not run", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "live")
        self.assertEqual(self.q("SELECT COUNT(*) FROM audit_log WHERE action='draw.blocked'"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 0)

    def test_audit_chain_detects_tampering(self):
        self.skip_integrity = True
        self.post(f"/admin/users/{self.uid}", {"action": "verify"})
        html = self.client.get("/admin/audit?verify=1").get_data(as_text=True)
        self.assertIn("entries are intact", html)
        self.assertIsNone(self.q("SELECT COUNT(*) FROM audit_log WHERE row_hash IS NULL") or None)
        db = self.db()
        victim = db.execute("SELECT id FROM audit_log ORDER BY id LIMIT 1 OFFSET 1").fetchone()[0]
        db.execute("DROP TRIGGER audit_no_update")
        db.execute("UPDATE audit_log SET detail='nothing to see' WHERE id=?", (victim,))
        db.commit()
        self.assertIn(f"Chain broken at entry #{victim}", self.client.get("/admin/audit?verify=1").get_data(as_text=True))
        self.run_jobs()
        self.assertIn("Audit log chain broken", self.client.get("/admin/health").get_data(as_text=True))

    def test_old_audit_rows_are_chained_on_upgrade(self):
        from app.db import init_db
        from app.services import verify_audit_chain
        db = self.db()
        db.execute("INSERT INTO audit_log (created_at, action, detail) VALUES ('2020-01-01T00:00:00Z', 'legacy', 'before v11')")
        db.commit()
        init_db(self.app.config["DATABASE"])
        db = self.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE row_hash IS NULL").fetchone()[0], 0)
        self.assertIsNone(verify_audit_chain(db))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE audit_log SET detail='x'")

    def test_ledgers_are_append_only(self):
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "5", "kind": "credit", "reason": "Goodwill"})
        with self.app.app_context():
            from app.db import get_db
            from app.services import add_points
            add_points(get_db(), self.uid, 10, "Test points")
        db = self.db()
        for sql in ("UPDATE credit_ledger SET amount=99999", "DELETE FROM credit_ledger",
                    "UPDATE points_ledger SET points=1", "DELETE FROM points_ledger"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                db.execute(sql)
        from app.services import start_fresh
        with self.app.app_context():
            out, _ = start_fresh(wallets=True)
        self.assertGreaterEqual(out["transactions"], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM maintenance_unlock"), 0)
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "5", "kind": "credit", "reason": "Goodwill"})
        with self.assertRaises(sqlite3.DatabaseError):           # locked again afterwards
            self.db().execute("DELETE FROM credit_ledger")

    def test_integrity_job_reports_problems(self):
        self.run_jobs()
        self.assertIn("No problems found", self.client.get("/admin/health").get_data(as_text=True))
        self.skip_integrity = True
        db = self.db()
        db.execute("INSERT INTO credit_ledger (user_id, amount, reason, created_at, kind) VALUES (?,?,?,?,?)",
                   (self.uid, -500, "bad", "2026-01-01T00:00:00Z", "cash"))
        db.execute("DELETE FROM job_status WHERE job='integrity'")
        db.commit()
        self.run_jobs()
        html = self.client.get("/admin/health").get_data(as_text=True)
        self.assertIn("negative cash balance", html)

    def test_reconciliation_finds_mismatches(self):
        from app import payments, reconcile
        from app.db import iso, utcnow
        from datetime import timedelta
        self.add(self.cid, 2, client=self.p)
        good = self.checkout(client=self.p)
        self.add(self.cid, 1, client=self.p)
        lost = self.checkout(client=self.p, pay=False)
        old = iso(utcnow() - timedelta(hours=3))
        db = self.db()
        db.execute("UPDATE checkouts SET stripe_session_id='cs_good', created_at=? WHERE id=?", (old, good))
        db.execute("UPDATE checkouts SET stripe_session_id='cs_lost', created_at=?, status='expired' WHERE id=?", (old, lost))
        db.commit()
        due = self.q("SELECT cash_due FROM checkouts WHERE id=?", good)
        sessions = [
            {"id": "cs_good", "client_reference_id": f"checkout-{good}", "amount_total": due, "payment_status": "paid", "payment_intent": "pi_g"},
            {"id": "cs_lost", "client_reference_id": f"checkout-{lost}", "amount_total": 250, "payment_status": "paid", "payment_intent": "pi_l"},
            {"id": "cs_ghost", "client_reference_id": "checkout-99999", "amount_total": 100, "payment_status": "paid", "payment_intent": "pi_x"},
            {"id": "cs_open", "client_reference_id": f"checkout-{lost}", "amount_total": 250, "payment_status": "unpaid"},
        ]
        refunds = [{"id": "re_1", "payment_intent": "pi_g", "amount": 100, "status": "succeeded"}]
        with self.app.app_context():
            from app.db import get_db
            found = dict(reconcile.compare(get_db(), sessions, refunds))
        self.assertIn(f"checkout:{lost}:unfulfilled", found)
        self.assertIn("checkout:99999:missing", found)
        self.assertIn(f"checkout:{good}:refunded", found)
        self.assertEqual(len(found), 3)
        sessions[0]["amount_total"] = due + 1
        with self.app.app_context():
            found = dict(reconcile.compare(get_db(), sessions[:1], []))
        self.assertEqual(list(found), [f"checkout:{good}:amount"])
        with self.app.app_context():                            # paid here, Stripe never saw it
            found = dict(reconcile.compare(get_db(), [], []))
        self.assertIn(f"checkout:{good}:notpaid", found)
        # the daily job: flags + a record on the finance page
        self.app.config.update(DEMO_PAYMENTS=False, STRIPE_SECRET_KEY="sk_test_dummy")
        orig = payments.list_sessions, payments.list_refunds
        payments.list_sessions, payments.list_refunds = (lambda since: sessions[1:3]), (lambda since: [])
        try:
            with self.app.test_request_context():
                summary = reconcile.run(get_db())
        finally:
            payments.list_sessions, payments.list_refunds = orig
            self.app.config.update(DEMO_PAYMENTS=True, STRIPE_SECRET_KEY="")
        self.assertIn("mismatch", summary)
        self.assertGreaterEqual(self.q("SELECT COUNT(*) FROM flags WHERE kind='Reconciliation'"), 2)
        self.assertIn("mismatches to review", self.client.get("/admin/finance").get_data(as_text=True))

    def test_feature_flags(self):
        self.assertEqual(self.p.get("/search?q=prize").status_code, 200)
        self.post("/admin/features", {"key": "search", "state": "off"})
        self.assertEqual(self.p.get("/search?q=prize").status_code, 404)
        self.assertNotIn('href="/search"', self.p.get("/").get_data(as_text=True))
        self.post("/admin/features", {"key": "search", "state": "staff"})
        self.assertEqual(self.p.get("/search?q=prize").status_code, 404)
        self.assertEqual(self.client.get("/search?q=prize").status_code, 200)      # staff can try it on the live site
        self.assertEqual(self.q("SELECT COUNT(*) FROM audit_log WHERE action='feature.set'"), 2)
        self.post("/admin/features", {"key": "deposits", "state": "off"})
        self.assertEqual(self.post("/account/deposit", {"amount": "10"}, client=self.p).status_code, 404)
        self.assertIn("Adding funds is unavailable", self.p.get("/account?tab=wallet").get_data(as_text=True))
        self.post("/admin/features", {"key": "support_centre", "state": "off"})
        self.assertIn("/contact", self.p.get("/support").headers["Location"])
        self.post("/admin/features", {"key": "watchlist", "state": "off"})
        self.assertEqual(self.post(f"/watch/{self.slug(self.cid)}", client=self.p).status_code, 404)
        self.assertNotIn("Save &amp; remind me", self.p.get(f"/c/{self.slug(self.cid)}").get_data(as_text=True))
        self.role("support")
        r = self.post("/admin/features", {"key": "search", "state": "on"}, client=self.p)
        self.assertEqual(self.q("SELECT state FROM feature_flags WHERE key='search'"), "staff")

    def test_support_case_locks_and_history(self):
        from app.control import open_case
        with self.app.test_request_context():
            low = open_case("Pat Player", "player@example.com", "Account", "Change my name please", user_id=self.uid)
            high = open_case("Pat Player", "player@example.com", "Payment", "I was charged twice", user_id=self.uid)
            from app.db import get_db
            get_db().execute("UPDATE cases SET priority='high' WHERE id=?", (high,))
            get_db().execute("UPDATE cases SET priority='low' WHERE id=?", (low,))
        html = self.client.get("/admin/cases").get_data(as_text=True)
        self.assertLess(html.index(f"#{high}<"), html.index(f"#{low}<"))          # high priority first
        page = self.client.get(f"/admin/cases/{high}").get_data(as_text=True)     # admin opens it -> theirs
        self.assertIn("You're handling this case", page)
        self.assertIn("Customer history", page)
        self.assertIn(f"#{low} Account", page)                                      # their other case
        self.role("support")
        page = self.p.get(f"/admin/cases/{high}").get_data(as_text=True)
        self.assertIn("Admin Person is working on this case", page)
        self.post(f"/admin/cases/{high}", {"action": "reply", "body": "Sorted!"}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM case_notes WHERE case_id=? AND kind='reply'", high), 0)
        self.assertIn("Admin Person", self.client.get("/admin/cases").get_data(as_text=True))
        self.post(f"/admin/cases/{high}", {"action": "takeover"}, client=self.p)
        self.post(f"/admin/cases/{high}", {"action": "reply", "body": "Sorted!"}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM case_notes WHERE case_id=? AND kind='reply'", high), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM audit_log WHERE action='case.takeover'"), 1)
        self.assertIn("Pat Player is working on this case", self.client.get(f"/admin/cases/{high}").get_data(as_text=True))
        self.assertIn("replied: Payment", self.p.get("/account/notifications").get_data(as_text=True))

    def test_liability_dashboard(self):
        iw = self.make_comp("Instant", max_tickets=4, max_per_user=4, price="1.00",
                            instant=[{"title": "£2 Credit", "value": "2", "type": "credit", "quantity": "2"}])
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        html = self.client.get("/admin/liability").get_data(as_text=True)
        self.assertIn("Draw winners awaiting their prize", html)
        self.assertIn("Test Prize", html)
        self.assertIn("Instant", html)
        self.assertIn("£500<", html)
        with self.assertRaises(sqlite3.DatabaseError):
            self.db().execute("DELETE FROM draws")              # draw records can't be deleted
        self.role("support")
        self.assertNotIn("Draw winners", self.p.get("/admin/liability", follow_redirects=True).get_data(as_text=True))

    def test_audit_viewer_filters_and_export(self):
        self.post(f"/admin/users/{self.uid}", {"action": "verify"})
        html = self.client.get("/admin/audit?action=comp").get_data(as_text=True)
        self.assertIn("comp.publish", html)
        self.assertNotIn("<code>user.", html)
        html = self.client.get("/admin/audit?actor=system").get_data(as_text=True)
        self.assertNotIn("admin@example.com</td>", html)
        r = self.client.get("/admin/audit?format=csv&action=comp")
        self.assertEqual(r.mimetype, "text/csv")
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith("id,created_at_utc,actor,action"))
        self.assertIn("comp.publish", body)
        self.assertEqual(self.q("SELECT COUNT(*) FROM audit_log WHERE action='audit.export'"), 1)

    def test_anomaly_alerts(self):
        from app.jobs import anomalies
        db = self.db()
        for i in range(6):
            db.execute("INSERT INTO checkouts (user_id, subtotal, cash_due, status, stripe_session_id, created_at) "
                       "VALUES (?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (self.uid, 100, 100, "credit_refused", f"cs_r{i}"))
        db.commit()
        with self.app.app_context():
            from app.db import get_db
            odd = anomalies(get_db())
        self.assertTrue(any("refunded" in o for o in odd), odd)
        self.assertIn("Unusual activity", self.client.get("/admin/health").get_data(as_text=True))


class RecoveryTests(PlatformBase):
    def test_disaster_recovery_drill(self):
        r = self.cli("dr-drill")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("No backup found", r.output)
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.post(f"/admin/competitions/{self.cid}/draw")
        self.assertEqual(self.cli("backup").exit_code, 0)
        mails = self.q("SELECT COUNT(*) FROM notifications")
        r = self.cli("dr-drill")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Page for a drawn competition", r.output)
        self.assertIn("PASSED", r.output)
        self.assertIn("RTO", r.output)
        self.assertEqual(self.q("SELECT COUNT(*) FROM notifications"), mails)        # live site untouched
        self.assertIn("Last drill passed", self.client.get("/admin/health").get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM audit_log WHERE action='dr.drill'"), 2)


class ReportingTests(PlatformBase):
    def test_business_report_figures_and_csv(self):
        self.post("/admin/promos", {"code": "TENOFF", "percent": "10", "fixed": "0", "min_spend": "0", "per_user": "5"})
        self.add(self.cid, 4, client=self.p)                          # 4 × £2.50 = £10.00
        self.checkout(client=self.p, promo="TENOFF")                  # £1 promo discount, £9 by card
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "5", "kind": "credit", "reason": "Goodwill"})
        with self.app.app_context():
            from app.control import business_report
            from app.db import get_db
            from datetime import timedelta
            from app.services import uk_midnight
            r = business_report(get_db(), uk_midnight() - timedelta(days=1), uk_midnight() + timedelta(days=1))
        self.assertEqual(r["gross_entries"], 1000)
        self.assertEqual(r["promo_codes"], 100)
        self.assertEqual(r["customer_money"], 900)
        self.assertEqual(r["customers"], 1)
        self.assertEqual(r["new"], 1)
        self.assertEqual(r["paid_entries"], 4)
        self.assertEqual(r["promo_credit"], 500)
        html = self.client.get("/admin/reports").get_data(as_text=True)
        self.assertIn("Gross paid entries", html)
        self.assertIn("agree them with your accountant", html)
        csv_ = self.client.get("/admin/reports?format=csv").get_data(as_text=True)
        self.assertIn("Gross paid entries,1000", csv_)
        self.post("/admin/reports", {"fee_percent": "1.4", "fee_fixed_pence": "20"})
        self.assertEqual(self.q("SELECT value FROM settings WHERE key='fee_percent'"), "1.4")
        self.role("support")
        self.assertNotIn("Gross paid entries", self.p.get("/admin/reports", follow_redirects=True).get_data(as_text=True))

    def test_errors_are_recorded_and_targets_shown(self):
        from app import metrics
        self.client.get("/")
        self.client.get("/competitions")
        with self.app.app_context():
            from app.db import get_db
            metrics.maybe_flush(get_db(), force=True)
        self.assertGreaterEqual(self.q("SELECT n FROM request_stats WHERE metric='requests'"), 2)
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.app.testing = False

        def broken():
            raise RuntimeError("kaboom")
        orig = self.app.view_functions["public.transparency"]
        self.app.view_functions["public.transparency"] = broken
        try:
            for _ in range(2):
                r = self.p.get("/transparency")
                self.assertEqual(r.status_code, 500)
                self.assertIn("Something broke on our side", r.get_data(as_text=True))
        finally:
            self.app.view_functions["public.transparency"] = orig
            self.app.testing = True
        self.assertEqual(self.q("SELECT count FROM error_log"), 2)       # grouped
        html = self.client.get("/admin/targets").get_data(as_text=True)
        self.assertIn("RuntimeError: kaboom", html)
        self.assertIn("Checkout completion", html)
        self.assertIn("Server errors", self.client.get("/admin/health").get_data(as_text=True))
        eid = self.q("SELECT id FROM error_log")
        self.post(f"/admin/errors/{eid}/resolve")
        self.assertIsNotNone(self.q("SELECT resolved_at FROM error_log"))
        self.post("/admin/targets", {"uptime": "99.95", "lcp_mobile": "2.1"})
        html = self.client.get("/admin/targets").get_data(as_text=True)
        self.assertIn("99.95%", html)
        self.assertIn("On target", html)


class SecurityTests(PlatformBase):
    """Attacks tried against our own test instance: IDOR, privilege escalation, tampering, CSRF, injection,
    races, redirects, rate limits. See docs/SECURITY-REVIEW.md."""

    def setUp(self):
        super().setUp()
        self.mallory = self.app.test_client()
        self.signup("mallory@example.com", client=self.mallory, name="Mallory Evil")
        db = self.db()
        db.execute("UPDATE users SET email_verified=1")
        db.commit()

    def test_idor_other_customers_records(self):
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p)
        tid = self.q("SELECT id FROM tickets WHERE user_id=? LIMIT 1", self.uid)
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "20", "kind": "cash", "reason": "Prize"})
        bank = {"amount": "10", "method": "bank", "account_name": "Pat Player", "sort_code": "12-34-56", "account_number": "12345678",
                "step": "confirm"}
        self.post("/account/withdraw", bank, client=self.p)
        wid = self.q("SELECT id FROM withdrawals")
        from app.control import open_case
        with self.app.test_request_context():
            case = open_case("Pat", "player@example.com", "Account", "private", user_id=self.uid)
        nid = self.q("SELECT id FROM notifications WHERE user_id=? LIMIT 1", self.uid)
        for url in (f"/checkout/{chk}/done", f"/checkout/{chk}/pay", f"/checkout/{chk}/cancel", f"/account/orders/{chk}",
                    f"/account/tickets/{tid}", f"/account/withdrawals/{wid}", f"/support/requests/{case}",
                    f"/account/notifications/{nid}/open"):
            self.assertEqual(self.mallory.get(url).status_code, 404, url)
        self.assertEqual(self.post(f"/checkout/{chk}/demo-pay", client=self.mallory).status_code, 404)
        r = self.post(f"/play/reveal/{tid}", client=self.mallory)
        self.assertEqual(r.status_code, 404)
        self.post("/account/notifications/read", {"nid": str(nid)}, client=self.mallory)
        self.assertIsNone(self.q("SELECT read_at FROM notifications WHERE id=?", nid))
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")

    def test_privilege_escalation(self):
        for url in ("/admin/", "/admin/payouts", "/admin/users", "/admin/audit", "/admin/finance", "/admin/features",
                    "/admin/liability", "/admin/cases", f"/admin/competitions/{self.cid}"):
            self.assertEqual(self.mallory.get(url).status_code, 404, url)
        mid = self.q("SELECT id FROM users WHERE email='mallory@example.com'")
        self.assertEqual(self.post(f"/admin/users/{mid}", {"action": "admin", "role": "admin"}, client=self.mallory).status_code, 404)
        self.assertEqual(self.post(f"/admin/users/{mid}", {"action": "credit", "amount": "500", "kind": "cash", "reason": "x"},
                                   client=self.mallory).status_code, 404)
        # mass assignment: extra fields on the profile form are ignored
        self.post("/account/profile", {"phone": "07700900000", "is_admin": "1", "admin_role": "admin", "email_verified": "1",
                                       "points": "99999"}, client=self.mallory)
        row = self.db().execute("SELECT is_admin, admin_role, points FROM users WHERE id=?", (mid,)).fetchone()
        self.assertEqual((row[0], row[2]), (0, 0))
        # a Support-role admin can't reach money or settings
        self.role("support")
        for url in ("/admin/payouts", "/admin/features", "/admin/promos"):
            self.assertNotEqual(self.p.get(url).status_code, 200, url)
        self.post(f"/admin/users/{mid}", {"action": "credit", "amount": "500", "kind": "cash", "reason": "x"}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", mid), 0)

    def test_price_and_quantity_tampering(self):
        slug = self.slug(self.cid)
        for qty in ("-5", "0", "1e9", "nan", "abc", "999999"):
            self.post("/basket/add", {"slug": slug, "quantity": qty, "answer": "b", "ticket_price": "0.01", "price": "1"},
                      client=self.mallory)
        self.post("/basket/add", {"slug": slug, "quantity": "1", "numbers": "0,-1,21,99999", "answer": "b"}, client=self.mallory)
        self.post("/basket/add", {"slug": slug, "quantity": "2", "answer": "b", "price": "0.01"}, client=self.mallory)
        chk = self.checkout(client=self.mallory)
        n = self.q("SELECT COUNT(*) FROM tickets WHERE user_id=(SELECT id FROM users WHERE email='mallory@example.com')")
        self.assertLessEqual(n, 10)                                     # per-person limit held
        self.assertEqual(self.q("SELECT cash_due FROM checkouts WHERE id=?", chk), 250 * n)   # server-side price
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE number<1 OR number>20"), 0)
        for amount in ("inf", "-10", "nan", "1e308", "0.001"):
            r = self.post("/account/withdraw", {"amount": amount, "method": "bank", "account_name": "M", "sort_code": "12-34-56",
                                                "account_number": "12345678", "step": "confirm"}, client=self.mallory)
            self.assertLess(r.status_code, 500, amount)
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 0)
        for blocks in ("-3", "0", "inf"):
            self.assertLess(self.post("/account/redeem", {"blocks": blocks}, client=self.mallory).status_code, 500)
        self.assertEqual(self.q("SELECT COUNT(*) FROM credit_ledger"), 0)

    def test_promo_cannot_be_reused_or_raced(self):
        self.post("/admin/promos", {"code": "ONCE", "percent": "50", "fixed": "0", "min_spend": "0", "per_user": "1"})
        slug = self.slug(self.cid)
        token = self.csrf(self.mallory)
        self.mallory.post("/basket/add", data={"csrf": token, "slug": slug, "quantity": "1", "answer": "b"})
        barrier, results = threading.Barrier(5), []

        def go():
            barrier.wait()
            r = self.mallory.post("/basket/checkout", data={"csrf": token, "promo": "ONCE"})
            results.append(r.status_code)
        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertLessEqual(self.q("SELECT COUNT(*) FROM checkouts WHERE promo_id IS NOT NULL"), 1)
        self.assertLessEqual(self.q("SELECT uses FROM promo_codes"), 1)
        self.post("/basket/add", {"slug": slug, "quantity": "1", "answer": "b"}, client=self.mallory)
        self.assertIsNone(self.checkout(client=self.mallory, promo="ONCE"))     # second use refused
        self.assertIsNone(self.checkout(client=self.mallory, promo="once' OR '1'='1"))

    def test_concurrent_withdrawals_cannot_overdraw(self):
        mid = self.q("SELECT id FROM users WHERE email='mallory@example.com'")
        self.post(f"/admin/users/{mid}", {"action": "credit", "amount": "20", "kind": "cash", "reason": "Prize"})
        token = self.csrf(self.mallory)
        barrier = threading.Barrier(6)

        def go():
            barrier.wait()
            self.mallory.post("/account/withdraw", data={"csrf": token, "amount": "15", "method": "bank", "account_name": "Mallory Evil",
                                                         "sort_code": "12-34-56", "account_number": "12345678", "step": "confirm"})
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 1)
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=? AND kind='cash'", mid), 500)

    def test_csrf_required(self):
        mid = self.q("SELECT id FROM users WHERE email='mallory@example.com'")
        r = self.client.post(f"/admin/users/{mid}", data={"action": "credit", "amount": "500", "kind": "cash", "reason": "x"})
        self.assertEqual(r.status_code, 400)
        foreign = self.csrf(self.mallory)                                   # a token from another session
        r = self.client.post(f"/admin/users/{mid}", data={"csrf": foreign, "action": "credit", "amount": "500", "kind": "cash", "reason": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.p.post("/account/profile", data={"phone": "1"}).status_code, 400)
        self.assertEqual(self.q("SELECT COUNT(*) FROM credit_ledger"), 0)

    def test_injection_and_escaping(self):
        evil = "<script>alert(1)</script>"
        self.post("/account/profile", {"phone": "07700900000"}, client=self.mallory)
        db = self.db()
        db.execute("UPDATE users SET name=? WHERE email='mallory@example.com'", (evil + " Evil",))
        db.commit()
        from app.control import open_case
        mid = self.q("SELECT id FROM users WHERE email='mallory@example.com'")
        with self.app.test_request_context():
            case = open_case(evil, "mallory@example.com", "Other", evil + "' OR 1=1 --", user_id=mid)
        for url in (f"/admin/cases/{case}", "/admin/users", f"/admin/customers/{mid}/timeline", "/admin/cases"):
            body = self.client.get(url).get_data(as_text=True)
            self.assertNotIn(evil, body, url)
        for q in ("' OR 1=1 --", "%", "\"\"; DROP TABLE users; --", evil):
            r = self.client.get("/search", query_string={"q": q})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(evil, r.get_data(as_text=True))
            self.assertEqual(self.post("/login", {"email": q, "password": q}, client=self.app.test_client()).status_code, 400)
        self.assertGreater(self.q("SELECT COUNT(*) FROM users"), 0)
        r = self.client.get("/admin/audit", query_string={"q": "' OR 1=1 --", "actor": "%", "from": "2020-13-99"})
        self.assertEqual(r.status_code, 200)

    def test_open_redirects_blocked(self):
        for nxt in ("https://evil.com", "//evil.com", "/\\evil.com", "/\\/evil.com", "javascript:alert(1)", "/%0d%0aSet-Cookie:x=1"):
            c = self.app.test_client()
            r = self.post("/login", {"email": "player@example.com", "password": "supersecret123"}, client=c,
                          query_string={"next": nxt})
            loc = r.headers.get("Location", "")
            self.assertTrue(loc.startswith("/") and not loc.startswith("//") and "\\" not in loc and "evil" not in loc.split("?")[0][:20]
                            or loc.endswith("/"), (nxt, loc))
        self.assertEqual(self.post("/login", {"email": "player@example.com", "password": "supersecret123"},
                                   client=self.app.test_client(), query_string={"next": "/account"}).headers["Location"], "/account")

    def test_login_lockout_survives_spoofed_ip(self):
        c = self.app.test_client()
        for i in range(9):
            self.post("/login", {"email": "player@example.com", "password": "wrong"}, client=c,
                      headers={"X-Forwarded-For": f"10.0.0.{i}"})
        r = self.post("/login", {"email": "player@example.com", "password": "supersecret123"}, client=c,
                      headers={"X-Forwarded-For": "10.9.9.9"})
        self.assertEqual(r.status_code, 429)                  # the account itself is locked, whatever the IP

    def test_webhook_needs_valid_signature(self):
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_x", "client_reference_id": "checkout-1",
                                                                                      "payment_status": "paid", "amount_total": 0}}})
        r = self.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": "t=1,v1=deadbeef"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/stripe/webhook", data=body)
        self.assertEqual(r.status_code, 400)

    def test_security_headers(self):
        r = self.client.get("/")
        csp = r.headers.get("Content-Security-Policy", "")
        for part in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'"):
            self.assertIn(part, csp)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("camera=()", r.headers.get("Permissions-Policy", ""))
        r = self.client.get("/", base_url="https://localhost")
        self.assertIn("max-age=", r.headers.get("Strict-Transport-Security", ""))
        self.assertNotIn("Strict-Transport-Security", self.client.get("/").headers)


def stripe_event(etype, obj, secret=b"whsec_test"):
    body = json.dumps({"type": etype, "data": {"object": obj}}).encode()
    ts = int(time.time())
    sig = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}"}


class AutoDrawBase(PlatformBase):
    def close(self, cid=None):
        db = self.db()
        db.execute("UPDATE competitions SET auto_draw=1")                 # as the admin form defaults to
        db.commit()
        super().close(cid)


class MoneyJourneyTests(AutoDrawBase):
    """Phase 4: every money-critical journey end to end, through the real Stripe webhook path (Stripe mocked)."""

    def stripe_checkout(self, client, n=1, **add):
        from unittest import mock
        from app import payments
        self.add(add.pop("cid", self.cid), n, client=client, **add)
        sid = f"cs_{time.time_ns()}"
        with mock.patch.object(payments, "enabled", return_value=True), \
             mock.patch.object(payments, "create_checkout", return_value={"id": sid, "url": "https://checkout.stripe.test/x"}):
            r = self.post("/basket/checkout", {}, client=client)
        self.assertEqual(r.headers.get("Location"), "https://checkout.stripe.test/x")
        chk = self.q("SELECT MAX(id) FROM checkouts")
        return chk, sid

    def webhook(self, etype, chk, sid, amount=None, pi=None, funding="debit", times=1):
        from unittest import mock
        from app import payments
        obj = {"id": sid, "payment_status": "paid" if "completed" in etype or "succeeded" in etype else "unpaid",
               "payment_intent": pi or f"pi_{chk}", "metadata": {"checkout_id": str(chk)},
               "amount_total": self.q("SELECT cash_due FROM checkouts WHERE id=?", chk) if amount is None else amount}
        body, headers = stripe_event(etype, obj)
        codes = []
        with mock.patch.object(payments, "card_funding", return_value=funding), mock.patch.object(payments, "refund"):
            for _ in range(times):
                codes.append(self.client.post("/stripe/webhook", data=body, headers=headers).status_code)
        return codes

    def verify_player(self):
        db = self.db()
        db.execute("UPDATE users SET email_verified=1")
        db.commit()

    def test_full_journey_entry_to_withdrawal(self):
        self.verify_player()
        # an instant-win competition where every number wins £3 cash, and the main draw
        iw = self.make_comp("Cash Instant", max_tickets=3, max_per_user=3, price="1.00",
                            instant=[{"title": "£6 Cash", "value": "6", "type": "cash", "quantity": "3"}])
        chk, sid = self.stripe_checkout(self.p, 2)                          # 2 × £2.50 on the main draw
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='held'"), 2)
        self.assertEqual(self.webhook("checkout.session.completed", chk, sid, times=3), [200, 200, 200])   # duplicate callbacks
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")
        self.assertEqual(self.q("SELECT payment_intent FROM checkouts WHERE id=?", chk), f"pi_{chk}")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued' AND user_id=?", self.uid), 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM points_ledger WHERE ref=?", f"c{chk}"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='order'", self.uid) <= 1, True)
        chk2, sid2 = self.stripe_checkout(self.p, 1, cid=iw)                # instant cash win → wallet
        self.webhook("checkout.session.completed", chk2, sid2, times=2)
        self.assertEqual(self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='cash'", self.uid), 600)
        # closing → frozen list → automatic draw → winner and prize claim, once, however often jobs run
        self.close()
        for _ in range(3):
            self.run_jobs()
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "drawn")
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws WHERE competition_id=?", self.cid), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims WHERE competition_id=?", self.cid), 1)
        self.assertEqual(self.q("SELECT user_id FROM tickets WHERE id=(SELECT winner_ticket_id FROM competitions WHERE id=?)", self.cid),
                         self.uid)
        self.assertIn("Winner", self.p.get("/account?tab=entries&show=winner").get_data(as_text=True))
        # withdrawal of the instant cash, paid by finance
        bank = {"amount": "6", "method": "bank", "account_name": "Pat Player", "sort_code": "12-34-56",
                "account_number": "12345678", "step": "confirm"}
        self.post("/account/withdraw", bank, client=self.p)
        self.post("/account/withdraw", bank, client=self.p)                  # second attempt: no balance left
        wid = self.q("SELECT id FROM withdrawals")
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 1)
        self.assertEqual(self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind='cash'", self.uid), 0)
        self.post("/admin/payouts", {"wid": str(wid), "action": "paid"})
        self.post("/admin/payouts", {"wid": str(wid), "action": "paid"})            # double-click
        self.assertEqual(self.q("SELECT status FROM withdrawals"), "paid")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db().execute("UPDATE withdrawals SET status='requested'")
        self.assertIn("Paid", self.p.get(f"/account/withdrawals/{wid}").get_data(as_text=True))

    def test_failed_and_expired_payments_release_everything(self):
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "1", "kind": "credit", "reason": "Gift"})
        chk, sid = self.stripe_checkout(self.p, 2, use_credit=True) if False else self.stripe_checkout(self.p, 2)
        self.webhook("checkout.session.async_payment_failed", chk, sid)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "expired")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        chk, sid = self.stripe_checkout(self.p, 1)
        self.webhook("checkout.session.expired", chk, sid, times=2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        # a late "paid" after we released the numbers: nothing issued, flagged for refund
        self.webhook("checkout.session.completed", chk, sid)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "needs_refund")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=?", self.uid), 100)   # gift intact

    def test_reservation_expiry_without_any_webhook(self):
        chk, sid = self.stripe_checkout(self.p, 3)
        db = self.db()
        db.execute("UPDATE checkouts SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (chk,))
        db.commit()
        self.run_jobs()
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "expired")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)

    def test_wrong_amount_and_cancelled_competition(self):
        chk, sid = self.stripe_checkout(self.p, 1)
        self.webhook("checkout.session.completed", chk, sid, amount=1)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "needs_refund")
        chk, sid = self.stripe_checkout(self.p, 1)
        self.post(f"/admin/competitions/{self.cid}/status", {"action": "cancel"})
        self.webhook("checkout.session.completed", chk, sid)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "needs_refund")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 0)
        self.post(f"/admin/refunds/{chk}/done")
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "refunded")    # history kept

    def test_cancellation_refunds_once_even_if_repeated(self):
        self.verify_player()
        chk, sid = self.stripe_checkout(self.p, 4)
        self.webhook("checkout.session.completed", chk, sid)
        self.post(f"/admin/competitions/{self.cid}/status", {"action": "cancel"})
        from app.services import refund_competition
        with self.app.app_context():
            for _ in range(3):
                refund_competition(self.cid)
        self.run_jobs()
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE user_id=? AND ref LIKE 'refund-o%'", self.uid), 1000)

    def test_interrupted_session_still_gets_tickets(self):
        chk, sid = self.stripe_checkout(self.p, 2)
        self.post("/logout", client=self.p)                              # browser closed / session lost mid-payment
        self.webhook("checkout.session.completed", chk, sid)
        self.post("/login", {"email": "player@example.com", "password": "supersecret123"}, client=self.p)
        html = self.p.get("/account?tab=entries").get_data(as_text=True)
        self.assertIn("Test Prize", html)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 2)

    def test_chargeback_and_dashboard_refund_flagged(self):
        chk, sid = self.stripe_checkout(self.p, 1)
        self.webhook("checkout.session.completed", chk, sid)
        for etype, obj in (("charge.dispute.created", {"id": "dp_1", "payment_intent": f"pi_{chk}", "amount": 250}),
                           ("charge.refunded", {"id": "ch_1", "payment_intent": f"pi_{chk}", "amount_refunded": 250})):
            body, headers = stripe_event(etype, obj)
            for _ in range(2):
                self.assertEqual(self.client.post("/stripe/webhook", data=body, headers=headers).status_code, 200)
        self.assertEqual(self.q("SELECT COUNT(*) FROM flags WHERE kind='Payment reversal'"), 2)
        self.assertIn(f"checkout #{chk}", self.client.get("/admin/flags").get_data(as_text=True))
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")        # staff decide


class HardeningTests(AutoDrawBase):
    """Phase 4: the database refuses impossible states; jobs are idempotent; failures leave data consistent."""

    def test_database_refuses_impossible_states(self):
        self.skip_integrity = True
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p)
        iw = self.make_comp("IW", max_tickets=2, max_per_user=2, price="1.00",
                            instant=[{"title": "£1 Cash", "value": "1", "type": "cash", "quantity": "2"}])
        self.add(iw, 1, client=self.p)
        self.checkout(client=self.p)
        other = self.make_comp("Other")
        db = self.db()
        t = db.execute("SELECT * FROM tickets WHERE competition_id=? LIMIT 1", (self.cid,)).fetchone()
        ip = db.execute("SELECT * FROM instant_prizes WHERE ticket_id IS NOT NULL").fetchone()
        now = "2026-01-01T00:00:00Z"
        attempts = {
            "number out of range": ("INSERT INTO tickets (competition_id, number, user_id, status, created_at) VALUES (?, 999, ?, 'issued', ?)",
                                    (self.cid, self.uid, now)),
            "number zero": ("INSERT INTO tickets (competition_id, number, user_id, status, created_at) VALUES (?, 0, ?, 'issued', ?)",
                            (self.cid, self.uid, now)),
            "duplicate number": ("INSERT INTO tickets (competition_id, number, user_id, status, created_at) VALUES (?,?,?, 'issued', ?)",
                                 (self.cid, t["number"], self.uid, now)),
            "issued back to held": ("UPDATE tickets SET status='held' WHERE id=?", (t["id"],)),
            "zero wallet entry": ("INSERT INTO credit_ledger (user_id, amount, reason, created_at, kind) VALUES (?, 0, 'x', ?, 'cash')",
                                  (self.uid, now)),
            "unknown wallet kind": ("INSERT INTO credit_ledger (user_id, amount, reason, created_at, kind) VALUES (?, 5, 'x', ?, 'gold')",
                                    (self.uid, now)),
            "prize paid twice": ("INSERT INTO credit_ledger (user_id, amount, reason, ref, created_at, kind) VALUES (?, 100, 'x', ?, ?, 'cash')",
                                 (self.uid, f"ip{ip['id']}", now)),
            "negative withdrawal": ("INSERT INTO withdrawals (user_id, amount, created_at) VALUES (?, -5, ?)", (self.uid, now)),
            "paid checkout un-paid": ("UPDATE checkouts SET status='expired' WHERE id=?", (chk,)),
            "paid checkout amount": ("UPDATE checkouts SET cash_due=1 WHERE id=?", (chk,)),
            "won prize reassigned": ("UPDATE instant_prizes SET ticket_id=? WHERE id=?", (t["id"], ip["id"])),
            "draw with foreign ticket": ("INSERT INTO draws (competition_id, drawn_at, method, seed, seed_hash, entries_hash, entry_count, "
                                         "winning_index, winning_number, winning_ticket_id, entries) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                         (other, now, "manual", "s", "h", "e", 1, 0, t["number"], t["id"], "[]")),
            "empty order": ("INSERT INTO orders (user_id, competition_id, quantity, amount, created_at) VALUES (?,?,0,0,?)",
                            (self.uid, self.cid, now)),
        }
        for label, (sql, args) in attempts.items():
            with self.assertRaises(sqlite3.DatabaseError, msg=label):
                db.execute(sql, args)
        # overselling: fill the competition directly, then one more is refused by the database itself
        small = self.make_comp("Two", max_tickets=2, max_per_user=2)
        for n in (1, 2):
            db.execute("INSERT INTO tickets (competition_id, number, user_id, status, created_at) VALUES (?,?,?, 'issued', ?)",
                       (small, n, self.uid, now))
        db.commit()
        db.execute("UPDATE competitions SET max_tickets=3 WHERE id=?", (small,))  # even with room in the range…
        db.execute("UPDATE competitions SET max_tickets=2 WHERE id=?", (small,))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("INSERT INTO tickets (competition_id, number, user_id, status, created_at) VALUES (?,3,?, 'issued', ?)",
                       (small, self.uid, now))
        self.assertEqual(self.q("SELECT value FROM settings WHERE key='constraints_skipped'"), "")

    def test_jobs_are_idempotent_even_in_parallel(self):
        iw = self.make_comp("Scratchy", max_tickets=4, max_per_user=4, price="1.00",
                            instant=[{"title": "£2 Cash", "value": "2", "type": "cash", "quantity": "4"}])
        db = self.db()
        db.execute("UPDATE competitions SET game_type='scratch' WHERE id=?", (iw,))      # a game: prizes paid on reveal / settle
        db.execute("UPDATE users SET email_verified=1")
        db.commit()
        self.add(iw, 2, client=self.p)
        self.checkout(client=self.p)
        self.add(self.cid, 3, client=self.p)
        self.checkout(client=self.p)
        db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z', auto_draw=1")
        db.execute("UPDATE instant_prizes SET won_at='2000-01-01T00:00:00Z' WHERE ticket_id IS NOT NULL")
        db.commit()
        from app.jobs import run_all_jobs
        errors = []

        def go():
            try:
                with self.app.test_request_context():
                    for _ in range(2):
                        run_all_jobs(force=True)
            except Exception as e:                 # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM entry_snapshots WHERE competition_id=?", self.cid), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM credit_ledger WHERE ref LIKE 'ip%'"), 2)        # each won prize paid once
        self.assertEqual(self.q("SELECT SUM(amount) FROM credit_ledger WHERE ref LIKE 'ip%'"), 400)
        dup = self.q("SELECT COUNT(*) FROM (SELECT dedupe_key FROM notifications WHERE dedupe_key IS NOT NULL GROUP BY dedupe_key HAVING COUNT(*)>1)")
        self.assertEqual(dup, 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM notifications WHERE user_id=? AND title LIKE '%won%'", self.uid), 1)

    def test_payment_provider_down_at_checkout(self):
        from unittest import mock
        from app import payments
        self.add(self.cid, 2, client=self.p)
        with mock.patch.object(payments, "enabled", return_value=True), \
             mock.patch.object(payments, "create_checkout", side_effect=RuntimeError("Stripe 503")):
            r = self.post("/basket/checkout", {}, client=self.p, follow_redirects=True)
        self.assertIn("not been charged", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets"), 0)                 # numbers released
        self.assertIn("Test Prize", self.p.get("/basket").get_data(as_text=True))   # basket kept

    def test_email_outage_never_blocks_purchases(self):
        from unittest import mock
        from app import mailer
        self.app.config.update(SMTP_HOST="smtp.invalid", MAIL_FROM="x@example.com")
        try:
            with mock.patch.object(mailer, "_transmit", side_effect=OSError("SMTP down")):
                self.add(self.cid, 2, client=self.p)
                chk = self.checkout(client=self.p)
                self.run_jobs()
            self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")
            self.assertGreater(self.q("SELECT COUNT(*) FROM notifications WHERE email_status='failed'"), 0)
            self.assertIn("Email sending", self.client.get("/admin/health").get_data(as_text=True))
            with mock.patch.object(mailer, "_transmit", return_value=None):              # SMTP back: retried
                db = self.db()
                db.execute("UPDATE notifications SET email_tries=1 WHERE email_status='failed'")
                db.commit()
                self.run_jobs()
            self.assertEqual(self.q("SELECT COUNT(*) FROM notifications WHERE email_status='failed'"), 0)
        finally:
            self.app.config.update(SMTP_HOST="")

    def test_database_error_mid_payment_rolls_back_then_retry_succeeds(self):
        from unittest import mock
        from app import services
        chk = self.checkout_pending()
        with mock.patch.object(services, "award_points", side_effect=sqlite3.OperationalError("database is locked")):
            with self.app.test_request_context():
                with self.assertRaises(sqlite3.OperationalError):
                    services.fulfil_checkout(chk)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "pending")    # nothing half-done
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM orders WHERE status='paid'"), 0)
        with self.app.test_request_context():
            self.assertEqual(services.fulfil_checkout(chk), "paid")                             # Stripe retries
            self.assertEqual(services.fulfil_checkout(chk), "paid")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM points_ledger"), 1)

    def checkout_pending(self):
        self.add(self.cid, 2, client=self.p)
        return self.checkout(client=self.p, pay=False)

    def test_draw_failing_halfway_leaves_no_trace_and_retries(self):
        from unittest import mock
        from app import services
        self.add(self.cid, 3, client=self.p)
        self.checkout(client=self.p)
        self.close()
        with mock.patch.object(services, "_new_claim", side_effect=RuntimeError("crash mid-draw")):
            with self.app.test_request_context():
                with self.assertRaises(RuntimeError):
                    services.run_draw(self.cid)
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "live")
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 0)
        self.assertIsNone(self.q("SELECT winner_ticket_id FROM competitions WHERE id=?", self.cid))
        self.run_jobs()                                                                          # next job run draws it
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)

    def test_restart_during_checkout(self):
        chk = self.checkout_pending()
        from app import create_app
        fresh = create_app({"TESTING": True, "DATABASE": self.app.config["DATABASE"], "UPLOAD_DIR": self.app.config["UPLOAD_DIR"],
                            "STRIPE_WEBHOOK_SECRET": "whsec_test", "ADMIN_MFA": False})          # the site restarted
        body, headers = stripe_event("checkout.session.completed", {"id": "cs_r", "payment_status": "paid", "payment_intent": "pi_r",
                                                                   "metadata": {"checkout_id": str(chk)}, "amount_total": 500})
        self.assertEqual(fresh.test_client().post("/stripe/webhook", data=body, headers=headers).status_code, 200)
        self.assertEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE status='issued'"), 2)

    def test_worker_down_jobs_still_run_from_web(self):
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.jobs_due()
        self.p.get("/competitions")                                   # any customer visit
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "drawn")


class MigrationTest(unittest.TestCase):
    def test_v1_database_upgrades_in_place(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "prizes.db")
        conn = sqlite3.connect(path)
        conn.executescript(open(os.path.join(HERE, "v1_schema.sql")).read())
        conn.execute("INSERT INTO users (email,name,password_hash,dob,is_admin,created_at) VALUES "
                     "('old@example.com','Old User','x','1990-01-01',1,'2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO competitions (slug,title,ticket_price,max_tickets,ends_at,question,answer_a,answer_b,"
                     "answer_c,correct,status,seed,seed_hash,created_at) VALUES ('old','Old Comp',100,10,"
                     "'2099-01-01T00:00:00Z','q','a','b','c','a','live','s','h','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO orders (user_id,competition_id,quantity,amount,status,created_at) VALUES (1,1,2,200,'paid','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO tickets (competition_id,number,user_id,order_id,created_at) VALUES (1,1,1,1,'x'),(1,2,1,1,'x')")
        conn.commit()
        conn.close()
        os.environ["DATA_DIR"] = tmp
        app = create_app({"TESTING": True})
        db = _connect(path)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets WHERE status='issued'").fetchone()[0], 2)
        self.assertTrue(db.execute("SELECT referral_code FROM users").fetchone()[0])
        self.assertEqual(db.execute("SELECT category FROM competitions").fetchone()[0], "other")
        create_app({"TESTING": True})  # running again is harmless
        self.assertEqual(app.test_client().get("/c/old").status_code, 200)


if __name__ == "__main__":
    unittest.main()
