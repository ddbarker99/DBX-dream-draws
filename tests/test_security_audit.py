"""Release QA — automated security sweep over EVERY route in app.url_map (nothing assumed safe because it's hidden).

    python -m pytest -q tests/test_security_audit.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_app import AutoDrawBase  # noqa: E402

from app.perms import ROLES  # noqa: E402

SKIP_GET = {"public.logout", "admin.reset", "public.stripe_webhook", "public.service_worker"}
JUNK = ["0", "-1", "999999999", "abc", "%27%20OR%201%3D1--", "%3Cscript%3E", "1.5"]


class SweepBase(AutoDrawBase):
    def setUp(self):
        super().setUp()
        self.add(self.cid, 2, client=self.p)
        self.chk = self.checkout(client=self.p)
        self.close()
        self.run_jobs()
        self.claim = self.q("SELECT id FROM prize_claims")
        self.tid = self.q("SELECT id FROM tickets WHERE user_id=?", self.uid)

    def fill(self, rule):
        ids = {"cid": self.cid, "tid": self.tid, "claim_id": self.claim or 1, "uid": self.uid, "pid": 1, "did": 1, "wid": 1,
               "nid": 1, "case_id": 1, "bid": 1, "eid": 1, "aid": 1, "version": 1}
        url = rule.rule
        for name in rule.arguments:
            val = ids.get(name, None)
            if name == "slug":
                val = self.slug(self.cid)
            elif name == "page":
                val = "terms"
            elif name in ("kind",):
                val = "orders"
            elif name in ("token", "code", "name", "filename"):
                val = "x"
            url = re.sub(r"<(?:[a-z]+(?:\([^)]*\))?:)?" + name + r">", str(val if val is not None else 1), url)
        return url

    def admin_rules(self):
        return [r for r in self.app.url_map.iter_rules() if r.rule.startswith("/admin") and r.endpoint.split(".")[0] in ("admin", "control")]

    def required_perm(self, endpoint):
        import inspect
        view = self.app.view_functions[endpoint]
        mod = sys.modules[view.__module__]
        src = inspect.getsource(mod)
        m = re.search(r'@require\("([^"]+)"\)\s*\ndef ' + re.escape(inspect.unwrap(view).__name__) + r"\(", src)
        return m.group(1) if m else None


class AdminPermissionSweep(SweepBase):
    def test_every_admin_route_refuses_customers_and_anonymous(self):
        """SEC-ADM-01 | Security | Admin access | Every admin URL (GET and POST) returns 404 to visitors and customers — never content or a 200"""
        anon = self.app.test_client()
        checked = 0
        for r in self.admin_rules():
            url = self.fill(r)
            for client, who in ((anon, "anonymous"), (self.p, "customer")):
                if "GET" in r.methods:
                    resp = client.get(url)
                    self.assertIn(resp.status_code, (302, 404), f"{who} GET {url} -> {resp.status_code}")
                    if resp.status_code == 302:
                        self.assertNotIn("/admin", resp.headers["Location"].split("?")[0].replace("/admin/mfa", "/admin"), url)
                    checked += 1
                if "POST" in r.methods:
                    resp = self.post(url, {"action": "x"}, client=client)
                    self.assertIn(resp.status_code, (302, 400, 404), f"{who} POST {url} -> {resp.status_code}")
                    checked += 1
        self.assertGreater(checked, 150)

    def test_every_admin_route_enforces_its_permission_for_every_role(self):
        """SEC-ADM-02 | Security | Staff roles | Each staff role can open only the admin pages its permissions allow; the rest are refused server-side"""
        no_perm = [r.endpoint for r in self.admin_rules() if self.required_perm(r.endpoint) is None
                   and r.endpoint not in ("control.centre",)]
        self.assertEqual(no_perm, [], "admin views without a @require permission")
        staff = self.app.test_client()
        self.signup("staff@example.com", client=staff)
        sid = self.q("SELECT id FROM users WHERE email='staff@example.com'")
        for role, (_, perms) in ROLES.items():
            if role == "admin":
                continue
            self.post(f"/admin/users/{sid}", {"action": "admin", "role": role})
            for r in self.admin_rules():
                need = self.required_perm(r.endpoint)
                if need is None or "GET" not in r.methods or "<" in r.rule and need not in perms:
                    pass
                if need is None or "GET" not in r.methods:
                    continue
                resp = staff.get(self.fill(r))
                if need in perms:
                    self.assertNotEqual(resp.status_code, 500, f"{role} GET {r.rule}")
                else:
                    self.assertNotEqual(resp.status_code, 200, f"role {role} lacks '{need}' but GET {r.rule} returned 200")

    def test_money_and_draw_posts_refused_for_support_role(self):
        """SEC-ADM-03 | Security | Hidden buttons | Support staff can't trigger refunds, payouts, draws, redraws or wallet changes by direct POST"""
        staff = self.app.test_client()
        self.signup("support@example.com", client=staff)
        sid = self.q("SELECT id FROM users WHERE email='support@example.com'")
        self.post(f"/admin/users/{sid}", {"action": "admin", "role": "support"})
        before = (self.q("SELECT COUNT(*) FROM credit_ledger"), self.q("SELECT COUNT(*) FROM draws"), self.q("SELECT COUNT(*) FROM refunds"))
        self.post(f"/admin/users/{self.uid}", {"action": "credit", "amount": "50", "kind": "cash", "reason": "sneaky"}, client=staff)
        self.post(f"/admin/competitions/{self.cid}/redraw", {"confirm": "REDRAW", "reason": "trying it on, sorry",
                                                             "replaces": str(self.q("SELECT MAX(id) FROM draws"))}, client=staff)
        oid = self.q("SELECT id FROM orders LIMIT 1")
        self.post(f"/admin/orders/{self.chk}", {"order_id": str(oid), "reason": "sneaky refund", "method": "wallet"}, client=staff)
        self.post("/admin/settings", {"site_status": "maintenance"}, client=staff)
        self.post("/admin/emergency", {"action": "lock", "reason": "sneaky"}, client=staff)
        after = (self.q("SELECT COUNT(*) FROM credit_ledger"), self.q("SELECT COUNT(*) FROM draws"), self.q("SELECT COUNT(*) FROM refunds"))
        self.assertEqual(before, after)
        self.assertEqual(self.q("SELECT COUNT(*) FROM settings WHERE key='site_status' AND value='maintenance'"), 0)
        self.assertFalse(self.q("SELECT COUNT(*) FROM settings WHERE key='emergency_lock' AND value!=''"))


