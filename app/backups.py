"""Backups that are proven: every backup is restored into a scratch copy and checked before it counts.

  flask --app wsgi backup                 make a backup, then restore-test it
  flask --app wsgi restore-test FILE      restore-test any backup file
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
