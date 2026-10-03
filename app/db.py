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
    ("checkouts", "device", "TEXT"),                         # mobile | tablet | desktop, for journey counts only
    ("competitions", "question_mode", "TEXT NOT NULL DEFAULT 'multiple_choice'"),  # multiple_choice | none
    # v10: communication preferences, reminders, support desk
    ("users", "marketing_sms", "INTEGER NOT NULL DEFAULT 0"),      # opt-in only
    ("users", "reminder_emails", "INTEGER NOT NULL DEFAULT 1"),    # reminders the customer asked for, by email
    ("watchlist", "remind_close", "INTEGER NOT NULL DEFAULT 1"),
    ("watchlist", "remind_result", "INTEGER NOT NULL DEFAULT 0"),
    ("cases", "priority", "TEXT NOT NULL DEFAULT 'normal'"),        # high | normal | low
    ("cases", "withdrawal_id", "INTEGER"),
    ("cases", "locked_by", "INTEGER"),                             # staff member currently working on it
    ("cases", "locked_at", "TEXT"),
    # v11: tamper-evident audit log, feature flags
    ("audit_log", "prev_hash", "TEXT"),
    ("audit_log", "row_hash", "TEXT"),
    # v12: production hardening
    ("checkouts", "payment_intent", "TEXT"),                  # Stripe payment, to match refunds and chargebacks
    ("cases", "reason", "TEXT"),                              # what the customer was actually confused about
    # v13: customer experience & growth
    ("prize_claims", "delivery_name", "TEXT"),                # winner's delivery details, given on their prize page
    ("prize_claims", "delivery_address", "TEXT"),
    ("prize_claims", "delivery_phone", "TEXT"),
    ("prize_claims", "choice_at", "TEXT"),
    ("promo_codes", "starts_at", "TEXT"),                     # promotion scheduling
    ("promo_codes", "comp_ids", "TEXT"),                      # comma-separated competition ids it applies to ('' = all)
    ("promo_codes", "category", "TEXT"),                      # or only this category
    ("promo_codes", "new_customers", "INTEGER NOT NULL DEFAULT 0"),  # only customers with no paid order yet
    ("promo_codes", "description", "TEXT"),
]

# Triggers that use columns added by MIGRATIONS, so they're created after them.
POST_TRIGGERS = [
    # Money and points histories are permanent: mistakes are corrected with a new adjustment line.
    """CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON credit_ledger
       BEGIN SELECT RAISE(ABORT, 'Wallet history is permanent: add an adjustment instead.'); END""",
    """CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON credit_ledger
       WHEN NOT EXISTS (SELECT 1 FROM maintenance_unlock)
       BEGIN SELECT RAISE(ABORT, 'Wallet history is permanent: add an adjustment instead.'); END""",
    """CREATE TRIGGER IF NOT EXISTS points_no_update BEFORE UPDATE ON points_ledger
       BEGIN SELECT RAISE(ABORT, 'Points history is permanent: add an adjustment instead.'); END""",
    """CREATE TRIGGER IF NOT EXISTS points_no_delete BEFORE DELETE ON points_ledger
       WHEN NOT EXISTS (SELECT 1 FROM maintenance_unlock)
       BEGIN SELECT RAISE(ABORT, 'Points history is permanent: add an adjustment instead.'); END""",
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
    # Draw records and frozen entry lists can only be removed with their (test) competition.
    """CREATE TRIGGER IF NOT EXISTS draws_no_delete BEFORE DELETE ON draws
       WHEN (SELECT purging FROM competitions WHERE id=OLD.competition_id) = 0
       BEGIN SELECT RAISE(ABORT, 'Draw records are permanent.'); END""",
    """CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON entry_snapshots
       WHEN (SELECT purging FROM competitions WHERE id=OLD.competition_id) = 0
       BEGIN SELECT RAISE(ABORT, 'Entry snapshots are permanent.'); END""",
    # Result fields of a drawn competition only change through a recorded redraw.
    "DROP TRIGGER IF EXISTS comp_result_locked",
    """CREATE TRIGGER comp_result_locked BEFORE UPDATE ON competitions
       WHEN OLD.status = 'drawn' AND (NEW.status IS NOT OLD.status OR NEW.entries_hash IS NOT OLD.entries_hash
            OR NEW.drawn_at IS NOT OLD.drawn_at OR NEW.seed IS NOT OLD.seed OR NEW.ends_at IS NOT OLD.ends_at
            OR (NEW.winner_ticket_id IS NOT OLD.winner_ticket_id AND NEW.winner_ticket_id IS NOT
                (SELECT winning_ticket_id FROM draws WHERE competition_id=OLD.id ORDER BY id DESC LIMIT 1)))
       BEGIN SELECT RAISE(ABORT, 'Draw results are permanent.'); END""",
]

