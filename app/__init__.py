import os
import secrets
from datetime import timezone
from zoneinfo import ZoneInfo

import click
from flask import Flask, abort, g, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db as dbmod
from .db import parse_iso, utcnow

UK = ZoneInfo("Europe/London")
ASSET_V = "15"   # bump when style.css or images change, so browsers fetch the new copy
_PLACEHOLDERS = ("example street", "example.com", "yourdomain", "ab1 2cd")


def _clean(value):
    """Treat the sample values from .env.example as 'not set' so they never show on the live site."""
    v = (value or "").strip()
    return "" if any(p in v.lower() for p in _PLACEHOLDERS) else v


def _fill_contact_defaults(cfg):
    from email.utils import parseaddr
    from urllib.parse import urlparse
    if not cfg.get("SUPPORT_EMAIL"):
        addr = parseaddr(cfg.get("MAIL_FROM") or "")[1]
        host = (urlparse(cfg.get("SITE_URL", "")).hostname or "").removeprefix("www.")
        cfg["SUPPORT_EMAIL"] = addr or (f"support@{host}" if host and "." in host else "")
    if not cfg.get("MAIL_FROM"):
        cfg["MAIL_FROM"] = cfg["SUPPORT_EMAIL"]


def _env(name, default=""):
    return os.environ.get(name, default)


