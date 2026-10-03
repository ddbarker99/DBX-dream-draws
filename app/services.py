"""Business rules: basket checkout, ticket numbers, instant wins, wallet,
promo codes, referrals, spend limits and the provably fair draw."""
import hashlib
import hmac
import json
import random
import secrets
from datetime import timedelta

from flask import current_app

from zoneinfo import ZoneInfo

from .db import get_db, iso, parse_iso, utcnow, write_txn

UK = ZoneInfo("Europe/London")
MIN_WITHDRAWAL = 500       # £5

HOLD_MINUTES = 45          # tickets held for an unpaid checkout (Stripe sessions die at 30)
STRIPE_MIN = 30            # pence
MAX_PICKS = 100            # hand-picked numbers per line
_rand = random.SystemRandom()

CATEGORIES = [
    ("cars", "Cars & Bikes"), ("tech", "Tech"), ("cash", "Cash"), ("holidays", "Holidays"),
    ("home", "Home & Garden"), ("watches", "Watches & Jewellery"), ("other", "Other"),
]
CATEGORY_NAMES = dict(CATEGORIES)


class PurchaseError(Exception):
    pass


# ---------------- provably fair maths ----------------

def new_seed():
    seed = secrets.token_hex(32)
    return seed, hashlib.sha256(seed.encode()).hexdigest()


def entries_digest(numbers):
    return hashlib.sha256(",".join(str(n) for n in sorted(numbers)).encode()).hexdigest()


def pick_index(seed, digest, count):
    return int(hmac.new(seed.encode(), digest.encode(), hashlib.sha256).hexdigest(), 16) % count


def instant_commitment(salt, prizes):
    """Hash of every instant-win number + prize, published before sales."""
    body = ",".join(f"{p['number']}:{p['title']}" for p in sorted(prizes, key=lambda p: p["number"]))
    return hashlib.sha256(f"{salt}|{body}".encode()).hexdigest()


# ---------------- pricing ----------------

def parse_tiers(text):
    """'10:10, 25:15' -> [(25, 15), (10, 10)]  (buy 25+ get 15% off, 10+ get 10%)."""
    tiers = []
    for part in (text or "").replace(" ", "").split(","):
        if ":" in part:
            try:
                q, p = part.split(":")
                q, p = int(q), int(p)
                if q > 1 and 0 < p < 90:
                    tiers.append((q, p))
            except ValueError:
                pass
    return sorted(tiers, reverse=True)


def line_price(comp, qty):
    """-> (gross, discount, net, pct)"""
    gross = comp["ticket_price"] * qty
    pct = next((p for q, p in parse_tiers(comp["discount_tiers"]) if qty >= q), 0)
    discount = gross * pct // 100
    return gross, discount, gross - discount, pct


# ---------------- competition state ----------------

def comp_state(comp, sold=None):
    if comp["status"] != "live":
        return comp["status"]
    if parse_iso(comp["ends_at"]) <= utcnow():
        return "ended"
    if sold is not None and sold >= comp["max_tickets"]:
        return "soldout"
    return "live"


def sold_count(db, comp_id):
    return db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='issued'", (comp_id,)).fetchone()[0]


def taken_count(db, comp_id):
    return db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=?", (comp_id,)).fetchone()[0]


def taken_numbers(db, comp_id, lo=None, hi=None):
    q, a = "SELECT number FROM tickets WHERE competition_id=?", [comp_id]
    if lo is not None:
        q += " AND number BETWEEN ? AND ?"
        a += [lo, hi]
    return {r[0] for r in db.execute(q, a)}


def user_ticket_count(db, comp_id, user_id):
    return db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND user_id=?",
                      (comp_id, user_id)).fetchone()[0]


def is_new(comp):
    return parse_iso(comp["created_at"]) > utcnow() - timedelta(hours=48)


# ---------------- wallet ----------------

def balance(db, user_id, kind=None):
    """kind=None: everything; 'cash': withdrawable winnings; 'credit': site credit (spend only)."""
    if kind:
        return db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? AND kind=?",
                          (user_id, kind)).fetchone()[0]
    return db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=?", (user_id,)).fetchone()[0]


def balances(db, user_id):
    rows = dict(db.execute("SELECT kind, COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=? GROUP BY kind",
                           (user_id,)).fetchall())
    cash, credit, deposit = rows.get("cash", 0), rows.get("credit", 0), rows.get("deposit", 0)
    return {"cash": cash, "credit": credit, "deposit": deposit, "total": cash + credit + deposit}


def add_credit(db, user_id, amount, reason, ref=None, kind="credit"):
    db.execute("INSERT INTO credit_ledger (user_id, amount, reason, ref, created_at, kind) VALUES (?,?,?,?,?,?)",
               (user_id, amount, reason, ref, iso(utcnow()), kind))


def prize_kind(prize):
    """'cash' | 'credit' | 'physical' (older prizes have no type: credit if they paid credit)."""
    if prize["prize_type"]:
        return prize["prize_type"]
    return "credit" if prize["credit_amount"] else "physical"


def pay_prize(db, user_id, prize):
    kind = prize_kind(prize)
    if kind in ("cash", "credit") and prize["credit_amount"]:
        label = "Instant cash win" if kind == "cash" else "Instant win"
        add_credit(db, user_id, prize["credit_amount"], f"{label}: {prize['title']}", f"ip{prize['id']}", kind=kind)
        return True
    return False


def _clean_digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


