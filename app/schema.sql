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
