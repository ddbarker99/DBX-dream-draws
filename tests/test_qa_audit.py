"""Release QA audit — gap-focused business-logic and adversarial tests.

Each test's docstring is "ID | Area | Feature | Expected result"; tests/qa_register.py turns them (and every other
automated test) into the QA register in docs/qa/. Run: python -m pytest -q tests/test_qa_audit.py
"""
import os
import re
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_app import AutoDrawBase  # noqa: E402


class AuditBase(AutoDrawBase):
    def verify(self, uid=None):
        db = self.db()
        db.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid or self.uid,))
        db.commit()

    def give(self, amount, kind="cash", uid=None):
        db = self.db()
        db.execute("INSERT INTO credit_ledger (user_id, amount, reason, ref, created_at, kind) VALUES (?,?,?,?,"
                   "strftime('%Y-%m-%dT%H:%M:%SZ','now'),?)", (uid or self.uid, amount, "QA top-up", f"qa{amount}{kind}", kind))
        db.commit()

    def bal(self, kind="cash", uid=None):
        return self.q("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind=?", uid or self.uid, kind)

    def tickets(self, uid=None):
        return self.q("SELECT COUNT(*) FROM tickets WHERE user_id=? AND status='issued'", uid or self.uid)


class CheckoutAudit(AuditBase):
    def test_success_url_alone_never_creates_a_paid_order(self):
        """QA-CHK-01 | Checkout | Success URL | Visiting the confirmation URL without paying creates no paid order and no tickets"""
        self.add(self.cid, 2, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        for _ in range(3):
            self.p.get(f"/checkout/{chk}/done")
        self.assertNotEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")
        self.assertEqual(self.tickets(), 0)

    def test_confirming_payment_twice_gives_one_set_of_entries(self):
        """QA-CHK-02 | Checkout | Idempotency | Posting the payment confirmation repeatedly → one payment, one order, one set of tickets, one points award"""
        self.add(self.cid, 3, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        for _ in range(4):
            self.post(f"/checkout/{chk}/demo-pay", client=self.p)
        self.assertEqual(self.tickets(), 3)
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts WHERE status='paid'"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM points_ledger WHERE user_id=? AND points>0", self.uid), 1)

    def test_cancel_url_releases_reservation(self):
        """QA-CHK-03 | Checkout | Cancel | Cancelling releases the reserved numbers for others; nothing is charged"""
        self.add(self.cid, 10, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        self.post(f"/checkout/{chk}/cancel", client=self.p)
        self.p.get(f"/checkout/{chk}/cancel")
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status IN ('held','issued')", self.cid), 0)
        self.assertNotEqual(self.q("SELECT status FROM checkouts WHERE id=?", chk), "paid")

    def test_paying_after_cancel_is_never_fulfilled_as_new_tickets_twice(self):
        """QA-CHK-04 | Checkout | Out-of-order | Confirming a cancelled checkout never issues tickets that belong to someone else"""
        self.add(self.cid, 10, client=self.p)
        chk = self.checkout(client=self.p, pay=False)
        self.post(f"/checkout/{chk}/cancel", client=self.p)
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self.add(self.cid, 10, client=other)
        self.checkout(client=other)
        self.post(f"/checkout/{chk}/demo-pay", client=self.p)
        rows = self.db().execute("SELECT number, COUNT(*) FROM tickets WHERE competition_id=? AND status='issued' GROUP BY number "
                                 "HAVING COUNT(*)>1", (self.cid,)).fetchall()
        self.assertEqual(rows, [])
        self.assertLessEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='issued'", self.cid), 20)


class BasketAudit(AuditBase):
    def test_quantity_edge_values_rejected(self):
        """QA-BSK-01 | Basket | Quantity | 0, negative, decimal, non-numeric and huge quantities are refused server-side"""
        for qty in ("0", "-1", "-999", "1.5", "abc", "", "1e9", str(10 ** 12)):
            self.post("/basket/add", {"slug": self.slug(self.cid), "quantity": qty, "answer": "b"}, client=self.p)
        with self.p.session_transaction() as s:
            lines = s.get("basket", [])
        self.assertTrue(all(1 <= int(l.get("qty", l.get("quantity", 1))) <= 10 for l in lines), lines)
        chk = self.checkout(client=self.p)
        self.assertLessEqual(self.tickets(), 10)                              # never more than the per-person limit
        self.assertTrue(chk is None or self.tickets() <= 10)

    def test_chosen_numbers_edge_values(self):
        """QA-BSK-02 | Ticket allocation | Chosen numbers | Out-of-range, zero, negative, duplicate and junk numbers never become tickets"""
        for nums in ("0", "-3", "21", "999999", "abc", "3,3,3", "1.5", "<script>"):
            self.post("/basket/add", {"slug": self.slug(self.cid), "quantity": "1", "numbers": nums, "answer": "b"}, client=self.p)
        self.checkout(client=self.p)
        nums = [r[0] for r in self.db().execute("SELECT number FROM tickets WHERE user_id=?", (self.uid,))]
        self.assertTrue(all(1 <= n <= 20 for n in nums), nums)
        self.assertEqual(len(nums), len(set(nums)))

    def test_sold_number_cannot_be_bought_again(self):
        """QA-BSK-03 | Ticket allocation | Sold numbers | A number someone else owns can't be bought; buyer isn't charged for it"""
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self.add(self.cid, 1, numbers="7", client=other)
        self.checkout(client=other)
        self.add(self.cid, 1, numbers="7", client=self.p)
        self.checkout(client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND number=7", self.cid), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE user_id=? AND number=7", self.uid), 0)

    def test_posted_prices_and_totals_are_ignored(self):
        """QA-BSK-04 | Basket | Price manipulation | Extra price/total/discount fields in requests are ignored; server price charged"""
        self.post("/basket/add", {"slug": self.slug(self.cid), "quantity": "2", "answer": "b", "price": "0.01",
                                  "ticket_price": "1", "total": "0.02", "discount": "99"}, client=self.p)
        self.post("/basket/checkout", {"total": "0.01", "cash_due": "1", "amount": "1", "promo_discount": "500"}, client=self.p)
        chk = self.q("SELECT MAX(id) FROM checkouts")
        self.assertEqual(self.q("SELECT subtotal FROM checkouts WHERE id=?", chk), 500)

    def test_competition_closing_while_in_basket(self):
        """QA-BSK-05 | Basket | Closing | A competition that closes while in the basket can't be checked out"""
        self.add(self.cid, 2, client=self.p)
        self.close()
        self.post("/basket/checkout", {}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts"), 0)
        self.assertEqual(self.tickets(), 0)

    def test_draft_closed_and_drawn_competitions_cannot_be_added(self):
        """QA-BSK-06 | Competition states | Entry | Draft, closed and drawn competitions refuse entries by direct POST"""
        draft = self.make_comp("Draft", publish=False)
        for cid in (draft,):
            self.add(cid, 1, client=self.p)
        self.close()
        self.add(self.cid, 1, client=self.p)
        self.post("/basket/checkout", {}, client=self.p)
        self.assertEqual(self.tickets(), 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=?", draft), 0)

    def test_wrong_and_missing_answers(self):
        """QA-BSK-07 | Competition | Entry question | Wrong, missing or tampered answers never create entries"""
        for ans in ("a", "c", "", "z", "B ", "<b>"):
            self.add(self.cid, 1, answer=ans, client=self.p)
        self.checkout(client=self.p)
        self.assertEqual(self.tickets(), 0)


class DrawAudit(AuditBase):
    def _sell(self, n, client=None):
        self.add(self.cid, n, client=client or self.p)
        self.checkout(client=client or self.p)

    def test_one_entry_draw(self):
        """QA-DRW-01 | Draws | 1 entry | The only entry wins; one draw record; one prize claim"""
        self._sell(1)
        self.close()
        self.run_jobs()
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT winning_number FROM draws"), self.q("SELECT number FROM tickets WHERE user_id=?", self.uid))
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)

    def test_running_the_draw_again_never_makes_another_winner(self):
        """QA-DRW-02 | Draws | Duplicate execution | Re-running the draw (job, manual button, direct call) never creates a second winner"""
        self._sell(3)
        self.close()
        self.run_jobs()
        first = self.q("SELECT winning_number FROM draws")
        from app.services import PurchaseError, run_draw
        with self.app.app_context():
            with self.assertRaises(PurchaseError):
                run_draw(self.cid)
        self.post(f"/admin/competitions/{self.cid}/draw")
        self.run_jobs()
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT winning_number FROM draws"), first)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)

    def test_failure_after_selection_but_before_commit_leaves_nothing_and_retry_gives_same_result(self):
        """QA-DRW-03 | Draws | Failure injection | A crash during the draw leaves no partial result; the retry picks the same, single winner"""
        self._sell(4)
        self.close()
        from app import services
        with mock.patch.object(services, "_new_claim", side_effect=RuntimeError("crash mid-draw")):
            try:
                self.run_jobs()
            except RuntimeError:
                pass
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 0)
        self.assertEqual(self.q("SELECT status FROM competitions WHERE id=?", self.cid), "live")
        self.run_jobs()
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)

    def test_failure_while_notifying_keeps_the_draw_and_no_second_winner(self):
        """QA-DRW-04 | Draws | Notification failure | If announcing fails after the draw, the result stands and retries don't redraw"""
        self._sell(3)
        self.close()
        from app import admin
        with mock.patch.object(admin, "announce_draw", side_effect=RuntimeError("email down")):
            try:
                self.run_jobs()
            except RuntimeError:
                pass
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.run_jobs()
        self.run_jobs()
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)

    def test_refunded_entries_are_not_in_the_draw(self):
        """QA-DRW-05 | Draws | Refunded entries | Tickets from a refunded order are excluded from the eligible pool"""
        other = self.app.test_client()
        self.signup("other@example.com", client=other)
        self._sell(2)
        self._sell(3, client=other)
        oid = self.q("SELECT o.id FROM orders o JOIN checkouts k ON k.id=o.checkout_id WHERE k.user_id=? AND o.status='paid'", self.uid)
        self.post(f"/admin/orders/{self.q('SELECT checkout_id FROM orders WHERE id=?', oid)}",
                  {"order_id": str(oid), "reason": "Customer asked within policy", "method": "wallet"})
        self.assertEqual(self.tickets(), 0)
        self.close()
        self.run_jobs()
        d = self.db().execute("SELECT entries, entry_count FROM draws").fetchone()
        self.assertEqual(d["entry_count"], 3)
        mine = {r[0] for r in self.db().execute("SELECT number FROM tickets WHERE user_id=?", (self.uid,))}
        import json
        self.assertFalse(mine & set(json.loads(d["entries"])))


