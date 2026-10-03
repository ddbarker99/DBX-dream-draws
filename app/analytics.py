"""Cookie-free journey counts. Each step of the journey adds 1 to a row for (day, step, device type, competition).
No identifier, cookie or IP address is stored, so nothing can be tied back to a person — which is why this
needs no consent banner. Bots are ignored."""
from flask import request

from .db import get_db, utcnow

STEPS = [("home", "Homepage"), ("competition", "Competition page"), ("select", "Started choosing entries"),
         ("add", "Added to basket"), ("basket", "Basket"),
         ("checkout", "Started checkout"), ("paid", "Paid")]
BOT = ("bot", "crawl", "spider", "slurp", "preview", "monitor", "curl", "python-requests", "headless")


def device_type(agent=None):
    a = (agent if agent is not None else (request.user_agent.string if request else "")).lower()
    if any(b in a for b in BOT) or not a:
        return None
    if "ipad" in a or "tablet" in a:
        return "tablet"
    if "mobi" in a or "iphone" in a or "android" in a:
        return "mobile"
    return "desktop"


def count(step, comp_id=0, device=None, db=None):
    device = device or device_type()
    if device is None:
        return
    (db or get_db()).execute(
        "INSERT INTO funnel_counts (day, step, device, comp_id, n) VALUES (?,?,?,?,1) "
        "ON CONFLICT(day, step, device, comp_id) DO UPDATE SET n=n+1",
        (utcnow().strftime("%Y-%m-%d"), step, device, comp_id or 0))
