"""Account security: device sessions (sign out anywhere), admin MFA (TOTP, RFC 6238) and admin login alerts."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for)

from . import mailer
from .db import get_db, iso, parse_iso, utcnow

bp = Blueprint("security", __name__, url_prefix="/admin/mfa")


# ---------------- sessions ----------------

def start_session(user):
    """Call after a successful log in / sign up. Registers this device so it can be signed out later."""
    db = get_db()
    sid = secrets.token_urlsafe(24)
    ip, agent = request.remote_addr or "", (request.user_agent.string or "")[:200]
    known = db.execute("SELECT 1 FROM user_sessions WHERE user_id=? AND ip=? AND agent=? LIMIT 1",
                       (user["id"], ip, agent)).fetchone()
    now = iso(utcnow())
    db.execute("INSERT INTO user_sessions (sid, user_id, created_at, last_seen, ip, agent) VALUES (?,?,?,?,?,?)",
               (sid, user["id"], now, now, ip, agent))
    session["sid"] = sid
    session.pop("mfa_ok", None)
    session["sudo_at"] = time.time()          # signing in counts as a fresh confirmation
    if user["is_admin"]:
        from .services import audit
        audit(db, "admin.login", f"user:{user['id']}", f"Admin log in from {ip}" + ("" if known else " (new device)"), actor=user)
        if not known and not current_app.testing:
            mailer.send(user["email"], "New sign-in to your admin account",
                        f"Hi {user['name'].split()[0]},\n\nYour {current_app.config['SITE_NAME']} admin account was just used to log in "
                        f"from a device we haven't seen before.\n\nIP address: {ip}\nBrowser: {agent[:120]}\n\n"
                        "If this wasn't you, change your password straight away — that signs out every other device.",
                        heading="New admin sign-in")
    return sid


def check_session(user):
    """True if this browser's session is still valid. Old sessions (before device tracking) are registered."""
    db = get_db()
    sid = session.get("sid")
    if not sid:
        start_session(user)
        return True
    row = db.execute("SELECT * FROM user_sessions WHERE sid=?", (sid,)).fetchone()
    if row is None or row["revoked_at"] or row["user_id"] != user["id"]:
        return False
    if (utcnow() - parse_iso(row["last_seen"])).total_seconds() > 300:
        db.execute("UPDATE user_sessions SET last_seen=? WHERE sid=?", (iso(utcnow()), sid))
    return True


def end_session():
    sid = session.get("sid")
    if sid:
        get_db().execute("UPDATE user_sessions SET revoked_at=? WHERE sid=? AND revoked_at IS NULL", (iso(utcnow()), sid))


def revoke_others(user_id, keep_sid=None):
    return get_db().execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND sid IS NOT ?",
                            (iso(utcnow()), user_id, keep_sid)).rowcount


def device_name(agent):
    a = (agent or "").lower()
    os_ = next((n for k, n in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("windows", "Windows"),
                                ("mac os", "Mac"), ("linux", "Linux")) if k in a), "Unknown device")
    br = next((n for k, n in (("edg/", "Edge"), ("chrome/", "Chrome"), ("firefox/", "Firefox"), ("safari/", "Safari")) if k in a), "")
    return f"{br} on {os_}" if br else os_


