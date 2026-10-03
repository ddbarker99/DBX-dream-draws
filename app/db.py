import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import current_app, g


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; we manage transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def write_txn():
    """Exclusive write transaction. SQLite allows one writer at a time, so
    anything checked inside this block cannot change until it commits —
    this is what stops tickets being oversold."""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# Columns added after v1. Applied with ALTER TABLE only if missing, so the
# live database upgrades in place and nothing is lost.
MIGRATIONS = [
    ("users", "referral_code", "TEXT"),
    ("users", "referred_by", "INTEGER REFERENCES users(id)"),
    ("users", "daily_limit", "INTEGER"),
    ("users", "weekly_limit", "INTEGER"),
    ("users", "marketing", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "pending_daily", "INTEGER"),     # -1 = remove limit
    ("users", "pending_weekly", "INTEGER"),
    ("competitions", "category", "TEXT NOT NULL DEFAULT 'other'"),
    ("competitions", "featured", "INTEGER NOT NULL DEFAULT 0"),
    ("competitions", "prize_value", "INTEGER NOT NULL DEFAULT 0"),
    ("competitions", "discount_tiers", "TEXT NOT NULL DEFAULT ''"),
    ("competitions", "live_url", "TEXT"),
    ("competitions", "instant_salt", "TEXT"),
    ("competitions", "instant_hash", "TEXT"),
    ("competitions", "winner_photo", "TEXT"),
    ("competitions", "winner_quote", "TEXT"),
    ("orders", "checkout_id", "INTEGER REFERENCES checkouts(id)"),
    ("orders", "discount", "INTEGER NOT NULL DEFAULT 0"),
    ("tickets", "status", "TEXT NOT NULL DEFAULT 'issued'"),
    ("tickets", "revealed_at", "TEXT"),                       # games: when the player revealed it
    ("competitions", "game_type", "TEXT NOT NULL DEFAULT ''"),  # '' = prize draw, else scratch/spin/box
    # v4: cash winnings, loyalty, verification, free daily game, auto draw
    ("credit_ledger", "kind", "TEXT NOT NULL DEFAULT 'credit'"),      # 'cash' (withdrawable) | 'credit' (spend only)
    ("instant_prizes", "prize_type", "TEXT NOT NULL DEFAULT ''"),     # cash | credit | physical ('' = legacy)
    ("checkouts", "cash_used", "INTEGER NOT NULL DEFAULT 0"),
    ("withdrawals", "method", "TEXT"),
    ("withdrawals", "account_name", "TEXT"),
    ("withdrawals", "sort_code", "TEXT"),
    ("withdrawals", "account_number", "TEXT"),
    ("withdrawals", "paypal_email", "TEXT"),
    ("users", "phone", "TEXT"),
    ("users", "email_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "points", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "points_lifetime", "INTEGER NOT NULL DEFAULT 0"),
    ("competitions", "auto_draw", "INTEGER NOT NULL DEFAULT 1"),
    ("competitions", "free_daily", "INTEGER NOT NULL DEFAULT 0"),
    # v6: scheduled launches
    ("competitions", "starts_at", "TEXT"),
    ("competitions", "scheduled", "INTEGER NOT NULL DEFAULT 0"),
    # v7: deposits
    ("checkouts", "deposit_used", "INTEGER NOT NULL DEFAULT 0"),
    # v8: integrity, postal workflow, admin roles, winner consent
    ("checkouts", "idem_key", "TEXT"),                        # stops a double-click creating two checkouts
    ("checkouts", "pay_url", "TEXT"),                         # the payment page for this checkout
    ("postal_entries", "received_at", "TEXT"),                # the date the envelope arrived
    ("postal_entries", "user_id", "INTEGER REFERENCES users(id)"),  # matched to an account by email
    ("postal_entries", "status", "TEXT NOT NULL DEFAULT 'accepted'"),  # accepted | rejected
    ("postal_entries", "reject_reason", "TEXT"),
    ("postal_entries", "dob", "TEXT"),
    ("postal_entries", "phone", "TEXT"),
    ("users", "admin_role", "TEXT NOT NULL DEFAULT 'owner'"),  # owner (everything) | staff (no money or accounts)
    ("competitions", "winner_consent_at", "TEXT"),           # winner agreed to their photo/quote being shown
    ("competitions", "winner_consent_by", "INTEGER"),
    # v9: lifecycle, MFA, redraws
    ("competitions", "locked_at", "TEXT"),                   # final entry list frozen
    ("competitions", "purging", "INTEGER NOT NULL DEFAULT 0"),  # set only while deleting test data
    ("competitions", "completed_at", "TEXT"),
    ("users", "mfa_secret", "TEXT"),
    ("users", "mfa_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "mfa_recovery", "TEXT"),                       # JSON list of hashed one-time recovery codes
    ("draws", "redraw_of", "INTEGER"),
    ("draws", "reason", "TEXT"),
    ("draws", "winner_user_id", "INTEGER"),
    ("draws", "snapshot_id", "INTEGER"),
]

# Triggers that use columns added by MIGRATIONS, so they're created after them.
POST_TRIGGERS = [
    # Once the final entry list is frozen, tickets can't be added, removed or reassigned.
    """CREATE TRIGGER IF NOT EXISTS tickets_locked_insert BEFORE INSERT ON tickets
       WHEN (SELECT locked_at FROM competitions WHERE id=NEW.competition_id) IS NOT NULL
       BEGIN SELECT RAISE(ABORT, 'This competition has closed: its entry list is final.'); END""",
    """CREATE TRIGGER IF NOT EXISTS tickets_locked_delete BEFORE DELETE ON tickets
       WHEN (SELECT locked_at IS NOT NULL AND purging=0 FROM competitions WHERE id=OLD.competition_id)
       BEGIN SELECT RAISE(ABORT, 'This competition has closed: its entry list is final.'); END""",
    """CREATE TRIGGER IF NOT EXISTS tickets_locked_update BEFORE UPDATE OF number, status, user_id, competition_id ON tickets
       WHEN (SELECT locked_at FROM competitions WHERE id=OLD.competition_id) IS NOT NULL
       BEGIN SELECT RAISE(ABORT, 'This competition has closed: its entry list is final.'); END""",
    """CREATE TRIGGER IF NOT EXISTS comp_lock_permanent BEFORE UPDATE OF locked_at ON competitions
       WHEN OLD.locked_at IS NOT NULL AND NEW.locked_at IS NOT OLD.locked_at
       BEGIN SELECT RAISE(ABORT, 'A closed competition cannot be reopened.'); END""",
    # Result fields of a drawn competition only change through a recorded redraw.
    "DROP TRIGGER IF EXISTS comp_result_locked",
    """CREATE TRIGGER comp_result_locked BEFORE UPDATE ON competitions
       WHEN OLD.status = 'drawn' AND (NEW.status IS NOT OLD.status OR NEW.entries_hash IS NOT OLD.entries_hash
            OR NEW.drawn_at IS NOT OLD.drawn_at OR NEW.seed IS NOT OLD.seed OR NEW.ends_at IS NOT OLD.ends_at
            OR (NEW.winner_ticket_id IS NOT OLD.winner_ticket_id AND NEW.winner_ticket_id IS NOT
                (SELECT winning_ticket_id FROM draws WHERE competition_id=OLD.id ORDER BY id DESC LIMIT 1)))
       BEGIN SELECT RAISE(ABORT, 'Draw results are permanent.'); END""",
]

POST_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_orders_comp ON orders(competition_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_checkout ON orders(checkout_id);
CREATE INDEX IF NOT EXISTS ix_tickets_user ON tickets(user_id);
CREATE INDEX IF NOT EXISTS ix_tickets_comp_status ON tickets(competition_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_ref ON users(referral_code);
CREATE INDEX IF NOT EXISTS ix_ledger_ref ON credit_ledger(ref);
CREATE UNIQUE INDEX IF NOT EXISTS ux_checkout_idem ON checkouts(user_id, idem_key);
CREATE INDEX IF NOT EXISTS ix_postal_comp ON postal_entries(competition_id, status);
"""


def init_db(path):
    import secrets
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = _connect(path)
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        conn.executescript(f.read())
    conn.execute("BEGIN IMMEDIATE")  # several app workers may start at once
    try:
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        for stmt in POST_INDEXES.strip().split(";"):
            if stmt.strip():
                conn.execute(stmt)
        for stmt in POST_TRIGGERS:
            conn.execute(stmt)
        # Points earned before the points history existed get one opening line, so totals always add up.
        conn.execute("INSERT INTO points_ledger (user_id, points, reason, created_at) SELECT id, points, "
                     "'Opening balance (points earned before the history was kept)', strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                     "FROM users WHERE points>0 AND id NOT IN (SELECT user_id FROM points_ledger)")
        conn.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at, status, rewarded_at) "
                     "SELECT u.referred_by, u.id, u.created_at, CASE WHEN EXISTS (SELECT 1 FROM credit_ledger l WHERE l.ref='u' || u.id) "
                     "THEN 'rewarded' ELSE 'joined' END, NULL FROM users u WHERE u.referred_by IS NOT NULL")
        conn.execute("UPDATE users SET admin_role=CASE admin_role WHEN 'owner' THEN 'admin' WHEN 'staff' THEN 'competitions' "
                     "ELSE admin_role END WHERE admin_role IN ('owner','staff')")
        conn.execute("UPDATE postal_entries SET status='rejected', reject_reason='Wrong answer' "
                     "WHERE answer_correct=0 AND status='accepted' AND reject_reason IS NULL")
        for r in conn.execute("SELECT id FROM users WHERE referral_code IS NULL").fetchall():
            conn.execute("UPDATE users SET referral_code=? WHERE id=?", (secrets.token_hex(4).upper(), r["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