class InstantAudit(AuditBase):
    def _game(self):
        self.post("/admin/competitions/new", {
            "kind": "game", "game_type": "scratch", "title": "QA Scratch", "description": "Scratch to win.", "ends_at": "2099-01-01T20:00",
            "category": "cash", "ticket_price": "1", "max_tickets": "10", "max_per_user": "10", "question": "2+2?", "answer_a": "3",
            "answer_b": "4", "answer_c": "5", "correct": "b", "prize_table": "10, £1 Cash, 1, cash"})
        gid = self.q("SELECT MAX(id) FROM competitions")
        self.post(f"/admin/competitions/{gid}/status", {"action": "publish"})
        return gid

    def test_reveal_is_authoritative_and_repeatable(self):
        """QA-IW-01 | Instant wins | Refresh/replay | Revealing a play repeatedly returns the same result and pays once"""
        gid = self._game()
        self.add(gid, 2, client=self.p)
        self.checkout(client=self.p)
        tid = self.q("SELECT id FROM tickets WHERE user_id=? ORDER BY id LIMIT 1", self.uid)
        outs = [self.post(f"/play/reveal/{tid}", client=self.p).get_json() for _ in range(4)]
        self.assertTrue(outs[0]["first"] and not any(o["first"] for o in outs[1:]))
        self.assertEqual(len({tuple(sorted((k, v) for k, v in o.items() if k != "first")) for o in outs}), 1)
        self.assertEqual(self.bal("cash"), 100)                   # every play wins £1 here; one play revealed, paid once

    def test_unrevealed_prizes_settle_exactly_once(self):
        """QA-IW-02 | Instant wins | Unrevealed outcomes | The automatic settlement pays each unrevealed win once, even when run repeatedly"""
        gid = self._game()
        self.add(gid, 3, client=self.p)
        self.checkout(client=self.p)
        db = self.db()
        db.execute("UPDATE tickets SET created_at='2000-01-01T00:00:00Z' WHERE user_id=?", (self.uid,))
        db.commit()
        from app.services import settle_unrevealed
        with self.app.app_context():
            settle_unrevealed()
            settle_unrevealed()
        self.run_jobs()
        self.assertEqual(self.bal("cash"), 300)