def request_withdrawal(user, amount, details):
    """Cash winnings only. details: method 'bank' (account_name, sort_code, account_number) or 'paypal' (paypal_email)."""
    if not user["email_verified"]:
        raise PurchaseError("Please verify your email address before withdrawing — check your inbox or resend it from your account.")
    if amount < MIN_WITHDRAWAL:
        raise PurchaseError(f"The minimum withdrawal is £{MIN_WITHDRAWAL / 100:.0f}.")
    method = details.get("method")
    vals = {"method": method, "account_name": None, "sort_code": None, "account_number": None, "paypal_email": None}
    if method == "bank":
        vals["account_name"] = " ".join((details.get("account_name") or "").split())[:80]
        vals["sort_code"] = _clean_digits(details.get("sort_code"))
        vals["account_number"] = _clean_digits(details.get("account_number"))
        if not vals["account_name"] or len(vals["sort_code"]) != 6 or len(vals["account_number"]) != 8:
            raise PurchaseError("Enter the account holder name, a 6-digit sort code and an 8-digit account number.")
    elif method == "paypal":
        vals["paypal_email"] = (details.get("paypal_email") or "").strip()[:120]
        if "@" not in vals["paypal_email"]:
            raise PurchaseError("Enter your PayPal email address.")
    else:
        raise PurchaseError("Choose bank transfer or PayPal.")
    with write_txn() as db:
        if amount > balance(db, user["id"], "cash"):
            raise PurchaseError("That's more than your cash balance.")
        cur = db.execute("INSERT INTO withdrawals (user_id, amount, created_at, method, account_name, sort_code, account_number, "
                         "paypal_email) VALUES (?,?,?,?,?,?,?,?)",
                         (user["id"], amount, iso(utcnow()), method, vals["account_name"], vals["sort_code"],
                          vals["account_number"], vals["paypal_email"]))
        add_credit(db, user["id"], -amount, "Withdrawal to " + ("bank" if method == "bank" else "PayPal"),
                   f"w{cur.lastrowid}", kind="cash")
        return cur.lastrowid


def settle_withdrawal(wid, paid, note=""):
    with write_txn() as db:
        w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not w or w["status"] != "requested":
            raise PurchaseError("Already handled.")
        db.execute("UPDATE withdrawals SET status=?, note=?, done_at=? WHERE id=?",
                   ("paid" if paid else "rejected", note, iso(utcnow()), wid))
        # once handled, keep only the last 4 digits of bank details
        if w["account_number"]:
            db.execute("UPDATE withdrawals SET account_number=?, sort_code=? WHERE id=?",
                       ("****" + w["account_number"][-4:], "**-**-" + (w["sort_code"] or "")[-2:], wid))
        if not paid:
            add_credit(db, w["user_id"], w["amount"], "Withdrawal returned", f"w{wid}", kind="cash")
        return w


# ---------------- responsible play ----------------

def effective_limits(user):
    """Apply limit increases whose 72h cooling-off has passed. -> dict of pence or None."""
    db = get_db()
    if user["pending_limit_at"] and parse_iso(user["pending_limit_at"]) <= utcnow():
        sets = ["pending_limit_at=NULL", "pending_limit=NULL", "pending_daily=NULL", "pending_weekly=NULL"]
        if user["pending_limit"] is not None:
            sets.append(f"monthly_limit={int(user['pending_limit'])}")
        for col, src in (("daily_limit", "pending_daily"), ("weekly_limit", "pending_weekly")):
            if user[src] is not None:
                sets.append(f"{col}={'NULL' if user[src] < 0 else int(user[src])}")
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (user["id"],))
        user = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"daily": user["daily_limit"], "weekly": user["weekly_limit"], "monthly": user["monthly_limit"]}


def spend_since(db, user_id, since):
    """Money put in by card: entries paid by card plus wallet deposits. Spending from the wallet
    afterwards isn't counted again."""
    cards = db.execute(
        "SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE user_id=? AND created_at>=? AND status IN ('paid','pending')",
        (user_id, iso(since))).fetchone()[0]
    deps = db.execute(
        "SELECT COALESCE(SUM(amount - refunded),0) FROM deposits WHERE user_id=? AND created_at>=? "
        "AND (status='paid' OR (status='pending' AND created_at>=?))",
        (user_id, iso(since), iso(utcnow() - timedelta(minutes=HOLD_MINUTES)))).fetchone()[0]
    return cards + deps


def uk_midnight(days_back=0):
    """Start of today (UK time) as a UTC datetime."""
    now = utcnow().astimezone(UK)
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back))


def spend_summary(db, user_id):
    day = uk_midnight()
    return {"daily": spend_since(db, user_id, day),
            "weekly": spend_since(db, user_id, day - timedelta(days=day.weekday())),
            "monthly": spend_since(db, user_id, day.replace(day=1))}


def is_excluded(user):
    return bool(user["excluded_until"]) and parse_iso(user["excluded_until"]) > utcnow()


# ---------------- promo codes ----------------

