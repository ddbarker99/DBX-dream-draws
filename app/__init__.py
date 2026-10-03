import json
import os
import secrets
import time
from datetime import timezone
from zoneinfo import ZoneInfo

import click
from flask import Flask, abort, g, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db as dbmod
from .db import parse_iso, utcnow

UK = ZoneInfo("Europe/London")
ASSET_V = "28"   # bump when style.css or images change, so browsers fetch the new copy
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


def _read_release():
    """The deployed version: RELEASE in .env, else the RELEASE file written at build time, else 'dev'."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "RELEASE")) as f:
            v = f.read().strip()
            return v if v and not v.startswith("$Format") else "dev"
    except OSError:
        return "dev"


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
        REQUIRE_COMP_IMAGE=_env("REQUIRE_COMP_IMAGE", "1") == "1",   # pre-launch check: draws need a prize photo
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
        ADMIN_MFA=_env("ADMIN_MFA", "1") == "1",
        RELEASE=_env("RELEASE", "") or _read_release(),                 # shown in error reports and /admin/health
        ADMIN_IDLE_MINUTES=int(_env("ADMIN_IDLE_MINUTES", "30")),       # admin pages need re-confirmation after this idle time
        ADMIN_STEPUP_MINUTES=int(_env("ADMIN_STEPUP_MINUTES", "10")),   # sensitive actions need a confirmation this recent
        LARGE_ADJUSTMENT=int(float(_env("LARGE_ADJUSTMENT", "100")) * 100),
        REPORT_EMAILS=_env("REPORT_EMAILS", ""),                       # who gets the daily/weekly emails (default SUPPORT_EMAIL)
        GOODWILL_LIMIT=int(float(_env("GOODWILL_LIMIT", "20")) * 100),        # goodwill credit per customer per 30 days  # wallet adjustments above this: Administrator only          # two-step verification for every admin account
        STAGING=_env("STAGING", "0") == "1",
        SIGNUP_RATE_LIMIT=int(_env("SIGNUP_RATE_LIMIT", "10")),   # sign-ups per IP per hour (raise on staging for load tests)              # banner, noindex, every email goes to SUPPORT_EMAIL
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 30,   # static files carry ?v= so they can be cached hard
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # HTTPS sites get secure-only cookies by default (SECURE_COOKIES=0 to override).
        SESSION_COOKIE_SECURE=_env("SECURE_COOKIES", "1" if _env("SITE_URL", "").startswith("https://") else "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    )
    if test_config:
        app.config.update(test_config)
    _fill_contact_defaults(app.config)
    if app.config["SECRET_KEY"] == "dev-only-change-me" and not app.debug and not app.testing \
            and _env("ALLOW_DEV_KEY") != "1":
        raise RuntimeError("Set SECRET_KEY in your .env before running in production.")

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    dbmod.init_db(app.config["DATABASE"], app.config.get("RELEASE") if app.config.get("RELEASE") != "dev" else None)
    app.teardown_appcontext(dbmod.close_db)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.before_request
    def start_timer():
        g._t0 = time.perf_counter()

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
            if g.user is not None:
                from .security import check_session
                if not check_session(g.user):  # signed out from another device
                    g.user = None
                    session.clear()
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
        if request.endpoint in ("public.stripe_webhook", "public.health"):
            return
        if not app.config.get("JOBS_ON_REQUESTS", True):
            return
        from .jobs import run_all_jobs
        try:
            run_all_jobs()            # throttled per job; the worker container normally does this
        except Exception:
            app.logger.exception("background jobs failed")

    @app.before_request
    def maintenance():
        from .status import maintenance_gate
        return maintenance_gate()

    @app.before_request
    def admin_mfa():
        from .security import gate
        return gate()

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
        # Everything (scripts, styles, fonts, images) is served from this site; payments happen on Stripe's own page.
        # 'unsafe-inline' is still needed for the small inline scripts/handlers in the templates.
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; "
                                "form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'; object-src 'none'; "
                                "base-uri 'self'; manifest-src 'self'; worker-src 'self'")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.is_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Logged-in pages show balances, free plays and tickets that change after every action:
        # never let the browser (or the Back button) show a stale copy.
        if app.config["STAGING"]:
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        if g.get("user") and resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store, max-age=0"
        if g.get("_t0") is not None and request.endpoint not in (None, "static", "public.uploads"):
            from . import metrics
            metrics.record((time.perf_counter() - g._t0) * 1000, resp.status_code)
            try:
                metrics.maybe_flush(dbmod.get_db())
            except Exception:                      # measuring must never break a page
                app.logger.exception("metrics flush failed")
        return resp

    @app.context_processor
    def inject():
        from .services import CATEGORIES, get_setting
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        wallet, unread = None, 0
        if g.get("user"):
            from .services import balances
            wallet = balances(dbmod.get_db(), g.user["id"])
            from .notify import unread_count
            unread = unread_count(g.user["id"])
        from .flags import enabled as feature
        from .experiments import variant

        def content_block(slug):
            from .content import current, render
            cur = current(slug)
            return render(cur["body"]) if cur else None
        return {
            "feature": feature, "variant": variant, "content_block": content_block,
            "csrf_token": session["csrf"], "config": app.config, "user": g.get("user"), "wallet": wallet, "unread": unread,
            "basket_count": len(session.get("basket", [])),
            "announcements": dbmod.get_db().execute(
                "SELECT message, level, link FROM announcements WHERE active=1 AND starts_at<=? AND (ends_at IS NULL OR ends_at>?) "
                "ORDER BY level='warning' DESC, id DESC LIMIT 3", (dbmod.iso(utcnow()), dbmod.iso(utcnow()))).fetchall(),
            "live_now": get_setting("live_now_url"), "live_title": get_setting("live_now_title", "We're live!"),
            "categories": CATEGORIES, "asset_v": ASSET_V,
            "site_state": __import__("app.status", fromlist=["state"]).state(),
            "back": __import__("app.navigation", fromlist=["back_link"]).back_link(),
        }

    @app.template_filter("fromjson")
    def fromjson(text):
        import json as _json
        try:
            return _json.loads(text or "[]")
        except ValueError:
            return []

    @app.template_filter("gbp")
    def gbp(pence):
        pence = int(pence or 0)
        sign = "-" if pence < 0 else ""
        pence = abs(pence)
        return f"{sign}£{pence/100:,.2f}" if pence % 100 else f"{sign}£{pence//100:,}"

    @app.template_filter("thumb")
    def thumb(name):
        """URL of the small copy of an uploaded image (made on upload), or the original for older uploads."""
        from flask import url_for
        small = name.replace(".webp", "-sm.webp") if name and name.endswith(".webp") else None
        if small and os.path.exists(os.path.join(app.config["UPLOAD_DIR"], small)):
            return url_for("public.uploads", name=small)
        return url_for("public.uploads", name=name)

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
    def server_error(e):
        from flask import render_template
        ref = None
        try:
            from . import metrics
            db = dbmod.get_db()
            if db.in_transaction:
                db.execute("ROLLBACK")
            ref = metrics.record_error(db, getattr(e, "original_exception", None) or e, request.endpoint, request.path,
                                       request.method, g.user["id"] if g.get("user") else None)
        except Exception:
            app.logger.exception("couldn't record the error")
        return render_template("error.html", code=500, heading="Something went wrong", ref=ref,
                               msg="Something broke on our side. Nothing has been charged twice — please try again in a minute. "
                                   "If it keeps happening, contact us and quote the reference below."), 500

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

    @app.cli.command("run-jobs")
    @click.option("--loop", is_flag=True, help="Keep running every 20 seconds (for the worker container).")
    def run_jobs_cmd(loop):
        """Run background jobs: closing, draws, emails, health alerts…"""
        import time
        from .jobs import run_all_jobs
        while True:
            with app.test_request_context("/__jobs__"):
                g.user = None
                res = run_all_jobs()
                done = {k: v for k, v in res.items() if v not in (None, True)}
                if done:
                    click.echo(f"{utcnow():%H:%M:%S} {done}")
                dbmod.close_db()
            if not loop:
                break
            time.sleep(20)

    @app.cli.command("backup")
    def backup_cmd():
        """Back up the database and files, then prove the backup restores."""
        from .backups import make_backup, restore_test
        out_db, files = make_backup(app.config["DATABASE"])
        rep = restore_test(out_db, app.config["DATABASE"], files)
        for c in rep["checks"]:
            click.echo(f"{'OK ' if c['ok'] else 'FAIL'} {c['name']} {c['detail']}")
        with app.test_request_context("/__backup__"):
            from .services import audit, set_setting
            db = dbmod.get_db()
            if rep["ok"]:
                set_setting("last_backup_verified", dbmod.iso(utcnow()))
            audit(db, "backup.verified" if rep["ok"] else "backup.failed", None,
                  f"{os.path.basename(out_db)}: " + "; ".join(f"{c['name']}: {'ok' if c['ok'] else 'FAILED ' + c['detail']}" for c in rep["checks"]),
                  actor=False)
            db.execute("INSERT INTO job_runs (job, started_at, finished_at, ok, changed, error) VALUES ('backup',?,?,?,?,?)",
                       (dbmod.iso(utcnow()), dbmod.iso(utcnow()), 1 if rep["ok"] else 0, os.path.basename(out_db) if rep["ok"] else None,
                        None if rep["ok"] else json.dumps(rep["checks"])))
            dbmod.close_db()
        click.echo(f"Backup {'VERIFIED' if rep['ok'] else 'FAILED'}: {out_db}")
        if not rep["ok"]:
            raise SystemExit(1)

    @app.cli.command("restore-test")
    @click.argument("path")
    def restore_test_cmd(path):
        """Restore a backup file into a scratch copy and check it."""
        from .backups import restore_test
        rep = restore_test(path, app.config["DATABASE"], path.replace("prizes-", "files-").replace(".db", ".tar.gz"))
        for c in rep["checks"]:
            click.echo(f"{'OK ' if c['ok'] else 'FAIL'} {c['name']} {c['detail']}")
        raise SystemExit(0 if rep["ok"] else 1)

    @app.cli.command("dr-drill")
    @click.option("--backup", "backup_path", default=None, help="Backup file to restore (default: the newest one).")
    def dr_drill_cmd(backup_path):
        """Disaster-recovery exercise: restore a backup into a clean, separate copy of the site and check it works."""
        from .backups import dr_drill
        rep = dr_drill(app.config["DATABASE"], backup_path)
        for c in rep["checks"]:
            click.echo(f"{'OK ' if c['ok'] else 'FAIL'} {c['name']} {c['detail']}")
        click.echo(f"Backup age (data that would be lost, RPO): {rep.get('rpo_hours', '?')} h")
        click.echo(f"Time to restore and check (RTO, excluding server rebuild): {rep['seconds']:.1f} s")
        with app.test_request_context("/__dr__"):
            from .services import audit, set_setting
            db = dbmod.get_db()
            set_setting("last_dr_drill", json.dumps({"at": dbmod.iso(utcnow()), "ok": rep["ok"], "backup": rep.get("backup"),
                                                     "rpo_hours": rep.get("rpo_hours"), "seconds": round(rep["seconds"], 1)}))
            audit(db, "dr.drill", None, ("PASSED" if rep["ok"] else "FAILED") + f" using {rep.get('backup')}: " +
                  "; ".join(f"{c['name']}: {'ok' if c['ok'] else 'FAILED ' + c['detail']}" for c in rep["checks"]), actor=False)
            dbmod.close_db()
        click.echo(f"Disaster-recovery drill {'PASSED' if rep['ok'] else 'FAILED'}")
        raise SystemExit(0 if rep["ok"] else 1)

    @app.cli.command("release-check")
    def release_check_cmd():
        """Before and after every production release: settings, backups, data integrity and health in one go."""
        from .jobs import health_checks
        from .checks import integrity_problems
        from .services import get_setting
        cfg, rows = app.config, []

        def row(level, name, ok, detail=""):
            rows.append(("PASS" if ok else level, name, detail))

        live_key = (cfg.get("STRIPE_SECRET_KEY") or "").startswith("sk_live_")
        row("FAIL", "SECRET_KEY set", cfg["SECRET_KEY"] != "dev-only-change-me")
        row("FAIL", "Test payments switched off (DEMO_PAYMENTS=0)", not cfg.get("DEMO_PAYMENTS"))
        row("WARN", "Stripe live key", live_key, "test key in use" if cfg.get("STRIPE_SECRET_KEY") else "no Stripe key")
        row("FAIL", "Stripe webhook secret", bool(cfg.get("STRIPE_WEBHOOK_SECRET")))
        row("FAIL", "Email (SMTP) configured", bool(cfg.get("SMTP_HOST")))
        row("FAIL", "Free postal entry address", bool(cfg.get("POSTAL_ADDRESS")))
        row("WARN", "Company details", bool(cfg.get("COMPANY_DETAILS")))
        row("FAIL", "SITE_URL uses https", cfg["SITE_URL"].startswith("https://"), cfg["SITE_URL"])
        row("FAIL", "Secure cookies", bool(cfg.get("SESSION_COOKIE_SECURE")))
        row("FAIL", "Admin two-step verification on", bool(cfg.get("ADMIN_MFA")))
        row("WARN", "Release recorded", cfg.get("RELEASE") not in ("", "dev"), cfg.get("RELEASE"))
        with app.test_request_context("/__release_check__"):
            db = dbmod.get_db()
            last = get_setting("last_backup_verified")
            fresh = bool(last) and (utcnow() - dbmod.parse_iso(last)).total_seconds() < 86400
            row("FAIL", "Verified backup in the last 24 hours", fresh, last or "none — run ./backup.sh")
            skipped = get_setting("constraints_skipped")
            row("FAIL", "Database safety rules active", not skipped, skipped)
            problems = integrity_problems(db)
            row("FAIL", "Data integrity", not problems, "; ".join(problems[:3]))
            for c in health_checks(db):
                if c["key"] in ("backups", "integrity", "stripe", "email"):
                    continue                         # covered above
                row("WARN", f"Health: {c['name']}", c["ok"], "" if c["ok"] else c["detail"])
            dbmod.close_db()
        width = max(len(r[1]) for r in rows)
        for level, name, detail in rows:
            click.echo(f"{level:4}  {name:{width}}  {detail if level != 'PASS' else ''}".rstrip())
        fails = [r for r in rows if r[0] == "FAIL"]
        warns = [r for r in rows if r[0] == "WARN"]
        click.echo(f"\n{'NOT READY' if fails else 'READY'}: {len(fails)} failure(s), {len(warns)} warning(s)")
        conn = dbmod._connect(app.config["DATABASE"])
        conn.execute("INSERT INTO releases (release, first_seen) VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                     "ON CONFLICT(release) DO NOTHING", (cfg.get("RELEASE") or "dev",))
        conn.execute("UPDATE releases SET check_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), check_ok=?, check_summary=? WHERE release=?",
                     (0 if fails else 1, "\n".join(f"{lv} {n} {d}".strip() for lv, n, d in rows if lv != "PASS")[:3000] or "All checks passed",
                      cfg.get("RELEASE") or "dev"))
        conn.close()
        raise SystemExit(1 if fails else 0)

    @app.cli.command("demo-lifecycle")
    def demo_lifecycle_cmd():
        """Staging only: run a whole competition — entries, postal entry, draw, winner, delivery — with test money."""
        from .demo import run
        with app.app_context():
            run(app)

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
    from .security import bp as security_bp
    from .control import bp as control_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(security_bp)
    app.register_blueprint(control_bp)
    return app


def _set_admin(app, email, value):
    conn = dbmod._connect(app.config["DATABASE"])
    cur = conn.execute("UPDATE users SET is_admin=?, admin_role='admin' WHERE email=?", (value, email.strip().lower()))
    conn.close()
    if cur.rowcount:
        click.echo(f"{email}: admin {'ON' if value else 'OFF'}")
    else:
        click.echo(f"No account with email {email}", err=True)
        raise SystemExit(1)
