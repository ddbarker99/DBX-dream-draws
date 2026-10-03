"""Synthetic monitoring: visit the live site like a customer and fail loudly if a critical page is broken or slow.

    python tests/synthetic_check.py https://dbxdreamdraws.co.uk

Run it from somewhere other than the server (it can't warn you if the server itself is down): the scheduled GitHub
workflow `.github/workflows/uptime.yml` runs it every 15 minutes, and UptimeRobot can watch /healthz/deep as well.
Read-only — it never logs in, buys or submits anything. Standard library only. Exit code 0 = all good.
"""
import json
import sys
import time
import urllib.error
import urllib.request

SLOW_SECONDS = 3.0


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "DBX-synthetic-monitor/1.0 (+monitoring)"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body, time.monotonic() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.monotonic() - t
    except Exception as e:                       # DNS, TLS, timeout, connection refused
        return 0, str(e), time.monotonic() - t


def main(base):
    base = base.rstrip("/")
    problems, lines = [], []

    def check(name, path, must_contain=None, want=200):
        status, body, secs = get(base + path)
        ok = status == want and (must_contain is None or must_contain in body)
        lines.append(f"{'OK  ' if ok else 'FAIL'} {name:28} {status} {secs:5.2f}s")
        if not ok:
            problems.append(f"{name}: HTTP {status}" + (f" (missing {must_contain!r})" if status == want else ""))
        elif secs > SLOW_SECONDS:
            problems.append(f"{name}: slow ({secs:.1f}s)")
        return status, body

    check("Homepage", "/", "</html>")
    check("Competitions", "/competitions", "</html>")
    check("Login page", "/login", 'name="password"')
    check("Free entry page", "/free-entry", "</html>")
    status, body = check("Competition API", "/api/competitions", '"competitions"')
    if status == 200:
        try:
            comps = json.loads(body)["competitions"]
            if comps:
                check("A live competition page", "/c/" + comps[0]["slug"], "</html>")
        except (ValueError, KeyError):
            problems.append("Competition API returned invalid JSON")
    status, body = check("Deep health", "/healthz/deep")
    if status in (200, 503):
        try:
            for k, v in json.loads(body)["checks"].items():
                if not v:
                    problems.append(f"Deep health: {k} failing")
        except (ValueError, KeyError):
            problems.append("Deep health returned invalid JSON")
    print("\n".join(lines))
    print("\n".join(problems) if problems else "ALL CHECKS PASSED")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: synthetic_check.py https://yourdomain")
    sys.exit(main(sys.argv[1]))