def check_promo(db, code, user_id, subtotal):
    p = db.execute("SELECT * FROM promo_codes WHERE code=? AND active=1", (code.strip(),)).fetchone()
    if p is None:
        raise PurchaseError("That promo code isn't valid.")
    if p["expires_at"] and parse_iso(p["expires_at"]) < utcnow():
        raise PurchaseError("That promo code has expired.")
    if p["max_uses"] is not None and p["uses"] >= p["max_uses"]:
        raise PurchaseError("That promo code has been fully used.")
    if subtotal < p["min_spend"]:
        raise PurchaseError(f"That code needs a minimum spend of £{p['min_spend']/100:.2f}.")
    used = db.execute("SELECT COUNT(*) FROM checkouts WHERE user_id=? AND promo_id=? AND status IN ('paid','pending')",
                      (user_id, p["id"])).fetchone()[0]
    if used >= p["per_user"]:
        raise PurchaseError("You've already used that promo code.")
    discount = min(subtotal, subtotal * p["percent"] // 100 + p["fixed"])
    return p, discount


# ---------------- holds & cleanup ----------------

def _expire_checkout_db(db, cid):
    c = db.execute("SELECT * FROM checkouts WHERE id=?", (cid,)).fetchone()
    if c is None or c["status"] != "pending":
        return
    db.execute("DELETE FROM tickets WHERE status='held' AND order_id IN (SELECT id FROM orders WHERE checkout_id=?)", (cid,))
    db.execute("UPDATE orders SET status='expired' WHERE checkout_id=? AND status='pending'", (cid,))
    db.execute("UPDATE checkouts SET status='expired' WHERE id=?", (cid,))
    if c["credit_used"]:
        add_credit(db, c["user_id"], c["credit_used"], "Unpaid checkout — credit returned", f"c{cid}", kind="credit")
    if c["cash_used"]:
        add_credit(db, c["user_id"], c["cash_used"], "Unpaid checkout — cash returned", f"c{cid}", kind="cash")
    if c["deposit_used"]:
        add_credit(db, c["user_id"], c["deposit_used"], "Unpaid checkout — deposit returned", f"c{cid}", kind="deposit")


def cleanup_expired(db):
    cutoff = iso(utcnow() - timedelta(minutes=HOLD_MINUTES))
    for r in db.execute("SELECT id FROM checkouts WHERE status='pending' AND created_at<?", (cutoff,)).fetchall():
        _expire_checkout_db(db, r["id"])


def refuse_checkout(cid, status, stripe_session_id=None):
    """Payment taken but not accepted (e.g. credit card): release tickets, return balance, mark it."""
    with write_txn() as db:
        c = db.execute("SELECT * FROM checkouts WHERE id=?", (cid,)).fetchone()
        if c is None or c["status"] != "pending":
            return False
        _expire_checkout_db(db, cid)
        db.execute("UPDATE checkouts SET status=?, stripe_session_id=COALESCE(?, stripe_session_id) WHERE id=?",
                   (status, stripe_session_id, cid))
        return True


def expire_checkout(cid):
    with write_txn() as db:
        _expire_checkout_db(db, cid)


# ---------------- checkout ----------------

def _allocate(db, comp, qty, picks):
    taken = taken_numbers(db, comp["id"])
    if picks:
        bad = [n for n in picks if n < 1 or n > comp["max_tickets"]]
        if bad:
            raise PurchaseError(f"Ticket #{bad[0]} doesn't exist in {comp['title']}.")
        clash = [n for n in picks if n in taken]
        if clash:
            raise PurchaseError(f"Ticket #{clash[0]} in {comp['title']} was just taken — please pick another.")
        return picks
    free = comp["max_tickets"] - len(taken)
    if qty > free:
        raise PurchaseError(f"Only {free} ticket(s) left in {comp['title']}.")
    if comp["max_tickets"] <= 200000:
        pool = [n for n in range(1, comp["max_tickets"] + 1) if n not in taken]
        return sorted(_rand.sample(pool, qty))
    out = set()
    while len(out) < qty:
        n = _rand.randint(1, comp["max_tickets"])
        if n not in taken:
            out.add(n)
    return sorted(out)


def reserve_checkout(user, lines, promo_code="", use_credit=False):
    """lines: [{'comp_id', 'qty', 'numbers': [..] or [], 'answer'}]. Atomic: either
    everything is held or nothing is. Returns (checkout_id, cash_due)."""
    if not lines:
        raise PurchaseError("Your basket is empty.")
    if is_excluded(user):
        raise PurchaseError("Your account is on a break, so you can't enter right now.")
    limits = effective_limits(user)
    with write_txn() as db:
        cleanup_expired(db)
        now = iso(utcnow())
        orders, subtotal = [], 0
        for ln in lines:
            comp = db.execute("SELECT * FROM competitions WHERE id=?", (ln["comp_id"],)).fetchone()
            if comp is None or comp_state(comp) != "live":
                raise PurchaseError("A competition in your basket has closed — please remove it.")
            if comp["free_daily"]:
                raise PurchaseError("The daily free game can't be bought — claim your free play on its page.")
            if ln.get("answer") != comp["correct"]:
                raise PurchaseError(f"The answer for {comp['title']} isn't right.")
            picks = sorted(set(int(n) for n in (ln.get("numbers") or [])))
            qty = len(picks) if picks else int(ln["qty"])
            if qty < 1:
                raise PurchaseError("Choose at least one ticket.")
            mine = user_ticket_count(db, comp["id"], user["id"])
            if mine + qty > comp["max_per_user"]:
                raise PurchaseError(f"{comp['title']}: the limit is {comp['max_per_user']} tickets per person — you have {mine}.")
            numbers = _allocate(db, comp, qty, picks)
            gross, disc, net, _ = line_price(comp, qty)
            cur = db.execute(
                "INSERT INTO orders (user_id, competition_id, quantity, amount, discount, created_at) VALUES (?,?,?,?,?,?)",
                (user["id"], comp["id"], qty, net, disc, now))
            db.executemany(
                "INSERT INTO tickets (competition_id, number, user_id, order_id, status, created_at) VALUES (?,?,?,?, 'held', ?)",
                [(comp["id"], n, user["id"], cur.lastrowid, now) for n in numbers])
            orders.append(cur.lastrowid)
            subtotal += net

        promo, promo_disc = (None, 0)
        if promo_code.strip():
            promo, promo_disc = check_promo(db, promo_code, user["id"], subtotal)
        after = subtotal - promo_disc
        bal = balances(db, user["id"])
        credit = min(max(bal["credit"], 0), after) if use_credit else 0                    # site credit first
        dep_used = min(max(bal["deposit"], 0), after - credit) if use_credit else 0        # then deposited funds
        cash_used = min(max(bal["cash"], 0), after - credit - dep_used) if use_credit else 0  # then cash winnings
        cash = after - credit - dep_used - cash_used
        if 0 < cash < STRIPE_MIN:
            raise PurchaseError("Card payments must be at least 30p — add a ticket or use wallet credit.")

        spent = spend_summary(db, user["id"])
        for period in ("daily", "weekly", "monthly"):
            lim = limits[period]
            if lim is not None and spent[period] + cash > lim:
                raise PurchaseError(f"This would take you over your {period} spend limit of £{lim/100:.2f} "
                                    f"(£{spent[period]/100:.2f} spent). You can review limits on your account page.")

        cur = db.execute(
            "INSERT INTO checkouts (user_id, subtotal, promo_id, promo_discount, credit_used, cash_used, deposit_used, cash_due, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user["id"], subtotal, promo["id"] if promo else None, promo_disc, credit, cash_used, dep_used, cash, now))
        cid = cur.lastrowid
        db.execute(f"UPDATE orders SET checkout_id=? WHERE id IN ({','.join('?' * len(orders))})", [cid, *orders])
        if credit:
            add_credit(db, user["id"], -credit, "Site credit used at checkout", f"c{cid}", kind="credit")
        if cash_used:
            add_credit(db, user["id"], -cash_used, "Cash balance used at checkout", f"c{cid}", kind="cash")
        if dep_used:
            add_credit(db, user["id"], -dep_used, "Deposited funds used at checkout", f"c{cid}", kind="deposit")
        return cid, cash


def fulfil_checkout(cid, stripe_session_id=None):
    """Payment confirmed: issue tickets, pay instant wins, referral bonus.
    Idempotent — Stripe may send the same webhook more than once."""
    with write_txn() as db:
        c = db.execute("SELECT * FROM checkouts WHERE id=?", (cid,)).fetchone()
        if c is None:
            return None
        if c["status"] == "expired":
            # Paid after we released the tickets (very late webhook) — flag for a refund.
            db.execute("UPDATE checkouts SET status='needs_refund', stripe_session_id=COALESCE(?, stripe_session_id) WHERE id=?",
                       (stripe_session_id, cid))
            current_app.logger.error("Checkout %s paid after expiry — refund needed", cid)
            return None
        if c["status"] != "pending":
            return c["status"]
        now = iso(utcnow())
        db.execute("UPDATE checkouts SET status='paid', paid_at=?, stripe_session_id=COALESCE(?, stripe_session_id) WHERE id=?",
                   (now, stripe_session_id, cid))
        db.execute("UPDATE orders SET status='paid', paid_at=? WHERE checkout_id=?", (now, cid))
        db.execute("UPDATE tickets SET status='issued' WHERE order_id IN (SELECT id FROM orders WHERE checkout_id=?)", (cid,))
        wins = db.execute(
            "SELECT ip.*, t.id AS tid, comp.game_type FROM instant_prizes ip "
            "JOIN tickets t ON t.competition_id=ip.competition_id AND t.number=ip.number "
            "JOIN competitions comp ON comp.id=ip.competition_id "
            "JOIN orders o ON o.id=t.order_id WHERE o.checkout_id=? AND ip.ticket_id IS NULL", (cid,)).fetchall()
        for w in wins:
            # Games: the prize is locked to the player now, but credited when they reveal it
            # (or automatically after 24h / when the game closes) so the game isn't spoiled.
            pay_now = prize_kind(w) in ("cash", "credit") and bool(w["credit_amount"]) and not w["game_type"]
            db.execute("UPDATE instant_prizes SET ticket_id=?, won_at=?, fulfilled=? WHERE id=?",
                       (w["tid"], now, 1 if pay_now else 0, w["id"]))
            if pay_now:
                pay_prize(db, c["user_id"], w)
        if c["promo_id"]:
            db.execute("UPDATE promo_codes SET uses=uses+1 WHERE id=?", (c["promo_id"],))
        award_points(db, c["user_id"], c["cash_due"] + c["deposit_used"])
        # referral bonus on the referred user's first paid checkout
        u = db.execute("SELECT * FROM users WHERE id=?", (c["user_id"],)).fetchone()
        bonus = current_app.config["REFERRAL_BONUS"]
        if u["referred_by"] and bonus and c["subtotal"] > 0 and db.execute(
                "SELECT COUNT(*) FROM checkouts WHERE user_id=? AND status='paid'", (u["id"],)).fetchone()[0] == 1:
            add_credit(db, u["referred_by"], bonus, f"Referral bonus — {u['name'].split()[0]} joined", f"u{u['id']}")
        return "paid"


def checkout_summary(db, cid):
    rows = db.execute(
        "SELECT o.id, o.quantity, o.amount, c.title, c.slug, c.id AS comp_id, c.game_type FROM orders o "
        "JOIN competitions c ON c.id=o.competition_id WHERE o.checkout_id=?", (cid,)).fetchall()
    out = []
    for r in rows:
        nums = db.execute("SELECT t.number, CASE WHEN ? = '' THEN ip.title END AS win FROM tickets t "
                          "LEFT JOIN instant_prizes ip ON ip.ticket_id=t.id WHERE t.order_id=? ORDER BY t.number",
                          (r["game_type"], r["id"])).fetchall()
        out.append({"title": r["title"], "slug": r["slug"], "amount": r["amount"], "tickets": nums,
                    "game": r["game_type"]})
    return out


# ---------------- postal entries ----------------

def add_postal_entry(comp_id, name, email, address, answer_correct, admin_id):
    with write_txn() as db:
        cleanup_expired(db)
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
        if comp is None or comp["status"] != "live":
            raise PurchaseError("Competition isn't accepting entries.")
        cur = db.execute(
            "INSERT INTO postal_entries (competition_id, name, email, address, answer_correct, added_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)", (comp_id, name, email, address, 1 if answer_correct else 0, admin_id, iso(utcnow())))
        if not answer_correct:
            return None
        n = _allocate(db, comp, 1, None)[0]
        t = db.execute("INSERT INTO tickets (competition_id, number, postal_entry_id, status, revealed_at, created_at) "
                       "VALUES (?,?,?, 'issued', ?, ?)", (comp_id, n, cur.lastrowid, iso(utcnow()), iso(utcnow())))
        ip = db.execute("SELECT * FROM instant_prizes WHERE competition_id=? AND number=? AND ticket_id IS NULL",
                        (comp_id, n)).fetchone()
        if ip:  # postal entrants win instant prizes too (paid out manually)
            db.execute("UPDATE instant_prizes SET ticket_id=?, won_at=? WHERE id=?", (t.lastrowid, iso(utcnow()), ip["id"]))
        return n


# ---------------- instant wins ----------------

def _recommit(db, comp_id):
    comp = db.execute("SELECT instant_salt FROM competitions WHERE id=?", (comp_id,)).fetchone()
    salt = comp["instant_salt"] or secrets.token_hex(16)
    prizes = db.execute("SELECT number, title FROM instant_prizes WHERE competition_id=?", (comp_id,)).fetchall()
    h = instant_commitment(salt, prizes) if prizes else None
    db.execute("UPDATE competitions SET instant_salt=?, instant_hash=? WHERE id=?", (salt, h, comp_id))


def add_instant_prizes(comp_id, title, value, credit_amount, quantity, prize_type=""):
    with write_txn() as db:
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
        if taken_count(db, comp_id) or comp["status"] not in ("draft", "live"):
            raise PurchaseError("Instant wins are locked once tickets exist — that keeps them fair.")
        used = {r[0] for r in db.execute("SELECT number FROM instant_prizes WHERE competition_id=?", (comp_id,))}
        free = comp["max_tickets"] - len(used)
        if quantity < 1 or quantity > free:
            raise PurchaseError(f"You can add between 1 and {free} of these.")
        pool = [n for n in range(1, comp["max_tickets"] + 1) if n not in used]
        for n in _rand.sample(pool, quantity):
            db.execute("INSERT INTO instant_prizes (competition_id, title, value, credit_amount, number, prize_type) "
                       "VALUES (?,?,?,?,?,?)", (comp_id, title, value, credit_amount, n, prize_type))
        _recommit(db, comp_id)


def remove_instant_prize_group(comp_id, title):
    with write_txn() as db:
        if taken_count(db, comp_id):
            raise PurchaseError("Instant wins are locked once tickets exist.")
        db.execute("DELETE FROM instant_prizes WHERE competition_id=? AND title=?", (comp_id, title))
        _recommit(db, comp_id)


def instant_board(db, comp_id, reveal=False):
    """Grouped prizes for display. Unclaimed numbers stay secret unless reveal."""
    game = db.execute("SELECT game_type FROM competitions WHERE id=?", (comp_id,)).fetchone()["game_type"]
    rows = db.execute(
        "SELECT ip.*, t.number AS won_number, t.revealed_at, COALESCE(u.name, p.name) AS winner FROM instant_prizes ip "
        "LEFT JOIN tickets t ON t.id=ip.ticket_id LEFT JOIN users u ON u.id=t.user_id "
        "LEFT JOIN postal_entries p ON p.id=t.postal_entry_id WHERE ip.competition_id=? ORDER BY ip.value DESC, ip.number",
        (comp_id,)).fetchall()
    groups = {}
    for r in rows:
        g = groups.setdefault(r["title"], {"title": r["title"], "value": r["value"], "credit": r["credit_amount"],
                                           "kind": prize_kind(r), "total": 0, "won": [], "hidden": []})
        g["total"] += 1
        # in games a prize only shows as won once the player has revealed it (no spoilers)
        if r["ticket_id"] and (not game or r["revealed_at"] or reveal):
            g["won"].append({"number": r["won_number"], "winner": public_name(r["winner"])})
        elif reveal:
            g["hidden"].append(r["number"])
    return list(groups.values())


# ---------------- the draw ----------------

def run_draw(comp_id):
    with write_txn() as db:
        cleanup_expired(db)
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
        if comp is None or comp["status"] != "live":
            raise PurchaseError("Only live competitions can be drawn.")
        if comp["game_type"]:
            raise PurchaseError("Instant-win games don't have a main draw.")
        if comp_state(comp, sold_count(db, comp_id)) not in ("ended", "soldout"):
            raise PurchaseError("Wait until the competition ends or sells out.")
        if db.execute("SELECT 1 FROM tickets WHERE competition_id=? AND status='held'", (comp_id,)).fetchone():
            raise PurchaseError("Some checkouts are still in progress. Try again in up to 45 minutes.")
        tickets = db.execute("SELECT id, number FROM tickets WHERE competition_id=? AND status='issued' ORDER BY number",
                             (comp_id,)).fetchall()
        if not tickets:
            raise PurchaseError("No entries to draw from.")
        digest = entries_digest(t["number"] for t in tickets)
        winner = tickets[pick_index(comp["seed"], digest, len(tickets))]
        db.execute("UPDATE competitions SET status='drawn', entries_hash=?, winner_ticket_id=?, drawn_at=? WHERE id=?",
                   (digest, winner["id"], iso(utcnow()), comp_id))
        return winner["number"]


def winner_details(db, comp):
    if not comp["winner_ticket_id"]:
        return None
    return db.execute(
        "SELECT t.number, COALESCE(u.name, p.name) AS name, COALESCE(u.email, p.email) AS email, "
        "CASE WHEN t.postal_entry_id IS NOT NULL THEN 1 ELSE 0 END AS postal "
        "FROM tickets t LEFT JOIN users u ON u.id=t.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id "
        "WHERE t.id=?", (comp["winner_ticket_id"],)).fetchone()


def public_name(name):
    parts = (name or "").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0]}."
    return parts[0] if parts else "Winner"


