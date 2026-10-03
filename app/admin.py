import csv
import io
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from . import UK, mailer, payments
from .notify import notify, tell
from .perms import can, require, ROLES
from .db import get_db, iso, utcnow, write_txn
from .services import (to_pence, receive_postal, process_postal, postal_problem, POSTAL_REJECT_REASONS, redraw, update_claim,
                       CLAIM_STATUSES, CLAIM_NAMES, lifecycle_stage, LIFECYCLE_NAMES, latest_snapshot, audit, cancel_competition, mark_withdrawal_processing, CATEGORIES, CATEGORY_NAMES, balances, prize_kind, refund_competition, GAME_NAMES, GAME_TYPES, game_info, PurchaseError, add_credit, add_instant_prizes, add_postal_entry,
                       balance, comp_state, get_setting, instant_board, new_seed, public_name,
                       remove_instant_prize_group, run_draw, set_setting, settle_withdrawal, site_stats, sold_count,
                       taken_count, winner_details, delete_competition, paid_unrefunded, start_fresh, publish_problem)

bp = Blueprint("admin", __name__, url_prefix="/admin")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_owner(user):
    return can(user, "money")


@bp.app_context_processor
def _admin_ctx():
    from .perms import ROLES, role_of
    u = g.get("user")
    return {"is_owner": is_owner(u), "can": lambda perm: can(u, perm), "admin_role_name": ROLES[role_of(u)][0] if role_of(u) else ""}


def _comp(cid):
    c = get_db().execute("SELECT * FROM competitions WHERE id=?", (cid,)).fetchone()
    if c is None:
        abort(404)
    return c


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "competition"


def money(v, field):
    try:
        return to_pence(v or 0)
    except ValueError:
        raise ValueError(f"{field} must be a number.")


def discord(text):
    hook = current_app.config["DISCORD_WEBHOOK_URL"]
    if hook:
        try:
            requests.post(hook, json={"content": text}, timeout=10)
        except requests.RequestException:
            current_app.logger.exception("Discord post failed")


# ---------------- dashboard ----------------

@bp.route("/competitions")
@require("comps.view")
def dashboard():
    db = get_db()
    rows = db.execute("SELECT * FROM competitions ORDER BY CASE status WHEN 'live' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, "
                      "CASE WHEN status IN ('live','draft') THEN ends_at END ASC, ends_at DESC").fetchall()
    comps = []
    for c in rows:
        sold = sold_count(db, c["id"])
        revenue = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE competition_id=? AND status='paid'",
                             (c["id"],)).fetchone()[0]
        comps.append({"c": c, "sold": sold, "state": comp_state(c, sold), "revenue": revenue})
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "revenue": db.execute("SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE status='paid'").fetchone()[0]
                   + db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND checkout_id IS NULL").fetchone()[0],
        "today": db.execute("SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE status='paid' AND paid_at>=?",
                            (iso(today),)).fetchone()[0],
        "tickets": db.execute("SELECT COUNT(*) FROM tickets WHERE status='issued'").fetchone()[0],
        "credit": db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger").fetchone()[0],
    }
    todo = {
        "withdrawals": db.execute("SELECT COUNT(*) FROM withdrawals WHERE status IN ('requested','processing')").fetchone()[0],
        "refunds": db.execute("SELECT COUNT(*) FROM checkouts WHERE status='needs_refund'").fetchone()[0]
                   + db.execute("SELECT COUNT(*) FROM deposits WHERE status='needs_refund'").fetchone()[0],
        "prizes": db.execute("SELECT COUNT(*) FROM instant_prizes WHERE ticket_id IS NOT NULL AND fulfilled=0").fetchone()[0],
        "draws": sum(1 for x in comps if x["state"] == "ended" and not x["c"]["game_type"]),
    }
    cfg = current_app.config
    setup = [(ok, text) for ok, text in [
        (bool(cfg["POSTAL_ADDRESS"]), "POSTAL_ADDRESS — the free postal entry address (legally required)"),
        (bool(cfg["COMPANY_DETAILS"]), "COMPANY_DETAILS — your trading name / company number for the footer and terms"),
        (bool(cfg["SUPPORT_EMAIL"]), "SUPPORT_EMAIL — where contact-form messages and alerts go"),
        (bool(cfg["SMTP_HOST"]), "SMTP settings — so emails actually send"),
        (bool(cfg["STRIPE_SECRET_KEY"]) and bool(cfg["STRIPE_WEBHOOK_SECRET"]), "Stripe keys — card payments"),
        (not cfg["DEMO_PAYMENTS"] or bool(cfg["STRIPE_SECRET_KEY"]), "DEMO_PAYMENTS=0 before taking real entries"),
    ]]
    live = [x for x in comps if x["state"] == "live"]
    prices = {x["c"]["ticket_price"] for x in live if x["c"]["game_type"] and not x["c"]["free_daily"]}
    launch = [
        (sum(1 for x in live if not x["c"]["game_type"]) >= 3, "At least 3 live prize draws", url_for("admin.new_competition", kind="draw")),
        (any(x["c"]["featured"] and not x["c"]["game_type"] for x in live), "A featured prize draw for the home page", None),
        (any(x["c"]["cash_alternative"] for x in live), "A cash alternative on at least one draw", None),
        (any(not x["c"]["game_type"] and x["c"]["image"] for x in live) and all(x["c"]["image"] for x in live if not x["c"]["game_type"]),
         "A photo on every prize draw", None),
        (any(db.execute("SELECT 1 FROM instant_prizes WHERE competition_id=? LIMIT 1", (x["c"]["id"],)).fetchone()
             for x in live if not x["c"]["game_type"]), "Instant prizes on at least one draw", None),
        (len(prices) >= 3, "Instant win games at 3 or more price points (e.g. 10p, 50p, £1)", url_for("admin.new_competition", kind="game")),
        (any(x["c"]["free_daily"] for x in live), "A daily free game (brings people back every day)", url_for("admin.new_competition", kind="free")),
        (db.execute("SELECT 1 FROM promo_codes WHERE active=1 LIMIT 1").fetchone() is not None, "A welcome promo code", url_for("admin.promos")),
    ]
    return render_template("admin/dashboard.html", comps=comps, stats=stats, todo=todo, site=site_stats(db),
                           setup=setup, setup_missing=sum(1 for ok, _ in setup if not ok),
                           launch=launch, launch_missing=sum(1 for ok, _, _ in launch if not ok),
                           game_names=GAME_NAMES,
                           draft_games=sum(1 for x in comps if x["c"]["game_type"] and x["c"]["status"] == "draft"))


# ---------------- competitions ----------------

def _save_image(file):
    """Check it's really an image, shrink it to max 1600px and save as WebP (much smaller, loads fast)."""
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in IMAGE_EXT:
        raise ValueError("Image must be JPG, PNG, WEBP or GIF.")
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream))
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ValueError("That file isn't a valid image.")
    if img.width * img.height > 60_000_000:
        raise ValueError("That image is too large.")
    img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    img.thumbnail((1600, 1600))
    name = secrets.token_hex(8) + ".webp"
    img.save(os.path.join(current_app.config["UPLOAD_DIR"], name), "WEBP", quality=84, method=4)
    small = img.copy()
    small.thumbnail((640, 640))       # cards, basket and lists use this — a fraction of the download
    small.save(os.path.join(current_app.config["UPLOAD_DIR"], name.replace(".webp", "-sm.webp")), "WEBP", quality=80, method=4)
    return name


