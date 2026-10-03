"""Performance budgets. Fails (exit 1) if a key page gets heavier or slower than agreed.

    python tests/perf_budget.py

Runs against a seeded local copy (same data as the crawler), so results are repeatable. What it measures per page:
  - HTML size (gzipped, as sent to phones)
  - every same-site file the page loads (stylesheet, scripts, fonts it preloads, images) and their total size
  - number of requests
  - inline script size
  - server time to build the page (median and 95th percentile of 15 requests)
Real-device numbers (Largest Contentful Paint etc.) still need checking with Lighthouse or PageSpeed Insights on
staging — see docs/TARGETS.md. Change a budget only on purpose, in the same commit as the change that needs it.
"""
import gzip
import os
import re
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

BUDGET = {
    "html_gz_kb": 15,          # per page, compressed
    "page_weight_kb": 250,     # everything the page loads on a first visit, excluding prize photos (seed has none;
                               # each uploaded photo is served as a ~640px thumbnail, lazy-loaded below the fold)
    "requests": 15,
    "inline_js_kb": 12,
    "server_p95_ms": 150,      # local test client, no network — catches slow queries and N+1s, not slow phones
    "css_gz_kb": 15,
}
STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def asset_urls(html):
    urls = []
    for pic in re.findall(r"<picture>(.*?)</picture>", html, re.S):
        m = re.search(r'<source[^>]+srcset="([^"\s,]+)', pic) or re.search(r'<img[^>]+src="([^"]+)"', pic)
        if m:
            urls.append(m.group(1))
    rest = re.sub(r"<picture>.*?</picture>", "", html, flags=re.S)
    urls += re.findall(r'<img[^>]+src="([^"]+)"', rest)
    urls += re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html)
    urls += re.findall(r'<link[^>]+rel="preload"[^>]+href="([^"]+)"', html)
    urls += re.findall(r'<script[^>]+src="([^"]+)"', html)
    return [u for u in dict.fromkeys(urls) if u.startswith("/") and not u.startswith("//")]


def size_of(client, url):
    path = url.split("?")[0]
    if path.startswith("/static/"):
        f = os.path.join(STATIC, path[len("/static/"):])
        if os.path.exists(f):
            data = open(f, "rb").read()
            return len(gzip.compress(data)) if f.endswith((".css", ".js", ".svg")) else len(data)
    r = client.get(url)
    return len(r.data)


def measure(client, url, n=15):
    times = []
    for _ in range(n):
        t = time.perf_counter()
        r = client.get(url)
        times.append((time.perf_counter() - t) * 1000)
        assert r.status_code == 200, (url, r.status_code)
    html = r.get_data(as_text=True)
    assets = asset_urls(html)
    inline_js = sum(len(s) for s in re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S))
    html_gz = len(gzip.compress(r.data))
    weight = html_gz + sum(size_of(client, a) for a in assets)
    times.sort()
    return {"url": url, "html_gz_kb": html_gz / 1024, "page_weight_kb": weight / 1024, "requests": 1 + len(assets),
            "inline_js_kb": inline_js / 1024, "server_p95_ms": times[int(len(times) * 0.95) - 1],
            "median_ms": statistics.median(times)}


def main():
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
    from app import create_app
    from qa_crawl import seed
    app = create_app({"TESTING": True, "DEMO_PAYMENTS": True, "SITE_URL": "http://localhost", "ADMIN_MFA": False,
                      "STRIPE_WEBHOOK_SECRET": "whsec_x", "POSTAL_ADDRESS": "PO Box 1", "REQUIRE_COMP_IMAGE": False})
    admin, player = seed(app)
    from app.db import _connect
    db = _connect(app.config["DATABASE"])
    live = db.execute("SELECT slug FROM competitions WHERE status='live' AND game_type='' ORDER BY id LIMIT 1").fetchone()[0]
    game = db.execute("SELECT slug FROM competitions WHERE status='live' AND game_type!='' AND free_daily=0 LIMIT 1").fetchone()[0]
    guest = app.test_client()
    pages = [(guest, "/"), (guest, "/competitions"), (guest, f"/c/{live}"), (guest, "/instant-wins"), (guest, "/results"),
             (guest, "/free-entry"), (player, "/account"), (player, "/account?tab=entries"), (player, f"/play/{game}"),
             (player, "/basket")]
    css = len(gzip.compress(open(os.path.join(STATIC, "style.css"), "rb").read())) / 1024
    over = []
    print(f"{'page':38} {'html gz':>8} {'weight':>8} {'reqs':>5} {'js':>6} {'p95 ms':>7}")
    for client, url in pages:
        m = measure(client, url)
        print(f"{url[:38]:38} {m['html_gz_kb']:7.1f}K {m['page_weight_kb']:7.0f}K {m['requests']:5} {m['inline_js_kb']:5.1f}K "
              f"{m['server_p95_ms']:7.0f}")
        for k in ("html_gz_kb", "page_weight_kb", "requests", "inline_js_kb", "server_p95_ms"):
            if m[k] > BUDGET[k]:
                over.append(f"{url}: {k} {m[k]:.1f} > budget {BUDGET[k]}")
    print(f"style.css gzipped: {css:.1f}K (budget {BUDGET['css_gz_kb']}K)")
    if css > BUDGET["css_gz_kb"]:
        over.append(f"style.css {css:.1f}K > budget {BUDGET['css_gz_kb']}K")
    print("\n".join(over) if over else "ALL WITHIN BUDGET")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