# ---------------- site stats (real numbers only) ----------------

def site_stats(db):
    main = db.execute("SELECT COUNT(*), COALESCE(SUM(prize_value),0) FROM competitions WHERE status='drawn'").fetchone()
    inst = db.execute("SELECT COUNT(*), COALESCE(SUM(value),0) FROM instant_prizes WHERE ticket_id IS NOT NULL").fetchone()
    return {"winners": main[0] + inst[0], "won_value": main[1] + inst[1], "instant_wins": inst[0],
            "draws": main[0]}


# ---------------- settings ----------------

def get_setting(key, default=""):
    r = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key, value):
    get_db().execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


def json_list(s):
    try:
        return json.loads(s) if s else []
    except ValueError:
        return []


# ---------------- instant-win games ----------------

GAME_TYPES = [("scratch", "Scratch card", "🎟️"), ("spin", "Spin the wheel", "🎡"), ("box", "Mystery box", "🎁")]
GAME_NAMES = {k: n for k, n, _ in GAME_TYPES}
GAME_ICONS = {k: i for k, _, i in GAME_TYPES}
GAME_PRICES = [10, 40, 50, 100, 500]   # pence — the price bands shown in the games lobby


def _reveal_db(db, ticket):
    """Mark one game ticket revealed and pay any wallet-credit prize on it. Returns the prize row or None."""
    now = iso(utcnow())
    if not ticket["revealed_at"]:
        db.execute("UPDATE tickets SET revealed_at=? WHERE id=?", (now, ticket["id"]))
    prize = db.execute("SELECT * FROM instant_prizes WHERE ticket_id=?", (ticket["id"],)).fetchone()
    if prize and not prize["fulfilled"] and ticket["user_id"] and prize_kind(prize) in ("cash", "credit"):
        if pay_prize(db, ticket["user_id"], prize):
            db.execute("UPDATE instant_prizes SET fulfilled=1 WHERE id=?", (prize["id"],))
    return prize