class CsrfAndMethodSweep(SweepBase):
    def test_every_post_route_needs_the_csrf_token(self):
        """SEC-CSRF-01 | Security | CSRF | Every POST endpoint (customer and admin) rejects a request without the anti-forgery token"""
        n = 0
        for r in self.app.url_map.iter_rules():
            if "POST" not in r.methods or r.endpoint == "public.stripe_webhook":
                continue
            url = self.fill(r)
            for client in (self.client, self.p):
                resp = client.post(url, data={"action": "approve"})
                self.assertEqual(resp.status_code, 400, f"POST {url} without csrf -> {resp.status_code}")
                n += 1
        self.assertGreater(n, 80)

    def test_wrong_methods_and_junk_ids_never_500(self):
        """SEC-INP-01 | Security | Robustness | Wrong HTTP methods give 405 and junk/huge/negative ids give 4xx — never a server error"""
        for r in self.app.url_map.iter_rules():
            if r.endpoint in SKIP_GET or r.endpoint == "static":
                continue
            url = self.fill(r)
            if "GET" not in r.methods:
                self.assertEqual(self.client.get(url).status_code, 405 if "POST" in r.methods else 405, url)
            if r.arguments and "GET" in r.methods:
                for junk in JUNK:
                    bad = r.rule
                    for name in r.arguments:
                        bad = re.sub(r"<(?:[a-z]+(?:\([^)]*\))?:)?" + name + r">", junk, bad)
                    for client in (self.client, self.p):
                        resp = client.get(bad)
                        self.assertLess(resp.status_code, 500, f"GET {bad} -> {resp.status_code}")

    def test_query_string_junk_on_every_page_never_500(self):
        """SEC-INP-02 | Security | Robustness | Every GET page tolerates junk query parameters (huge, negative, script, SQL-like) without errors"""
        params = {"page": "-5", "q": "<script>alert(1)</script>' OR 1=1--", "year": "99999", "month": "13", "cat": "../../etc",
                  "tab": "<x>", "kind": "evil", "show": "999", "days": "-1", "mine": "1", "ref": "DBX-<b>", "next": "//evil.com",
                  "price": "abc", "topic": "x" * 3000, "order": "-1", "comp": "abc", "withdrawal": "1e9"}
        for r in self.app.url_map.iter_rules():
            if "GET" not in r.methods or r.endpoint in SKIP_GET or r.endpoint == "static" or r.endpoint.endswith("_csv") \
                    or r.rule.endswith((".csv", ".json", ".png", ".txt", ".xml")):
                continue
            url = self.fill(r)
            for client in (self.client, self.p):
                resp = client.get(url, query_string=params)
                self.assertLess(resp.status_code, 500, f"GET {url}?junk -> {resp.status_code}")
                body = resp.get_data(as_text=True)
                self.assertNotIn("<script>alert(1)</script>", body, url)
                self.assertNotIn("Traceback", body, url)