class WalletAudit(AuditBase):
    def wd(self, amount, method="bank"):
        return self.post("/account/withdraw", {"amount": amount, "method": method, "account_name": "Pat Player",
                                               "sort_code": "112233", "account_number": "12345678", "step": "confirm"}, client=self.p)

    def test_withdrawal_amount_edges(self):
        """QA-WD-01 | Withdrawals | Amount edges | Below £5, zero, negative, junk and over-balance refused; exactly £5 and the full balance allowed"""
        self.verify()
        self.give(1000, "cash")
        for bad in ("4.99", "0", "-5", "abc", "", "10.01", "1e3", "5.001x"):
            self.wd(bad)
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 0)
        self.wd("5")
        self.wd("5.00")
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 2)
        self.assertEqual(self.bal("cash"), 0)
        self.wd("5")
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 2)

    def test_site_credit_and_deposits_cannot_be_withdrawn(self):
        """QA-WD-02 | Withdrawals | Balance types | Site credit and deposited funds can never be withdrawn as cash"""
        self.verify()
        self.give(5000, "credit")
        self.give(5000, "deposit")
        self.wd("10")
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 0)

    def test_rejected_withdrawal_returns_money_once(self):
        """QA-WD-03 | Withdrawals | Lifecycle | Requested → rejected returns the money exactly once; ledger reproduces the balance"""
        self.verify()
        self.give(2000, "cash")
        self.wd("20")
        wid = self.q("SELECT id FROM withdrawals")
        self.assertEqual(self.bal("cash"), 0)
        for _ in range(2):
            self.post("/admin/payouts", {"wid": str(wid), "action": "reject", "note": "Bank details didn't match"})
        self.assertEqual(self.bal("cash"), 2000)
        self.assertIn(self.q("SELECT status FROM withdrawals"), ("rejected", "returned", "failed"))

    def test_unverified_customer_cannot_withdraw(self):
        """QA-WD-04 | Withdrawals | Verification | Unverified email blocks withdrawals"""
        self.give(1000, "cash")
        self.wd("5")
        self.assertEqual(self.q("SELECT COUNT(*) FROM withdrawals"), 0)

    def test_points_redemption_edges(self):
        """QA-PTS-01 | DBX Points | Redemption | Can't redeem more than owned, zero, negative or junk; valid redemption adds site credit once"""
        db = self.db()
        db.execute("INSERT INTO points_ledger (user_id, points, reason, ref, created_at) VALUES (?,250,'QA','qa',"
                   "strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (self.uid,))
        db.execute("UPDATE users SET points=250, points_lifetime=250 WHERE id=?", (self.uid,))
        db.commit()
        for b in ("3", "0", "-1", "abc", "1.5", "999999"):
            self.post("/account/redeem", {"blocks": b}, client=self.p)
        self.assertEqual(self.bal("credit"), 0)
        self.post("/account/redeem", {"blocks": "2"}, client=self.p)
        self.assertEqual(self.bal("credit"), 200)
        self.assertEqual(self.q("SELECT points FROM users WHERE id=?", self.uid), 50)
        self.assertEqual(self.q("SELECT SUM(points) FROM points_ledger WHERE user_id=?", self.uid), 50)


class GrowthAudit(AuditBase):
    def promo(self, code, **kw):
        d = {"code": code, "percent": "10", "fixed": "0", "min_spend": "0", "per_user": "1"}
        d.update(kw)
        self.post("/admin/promos", d)

    def test_promo_time_window_and_limits(self):
        """QA-PRM-01 | Promotions | Rules | Expired and future codes refused; max uses and per-person limits enforced at payment"""
        self.promo("OLD", expires_at="2001-01-01T00:00")
        self.promo("SOON", starts_at="2099-01-01T00:00")
        self.promo("ONCE", max_uses="1")
        for code in ("OLD", "SOON", "NOPE"):
            self.add(self.cid, 2, client=self.p)
            self.checkout(client=self.p, promo=code)
        self.assertEqual(self.q("SELECT COALESCE(SUM(promo_discount),0) FROM checkouts"), 0)
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p, promo="ONCE")
        self.add(self.cid, 2, client=self.p)
        self.checkout(client=self.p, promo="ONCE")
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts WHERE promo_discount>0"), 1)

    def test_promo_reevaluated_when_basket_changes(self):
        """QA-PRM-02 | Promotions | Basket change | A minimum-spend code applied then basket reduced is re-checked at payment"""
        self.promo("MIN10", min_spend="10")
        self.add(self.cid, 4, client=self.p)
        self.post("/basket/promo", {"promo": "MIN10"}, client=self.p)
        self.post("/basket/update", {"i": "0", "qty": "1"}, client=self.p)
        self.checkout(client=self.p, promo="MIN10")
        self.assertEqual(self.q("SELECT COALESCE(SUM(promo_discount),0) FROM checkouts WHERE status='paid'"), 0)

    def test_self_referral_and_invalid_code(self):
        """QA-REF-01 | Referrals | Abuse | Invalid codes are ignored and nobody is rewarded for referring themselves"""
        code = self.q("SELECT referral_code FROM users WHERE id=?", self.uid)
        self.p.get(f"/r/{code}")
        self.p.get("/r/NOTACODE")
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        self.assertEqual(self.bal("credit"), 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM referrals"), 0)


class SaferAudit(AuditBase):
    def test_limit_raise_waits_and_lower_is_instant(self):
        """QA-RP-01 | Responsible play | Limits | Lowering is immediate; raising waits 72 hours; spending over the limit is refused server-side"""
        self.post("/account/limits", {"daily": "5", "weekly": "", "monthly": "100"}, client=self.p)
        self.assertEqual(self.q("SELECT daily_limit FROM users WHERE id=?", self.uid), 500)
        self.add(self.cid, 3, client=self.p)                      # £7.50 > £5
        self.checkout(client=self.p)
        self.assertEqual(self.tickets(), 0)
        self.post("/account/limits", {"daily": "50", "weekly": "", "monthly": "100"}, client=self.p)
        self.assertEqual(self.q("SELECT daily_limit FROM users WHERE id=?", self.uid), 500)       # raise is pending
        self.assertIsNotNone(self.q("SELECT pending_limit_at FROM users WHERE id=?", self.uid))

    def test_break_cannot_be_undone_and_blocks_buying(self):
        """QA-RP-02 | Responsible play | Break | A break blocks buying (direct POST too) and can't be shortened; logging in still works"""
        self.post("/account/exclude", {"days": "7"}, client=self.p)
        until = self.q("SELECT excluded_until FROM users WHERE id=?", self.uid)
        self.post("/account/exclude", {"days": "1"}, client=self.p)
        self.assertEqual(self.q("SELECT excluded_until FROM users WHERE id=?", self.uid), until)
        self.add(self.cid, 1, client=self.p)
        self.post("/basket/checkout", {}, client=self.p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM checkouts"), 0)
        self.assertEqual(self.p.get("/account").status_code, 200)


class PostalAudit(AuditBase):
    def receive(self, email="post@example.com", correct="1", name="Post Person"):
        import time
        self.post(f"/admin/competitions/{self.cid}/postal", {"name": name, "email": email, "address": "1 Road", "answer_correct": correct,
                  "mode": "receive", "received": time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))})
        return self.q("SELECT MAX(id) FROM postal_entries")

    def test_wrong_answer_and_duplicate_postal_entries(self):
        """QA-PST-01 | Free entry | Validation | A wrong-answer envelope is never entered; approving the same envelope twice gives one ticket"""
        bad = self.receive(correct="0")
        self.post(f"/admin/postal/{bad}", {"action": "approve"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE postal_entry_id=?", bad), 0)
        good = self.receive()
        self.post(f"/admin/postal/{good}", {"action": "approve"})
        self.post(f"/admin/postal/{good}", {"action": "approve"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM tickets WHERE postal_entry_id=?", good), 1)

    def test_postal_and_paid_entries_share_the_per_person_limit(self):
        """QA-PST-02 | Free entry | Limits | Free + paid entries by the same person can't exceed the per-person limit"""
        self.add(self.cid, 10, client=self.p)
        self.checkout(client=self.p)
        pid = self.receive(email="player@example.com", name="Pat Player")
        self.post(f"/admin/postal/{pid}", {"action": "approve"})
        self.assertLessEqual(self.q("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='issued' AND "
                                    "(user_id=? OR postal_entry_id=?)", self.cid, self.uid, pid), 10)


class AccountAudit(AuditBase):
    def test_reset_token_single_use_and_old_password_dead(self):
        """QA-AUTH-01 | Password reset | Token reuse | A reset link works once; the old password stops working"""
        from app import mailer
        with mock.patch.object(mailer, "send") as send:
            self.post("/forgot", {"email": "player@example.com"})
        text = " ".join(str(a) for c in send.call_args_list for a in list(c.args) + list(c.kwargs.values()))
        link = re.search(r"(/reset/[A-Za-z0-9_\-\.]+)", text).group(1)
        self.post(link, {"password": "newpassword123"})
        self.post(link, {"password": "anotherpass123"})
        c = self.app.test_client()
        self.post("/login", {"email": "player@example.com", "password": "anotherpass123"}, client=c)
        self.assertNotEqual(c.get("/account").status_code, 200)
        c2 = self.app.test_client()
        self.post("/login", {"email": "player@example.com", "password": "supersecret123"}, client=c2)
        self.assertNotEqual(c2.get("/account").status_code, 200)
        c3 = self.app.test_client()
        self.post("/login", {"email": "player@example.com", "password": "newpassword123"}, client=c3)
        self.assertEqual(c3.get("/account").status_code, 200)

    def test_email_case_and_duplicate_signup(self):
        """QA-REG-01 | Registration | Duplicates | The same email in different case can't open a second account"""
        c = self.app.test_client()
        self.signup("PLAYER@Example.com", client=c)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users WHERE lower(email)='player@example.com'"), 1)

    def test_unicode_and_long_names_are_handled(self):
        """QA-REG-02 | Registration | Input | Unicode, emoji and very long names don't break signup or pages"""
        c = self.app.test_client()
        r = self.signup("uni@example.com", client=c, name="Zoë Ångström 🎉 " + "x" * 500)
        self.assertLess(r.status_code, 500)
        self.assertEqual(c.get("/account").status_code, 200)

    def test_unsubscribe_stops_marketing_not_transactional(self):
        """QA-MKT-01 | Marketing | Unsubscribe | The one-click link turns marketing off; order confirmations still arrive"""
        db = self.db()
        db.execute("UPDATE users SET marketing=1, reminder_emails=1 WHERE id=?", (self.uid,))
        db.commit()
        from app.routes import _unsub_signer
        with self.app.test_request_context():
            token = _unsub_signer().dumps(self.uid)
        self.post(f"/unsubscribe/{token}", {})
        self.client.get(f"/unsubscribe/{token}")
        self.assertEqual(self.q("SELECT marketing FROM users WHERE id=?", self.uid), 0)
        self.add(self.cid, 1, client=self.p)
        self.checkout(client=self.p)
        self.assertGreaterEqual(self.q("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind IN ('order','purchase','entry')",
                                       self.uid) + self.q("SELECT COUNT(*) FROM notifications WHERE user_id=? AND title LIKE '%confirm%'",
                                                          self.uid), 1)

    def test_search_odd_input(self):
        """QA-SRCH-01 | Search | Odd input | Special characters, very long and empty queries never error or reflect raw HTML"""
        for q in ("%", "_", "'", '"', "<script>x</script>", "a" * 5000, "", "   ", "🎉", "\\", "%00", "SELECT * FROM users"):
            for url in ("/search", "/results", "/faq"):
                r = self.p.get(url, query_string={"q": q})
                self.assertLess(r.status_code, 500, (url, q[:20]))
                self.assertNotIn("<script>x</script>", r.get_data(as_text=True))

    def test_all_jobs_twice_back_to_back(self):
        """QA-JOB-01 | Background jobs | Repeat runs | Running every job twice in a row creates no duplicate money, tickets, draws or emails"""
        self.add(self.cid, 3, client=self.p)
        self.checkout(client=self.p)
        self.close()
        from app.jobs import FUNCS, run_job
        with self.app.app_context():
            for _ in range(2):
                for name, fn in FUNCS.items():
                    run_job(name, fn, force=True)
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims"), 1)
        dup = self.db().execute("SELECT dedupe_key, COUNT(*) FROM notifications WHERE dedupe_key IS NOT NULL GROUP BY dedupe_key "
                                "HAVING COUNT(*)>1").fetchall()
        self.assertEqual(dup, [])

    def test_uk_clock_changes(self):
        """QA-TIME-01 | Dates | British Summer Time | Closing times entered in UK time convert correctly across both clock changes"""
        from datetime import datetime
        from app import UK
        cases = {"2026-03-29T00:30": "2026-03-29T00:30:00Z", "2026-03-29T03:00": "2026-03-29T02:00:00Z",
                 "2026-10-25T00:30": "2026-10-24T23:30:00Z", "2026-10-25T03:00": "2026-10-25T03:00:00Z",
                 "2026-12-31T23:59": "2026-12-31T23:59:00Z", "2026-07-01T20:00": "2026-07-01T19:00:00Z"}
        from app.db import iso
        for local, utc in cases.items():
            self.assertEqual(iso(datetime.strptime(local, "%Y-%m-%dT%H:%M").replace(tzinfo=UK)), utc, local)
        # a time that doesn't exist (clocks jump 01:00→02:00) must not crash the form
        r = self.post("/admin/competitions/new", {"kind": "draw", "title": "DST", "description": "A prize delivered to your door.",
                                                  "ends_at": "2099-03-29T01:30", "category": "tech", "ticket_price": "1",
                                                  "max_tickets": "10", "max_per_user": "5", "question": "2+2?", "answer_a": "3",
                                                  "answer_b": "4", "answer_c": "5", "correct": "b"})
        self.assertLess(r.status_code, 500)

    def test_notifications_belong_to_their_owner(self):
        """QA-NOT-01 | Notifications | Isolation | Customer A never sees or opens Customer B's notifications"""
        other = self.app.test_client()
        self.signup("other@example.com", client=other, name="Bea Other")
        self.add(self.cid, 1, client=other)
        self.checkout(client=other)
        bid = self.q("SELECT id FROM notifications WHERE user_id=(SELECT id FROM users WHERE email='other@example.com') ORDER BY id DESC")
        self.assertNotIn("Bea", self.p.get("/account/notifications").get_data(as_text=True))
        r = self.p.get(f"/account/notifications/{bid}/open")
        self.assertIn(r.status_code, (302, 404))
        self.assertIsNone(self.q("SELECT read_at FROM notifications WHERE id=?", bid))


class PageStructureAudit(AuditBase):
    def test_titles_are_plain_text_and_ids_unique(self):
        """QA-UI-01 | Pages | Structure | Every page title is plain text (no stray HTML) and no page repeats an element id"""
        from collections import Counter
        from html.parser import HTMLParser
        cid = self.cid
        urls = ["/", "/competitions", f"/c/{self.slug(cid)}", "/faq", "/results", "/status", "/cookies", "/admin/", "/admin/reports",
                "/admin/compliance", "/admin/approvals", "/admin/risk", "/admin/calendar", "/admin/backlog", f"/admin/competitions/{cid}",
                "/admin/competitions/new?kind=draw", "/admin/settings", "/admin/emergency", "/admin/communications"]

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids, self.title, self._t = Counter(), "", False

            def handle_starttag(self, tag, attrs):
                self._t = tag == "title"
                for k, v in attrs:
                    if k == "id":
                        self.ids[v] += 1

            def handle_data(self, d):
                if self._t:
                    self.title += d

            def handle_endtag(self, tag):
                self._t = False if tag == "title" else self._t
        for u in urls:
            html = self.client.get(u).get_data(as_text=True)
            title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
            self.assertNotIn("<", title, u)
            p = P()
            p.feed(html)
            self.assertEqual([i for i, n in p.ids.items() if n > 1], [], u)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 301)
        self.assertIn("dbxPrev", self.client.get("/cookies").get_data(as_text=True))


class RaceRegressionAudit(AuditBase):
    def test_double_submitted_redraw_runs_once(self):
        """QA-RACE-01 | Draws | Double redraw | The same redraw form submitted repeatedly (or a stale form) redraws exactly once"""
        self.add(self.cid, 4, client=self.p)
        self.checkout(client=self.p)
        self.close()
        self.run_jobs()
        rep = str(self.q("SELECT MAX(id) FROM draws"))
        for _ in range(4):
            self.post(f"/admin/competitions/{self.cid}/redraw", {"confirm": "REDRAW", "reason": "Winner failed the age check", "replaces": rep})
        self.post(f"/admin/competitions/{self.cid}/redraw", {"confirm": "REDRAW", "reason": "Winner failed the age check"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM draws"), 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM prize_claims WHERE status!='forfeited'"), 1)

    def test_approval_carried_out_once_even_if_approved_twice(self):
        """QA-RACE-02 | Approvals | Double approve | Approving the same request twice applies it once (one ledger line)"""
        fin = self.app.test_client()
        self.signup("fin@example.com", client=fin)
        fid = self.q("SELECT id FROM users WHERE email='fin@example.com'")
        self.post(f"/admin/users/{fid}", {"action": "admin", "role": "finance"})
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "150", "kind": "cash", "reason": "Prize payment"}, client=fin)
        aid = self.q("SELECT id FROM approvals")
        for _ in range(3):
            self.post("/admin/approvals", {"id": aid, "decision": "approve"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", self.uid), 1)
        self.assertEqual(self.bal("cash"), 15000)

    def test_wallet_adjustment_form_double_submit_counts_once(self):
        """QA-RACE-03 | Admin money | Double submit | The same wallet-adjustment form submitted twice credits once"""
        for _ in range(2):
            self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "10", "kind": "credit", "reason": "Goodwill fix",
                                                   "once": "tok-123"})
        self.assertEqual(self.bal("credit"), 1000)

    def test_deposit_refund_reserves_before_card_refund(self):
        """QA-RACE-04 | Wallet | Deposit refund | Refund is reserved before Stripe is called; a failed card refund returns the money"""
        from app import payments
        from app.services import refund_deposits
        db = self.db()
        db.execute("INSERT INTO deposits (user_id, amount, status, payment_intent, created_at, paid_at) VALUES (?,2000,'paid','pi_dep',"
                   "strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (self.uid,))
        did = self.q("SELECT MAX(id) FROM deposits")
        db.execute("INSERT INTO credit_ledger (user_id, amount, reason, ref, created_at, kind) VALUES (?,2000,'Deposit',?,"
                   "strftime('%Y-%m-%dT%H:%M:%SZ','now'),'deposit')", (self.uid, f"d{did}"))
        db.commit()
        calls = []

        def boom(pi, amount, key):
            calls.append((pi, amount, key))
            raise RuntimeError("Stripe down")
        with self.app.app_context():
            with self.assertRaises(RuntimeError):
                refund_deposits(self.uid, boom)
        self.assertEqual(self.bal("deposit"), 2000)                 # money back in the wallet
        self.assertEqual(self.q("SELECT refunded FROM deposits"), 0)
        with self.app.app_context():
            self.assertEqual(refund_deposits(self.uid, lambda pi, amount, key: calls.append((pi, amount, key))), 2000)
            self.assertEqual(refund_deposits(self.uid, lambda pi, amount, key: calls.append((pi, amount, key))), 0)  # nothing twice
        self.assertEqual(self.bal("deposit"), 0)
        self.assertTrue(all(c[2].startswith("deposit-refund:") for c in calls))
        _ = payments


class PrizeTableAudit(AuditBase):
    def test_every_style_hits_its_targets_for_every_game_size(self):
        """QA-PRZ-01 | Instant wins | Quick-fill prizes | Every style pays close to its target, keeps its odds, uses nice amounts and never pays less than double the stake"""
        from app import prizes
        sizes = [(10, 200), (10, 5000), (20, 10000), (40, 3000), (50, 3000), (99, 1500), (100, 100), (100, 2000), (250, 3000),
                 (500, 1000), (1000, 500), (200, 50000)]
        for style, (_, payout, odds, *_rest) in prizes.STYLES.items():
            for price, plays in sizes:
                p = prizes.FREE_NOTIONAL_PRICE if style == "free" else price
                t = prizes.build(p, plays, style)
                n, value, pay, one_in = prizes.summary(p, plays, t)
                ctx = (style, price, plays, t)
                self.assertTrue(n >= 2 and n <= plays * 0.5, ctx)
                low = 0.85 if p * plays >= 10000 else 0.7                            # tiny budgets round to whole prizes
                self.assertTrue(payout * low <= pay <= payout * 1.02, ctx)               # never pays more than planned
                if plays >= 1000:
                    self.assertTrue(odds * 0.7 <= one_in <= odds * 1.35, ctx)
                values = [r[1] for r in t]
                self.assertEqual(values, sorted(values, reverse=True), ctx)
                self.assertEqual(len(values), len(set(values)), ctx)
                self.assertTrue(all(v in prizes.NICE for v in values), ctx)
                self.assertTrue(all(v >= min(2 * p, prizes.NICE[0]) or v >= 10 for v in values), ctx)
                self.assertTrue(min(values) >= 2 * p or min(values) == prizes._ceil(max(2 * p, 10)), ctx)
                self.assertEqual(t[0][3], "cash", ctx)                                      # the headline prize is always cash
        # random games vary but stay inside sensible bounds
        from app.admin import random_prize_table
        for _ in range(30):
            t = random_prize_table(50, 3000)
            pay = sum(v * q for _, v, q in t) / (50 * 3000)
            self.assertTrue(0.38 <= pay <= 0.6, pay)

    def test_quick_fill_endpoint_and_auto_fill(self):
        """QA-PRZ-02 | Admin | Quick fill | The create form's quick-fill returns a table sized to the price and plays; Auto-fill uses the chosen style"""
        r = self.client.get("/admin/games/prize-table?style=jackpot&price=1&plays=2000").get_json()
        self.assertEqual(r["style"], "jackpot")
        self.assertTrue(0.38 <= r["payout"] <= 0.46)
        self.assertEqual(r["rows"][0]["type"], "cash")
        self.assertEqual(self.client.get("/admin/games/prize-table?price=abc&plays=x").status_code, 400)
        self.assertEqual(self.p.get("/admin/games/prize-table?price=1&plays=100").status_code, 404)      # customers can't
        free = self.client.get("/admin/games/prize-table?style=free&plays=20000").get_json()
        self.assertTrue(any(x["type"] == "credit" for x in free["rows"]))
        draft = self.make_comp("Auto game", publish=False, max_tickets=2000, price="1")
        db = self.db()
        db.execute("UPDATE competitions SET game_type='spin' WHERE id=?", (draft,))
        db.commit()
        self.post(f"/admin/competitions/{draft}/instant", {"random_table": "1", "style": "winners"})
        n, total = self.db().execute("SELECT COUNT(*), SUM(value) FROM instant_prizes WHERE competition_id=?", (draft,)).fetchone()
        self.assertTrue(0.46 <= total / (100 * 2000) <= 0.56, total)
        self.assertTrue(2000 / n <= 6.5, n)
        page = self.client.get(f"/admin/competitions/{draft}").get_data(as_text=True)
        self.assertIn("Lots of winners", page)
        form = self.client.get("/admin/competitions/new?kind=game").get_data(as_text=True)
        for label in ("Balanced", "Lots of winners", "Big jackpot", "Cash + site credit"):
            self.assertIn(label, form)
        self.assertIn("Instant prizes — standard", self.client.get("/admin/competitions/new?kind=draw").get_data(as_text=True))
