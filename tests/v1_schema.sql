PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name             TEXT NOT NULL,
    password_hash    TEXT NOT NULL,
    dob              TEXT NOT NULL,              -- YYYY-MM-DD
    is_admin         INTEGER NOT NULL DEFAULT 0,
    monthly_limit    INTEGER NOT NULL DEFAULT 25000,  -- pence
    pending_limit    INTEGER,                    -- requested increase (pence)
    pending_limit_at TEXT,                       -- UTC ISO; increase applies after this
    excluded_until   TEXT,                       -- UTC ISO; self-exclusion
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competitions (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    image           TEXT,                        -- filename in uploads
    cash_alternative TEXT,
    ticket_price    INTEGER NOT NULL,            -- pence
    max_tickets     INTEGER NOT NULL,
    max_per_user    INTEGER NOT NULL DEFAULT 50,
    ends_at         TEXT NOT NULL,               -- UTC ISO
    question        TEXT NOT NULL,
    answer_a        TEXT NOT NULL,
    answer_b        TEXT NOT NULL,
    answer_c        TEXT NOT NULL,
    correct         TEXT NOT NULL CHECK (correct IN ('a','b','c')),
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','drawn','cancelled')),
    seed            TEXT NOT NULL,               -- secret until drawn
    seed_hash       TEXT NOT NULL,               -- published from day one
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
    amount            INTEGER NOT NULL,          -- pence
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','expired')),
    stripe_session_id TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    paid_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_comp ON orders(competition_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id, status);

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
CREATE INDEX IF NOT EXISTS ix_tickets_user ON tickets(user_id);
