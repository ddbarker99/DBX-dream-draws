import hashlib
import json
import random
import secrets
from datetime import date, timedelta
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import mailer, payments
from .notify import notify, tell, unread_count
from .analytics import count as track, device_type
from .security import device_name, end_session, revoke_others, start_session
from .db import get_db, iso, parse_iso, utcnow, write_txn
from . import UK
from .flags import enabled as feature_on, require_feature
from .services import (to_pence, audit, entrant_count, CATEGORIES, CATEGORY_NAMES, check_promo, MIN_DEPOSIT, MAX_DEPOSIT, create_deposit, deposit_room,
                       fulfil_deposit, set_deposit_status, expire_stale_deposits, refundable_deposits, refund_deposits, MIN_WITHDRAWAL, REDEEM_BLOCK, TIERS, balances, claim_free_play, free_play_today,
                       redeem_points, tier_for, check_withdrawal, GAME_ICONS, GAME_NAMES, GAME_PRICES, GAME_TYPES, MAX_PICKS, game_info, reveal_ticket,
                       unplayed, PurchaseError, balance, checkout_summary, comp_state,
                       effective_limits, expire_checkout, fulfil_checkout, instant_board, is_excluded, is_new,
                       line_price, parse_tiers, public_name, request_withdrawal, reserve_checkout, refuse_checkout, site_stats, start_break,
                       sold_count, spend_summary, taken_numbers, winner_details)

bp = Blueprint("public", __name__)
MAX_BASKET_LINES = 10


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            flash("Log in to continue.")
            return redirect(url_for("public.login", next=request.full_path))
        return view(*a, **kw)
    return wrapped


def _signer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="verify-email")


def send_verification(user):
    token = _signer().dumps({"u": user["id"], "e": user["email"]})
    link = current_app.config["SITE_URL"] + url_for("public.verify_email", token=token)
    mailer.send(user["email"], f"Confirm your email — {current_app.config['SITE_NAME']}",
                f"Hi {user['name'].split()[0]},\n\nWelcome to {current_app.config['SITE_NAME']}! Confirm your email address "
                "to unlock cash withdrawals and your daily free play.\n\nThis link lasts 3 days.",
                button=("Confirm my email", link), heading="One click to go",
                preheader="Confirm your email to unlock withdrawals and your daily free play.")


_FAILS = {}   # key -> [timestamps]   (per worker; good enough to stop password guessing)


def _too_many(key, limit=8, window=900):
    now = utcnow().timestamp()
    hits = [t for t in _FAILS.get(key, []) if now - t < window]
    _FAILS[key] = hits
    return len(hits) >= limit


def _fail(key):
    _FAILS.setdefault(key, []).append(utcnow().timestamp())


def safe_next(target, default=None):
    """Only same-site paths. Browsers treat a backslash like a slash, so "/\\evil.com" would leave the site."""
    ok = target and target.startswith("/") and not target.startswith("//") and "\\" not in target \
        and not any(ord(ch) < 32 for ch in target)
    return target if ok else (default or url_for("public.home"))


def _shuffle(items):
    random.SystemRandom().shuffle(items)


def pct(sold, total):
    return 0 if not sold else max(1.5, min(100, round(100 * sold / total, 1)))


def card_data(db, c):
    sold = sold_count(db, c["id"])
    n_instant = db.execute("SELECT COUNT(*), SUM(ticket_id IS NULL) FROM instant_prizes WHERE competition_id=?",
                           (c["id"],)).fetchone()
    state = comp_state(c, sold)
    p = pct(sold, c["max_tickets"])
    hours_left = (parse_iso(c["ends_at"]) - utcnow()).total_seconds() / 3600
    badges = []
    if state == "live":
        if n_instant[0]:
            badges.append(("instant", f"{n_instant[1] or 0} instant prizes left"))
        if p >= 80:
            badges.append(("hot", "Almost gone"))
        elif hours_left < 24:
            badges.append(("hot", "Ending soon"))
        elif is_new(c):
            badges.append(("new", "Just launched"))
        if parse_tiers(c["discount_tiers"]):
            best = parse_tiers(c["discount_tiers"])[0]
            badges.append(("deal", f"Up to {best[1]}% off"))
    game = None
    if c["game_type"]:
        gi = game_info(db, c)
        game = {"type": c["game_type"], "name": GAME_NAMES.get(c["game_type"], "Game"), "icon": GAME_ICONS.get(c["game_type"], "⚡"),
                "odds": gi["odds"], "left": n_instant[1] or 0}
        badges = [b for b in badges if b[0] != "instant"]
    return {"c": c, "sold": sold, "state": state, "pct": p, "badges": badges, "instant": n_instant[0],
            "hours_left": hours_left, "game": game}


# ---------------- home & listings ----------------

def _recent_instant(db, limit):
    return db.execute(
        "SELECT ip.title, ip.value, ip.won_at, t.number, c.title AS comp, c.slug, COALESCE(u.name, p.name) AS name "
        "FROM instant_prizes ip JOIN tickets t ON t.id=ip.ticket_id JOIN competitions c ON c.id=ip.competition_id "
        "LEFT JOIN users u ON u.id=t.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id "
        "WHERE c.game_type='' OR t.revealed_at IS NOT NULL ORDER BY ip.won_at DESC LIMIT ?", (limit,)).fetchall()


def _draw_winners(db, limit):
    items = []
    for c in db.execute("SELECT * FROM competitions WHERE status='drawn' AND game_type='' ORDER BY drawn_at DESC LIMIT ?",
                        (limit,)).fetchall():
        w = winner_details(db, c)
        items.append({"c": c, "name": public_name(w["name"]) if w else "", "number": w["number"] if w else None})
    return items


def _free_game(db, cards):
    fg = next((x for x in cards if x["c"]["free_daily"] and x["state"] == "live"), None)
    claimed = bool(fg and g.user and free_play_today(db, g.user["id"], fg["c"]["id"]))
    return fg, claimed


@bp.route("/")
def home():
    if request.args.get("tab") or request.args.get("q"):          # old home-page filters now live on /competitions
        return redirect(url_for("public.competitions", **request.args), code=301)
    db = get_db()
    cards = [card_data(db, c) for c in
             db.execute("SELECT * FROM competitions WHERE status='live' ORDER BY featured DESC, ends_at").fetchall()]
    free_game, free_claimed = _free_game(db, cards)
    games = [x for x in cards if x["game"] and x["state"] == "live" and not x["c"]["free_daily"]]
    _shuffle(games)                                    # fresh random order every visit
    draws = [x for x in cards if not x["game"] and x["state"] == "live"]
    track("home")
    featured = next((x for x in draws if x["c"]["featured"]), None)
    ending = sorted([x for x in draws if x is not featured], key=lambda x: x["hours_left"])[:8]
    return render_template("home.html", featured=featured, ending=ending, n_draws=len(draws), games=games[:4],
                           n_games=len(games), free_game=free_game, free_claimed=free_claimed,
                           stats=site_stats(db), recent=_recent_instant(db, 6), draw_winners=_draw_winners(db, 3),
                           public_name=public_name)


FILTERS = [("all", "All live"), ("ending", "Ending soon"), ("new", "New"), ("instant", "With instant prizes")]


@bp.route("/competitions")
def competitions():
    db = get_db()
    tab = request.args.get("tab", "all")
    q = request.args.get("q", "").strip()[:60]
    rows = db.execute("SELECT * FROM competitions WHERE status='live' AND game_type='' ORDER BY featured DESC, ends_at").fetchall()
    cards = [card_data(db, c) for c in rows]
    cards = [x for x in cards if x["state"] in ("live", "soldout")] + [x for x in cards if x["state"] == "ended"]
    if tab == "instant":
        cards = [x for x in cards if x["instant"]]
    elif tab == "ending":
        cards = sorted([x for x in cards if x["state"] == "live"], key=lambda x: x["hours_left"])
    elif tab == "new":
        cards = [x for x in cards if x["state"] == "live" and is_new(x["c"])]
    elif tab in CATEGORY_NAMES:
        cards = [x for x in cards if x["c"]["category"] == tab]
    elif tab != "all":
        return redirect(url_for("public.competitions", q=q or None), code=301)
    if q:
        ql = q.lower()
        cards = [x for x in cards if ql in x["c"]["title"].lower() or ql in (x["c"]["description"] or "").lower()]
    present = {c["category"] for c in rows}
    filters = FILTERS + [(k, n) for k, n in CATEGORIES if k in present]
    return render_template("competitions.html", cards=cards, tab=tab, q=q, filters=filters,
                           tab_name=dict(filters).get(tab, "All"))