def reveal_ticket(user_id, ticket_id):
    with write_txn() as db:
        t = db.execute("SELECT t.*, c.game_type FROM tickets t JOIN competitions c ON c.id=t.competition_id "
                       "WHERE t.id=? AND t.user_id=? AND t.status='issued'", (ticket_id, user_id)).fetchone()
        if t is None or not t["game_type"]:
            raise PurchaseError("That play doesn't exist.")
        first = not t["revealed_at"]
        prize = _reveal_db(db, t)
        kind = prize_kind(prize) if prize else None
        return {"id": t["id"], "number": t["number"], "first": first,
                "win": bool(prize), "title": prize["title"] if prize else None,
                "value": prize["value"] if prize else 0, "kind": kind,
                "credit": kind in ("cash", "credit")}


def settle_unrevealed(user_id=None, hours=24):
    """Pay out game prizes the player never revealed: after `hours`, or once the game has ended."""
    db = get_db()
    cutoff = iso(utcnow() - timedelta(hours=hours))
    sql = ("SELECT t.* FROM tickets t JOIN competitions c ON c.id=t.competition_id WHERE c.game_type!='' "
           "AND t.status='issued' AND t.revealed_at IS NULL AND (t.created_at<? OR c.ends_at<? OR c.status!='live')")
    args = [cutoff, iso(utcnow())]
    if user_id is not None:
        sql += " AND t.user_id=?"
        args.append(user_id)
    if not db.execute(sql + " LIMIT 1", args).fetchone():
        return 0
    with write_txn() as wdb:
        rows = wdb.execute(sql, args).fetchall()
        for t in rows:
            _reveal_db(wdb, t)
        return len(rows)