def create_app(test_config=None):
    app = Flask(__name__)
    data_dir = _env("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
    app.config.update(
        SECRET_KEY=_env("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.path.join(data_dir, "prizes.db"),
        UPLOAD_DIR=os.path.join(data_dir, "uploads"),
        SITE_NAME=_env("SITE_NAME", "PrizeHub"),
        SITE_TAGLINE=_env("SITE_TAGLINE", "Win something brilliant."),
        SITE_URL=_env("SITE_URL", "http://localhost:5000").rstrip("/"),
        POSTAL_ADDRESS=_clean(_env("POSTAL_ADDRESS")),
        SUPPORT_EMAIL=_clean(_env("SUPPORT_EMAIL")),
        COMPANY_DETAILS=_env("COMPANY_DETAILS", ""),
        STRIPE_SECRET_KEY=_env("STRIPE_SECRET_KEY"),
        STRIPE_WEBHOOK_SECRET=_env("STRIPE_WEBHOOK_SECRET"),
        DEMO_PAYMENTS=_env("DEMO_PAYMENTS", "0") == "1",
        DISCORD_WEBHOOK_URL=_env("DISCORD_WEBHOOK_URL"),
        SMTP_HOST=_env("SMTP_HOST"), SMTP_PORT=_env("SMTP_PORT", "587"),
        SMTP_USER=_env("SMTP_USER"), SMTP_PASSWORD=_env("SMTP_PASSWORD"),
        MAIL_FROM=_env("MAIL_FROM"),
        REFERRAL_BONUS=int(float(_env("REFERRAL_BONUS", "1")) * 100),
        MAX_MONTHLY_LIMIT=int(float(_env("MAX_MONTHLY_LIMIT", "250")) * 100),
        SOCIAL={k: _env(k.upper() + "_URL") for k in ("facebook", "instagram", "tiktok", "youtube", "discord", "twitch")},
        TRUSTPILOT_URL=_env("TRUSTPILOT_URL"),
        BLOCK_CREDIT_CARDS=_env("BLOCK_CREDIT_CARDS", "1") == "1",
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 30,   # static files carry ?v= so they can be cached hard
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env("SECURE_COOKIES", "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    )
    if test_config:
        app.config.update(test_config)
    _fill_contact_defaults(app.config)
    if app.config["SECRET_KEY"] == "dev-only-change-me" and not app.debug and not app.testing \
            and _env("ALLOW_DEV_KEY") != "1":
        raise RuntimeError("Set SECRET_KEY in your .env before running in production.")

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    dbmod.init_db(app.config["DATABASE"])
    app.teardown_appcontext(dbmod.close_db)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = dbmod.get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            pwv = g.user["password_hash"][-16:] if g.user is not None else None
            if g.user is not None and session.get("pwv") not in (None, pwv):
                g.user = None                  # password changed on another device: sign this one out
                session.clear()
            elif g.user is not None and session.get("pwv") is None:
                session["pwv"] = pwv           # sessions from before this check existed
            if g.user is None:
                session.pop("uid", None)
            elif request.endpoint not in ("static", "public.uploads", "public.service_worker", "public.manifest"):
                from .services import settle_unrevealed
                try:
                    settle_unrevealed(uid)   # pays game prizes left unrevealed for 24h / after the game ends
                except Exception:
                    app.logger.exception("settle_unrevealed failed")

    @app.before_request
    def background_jobs():
        """Cheap housekeeping piggy-backed on requests: referral links, automatic draws."""
        if request.endpoint in (None, "static", "public.uploads", "public.service_worker", "public.manifest"):
            return
        ref = request.args.get("ref", "").strip().upper()
        if ref and not session.get("ref") and not g.get("user"):
            if dbmod.get_db().execute("SELECT 1 FROM users WHERE referral_code=?", (ref,)).fetchone():
                session["ref"] = ref
        from .services import due_auto_draws, due_scheduled
        try:
            launched = due_scheduled()
        except Exception:
            app.logger.exception("scheduled publish failed")
            launched = []
        if launched:
            from .admin import announce_live
            for c in launched:
                try:
                    announce_live(c)
                except Exception:
                    app.logger.exception("announce live %s failed", c["id"])
        try:
            drawn = due_auto_draws()
        except Exception:
            app.logger.exception("auto draw failed")
            drawn = []
        if drawn:
            from .admin import announce_draw
            for cid in drawn:
                try:
                    announce_draw(cid)
                except Exception:
                    app.logger.exception("announce draw %s failed", cid)

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and request.endpoint not in ("public.stripe_webhook",):
            token = session.get("csrf")
            if not token or token != request.form.get("csrf"):
                abort(400, "Form expired — go back, refresh and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        # Logged-in pages show balances, free plays and tickets that change after every action:
        # never let the browser (or the Back button) show a stale copy.
        if g.get("user") and resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    @app.context_processor
    def inject():
        from .services import CATEGORIES, get_setting
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        wallet = None
        if g.get("user"):
            from .services import balances
            wallet = balances(dbmod.get_db(), g.user["id"])
        return {
            "csrf_token": session["csrf"], "config": app.config, "user": g.get("user"), "wallet": wallet,
            "basket_count": len(session.get("basket", [])),
            "announcement": get_setting("announcement"),
            "live_now": get_setting("live_now_url"), "live_title": get_setting("live_now_title", "We're live!"),
            "categories": CATEGORIES, "asset_v": ASSET_V,
        }

    @app.template_filter("gbp")
    def gbp(pence):
        pence = int(pence or 0)
        sign = "-" if pence < 0 else ""
        pence = abs(pence)
        return f"{sign}£{pence/100:,.2f}" if pence % 100 else f"{sign}£{pence//100:,}"

    @app.template_filter("pp")
    def pp(pence):
        """10 -> '10p', 100 -> '£1', 250 -> '£2.50'"""
        pence = int(pence or 0)
        return f"{pence}p" if pence < 100 else gbp(pence)

    @app.template_filter("uktime")
    def uktime(s, fmt="%a %d %b %Y, %H:%M"):
        return parse_iso(s).astimezone(UK).strftime(fmt) if s else ""

    @app.template_filter("epoch")
    def epoch(s):
        return int(parse_iso(s).replace(tzinfo=timezone.utc).timestamp())

    @app.template_filter("ago")
    def ago(s):
        secs = (utcnow() - parse_iso(s)).total_seconds()
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= n:
                return f"{int(secs // n)}{unit} ago"
        return "just now"

    @app.errorhandler(404)
    def not_found(_e):
        from flask import render_template
        return render_template("error.html", code=404, heading="Page not found",
                               msg="That page doesn't exist or has moved. Try one of these instead."), 404

    @app.errorhandler(400)
    def bad_request(e):
        from flask import render_template
        return render_template("error.html", code=400, heading="Something went wrong",
                               msg=getattr(e, "description", "") or "That request didn't work — go back and try again."), 400

    @app.errorhandler(500)
    def server_error(_e):
        from flask import render_template
        return render_template("error.html", code=500, heading="Sorry, that didn't work",
                               msg="Something broke on our side. Nothing has been charged twice — please try again in a minute."), 500

    @app.cli.command("make-admin")
    @click.argument("email")
    def make_admin(email):
        """Give an existing account admin access."""
        _set_admin(app, email, 1)

    @app.cli.command("remove-admin")
    @click.argument("email")
    def remove_admin(email):
        """Take admin access away from an account."""
        _set_admin(app, email, 0)

    @app.cli.command("list-admins")
    def list_admins():
        """Show every account with admin access."""
        conn = dbmod._connect(app.config["DATABASE"])
        rows = conn.execute("SELECT email, name FROM users WHERE is_admin=1 ORDER BY id").fetchall()
        conn.close()
        for r in rows:
            click.echo(f"{r['email']}  ({r['name']})")
        if not rows:
            click.echo("No admins.")

    from .routes import bp as public_bp
    from .admin import bp as admin_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)
    return app


def _set_admin(app, email, value):
    conn = dbmod._connect(app.config["DATABASE"])
    cur = conn.execute("UPDATE users SET is_admin=? WHERE email=?", (value, email.strip().lower()))
    conn.close()
    if cur.rowcount:
        click.echo(f"{email}: admin {'ON' if value else 'OFF'}")
    else:
        click.echo(f"No account with email {email}", err=True)
        raise SystemExit(1)
