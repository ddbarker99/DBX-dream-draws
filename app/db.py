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
]

POST_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_orders_comp ON orders(competition_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_checkout ON orders(checkout_id);
CREATE INDEX IF NOT EXISTS ix_tickets_user ON tickets(user_id);
CREATE INDEX IF NOT EXISTS ix_tickets_comp_status ON tickets(competition_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_ref ON users(referral_code);
CREATE INDEX IF NOT EXISTS ix_ledger_ref ON credit_ledger(ref);
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
        for r in conn.execute("SELECT id FROM users WHERE referral_code IS NULL").fetchall():
            conn.execute("UPDATE users SET referral_code=? WHERE id=?", (secrets.token_hex(4).upper(), r["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