# ---------------- TOTP ----------------

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret, at=None, step=30, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((at if at is not None else time.time()) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def verify_totp(secret, code, window=1):
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return False
    now = time.time()
    return any(hmac.compare_digest(totp(secret, now + 30 * w), code) for w in range(-window, window + 1))


def _hash_code(c):
    return hashlib.sha256(c.replace("-", "").strip().lower().encode()).hexdigest()


def use_recovery_code(user, code):
    codes = json.loads(user["mfa_recovery"] or "[]")
    h = _hash_code(code)
    if h not in codes:
        return False
    codes.remove(h)
    get_db().execute("UPDATE users SET mfa_recovery=? WHERE id=?", (json.dumps(codes), user["id"]))
    return True


def mfa_required(user):
    return bool(user and user["is_admin"] and current_app.config.get("ADMIN_MFA", True))


# POSTs that move money, change results or change who can do what: the admin must have confirmed it's them
# (password or MFA code) in the last few minutes. endpoint -> form "action" values (None = every POST),
# or ("not", {...}) for every action except those.
SENSITIVE = {
    "admin.user_detail": {"credit", "admin"},
    "admin.payouts": ("not", {"processing"}),
    "admin.refund_done": None, "admin.deposit_refund_done": None,
    "admin.set_status": {"cancel"},
    "admin.draw": None, "admin.redraw_comp": None,
    "admin.claim_detail": None,
    "admin.delete_comp": None, "admin.delete_selected": None, "admin.start_fresh": None,
    "admin.promos": None, "admin.features": None,
}


def is_sensitive():
    if request.method != "POST" or request.endpoint not in SENSITIVE:
        return False
    rule, action = SENSITIVE[request.endpoint], request.form.get("action")
    if rule is None:
        return True
    if isinstance(rule, tuple):
        return action not in rule[1]
    return action in rule


def recently_confirmed():
    return time.time() - session.get("sudo_at", 0) < current_app.config.get("ADMIN_STEPUP_MINUTES", 10) * 60


def gate():
    """Before any admin page: MFA in this session, sign out of admin after inactivity, and a fresh confirmation
    before sensitive actions."""
    if request.blueprint not in ("admin", "control") or g.get("user") is None or not g.user["is_admin"]:
        return None
    now = time.time()
    idle = current_app.config.get("ADMIN_IDLE_MINUTES", 30) * 60
    if session.get("admin_seen") and now - session["admin_seen"] > idle:
        session.pop("mfa_ok", None)
        session.pop("sudo_at", None)
        session.pop("admin_seen", None)
        flash("For security, admin access timed out after inactivity. Confirm it's you to carry on.")
        session["mfa_next"] = request.full_path if request.method == "GET" else url_for("control.centre")
        return redirect(url_for("security.verify" if mfa_required(g.user) else "security.confirm", next=session["mfa_next"]))
    session["admin_seen"] = now
    if mfa_required(g.user) and session.get("mfa_ok") != session.get("sid"):
        session["mfa_next"] = request.full_path if request.method == "GET" else url_for("control.centre")
        return redirect(url_for("security.setup" if not g.user["mfa_enabled"] else "security.verify"))
    if is_sensitive() and not recently_confirmed():
        back = request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("control.centre")
        flash("This is a sensitive action — confirm it's you, then press the button again.")
        return redirect(url_for("security.confirm", next=back[len(request.host_url) - 1:] if back.startswith(request.host_url) else back))
    return None


@bp.route("/confirm", methods=["GET", "POST"])
def confirm():
    """Re-confirm identity (MFA code, or password where MFA is off) before sensitive admin actions."""
    if g.user is None or not g.user["is_admin"]:
        return redirect(url_for("public.login"))
    from .routes import _fail, _too_many, safe_next
    from werkzeug.security import check_password_hash
    use_mfa = mfa_required(g.user) and g.user["mfa_enabled"]
    nxt = safe_next(request.args.get("next"), url_for("control.centre"))
    key = f"sudo:{g.user['id']}"
    if request.method == "POST":
        if _too_many(key, limit=6, window=900):
            flash("Too many attempts. Wait 15 minutes.", "error")
            return render_template("admin/mfa.html", mode="confirm", use_mfa=use_mfa, next=nxt), 429
        code = request.form.get("code", "")
        ok = (verify_totp(g.user["mfa_secret"], code) or (len(code.strip()) > 6 and use_recovery_code(g.user, code))) if use_mfa \
            else check_password_hash(g.user["password_hash"], request.form.get("password", ""))
        from .services import audit
        if ok:
            session["sudo_at"] = time.time()
            session["admin_seen"] = time.time()
            if mfa_required(g.user):
                session["mfa_ok"] = session.get("sid")
            audit(get_db(), "admin.confirmed", f"user:{g.user['id']}", "Confirmed identity for sensitive actions")
            return redirect(nxt)
        _fail(key)
        audit(get_db(), "admin.confirm_failed", f"user:{g.user['id']}", "Wrong code or password at confirmation")
        flash("That didn't match. Try again.", "error")
    return render_template("admin/mfa.html", mode="confirm", use_mfa=use_mfa, next=nxt)


@bp.route("/", methods=["GET", "POST"])
def verify():
    if g.user is None or not g.user["is_admin"]:
        return redirect(url_for("public.login"))
    if not g.user["mfa_enabled"]:
        return redirect(url_for("security.setup"))
    from .routes import _too_many, _fail
    key = f"mfa:{g.user['id']}"
    if request.method == "POST":
        if _too_many(key, limit=6, window=900):
            flash("Too many attempts. Wait 15 minutes.", "error")
            return render_template("admin/mfa.html", mode="verify"), 429
        code = request.form.get("code", "")
        ok = verify_totp(g.user["mfa_secret"], code) or (len(code.strip()) > 6 and use_recovery_code(g.user, code))
        if ok:
            session["mfa_ok"] = session.get("sid")
            session["sudo_at"] = session["admin_seen"] = time.time()
            from .routes import safe_next
            return redirect(safe_next(session.pop("mfa_next", None), url_for("control.centre")))
        _fail(key)
        from .services import audit
        audit(get_db(), "admin.mfa_failed", f"user:{g.user['id']}", "Wrong MFA code")
        flash("That code didn't work. Check your authenticator app's time is set automatically, or use a recovery code.", "error")
    return render_template("admin/mfa.html", mode="verify")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if g.user is None or not g.user["is_admin"]:
        return redirect(url_for("public.login"))
    if g.user["mfa_enabled"] and session.get("mfa_ok") != session.get("sid"):
        return redirect(url_for("security.verify"))          # re-enrolling needs the current factor first
    secret = session.get("mfa_pending") or new_secret()
    session["mfa_pending"] = secret
    if request.method == "POST":
        if not verify_totp(secret, request.form.get("code", "")):
            flash("That code didn't match. Type the 6 digits your app shows now.", "error")
        else:
            codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(8)]
            db = get_db()
            db.execute("UPDATE users SET mfa_secret=?, mfa_enabled=1, mfa_recovery=? WHERE id=?",
                       (secret, json.dumps([_hash_code(c) for c in codes]), g.user["id"]))
            from .services import audit
            audit(db, "admin.mfa_enabled", f"user:{g.user['id']}", "Two-step verification turned on")
            session.pop("mfa_pending", None)
            session["mfa_ok"] = session.get("sid")
            return render_template("admin/mfa.html", mode="codes", codes=codes, next=session.pop("mfa_next", None))
    issuer = current_app.config["SITE_NAME"]
    uri = f"otpauth://totp/{quote(issuer)}:{quote(g.user['email'])}?secret={secret}&issuer={quote(issuer)}"
    return render_template("admin/mfa.html", mode="setup", secret=secret, uri=uri,
                           grouped=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
