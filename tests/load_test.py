"""Load test for launches and closing periods. Run it against STAGING, never production.

    python tests/load_test.py https://staging.example.com --users 200 --seconds 60
    python tests/load_test.py http://127.0.0.1:5056 --users 50 --buyers 20 --slug ps5-pro-bundle --answer b

Each virtual user browses like a real visitor (home → competition → number picker → basket). With --buyers N,
N of them also sign up and try to buy the SAME ticket number at the same moment (needs DEMO_PAYMENTS=1 on
staging) — afterwards exactly one should hold it. Reports requests/second, p50/p95/p99 latency and errors.
Only the Python standard library is used. Set SIGNUP_RATE_LIMIT=1000 on staging first (every buyer signs up
from your one IP address).
"""
import argparse
import http.cookiejar
import random
import re
import statistics
import threading
import time
import urllib.parse
import urllib.request

lock = threading.Lock()
ANSWER = "a"
lat, errors, codes = [], [], {}


def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def hit(op, url, data=None):
    t = time.perf_counter()
    try:
        r = op.open(urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None), timeout=30)
        body, code = r.read().decode("utf-8", "replace"), r.status
        body = f"<!--url:{r.geturl()}-->" + body
    except urllib.error.HTTPError as e:
        body, code = "", e.code
    except Exception as e:                                # connection refused, timeout...
        with lock:
            errors.append(str(e)[:80])
        return ""
    with lock:
        lat.append(time.perf_counter() - t)
        codes[code] = codes.get(code, 0) + 1
        if code >= 500:
            errors.append(f"{code} {url}")
    return body


def csrf(body):
    m = re.search(r'name="csrf" value="([^"]+)"', body)
    return m.group(1) if m else ""


def browser(base, slug, until):
    op = opener()
    while time.time() < until:
        hit(op, base + "/")
        if slug:
            hit(op, f"{base}/c/{slug}")
            hit(op, f"{base}/c/{slug}/numbers?start={random.randint(0, 20) * 100 + 1}")
        hit(op, base + "/competitions")
        hit(op, base + "/basket")
        time.sleep(random.uniform(0.2, 1.0))


def buyer(base, slug, i, barrier, results):
    op = opener()
    token = csrf(hit(op, base + "/signup"))
    hit(op, base + "/signup", {"csrf": token, "name": f"Load Tester{i}", "email": f"load{i}.{int(time.time())}@example.test",
                               "dob": "1990-01-01", "password": "loadtest-password", "agree": "1"})
    page = hit(op, f"{base}/c/{slug}")
    token = csrf(page)
    answer = ANSWER
    hit(op, base + "/basket/add", {"csrf": token, "slug": slug, "numbers": "1", "answer": answer})
    page = hit(op, base + "/basket")
    idem = (re.search(r'name="idem" value="([^"]+)"', page) or [None, ""])[1]
    barrier.wait()
    body = hit(op, base + "/basket/checkout", {"csrf": csrf(page), "idem": idem})
    final = (re.search(r"<!--url:([^>]*)-->", body) or [None, ""])[1]
    results.append("/checkout/" in final)            # landed on a payment page = got the number


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--slug", default="")
    ap.add_argument("--buyers", type=int, default=0)
    ap.add_argument("--answer", default="a", help="correct answer letter (a/b/c) for the competition's question")
    a = ap.parse_args()
    global ANSWER
    ANSWER = a.answer
    base = a.base.rstrip("/")
    until = time.time() + a.seconds
    threads = [threading.Thread(target=browser, args=(base, a.slug, until), daemon=True) for _ in range(a.users)]
    results = []
    if a.buyers and a.slug:
        barrier = threading.Barrier(a.buyers)
        threads += [threading.Thread(target=buyer, args=(base, a.slug, i, barrier, results), daemon=True) for i in range(a.buyers)]
    start = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.time() - start
    q = sorted(lat)
    pct = lambda p: q[min(len(q) - 1, int(len(q) * p))] * 1000 if q else 0
    print(f"{len(q)} requests in {took:.0f}s = {len(q) / took:.1f}/s with {a.users} browsing users")
    print(f"latency ms: p50 {pct(.5):.0f} · p95 {pct(.95):.0f} · p99 {pct(.99):.0f} · mean {statistics.mean(q) * 1000 if q else 0:.0f}")
    print("status codes:", dict(sorted(codes.items())))
    print(f"errors: {len(errors)}", (errors[:5] if errors else ""))
    if results:
        print(f"{a.buyers} buyers raced for ticket #1: {sum(results)} reached payment (should be 1 — the others are told it's taken)")
    ok = not errors and pct(.95) < 2000 and (not results or sum(results) <= 1)
    print("PASS" if ok else "CHECK: errors, slow p95 (>2s) or more than one buyer got the same number")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