# The database itself refuses impossible states, so a bug in the application can't create them.
POST_TRIGGERS += [
    # Tickets: never more than the competition's maximum, never outside its number range, only known states.
    """CREATE TRIGGER IF NOT EXISTS tickets_no_oversell BEFORE INSERT ON tickets
       WHEN (SELECT COUNT(*) FROM tickets WHERE competition_id=NEW.competition_id)
            >= (SELECT max_tickets FROM competitions WHERE id=NEW.competition_id)
       BEGIN SELECT RAISE(ABORT, 'Sold out: no tickets left.'); END""",
    """CREATE TRIGGER IF NOT EXISTS tickets_valid BEFORE INSERT ON tickets
       WHEN NEW.number < 1 OR NEW.number > (SELECT max_tickets FROM competitions WHERE id=NEW.competition_id)
            OR NEW.status NOT IN ('held', 'issued')
       BEGIN SELECT RAISE(ABORT, 'Invalid ticket.'); END""",
    """CREATE TRIGGER IF NOT EXISTS tickets_status_valid BEFORE UPDATE OF status ON tickets
       WHEN NEW.status NOT IN ('held', 'issued') OR (OLD.status = 'issued' AND NEW.status != 'issued')
       BEGIN SELECT RAISE(ABORT, 'An issued ticket stays issued.'); END""",
    # Money rows must make sense.
    """CREATE TRIGGER IF NOT EXISTS ledger_valid BEFORE INSERT ON credit_ledger
       WHEN NEW.amount = 0 OR NEW.kind NOT IN ('cash', 'credit', 'deposit')
       BEGIN SELECT RAISE(ABORT, 'Invalid wallet entry.'); END""",
    """CREATE TRIGGER IF NOT EXISTS withdrawals_valid BEFORE INSERT ON withdrawals
       WHEN NEW.amount <= 0 BEGIN SELECT RAISE(ABORT, 'Invalid withdrawal.'); END""",
    """CREATE TRIGGER IF NOT EXISTS withdrawals_final BEFORE UPDATE ON withdrawals
       WHEN NEW.amount != OLD.amount OR NEW.user_id != OLD.user_id
            OR (OLD.status IN ('paid', 'rejected') AND NEW.status != OLD.status)
       BEGIN SELECT RAISE(ABORT, 'A completed withdrawal cannot be changed.'); END""",
    """CREATE TRIGGER IF NOT EXISTS orders_valid BEFORE INSERT ON orders
       WHEN NEW.quantity <= 0 OR NEW.amount < 0 BEGIN SELECT RAISE(ABORT, 'Invalid order.'); END""",
    # A paid checkout or deposit stays paid (refunds are separate records); amounts never change.
    """CREATE TRIGGER IF NOT EXISTS checkouts_final BEFORE UPDATE ON checkouts
       WHEN NEW.cash_due != OLD.cash_due OR NEW.user_id != OLD.user_id OR (OLD.status = 'paid' AND NEW.status != 'paid')
            OR (OLD.status IN ('credit_refused', 'refunded') AND NEW.status != OLD.status)
       BEGIN SELECT RAISE(ABORT, 'A completed payment cannot be changed.'); END""",
    """CREATE TRIGGER IF NOT EXISTS deposits_final BEFORE UPDATE ON deposits
       WHEN NEW.amount != OLD.amount OR NEW.user_id != OLD.user_id OR NEW.refunded < OLD.refunded
            OR NEW.refunded > NEW.amount OR (OLD.status = 'paid' AND NEW.status != 'paid')
       BEGIN SELECT RAISE(ABORT, 'A completed deposit cannot be changed.'); END""",
    # An instant prize, once won, belongs to that ticket for good; paid stays paid.
    """CREATE TRIGGER IF NOT EXISTS instant_prize_final BEFORE UPDATE ON instant_prizes
       WHEN (OLD.ticket_id IS NOT NULL AND NEW.ticket_id IS NOT OLD.ticket_id) OR (OLD.fulfilled = 1 AND NEW.fulfilled = 0)
            OR (OLD.ticket_id IS NOT NULL AND (NEW.number != OLD.number OR NEW.credit_amount != OLD.credit_amount))
       BEGIN SELECT RAISE(ABORT, 'A won prize cannot be changed.'); END""",
    # Draw records: the winner must be an issued ticket in that competition.
    """CREATE TRIGGER IF NOT EXISTS draws_valid BEFORE INSERT ON draws
       WHEN (SELECT competition_id FROM tickets WHERE id=NEW.winning_ticket_id AND status='issued') IS NOT NEW.competition_id
       BEGIN SELECT RAISE(ABORT, 'The winning ticket is not a valid entry in this competition.'); END""",
    # Refund records: amounts and the order never change; only a card refund's outcome is filled in once.
    """CREATE TRIGGER IF NOT EXISTS refunds_final BEFORE UPDATE ON refunds
       WHEN NEW.order_id != OLD.order_id OR NEW.card_amount != OLD.card_amount OR NEW.wallet_amount != OLD.wallet_amount
            OR NEW.user_id != OLD.user_id OR OLD.status != 'card_pending'
       BEGIN SELECT RAISE(ABORT, 'Refund records are permanent.'); END""",
    """CREATE TRIGGER IF NOT EXISTS refunds_no_delete BEFORE DELETE ON refunds
       WHEN NOT EXISTS (SELECT 1 FROM maintenance_unlock)
       BEGIN SELECT RAISE(ABORT, 'Refund records are permanent.'); END""",
    # Prize-claim history and processed postal entries are permanent.
    """CREATE TRIGGER IF NOT EXISTS claim_events_no_update BEFORE UPDATE ON claim_events
       BEGIN SELECT RAISE(ABORT, 'Prize-claim history is permanent.'); END""",
    """CREATE TRIGGER IF NOT EXISTS claim_events_no_delete BEFORE DELETE ON claim_events
       WHEN NOT EXISTS (SELECT 1 FROM maintenance_unlock)
            AND (SELECT purging FROM competitions c JOIN prize_claims p ON p.competition_id=c.id WHERE p.id=OLD.claim_id) = 0
       BEGIN SELECT RAISE(ABORT, 'Prize-claim history is permanent.'); END""",
    """CREATE TRIGGER IF NOT EXISTS postal_decision_final BEFORE UPDATE OF status, answer_correct, received_at, competition_id ON postal_entries
       WHEN OLD.status IN ('accepted', 'rejected') AND (NEW.status IS NOT OLD.status OR NEW.answer_correct IS NOT OLD.answer_correct
            OR NEW.received_at IS NOT OLD.received_at OR NEW.competition_id IS NOT OLD.competition_id)
       BEGIN SELECT RAISE(ABORT, 'A processed postal entry cannot be changed.'); END""",
]