def unplayed(db, user_id, comp_id=None):
    sql = ("SELECT t.id, t.number, c.slug, c.title, c.game_type FROM tickets t JOIN competitions c ON c.id=t.competition_id "
           "WHERE t.user_id=? AND t.status='issued' AND t.revealed_at IS NULL AND c.game_type!=''")
    args = [user_id]
    if comp_id:
        sql += " AND c.id=?"
        args.append(comp_id)
    return db.execute(sql + " ORDER BY t.id", args).fetchall()


def game_info(db, comp):
    """Prize pool, odds and return-to-player for a game (or any comp with instant wins)."""
    r = db.execute("SELECT COUNT(*), COALESCE(SUM(value),0) FROM instant_prizes WHERE competition_id=?",
                   (comp["id"],)).fetchone()
    prizes, pool = r[0], r[1]
    takings = comp["ticket_price"] * comp["max_tickets"]
    return {"prizes": prizes, "pool": pool, "odds": round(comp["max_tickets"] / prizes, 1) if prizes else None,
            "rtp": round(100 * pool / takings) if takings else 0, "takings": takings}


# ---------------- DBX Points loyalty ----------------

TIERS = [("Bronze", 0, 1.0), ("Silver", 250, 1.1), ("Gold", 1000, 1.25), ("Diamond", 5000, 1.5)]
POINTS_PER_POUND = 1
REDEEM_BLOCK = 100          # 100 points = £1 site credit


def tier_for(lifetime):
    cur = TIERS[0]
    for t in TIERS:
        if lifetime >= t[1]:
            cur = t
    nxt = next((t for t in TIERS if t[1] > lifetime), None)
    return {"name": cur[0], "mult": cur[2], "next": nxt[0] if nxt else None,
            "to_next": (nxt[1] - lifetime) if nxt else 0,
            "pct": 100 if not nxt else round(100 * (lifetime - cur[1]) / (nxt[1] - cur[1]))}


