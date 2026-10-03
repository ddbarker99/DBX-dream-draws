"""End-to-end QA harness.

  python tests/qa_crawl.py            -> seeds a realistic site, crawls every internal link as
                                         guest / player / admin, reports broken links, server errors,
                                         dead ends, placeholder text and missing SEO basics.
  python tests/qa_crawl.py --serve N  -> seeds the same data and serves it on port N (for browser checks).
"""
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ALLOW_DEV_KEY", "1")
os.environ["DATA_DIR"] = os.environ.get("QA_DATA_DIR") or tempfile.mkdtemp()

from app import create_app  # noqa: E402
from app.db import _connect  # noqa: E402

PLACEHOLDER = re.compile(r"lorem|TODO|FIXME|example\.com|Example Street|placeholder text|\{\{|\{%", re.I)
SKIP = ("/logout", "/stripe/webhook", "/sw.js", "/manifest.webmanifest")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.title, self.desc, self.h1, self.imgs_no_alt, self.canonical = [], None, None, 0, 0, None
        self.noindex, self.unlabelled, self._ids, self._labels = False, [], set(), set()
        self._t = False

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "title":
            self._t = True
        elif tag == "meta" and a.get("name") == "description":
            self.desc = a.get("content")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "img" and "alt" not in a:
            self.imgs_no_alt += 1
        elif tag == "meta" and a.get("name") == "robots" and "noindex" in (a.get("content") or ""):
            self.noindex = True
        if a.get("id"):
            self._ids.add(a["id"])
        if tag == "label" and a.get("for"):
            self._labels.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "checkbox", "radio") \
                and not a.get("aria-label") and not (a.get("id") and a["id"] in self._labels):
            self.unlabelled.append(a.get("name") or a.get("id") or tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._t = False

    def handle_data(self, d):
        if self._t:
            self.title = (self.title or "") + d


def csrf(c):
    html = c.get("/login").get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def post(c, url, data=None, **kw):
    d = dict(data or {})
    d["csrf"] = csrf(c)
    return c.post(url, data=d, **kw)


def seed(app):
    admin, player = app.test_client(), app.test_client()
    for c, email, name in ((admin, "admin@dbx.test", "Darren Barker"), (player, "player@dbx.test", "Sam Player")):
        post(c, "/signup", {"name": name, "email": email, "dob": "1990-01-01", "password": "supersecret123",
                            "agree": "1", "phone": "07700900123"})
    app.test_cli_runner().invoke(args=["make-admin", "admin@dbx.test"])
    db = _connect(app.config["DATABASE"])
    db.execute("UPDATE users SET email_verified=1")
    db.commit()

    def comp(title, price, tickets, game="", prize=None, ends="2099-01-01T20:00", tiers="", cash_alt=""):
        post(admin, "/admin/competitions/new", {
            "title": title, "description": "Brand new, boxed and delivered free to your door.\nDrawn live on our socials.",
            "ends_at": ends, "category": "tech" if not game else "cash", "game_type": game, "ticket_price": price,
            "max_tickets": str(tickets), "max_per_user": "100", "prize_value": "600", "discount_tiers": tiers,
            "cash_alternative": cash_alt, "auto_draw": "1", "featured": "1" if title.startswith("PS5") else "",
            "question": "What is the capital of Scotland?", "answer_a": "Glasgow", "answer_b": "Edinburgh",
            "answer_c": "Aberdeen", "correct": "b"})
        cid = db.execute("SELECT id FROM competitions ORDER BY id DESC").fetchone()[0]
        if prize:
            post(admin, f"/admin/competitions/{cid}/instant", prize)
        post(admin, f"/admin/competitions/{cid}/status", {"action": "publish"})
        return cid

    ps5 = comp("PS5 Pro Bundle", "1.49", 3000, tiers="10:10,25:15", cash_alt="£550",
               prize={"title": "£5 Cash", "value": "5", "type": "cash", "quantity": "60"})
    cash = comp("£1,000 Tax-Free Cash", "0.99", 5000)
    game = comp("50p Turbo Scratch", "0.50", 2000, game="scratch",
                prize={"title": "£2 Cash", "value": "2", "type": "cash", "quantity": "300"})
    drawn = comp("Apple Watch Ultra", "1.00", 50)
    post(admin, "/admin/games/free-daily", {"kind": "spin"})
    post(admin, "/admin/promos", {"code": "WELCOME10", "percent": "10", "fixed": "0", "min_spend": "0", "per_user": "5"})

    def buy(cid, qty):
        slug = db.execute("SELECT slug FROM competitions WHERE id=?", (cid,)).fetchone()[0]
        post(player, "/basket/add", {"slug": slug, "quantity": str(qty), "answer": "b"})
        r = post(player, "/basket/checkout", {"use_credit": "1"})
        m = re.search(r"/checkout/(\d+)/demo-pay", r.headers.get("Location", ""))
        if m:
            post(player, f"/checkout/{m.group(1)}/demo-pay")

    buy(ps5, 25)
    buy(game, 20)
    buy(drawn, 10)
    db.execute("UPDATE competitions SET ends_at='2000-01-01T00:00:00Z' WHERE id=?", (drawn,))
    db.commit()
    post(admin, f"/admin/competitions/{drawn}/draw")
    return admin, player


def crawl(client, who, start=("/",), limit=400):
    seen, queue, problems, pages = set(), list(start), [], {}
    while queue and len(seen) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = client.get(url)
        if r.status_code in (301, 302, 303, 308):
            loc = urlparse(r.headers["Location"])
            nxt = loc.path + (("?" + loc.query) if loc.query else "")
            if nxt not in seen:
                queue.append(nxt)
            continue
        if r.status_code >= 400:
            problems.append(f"[{who}] {r.status_code} {url}")
            continue
        if not r.mimetype.startswith("text/html"):
            continue
        html = r.get_data(as_text=True)
        p = Links()
        p.feed(html)
        pages[url] = p
        body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
        if PLACEHOLDER.search(re.sub(r"<[^>]+>", " ", body)):
            problems.append(f"[{who}] placeholder text on {url}: {PLACEHOLDER.search(re.sub(r'<[^>]+>', ' ', body)).group(0)!r}")
        for href in p.links:
            href, _ = urldefrag(href)
            full = urlparse(urljoin("http://localhost" + url, href))
            if full.scheme not in ("http", "https") or full.netloc not in ("localhost", "localhost:5000"):
                continue
            path = full.path + (("?" + full.query) if full.query else "")
            if path.startswith(SKIP) or "/uploads/" in path or path.endswith(".csv"):
                continue
            if path not in seen:
                queue.append(path)
    return pages, problems


def main():
    app = create_app({"TESTING": True, "DEMO_PAYMENTS": True, "SITE_URL": "http://localhost", "ADMIN_MFA": False,
                      "STRIPE_WEBHOOK_SECRET": "whsec_x", "POSTAL_ADDRESS": "DBX Dream Draws, PO Box 1, Testtown, TE1 1ST", "REQUIRE_COMP_IMAGE": False})
    admin, player = seed(app)
    if "--serve" in sys.argv:
        port = int(sys.argv[sys.argv.index("--serve") + 1])
        print("serving on", port, "data", os.environ["DATA_DIR"], flush=True)
        app.config["TESTING"] = False
        app.run(port=port)
        return
    all_problems, titles, descs = [], {}, {}
    for who, client in (("guest", app.test_client()), ("player", player), ("admin", admin)):
        start = ["/"] + (["/account"] if who != "guest" else []) + (["/admin/"] if who == "admin" else [])
        pages, problems = crawl(client, who, start)
        all_problems += problems
        for url, p in pages.items():
            if url.startswith("/admin"):
                continue
            if not p.desc:
                all_problems.append(f"[{who}] no meta description: {url}")
            if p.h1 != 1:
                all_problems.append(f"[{who}] {p.h1} <h1> on {url}")
            if p.unlabelled:
                all_problems.append(f"[{who}] form fields without a label on {url}: {p.unlabelled[:4]}")
            if p.imgs_no_alt:
                all_problems.append(f"[{who}] {p.imgs_no_alt} image(s) without alt on {url}")
            if p.noindex:
                continue
            titles.setdefault((p.title or "").strip(), []).append(url)
            descs.setdefault((p.desc or "").strip(), []).append(url)
            if not p.canonical:
                all_problems.append(f"[{who}] no canonical: {url}")
        print(f"{who}: crawled {len(pages)} pages")
    for t, urls in titles.items():
        bases = {u.split("?")[0] for u in urls}
        if len(bases) > 1:
            all_problems.append(f"duplicate <title> {t!r} on {sorted(bases)[:5]}")
    for d, urls in descs.items():
        bases = {u.split("?")[0] for u in urls}
        if len(bases) > 1:
            all_problems.append(f"duplicate description {d[:50]!r} on {sorted(bases)[:5]}")
    with app.app_context():                     # after every page has been visited, the data must still be consistent
        from app.checks import integrity_problems
        from app.db import get_db
        all_problems += [f"[integrity] {p}" for p in integrity_problems(get_db())]
    print("\n".join(sorted(set(all_problems))) or "NO PROBLEMS FOUND")
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())
