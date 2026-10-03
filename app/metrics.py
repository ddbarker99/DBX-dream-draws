"""Site reliability numbers kept by the app itself: request counts, server time and errors.

Each web worker counts in memory and writes a summary at most once a minute (so measuring never slows pages down).
Errors are grouped by where they happened, with a count, so one bug shows as one line, not thousands.
"""
import re
import threading
import time
import traceback

from .db import iso, utcnow

BUCKETS = [(100, "lt100"), (250, "lt250"), (500, "lt500"), (1000, "lt1000"), (2500, "lt2500"), (None, "ge2500")]
_lock = threading.Lock()
_buf = {}
_last_flush = [time.monotonic()]


def record(ms, status):
    key = next(name for limit, name in BUCKETS if limit is None or ms < limit)
    with _lock:
        for k in ("requests", key) + (("errors_5xx",) if status >= 500 else ()):
            _buf[k] = _buf.get(k, 0) + 1


def maybe_flush(db, force=False):
    if not force and time.monotonic() - _last_flush[0] < 60:
        return
    with _lock:
        data = dict(_buf)
        _buf.clear()
        _last_flush[0] = time.monotonic()
    if not data:
        return
    day = iso(utcnow())[:10]
    for k, n in data.items():
        db.execute("INSERT INTO request_stats (day, metric, n) VALUES (?,?,?) ON CONFLICT(day, metric) DO UPDATE SET n=n+excluded.n",
                   (day, k, n))


_SCRUB = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b\d(?:[ -]?\d){11,18}\b"), "[card/account number]"),
    (re.compile(r"\b\d{2}-\d{2}-\d{2}\b"), "[sort code]"),
    (re.compile(r"\b\d{8}\b"), "[number]"),
    (re.compile(r"(?i)(password|secret|token|key|authorization)([\"']?\s*[:=]\s*)[^\s,'\")]+"), r"\1\2[hidden]"),
    (re.compile(r"\b(?:sk|rk|whsec|pk)_(?:live|test)?_?[A-Za-z0-9]{8,}"), "[stripe key]"),
]


def scrub(text):
    """Remove personal and secret data from error text before it's stored or emailed."""
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text or "")
    return text


def record_error(db, exc, endpoint, path, method="GET", user_id=None):
    """Store a server error, grouped by where it happened, with enough context to fix it and no personal data:
    the code location and stack, the page (no query string), method, release, and the account number at most."""
    from flask import current_app
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    where = next((f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in reversed(tb) if "/app/" in f.filename), "?")
    sig = f"{type(exc).__name__}@{where}@{endpoint}"
    now = iso(utcnow())
    context = f"{method} {path.split('?')[0][:200]} · endpoint {endpoint} · release {current_app.config.get('RELEASE', '?')}" \
              + (f" · account #{user_id}" if user_id else "")
    trace = scrub(context + "\n\n" + "".join(traceback.format_exception(exc)))[-3500:]
    new = db.execute("SELECT 1 FROM error_log WHERE signature=? AND resolved_at IS NULL", (sig,)).fetchone() is None
    db.execute("INSERT INTO error_log (signature, first_at, last_at, count, endpoint, path, error, trace) VALUES (?,?,?,?,?,?,?,?) "
               "ON CONFLICT(signature) DO UPDATE SET last_at=excluded.last_at, count=count+1, path=excluded.path, "
               "error=excluded.error, trace=excluded.trace, resolved_at=NULL",
               (sig, now, now, 1, endpoint, path.split("?")[0][:300], scrub(f"{type(exc).__name__}: {exc}")[:500], trace))
    if new and current_app.config.get("SUPPORT_EMAIL") and not current_app.testing:
        try:                                       # tell developers at once about a new kind of error
            from . import mailer
            mailer.send(current_app.config["SUPPORT_EMAIL"], f"⚠ New server error: {type(exc).__name__} on {endpoint}",
                        trace[-2500:] + "\n\nAdmin → Targets → Server errors", heading="New server error")
        except Exception:
            pass
    return new


def stats(db, days=7):
    """Totals for the last `days` days: requests, 5xx errors and an estimated 95th-percentile server time."""
    since = iso(utcnow())[:10] if days <= 1 else None
    rows = db.execute("SELECT metric, SUM(n) FROM request_stats WHERE day>=date('now', ?) GROUP BY metric",
                      (f"-{days - 1} days",)).fetchall() if not since else \
        db.execute("SELECT metric, SUM(n) FROM request_stats WHERE day=? GROUP BY metric", (since,)).fetchall()
    m = {k: v for k, v in rows}
    total = m.get("requests", 0)
    p95 = None
    if total:
        seen = 0
        for limit, name in BUCKETS:
            seen += m.get(name, 0)
            if seen >= 0.95 * total:
                p95 = limit or 5000
                break
    return {"requests": total, "errors": m.get("errors_5xx", 0), "p95_under_ms": p95,
            "error_rate": (m.get("errors_5xx", 0) / total * 100) if total else None}