# Uniqueness rules that stop anything being processed twice. Created one by one: if old data already breaks one,
# the site still starts, the rule is skipped and System health → Data integrity says which (fix the data, restart).
CONSTRAINTS = [
    ("ux_ledger_once", "CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_once ON credit_ledger(ref, kind, amount > 0) "
                       "WHERE ref IS NOT NULL AND ref NOT LIKE 'admin%'"),   # each prize, refund, deposit… paid once
    ("ux_points_once", "CREATE UNIQUE INDEX IF NOT EXISTS ux_points_once ON points_ledger(ref, points > 0) WHERE ref IS NOT NULL"),
    ("ux_deposit_session", "CREATE UNIQUE INDEX IF NOT EXISTS ux_deposit_session ON deposits(stripe_session_id) "
                           "WHERE stripe_session_id IS NOT NULL"),
    ("ux_claim_per_draw", "CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_per_draw ON prize_claims(draw_id) WHERE draw_id IS NOT NULL"),
    ("ux_prize_per_ticket", "CREATE UNIQUE INDEX IF NOT EXISTS ux_prize_per_ticket ON instant_prizes(ticket_id) WHERE ticket_id IS NOT NULL"),
    ("ux_ticket_per_postal", "CREATE UNIQUE INDEX IF NOT EXISTS ux_ticket_per_postal ON tickets(postal_entry_id) "
                             "WHERE postal_entry_id IS NOT NULL"),
    ("ux_draw_winner", "CREATE UNIQUE INDEX IF NOT EXISTS ux_draw_winner ON draws(competition_id, winning_ticket_id)"),
    ("ux_withdrawal_once", "CREATE UNIQUE INDEX IF NOT EXISTS ux_withdrawal_ledger ON credit_ledger(ref) "
                           "WHERE ref LIKE 'w%' AND amount < 0"),
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


def init_db(path, release=None):
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
        _chain_old_audit_rows(conn)
        skipped = []
        for name, sql in CONSTRAINTS:
            conn.execute("SAVEPOINT c")
            try:
                conn.execute(sql)
                conn.execute("RELEASE c")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK TO c")
                conn.execute("RELEASE c")
                skipped.append(name)
        conn.execute("INSERT INTO settings (key, value) VALUES ('constraints_skipped', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (",".join(skipped),))
        if release:
            cols = sum(len(conn.execute(f"PRAGMA table_info({t})").fetchall()) for (t,) in
                       conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            conn.execute("INSERT OR IGNORE INTO releases (release, first_seen, schema_columns, constraints_skipped) VALUES "
                         "(?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?)", (release, cols, ",".join(skipped)))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()


def _chain_old_audit_rows(conn):
    """Audit entries written before the hash chain existed get hashed once, in order, so the whole log is covered."""
    if not conn.execute("SELECT 1 FROM audit_log WHERE row_hash IS NULL LIMIT 1").fetchone():
        return
    from .services import audit_hash
    conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
    prev = ""
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall():
        if r["row_hash"] is None:
            h = audit_hash(prev, r["created_at"], r["actor_id"], r["actor_email"], r["action"], r["target"], r["detail"])
            conn.execute("UPDATE audit_log SET prev_hash=?, row_hash=? WHERE id=?", (prev, h, r["id"]))
            prev = h
        else:
            prev = r["row_hash"]
    conn.execute("CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log "
                 "BEGIN SELECT RAISE(ABORT, 'The audit log is append-only.'); END")
