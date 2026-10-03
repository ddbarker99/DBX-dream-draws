-- Base tables (v1). New columns are added by db.py MIGRATIONS so existing
-- databases upgrade in place without losing data.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name             TEXT NOT NULL,
    password_hash    TEXT NOT NULL,
    dob              TEXT NOT NULL,
    is_admin         INTEGER NOT NULL DEFAULT 0,
    monthly_limit    INTEGER NOT NULL DEFAULT 25000,
    pending_limit    INTEGER,
    pending_limit_at TEXT,
    excluded_until   TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competitions (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    image           TEXT,
    cash_alternative TEXT,
    ticket_price    INTEGER NOT NULL,
    max_tickets     INTEGER NOT NULL,
    max_per_user    INTEGER NOT NULL DEFAULT 50,
    ends_at         TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer_a        TEXT NOT NULL,
    answer_b        TEXT NOT NULL,
    answer_c        TEXT NOT NULL,
    correct         TEXT NOT NULL CHECK (correct IN ('a','b','c')),
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','drawn','cancelled')),
    seed            TEXT NOT NULL,
    seed_hash       TEXT NOT NULL,
    entries_hash    TEXT,
    winner_ticket_id INTEGER,
    drawn_at        TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    competition_id    INTEGER NOT NULL REFERENCES competitions(id),
    quantity          INTEGER NOT NULL,
    amount            INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','expired')),
    stripe_session_id TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    paid_at           TEXT
);

CREATE TABLE IF NOT EXISTS postal_entries (
    id             INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    name           TEXT NOT NULL,
    email          TEXT NOT NULL,
    address        TEXT NOT NULL,
    answer_correct INTEGER NOT NULL DEFAULT 1,
    added_by       INTEGER REFERENCES users(id),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    competition_id  INTEGER NOT NULL REFERENCES competitions(id),
    number          INTEGER NOT NULL,
    user_id         INTEGER REFERENCES users(id),
    order_id        INTEGER REFERENCES orders(id),
    postal_entry_id INTEGER REFERENCES postal_entries(id),
    created_at      TEXT NOT NULL,
    UNIQUE (competition_id, number)
);

-- ---------- v2 tables ----------