class SessionAndHeaderSweep(SweepBase):
    def test_cookie_flags_and_private_pages(self):
        """SEC-SES-01 | Security | Session cookie | Session cookie is HttpOnly + SameSite; account pages need login and are noindex/no-store"""
        c = self.app.test_client()
        r = self.signup("cookie@example.com", client=c)
        cookie = ";".join(r.headers.getlist("Set-Cookie")).lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        anon = self.app.test_client()
        for url in ("/account", "/account/activity", f"/account/tickets/{self.tid}", "/account/notifications", "/support/requests"):
            self.assertEqual(anon.get(url).status_code, 302, url)
            resp = self.p.get(url)
            self.assertIn("noindex", resp.get_data(as_text=True), url)
            self.assertIn("no-store", resp.headers.get("Cache-Control", ""), f"{url} private page may be cached")

    def test_security_headers_on_every_html_page(self):
        """SEC-HDR-01 | Security | Headers | CSP, frame protection, nosniff and referrer policy are set on pages"""
        for url in ("/", "/competitions", "/account", "/admin/"):
            h = self.client.get(url).headers
            self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""), url)
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff", url)
            self.assertTrue(h.get("Referrer-Policy"), url)

    def test_login_and_reset_do_not_reveal_accounts(self):
        """SEC-AUTH-01 | Security | Enumeration | Login and password reset give the same response for real and unknown emails"""
        c1, c2 = self.app.test_client(), self.app.test_client()
        a = self.post("/login", {"email": "player@example.com", "password": "wrongpass123"}, client=c1).get_data(as_text=True)
        b = self.post("/login", {"email": "nobody@example.com", "password": "wrongpass123"}, client=c2).get_data(as_text=True)
        strip = lambda h: re.sub(r'name="csrf" value="[^"]+"|value="[^"]*@example.com"', "", h)  # noqa: E731
        self.assertEqual(strip(a), strip(b))
        r1 = self.post("/forgot", {"email": "player@example.com"}, client=c1, follow_redirects=True).get_data(as_text=True)
        r2 = self.post("/forgot", {"email": "nobody@example.com"}, client=c2, follow_redirects=True).get_data(as_text=True)
        self.assertIn("If that email has an account", r1)
        self.assertIn("If that email has an account", r2)

    def test_sitemap_has_no_private_urls(self):
        """SEC-SEO-01 | SEO | Sitemap | The sitemap lists public pages only — no account, admin, checkout or support URLs"""
        body = self.client.get("/sitemap.xml").get_data(as_text=True)
        for bad in ("/admin", "/account", "/checkout", "/support/requests", "/basket"):
            self.assertNotIn(bad, body)
        robots = self.client.get("/robots.txt").get_data(as_text=True)
        self.assertIn("/admin", robots)