def _parse_form(f, locked):
    data = {k: f.get(k, "").strip() for k in
            ("title", "description", "cash_alternative", "question", "answer_a", "answer_b", "answer_c", "correct",
             "category", "live_url", "discount_tiers")}
    if not data["title"]:
        raise ValueError("Title is required.")
    if data["category"] not in CATEGORY_NAMES:
        data["category"] = "other"
    kind = f.get("kind") if f.get("kind") in ("draw", "game", "free") else \
        ("game" if f.get("game_type", "") in GAME_NAMES else "draw")
    data["featured"] = 1 if f.get("featured") and kind != "free" else 0
    data["prize_value"] = money(f.get("prize_value"), "Prize value")
    data["game_type"] = f.get("game_type", "") if f.get("game_type", "") in GAME_NAMES else ""
    if not locked and kind in ("game", "free") and not data["game_type"]:
        raise ValueError("Choose the game style: scratch card, wheel or mystery box.")
    if kind == "draw":
        data["game_type"] = ""
    data["free_daily"] = 1 if kind == "free" else 0
    data["auto_draw"] = 1 if f.get("auto_draw") or kind != "draw" else 0
    try:
        local = datetime.strptime(f.get("ends_at", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=UK)
    except ValueError:
        raise ValueError("Enter a valid closing date and time.")
    data["ends_at"] = iso(local)
    if f.get("starts_at", "").strip():
        try:
            start = datetime.strptime(f["starts_at"], "%Y-%m-%dT%H:%M").replace(tzinfo=UK)
        except ValueError:
            raise ValueError("Enter a valid go-live date and time, or leave it blank.")
        if start >= local:
            raise ValueError("The go-live time must be before the closing time.")
        data["starts_at"] = iso(start)
    else:
        data["starts_at"] = None
    data["question_mode"] = f.get("question_mode") if f.get("question_mode") in ("multiple_choice", "none") else "multiple_choice"
    if (kind == "free" or data["question_mode"] == "none") and not locked:
        data["question"] = data["question"] or ("Free game — no question" if kind == "free" else "No question — free draw")
        for k in ("answer_a", "answer_b", "answer_c"):
            data[k] = data[k] or "-"
        data["correct"] = data["correct"] if data["correct"] in ("a", "b", "c") else "a"
    if data["live_url"] and not data["live_url"].startswith("https://"):
        raise ValueError("Live draw link must start with https://")
    if not locked:
        try:
            data["ticket_price"] = 0 if kind == "free" else to_pence(f.get("ticket_price", ""))
            data["max_tickets"] = int(f.get("max_tickets", ""))
            data["max_per_user"] = int(f.get("max_per_user") or 0) if kind != "free" else 366
        except ValueError:
            raise ValueError("Price, max tickets and per-person limit must be numbers.")
        if (kind != "free" and data["ticket_price"] < 1) or data["max_tickets"] < 1 or data["max_per_user"] < 1:
            raise ValueError("Price must be at least 1p and limits at least 1.")
        if data["max_tickets"] > 1000000:
            raise ValueError("Max 1,000,000 tickets.")
        if not all(data[k] for k in ("question", "answer_a", "answer_b", "answer_c")) or data["correct"] not in "abc":
            raise ValueError("Fill in the question, all three answers and pick the correct one.")
    else:
        for k in ("question", "answer_a", "answer_b", "answer_c", "correct", "discount_tiers", "game_type", "free_daily", "question_mode"):
            data.pop(k, None)
    return data


PRIZE_TYPES = ("cash", "credit", "physical")


def parse_prize_table(text):
    """One prize per line: quantity, name, value in £, type (cash / credit / physical).
    e.g.  "1, £100 Cash, 100, cash"   or   "5, AirPods Pro, 229, physical"."""
    out = []
    for i, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            raise ValueError(f"Prize line {i}: use  quantity, name, value, type  — e.g. 5, £10 Cash, 10, cash")
        try:
            qty = int(parts[0].lower().rstrip("x").strip())
        except ValueError:
            raise ValueError(f"Prize line {i}: the quantity '{parts[0]}' isn't a whole number.")
        name = parts[1]
        value = money(parts[2].lstrip("£"), f"Prize line {i} value")
        kind = (parts[3].lower() if len(parts) > 3 else prize_kind_for(name))
        kind = {"site credit": "credit", "prize": "physical", "item": "physical"}.get(kind, kind)
        if kind not in PRIZE_TYPES:
            raise ValueError(f"Prize line {i}: type must be cash, credit or physical.")
        if qty < 1 or not name:
            raise ValueError(f"Prize line {i}: needs a quantity of at least 1 and a name.")
        out.append((qty, name, value, kind))
    return out


def _add_prize_table(cid, rows):
    for qty, name, value, kind in rows:
        add_instant_prizes(cid, name, value, value if kind in ("cash", "credit") else 0, qty, kind)


def _form_from(c):
    form = dict(c)
    form["ticket_price"] = f"{c['ticket_price']/100:.2f}"
    form["prize_value"] = f"{c['prize_value']/100:.0f}" if c["prize_value"] else ""
    form["ends_at"] = _local(c["ends_at"])
    form["starts_at"] = _local(c["starts_at"]) if c["starts_at"] else ""
    form["kind"] = "free" if c["free_daily"] else ("game" if c["game_type"] else "draw")
    return form


def _local(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(UK).strftime("%Y-%m-%dT%H:%M")


@bp.route("/competitions/new", methods=["GET", "POST"])
@require("comps")
def new_competition():
    copy = request.args.get("copy", type=int)
    if request.method == "POST":
        try:
            data = _parse_form(request.form, locked=False)
            prizes = parse_prize_table(request.form.get("prize_table"))
            data["image"] = _save_image(request.files.get("image")) or request.form.get("keep_image") or None
        except ValueError as e:
            flash(str(e), "error")
            return render_template("admin/edit.html", default_question_mode=get_setting("default_question_mode", "multiple_choice"), c=None, form=request.form, locked=False, categories=CATEGORIES, game_types=GAME_TYPES)
        db = get_db()
        slug = base = slugify(data["title"])
        n = 2
        while db.execute("SELECT 1 FROM competitions WHERE slug=?", (slug,)).fetchone():
            slug, n = f"{base}-{n}", n + 1
        seed, seed_hash = new_seed()
        cols = list(data) + ["slug", "seed", "seed_hash", "created_at"]
        vals = list(data.values()) + [slug, seed, seed_hash, iso(utcnow())]
        cur = db.execute(f"INSERT INTO competitions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        from .services import save_comp_terms
        save_comp_terms(db, cur.lastrowid, request.form.get("comp_terms", ""), g.user)
        try:
            _add_prize_table(cur.lastrowid, prizes)
        except PurchaseError as e:
            flash(f"Saved as a draft, but the prizes weren't added: {e}", "error")
            return redirect(url_for("admin.entries", cid=cur.lastrowid) + "#instant")
        flash("Saved as a draft." + (f" {sum(p[0] for p in prizes)} prizes added and sealed." if prizes else "") +
              " Check it over, then press Publish (or Schedule).")
        return redirect(url_for("admin.entries", cid=cur.lastrowid))
    form = {"kind": request.args.get("kind", "draw")}
    if form["kind"] == "free":
        form.update(max_tickets=20000, title="Daily Free Spin", game_type="spin",
                    description="One free play every day for every verified member — no purchase needed. Win real cash or site credit.")
    elif form["kind"] == "game":
        form.update(game_type="scratch", max_per_user=100, category="cash")
    if copy:
        form = _form_from(_comp(copy))
        form["title"] = form["title"] + " (copy)"
        form["keep_image"] = form.get("image")
    return render_template("admin/edit.html", default_question_mode=get_setting("default_question_mode", "multiple_choice"), c=None, form=form, locked=False, categories=CATEGORIES, game_types=GAME_TYPES)


@bp.route("/competitions/<int:cid>/edit", methods=["GET", "POST"])
@require("comps")
def edit_competition(cid):
    c = _comp(cid)
    if c["status"] in ("drawn", "cancelled"):
        flash("Finished competitions can't be edited.", "error")
        return redirect(url_for("admin.entries", cid=cid))
    db = get_db()
    locked = taken_count(db, cid) > 0 or db.execute(
        "SELECT 1 FROM postal_entries WHERE competition_id=? LIMIT 1", (cid,)).fetchone() is not None
    if request.method == "POST":
        try:
            data = _parse_form(request.form, locked)
            img = _save_image(request.files.get("image"))
            if img:
                data["image"] = img
        except ValueError as e:
            flash(str(e), "error")
            return render_template("admin/edit.html", default_question_mode=get_setting("default_question_mode", "multiple_choice"), c=c, form=request.form, locked=locked, categories=CATEGORIES, game_types=GAME_TYPES)
        if c["status"] == "live" and data["ends_at"] != c["ends_at"] and data["ends_at"] <= iso(utcnow()):
            flash("A live competition's closing time can't be moved into the past.", "error")
            return render_template("admin/edit.html", default_question_mode=get_setting("default_question_mode", "multiple_choice"), c=c, form=request.form, locked=locked, categories=CATEGORIES, game_types=GAME_TYPES)
        changed = {k: (c[k], v) for k, v in data.items() if k in c.keys() and c[k] != v and k != "description"}
        db.execute(f"UPDATE competitions SET {','.join(k + '=?' for k in data)} WHERE id=?", [*data.values(), cid])
        from .services import save_comp_terms
        save_comp_terms(db, cid, request.form.get("comp_terms", ""), g.user)
        if changed:
            audit(db, "comp.edit", f"comp:{cid}", "; ".join(f"{k}: {o!r} → {n!r}" for k, (o, n) in changed.items()))
        flash("Saved.")
        return redirect(url_for("admin.entries", cid=cid))
    from .services import comp_terms_current
    form = _form_from(c)
    ct = comp_terms_current(db, cid)
    form["comp_terms"] = ct["body"] if ct else ""
    return render_template("admin/edit.html", default_question_mode=get_setting("default_question_mode", "multiple_choice"), c=c, form=form, locked=locked, categories=CATEGORIES, game_types=GAME_TYPES, terms_version=ct["version"] if ct else 0)


@bp.route("/competitions/<int:cid>/status", methods=["POST"])
@require("comps")
def set_status(cid):
    c = _comp(cid)
    action = request.form.get("action")
    db = get_db()
    if action in ("publish", "schedule") and c["status"] == "draft":
        problem = publish_problem(db, c)
        if problem:
            flash(problem, "error")
            return redirect(url_for("admin.entries", cid=cid))
        if action == "schedule" and c["starts_at"] and c["starts_at"] > iso(utcnow()):
            db.execute("UPDATE competitions SET scheduled=1 WHERE id=?", (cid,))
            audit(db, "comp.schedule", f"comp:{cid}", f"Scheduled for {c['starts_at']}")
            flash(f"Scheduled — it goes live automatically on {_local(c['starts_at']).replace('T', ' at ')} (UK time).")
        else:
            db.execute("UPDATE competitions SET status='live', scheduled=0 WHERE id=?", (cid,))
            audit(db, "comp.publish", f"comp:{cid}", f"Published “{c['title']}”")
            flash("Published — it's live on the site.")
            announce_live(c)
    elif action == "unpublish" and (c["status"] == "live" or c["scheduled"]):
        if taken_count(db, cid) or db.execute("SELECT 1 FROM tickets WHERE competition_id=? LIMIT 1", (cid,)).fetchone():
            flash("People have already entered, so it can't go back to draft. Cancel it instead (that refunds everyone).", "error")
            return redirect(url_for("admin.entries", cid=cid))
        db.execute("UPDATE competitions SET status='draft', scheduled=0 WHERE id=?", (cid,))
        audit(db, "comp.unpublish", f"comp:{cid}", "Back to draft")
        flash("Back to draft — it's hidden from the site.")
    elif action == "cancel" and c["status"] in ("draft", "live"):
        n, total = cancel_competition(cid)
        refunded = db.execute(
            "SELECT l.user_id, SUM(l.amount) AS amt, u.email FROM orders o JOIN credit_ledger l ON l.ref IN "
            "('refund-o' || o.id, 'refund-o' || o.id || '-credit', 'refund-o' || o.id || '-deposit') "
            "JOIN users u ON u.id=l.user_id WHERE o.competition_id=? GROUP BY l.user_id", (cid,)).fetchall()
        for r in refunded:
            tell(r["email"], f"{c['title']} was cancelled — you've been refunded", kind="refund", key=f"refund:{cid}:{r['user_id']}",
                 user_id=r["user_id"], link=url_for("public.account", tab="wallet"),
                 body=f"We're sorry — {c['title']} has been cancelled. Everything you paid has been refunded to your wallet: "
                      "card and cash payments to your cash balance (withdrawable), site credit as site credit.",
                 highlight=f"£{r['amt'] / 100:.2f} refunded", heading="Competition cancelled")
        flash(f"Cancelled. {n} entrant{'s' if n != 1 else ''} refunded £{total / 100:.2f} — card and cash payments to their "
              "cash balance (withdrawable), site credit back as site credit.", "error")
    else:
        abort(400)
    return redirect(url_for("admin.entries", cid=cid))


def _remove_images(names):
    names = list(names) + [n.replace(".webp", "-sm.webp") for n in names if n.endswith(".webp")]
    for n in names:
        try:
            os.remove(os.path.join(current_app.config["UPLOAD_DIR"], os.path.basename(n)))
        except OSError:
            pass


@bp.route("/competitions/<int:cid>/delete", methods=["POST"])
@require("comps")
def delete_comp(cid):
    c = _comp(cid)
    if request.form.get("confirm", "").strip().upper() != "DELETE":
        flash("Type DELETE to confirm.", "error")
        return redirect(url_for("admin.entries", cid=cid) + "#delete")
    try:
        _remove_images(delete_competition(cid))
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.entries", cid=cid) + "#delete")
    current_app.logger.warning("Admin %s deleted competition %s (%s)", g.user["email"], cid, c["title"])
    flash(f"Deleted “{c['title']}”.")
    return redirect(url_for("admin.dashboard"))


@bp.route("/competitions/delete-selected", methods=["POST"])
@require("comps")
def delete_selected():
    ids = [int(x) for x in request.form.getlist("cid") if x.isdigit()]
    if not ids:
        flash("Tick at least one competition or game.", "error")
        return redirect(url_for("admin.dashboard"))
    done, skipped = 0, []
    for cid in ids:
        row = get_db().execute("SELECT title FROM competitions WHERE id=?", (cid,)).fetchone()
        if row is None:
            continue
        try:
            _remove_images(delete_competition(cid))
            done += 1
        except PurchaseError:
            skipped.append(row["title"])
    current_app.logger.warning("Admin %s deleted %s competitions", g.user["email"], done)
    msg = f"Deleted {done}."
    if skipped:
        msg += f" Not deleted (paid entries not refunded — cancel them first): {', '.join(skipped[:5])}" + ("…" if len(skipped) > 5 else "")
    flash(msg, "error" if skipped else "message")
    return redirect(url_for("admin.dashboard"))


def _backup_db():
    import sqlite3
    folder = os.path.join(os.path.dirname(current_app.config["DATABASE"]), "backups")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"before-reset-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db")
    src = sqlite3.connect(current_app.config["DATABASE"])
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return path


@bp.route("/start-fresh", methods=["GET", "POST"])
@require("reset")
def reset():
    db = get_db()
    counts = {
        "draws": db.execute("SELECT COUNT(*) FROM competitions WHERE game_type=''").fetchone()[0],
        "games": db.execute("SELECT COUNT(*) FROM competitions WHERE game_type!=''").fetchone()[0],
        "players": db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
        "promos": db.execute("SELECT COUNT(*) FROM promo_codes").fetchone()[0],
        "ledger": db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0],
        "withdrawals": db.execute("SELECT COUNT(*) FROM withdrawals WHERE status IN ('requested','processing')").fetchone()[0],
        "unrefunded": sum(paid_unrefunded(db, r[0]) for r in db.execute("SELECT id FROM competitions")),
        "card": db.execute("SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE status='paid'").fetchone()[0],
    }
    if request.method == "POST":
        f = request.form
        scope = f.get("competitions", "")
        if scope not in ("", "games", "draws", "all"):
            abort(400)
        opts = dict(competitions=scope, wallets=bool(f.get("wallets")), accounts=bool(f.get("accounts")),
                    promos=bool(f.get("promos")))
        if not (scope or opts["wallets"] or opts["accounts"] or opts["promos"]):
            flash("Choose what to delete.", "error")
            return redirect(url_for("admin.reset"))
        if f.get("confirm", "").strip().upper() != "RESET":
            flash("Type RESET in the box to confirm.", "error")
            return redirect(url_for("admin.reset"))
        backup = _backup_db()
        out, images = start_fresh(**opts)
        _remove_images(images)
        current_app.logger.warning("Admin %s ran start-fresh %s -> %s (backup %s)", g.user["email"], opts, out, backup)
        flash("Done — deleted " + ", ".join(f"{v} {k}" for k, v in out.items()) +
              f". A backup of everything before the reset was saved as {os.path.basename(backup)}.")
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/reset.html", n=counts)


@bp.route("/competitions/<int:cid>")
@require("comps.view")
def entries(cid):
    c = _comp(cid)
    db = get_db()
    sold = sold_count(db, cid)
    rows = db.execute(
        "SELECT t.number, t.created_at, COALESCE(u.name, p.name) AS name, COALESCE(u.email, p.email) AS email, "
        "CASE WHEN t.postal_entry_id IS NULL THEN 'Paid' ELSE 'Postal' END AS route "
        "FROM tickets t LEFT JOIN users u ON u.id=t.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id "
        "WHERE t.competition_id=? AND t.status='issued' ORDER BY t.id DESC LIMIT 300", (cid,)).fetchall()
    held = db.execute("SELECT COUNT(*) FROM tickets WHERE competition_id=? AND status='held'", (cid,)).fetchone()[0]
    postal_rejected = db.execute("SELECT COUNT(*) FROM postal_entries WHERE competition_id=? AND status='rejected'",
                                 (cid,)).fetchone()[0]
    postal_log = db.execute(
        "SELECT p.*, t.number, a.name AS added_by_name FROM postal_entries p LEFT JOIN tickets t ON t.postal_entry_id=p.id "
        "LEFT JOIN users a ON a.id=p.added_by WHERE p.competition_id=? ORDER BY p.id DESC LIMIT 200", (cid,)).fetchall()
    draw_recs = db.execute("SELECT d.*, u.email AS run_by_email FROM draws d LEFT JOIN users u ON u.id=d.run_by "
                           "WHERE d.competition_id=? ORDER BY d.id", (cid,)).fetchall()
    draw_rec = draw_recs[0] if draw_recs else None
    claim = db.execute("SELECT * FROM prize_claims WHERE competition_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    revenue = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE competition_id=? AND status='paid'",
                         (cid,)).fetchone()[0]
    prizes = db.execute(
        "SELECT ip.*, t.number AS won_number, COALESCE(u.name, p.name) AS winner, COALESCE(u.email, p.email) AS email "
        "FROM instant_prizes ip LEFT JOIN tickets t ON t.id=ip.ticket_id LEFT JOIN users u ON u.id=t.user_id "
        "LEFT JOIN postal_entries p ON p.id=t.postal_entry_id WHERE ip.competition_id=? ORDER BY ip.value DESC, ip.number",
        (cid,)).fetchall()
    from .checks import draw_checks, launch_checks
    checklist, checklist_kind = None, None
    if c["status"] == "draft":
        checklist, checklist_kind = launch_checks(db, c), "launch"
    elif c["status"] == "live" and not c["game_type"] and c["ends_at"] <= iso(utcnow()):
        checklist, checklist_kind = draw_checks(db, c), "draw"
    return render_template("admin/entries.html", c=c, sold=sold, held=held, state=comp_state(c, sold), rows=rows,
                           checklist=checklist, checklist_kind=checklist_kind,
                           winner=winner_details(db, c), postal_rejected=postal_rejected, revenue=revenue,
                           prizes=prizes, board=instant_board(db, cid, reveal=True), gi=game_info(db, c),
                           game_name=GAME_NAMES.get(c["game_type"]),
                           can_edit_instant=taken_count(db, cid) == 0 and c["status"] in ("draft", "live"),
                           now_iso=iso(utcnow()), postal_log=postal_log, draw_rec=draw_rec, draw_recs=draw_recs,
                           claim=claim, claim_names=CLAIM_NAMES, stage=lifecycle_stage(db, c), stage_names=LIFECYCLE_NAMES,
                           snapshot=latest_snapshot(db, cid),
                           postal_waiting=db.execute("SELECT COUNT(*) FROM postal_entries WHERE competition_id=? AND status='received'",
                                                     (cid,)).fetchone()[0],
                           today=datetime.now(UK).strftime("%Y-%m-%d"),
                           audit_rows=db.execute("SELECT * FROM audit_log WHERE target=? ORDER BY id DESC LIMIT 20",
                                                 (f"comp:{cid}",)).fetchall())


@bp.route("/competitions/<int:cid>/instant", methods=["POST"])
@require("comps")
def instant(cid):
    f = request.form
    try:
        if f.get("remove"):
            remove_instant_prize_group(cid, f["remove"])
            flash("Removed.")
        elif f.get("prize_table") is not None:
            rows = parse_prize_table(f.get("prize_table"))
            if not rows:
                raise ValueError("Add at least one prize line.")
            _add_prize_table(cid, rows)
            flash(f"Added {sum(r[0] for r in rows)} prizes — numbers picked at random and sealed.")
        elif f.get("random_table"):
            c = _comp(cid)
            _add_prize_table(cid, [(q, n, v, prize_kind_for(n)) for n, v, q in random_prize_table(c["ticket_price"] or 10, c["max_tickets"])])
            flash("Added a balanced random prize table. Check it below — remove any line and add your own if you like.")
        else:
            title = f.get("title", "").strip()
            if not title:
                raise ValueError("Give the prize a name.")
            value = money(f.get("value"), "Value")
            kind = f.get("type") if f.get("type") in ("cash", "credit", "physical") else "cash"
            credit = value if kind in ("cash", "credit") else 0
            add_instant_prizes(cid, title, value, credit, int(f.get("quantity", "1")), kind)
            flash(f"Added — numbers picked at random and sealed with a hash.")
    except (ValueError, PurchaseError) as e:
        flash(str(e), "error")
    else:
        audit(get_db(), "instant.change", f"comp:{cid}", "Removed prize group " + repr(f["remove"]) if f.get("remove")
              else "Added instant prizes")
    return redirect(url_for("admin.entries", cid=cid) + "#instant")


@bp.route("/instant/<int:pid>/fulfilled", methods=["POST"])
@require("prizes")
def instant_fulfilled(pid):
    db = get_db()
    p = db.execute("SELECT * FROM instant_prizes WHERE id=?", (pid,)).fetchone()
    if p is None:
        abort(404)
    db.execute("UPDATE instant_prizes SET fulfilled=1 WHERE id=?", (pid,))
    audit(db, "prize.sent", f"comp:{p['competition_id']}", f"Instant prize #{pid} “{p['title']}” (ticket #{p['number']}) marked sent")
    flash("Marked as sent.")
    return redirect(request.form.get("back") or url_for("admin.entries", cid=p["competition_id"]))


@bp.route("/competitions/<int:cid>/postal", methods=["POST"])
@require("postal")
def postal(cid):
    from datetime import date
    f = request.form
    back = url_for("admin.entries", cid=cid) + "#postal"
    if not all(f.get(k, "").strip() for k in ("name", "email", "address", "received")):
        flash("Name, email, address and the date it arrived are all required.", "error")
        return redirect(back)
    try:
        received = datetime.strptime(f["received"], "%Y-%m-%d").replace(hour=12, tzinfo=UK)
        dob = date.fromisoformat(f["dob"]) if f.get("dob") else None
    except ValueError:
        flash("Enter the dates as shown (day, month, year).", "error")
        return redirect(back)
    if received.date() > datetime.now(UK).date():
        flash("The date it arrived can't be in the future.", "error")
        return redirect(back)
    # An envelope that arrives on the closing day counts as on time if the competition closes later that day.
    comp_ = _comp(cid)
    rec_iso = iso(received)
    if received.date() == datetime.strptime(comp_["ends_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(UK).date():
        rec_iso = min(rec_iso, comp_["ends_at"])
    rec_iso = min(rec_iso, iso(utcnow()))
    phone = "".join(ch for ch in f.get("phone", "") if ch.isdigit() or ch == "+")[:20]
    try:
        if f.get("mode") == "receive":
            receive_postal(cid, f["name"].strip(), f["email"].strip(), f["address"].strip(),
                           f.get("answer_correct") == "1", g.user["id"], rec_iso, dob, phone)
            flash("Envelope logged as received. Approve or reject it from the postal queue.")
            return redirect(back)
        pid, number, reason = add_postal_entry(cid, f["name"].strip(), f["email"].strip(), f["address"].strip(),
                                                f.get("answer_correct") == "1", g.user["id"], rec_iso, dob, phone)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(back)
    if number:
        flash(f"Postal entry accepted — ticket #{number}.")
        _postal_email(comp_, f["email"].strip(), f["name"], number, pid)
    else:
        flash(f"Logged as rejected: {reason}. No ticket was issued.", "error")
    return redirect(back)


def _postal_email(comp_, email, name, number, pid=None):
    u = get_db().execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
    tell(email, "Your free entry is in", kind="entry", key=f"postal:{pid or comp_['id']}:{number}", user_id=u["id"] if u else None,
         link=url_for("public.competition", slug=comp_["slug"]), title=f"Postal entry accepted — {comp_['title']}",
         body=f"Hi {name.split()[0]},\n\nYour postal entry for {comp_['title']} has been added. Your ticket number:",
         highlight=f"Ticket #{number}", heading="Free entry confirmed",
         button=("View the competition", f"{current_app.config['SITE_URL']}{url_for('public.competition', slug=comp_['slug'])}"))


@bp.route("/postal")
@require("postal")
def postal_queue():
    db = get_db()
    waiting = db.execute(
        "SELECT p.*, c.title, c.ends_at, c.max_per_user, c.slug, a.name AS added_by_name FROM postal_entries p "
        "JOIN competitions c ON c.id=p.competition_id LEFT JOIN users a ON a.id=p.added_by "
        "WHERE p.status='received' ORDER BY c.ends_at, p.id").fetchall()
    rows = []
    for e in waiting:
        comp = db.execute("SELECT * FROM competitions WHERE id=?", (e["competition_id"],)).fetchone()
        rows.append({"e": e, "problem": postal_problem(db, comp, e)})
    recent = db.execute("SELECT p.*, c.title FROM postal_entries p JOIN competitions c ON c.id=p.competition_id "
                        "WHERE p.status!='received' ORDER BY p.id DESC LIMIT 50").fetchall()
    return render_template("admin/postal.html", rows=rows, recent=recent, reasons=POSTAL_REJECT_REASONS)


@bp.route("/postal/<int:pid>", methods=["POST"])
@require("postal")
def postal_process(pid):
    approve = request.form.get("action") == "approve"
    reason = request.form.get("reason", "")
    if reason == "Other (see note)" or not reason:
        reason = request.form.get("note", "").strip() or reason
    try:
        number, why = process_postal(pid, approve, g.user, reason)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(request.form.get("back") or url_for("admin.postal_queue"))
    e = get_db().execute("SELECT p.*, c.title, c.slug FROM postal_entries p JOIN competitions c ON c.id=p.competition_id "
                         "WHERE p.id=?", (pid,)).fetchone()
    if number:
        _postal_email(e, e["email"], e["name"], number, pid)
        flash(f"Approved — ticket #{number} issued to {e['name']}.")
    else:
        flash(f"Rejected: {why}." + (" The rules didn't allow it to be approved." if approve else ""), "error")
    return redirect(request.form.get("back") or url_for("admin.postal_queue"))


def announce_live(c):
    url = f"{current_app.config['SITE_URL']}{url_for('public.competition', slug=c['slug'])}"
    if c["free_daily"]:
        discord(f"🎁 **{c['title']}** is live — everyone gets a free play every day! {url}")
    elif c["game_type"]:
        discord(f"⚡ **{c['title']}** is live — {c['ticket_price'] / 100:.2f} a play! {url}")
    else:
        discord(f"🆕 **{c['title']}** is live! {c['ticket_price'] / 100:.2f} a ticket — {url}")


def announce_draw(cid):
    """Tell the winner, every other entrant (in their account) and staff; post to Discord. Safe to call again."""
    db = get_db()
    c = db.execute("SELECT * FROM competitions WHERE id=?", (cid,)).fetchone()
    w = winner_details(db, c)
    if not w:
        return None
    d = db.execute("SELECT * FROM draws WHERE competition_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    link = url_for("public.competition", slug=c["slug"])
    full = f"{current_app.config['SITE_URL']}{link}"
    wuid = d["winner_user_id"] if d else None
    claim = db.execute("SELECT id FROM prize_claims WHERE draw_id=?", (d["id"],)).fetchone() if d else None
    prize_link = url_for("public.prize_claim", claim_id=claim["id"]) if claim and wuid else link
    tell(w["email"], f"🎉 You've won {c['title']}!", kind="win", key=f"win:{d['id'] if d else cid}", user_id=wuid, link=prize_link,
         title=f"You won {c['title']}!",
         body=f"Hi {w['name'].split()[0]},\n\nCongratulations — your ticket #{w['number']} has just won:"
              + (f"\n\nPrefer cash? You can choose the cash alternative of {c['cash_alternative']} instead." if c["cash_alternative"] else "")
              + "\n\nOpen your prize page to tell us how you'd like it and where to send it, and to follow its progress. "
              "We'll also be in touch very shortly. We will never ask you to pay to claim it.",
         highlight=c["title"], heading="You're a winner!",
         button=("Claim my prize", current_app.config["SITE_URL"] + prize_link) if prize_link != link else ("See the draw", full),
         preheader=f"Ticket #{w['number']} has won {c['title']}!")
    for (uid,) in db.execute("SELECT DISTINCT user_id FROM tickets WHERE competition_id=? AND status='issued' AND user_id IS NOT NULL "
                             "AND user_id IS NOT ?", (cid, wuid)).fetchall():
        notify(uid, "result", f"Draw result: {c['title']}", f"The winning ticket was #{w['number']}. Your tickets didn't win "
               "this time — thanks for entering.", link=link, dedupe_key=f"result:{d['id'] if d else cid}:{uid}")
    entrants = {r[0] for r in db.execute("SELECT DISTINCT user_id FROM tickets WHERE competition_id=? AND user_id IS NOT NULL", (cid,))}
    for r in db.execute("SELECT w.user_id, u.email, u.reminder_emails FROM watchlist w JOIN users u ON u.id=w.user_id "
                        "WHERE w.competition_id=? AND w.remind_result=1", (cid,)).fetchall():
        if r["user_id"] in entrants:
            continue                       # entrants already hear the result
        nid = notify(r["user_id"], "result", f"Result: {c['title']}", f"The draw has taken place — the winning ticket was #{w['number']}.",
                     link=link, dedupe_key=f"watch-result:{d['id'] if d else cid}:{r['user_id']}",
                     email=r["email"] if r["reminder_emails"] else None,
                     mail={"button": ("See the result", full), "heading": "Draw completed", "subject": f"Result: {c['title']}"})
        if nid and r["reminder_emails"]:
            from .notify import send_one
            send_one(nid)
    if current_app.config["SUPPORT_EMAIL"]:
        mailer.send(current_app.config["SUPPORT_EMAIL"], f"Draw result: {c['title']}",
                    f"Winning ticket #{w['number']}: {w['name']} <{w['email']}>. Arrange the prize from Admin → Winners.\n{full}")
    discord(f"🎉 **{c['title']}** has been drawn! Winning ticket **#{w['number']}** — congratulations "
            f"{public_name(w['name'])}! {full}")
    return w


@bp.route("/competitions/<int:cid>/draw", methods=["POST"])
@require("draws")
def draw(cid):
    try:
        number = run_draw(cid, actor=g.user)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.entries", cid=cid))
    w = announce_draw(cid)
    flash(f"Winning ticket: #{number} — {w['name']} ({w['email']}).")
    return redirect(url_for("admin.entries", cid=cid))


@bp.route("/competitions/<int:cid>/winner", methods=["POST"])
@require("prizes")
def winner_story(cid):
    data = {"winner_quote": request.form.get("winner_quote", "").strip()[:500]}
    has_photo = bool(request.files.get("winner_photo") and request.files["winner_photo"].filename)
    if (has_photo or data["winner_quote"]) and not request.form.get("consent"):
        flash("Tick the box to confirm the winner has agreed to their photo and comment being shown.", "error")
        return redirect(url_for("admin.entries", cid=cid))
    if request.form.get("consent"):
        data.update(winner_consent_at=iso(utcnow()), winner_consent_by=g.user["id"])
    try:
        img = _save_image(request.files.get("winner_photo"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.entries", cid=cid))
    if img:
        data["winner_photo"] = img
    db = get_db()
    db.execute(f"UPDATE competitions SET {','.join(k + '=?' for k in data)} WHERE id=?", [*data.values(), cid])
    audit(db, "winner.story", f"comp:{cid}", "Winner story saved" + (" with recorded permission" if request.form.get("consent") else ""))
    flash("Winner story saved — it shows on the winners page.")
    return redirect(url_for("admin.entries", cid=cid))


@bp.route("/competitions/<int:cid>/export.csv")
@require("comps.view")
def export(cid):
    c = _comp(cid)
    rows = get_db().execute(
        "SELECT t.number, CASE WHEN t.postal_entry_id IS NULL THEN 'paid' ELSE 'postal' END, "
        "COALESCE(u.name, p.name), COALESCE(u.email, p.email), t.created_at, t.order_id "
        "FROM tickets t LEFT JOIN users u ON u.id=t.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id "
        "WHERE t.competition_id=? AND t.status='issued' ORDER BY t.number", (cid,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ticket", "route", "name", "email", "issued_utc", "order_id"])
    w.writerows([tuple(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={c['slug']}-entries.csv"})


# ---------------- starter instant-win games ----------------

# (title, game type, price p, plays, max per person, [(prize name, value £, how many)])
# All prizes are real cash paid to the player's cash balance (withdrawable). Return-to-player is roughly 45–50%. Edit freely before publishing.
STARTER_GAMES = [
    ("10p Penny Scratch", "scratch", 10, 5000, 500,
     [("£50 Cash", 50, 1), ("£20 Cash", 20, 2), ("£10 Cash", 10, 5), ("£2 Cash", 2, 20), ("50p Cash", 0.5, 60),
      ("20p Cash", 0.2, 200)]),
    ("40p Spin & Win", "spin", 40, 3000, 300,
     [("£100 Cash", 100, 1), ("£25 Cash", 25, 3), ("£10 Cash", 10, 10), ("£2 Cash", 2, 40), ("£1 Cash", 1, 150)]),
    ("50p Mystery Box", "box", 50, 3000, 300,
     [("£150 Cash", 150, 1), ("£25 Cash", 25, 4), ("£10 Cash", 10, 15), ("£2 Cash", 2, 60), ("£1 Cash", 1, 200)]),
    ("£1 Golden Scratch", "scratch", 100, 2000, 200,
     [("£250 Cash", 250, 1), ("£50 Cash", 50, 5), ("£10 Cash", 10, 20), ("£2 Cash", 2, 100)]),
    ("£5 High Roller Spin", "spin", 500, 1000, 100,
     [("£1,000 Cash", 1000, 1), ("£250 Cash", 250, 2), ("£50 Cash", 50, 10), ("£10 Cash", 10, 40)]),
]
STARTER_QUESTIONS = [
    ("How many days are there in a week?", "5", "7", "10", "b"),
    ("What colour do you get by mixing red and white?", "Pink", "Green", "Blue", "a"),
    ("Which of these is a fruit?", "Carrot", "Potato", "Apple", "c"),
    ("How many sides does a triangle have?", "3", "4", "5", "a"),
    ("What is the capital of England?", "Paris", "London", "Madrid", "b"),
]


import random as _random
_rng = _random.SystemRandom()

GAME_WORDS = ["Inferno", "Red Hot", "Turbo", "Lucky", "Golden", "Diamond", "Neon", "Mega", "Blaze", "Jackpot", "Royal",
              "Crimson", "Thunder", "Nitro", "Rapid", "Supreme", "Platinum", "Ruby", "Firestorm", "Elite"]
GAME_NOUNS = {"scratch": ["Scratch", "Scratcher", "Scratch Card"], "spin": ["Spin", "Wheel", "Spin & Win"],
              "box": ["Mystery Box", "Vault", "Loot Box"]}
NICE = [10, 20, 50, 100, 200, 300, 500, 1000, 1500, 2000, 2500, 5000, 7500, 10000, 15000, 20000, 25000, 50000, 75000,
        100000, 150000, 200000, 250000]   # pence
PLAY_OPTIONS = {10: [3000, 5000, 8000], 40: [2000, 3000, 4000], 50: [2000, 3000, 5000], 100: [1000, 2000, 3000],
                500: [500, 1000, 1500]}


def _label(pence):
    """Game prizes are real cash (withdrawable)."""
    return f"{pence}p Cash" if pence < 100 else f"£{pence // 100:,} Cash" if pence % 100 == 0 else f"£{pence / 100:.2f} Cash"


def prize_kind_for(name):
    return "credit" if "credit" in name.lower() else "cash"


def random_prize_table(price, plays):
    """Random prize table paying back roughly 30–50% if the game sells out, with a win roughly every
    8–25 plays (lots of small wins, a few big ones). Values in pence."""
    takings = price * plays
    pool = takings * _rng.uniform(0.42, 0.55)
    small = min(v for v in NICE if v >= max(2 * price, 20))            # the frequent "win your money back x2" prize
    win_rate = _rng.uniform(1 / 25, 1 / 8)
    n_small = max(1, min(int(plays * win_rate), int(pool * 0.45 / small)))
    left = pool - n_small * small
    top = max([v for v in NICE if v <= left * _rng.uniform(0.25, 0.45)] or [small])
    table, left = [(top, 1)], left - top
    middle = [v for v in NICE if small < v < top]
    for v in sorted(_rng.sample(middle, min(len(middle), _rng.randint(2, 3))), reverse=True):
        qty = int(left * _rng.uniform(0.35, 0.55) / v)
        if qty >= 1:
            table.append((v, qty))
            left -= v * qty
    table.append((small, n_small))
    merged = {}
    for v, q in table:
        merged[v] = merged.get(v, 0) + q
    return [(_label(v), v, q) for v, q in sorted(merged.items(), reverse=True)]


def _create_game(db, title, kind, price, plays, per_user, prizes, days, publish, qi):
    """prizes: [(name, value_in_pence, quantity)]"""
    from datetime import timedelta
    slug = base = slugify(title)
    n = 2
    while db.execute("SELECT 1 FROM competitions WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    seed, seed_hash = new_seed()
    pool = sum(v * k for _, v, k in prizes)
    q = STARTER_QUESTIONS[qi % len(STARTER_QUESTIONS)]
    cur = db.execute(
        "INSERT INTO competitions (slug, title, description, ticket_price, max_tickets, max_per_user, ends_at, question, "
        "answer_a, answer_b, answer_c, correct, status, seed, seed_hash, created_at, category, game_type, prize_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'draft', ?,?,?, 'cash', ?, ?)",
        (slug, title, f"{GAME_NAMES[kind]} instant-win game. {plays:,} plays, "
                      f"{sum(k for _, _, k in prizes)} winning plays. Every prize is paid straight into your wallet.",
         price, plays, per_user, iso(utcnow() + timedelta(days=days)), *q, seed, seed_hash, iso(utcnow()), kind, pool))
    cid = cur.lastrowid
    for name, value, qty in prizes:
        add_instant_prizes(cid, name, value, value, qty, prize_kind_for(name))
    if publish:
        db.execute("UPDATE competitions SET status='live' WHERE id=?", (cid,))
    return cid, slug


def _announce_games(made):
    if not made:
        return
    base = current_app.config["SITE_URL"]
    lines = "\n".join(f"• **{t}** — {url}" for t, url in made[:10])
    discord(f"⚡ **New instant win games are live!**\n{lines}\n{base}{url_for('public.instant_wins')}")


@bp.route("/games/starter", methods=["POST"])
@require("comps")
def starter_games():
    db = get_db()
    publish = request.form.get("draft") != "1"          # live straight away unless "keep as drafts" ticked
    days = max(1, min(365, request.form.get("days", 30, type=int)))
    made = []
    for i, (title, kind, price, plays, per_user, prizes) in enumerate(STARTER_GAMES):
        cid, slug = _create_game(db, title, kind, price, plays, per_user,
                                 [(n, round(v * 100), k) for n, v, k in prizes], days, publish, i)
        made.append((title, current_app.config["SITE_URL"] + url_for("public.competition", slug=slug)))
    if publish:
        _announce_games(made)
    flash(f"Created {len(made)} games{' — live now' if publish else ' as drafts'}: {', '.join(t for t, _ in made)}.")
    return redirect(url_for("admin.dashboard"))


@bp.route("/games/random", methods=["POST"])
@require("comps")
def random_games():
    db = get_db()
    publish = request.form.get("draft") != "1"
    days = max(1, min(365, request.form.get("days", 30, type=int)))
    per_price = max(1, min(5, request.form.get("per_price", 1, type=int)))
    prices = [p for p in (10, 40, 50, 100, 500) if "1" in request.form.getlist(f"p{p}")] or [10, 40, 50, 100, 500]
    made = []
    for price in prices:
        for _ in range(per_price):
            kind = _rng.choice(list(GAME_NAMES))
            title = f"{price}p" if price < 100 else f"£{price // 100}"
            title += f" {_rng.choice(GAME_WORDS)} {_rng.choice(GAME_NOUNS[kind])}"
            plays = _rng.choice(PLAY_OPTIONS[price])
            prizes = random_prize_table(price, plays)
            cid, slug = _create_game(db, title, kind, price, plays, max(50, plays // 10), prizes, days, publish,
                                     _rng.randrange(len(STARTER_QUESTIONS)))
            made.append((title, current_app.config["SITE_URL"] + url_for("public.competition", slug=slug)))
    if publish:
        _announce_games(made)
    flash(f"Created {len(made)} random games{' — live now' if publish else ' as drafts'}: {', '.join(t for t, _ in made)}.")
    return redirect(url_for("admin.dashboard"))


@bp.route("/games/publish-drafts", methods=["POST"])
@require("comps")
def publish_draft_games():
    db = get_db()
    rows = db.execute("SELECT * FROM competitions WHERE status='draft' AND game_type!=''").fetchall()
    made, skipped = [], []
    for c in rows:
        if not db.execute("SELECT 1 FROM instant_prizes WHERE competition_id=? LIMIT 1", (c["id"],)).fetchone():
            skipped.append(c["title"])
            continue
        db.execute("UPDATE competitions SET status='live' WHERE id=?", (c["id"],))
        made.append((c["title"], current_app.config["SITE_URL"] + url_for("public.competition", slug=c["slug"])))
    _announce_games(made)
    msg = f"Published {len(made)} game{'s' if len(made) != 1 else ''}." if made else "No draft games to publish."
    if skipped:
        msg += f" Skipped (no prizes yet): {', '.join(skipped)}."
    flash(msg)
    return redirect(url_for("admin.dashboard"))


# ---------------- promo codes ----------------

@bp.route("/promos", methods=["GET", "POST"])
@require("money")
def promos():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            if f.get("toggle"):
                db.execute("UPDATE promo_codes SET active=1-active WHERE id=?", (int(f["toggle"]),))
                audit(db, "promo.toggle", f"promo:{f['toggle']}", "Switched on/off")
            else:
                code = re.sub(r"[^A-Za-z0-9]", "", f.get("code", "")).upper()
                if not code:
                    raise ValueError("Enter a code (letters and numbers).")
                percent = int(f.get("percent") or 0)
                fixed = money(f.get("fixed"), "Fixed amount")
                if not (0 <= percent <= 100) or (percent == 0 and fixed == 0):
                    raise ValueError("Set a percentage or a fixed amount off.")
                def when(v):
                    return iso(datetime.strptime(v, "%Y-%m-%dT%H:%M").replace(tzinfo=UK)) if v else None
                start, exp = when(f.get("starts_at")), when(f.get("expires_at"))
                if start and exp and exp <= start:
                    raise ValueError("The end must be after the start.")
                ids = ",".join(str(int(x)) for x in re.findall(r"\d+", f.get("comp_ids", "")))
                if ids and db.execute(f"SELECT COUNT(*) FROM competitions WHERE id IN ({ids})").fetchone()[0] != len(ids.split(",")):
                    raise ValueError("One of those competition numbers doesn't exist.")
                cat = f.get("category") if f.get("category") in dict(CATEGORIES) else None
                db.execute("INSERT INTO promo_codes (code, percent, fixed, min_spend, max_uses, per_user, expires_at, created_at, starts_at, "
                           "comp_ids, category, new_customers, description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (code, percent, fixed, money(f.get("min_spend"), "Minimum spend"),
                            int(f["max_uses"]) if f.get("max_uses") else None, int(f.get("per_user") or 1), exp,
                            iso(utcnow()), start, ids or None, cat, 1 if f.get("new_customers") else 0,
                            f.get("description", "").strip()[:200] or None))
                audit(db, "promo.create", f"promo:{code}", f"{percent}% + {fixed}p off; {start or 'now'} → {exp or 'no end'}; "
                      f"competitions {ids or 'all'}; category {cat or 'any'}; first order only {bool(f.get('new_customers'))}")
                flash(f"Promo code {code} created.")
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:  # unique code clash
            if "UNIQUE" in str(e):
                flash("That code already exists.", "error")
            else:
                raise
        return redirect(url_for("admin.promos"))
    rows = db.execute("SELECT * FROM promo_codes ORDER BY id DESC").fetchall()
    return render_template("admin/promos.html", rows=rows, now=iso(utcnow()), categories=CATEGORIES)


# ---------------- users & wallet ----------------

@bp.route("/users")
@require("users.view")
def users():
    db = get_db()
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.*, (SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=u.id) AS credit, "
           "(SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE user_id=u.id AND status='paid') AS spent FROM users u")
    args = []
    if q:
        sql += " WHERE u.email LIKE ? OR u.name LIKE ?"
        args = [f"%{q}%", f"%{q}%"]
    rows = db.execute(sql + " ORDER BY u.id DESC LIMIT 200", args).fetchall()
    return render_template("admin/users.html", rows=rows, q=q)


@bp.route("/users/<int:uid>", methods=["GET", "POST"])
@require("users.view")
def user_detail(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        need = {"credit": "money", "verify": "users.verify", "admin": "users.manage", "goodwill": "goodwill"}.get(f.get("action"))
        if not need or not can(g.user, need):
            flash("Your role can't do that.", "error")
            return redirect(url_for("admin.user_detail", uid=uid))
        if f.get("action") == "credit":
            try:
                amt = money(f.get("amount"), "Amount")
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("admin.user_detail", uid=uid))
            reason = f.get("reason", "").strip()
            if len(reason) < 3:
                flash("Give a reason for the adjustment (it's kept permanently in their history).", "error")
                return redirect(url_for("admin.user_detail", uid=uid))
            limit = current_app.config.get("LARGE_ADJUSTMENT", 10000)
            kind = "cash" if f.get("kind") == "cash" else "credit"
            from . import approvals
            if abs(amt) > limit and approvals.required(db, "wallet_adjust", g.user):
                approvals.request_approval(db, "wallet_adjust", f"user:{uid}", {"uid": uid, "amount": amt, "kind": kind, "reason": reason},
                                           f"{amt / 100:+.2f} £ {kind} for {u['email']}", reason, g.user)
                flash(f"Adjustments over £{limit / 100:.0f} need a second administrator — sent for approval. Nothing changes until it's approved.")
                return redirect(url_for("admin.user_detail", uid=uid))
            if abs(amt) > limit and not can(g.user, "money.large"):
                flash(f"Adjustments over £{limit / 100:.0f} need an Administrator.", "error")
                audit(db, "wallet.adjust_refused", f"user:{uid}", f"{amt:+}p over the large-adjustment limit")
                return redirect(url_for("admin.user_detail", uid=uid))
            with write_txn() as wdb:
                if amt < 0 and balance(wdb, uid, kind) + amt < 0:
                    flash("That would make their balance negative.", "error")
                    return redirect(url_for("admin.user_detail", uid=uid))
                add_credit(wdb, uid, amt, reason, f"admin{g.user['id']}", kind=kind)
                audit(wdb, "wallet.adjust", f"user:{uid}", f"{amt:+}p {kind}: {reason}")
            flash("Wallet updated.")
        elif f.get("action") == "goodwill":
            from .services import goodwill_credit
            try:
                goodwill_credit(uid, money(f.get("amount"), "Amount"), f.get("reason", ""), g.user,
                                int(f["case_id"]) if f.get("case_id", "").isdigit() else None)
                flash("Goodwill credit added — the customer can see it in their wallet history.")
            except (ValueError, PurchaseError) as e:
                flash(str(e), "error")
        elif f.get("action") == "verify":
            db.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
            audit(db, "user.verify", f"user:{uid}", "Email marked verified by admin")
            flash("Email marked as verified.")
        elif f.get("action") == "admin" and uid != g.user["id"]:
            role = f.get("role") if f.get("role") in ROLES else None
            from . import approvals
            if approvals.required(db, "admin_access", g.user):
                approvals.request_approval(db, "admin_access", f"user:{uid}", {"uid": uid, "role": role},
                                           f"Set admin access for {u['email']} to {ROLES[role][0] if role else 'none'}",
                                           f.get("reason", ""), g.user)
                flash("Admin access changes need a second administrator — sent for approval.")
                return redirect(url_for("admin.user_detail", uid=uid))
            if role:
                db.execute("UPDATE users SET is_admin=1, admin_role=? WHERE id=?", (role, uid))
            else:
                db.execute("UPDATE users SET is_admin=0 WHERE id=?", (uid,))
            audit(db, "user.admin_access", f"user:{uid}", f"Admin access set to {role or 'none'}")
            flash("Admin access changed.")
        return redirect(url_for("admin.user_detail", uid=uid))
    tickets = db.execute("SELECT c.title, COUNT(*) AS n FROM tickets t JOIN competitions c ON c.id=t.competition_id "
                         "WHERE t.user_id=? AND t.status='issued' GROUP BY c.id ORDER BY MAX(t.id) DESC", (uid,)).fetchall()
    ledger = db.execute("SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    pays = db.execute("SELECT * FROM checkouts WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    from .perms import role_of
    return render_template("admin/user.html", roles=ROLES, role_key=role_of(u), u=u, tickets=tickets, ledger=ledger, pays=pays, credit=balance(db, uid),
                           bal=balances(db, uid))


@bp.route("/payouts", methods=["GET", "POST"])
@require("money")
def payouts():
    db = get_db()
    if request.method == "POST":
        if request.form.get("action") == "processing":
            mark_withdrawal_processing(int(request.form["wid"]))
            flash("Marked as processing — the player can see it's being paid.")
            return redirect(url_for("admin.payouts"))
        try:
            w = settle_withdrawal(int(request.form["wid"]), request.form.get("action") == "paid", request.form.get("note", ""))
            flash("Updated.")
            u = db.execute("SELECT * FROM users WHERE id=?", (w["user_id"],)).fetchone()
            paid_ = request.form.get("action") == "paid"
            tell(u["email"], "Your withdrawal has been paid 💷" if paid_ else "Your withdrawal", kind="withdrawal",
                 key=f"withdraw-{'paid' if paid_ else 'returned'}:{w['id']}", user_id=u["id"],
                 link=url_for("public.withdrawal_detail", wid=w["id"]),
                 title=f"Withdrawal of £{w['amount']/100:.2f} {'paid' if paid_ else 'returned to your balance'}", body=
                        f"Hi {u['name'].split()[0]},\n\n"
                        + ("Your withdrawal is on its way — it should show in your account shortly:" if paid_
                           else "We couldn't complete your withdrawal, so it's been returned to your cash balance:")
                        + (f"\n\nNote: {request.form['note']}" if request.form.get("note") else ""),
                        highlight=f"£{w['amount']/100:.2f}", heading="Withdrawal paid" if paid_ else "Withdrawal returned",
                        button=("My wallet", f"{current_app.config['SITE_URL']}{url_for('public.account', tab='wallet')}"))
        except PurchaseError as e:
            flash(str(e), "error")
        return redirect(url_for("admin.payouts"))
    withdrawals = db.execute("SELECT w.*, u.name, u.email FROM withdrawals w JOIN users u ON u.id=w.user_id "
                             "ORDER BY w.status IN ('requested','processing') DESC, w.id DESC LIMIT 100").fetchall()
    prizes = db.execute(
        "SELECT ip.*, c.title AS comp, t.number AS won_number, COALESCE(u.name, p.name) AS winner, "
        "COALESCE(u.email, p.email) AS email, p.address FROM instant_prizes ip JOIN competitions c ON c.id=ip.competition_id "
        "JOIN tickets t ON t.id=ip.ticket_id LEFT JOIN users u ON u.id=t.user_id "
        "LEFT JOIN postal_entries p ON p.id=t.postal_entry_id WHERE ip.fulfilled=0 ORDER BY ip.won_at").fetchall()
    refunds = db.execute("SELECT c.*, u.name, u.email FROM checkouts c JOIN users u ON u.id=c.user_id "
                         "WHERE c.status='needs_refund'").fetchall()
    dep_refunds = db.execute("SELECT d.*, u.name, u.email FROM deposits d JOIN users u ON u.id=d.user_id "
                             "WHERE d.status='needs_refund'").fetchall()
    deposits = db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE kind='deposit'").fetchone()[0]
    return render_template("admin/payouts.html", withdrawals=withdrawals, prizes=prizes, refunds=refunds,
                           dep_refunds=dep_refunds, deposits_held=deposits)


@bp.route("/orders/<int:cid>", methods=["GET", "POST"])
@require("users.view")
def order_detail(cid):
    """One order (checkout): what was bought, how it was paid, its tickets, and refunds — with refund tools."""
    from .services import refund_order
    db = get_db()
    k = db.execute("SELECT k.*, u.name, u.email FROM checkouts k JOIN users u ON u.id=k.user_id WHERE k.id=?", (cid,)).fetchone()
    if k is None:
        abort(404)
    if request.method == "POST":
        if not can(g.user, "money"):
            flash("Your role can't refund orders.", "error")
            return redirect(url_for("admin.order_detail", cid=cid))
        to_card = request.form.get("method") == "card"
        try:
            rid = refund_order(int(request.form["order_id"]), g.user, request.form.get("reason", ""), to_card=to_card)
        except PurchaseError as e:
            flash(str(e), "error")
            return redirect(url_for("admin.order_detail", cid=cid))
        r = db.execute("SELECT * FROM refunds WHERE id=?", (rid,)).fetchone()
        if r["method"] == "card" and r["card_amount"] > 0:
            try:
                res = payments.refund(k["payment_intent"], amount=r["card_amount"], why=f"order refund: {r['reason'][:80]}")
                db.execute("UPDATE refunds SET status='done', stripe_refund_id=? WHERE id=?", (res.get("id"), rid))
            except Exception:
                current_app.logger.exception("Card refund failed for refund %s", rid)
                db.execute("UPDATE refunds SET status='card_failed' WHERE id=?", (rid,))
                db.execute("INSERT OR IGNORE INTO flags (kind, subject, detail, created_at) VALUES ('Refund failed', ?, ?, ?)",
                           (f"refund:{rid}", f"Card refund of {r['card_amount']}p for order #{cid} failed — refund it in Stripe "
                                             f"({k['payment_intent']}) and note it here.", iso(utcnow())))
                flash("Tickets released and wallet parts refunded, but the card refund failed — it's flagged; refund it in Stripe.", "error")
                return redirect(url_for("admin.order_detail", cid=cid))
        u = db.execute("SELECT * FROM users WHERE id=?", (k["user_id"],)).fetchone()
        total = r["card_amount"] + r["wallet_amount"]
        tell(u["email"], f"Refund for order #{cid}", f"Hi {u['name'].split()[0]},\n\nWe've refunded £{total / 100:.2f} for part of order #{cid}"
             + (f": £{r['card_amount'] / 100:.2f} to your card (3–10 days)" if r["card_amount"] else "")
             + (f"{' and ' if r['card_amount'] else ': '}£{r['wallet_amount'] / 100:.2f} to your wallet" if r["wallet_amount"] else "")
             + ". The tickets for it have been released.", kind="payment", key=f"refund:{rid}", user_id=u["id"],
             link=url_for("public.order_detail", cid=cid), heading="Refund processed", highlight=f"£{total / 100:.2f}")
        flash("Refunded.")
        return redirect(url_for("admin.order_detail", cid=cid))
    lines = db.execute("SELECT o.*, c.title, c.slug, c.status AS comp_status, c.locked_at, "
                       "(SELECT COUNT(*) FROM tickets t WHERE t.order_id=o.id) AS n_tickets, "
                       "(SELECT GROUP_CONCAT(number, ', ') FROM (SELECT number FROM tickets t WHERE t.order_id=o.id ORDER BY number LIMIT 40)) AS numbers "
                       "FROM orders o JOIN competitions c ON c.id=o.competition_id WHERE o.checkout_id=?", (cid,)).fetchall()
    refunds = {r["order_id"]: r for r in db.execute("SELECT r.*, u.email AS staff FROM refunds r LEFT JOIN users u ON u.id=r.staff_id "
                                                     "WHERE r.checkout_id=?", (cid,))}
    from .services import order_refund_parts, _refunded
    info = {ln["id"]: {"parts": order_refund_parts(db, ln), "refunded": _refunded(db, ln["id"])} for ln in lines}
    return render_template("admin/order.html", k=k, lines=lines, refunds=refunds, info=info)


@bp.route("/content")
@require("settings")
def content_list():
    from .content import EDITABLE, current
    db = get_db()
    rows = [(slug, title, legal, current(slug, db)) for slug, (title, legal) in EDITABLE.items()]
    return render_template("admin/content.html", rows=rows)


@bp.route("/content/<slug>", methods=["GET", "POST"])
@require("settings")
def content_edit(slug):
    from .content import EDITABLE, current, publish, render
    if slug not in EDITABLE:
        abort(404)
    db = get_db()
    title, legal = EDITABLE[slug]
    cur = current(slug, db)
    body = request.form.get("body", cur["body"] if cur else "")
    if request.method == "POST" and request.form.get("action") == "publish":
        note = request.form.get("note", "").strip()
        if not body.strip():
            flash("Write the content first.", "error")
        elif legal and len(note) < 5:
            flash("Legal documents need a short note saying what changed (it's shown in the public version history).", "error")
        else:
            _, v = publish(slug, body, note, g.user, db)
            audit(db, "content.publish", f"content:{slug}", f"{title} version {v}: {note[:200]}")
            flash(f"Published version {v}. Earlier versions are kept.")
            return redirect(url_for("admin.content_edit", slug=slug))
    versions = db.execute("SELECT v.version, v.note, v.created_at, u.email FROM content_versions v LEFT JOIN users u ON u.id=v.created_by "
                          "WHERE slug=? ORDER BY version DESC", (slug,)).fetchall()
    return render_template("admin/content_edit.html", slug=slug, title=title, legal=legal, body=body, preview=render(body),
                           versions=versions, cur=cur)


@bp.route("/announcements", methods=["GET", "POST"])
@require("settings")
def announcements():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("end"):
            db.execute("UPDATE announcements SET active=0 WHERE id=?", (int(f["end"]),))
            audit(db, "announcement.end", f"announcement:{f['end']}", "Ended")
            flash("Announcement ended.")
            return redirect(url_for("admin.announcements"))
        msg = " ".join(f.get("message", "").split())[:300]
        link = f.get("link", "").strip()[:300] or None
        if not msg:
            flash("Write the announcement.", "error")
            return redirect(url_for("admin.announcements"))
        if link and not (link.startswith("/") or link.startswith("https://")):
            flash("Links must start with / or https://", "error")
            return redirect(url_for("admin.announcements"))

        def when(v, default):
            try:
                return iso(datetime.strptime(v, "%Y-%m-%dT%H:%M").replace(tzinfo=UK)) if v else default
            except ValueError:
                return default
        starts, ends = when(f.get("starts_at"), iso(utcnow())), when(f.get("ends_at"), None)
        if ends and ends <= starts:
            flash("The end must be after the start.", "error")
            return redirect(url_for("admin.announcements"))
        db.execute("INSERT INTO announcements (message, level, link, starts_at, ends_at, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                   (msg, "warning" if f.get("level") == "warning" else "info", link, starts, ends, g.user["id"], iso(utcnow())))
        audit(db, "announcement.create", None, f"{msg} ({starts} → {ends or 'until ended'})")
        flash("Announcement scheduled." if starts > iso(utcnow()) else "Announcement published.")
        return redirect(url_for("admin.announcements"))
    rows = db.execute("SELECT a.*, u.email FROM announcements a LEFT JOIN users u ON u.id=a.created_by ORDER BY a.id DESC LIMIT 50").fetchall()
    return render_template("admin/announcements.html", rows=rows, now=iso(utcnow()))


@bp.route("/deposit-refunds/<int:did>/done", methods=["POST"])
@require("money")
def deposit_refund_done(did):
    db = get_db()
    db.execute("UPDATE deposits SET status='refunded' WHERE id=? AND status='needs_refund'", (did,))
    audit(db, "refund.deposit_done", f"deposit:{did}", "Marked as refunded by hand")
    flash("Marked as refunded.")
    return redirect(url_for("admin.payouts"))


@bp.route("/refunds/<int:cid>/done", methods=["POST"])
@require("money")
def refund_done(cid):
    db = get_db()
    db.execute("UPDATE checkouts SET status='refunded' WHERE id=? AND status='needs_refund'", (cid,))
    audit(db, "refund.checkout_done", f"checkout:{cid}", "Marked as refunded by hand")
    flash("Marked as refunded.")
    return redirect(url_for("admin.payouts"))


@bp.route("/payouts/export.csv")
@require("money")
def payouts_csv():
    """Pending withdrawals in a simple CSV you can use for bank bulk payments."""
    rows = get_db().execute(
        "SELECT w.id, u.name, u.email, w.amount, w.method, w.account_name, w.sort_code, w.account_number, w.paypal_email, "
        "w.created_at FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.status IN ('requested','processing') ORDER BY w.id").fetchall()
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["ref", "name", "email", "amount_gbp", "method", "account_name", "sort_code", "account_number",
                 "paypal_email", "requested_utc"])
    for r in rows:
        wr.writerow([f"DBX{r['id']}", r["name"], r["email"], f"{r['amount'] / 100:.2f}", r["method"], r["account_name"],
                     r["sort_code"], r["account_number"], r["paypal_email"], r["created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pending-withdrawals.csv"})


@bp.route("/stats")
@require("reports")
def stats():
    from datetime import timedelta
    db = get_db()
    days = []
    for i in range(29, -1, -1):
        d0 = (utcnow() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        d1 = d0 + timedelta(days=1)
        rev = db.execute("SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE status='paid' AND paid_at>=? AND paid_at<?",
                         (iso(d0), iso(d1))).fetchone()[0]
        signups = db.execute("SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?", (iso(d0), iso(d1))).fetchone()[0]
        days.append({"label": d0.strftime("%d %b"), "rev": rev, "signups": signups})
    peak = max([d["rev"] for d in days] + [1])
    top = db.execute(
        "SELECT c.id, c.title, c.game_type, c.ticket_price, COALESCE(SUM(o.amount),0) AS revenue, COALESCE(SUM(o.quantity),0) AS sold "
        "FROM competitions c LEFT JOIN orders o ON o.competition_id=c.id AND o.status='paid' "
        "GROUP BY c.id ORDER BY revenue DESC LIMIT 15").fetchall()
    games = []
    for c in db.execute("SELECT * FROM competitions WHERE game_type!='' AND status!='draft' ORDER BY id DESC LIMIT 30"):
        sales = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE competition_id=? AND status='paid'", (c["id"],)).fetchone()[0]
        paid = db.execute("SELECT COALESCE(SUM(value),0) FROM instant_prizes WHERE competition_id=? AND ticket_id IS NOT NULL",
                          (c["id"],)).fetchone()[0]
        games.append({"c": c, "sales": sales, "paid": paid, "rtp": round(100 * paid / sales) if sales else None,
                      "plays": sold_count(db, c["id"])})
    totals = {
        "revenue": db.execute("SELECT COALESCE(SUM(cash_due),0) FROM checkouts WHERE status='paid'").fetchone()[0],
        "cash_won": db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE kind='cash' AND amount>0 AND ref LIKE 'ip%'").fetchone()[0],
        "cash_owed": db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE kind='cash'").fetchone()[0],
        "credit_owed": db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE kind='credit'").fetchone()[0],
        "paid_out": db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone()[0],
        "players": db.execute("SELECT COUNT(DISTINCT user_id) FROM checkouts WHERE status='paid'").fetchone()[0],
        "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    }
    totals["arpu"] = totals["revenue"] // totals["players"] if totals["players"] else 0
    return render_template("admin/stats.html", days=days, peak=peak, top=top, games=games, totals=totals)


FREE_DAILY_PRIZES = [("£50 Cash", 5000, 1, "cash"), ("£10 Cash", 1000, 5, "cash"), ("£5 Site Credit", 500, 20, "credit"),
                     ("£1 Site Credit", 100, 150, "credit"), ("50p Site Credit", 50, 400, "credit")]


@bp.route("/games/free-daily", methods=["POST"])
@require("comps")
def free_daily_game():
    from datetime import timedelta
    db = get_db()
    kind = request.form.get("kind") if request.form.get("kind") in GAME_NAMES else "spin"
    plays, days = 20000, 90
    slug = base = "daily-free-" + kind
    n = 2
    while db.execute("SELECT 1 FROM competitions WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    seed, seed_hash = new_seed()
    title = {"spin": "Daily Free Spin", "scratch": "Daily Free Scratch", "box": "Daily Free Mystery Box"}[kind]
    cur = db.execute(
        "INSERT INTO competitions (slug, title, description, ticket_price, max_tickets, max_per_user, ends_at, question, "
        "answer_a, answer_b, answer_c, correct, status, seed, seed_hash, created_at, category, game_type, prize_value, "
        "free_daily) VALUES (?,?,?,0,?,?,?,?,?,?,?,?, 'live', ?,?,?, 'cash', ?, ?, 1)",
        (slug, title, "One free play every day for every verified member — no purchase needed. Win real cash or site credit.",
         plays, days + 1, iso(utcnow() + timedelta(days=days)), "Free game — no question", "-", "-", "-", "a",
         seed, seed_hash, iso(utcnow()), kind, sum(v * q for _, v, q, _ in FREE_DAILY_PRIZES)))
    for name, v, q, k in FREE_DAILY_PRIZES:
        add_instant_prizes(cur.lastrowid, name, v, v, q, k)
    discord(f"🎁 **{title}** is live — everyone gets a free play every day! "
            f"{current_app.config['SITE_URL']}{url_for('public.competition', slug=slug)}")
    flash(f"{title} is live. Members get one free play a day for {days} days.")
    return redirect(url_for("admin.entries", cid=cur.lastrowid))


# ---------------- site settings ----------------

@bp.route("/features", methods=["GET", "POST"])
@require("settings")
def features():
    from .flags import STATES, all_flags, set_state
    db = get_db()
    if request.method == "POST":
        changed = set_state(db, request.form.get("key", ""), request.form.get("state", ""), g.user)
        flash("Saved — the change applies straight away." if changed else "No change.")
        return redirect(url_for("admin.features"))
    return render_template("admin/features.html", rows=all_flags(db), states=STATES)


@bp.route("/settings", methods=["GET", "POST"])
@require("settings")
def settings():
    keys = ("live_now_url", "live_now_title")
    if request.method == "POST" and request.form.get("site_status") is not None:
        mode = request.form["site_status"] if request.form["site_status"] in ("ok", "payments_paused", "maintenance") else "ok"
        set_setting("site_status", mode)
        set_setting("site_status_message", request.form.get("site_status_message", "").strip()[:300])
        if mode == "ok":
            set_setting("payments_auto_paused_until", "")
        audit(get_db(), "site.status", None, f"Site status set to {mode}: {request.form.get('site_status_message', '')[:200]}")
        flash({"ok": "Everything's open again.", "payments_paused": "Payments paused — customers see your message.",
               "maintenance": "Maintenance mode on. Admins can still use the site; customers see the maintenance page."}[mode])
        return redirect(url_for("admin.settings"))
    if request.method == "POST" and request.form.get("default_question_mode"):
        v = request.form["default_question_mode"] if request.form["default_question_mode"] in ("multiple_choice", "none") else "multiple_choice"
        set_setting("default_question_mode", v)
        audit(get_db(), "site.mechanics", None, f"Default entry mechanic for new competitions set to {v}")
        flash("Default for new competitions saved. Existing competitions keep their own setting.")
        return redirect(url_for("admin.settings"))
    if request.method == "POST":
        for k in keys:
            v = request.form.get(k, "").strip()
            if k == "live_now_url" and v and not v.startswith("https://"):
                flash("Live link must start with https://", "error")
                return redirect(url_for("admin.settings"))
            set_setting(k, v)
        audit(get_db(), "site.settings", None, {k: request.form.get(k, "") for k in keys})
        if request.form.get("live_now_url") and request.form.get("announce"):
            discord(f"🔴 **We're LIVE!** {request.form.get('live_now_title') or ''} {request.form['live_now_url']}")
        flash("Saved.")
        return redirect(url_for("admin.settings"))
    from .status import state
    return render_template("admin/settings.html", s={k: get_setting(k) for k in keys}, state=state(),
                           status_message=get_setting("site_status_message"), mode=get_setting("site_status", "ok"),
                           default_qm=get_setting("default_question_mode", "multiple_choice"))


# ---------------- audit log ----------------

@bp.route("/audit")
@require("audit")
def audit_log():
    from .services import verify_audit_chain
    db = get_db()
    a = request.args
    f = {k: a.get(k, "").strip()[:80] for k in ("target", "actor", "action", "q", "from", "to")}
    where, args = [], []
    if f["target"]:
        where.append("target=?"); args.append(f["target"])
    if f["actor"] == "system":
        where.append("actor_id IS NULL")
    elif f["actor"]:
        where.append("actor_email LIKE ?"); args.append(f"%{f['actor']}%")
    if f["action"]:
        where.append("action LIKE ?"); args.append(f"{f['action']}%")
    if f["q"]:
        where.append("(detail LIKE ? OR target LIKE ?)"); args += [f"%{f['q']}%"] * 2
    for key, op, extra in (("from", ">=", ""), ("to", "<", "")):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[key]):
            try:
                day = datetime.strptime(f[key], "%Y-%m-%d").replace(tzinfo=UK)
            except ValueError:
                continue
            if key == "to":
                day += timedelta(days=1)
            where.append(f"created_at{op}?"); args.append(iso(day))
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
    if a.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "created_at_utc", "actor", "action", "target", "detail", "row_hash"])
        for r in db.execute(sql + " LIMIT 50000", args):
            w.writerow([r["id"], r["created_at"], r["actor_email"] or "System", r["action"], r["target"] or "", r["detail"] or "",
                        r["row_hash"] or ""])
        audit(db, "audit.export", None, f"Exported audit log CSV with filters {dict((k, v) for k, v in f.items() if v)}")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=audit-log-{utcnow():%Y%m%d}.csv"})
    rows = db.execute(sql + " LIMIT 300", args).fetchall()
    actions = [r[0] for r in db.execute("SELECT DISTINCT substr(action, 1, instr(action || '.', '.') - 1) FROM audit_log ORDER BY 1")]
    chain = None
    if a.get("verify"):
        broken = verify_audit_chain(db)
        total = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        chain = {"ok": broken is None, "broken": broken, "total": total}
    return render_template("admin/audit.html", rows=rows, f=f, target=f["target"], actions=actions, chain=chain,
                           filtered=any(f.values()))


# ---------------- redraws & prize claims ----------------

@bp.route("/competitions/<int:cid>/redraw", methods=["POST"])
@require("draws")
def redraw_comp(cid):
    if request.form.get("confirm", "").strip().upper() != "REDRAW":
        flash("Type REDRAW to confirm.", "error")
        return redirect(url_for("admin.entries", cid=cid) + "#draw")
    from . import approvals
    if approvals.required(get_db(), "redraw", g.user):
        reason = request.form.get("reason", "").strip()
        if len(reason) < 10:
            flash("Explain why a redraw is needed (at least 10 characters) — it's published in the draw record.", "error")
            return redirect(url_for("admin.entries", cid=cid) + "#draw")
        title = get_db().execute("SELECT title FROM competitions WHERE id=?", (cid,)).fetchone()["title"]
        approvals.request_approval(get_db(), "redraw", f"comp:{cid}", {"cid": cid, "reason": reason},
                                   f"Redraw “{title}”", reason, g.user)
        flash("A redraw changes a published result, so a second administrator has to approve it — sent for approval.")
        return redirect(url_for("admin.entries", cid=cid) + "#draw")
    try:
        n = redraw(cid, request.form.get("reason", ""), g.user)
    except PurchaseError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.entries", cid=cid) + "#draw")
    announce_draw(cid)
    flash(f"Redraw complete — new winning ticket #{n}. The reason and previous result are kept in the draw record.")
    return redirect(url_for("admin.entries", cid=cid) + "#draw")


def _evidence_dir():
    d = os.path.join(os.path.dirname(current_app.config["UPLOAD_DIR"]), "evidence")
    os.makedirs(d, exist_ok=True)
    return d


@bp.route("/prizes")
@require("prizes")
def prizes():
    db = get_db()
    claims = db.execute(
        "SELECT pc.*, c.title, c.slug, c.cash_alternative, c.prize_value, t.number, COALESCE(u.name, p.name) AS winner, "
        "COALESCE(u.email, p.email) AS email, (SELECT MAX(created_at) FROM claim_events e WHERE e.claim_id=pc.id) AS last_event "
        "FROM prize_claims pc JOIN competitions c ON c.id=pc.competition_id JOIN tickets t ON t.id=pc.ticket_id "
        "LEFT JOIN users u ON u.id=pc.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id "
        "ORDER BY pc.status IN ('delivered','forfeited'), pc.updated_at").fetchall()
    unsent = db.execute(
        "SELECT ip.*, c.title AS comp, t.number AS won_number, COALESCE(u.name, p.name) AS winner FROM instant_prizes ip "
        "JOIN competitions c ON c.id=ip.competition_id JOIN tickets t ON t.id=ip.ticket_id LEFT JOIN users u ON u.id=t.user_id "
        "LEFT JOIN postal_entries p ON p.id=t.postal_entry_id WHERE ip.fulfilled=0 ORDER BY ip.won_at").fetchall()
    return render_template("admin/prizes.html", claims=claims, unsent=unsent, names=CLAIM_NAMES)


@bp.route("/prizes/<int:claim_id>", methods=["GET", "POST"])
@require("prizes")
def claim_detail(claim_id):
    db = get_db()
    if request.method == "POST":
        evidence = None
        f = request.files.get("evidence")
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in IMAGE_EXT | {".pdf"}:
                flash("Evidence must be an image or PDF.", "error")
                return redirect(url_for("admin.claim_detail", claim_id=claim_id))
            evidence = f"claim{claim_id}-{secrets.token_hex(6)}{ext}"
            f.save(os.path.join(_evidence_dir(), evidence))
        try:
            before = update_claim(claim_id, request.form.get("status") or None, request.form.get("note", ""), g.user, evidence,
                                  request.form.get("choice"))
            new = request.form.get("status")
            if new and new != before["status"] and before["user_id"]:
                from .services import CLAIM_CUSTOMER
                u = db.execute("SELECT email, name FROM users WHERE id=?", (before["user_id"],)).fetchone()
                comp = db.execute("SELECT title FROM competitions WHERE id=?", (before["competition_id"],)).fetchone()
                head, nxt = CLAIM_CUSTOMER.get(new, ("Update", ""))
                tell(u["email"], f"Your prize: {head} — {comp['title']}", f"Hi {u['name'].split()[0]},\n\n{head}.\n\n{nxt}",
                     kind="win", key=f"claim:{claim_id}:{new}", user_id=before["user_id"],
                     link=url_for("public.prize_claim", claim_id=claim_id), title=f"{comp['title']}: {head}", heading=head,
                     button=("See your prize", current_app.config["SITE_URL"] + url_for("public.prize_claim", claim_id=claim_id)))
            flash("Saved.")
        except PurchaseError as e:
            flash(str(e), "error")
        return redirect(url_for("admin.claim_detail", claim_id=claim_id))
    c = db.execute(
        "SELECT pc.*, c.title, c.slug, c.cash_alternative, c.prize_value, c.id AS comp_id, t.number, "
        "COALESCE(u.name, p.name) AS winner, COALESCE(u.email, p.email) AS email, u.phone, p.address, u.id AS uid "
        "FROM prize_claims pc JOIN competitions c ON c.id=pc.competition_id JOIN tickets t ON t.id=pc.ticket_id "
        "LEFT JOIN users u ON u.id=pc.user_id LEFT JOIN postal_entries p ON p.id=t.postal_entry_id WHERE pc.id=?",
        (claim_id,)).fetchone()
    if c is None:
        abort(404)
    events = db.execute("SELECT e.*, u.email AS actor FROM claim_events e LEFT JOIN users u ON u.id=e.actor_id "
                        "WHERE claim_id=? ORDER BY e.id DESC", (claim_id,)).fetchall()
    return render_template("admin/claim.html", c=c, events=events, statuses=CLAIM_STATUSES, names=CLAIM_NAMES)


@bp.route("/evidence/<name>")
@require("prizes")
def evidence(name):
    from flask import send_from_directory
    return send_from_directory(_evidence_dir(), os.path.basename(name), as_attachment=False)