-- One payment covering one or more orders (basket).
CREATE TABLE IF NOT EXISTS checkouts (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    subtotal          INTEGER NOT NULL,            -- after multi-buy discounts
    promo_id          INTEGER REFERENCES promo_codes(id),
    promo_discount    INTEGER NOT NULL DEFAULT 0,
    credit_used       INTEGER NOT NULL DEFAULT 0,
    cash_due          INTEGER NOT NULL,            -- charged by card
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | expired | needs_refund
    stripe_session_id TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    paid_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_checkouts_user ON checkouts(user_id, status);

CREATE TABLE IF NOT EXISTS instant_prizes (
    id             INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    title          TEXT NOT NULL,
    value          INTEGER NOT NULL DEFAULT 0,     -- pence, for display/stats
    credit_amount  INTEGER NOT NULL DEFAULT 0,     -- pence auto-paid to wallet (0 = physical prize)
    number         INTEGER NOT NULL,
    ticket_id      INTEGER REFERENCES tickets(id),
    won_at         TEXT,
    fulfilled      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (competition_id, number)
);

-- Every wallet movement. Balance = SUM(amount).
CREATE TABLE IF NOT EXISTS credit_ledger (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    amount     INTEGER NOT NULL,                   -- pence, + or -
    reason     TEXT NOT NULL,
    ref        TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_user ON credit_ledger(user_id);

CREATE TABLE IF NOT EXISTS promo_codes (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    percent    INTEGER NOT NULL DEFAULT 0,
    fixed      INTEGER NOT NULL DEFAULT 0,         -- pence
    min_spend  INTEGER NOT NULL DEFAULT 0,
    max_uses   INTEGER,
    per_user   INTEGER NOT NULL DEFAULT 1,
    uses       INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    amount     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'requested',  -- requested | paid | rejected
    note       TEXT,
    created_at TEXT NOT NULL,
    done_at    TEXT
);

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- v7: money a player adds to their wallet by card. Spendable on entries; unspent deposits are
-- refunded to the card they came from (never paid out to a bank account).
CREATE TABLE IF NOT EXISTS deposits (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    amount            INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | expired | credit_refused | needs_refund
    stripe_session_id TEXT,
    payment_intent    TEXT,
    refunded          INTEGER NOT NULL DEFAULT 0,        -- pence refunded back to the card so far
    created_at        TEXT NOT NULL,
    paid_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_deposits_user ON deposits(user_id, status);

-- v8: integrity. A permanent snapshot of every draw, and an append-only log of sensitive actions.
-- Triggers stop either being edited afterwards, and stop a drawn competition's result changing.
CREATE TABLE IF NOT EXISTS draws (
    id                INTEGER PRIMARY KEY,
    competition_id    INTEGER NOT NULL,
    drawn_at          TEXT NOT NULL,
    method            TEXT NOT NULL,              -- 'automatic' | 'manual'
    run_by            INTEGER,                    -- admin user id for manual draws
    seed              TEXT NOT NULL,
    seed_hash         TEXT NOT NULL,
    entries_hash      TEXT NOT NULL,
    entry_count       INTEGER NOT NULL,
    winning_index     INTEGER NOT NULL,
    winning_number    INTEGER NOT NULL,
    winning_ticket_id INTEGER NOT NULL,
    entries           TEXT NOT NULL               -- JSON list of every eligible ticket number, sorted
);
CREATE INDEX IF NOT EXISTS ix_draws_comp ON draws(competition_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    actor_id    INTEGER,                          -- NULL = the system (automatic draw, webhook, ...)
    actor_email TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_log(target);

CREATE TRIGGER IF NOT EXISTS draws_no_update BEFORE UPDATE ON draws
BEGIN SELECT RAISE(ABORT, 'Draw records are permanent.'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'The audit log is append-only.'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'The audit log is append-only.'); END;

-- comp_result_locked is created in db.py (POST_TRIGGERS) because it refers to later columns.

-- v9: sessions, entry snapshots, prize claims
CREATE TABLE IF NOT EXISTS user_sessions (
    sid        TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    ip         TEXT,
    agent      TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON user_sessions(user_id);

-- The final entry list, frozen when a competition closes. Draws are made from this.
CREATE TABLE IF NOT EXISTS entry_snapshots (
    id             INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL,
    taken_at       TEXT NOT NULL,
    entry_count    INTEGER NOT NULL,
    paid_count     INTEGER NOT NULL,
    postal_count   INTEGER NOT NULL,
    entries_hash   TEXT NOT NULL,
    entries        TEXT NOT NULL                 -- JSON list of ticket numbers, sorted
);
CREATE INDEX IF NOT EXISTS ix_snap_comp ON entry_snapshots(competition_id);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON entry_snapshots
BEGIN SELECT RAISE(ABORT, 'Entry snapshots are permanent.'); END;

-- A main-draw winner's journey from selection to receiving the prize.
CREATE TABLE IF NOT EXISTS prize_claims (
    id             INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL,
    draw_id        INTEGER,
    ticket_id      INTEGER NOT NULL,
    user_id        INTEGER,
    status         TEXT NOT NULL DEFAULT 'selected',
    prize_choice   TEXT,                         -- 'prize' | 'cash' (cash alternative)
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claims_comp ON prize_claims(competition_id);
CREATE TABLE IF NOT EXISTS claim_events (
    id         INTEGER PRIMARY KEY,
    claim_id   INTEGER NOT NULL REFERENCES prize_claims(id),
    created_at TEXT NOT NULL,
    actor_id   INTEGER,
    status     TEXT,
    note       TEXT,
    evidence   TEXT                              -- file name in DATA_DIR/evidence (never public)
);
CREATE TRIGGER IF NOT EXISTS claim_events_no_update BEFORE UPDATE ON claim_events
BEGIN SELECT RAISE(ABORT, 'Claim history is append-only.'); END;

-- v9: support cases, review flags, background job runs, notifications
CREATE TABLE IF NOT EXISTS cases (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER REFERENCES users(id),
    name           TEXT NOT NULL,
    email          TEXT NOT NULL,
    topic          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',   -- open | waiting (on customer) | resolved
    competition_id INTEGER,
    checkout_id    INTEGER,
    assigned_to    INTEGER,
    resolution     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cases_status ON cases(status, updated_at);
CREATE TABLE IF NOT EXISTS case_notes (
    id         INTEGER PRIMARY KEY,
    case_id    INTEGER NOT NULL REFERENCES cases(id),
    created_at TEXT NOT NULL,
    actor_id   INTEGER,
    kind       TEXT NOT NULL,                     -- customer | reply | internal
    body       TEXT NOT NULL
);

-- Things a rule noticed that a person should look at. Never an automatic accusation or ban.
CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL,                    -- e.g. user:12 or phone:0770…
    detail      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',     -- open | reviewed | dismissed
    created_at  TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    note        TEXT,
    UNIQUE (kind, subject)
);

-- Every automated process records each run: did it run, when, did it succeed, what did it change.
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    changed     TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs ON job_runs(job, id);

-- Transactional emails and in-account notifications. dedupe_key makes a retried job harmless.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    email       TEXT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    link        TEXT,
    dedupe_key  TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    read_at     TEXT,
    email_status TEXT,                            -- NULL (in-app only) | queued | sent | failed
    email_tries INTEGER NOT NULL DEFAULT 0,
    email_error TEXT,
    sent_at     TEXT,
    email_payload TEXT                            -- JSON for the mailer
);
CREATE INDEX IF NOT EXISTS ix_notes_user ON notifications(user_id, id);
CREATE INDEX IF NOT EXISTS ix_notes_email ON notifications(email_status);

-- Customers' saved competitions.
CREATE TABLE IF NOT EXISTS watchlist (
    user_id        INTEGER NOT NULL REFERENCES users(id),
    competition_id INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (user_id, competition_id)
);

-- Every DBX Points movement, so points never appear or disappear without a reason.
CREATE TABLE IF NOT EXISTS points_ledger (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    points     INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    ref        TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_points_user ON points_ledger(user_id, id);

-- Cookie-free funnel counts: one row per day, step and device type. No identifiers.
CREATE TABLE IF NOT EXISTS funnel_counts (
    day    TEXT NOT NULL,
    step   TEXT NOT NULL,
    device TEXT NOT NULL,
    comp_id INTEGER NOT NULL DEFAULT 0,
    n      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, step, device, comp_id)
);

-- Latest state of each background job (job_runs keeps the history of runs that changed something or failed).
CREATE TABLE IF NOT EXISTS job_status (
    job          TEXT PRIMARY KEY,
    last_started TEXT,
    last_ok      TEXT,
    last_error_at TEXT,
    last_error   TEXT,
    last_changed TEXT
);
