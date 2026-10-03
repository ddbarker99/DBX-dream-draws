"""Backups that are proven: every backup is restored into a scratch copy and checked before it counts.

  flask --app wsgi backup                 make a backup, then restore-test it
  flask --app wsgi restore-test FILE      restore-test any backup file
  flask --app wsgi dr-drill               restore the newest backup into a clean copy of the whole site and use it
"""
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

APPEND_ONLY = ("draws", "audit_log", "entry_snapshots", "credit_ledger", "tickets", "users", "checkouts")


def make_backup(db_path, keep=30):
    data_dir = os.path.dirname(os.path.abspath(db_path))
    folder = os.path.join(data_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_db = os.path.join(folder, f"prizes-{stamp}.db")
    src, dst = sqlite3.connect(db_path), sqlite3.connect(out_db)
    with dst:
        src.backup(dst)                      # consistent copy even while the site is running
    src.close()
    dst.close()
    files = os.path.join(folder, f"files-{stamp}.tar.gz")
    with tarfile.open(files, "w:gz") as tar:
        for sub in ("uploads", "evidence"):
            p = os.path.join(data_dir, sub)
            if os.path.isdir(p):
                tar.add(p, arcname=sub)
    for pattern in ("prizes-", "files-"):
        old = sorted(f for f in os.listdir(folder) if f.startswith(pattern))[:-keep]
        for f in old:
            os.remove(os.path.join(folder, f))
    return out_db, files


def restore_test(backup_db, live_db=None, files=None):
    """Restore into a scratch folder and prove it works. Returns a report dict; report['ok'] says if it passed."""
    from .db import init_db
    from .services import pick_index, entries_digest
    rep = {"backup": os.path.basename(backup_db), "checks": []}

    def check(name, ok, detail=""):
        rep["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    tmp = tempfile.mkdtemp(prefix="restore-test-")
    try:
        restored = os.path.join(tmp, "prizes.db")
        shutil.copy(backup_db, restored)
        conn = sqlite3.connect(restored)
        conn.row_factory = sqlite3.Row
        check("Integrity check", conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        conn.close()
        try:
            init_db(restored)                 # the app's own start-up: schema + every migration applies cleanly
            check("Opens with the current app version", True)
        except Exception as e:
            check("Opens with the current app version", False, str(e))
        conn = sqlite3.connect(restored)
        conn.row_factory = sqlite3.Row
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in APPEND_ONLY}
        rep["counts"] = counts
        if live_db and os.path.exists(live_db):
            live = sqlite3.connect(live_db)
            behind = [t for t in APPEND_ONLY if live.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] < counts[t]]
            live.close()
            check("Not ahead of the live database", not behind, ", ".join(behind))
        bad = 0
        for d in conn.execute("SELECT * FROM draws WHERE method!='redraw'").fetchall():
            nums = json.loads(d["entries"])
            if entries_digest(nums) != d["entries_hash"] or nums[pick_index(d["seed"], d["entries_hash"], len(nums))] != d["winning_number"]:
                bad += 1
        check("Every stored draw still recomputes to the same winner", bad == 0, f"{bad} mismatched" if bad else
              f"{conn.execute('SELECT COUNT(*) FROM draws').fetchone()[0]} draw(s) checked")
        conn.close()
        if files and os.path.exists(files):
            with tarfile.open(files) as tar:
                tar.extractall(tmp, filter="data")
            check("Uploaded files restore", True, f"{len(os.listdir(os.path.join(tmp, 'uploads'))) if os.path.isdir(os.path.join(tmp, 'uploads')) else 0} file(s)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rep["ok"] = all(c["ok"] for c in rep["checks"])
    return rep


def latest_backup(db_path):
    folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    found = sorted(f for f in os.listdir(folder) if f.startswith("prizes-") and f.endswith(".db")) if os.path.isdir(folder) else []
    return os.path.join(folder, found[-1]) if found else None


def dr_drill(live_db, backup_db=None):
    """Restore a backup (database + uploaded files) into an empty folder, start a separate copy of the app on it with
    every outside connection switched off (no email, payments or Discord), and use it like a visitor would.
    Nothing touches the live site. Returns a report: checks, ok, seconds (time to restore), rpo_hours (backup age)."""
    import time
    from . import create_app
    t0 = time.monotonic()
    rep = {"checks": []}

    def check(name, ok, detail=""):
        rep["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})

    backup_db = backup_db or latest_backup(live_db)
    if not backup_db or not os.path.exists(backup_db):
        check("A backup exists", False, "No backup found — run ./backup.sh first")
        rep.update(ok=False, seconds=time.monotonic() - t0)
        return rep
    rep["backup"] = os.path.basename(backup_db)
    try:
        stamp = datetime.strptime(rep["backup"][len("prizes-"):-3], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        rep["rpo_hours"] = round((datetime.now(timezone.utc) - stamp).total_seconds() / 3600, 1)
    except ValueError:
        stamp = None
    files = backup_db.replace("prizes-", "files-").replace(".db", ".tar.gz")
    tmp = tempfile.mkdtemp(prefix="dr-drill-")
    try:
        restored = os.path.join(tmp, "prizes.db")
        shutil.copy(backup_db, restored)
        if os.path.exists(files):
            with tarfile.open(files) as tar:
                tar.extractall(tmp, filter="data")
        check("Files archive found", os.path.exists(files), os.path.basename(files))
        app = create_app({"DATABASE": restored, "UPLOAD_DIR": os.path.join(tmp, "uploads"), "TESTING": True,
                          "SMTP_HOST": "", "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "DEMO_PAYMENTS": False,
                          "DISCORD_WEBHOOK_URL": "", "JOBS_ON_REQUESTS": False, "ADMIN_MFA": True})
        check("Restored copy starts (schema and migrations apply)", True)
        client = app.test_client()
        conn = sqlite3.connect(restored)
        conn.row_factory = sqlite3.Row
        for path in ("/healthz", "/", "/competitions", "/results", "/winners", "/free-entry", "/login"):
            code = client.get(path).status_code
            check(f"Page {path}", code == 200, code)
        live = conn.execute("SELECT slug FROM competitions WHERE status='live' ORDER BY id DESC LIMIT 1").fetchone()
        drawn = conn.execute("SELECT slug FROM competitions WHERE status='drawn' ORDER BY id DESC LIMIT 1").fetchone()
        for label, row in (("a live competition", live), ("a drawn competition", drawn)):
            if row:
                code = client.get(f"/c/{row['slug']}").status_code
                check(f"Page for {label}", code == 200, code)
        missing = [r[0] for r in conn.execute("SELECT image FROM competitions WHERE image IS NOT NULL AND image!='' "
                                              "AND status IN ('live','drawn')")
                   if not os.path.exists(os.path.join(tmp, "uploads", r[0]))]
        check("Competition images restored", not missing, f"missing: {', '.join(missing[:5])}" if missing else "all present")
        with app.app_context():
            from .checks import integrity_problems
            from .db import get_db
            problems = integrity_problems(get_db())
        check("Data integrity (tickets, wallets, draws, audit chain)", not problems, "; ".join(problems[:3]))
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("users", "competitions", "tickets", "draws")}
        check("Data present", counts["users"] > 0, ", ".join(f"{v} {k}" for k, v in counts.items()))
        conn.close()
    except Exception as e:                   # the drill reports failures rather than crashing
        check("Restored copy starts (schema and migrations apply)", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rep["ok"] = all(c["ok"] for c in rep["checks"])
    rep["seconds"] = time.monotonic() - t0
    return rep