@bp.route("/c/<slug>")
def competition(slug):
    db = get_db()
    c = db.execute("SELECT * FROM competitions WHERE slug=? AND status!='draft'", (slug,)).fetchone()
    if c is None:
        abort(404)
    data = card_data(db, c)
    if data["state"] == "live":
        track("competition", c["id"])
    mine = []
    if g.user:
        mine = db.execute(
            "SELECT t.number, ip.title AS win FROM tickets t LEFT JOIN instant_prizes ip ON ip.ticket_id=t.id "
            "WHERE t.competition_id=? AND t.user_id=? AND t.status='issued' ORDER BY t.number",
            (c["id"], g.user["id"])).fetchall()
    finished = data["state"] in ("ended", "drawn", "cancelled")
    winner = winner_details(db, c)
    others = [card_data(db, o) for o in db.execute(
        "SELECT * FROM competitions WHERE status='live' AND id!=? ORDER BY featured DESC, ends_at LIMIT 4", (c["id"],))]
    tiers = sorted(parse_tiers(c["discount_tiers"]))
    waiting = unplayed(db, g.user["id"], c["id"]) if g.user and c["game_type"] else []
    claimed = bool(c["free_daily"] and g.user and free_play_today(db, g.user["id"], c["id"]))
    held = entrant_count(db, c["id"], g.user["id"], g.user["email"]) if g.user else 0
    allowance = max(0, c["max_per_user"] - held)
    if data["state"] == "live":
        allowance = min(allowance, c["max_tickets"] - sold_count(db, c["id"]))
    draw_rec = db.execute("SELECT entry_count, drawn_at, method FROM draws WHERE competition_id=? ORDER BY id DESC LIMIT 1",
                          (c["id"],)).fetchone()
    draw_history = db.execute("SELECT drawn_at, method, reason, winning_number, entry_count, entries_hash FROM draws "
                              "WHERE competition_id=? ORDER BY id", (c["id"],)).fetchall()
    snapshot = db.execute("SELECT taken_at, entry_count, paid_count, postal_count, entries_hash FROM entry_snapshots "
                          "WHERE competition_id=? ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone()
    watching = db.execute("SELECT * FROM watchlist WHERE user_id=? AND competition_id=?",
                          (g.user["id"], c["id"])).fetchone() if g.user else None
    share = current_app.config["SITE_URL"] + url_for("public.competition", slug=c["slug"])
    if g.user:
        share += "?ref=" + g.user["referral_code"]
    if c["game_type"] and mine:
        revealed = {r[0] for r in db.execute("SELECT number FROM tickets WHERE competition_id=? AND user_id=? AND revealed_at IS NOT NULL",
                                             (c["id"], g.user["id"]))}
        mine = [{"number": t["number"], "win": t["win"] if t["number"] in revealed else None} for t in mine]
    return render_template("competition.html", c=c, d=data, mine=mine, waiting=waiting, gi=game_info(db, c),
                           claimed=claimed, share=share, board=instant_board(db, c["id"], reveal=finished),
                           winner_name=public_name(winner["name"]) if winner else None,
                           winner_number=winner["number"] if winner else None, others=others, tiers=tiers,
                           category=CATEGORY_NAMES.get(c["category"], "Other"), max_picks=MAX_PICKS,
                           tiers_json=json.dumps(tiers), held=held, allowance=allowance, draw_rec=draw_rec,
                           draw_history=draw_history, snapshot=snapshot, watching=watching)


@bp.route("/c/<slug>/numbers")
def numbers(slug):
    """Taken ticket numbers in a 100-number page, for the number picker."""
    db = get_db()
    c = db.execute("SELECT id, max_tickets FROM competitions WHERE slug=? AND status='live'", (slug,)).fetchone()
    if c is None:
        abort(404)
    lo = max(1, request.args.get("start", 1, type=int))
    hi = min(c["max_tickets"], lo + 99)
    return jsonify({"start": lo, "end": hi, "max": c["max_tickets"], "taken": sorted(taken_numbers(db, c["id"], lo, hi))})


@bp.route("/c/<slug>/entries")
def entry_list(slug):
    db = get_db()
    c = db.execute("SELECT * FROM competitions WHERE slug=? AND status!='draft'", (slug,)).fetchone()
    if c is None:
        abort(404)
    page = max(1, request.args.get("page", 1, type=int))
    q = request.args.get("q", type=int)
    sql = ("SELECT t.number, t.created_at, COALESCE(u.name, p.name) AS name, t.postal_entry_id IS NOT NULL AS postal "
           "FROM tickets t LEFT JOIN users u ON u.id=t.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id "
           "WHERE t.competition_id=? AND t.status='issued'")
    args = [c["id"]]
    if q:
        sql += " AND t.number=?"
        args.append(q)
    total = db.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    rows = db.execute(sql + " ORDER BY t.number LIMIT 200 OFFSET ?", args + [(page - 1) * 200]).fetchall()
    mine = {r[0] for r in db.execute("SELECT number FROM tickets WHERE competition_id=? AND user_id=? AND status='issued'",
                                     (c["id"], g.user["id"]))} if g.user else set()
    return render_template("entries.html", c=c, rows=rows, page=page, pages=max(1, -(-total // 200)), total=total,
                           public_name=public_name, q=q, mine=mine)


@bp.route("/winners")
def winners():
    db = get_db()
    tab = request.args.get("tab", "winners")
    if tab == "results":
        return redirect(url_for("public.results"), code=301)      # one results destination: the permanent archive
    if tab not in ("winners", "live"):
        return redirect(url_for("public.winners"), code=301)
    ctx = {"tab": tab, "stats": site_stats(db), "public_name": public_name}
    if tab == "winners":
        ctx.update(items=_draw_winners(db, 60), instant=_recent_instant(db, 50))
    elif tab == "results":
        items = []
        for c in db.execute("SELECT * FROM competitions WHERE status IN ('drawn','live') AND game_type='' "
                            "ORDER BY ends_at DESC LIMIT 200").fetchall():
            d = card_data(db, c)
            if d["state"] in ("drawn", "ended", "soldout"):
                w = winner_details(db, c)
                items.append({"d": d, "name": public_name(w["name"]) if w else None, "number": w["number"] if w else None})
        ctx["items"] = items
    else:
        ctx["upcoming"] = [x for x in (card_data(db, c) for c in db.execute(
            "SELECT * FROM competitions WHERE status='live' AND game_type='' ORDER BY ends_at LIMIT 12")) if x["state"] == "live"]
    return render_template("winners.html", **ctx)


@bp.route("/results")
def results():
    """Permanent archive of every completed draw: nothing finished ever disappears."""
    db = get_db()
    q = request.args.get("q", "").strip()[:60]
    year = request.args.get("year", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    sql, args = ("SELECT c.*, d.winning_number, d.entry_count, (SELECT COUNT(*) FROM draws x WHERE x.competition_id=c.id) AS n_draws "
                 "FROM competitions c LEFT JOIN draws d ON d.id=(SELECT MAX(id) FROM draws WHERE competition_id=c.id) "
                 "WHERE c.status='drawn' AND c.game_type=''"), []
    if q:
        sql += " AND c.title LIKE ?"
        args.append(f"%{q}%")
    if year:
        sql += " AND substr(c.drawn_at,1,4)=?"
        args.append(str(year))
    total = db.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    rows = db.execute(sql + " ORDER BY c.drawn_at DESC LIMIT 30 OFFSET ?", args + [(page - 1) * 30]).fetchall()
    items = []
    for c in rows:
        w = winner_details(db, c)
        items.append({"c": c, "name": public_name(w["name"]) if w else None, "number": w["number"] if w else c["winning_number"]})
    years = [r[0] for r in db.execute("SELECT DISTINCT substr(drawn_at,1,4) FROM competitions WHERE status='drawn' AND game_type='' "
                                      "ORDER BY 1 DESC")]
    return render_template("results.html", items=items, q=q, year=year, years=years, page=page,
                           pages=max(1, -(-total // 30)), total=total)


@bp.route("/search")
def search():
    require_feature("search")
    db = get_db()
    q = " ".join(request.args.get("q", "").split())[:60]
    found = {"live": [], "games": [], "results": []}
    if q:
        like = f"%{q}%"
        for c in db.execute("SELECT * FROM competitions WHERE status='live' AND free_daily=0 AND (title LIKE ? OR description LIKE ?) "
                            "ORDER BY ends_at LIMIT 40", (like, like)).fetchall():
            d = card_data(db, c)
            if d["state"] in ("live", "soldout"):
                found["games" if c["game_type"] else "live"].append(d)
        found["results"] = [card_data(db, c) for c in db.execute(
            "SELECT * FROM competitions WHERE status='drawn' AND game_type='' AND title LIKE ? ORDER BY drawn_at DESC LIMIT 12", (like,))]
    return render_template("search.html", q=q, found=found, n=sum(len(v) for v in found.values()))


@bp.route("/watch/<slug>", methods=["POST"])
@login_required
def watch(slug):
    require_feature("watchlist")
    db = get_db()
    c = db.execute("SELECT id, title FROM competitions WHERE slug=?", (slug,)).fetchone()
    if c is None:
        abort(404)
    if request.form.get("action") == "remove":
        db.execute("DELETE FROM watchlist WHERE user_id=? AND competition_id=?", (g.user["id"], c["id"]))
        flash(f"Removed {c['title']} from your saved competitions. No reminders will be sent.")
    else:
        close, result = 1 if request.form.get("remind_close") else 0, 1 if request.form.get("remind_result") else 0
        db.execute("INSERT INTO watchlist (user_id, competition_id, created_at, remind_close, remind_result) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(user_id, competition_id) DO UPDATE SET remind_close=excluded.remind_close, "
                   "remind_result=excluded.remind_result", (g.user["id"], c["id"], iso(utcnow()), close, result))
        what = " and ".join(x for x in ("before it closes" if close else "", "when the result is in" if result else "") if x)
        how = "by email and in your notifications" if g.user["reminder_emails"] else "in your notifications"
        flash(f"Saved {c['title']}." + (f" We'll remind you {what}, {how}." if what else " No reminders — you can find it in My account."))
    return redirect(url_for("public.competition", slug=slug))


@bp.route("/live")
def live():
    return redirect(url_for("public.winners", tab="live"), code=301)


# ---------------- instant-win games ----------------

@bp.route("/games")
def games():
    return redirect(url_for("public.instant_wins", **request.args), code=301)


@bp.route("/instant-wins")
def instant_wins():
    db = get_db()
    price = request.args.get("price", type=int)
    kind = request.args.get("type", "")
    rows = db.execute("SELECT * FROM competitions WHERE status='live' AND game_type!='' ORDER BY ticket_price, ends_at").fetchall()
    cards = [x for x in (card_data(db, c) for c in rows) if x["state"] == "live"]
    free_game, free_claimed = _free_game(db, cards)
    cards = [x for x in cards if not x["c"]["free_daily"]]
    if price:
        cards = [x for x in cards if x["c"]["ticket_price"] == price]
    if kind:
        cards = [x for x in cards if x["c"]["game_type"] == kind]
    _shuffle(cards)                                    # fresh random order every visit
    waiting = unplayed(db, g.user["id"]) if g.user else []
    return render_template("games.html", cards=cards, price=price, kind=kind, prices=GAME_PRICES, types=GAME_TYPES,
                           waiting=waiting, free_game=free_game, free_claimed=free_claimed)



@bp.route("/free-play/<slug>", methods=["POST"])
@login_required
def free_play(slug):
    c = get_db().execute("SELECT * FROM competitions WHERE slug=?", (slug,)).fetchone()
    if c is None:
        abort(404)
    try:
        claim_free_play(g.user, c["id"])
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(request.referrer if request.referrer and request.referrer.startswith(current_app.config["SITE_URL"])
                        else url_for("public.instant_wins"))
    return redirect(url_for("public.play", slug=slug))


@bp.route("/play/<slug>")
@login_required
def play(slug):
    db = get_db()
    c = db.execute("SELECT * FROM competitions WHERE slug=? AND game_type!=''", (slug,)).fetchone()
    if c is None:
        abort(404)
    waiting = unplayed(db, g.user["id"], c["id"])
    history = db.execute(
        "SELECT t.number, ip.title AS win, ip.value FROM tickets t LEFT JOIN instant_prizes ip ON ip.ticket_id=t.id "
        "WHERE t.competition_id=? AND t.user_id=? AND t.revealed_at IS NOT NULL ORDER BY t.revealed_at DESC LIMIT 50",
        (c["id"], g.user["id"])).fetchall()
    prizes = db.execute("SELECT title, value FROM instant_prizes WHERE competition_id=? GROUP BY title ORDER BY value DESC",
                        (c["id"],)).fetchall()
    return render_template("play.html", c=c, waiting=waiting, history=history, prizes=prizes,
                           d=card_data(db, c), game_name=GAME_NAMES.get(c["game_type"], "Game"),
                           more=[x for x in (card_data(db, o) for o in db.execute(
                               "SELECT * FROM competitions WHERE status='live' AND game_type!='' AND id!=? "
                               "ORDER BY RANDOM() LIMIT 4", (c["id"],))) if x["state"] == "live"])


@bp.route("/play/reveal/<int:tid>", methods=["POST"])
@login_required
def play_reveal(tid):
    try:
        return jsonify(reveal_ticket(g.user["id"], tid))
    except PurchaseError as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/play/<slug>/reveal-all", methods=["POST"])
@login_required
def play_reveal_all(slug):
    db = get_db()
    c = db.execute("SELECT id FROM competitions WHERE slug=?", (slug,)).fetchone()
    if c is None:
        abort(404)
    won = []
    for t in unplayed(db, g.user["id"], c["id"]):
        r = reveal_ticket(g.user["id"], t["id"])
        if r["win"]:
            won.append(f"#{r['number']}: {r['title']}")
    flash(("🎉 You won: " + ", ".join(won)) if won else "All plays revealed — no wins this time. Good luck next time!")
    return redirect(url_for("public.play", slug=slug))


# ---------------- basket ----------------

def _basket():
    return session.get("basket", [])


def _basket_view(db):
    lines, total, gross_total = [], 0, 0
    for i, ln in enumerate(_basket()):
        c = db.execute("SELECT * FROM competitions WHERE id=?", (ln["comp_id"],)).fetchone()
        if c is None:
            continue
        qty = len(ln["numbers"]) if ln["numbers"] else ln["qty"]
        gross, disc, net, p = line_price(c, qty)
        nxt = next(((q, pc) for q, pc in sorted(parse_tiers(c["discount_tiers"])) if q > qty), None)
        clash = sorted(set(ln["numbers"]) & taken_numbers(db, c["id"])) if ln["numbers"] else []
        lines.append({"i": i, "c": c, "qty": qty, "numbers": ln["numbers"], "gross": gross, "disc": disc, "net": net,
                      "pct": p, "open": comp_state(c) == "live", "next_tier": nxt, "clash": clash})
        total += net
        gross_total += gross
    return lines, total, gross_total


def _totals(db, total):
    """Everything the customer will pay, worked out before they leave for the payment page."""
    t = {"promo": None, "promo_disc": 0, "promo_error": None, "credit": 0, "deposit": 0, "cash": 0, "card": 0}
    code = session.get("promo", "")
    if code and total:
        try:
            p, t["promo_disc"] = check_promo(db, code, g.user["id"] if g.user else 0, total)
            t["promo"] = p["code"]
        except PurchaseError as e:
            t["promo_error"] = str(e)
    after = total - t["promo_disc"]
    if g.user:
        bal = balances(db, g.user["id"])
        t["credit"] = min(max(bal["credit"], 0), after)
        t["deposit"] = min(max(bal["deposit"], 0), after - t["credit"])
        t["cash"] = min(max(bal["cash"], 0), after - t["credit"] - t["deposit"])
        t["bal"] = bal
    t["after"] = after
    t["card"] = after - t["credit"] - t.get("deposit", 0) - t["cash"]
    return t


@bp.route("/basket")
def basket():
    db = get_db()
    lines, total, gross = _basket_view(db)
    if lines:
        track("basket")
    session["basket_idem"] = secrets.token_urlsafe(16)   # new key per view: a double-click shares it, a fresh visit doesn't
    return render_template("basket.html", lines=lines, total=total, gross=gross, t=_totals(db, total),
                           closed=any(not ln["open"] for ln in lines), clash=any(ln["clash"] for ln in lines),
                           idem=session["basket_idem"])


@bp.route("/basket/add", methods=["POST"])
def basket_add():
    db = get_db()
    c = db.execute("SELECT * FROM competitions WHERE slug=?", (request.form.get("slug", ""),)).fetchone()
    if c is None:
        abort(404)
    back = url_for("public.competition", slug=c["slug"]) + "#enter"
    if comp_state(c, sold_count(db, c["id"])) != "live":
        flash("This competition isn't open for entries.", "error")
        return redirect(back)
    if c["free_daily"]:
        flash("This game is free — claim your daily play instead.", "error")
        return redirect(back)
    no_q = c["question_mode"] == "none"
    if not no_q and not request.form.get("answer"):
        flash("Choose an answer to the question first.", "error")
        return redirect(back)
    if not no_q and request.form.get("answer") != c["correct"]:
        flash("That answer isn't right — have another look at the question.", "error")
        return redirect(back)
    try:
        nums = sorted({int(x) for x in request.form.get("numbers", "").split(",") if x.strip()})
        qty = len(nums) if nums else int(request.form.get("quantity", "1"))
    except ValueError:
        flash("Enter a valid number of tickets.", "error")
        return redirect(back)
    if qty < 1 or len(nums) > MAX_PICKS or any(n < 1 or n > c["max_tickets"] for n in nums):
        flash("Pick valid ticket numbers.", "error")
        return redirect(back)
    if qty > c["max_per_user"]:
        flash(f"The limit is {c['max_per_user']} tickets per person.", "error")
        return redirect(back)
    if nums:
        gone = sorted(set(nums) & taken_numbers(db, c["id"]))
        if gone:
            flash(f"Sorry — {', '.join('#' + str(n) for n in gone[:10])} {'has' if len(gone) == 1 else 'have'} just been taken. "
                  "Pick different numbers or use lucky dip.", "error")
            return redirect(back)
    if g.user:
        have = entrant_count(db, c["id"], g.user["id"], g.user["email"])
        if have + qty > c["max_per_user"]:
            left = max(0, c["max_per_user"] - have)
            flash(f"The limit is {c['max_per_user']} per person and you already have {have}"
                  + (f" — you can add up to {left} more." if left else ", so you can't add any more."), "error")
            return redirect(back)
    b = [ln for ln in _basket() if ln["comp_id"] != c["id"]]  # one line per competition
    if len(b) >= MAX_BASKET_LINES:
        flash("Your basket is full — check out first.", "error")
        return redirect(back)
    b.append({"comp_id": c["id"], "qty": qty, "numbers": nums, "answer": request.form.get("answer", "")})
    track("add", c["id"])
    session["basket"] = b
    word = "play" if c["game_type"] else "ticket"
    flash(f"Added {qty} {word}{'s' if qty != 1 else ''} for {c['title']} to your basket.")
    return redirect(url_for("public.basket") if request.form.get("go") == "checkout" else back.split("#")[0])


@bp.route("/basket/update", methods=["POST"])
def basket_update():
    db = get_db()
    i = request.form.get("i", type=int)
    qty = request.form.get("qty", type=int)
    b = _basket()
    if i is None or not 0 <= i < len(b) or qty is None:
        return redirect(url_for("public.basket"))
    if qty < 1:
        b.pop(i)
    else:
        c = db.execute("SELECT max_per_user, max_tickets FROM competitions WHERE id=?", (b[i]["comp_id"],)).fetchone()
        if c is None:
            b.pop(i)
        elif b[i]["numbers"]:
            flash("You picked your own numbers for this one — remove it and pick again to change them.", "error")
            return redirect(url_for("public.basket"))
        else:
            if qty > c["max_per_user"]:
                flash(f"The limit is {c['max_per_user']} per person.", "error")
            b[i]["qty"] = min(qty, c["max_per_user"])
    session["basket"] = b
    return redirect(url_for("public.basket"))


@bp.route("/basket/promo", methods=["POST"])
def basket_promo():
    code = request.form.get("promo", "").strip().upper()[:40]
    if request.form.get("remove") or not code:
        session.pop("promo", None)
        flash("Promo code removed.")
        return redirect(url_for("public.basket"))
    db = get_db()
    _, total, _ = _basket_view(db)
    try:
        _, disc = check_promo(db, code, g.user["id"] if g.user else 0, total)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(url_for("public.basket"))
    session["promo"] = code
    flash(f"{code} applied — you save £{disc/100:.2f}.")
    return redirect(url_for("public.basket"))


@bp.route("/basket/remove", methods=["POST"])
def basket_remove():
    i = request.form.get("i", type=int)
    b = _basket()
    if i is not None and 0 <= i < len(b):
        b.pop(i)
        session["basket"] = b
    return redirect(url_for("public.basket"))


@bp.route("/basket/checkout", methods=["POST"])
@login_required
def checkout():
    db = get_db()
    lines = _basket()
    from .status import payments_paused
    paused = payments_paused()
    if paused:
        flash(paused, "error")
        return redirect(url_for("public.basket"))
    promo = request.form.get("promo") or session.get("promo", "")
    idem = (request.form.get("idem") or "")[:64] or None
    try:
        cid, cash, created = reserve_checkout(g.user, lines, promo, request.form.get("use_credit") == "1", idem)
    except (PurchaseError, ValueError) as e:
        if "spend limit" in str(e):
            audit(db, "safer.limit_block", f"user:{g.user['id']}", str(e)[:200])
        flash(str(e), "error")
        return redirect(url_for("public.basket"))
    if not created:                                  # second press of the same Pay button
        return redirect(url_for("public.checkout_pay", cid=cid))
    session.pop("basket_idem", None)
    dev = device_type()
    db.execute("UPDATE checkouts SET device=? WHERE id=?", (dev, cid))
    for ln in lines:
        track("checkout", ln["comp_id"], dev)
    session["held"] = {"cid": cid, "basket": lines, "promo": promo}   # restored if payment is cancelled
    session["basket"] = []
    session.pop("promo", None)
    if cash == 0:
        fulfil_checkout(cid)
        _confirm_email(cid)
        return redirect(url_for("public.checkout_done", cid=cid))
    if payments.enabled():
        base = current_app.config["SITE_URL"]
        desc = ", ".join(f"{r['quantity']}× {r['title']}" for r in db.execute(
            "SELECT o.quantity, c.title FROM orders o JOIN competitions c ON c.id=o.competition_id WHERE o.checkout_id=?", (cid,)))
        try:
            cs = payments.create_checkout(cid, cash, desc, g.user["email"],
                                          success_url=f"{base}{url_for('public.checkout_done', cid=cid)}",
                                          cancel_url=f"{base}{url_for('public.checkout_cancel', cid=cid)}")
        except Exception:
            current_app.logger.exception("Stripe checkout failed")
            from .status import record_provider_error
            record_provider_error()
            expire_checkout(cid)
            _restore_basket(cid)
            flash("Payment provider unavailable — you have not been charged. Please try again.", "error")
            return redirect(url_for("public.basket"))
        db.execute("UPDATE checkouts SET stripe_session_id=?, pay_url=? WHERE id=?", (cs["id"], cs["url"], cid))
        return redirect(cs["url"], code=303)
    if current_app.config["DEMO_PAYMENTS"]:
        db.execute("UPDATE checkouts SET pay_url=? WHERE id=?", (url_for("public.demo_pay", cid=cid), cid))
        return redirect(url_for("public.demo_pay", cid=cid))
    expire_checkout(cid)
    _restore_basket(cid)
    flash("Payments aren't switched on yet — you have not been charged.", "error")
    return redirect(url_for("public.basket"))


def _restore_basket(cid):
    held = session.get("held") or {}
    if held.get("cid") == cid and not session.get("basket"):
        session["basket"] = held.get("basket", [])
        if held.get("promo"):
            session["promo"] = held["promo"]
    session.pop("held", None)


def _own_checkout(cid):
    c = get_db().execute("SELECT * FROM checkouts WHERE id=?", (cid,)).fetchone()
    if c is None or c["user_id"] != g.user["id"]:
        abort(404)
    return c


def _confirm_email(cid):
    db = get_db()
    c = db.execute("SELECT c.*, u.email, u.name FROM checkouts c JOIN users u ON u.id=c.user_id WHERE c.id=?", (cid,)).fetchone()
    if not c or c["status"] != "paid":
        return
    lines, chips, games_ = [], [], []
    for s in checkout_summary(db, cid):
        if s["game"]:
            games_.append(s)
            lines.append(f"{s['title']}: {len(s['tickets'])} play(s) ready to reveal")
            continue
        lines.append(f"{s['title']}: {len(s['tickets'])} ticket(s)")
        chips += [f"#{t['number']}" + (f" ⚡ {t['win']}" if t["win"] else "") for t in s["tickets"][:60]]
    base = current_app.config["SITE_URL"]
    button = (("▶ Play now", base + url_for("public.play", slug=games_[0]["slug"])) if games_
              else ("View my entries", base + url_for("public.account", tab="entries")))
    tell(c["email"], "You're in! Your entries are confirmed 🎟️", kind="order", key=f"order:{cid}", user_id=c["user_id"],
         link=url_for("public.order_detail", cid=cid), title=f"Entries confirmed — order #{cid}", body=
                f"Hi {c['name'].split()[0]},\n\nThanks for entering — you're all set.\n\n" + "\n".join(lines) +
                ("\n\nYour ticket numbers:" if chips else "") ,
                highlight=chips or None, button=button, heading="You're in!",
                preheader="Your entries are confirmed — good luck!")


@bp.route("/checkout/<int:cid>/pay")
@login_required
def checkout_pay(cid):
    """Where a repeated Pay press (or a refresh) lands: carry on with the one checkout already started."""
    c = _own_checkout(cid)
    if c["status"] != "pending" or c["cash_due"] == 0:
        return redirect(url_for("public.checkout_done", cid=cid))
    if c["pay_url"]:
        return redirect(c["pay_url"], code=303)
    return render_template("checkout_wait.html", c=c)       # payment page still being prepared


@bp.route("/checkout/<int:cid>/demo-pay", methods=["GET", "POST"])
@login_required
def demo_pay(cid):
    if not current_app.config["DEMO_PAYMENTS"] or payments.enabled():
        abort(404)
    c = _own_checkout(cid)
    if request.method == "POST":
        fulfil_checkout(cid)
        _confirm_email(cid)
        return redirect(url_for("public.checkout_done", cid=cid))
    return render_template("demo_pay.html", c=c, lines=checkout_summary(get_db(), cid))


@bp.route("/checkout/<int:cid>/done")
@login_required
def checkout_done(cid):
    c = _own_checkout(cid)
    lines = checkout_summary(get_db(), cid)
    if c["status"] == "paid" and len(lines) == 1 and lines[0]["game"]:
        return redirect(url_for("public.play", slug=lines[0]["slug"]))   # straight into the game
    wins = [t for s in lines for t in s["tickets"] if t["win"]]
    return render_template("checkout_done.html", c=c, lines=lines, wins=wins)


@bp.route("/checkout/<int:cid>/cancel")
@login_required
def checkout_cancel(cid):
    c = _own_checkout(cid)
    if c["status"] == "pending":
        expire_checkout(cid)
    _restore_basket(cid)
    flash("Payment cancelled — you have not been charged. Your basket is below.")
    return redirect(url_for("public.basket"))


def _refuse_credit_card(cid, session):
    """UK prize-draw code: no credit cards. Refund automatically and release the tickets."""
    if not current_app.config["BLOCK_CREDIT_CARDS"] or not session.get("payment_intent"):
        return False
    pi = session["payment_intent"]
    try:
        funding = payments.card_funding(pi)
    except Exception:
        current_app.logger.exception("Couldn't check card type for checkout %s — accepting it", cid)
        return False
    if funding != "credit":
        return False
    db = get_db()
    c = db.execute("SELECT c.status, u.email, u.name FROM checkouts c JOIN users u ON u.id=c.user_id WHERE c.id=?",
                   (cid,)).fetchone()
    if c is None or c["status"] != "pending":
        return c is not None and c["status"] in ("credit_refused", "needs_refund")
    try:
        payments.refund(pi)
        status = "credit_refused"
    except Exception:
        current_app.logger.exception("Auto-refund failed for checkout %s — refund it in Stripe", cid)
        status = "needs_refund"
    refuse_checkout(cid, status, session.get("id"))
    current_app.logger.warning("Checkout %s paid by credit card — %s", cid, status)
    tell(c["email"], "Please use a debit card", kind="payment", key=f"refused:{cid}", user_id=None, body=
                f"Hi {c['name'].split()[0]},\n\nSorry — we can't accept credit cards for competition entries, so your "
                "payment has been refunded in full (it can take 5–10 days to show). No tickets were issued.\n\n"
                "Please try again with a debit card, Apple Pay / Google Pay linked to a debit card, or Pay by Bank.",
                heading="Credit cards not accepted", button=("Browse competitions", current_app.config["SITE_URL"] + url_for("public.competitions")))
    return True


@bp.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    secret = current_app.config["STRIPE_WEBHOOK_SECRET"]
    if not secret or not payments.verify_webhook(payload, request.headers.get("Stripe-Signature", ""), secret):
        abort(400)
    event = json.loads(payload)
    obj = event.get("data", {}).get("object", {})
    if event.get("type") in ("charge.refunded", "charge.dispute.created", "charge.dispute.closed"):
        _payment_reversal(event["type"], obj)
        return "", 200
    did = (obj.get("metadata") or {}).get("deposit_id")
    if did:
        if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded") \
                and obj.get("payment_status") == "paid":
            if _refuse_credit_card_deposit(int(did), obj):
                return "", 200
            if fulfil_deposit(int(did), obj.get("id"), obj.get("payment_intent")) == "paid":
                _deposit_email(int(did))
        elif event["type"] in ("checkout.session.expired", "checkout.session.async_payment_failed"):
            set_deposit_status(int(did), "expired")
        return "", 200
    cid = (obj.get("metadata") or {}).get("checkout_id")
    if cid:
        if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded") \
                and obj.get("payment_status") == "paid":
            if _refuse_credit_card(int(cid), obj):
                return "", 200
            if obj.get("payment_intent"):
                get_db().execute("UPDATE checkouts SET payment_intent=? WHERE id=? AND payment_intent IS NULL",
                                 (obj["payment_intent"], int(cid)))
            if fulfil_checkout(int(cid), obj.get("id"), obj.get("amount_total")) == "paid":
                _confirm_email(int(cid))
        elif event["type"] in ("checkout.session.expired", "checkout.session.async_payment_failed"):
            expire_checkout(int(cid))
    return "", 200


def _payment_reversal(kind, obj):
    """A refund made in Stripe's dashboard, or a chargeback. Nothing is changed automatically — staff decide —
    but it's flagged at once (not just at the next daily reconciliation) and staff are emailed."""
    from .control import _flag
    pi = obj.get("payment_intent")
    db = get_db()
    k = db.execute("SELECT id, user_id, status FROM checkouts WHERE payment_intent=?", (pi,)).fetchone() if pi else None
    d = db.execute("SELECT id, user_id FROM deposits WHERE payment_intent=?", (pi,)).fetchone() if pi and not k else None
    what = f"checkout #{k['id']} (customer {k['user_id']}, {k['status']})" if k else \
        f"deposit #{d['id']} (customer {d['user_id']})" if d else f"payment {pi} (no matching order)"
    label = {"charge.refunded": "Refunded in Stripe", "charge.dispute.created": "Chargeback opened",
             "charge.dispute.closed": "Chargeback closed"}[kind]
    amount = obj.get("amount_refunded") if kind == "charge.refunded" else obj.get("amount")
    detail = f"{label}: {what}, {amount}p. Check whether entries should be cancelled or the wallet adjusted."
    if kind == "charge.dispute.closed":
        detail = f"{label} ({obj.get('status')}): {what}"
    if _flag(db, "Payment reversal", f"{kind}:{obj.get('id')}", detail):
        audit(db, "payment.reversal", f"checkout:{k['id']}" if k else (f"deposit:{d['id']}" if d else None), detail, actor=False)
        if current_app.config["SUPPORT_EMAIL"]:
            mailer.send(current_app.config["SUPPORT_EMAIL"], f"⚠ {label}", detail + "\n\nSee Admin → Flags.", heading=label)


# ---------------- deposits ----------------

def _own_deposit(did):
    d = get_db().execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
    if d is None or d["user_id"] != g.user["id"]:
        abort(404)
    return d


@bp.route("/account/deposit", methods=["POST"])
@login_required
def deposit():
    require_feature("deposits")
    back = url_for("public.account", tab="wallet") + "#deposit"
    from .status import payments_paused
    if payments_paused():
        flash(payments_paused(), "error")
        return redirect(back)
    try:
        amount = to_pence(request.form.get("amount", ""))
        did = create_deposit(g.user, amount)
    except ValueError:
        flash("Enter an amount in pounds, e.g. 20.", "error")
        return redirect(back)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(back)
    if payments.enabled():
        base = current_app.config["SITE_URL"]
        try:
            cs = payments.create_checkout(did, amount, "Funds for competition entries", g.user["email"],
                                          success_url=f"{base}{url_for('public.deposit_done', did=did)}",
                                          cancel_url=f"{base}{url_for('public.deposit_cancel', did=did)}", kind="deposit")
        except Exception:
            current_app.logger.exception("Stripe deposit checkout failed")
            set_deposit_status(did, "expired")
            flash("Payment provider unavailable — you have not been charged. Please try again.", "error")
            return redirect(back)
        get_db().execute("UPDATE deposits SET stripe_session_id=? WHERE id=?", (cs["id"], did))
        return redirect(cs["url"], code=303)
    if current_app.config["DEMO_PAYMENTS"]:
        return redirect(url_for("public.deposit_demo", did=did))
    set_deposit_status(did, "expired")
    flash("Payments aren't switched on yet — you have not been charged.", "error")
    return redirect(back)


@bp.route("/deposit/<int:did>/demo-pay", methods=["GET", "POST"])
@login_required
def deposit_demo(did):
    if not current_app.config["DEMO_PAYMENTS"] or payments.enabled():
        abort(404)
    d = _own_deposit(did)
    if request.method == "POST":
        fulfil_deposit(did)
        _deposit_email(did)
        return redirect(url_for("public.deposit_done", did=did))
    return render_template("deposit_pay.html", d=d)


@bp.route("/deposit/<int:did>/done")
@login_required
def deposit_done(did):
    d = _own_deposit(did)
    return render_template("deposit_done.html", d=d, bal=balances(get_db(), g.user["id"]))


@bp.route("/deposit/<int:did>/cancel")
@login_required
def deposit_cancel(did):
    d = _own_deposit(did)
    if d["status"] == "pending":
        set_deposit_status(did, "expired")
    flash("Deposit cancelled — you have not been charged.")
    return redirect(url_for("public.account", tab="wallet"))


@bp.route("/account/deposit/refund", methods=["POST"])
@login_required
def deposit_refund():
    def card_refund(pi, amount):
        if payments.enabled():
            payments.refund(pi, amount=amount, why="unspent wallet deposit")
    try:
        total = refund_deposits(g.user["id"], card_refund)
    except Exception:
        current_app.logger.exception("Deposit refund failed for user %s", g.user["id"])
        flash("We couldn't process the refund automatically — we've been notified and will refund you by hand.", "error")
        if current_app.config["SUPPORT_EMAIL"]:
            mailer.send(current_app.config["SUPPORT_EMAIL"], "Deposit refund needs doing by hand",
                        f"{g.user['name']} ({g.user['email']}) asked for unspent deposits back and the automatic Stripe refund failed. "
                        "Check Admin → Users and Stripe.")
        return redirect(url_for("public.account", tab="wallet"))
    if total:
        flash(f"£{total / 100:.2f} is on its way back to your card. Refunds usually take 5–10 working days to appear.")
        tell(g.user["email"], "Your deposit refund", kind="wallet", key=f"deposit-refund:{g.user['id']}:{utcnow():%Y%m%d%H%M%S}",
             user_id=g.user["id"], link=url_for("public.account", tab="wallet"), body=
                    f"Hi {g.user['name'].split()[0]},\n\nWe've refunded £{total / 100:.2f} of unspent deposited funds to the card "
                    "you paid with. It usually takes 5–10 working days to appear on your statement.",
                    heading="Refund on its way")
    else:
        flash("You don't have any unspent deposited funds to refund.")
    return redirect(url_for("public.account", tab="wallet"))


def _deposit_email(did):
    db = get_db()
    d = db.execute("SELECT d.*, u.email, u.name FROM deposits d JOIN users u ON u.id=d.user_id WHERE d.id=?", (did,)).fetchone()
    if not d or d["status"] != "paid":
        return
    tell(d["email"], f"£{d['amount'] / 100:.2f} added to your wallet", kind="wallet", key=f"deposit:{did}", user_id=d["user_id"],
         link=url_for("public.account", tab="wallet"), body=
                f"Hi {d['name'].split()[0]},\n\nYour deposit of £{d['amount'] / 100:.2f} is in your wallet and ready to use on "
                "any competition or instant win game. It's used automatically at checkout.\n\n"
                "Changed your mind? Unspent deposits can be refunded to your card at any time from your wallet.",
                button=("Find a competition", current_app.config["SITE_URL"] + url_for("public.competitions")),
                heading="Funds added", highlight=f"£{d['amount'] / 100:.2f}")


def _refuse_credit_card_deposit(did, sess):
    if not current_app.config["BLOCK_CREDIT_CARDS"] or not sess.get("payment_intent"):
        return False
    d = get_db().execute("SELECT status FROM deposits WHERE id=?", (did,)).fetchone()
    if d is None or d["status"] != "pending":          # webhook retried — never refund twice
        return d is not None and d["status"] in ("credit_refused", "needs_refund")
    pi = sess["payment_intent"]
    try:
        if payments.card_funding(pi) != "credit":
            return False
    except Exception:
        current_app.logger.exception("Couldn't check card type for deposit %s — accepting it", did)
        return False
    try:
        payments.refund(pi)
        status = "credit_refused"
    except Exception:
        current_app.logger.exception("Auto-refund failed for deposit %s — refund it in Stripe", did)
        status = "needs_refund"
    set_deposit_status(did, status, pi)
    current_app.logger.warning("Deposit %s paid by credit card — %s", did, status)
    return True


# ---------------- static-ish pages ----------------

PAGES = {"free-entry": "free_entry.html", "terms": "terms.html", "fair-draws": "fair.html", "faq": "faq.html",
         "responsible-play": "responsible.html", "complaints": "complaints.html", "privacy": "privacy.html",
         "cookies": "cookies.html", "about": "about.html"}


@bp.route("/<any(" + ",".join(f'"{k}"' for k in PAGES) + "):page>")
def page(page):
    return render_template(PAGES[page])


@bp.route("/transparency")
def transparency():
    """One place that points to the single authoritative page for each subject, with live facts."""
    db = get_db()
    facts = {
        "draws": db.execute("SELECT COUNT(*) FROM draws WHERE method!='redraw'").fetchone()[0],
        "redraws": db.execute("SELECT COUNT(*) FROM draws WHERE method='redraw'").fetchone()[0],
        "entries": db.execute("SELECT COALESCE(SUM(entry_count),0) FROM entry_snapshots").fetchone()[0],
        "postal": db.execute("SELECT COALESCE(SUM(postal_count),0) FROM entry_snapshots").fetchone()[0],
        "postal_accepted": db.execute("SELECT COUNT(*) FROM postal_entries WHERE status='accepted'").fetchone()[0],
        "last": db.execute("SELECT c.title, c.slug, c.drawn_at FROM competitions c WHERE status='drawn' AND game_type='' "
                           "ORDER BY drawn_at DESC LIMIT 1").fetchone(),
        "paid_out": db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone()[0],
    }
    return render_template("transparency.html", facts=facts, stats=site_stats(db))


@bp.route("/how-it-works")
def how_it_works():
    return render_template("how.html")


SUPPORT_TOPICS = [("Payment", "A payment or order", "high"), ("Entry", "My tickets or an entry", "normal"),
                  ("Withdrawal", "Withdrawing winnings", "high"), ("Prize", "Claiming a prize", "high"),
                  ("Account", "My account or log in", "normal"), ("Responsible play", "Limits, breaks or support", "high"),
                  ("Other", "Something else", "low")]


@bp.route("/support", methods=["GET", "POST"])
def support_centre():
    if g.user is None or not feature_on("support_centre"):
        return redirect(url_for("public.contact"))
    db = get_db()
    uid = g.user["id"]
    orders = db.execute("SELECT k.id, k.status, k.cash_due, k.created_at, (SELECT GROUP_CONCAT(c.title, ', ') FROM orders o JOIN "
                        "competitions c ON c.id=o.competition_id WHERE o.checkout_id=k.id) AS titles FROM checkouts k "
                        "WHERE k.user_id=? AND k.status!='pending' ORDER BY k.id DESC LIMIT 20", (uid,)).fetchall()
    comps = db.execute("SELECT DISTINCT c.id, c.title FROM tickets t JOIN competitions c ON c.id=t.competition_id WHERE t.user_id=? "
                       "ORDER BY c.id DESC LIMIT 30", (uid,)).fetchall()
    wds = db.execute("SELECT id, amount, status, created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    form = request.form if request.method == "POST" else {"topic": request.args.get("topic", ""),
                                                           "checkout_id": request.args.get("order", ""),
                                                           "competition_id": request.args.get("comp", "")}
    errors = {}
    if request.method == "POST":
        topic = request.form.get("topic", "")
        msg = request.form.get("message", "").strip()[:4000]
        pr = dict((t, p) for t, _, p in SUPPORT_TOPICS).get(topic)
        if not pr:
            errors["topic"] = "Choose what your question is about."
        if len(msg) < 10:
            errors["message"] = "Tell us a little more (at least 10 characters)."
        ids = {}
        for field, rows in (("checkout_id", orders), ("competition_id", comps), ("withdrawal_id", wds)):
            v = request.form.get(field, type=int)
            if v and v in {r["id"] for r in rows}:          # only references that belong to this customer
                ids[field] = v
        key = "support:" + str(uid)
        if not errors and _too_many(key, limit=5, window=3600):
            errors["message"] = "You've sent several messages in the last hour — we'll reply to those first."
        if errors:
            return render_template("support.html", form=request.form, errors=errors, topics=SUPPORT_TOPICS, orders=orders,
                                   comps=comps, wds=wds), 400
        _fail(key)
        from .control import open_case
        case_id = open_case(g.user["name"], g.user["email"], topic, msg, uid, ids.get("competition_id"), ids.get("checkout_id"))
        db.execute("UPDATE cases SET priority=?, withdrawal_id=? WHERE id=?", (pr, ids.get("withdrawal_id"), case_id))
        tell(g.user["email"], f"We've got your message [case #{case_id}]", kind="support", key=f"case:{case_id}", user_id=uid,
             link=url_for("public.case_view", case_id=case_id), title=f"Support request #{case_id} received",
             body=f"Hi {g.user['name'].split()[0]},\n\nThanks — we've got your message about \"{topic.lower()}\" and we reply within "
                  "1 working day. You can follow it, and reply, from your account.\n\nYour message:\n" + msg, heading="Message received")
        if current_app.config["SUPPORT_EMAIL"]:
            mailer.send(current_app.config["SUPPORT_EMAIL"], f"[{pr}] Support case #{case_id}: {topic}",
                        f"{g.user['name']} <{g.user['email']}> (account #{uid})\nReferences: {ids or 'none'}\n\n{msg}")
        flash(f"Thanks — your request #{case_id} is with our team. We'll reply within 1 working day.")
        return redirect(url_for("public.case_view", case_id=case_id))
    return render_template("support.html", form=form, errors=errors, topics=SUPPORT_TOPICS, orders=orders, comps=comps, wds=wds)


@bp.route("/support/requests")
@login_required
def my_cases():
    rows = get_db().execute("SELECT k.*, (SELECT MAX(created_at) FROM case_notes n WHERE n.case_id=k.id AND n.kind='reply') AS replied "
                            "FROM cases k WHERE k.user_id=? ORDER BY k.updated_at DESC", (g.user["id"],)).fetchall()
    return render_template("my_cases.html", rows=rows)


@bp.route("/support/requests/<int:case_id>", methods=["GET", "POST"])
@login_required
def case_view(case_id):
    db = get_db()
    k = db.execute("SELECT * FROM cases WHERE id=? AND user_id=?", (case_id, g.user["id"])).fetchone()
    if k is None:
        abort(404)
    if request.method == "POST":
        body = request.form.get("body", "").strip()[:4000]
        if len(body) < 2:
            flash("Write your reply first.", "error")
        else:
            now = iso(utcnow())
            db.execute("INSERT INTO case_notes (case_id, created_at, kind, body) VALUES (?,?, 'customer', ?)", (case_id, now, body))
            db.execute("UPDATE cases SET status='open', updated_at=? WHERE id=?", (now, case_id))
            flash("Reply sent.")
        return redirect(url_for("public.case_view", case_id=case_id))
    notes = db.execute("SELECT kind, body, created_at FROM case_notes WHERE case_id=? AND kind!='internal' ORDER BY id",
                       (case_id,)).fetchall()       # internal staff notes are never shown to customers
    return render_template("case_view.html", k=k, notes=notes)


CONTACT_TOPICS = ["My entries or tickets", "A payment", "Withdrawing winnings", "Claiming a prize", "My account",
                  "Responsible play", "Something else"]


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        f = request.form
        name, email = " ".join(f.get("name", "").split())[:80], f.get("email", "").strip()[:120]
        topic, msg = f.get("topic", ""), f.get("message", "").strip()[:4000]
        key = "contact:" + (request.remote_addr or "?")
        errors = {}
        if not name:
            errors["name"] = "Enter your name."
        if "@" not in email or "." not in email.split("@")[-1]:
            errors["email"] = "Enter a valid email address so we can reply."
        if topic not in CONTACT_TOPICS:
            errors["topic"] = "Choose what your message is about."
        if len(msg) < 10:
            errors["message"] = "Tell us a little more (at least 10 characters)."
        if f.get("website"):                       # honeypot: real people never fill this in
            return redirect(url_for("public.contact", sent=1))
        if errors:
            return render_template("contact.html", form=f, errors=errors, topics=CONTACT_TOPICS), 400
        if _too_many(key, limit=3, window=3600):
            flash("You've sent a few messages already — we'll be in touch soon. For anything urgent, email us.", "error")
            return render_template("contact.html", form=f, errors={}, topics=CONTACT_TOPICS), 429
        _fail(key)
        from .control import open_case
        case_id = open_case(name, email, topic, msg, g.user["id"] if g.user else None)
        who = f"{name} <{email}>" + (f" (account #{g.user['id']})" if g.user else "") + f" — case #{case_id}"
        if current_app.config["SUPPORT_EMAIL"]:
            mailer.send(current_app.config["SUPPORT_EMAIL"], f"Contact form: {topic}",
                        f"From: {who}\nTopic: {topic}\n\n{msg}\n\nReply to: {email}")
        tell(email, f"We've got your message [case #{case_id}]", kind="support", key=f"case:{case_id}",
             user_id=g.user["id"] if g.user else None, title=f"Message received — case #{case_id}", body=
                    f"Hi {name.split()[0]},\n\nThanks for getting in touch about \"{topic.lower()}\". We reply within "
                    "1 working day.\n\nYour message:\n" + msg, heading="Message received")
        current_app.logger.warning("Contact form from %s: %s", email, topic)
        return redirect(url_for("public.contact", sent=1))
    return render_template("contact.html", form=form, errors={}, topics=CONTACT_TOPICS, sent=request.args.get("sent"))


@bp.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(current_app.config["UPLOAD_DIR"], name, max_age=86400)


@bp.route("/manifest.webmanifest")
def manifest():
    cfg = current_app.config
    return jsonify({
        "name": cfg["SITE_NAME"], "short_name": cfg["SITE_NAME"], "start_url": "/", "display": "standalone",
        "background_color": "#060406", "theme_color": "#060406",
        "icons": [{"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
                  {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"}],
    })


@bp.route("/sw.js")
def service_worker():
    js = ("self.addEventListener('install',e=>self.skipWaiting());"
          "self.addEventListener('activate',e=>self.clients.claim());"
          "self.addEventListener('fetch',e=>{});")
    return Response(js, mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


# ---------------- accounts ----------------

def _pw_version(db, uid):
    return db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[0][-16:]


def _age(dob):
    t = date.today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


@bp.route("/r/<code>")
def referral(code):
    if get_db().execute("SELECT 1 FROM users WHERE referral_code=?", (code.upper(),)).fetchone():
        session["ref"] = code.upper()
    return redirect(url_for("public.signup") if not g.user else url_for("public.home"))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        f = request.form
        email, name, pw = f.get("email", "").strip().lower(), " ".join(f.get("name", "").split()), f.get("password", "")
        key = "signup:" + (request.remote_addr or "?")
        if _too_many(key, limit=current_app.config["SIGNUP_RATE_LIMIT"], window=3600):
            flash("Too many sign-ups from this connection — please try again in an hour.", "error")
            return render_template("signup.html", form=f, errors={}), 429
        try:
            dob = date.fromisoformat(f.get("dob", ""))
        except ValueError:
            dob = None
        errors = {}
        if not name or len(name.split()) < 2:
            errors["name"] = "Enter your first name and surname."
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            errors["email"] = "Enter a valid email address, like name@example.com."
        elif get_db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            errors["email"] = "There's already an account with this email. Log in, or reset your password if you've forgotten it."
        if len(pw) < 10:
            errors["password"] = "Your password needs at least 10 characters."
        if dob is None:
            errors["dob"] = "Enter your date of birth."
        elif _age(dob) < 18:
            errors["dob"] = "You must be 18 or over to join."
        if not f.get("agree"):
            errors["agree"] = "Tick the box to confirm you're 18+, live in the UK and accept the terms."
        if errors:
            return render_template("signup.html", form=f, errors=errors), 400
        db = get_db()
        ref = db.execute("SELECT id FROM users WHERE referral_code=?", (session.get("ref", ""),)).fetchone()
        cur = db.execute(
            "INSERT INTO users (email, name, password_hash, dob, is_admin, referral_code, referred_by, marketing, created_at, phone) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (email, name, generate_password_hash(pw), dob.isoformat(), 0, secrets.token_hex(4).upper(),
             ref["id"] if ref else None, 1 if f.get("marketing") else 0, iso(utcnow()),
             "".join(ch for ch in f.get("phone", "") if ch.isdigit() or ch == "+")[:20] or None))
        _fail(key)
        basket_keep, promo_keep = session.get("basket", []), session.get("promo")
        session.clear()
        session.permanent = True
        session["uid"] = cur.lastrowid
        session["basket"] = basket_keep          # "your basket is kept" — the basket page promises it
        if promo_keep:
            session["promo"] = promo_keep
        session["pwv"] = _pw_version(db, cur.lastrowid)
        start_session(db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
        new_user = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        for ch, field in (("email", "marketing"), ("sms", "marketing_sms")):
            if f.get(field):
                if ch == "sms":
                    db.execute("UPDATE users SET marketing_sms=1 WHERE id=?", (cur.lastrowid,))
                db.execute("INSERT INTO consent_log (user_id, channel, granted, source, created_at, ip) VALUES (?,?,1,'signup',?,?)",
                           (cur.lastrowid, ch, iso(utcnow()), request.remote_addr))
        if ref:
            from .services import referral_problem
            new_u = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            why = referral_problem(db, ref["id"], new_u)
            db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at, status, reason) VALUES (?,?,?,?,?)",
                       (ref["id"], cur.lastrowid, iso(utcnow()), "not_eligible" if why else "joined", why))
        send_verification(db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
        flash("Welcome! We've emailed you a link to confirm your email address.")
        return redirect(safe_next(request.args.get("next")))
    return render_template("signup.html", form={}, errors={})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        keys = ("ip:" + (request.remote_addr or "?"), "em:" + email)
        if any(_too_many(k) for k in keys):
            flash("Too many attempts — please wait 15 minutes or reset your password.", "error")
            return render_template("login.html"), 429
        u = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not (u and check_password_hash(u["password_hash"], request.form.get("password", ""))):
            for k in keys:
                _fail(k)
            u = None
        if u:
            basket_keep = session.get("basket", [])
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            session["pwv"] = u["password_hash"][-16:]
            session["basket"] = basket_keep
            start_session(u)
            return redirect(safe_next(request.args.get("next")))
        flash("That email and password don't match an account. Check them and try again, or reset your password.", "error")
        return render_template("login.html"), 400
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    end_session()
    session.clear()
    return redirect(url_for("public.home"))


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        db = get_db()
        email = request.form.get("email", "").strip().lower()
        keys = ("reset-ip:" + (request.remote_addr or "?"), "reset-em:" + email)
        limited = _too_many(keys[0], limit=10, window=3600) or _too_many(keys[1], limit=3, window=3600)
        for k in keys:
            _fail(k)
        u = None if limited else db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u:
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), u["id"], iso(utcnow() + timedelta(hours=1))))
            link = current_app.config["SITE_URL"] + url_for("public.reset", token=token)
            mailer.send(u["email"], "Reset your password",
                        f"Hi {u['name'].split()[0]},\n\nSomeone (hopefully you) asked to reset your password. "
                        "The link below works for 1 hour.\n\nIf you didn't ask for this, you can ignore this email.",
                        button=("Reset my password", link), heading="Reset your password")
        flash("If that email has an account, we've sent a reset link.")
        return redirect(url_for("public.login"))
    return render_template("forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    db = get_db()
    h = hashlib.sha256(token.encode()).hexdigest()
    r = db.execute("SELECT * FROM password_resets WHERE token_hash=?", (h,)).fetchone()
    if r is None or parse_iso(r["expires_at"]) < utcnow():
        flash("That reset link has expired — request a new one.", "error")
        return redirect(url_for("public.forgot"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < 10:
            flash("Use a password of at least 10 characters.", "error")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), r["user_id"]))
            db.execute("DELETE FROM password_resets WHERE user_id=?", (r["user_id"],))
            revoke_others(r["user_id"])
            audit(db, "account.password_reset", f"user:{r['user_id']}", "Password reset by email link; all devices signed out",
                  actor=False)
            flash("Password updated — log in with your new password.")
            return redirect(url_for("public.login"))
    return render_template("reset.html")


ACCOUNT_TABS = [("overview", "Overview"), ("entries", "My tickets"), ("wins", "Wins & prizes"),
                ("transactions", "Transactions"), ("wallet", "Wallet"), ("points", "DBX Points"),
                ("safer", "Responsible play"), ("profile", "Profile & security")]
OLD_TABS = {"tickets": "entries", "orders": "transactions", "rewards": "points", "refer": "points",
            "settings": "safer"}


def _attention(db, user, bal):
    """Things this customer should do or know now, most important first. (text, link, link label, level)"""
    uid, out = user["id"], []
    for c in db.execute("SELECT pc.status, c.title FROM prize_claims pc JOIN competitions c ON c.id=pc.competition_id "
                        "WHERE pc.user_id=? AND pc.status IN ('selected','contacted','verification','chosen')", (uid,)):
        out.append((f"You won {c['title']}! " + ("We need to verify a few details — please check your email and reply."
                    if c["status"] in ("contacted", "verification") else "We'll be in touch shortly to arrange your prize."),
                    url_for("public.support_centre", topic="Prize"), "Contact us about my prize", "good"))
    n = db.execute("SELECT COUNT(*) FROM tickets t JOIN competitions c ON c.id=t.competition_id WHERE t.user_id=? "
                   "AND t.status='issued' AND t.revealed_at IS NULL AND c.game_type!=''", (uid,)).fetchone()[0]
    if n:
        w = unplayed(db, uid)
        out.append((f"You have {n} instant win play{'s' if n != 1 else ''} to reveal.", url_for("public.play", slug=w[0]["slug"]),
                    "Reveal now", "good"))
    if not user["email_verified"]:
        out.append(("Confirm your email address to withdraw winnings and claim free plays.", url_for("public.account", tab="profile"),
                    "Resend the link", "warn"))
    for w in db.execute("SELECT * FROM withdrawals WHERE user_id=? AND status IN ('requested','processing') ORDER BY id", (uid,)):
        out.append((f"Your withdrawal of £{w['amount'] / 100:.2f} is {'being processed' if w['status'] == 'processing' else 'requested'}.",
                    url_for("public.withdrawal_detail", wid=w["id"]), "Track it", "info"))
    if bal["cash"] >= MIN_WITHDRAWAL and not any(o[3] == "info" for o in out):
        out.append((f"You have £{bal['cash'] / 100:.2f} of cash winnings you can withdraw.", url_for("public.account", tab="wallet") + "#withdraw",
                    "Withdraw", "info"))
    if user["pending_limit_at"]:
        out.append((f"A spending limit increase you asked for starts {parse_iso(user['pending_limit_at']).astimezone(UK).strftime('%a %d %b, %H:%M')}.",
                    url_for("public.account", tab="safer"), "Review limits", "info"))
    cases = db.execute("SELECT COUNT(*) FROM cases WHERE user_id=? AND status='waiting'", (uid,)).fetchone()[0]
    if cases:
        out.append(("Our support team has replied to you.", url_for("public.my_cases"), "Read the reply", "info"))
    return out


def _entry_groups(db, uid):
    tickets = db.execute(
        "SELECT c.title, c.slug, c.status, c.ends_at, c.winner_ticket_id, c.game_type, c.image, c.max_tickets, c.drawn_at, "
        "c.auto_draw, (SELECT w.number FROM tickets w WHERE w.id=c.winner_ticket_id) AS winning_number, "
        "t.id, t.number, t.revealed_at, t.postal_entry_id, "
        "CASE WHEN c.game_type='' OR t.revealed_at IS NOT NULL THEN ip.title END AS win "
        "FROM tickets t JOIN competitions c ON c.id=t.competition_id LEFT JOIN instant_prizes ip ON ip.ticket_id=t.id "
        "WHERE t.user_id=? AND t.status='issued' ORDER BY c.ends_at DESC, t.number", (uid,)).fetchall()
    groups = {}
    for t in tickets:
        e = groups.setdefault(t["slug"], {"title": t["title"], "slug": t["slug"], "status": t["status"], "image": t["image"],
                                          "ends_at": t["ends_at"], "game": t["game_type"], "tickets": [], "won": False,
                                          "unplayed": 0, "instant_wins": 0, "drawn_at": t["drawn_at"],
                                          "winning_number": t["winning_number"], "auto_draw": t["auto_draw"]})
        e["tickets"].append(t)
        if t["winner_ticket_id"] == t["id"]:
            e["won"] = True
        if t["win"]:
            e["instant_wins"] += 1
        if t["game_type"] and not t["revealed_at"]:
            e["unplayed"] += 1
    out = list(groups.values())
    for e in out:
        e["open"] = e["status"] == "live" and parse_iso(e["ends_at"]) > utcnow()
        e["f"] = {
            "active": e["open"] or bool(e["unplayed"]),                                   # can still enter / plays to reveal
            "upcoming": e["status"] == "live" and not e["open"] and not e["game"],        # closed, draw pending
            "winner": e["won"] or e["instant_wins"] > 0,
            "completed": e["status"] in ("drawn", "cancelled") or (e["game"] and not e["open"] and not e["unplayed"]),
        }
        e["section"] = "active" if (e["f"]["active"] or e["f"]["upcoming"]) else ("won" if e["f"]["winner"] else "previous")
    out.sort(key=lambda e: (not (e["f"]["active"] or e["f"]["upcoming"]), e["ends_at"] if (e["f"]["active"] or e["f"]["upcoming"]) else "",))
    return out


@bp.route("/account")
@login_required
def account():
    db = get_db()
    tab = request.args.get("tab", "overview")
    if tab in OLD_TABS:
        return redirect(url_for("public.account", tab=OLD_TABS[tab]), code=301)
    if tab not in dict(ACCOUNT_TABS):
        return redirect(url_for("public.account"))
    user = db.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
    uid = user["id"]
    bal = balances(db, uid)
    ctx = {"u": user, "tab": tab, "tabs": ACCOUNT_TABS, "bal": bal, "tier": tier_for(user["points_lifetime"]),
           "excluded": is_excluded(user)}
    if tab in ("overview", "entries"):
        groups = _entry_groups(db, uid)
        show = {"won": "winner", "previous": "completed"}.get(request.args.get("show"), request.args.get("show", "active"))
        if show not in ("active", "upcoming", "winner", "completed"):
            show = "active"
        ctx.update(groups=groups, show=show, counts={k: sum(1 for e in groups if e["f"][k])
                                                     for k in ("active", "upcoming", "winner", "completed")},
                   live_groups=[e for e in groups if e["f"]["active"] or e["f"]["upcoming"]], waiting=unplayed(db, uid))
        ctx["upcoming"] = sorted([e for e in groups if (e["f"]["active"] or e["f"]["upcoming"]) and not e["game"]],
                                 key=lambda e: e["ends_at"])[:5]
    if tab == "overview":
        ctx["recent_results"] = [e for e in groups if e["status"] == "drawn" and e["drawn_at"]
                                 and parse_iso(e["drawn_at"]) > utcnow() - timedelta(days=30)][:5]
        ctx["attention"] = _attention(db, user, bal)
        ctx["latest_notes"] = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 4", (uid,)).fetchall()
    if tab in ("overview", "wins"):
        wins = db.execute(
            "SELECT ip.title, ip.value, ip.prize_type, ip.credit_amount, ip.fulfilled, ip.won_at, t.number, c.title AS comp, c.slug "
            "FROM instant_prizes ip JOIN tickets t ON t.id=ip.ticket_id JOIN competitions c ON c.id=ip.competition_id "
            "WHERE t.user_id=? AND (c.game_type='' OR t.revealed_at IS NOT NULL) ORDER BY ip.won_at DESC LIMIT 100",
            (uid,)).fetchall()
        draws_won = db.execute("SELECT c.title, c.slug, c.drawn_at, c.prize_value, c.cash_alternative, t.number FROM competitions c "
                               "JOIN tickets t ON t.id=c.winner_ticket_id WHERE t.user_id=? ORDER BY c.drawn_at DESC",
                               (uid,)).fetchall()
        ctx.update(wins=wins, draws_won=draws_won, total_won=sum(w["value"] for w in wins) + sum(d["prize_value"] or 0 for d in draws_won))
    if tab in ("overview", "transactions"):
        ctx["ledger"] = db.execute("SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                   (uid, 6 if tab == "overview" else 200)).fetchall()
        ctx["orders"] = db.execute(
            "SELECT k.*, (SELECT COUNT(*) FROM orders o WHERE o.checkout_id=k.id) AS lines, "
            "(SELECT SUM(o.quantity) FROM orders o WHERE o.checkout_id=k.id) AS entries, "
            "(SELECT GROUP_CONCAT(c.title, ', ') FROM orders o JOIN competitions c ON c.id=o.competition_id "
            " WHERE o.checkout_id=k.id) AS titles FROM checkouts k "
            "WHERE k.user_id=? AND k.status IN ('paid','credit_refused','needs_refund','refunded') ORDER BY k.id DESC LIMIT ?",
            (uid, 3 if tab == "overview" else 100)).fetchall()
    if tab == "wallet":
        expire_stale_deposits(db, uid)
        ctx.update(deposits=db.execute("SELECT * FROM deposits WHERE user_id=? AND status!='expired' ORDER BY id DESC LIMIT 20",
                                       (uid,)).fetchall(),
                   deposit_room=deposit_room(db, user), min_deposit=MIN_DEPOSIT, max_deposit=MAX_DEPOSIT,
                   refundable=sum(t for _, t in refundable_deposits(db, uid)),
                   withdrawals=db.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)).fetchall(),
                   min_withdrawal=MIN_WITHDRAWAL)
    if tab == "points":
        ctx.update(tiers=TIERS, redeem_block=REDEEM_BLOCK,
                   referred=db.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,)).fetchone()[0],
                   points_rows=db.execute("SELECT * FROM points_ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,)).fetchall(),
                   referrals=db.execute("SELECT r.*, u.name FROM referrals r JOIN users u ON u.id=r.referred_id WHERE r.referrer_id=? "
                                        "ORDER BY r.id DESC", (uid,)).fetchall(), public_name=public_name)
    if tab in ("overview", "entries"):
        ctx["saved"] = [card_data(db, c) for c in db.execute(
            "SELECT c.* FROM watchlist w JOIN competitions c ON c.id=w.competition_id WHERE w.user_id=? ORDER BY c.ends_at",
            (uid,)).fetchall()]
        ctx["tq"] = request.args.get("tq", "").strip()[:40]
    if tab == "profile":
        ctx["sessions"] = db.execute("SELECT * FROM user_sessions WHERE user_id=? AND revoked_at IS NULL ORDER BY last_seen DESC",
                                     (uid,)).fetchall()
        ctx["this_sid"] = session.get("sid")
        ctx["device_name"] = device_name
    if tab == "safer":
        ctx.update(limits=effective_limits(user), spent=spend_summary(db, uid))
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()   # limits may have just come into force
        ctx["u"] = user
    return render_template("account.html", **ctx)


@bp.route("/account/orders/<int:cid>")
@login_required
def order_detail(cid):
    c = _own_checkout(cid)
    if c["status"] == "pending":
        return redirect(url_for("public.checkout_done", cid=cid))
    if c["status"] not in ("paid", "credit_refused", "needs_refund", "refunded"):
        abort(404)
    return render_template("order.html", c=c, lines=checkout_summary(get_db(), cid))


@bp.route("/account/limits", methods=["POST"])
@login_required
def set_limits():
    cap = current_app.config["MAX_MONTHLY_LIMIT"]
    cur = effective_limits(g.user)
    new = {}
    for k in ("daily", "weekly", "monthly"):
        raw = request.form.get(k, "").strip()
        if raw == "" and k != "monthly":
            new[k] = None
            continue
        try:
            v = to_pence(raw)
        except ValueError:
            flash(f"Enter a number for your {k} limit.", "error")
            return redirect(url_for("public.account", tab="safer"))
        if v < 0 or v > cap:
            flash(f"Limits must be between £0 and £{cap/100:.0f}.", "error")
            return redirect(url_for("public.account", tab="safer"))
        new[k] = v
    sets, pend, raised = [], {}, False
    for k, col, pcol in (("daily", "daily_limit", "pending_daily"), ("weekly", "weekly_limit", "pending_weekly"),
                         ("monthly", "monthly_limit", "pending_limit")):
        old, nv = cur[k], new[k]
        loosen = (old is not None and nv is None) or (old is not None and nv is not None and nv > old)
        if loosen:
            pend[pcol] = -1 if nv is None else nv
            raised = True
        elif nv != old:
            sets.append((col, nv))
    with write_txn() as db:
        u = db.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
        for col, v in sets:
            db.execute(f"UPDATE users SET {col}=? WHERE id=?", (v, g.user["id"]))
        new_pending = (pend.get("pending_limit"), pend.get("pending_daily"), pend.get("pending_weekly"))
        old_pending = (u["pending_limit"], u["pending_daily"], u["pending_weekly"])
        if not raised:
            # Anything waiting to go up is cancelled: the limits just submitted are the ones that apply.
            db.execute("UPDATE users SET pending_limit=NULL, pending_daily=NULL, pending_weekly=NULL, pending_limit_at=NULL "
                       "WHERE id=?", (g.user["id"],))
        elif new_pending != old_pending or not u["pending_limit_at"]:
            db.execute("UPDATE users SET pending_limit=?, pending_daily=?, pending_weekly=?, pending_limit_at=? WHERE id=?",
                       (*new_pending, iso(utcnow() + timedelta(hours=72)), g.user["id"]))
        audit(db, "safer.limits", f"user:{g.user['id']}", f"Requested {new}; applied now {dict(sets)}; "
              f"waiting 72h {pend or 'nothing'}")
    if raised:
        flash("Lower limits apply now. Any increase takes 72 hours to start.")
    else:
        flash("Limits updated. They apply straight away.")
    return redirect(url_for("public.account", tab="safer"))


@bp.route("/account/exclude", methods=["POST"])
@login_required
def self_exclude():
    days = {"1": 1, "7": 7, "30": 30, "90": 90, "180": 180, "365": 365}.get(request.form.get("days"))
    if not days:
        abort(400)
    start_break(g.user, days)
    session["basket"] = []
    session.pop("held", None)
    flash(f"You're on a break for {days} day{'s' if days > 1 else ''}. You won't be able to buy tickets until then.")
    return redirect(url_for("public.account", tab="safer"))


@bp.route("/account/withdraw", methods=["POST"])
@login_required
def withdraw():
    """Two steps: 'review' checks everything and shows the details back; 'confirm' makes the request."""
    back = url_for("public.account", tab="wallet") + "#withdraw"
    try:
        amount = to_pence(request.form.get("amount", ""))
    except ValueError:
        flash("Enter the amount you'd like to withdraw, e.g. 25.", "error")
        return redirect(back)
    try:
        if request.form.get("step") != "confirm":
            vals = check_withdrawal(g.user, amount, request.form)
            return render_template("withdraw_review.html", amount=amount, v=vals)
        wid = request_withdrawal(g.user, amount, request.form)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(back)
    if current_app.config["SUPPORT_EMAIL"]:
        mailer.send(current_app.config["SUPPORT_EMAIL"], "Withdrawal request",
                    f"{g.user['name']} ({g.user['email']}) requested £{amount/100:.2f}. Pay it from Admin → Payouts.")
    tell(g.user["email"], "We've received your withdrawal request", kind="withdrawal", key=f"withdraw-req:{wid}",
         user_id=g.user["id"], link=url_for("public.withdrawal_detail", wid=wid), body=
                f"Hi {g.user['name'].split()[0]},\n\nWe've received your request to withdraw £{amount/100:.2f}. "
                "We aim to pay within 24 hours and we'll email you as soon as it's been sent.",
                heading="Withdrawal requested", highlight=f"£{amount/100:.2f}")
    return redirect(url_for("public.withdrawal_detail", wid=wid))


@bp.route("/account/withdrawals/<int:wid>")
@login_required
def withdrawal_detail(wid):
    w = get_db().execute("SELECT * FROM withdrawals WHERE id=? AND user_id=?", (wid, g.user["id"])).fetchone()
    if w is None:
        abort(404)
    return render_template("withdrawal.html", w=w)


@bp.route("/account/profile", methods=["POST"])
@login_required
def profile():
    db = get_db()
    f = request.form
    if f.get("new_password"):
        if not check_password_hash(g.user["password_hash"], f.get("current_password", "")):
            flash("Your current password is wrong.", "error")
            return redirect(url_for("public.account", tab="profile"))
        if len(f["new_password"]) < 10:
            flash("Use a password of at least 10 characters.", "error")
            return redirect(url_for("public.account", tab="profile"))
        new_hash = generate_password_hash(f["new_password"])
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, g.user["id"]))
        session["pwv"] = new_hash[-16:]          # this device stays logged in; every other one is signed out
        revoke_others(g.user["id"], session.get("sid"))
        audit(db, "account.password_changed", f"user:{g.user['id']}", "Changed password from account settings")
        tell(g.user["email"], "Your password was changed", kind="security", key=f"pw:{g.user['id']}:{new_hash[-12:]}",
             user_id=g.user["id"], link=url_for("public.account", tab="profile"),
             body="Your password was just changed and every other device was signed out. If this wasn't you, reset your "
                  "password straight away and contact us.", heading="Password changed")
    phone = "".join(ch for ch in f.get("phone", "") if ch.isdigit() or ch == "+")[:20] or None
    db.execute("UPDATE users SET phone=? WHERE id=?", (phone, g.user["id"]))
    flash("Saved.")
    return redirect(url_for("public.account", tab="profile"))


@bp.route("/account/tickets/<int:tid>")
@login_required
def ticket_detail(tid):
    """One ticket, everything about it — the record customers use instead of searching their email."""
    db = get_db()
    t = db.execute(
        "SELECT t.*, c.title, c.slug, c.status AS cstatus, c.ends_at, c.drawn_at, c.game_type, c.auto_draw, c.winner_ticket_id, "
        "c.image, o.checkout_id, (SELECT w.number FROM tickets w WHERE w.id=c.winner_ticket_id) AS winning_number, "
        "ip.title AS prize, ip.prize_type, ip.fulfilled FROM tickets t JOIN competitions c ON c.id=t.competition_id "
        "LEFT JOIN orders o ON o.id=t.order_id LEFT JOIN instant_prizes ip ON ip.ticket_id=t.id "
        "WHERE t.id=? AND t.user_id=? AND t.status='issued'", (tid, g.user["id"])).fetchone()
    if t is None:
        abort(404)
    claim = db.execute("SELECT status FROM prize_claims WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    from .services import CLAIM_NAMES
    return render_template("ticket.html", t=t, claim=claim, claim_names=CLAIM_NAMES,
                           revealed=not t["game_type"] or bool(t["revealed_at"]))


@bp.route("/account/notifications/<int:nid>/open")
@login_required
def notification_open(nid):
    db = get_db()
    n = db.execute("SELECT * FROM notifications WHERE id=? AND user_id=?", (nid, g.user["id"])).fetchone()
    if n is None:
        abort(404)
    db.execute("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=?", (iso(utcnow()), nid))
    return redirect(safe_next(n["link"], url_for("public.notifications")))


@bp.route("/account/notifications/read", methods=["POST"])
@login_required
def notifications_read():
    db = get_db()
    nid = request.form.get("nid", type=int)
    if nid:
        db.execute("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=? AND user_id=?", (iso(utcnow()), nid, g.user["id"]))
    else:
        db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (iso(utcnow()), g.user["id"]))
        flash("All notifications marked as read.")
    return redirect(url_for("public.notifications"))


@bp.route("/account/notifications")
@login_required
def notifications():
    db = get_db()
    show = request.args.get("show", "all")
    sql = "SELECT * FROM notifications WHERE user_id=?" + (" AND read_at IS NULL" if show == "unread" else "")
    rows = db.execute(sql + " ORDER BY id DESC LIMIT 100", (g.user["id"],)).fetchall()
    return render_template("notifications.html", rows=rows, show=show)


@bp.route("/account/sessions/revoke", methods=["POST"])
@login_required
def revoke_session():
    sid = request.form.get("sid")
    if sid == "others":
        n = revoke_others(g.user["id"], session.get("sid"))
        flash(f"Signed out of {n} other device{'s' if n != 1 else ''}.")
    elif sid and sid != session.get("sid"):
        get_db().execute("UPDATE user_sessions SET revoked_at=? WHERE sid=? AND user_id=?", (iso(utcnow()), sid, g.user["id"]))
        flash("Signed out of that device.")
    audit(get_db(), "account.sessions_revoked", f"user:{g.user['id']}", "Signed out other device(s)")
    return redirect(url_for("public.account", tab="profile") + "#devices")


def set_consent(db, user, channel, granted, source):
    col = {"email": "marketing", "sms": "marketing_sms", "reminders": "reminder_emails"}[channel]
    if bool(user[col]) == bool(granted):
        return False
    db.execute(f"UPDATE users SET {col}=? WHERE id=?", (1 if granted else 0, user["id"]))
    db.execute("INSERT INTO consent_log (user_id, channel, granted, source, created_at, ip) VALUES (?,?,?,?,?,?)",
               (user["id"], channel, 1 if granted else 0, source, iso(utcnow()), request.remote_addr))
    return True


@bp.route("/account/preferences", methods=["POST"])
@login_required
def preferences():
    db = get_db()
    changed = [ch for ch, field in (("email", "marketing_email"), ("sms", "marketing_sms"), ("reminders", "reminder_emails"))
               if set_consent(db, g.user, ch, request.form.get(field) == "1", "settings")]
    if request.form.get("marketing_sms") == "1" and not g.user["phone"]:
        flash("Add your mobile number above so we can text you.", "error")
    flash("Communication preferences saved." if changed else "No changes.")
    return redirect(url_for("public.account", tab="profile") + "#comms")


def _unsub_signer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="unsubscribe")


def unsubscribe_link(user_id):
    return current_app.config["SITE_URL"] + url_for("public.unsubscribe", token=_unsub_signer().dumps(user_id))


@bp.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    """One click from any marketing or reminder email, no log-in needed."""
    try:
        uid = _unsub_signer().loads(token, max_age=365 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        abort(404)
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u is None:
        abort(404)
    done = False
    if request.method == "POST":
        set_consent(db, u, "email", False, "unsubscribe link")
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        set_consent(db, u, "sms", False, "unsubscribe link")
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if request.form.get("reminders"):
            set_consent(db, u, "reminders", False, "unsubscribe link")
        done = True
    return render_template("unsubscribe.html", u=u, done=done)


@bp.route("/verify/<token>")
def verify_email(token):
    try:
        data = _signer().loads(token, max_age=3 * 24 * 3600)
    except SignatureExpired:
        flash("That link has expired — log in and resend it from your account.", "error")
        return redirect(url_for("public.login"))
    except BadSignature:
        abort(404)
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=? AND email=?", (data["u"], data["e"])).fetchone()
    if u is None:
        abort(404)
    db.execute("UPDATE users SET email_verified=1 WHERE id=?", (u["id"],))
    flash("Email confirmed ✔ You can now withdraw winnings and claim free plays.")
    return redirect(url_for("public.account"))


@bp.route("/account/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    key = "verify:" + str(g.user["id"])
    if _too_many(key, limit=3, window=3600):
        flash("We've already sent a few — please check your spam folder.", "error")
    else:
        _fail(key)
        send_verification(g.user)
        flash(f"Sent — check {g.user['email']} (and your spam folder).")
    return redirect(request.referrer or url_for("public.account"))


@bp.route("/account/redeem", methods=["POST"])
@login_required
def redeem():
    try:
        blocks = int(request.form.get("blocks", "0"))
        redeem_points(g.user["id"], blocks)
    except (ValueError, PurchaseError) as e:
        flash(str(e) if isinstance(e, PurchaseError) else "Choose an amount.", "error")
    else:
        flash(f"Redeemed {blocks * REDEEM_BLOCK} points for £{blocks} site credit.")
    return redirect(url_for("public.account", tab="points"))


@bp.route("/healthz")
def health():
    """For an external uptime monitor: 200 when the app and database answer, 503 otherwise."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "time": iso(utcnow())})
    except Exception:
        return jsonify({"ok": False}), 503


@bp.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin/\nDisallow: /account\nDisallow: /basket\nDisallow: /checkout/\n"
                    "Disallow: /play/\nDisallow: /*/entries\nDisallow: /forgot\nDisallow: /reset/\nDisallow: /verify/\nDisallow: /r/\n"
                    f"Sitemap: {current_app.config['SITE_URL']}/sitemap.xml\n", mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap():
    """Live competitions, plus finished draws for 180 days (people search for results); cancelled
    competitions and closed games drop out."""
    base = current_app.config["SITE_URL"]
    urls = [(base + url_for(e), None) for e in ("public.home", "public.competitions", "public.instant_wins",
                                                "public.winners", "public.results", "public.how_it_works", "public.contact")]
    urls += [(base + url_for("public.page", page=p), None) for p in PAGES]
    cutoff = iso(utcnow() - timedelta(days=180))
    for r in get_db().execute(
            "SELECT slug, COALESCE(drawn_at, created_at) AS mod FROM competitions WHERE free_daily=0 AND "
            "(status='live' OR (status='drawn' AND game_type='' AND drawn_at > ?))", ("2000-01-01",)):
        urls.append((base + url_for("public.competition", slug=r["slug"]), (r["mod"] or "")[:10]))
    body = "".join(f"<url><loc>{u}</loc>{f'<lastmod>{m}</lastmod>' if m else ''}</url>" for u, m in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                    mimetype="application/xml")
