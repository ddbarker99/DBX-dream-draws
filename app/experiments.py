"""Controlled A/B tests of legitimate UX changes, judged by completed purchases — not clicks.

Lawful by design: only logged-in customers take part (no tracking cookies), each account always gets the same
variant (worked out from the account number, nothing stored on the device), and only staff with the settings
permission can start or stop a test. Experiments are declared here in code so every variant is reviewed like any
other change; staff decide when each runs (Admin → Experiments).
"""
import hashlib
import math

from flask import g

from .db import get_db, iso, utcnow

EXPERIMENTS = {
    "basket_button": {
        "name": "Basket payment button wording",
        "variants": {"a": "Pay £X securely", "b": "Continue to secure payment"},
        "why": "Does saying where the button goes (rather than the amount) help more people finish paying?",
    },
}


def _bucket(key, user_id, n):
    return int(hashlib.sha256(f"{key}:{user_id}".encode()).hexdigest(), 16) % n


def running(db=None):
    db = db or get_db()
    return {r[0] for r in db.execute("SELECT key FROM experiments WHERE status='running'")}


def variant(key):
    """The variant this visitor sees. Everyone not taking part sees 'a' (the current design)."""
    exp = EXPERIMENTS.get(key)
    user = g.get("user")
    if not exp or not user or user["is_admin"]:
        return "a"
    cache = g.setdefault("_exp", {})
    if key in cache:
        return cache[key]
    db = get_db()
    if key not in running(db):
        cache[key] = "a"
        return "a"
    names = sorted(exp["variants"])
    v = names[_bucket(key, user["id"], len(names))]
    db.execute("INSERT OR IGNORE INTO experiment_members (key, user_id, variant, exposed_at) VALUES (?,?,?,?)",
               (key, user["id"], v, iso(utcnow())))
    cache[key] = v
    return v


def record_conversion(db, user_id):
    """Called when a customer completes a purchase."""
    db.execute("UPDATE experiment_members SET converted_at=? WHERE user_id=? AND converted_at IS NULL AND key IN "
               "(SELECT key FROM experiments WHERE status='running')", (iso(utcnow()), user_id))


def results(db, key):
    rows = {r["variant"]: (r["n"], r["c"]) for r in db.execute(
        "SELECT variant, COUNT(*) n, SUM(converted_at IS NOT NULL) c FROM experiment_members WHERE key=? GROUP BY variant", (key,))}
    out = []
    for v in sorted(EXPERIMENTS[key]["variants"]):
        n, c = rows.get(v, (0, 0))
        out.append({"variant": v, "label": EXPERIMENTS[key]["variants"][v], "n": n, "c": c or 0,
                    "rate": round(100 * (c or 0) / n, 1) if n else None})
    verdict = "Not enough people yet — keep it running until each version has at least 100."
    if len(out) == 2 and all(o["n"] >= 100 for o in out):
        (a, b) = out
        p = (a["c"] + b["c"]) / (a["n"] + b["n"])
        se = math.sqrt(p * (1 - p) * (1 / a["n"] + 1 / b["n"])) if 0 < p < 1 else 0
        z = ((b["c"] / b["n"]) - (a["c"] / a["n"])) / se if se else 0
        if abs(z) >= 1.96:
            better = b if z > 0 else a
            verdict = f"Clear difference (95% confidence): “{better['label']}” completes more purchases."
        else:
            verdict = "No clear difference yet — the gap could be chance. Keep the simpler design unless it keeps diverging."
    return out, verdict