def award_points(db, user_id, card_pence):
    if card_pence <= 0:
        return 0
    u = db.execute("SELECT points_lifetime FROM users WHERE id=?", (user_id,)).fetchone()
    pts = int(card_pence // 100 * POINTS_PER_POUND * tier_for(u["points_lifetime"])["mult"])
    if pts:
        db.execute("UPDATE users SET points=points+?, points_lifetime=points_lifetime+? WHERE id=?", (pts, pts, user_id))
    return pts


def redeem_points(user_id, blocks):
    if blocks < 1:
        raise PurchaseError("Choose how many points to redeem.")
    need = blocks * REDEEM_BLOCK
    with write_txn() as db:
        have = db.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()[0]
        if need > have:
            raise PurchaseError(f"You have {have} points.")
        db.execute("UPDATE users SET points=points-? WHERE id=?", (need, user_id))
        add_credit(db, user_id, blocks * 100, f"Redeemed {need} DBX Points", None, kind="credit")


# ---------------- cancellations ----------------

def refund_competition(comp_id):
    """Refund every paid entry to the entrant's CASH balance (withdrawable). Safe to run twice."""
    with write_txn() as db:
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
        rows = db.execute("SELECT * FROM orders WHERE competition_id=? AND status='paid'", (comp_id,)).fetchall()
        n = total = 0
        for o in rows:
            ref = f"refund-o{o['id']}"
            if db.execute("SELECT 1 FROM credit_ledger WHERE ref=?", (ref,)).fetchone():
                continue
            if o["amount"] > 0:
                add_credit(db, o["user_id"], o["amount"], f"Refund — {comp['title']} was cancelled", ref, kind="cash")
                n += 1
                total += o["amount"]
        return n, total


# ---------------- automatic draws ----------------

_last_auto = {"t": None}


def due_auto_draws():
    """Run main draws whose timer has ended. Throttled to once a minute per worker. Returns drawn comp ids."""
    now = utcnow()
    if _last_auto["t"] and (now - _last_auto["t"]).total_seconds() < 60:
        return []
    _last_auto["t"] = now
    db = get_db()
    rows = db.execute("SELECT id FROM competitions WHERE status='live' AND game_type='' AND auto_draw=1 AND ends_at<?",
                      (iso(now),)).fetchall()
    drawn = []
    for r in rows:
        try:
            run_draw(r["id"])
            drawn.append(r["id"])
        except PurchaseError:
            pass          # still has checkouts in progress, or no entries — try again later
    return drawn


# ---------------- daily free game ----------------

def free_play_today(db, user_id, comp_id):
    since = iso(uk_midnight())
    return db.execute("SELECT 1 FROM tickets WHERE competition_id=? AND user_id=? AND created_at>=? LIMIT 1",
                      (comp_id, user_id, since)).fetchone() is not None


def claim_free_play(user, comp_id):
    if not user["email_verified"]:
        raise PurchaseError("Verify your email address to claim free plays — check your inbox or resend it from your account.")
    if is_excluded(user):
        raise PurchaseError("Your account is on a break.")
    with write_txn() as db:
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
        if comp is None or not comp["free_daily"] or comp_state(comp) != "live":
            raise PurchaseError("This free game isn't running right now.")
        if free_play_today(db, user["id"], comp_id):
            raise PurchaseError("You've had today's free play — come back tomorrow!")
        n = _allocate(db, comp, 1, None)[0]
        cur = db.execute("INSERT INTO tickets (competition_id, number, user_id, status, created_at) VALUES (?,?,?, 'issued', ?)",
                         (comp_id, n, user["id"], iso(utcnow())))
        ip = db.execute("SELECT * FROM instant_prizes WHERE competition_id=? AND number=? AND ticket_id IS NULL",
                        (comp_id, n)).fetchone()
        if ip:
            db.execute("UPDATE instant_prizes SET ticket_id=?, won_at=? WHERE id=?", (cur.lastrowid, iso(utcnow()), ip["id"]))
        return cur.lastrowid


# ---------------- deleting & starting fresh ----------------

def paid_unrefunded(db, comp_id):
    """Paid orders on a competition whose money hasn't been given back yet."""
    return db.execute(
        "SELECT COUNT(*) FROM orders o WHERE o.competition_id=? AND o.status='paid' AND o.amount>0 "
        "AND NOT EXISTS (SELECT 1 FROM credit_ledger l WHERE l.ref='refund-o' || o.id)", (comp_id,)).fetchone()[0]


def _delete_comp_rows(db, comp_ids):
    """Remove competitions and everything hanging off them. Wallet history (prizes paid, refunds)
    is kept so balances stay correct."""
    if not comp_ids:
        return []
    q = ",".join("?" * len(comp_ids))
    images = [r[0] for r in db.execute(f"SELECT image FROM competitions WHERE id IN ({q}) AND image IS NOT NULL", comp_ids)]
    images += [r[0] for r in db.execute(f"SELECT winner_photo FROM competitions WHERE id IN ({q}) AND winner_photo IS NOT NULL", comp_ids)]
    checkouts = [r[0] for r in db.execute(
        f"SELECT DISTINCT checkout_id FROM orders WHERE competition_id IN ({q}) AND checkout_id IS NOT NULL", comp_ids)]
    db.execute(f"UPDATE competitions SET winner_ticket_id=NULL WHERE id IN ({q})", comp_ids)
    db.execute(f"DELETE FROM instant_prizes WHERE competition_id IN ({q})", comp_ids)
    db.execute(f"DELETE FROM tickets WHERE competition_id IN ({q})", comp_ids)
    db.execute(f"DELETE FROM postal_entries WHERE competition_id IN ({q})", comp_ids)
    db.execute(f"DELETE FROM orders WHERE competition_id IN ({q})", comp_ids)
    for cid in checkouts:   # a basket that only held these competitions has nothing left to show
        if not db.execute("SELECT 1 FROM orders WHERE checkout_id=?", (cid,)).fetchone():
            db.execute("DELETE FROM checkouts WHERE id=?", (cid,))
    db.execute(f"DELETE FROM competitions WHERE id IN ({q})", comp_ids)
    return images


def delete_competition(comp_id):
    """Delete one competition or game. Refuses while paid entries haven't been refunded — cancel it first."""
    with write_txn() as db:
        if db.execute("SELECT 1 FROM competitions WHERE id=?", (comp_id,)).fetchone() is None:
            raise PurchaseError("That competition doesn't exist.")
        if paid_unrefunded(db, comp_id):
            raise PurchaseError("This has paid entries that haven't been refunded. Cancel it first (that refunds "
                                "everyone), then delete it.")
        return _delete_comp_rows(db, [comp_id])


RESET_SCOPES = {"games": "game_type!=''", "draws": "game_type=''", "all": "1=1"}


def start_fresh(competitions="", wallets=False, accounts=False, promos=False):
    """Wipe test data. competitions: '' | 'games' | 'draws' | 'all'. Admin accounts are always kept.
    Returns (summary dict, image files to remove)."""
    if accounts:                      # player accounts own orders and wallet rows, so those must go too
        competitions, wallets = "all", True
    out, images = {}, []
    with write_txn() as db:
        if competitions in RESET_SCOPES:
            ids = [r[0] for r in db.execute(f"SELECT id FROM competitions WHERE {RESET_SCOPES[competitions]}")]
            images = _delete_comp_rows(db, ids)
            if competitions == "all":
                db.execute("DELETE FROM checkouts")
            out["games" if competitions == "games" else "competitions"] = len(ids)
        if wallets:
            out["transactions"] = db.execute("DELETE FROM credit_ledger").rowcount
            out["withdrawals"] = db.execute("DELETE FROM withdrawals").rowcount
            db.execute("DELETE FROM deposits")
            db.execute("UPDATE users SET points=0, points_lifetime=0")
        if promos:
            db.execute("UPDATE checkouts SET promo_id=NULL")
            out["promo codes"] = db.execute("DELETE FROM promo_codes").rowcount
        if accounts:
            players = [r[0] for r in db.execute("SELECT id FROM users WHERE is_admin=0")]
            if players:
                q = ",".join("?" * len(players))
                db.execute(f"DELETE FROM password_resets WHERE user_id IN ({q})", players)
                db.execute(f"UPDATE postal_entries SET added_by=NULL WHERE added_by IN ({q})", players)
                db.execute(f"UPDATE users SET referred_by=NULL WHERE referred_by IN ({q})", players)
                db.execute(f"DELETE FROM users WHERE id IN ({q})", players)
            out["player accounts"] = len(players)
    return out, images


# ---------------- publishing & scheduled launches ----------------

def publish_problem(db, comp):
    """Why this can't go live yet, or None."""
    if comp["game_type"] and not db.execute("SELECT 1 FROM instant_prizes WHERE competition_id=? LIMIT 1",
                                            (comp["id"],)).fetchone():
        return "Add prizes to this game before it goes live."
    if parse_iso(comp["ends_at"]) <= utcnow():
        return "The closing date is in the past — edit it first."
    return None


_last_sched = {"t": None}


def due_scheduled():
    """Put scheduled competitions live once their start time arrives. Returns the ones that went live."""
    now = utcnow()
    if _last_sched["t"] and (now - _last_sched["t"]).total_seconds() < 30:
        return []
    _last_sched["t"] = now
    db = get_db()
    rows = db.execute("SELECT * FROM competitions WHERE status='draft' AND scheduled=1 AND starts_at<=?",
                      (iso(now),)).fetchall()
    out = []
    for c in rows:
        if publish_problem(db, c):
            continue
        with write_txn() as w:
            if w.execute("UPDATE competitions SET status='live', scheduled=0 WHERE id=? AND status='draft' AND scheduled=1",
                         (c["id"],)).rowcount:
                out.append(c)
    return out


# ---------------- deposits ----------------

MIN_DEPOSIT, MAX_DEPOSIT = 500, 25000          # £5 – £250 a time


def expire_stale_deposits(db, user_id):
    db.execute("UPDATE deposits SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
               (user_id, iso(utcnow() - timedelta(minutes=HOLD_MINUTES))))


def deposit_room(db, user):
    """How much more this player may deposit right now under their spending limits (pence)."""
    limits, spent = effective_limits(user), spend_summary(db, user["id"])
    room = MAX_DEPOSIT
    for period in ("daily", "weekly", "monthly"):
        if limits[period] is not None:
            room = min(room, limits[period] - spent[period])
    return max(0, room)


def create_deposit(user, amount):
    if is_excluded(user):
        raise PurchaseError("Your account is on a break, so you can't add funds right now.")
    if not user["email_verified"]:
        raise PurchaseError("Confirm your email address before adding funds — check your inbox or resend the link.")
    if amount < MIN_DEPOSIT or amount > MAX_DEPOSIT:
        raise PurchaseError(f"You can add between £{MIN_DEPOSIT / 100:.0f} and £{MAX_DEPOSIT / 100:.0f} at a time.")
    with write_txn() as db:
        db.execute("UPDATE deposits SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
                   (user["id"], iso(utcnow() - timedelta(minutes=HOLD_MINUTES))))
        room = deposit_room(db, user)
        if amount > room:
            raise PurchaseError(f"That would take you over your spending limit — you can add up to £{room / 100:.2f} right now. "
                                "You can review your limits in Settings.")
        return db.execute("INSERT INTO deposits (user_id, amount, created_at) VALUES (?,?,?)",
                          (user["id"], amount, iso(utcnow()))).lastrowid


def fulfil_deposit(did, stripe_session_id=None, payment_intent=None):
    """Card payment confirmed: add the money to the player's deposited funds. Idempotent."""
    with write_txn() as db:
        d = db.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if d is None:
            return None
        if d["status"] == "expired":
            db.execute("UPDATE deposits SET status='needs_refund', payment_intent=COALESCE(?, payment_intent) WHERE id=?",
                       (payment_intent, did))
            current_app.logger.error("Deposit %s paid after it expired — refund needed", did)
            return None
        if d["status"] != "pending":
            return d["status"]
        db.execute("UPDATE deposits SET status='paid', paid_at=?, stripe_session_id=COALESCE(?, stripe_session_id), "
                   "payment_intent=COALESCE(?, payment_intent) WHERE id=?", (iso(utcnow()), stripe_session_id, payment_intent, did))
        add_credit(db, d["user_id"], d["amount"], f"Deposit by card #{did}", f"d{did}", kind="deposit")
        return "paid"


def set_deposit_status(did, status, payment_intent=None):
    with write_txn() as db:
        db.execute("UPDATE deposits SET status=?, payment_intent=COALESCE(?, payment_intent) WHERE id=? AND status='pending'",
                   (status, payment_intent, did))


def refundable_deposits(db, user_id):
    """Unspent deposited funds, matched to the card payments they came from (newest first)."""
    left = max(0, balance(db, user_id, "deposit"))
    out = []
    for d in db.execute("SELECT * FROM deposits WHERE user_id=? AND status='paid' AND amount>refunded ORDER BY id DESC",
                        (user_id,)).fetchall():
        if left <= 0:
            break
        take = min(left, d["amount"] - d["refunded"])
        out.append((d, take))
        left -= take
    return out


def refund_deposits(user_id, refund_fn):
    """Send unspent deposits back to the cards they came from. refund_fn(payment_intent, amount) does the
    card refund (or is a no-op in test mode). Returns pence refunded."""
    total = 0
    db = get_db()
    for d, take in refundable_deposits(db, user_id):
        if d["payment_intent"]:
            refund_fn(d["payment_intent"], take)          # raises if the payment provider says no
        with write_txn() as w:
            w.execute("UPDATE deposits SET refunded=refunded+? WHERE id=?", (take, d["id"]))
            add_credit(w, user_id, -take, f"Deposit #{d['id']} refunded to your card", f"dr{d['id']}-{d['refunded'] + take}",
                       kind="deposit")
        total += take
    return total
