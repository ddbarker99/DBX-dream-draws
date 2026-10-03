# Architecture

How DBX Dream Draws works, for a developer joining without the original author. Read with `docs/DATABASE.md` (tables), `docs/MECHANICS.md` (competition rules as configured) and `docs/DEPENDENCIES.md` (outside services).

## 1. The shape of it

```
 Browser ──HTTPS──▶ nginx (TLS, caching of /static and /uploads, login rate limit)
                      │
                      ▼ 127.0.0.1:8000
              ┌───────────────────┐        ┌──────────────────────┐
              │ web (gunicorn,    │        │ worker               │
              │ 3 processes ×     │        │ `flask run-jobs      │
              │ 4 threads, Flask) │        │  --loop` every 20 s  │
              └────────┬──────────┘        └──────────┬───────────┘
                       │  one SQLite file (WAL)       │
                       └──────────▶ /data/prizes.db ◀─┘
                                    /data/uploads, /data/evidence, /data/backups
   Stripe ◀── checkout sessions, refunds; webhooks ──▶ /stripe/webhook
   SMTP   ◀── emails (queued in the notifications table, retried)
```

- **One codebase, two containers** (`docker-compose.yml`): `web` serves pages; `worker` runs the background jobs. If the worker is down, the same jobs also run (throttled) at the start of web requests, so draws still happen.
- **One database**: SQLite in WAL mode. Reads are concurrent; writes are serialised. Every operation that must not race runs inside `write_txn()` (`BEGIN IMMEDIATE`), which takes the single write lock *before* checking anything, so check-then-write can't interleave.
- **No JavaScript framework**. Server-rendered Jinja templates; small inline scripts for the number picker, basket totals and instant-win reveals.

## 2. Code map (`app/`)

| File | What it owns |
|---|---|
| `__init__.py` | App factory: config from `.env`, security headers, CSRF, request timing, error handler, CLI commands (`backup`, `restore-test`, `dr-drill`, `release-check`, `run-jobs`, `make-admin`…) |
| `db.py` | Connection, `write_txn()`, schema upgrade (`MIGRATIONS`), protective triggers (`POST_TRIGGERS`), uniqueness rules (`CONSTRAINTS`) |
| `schema.sql` | Tables (see DATABASE.md) |
| `services.py` | **All business rules**: pricing (`line_price`), allocation (`_allocate`), checkout (`reserve_checkout`, `fulfil_checkout`, `expire_checkout`), wallet (`add_credit`, `balances`), withdrawals, deposits, refunds, instant wins (`pay_prize`, `reveal_ticket`, `settle_unrevealed`), closing (`close_competition`), draws (`run_draw`, `redraw`), postal entries, points, referrals, audit log |
| `routes.py` | Customer pages and the Stripe webhook. Thin: validates input, calls `services`, renders |
| `admin.py` | Competition management, postal queue, payouts, users, prizes, settings, audit viewer |
| `control.py` | Control Centre, finance, reports, targets, liability, timelines, flags, support desk, insights |
| `checks.py` | Pre-launch checks, draw-readiness checks, data-integrity checks, prize liability |
| `jobs.py` | Background jobs (`JOBS`), `run_job()` bookkeeping, `health_checks()`, anomaly detection |
| `security.py` | Device sessions, admin MFA (TOTP), step-up confirmation, idle timeout |
| `perms.py` | Roles → permissions; `@require(perm)` |
| `payments.py` | Stripe HTTP calls (no SDK) and webhook signature check |
| `reconcile.py` | Daily Stripe ↔ database comparison |
| `notify.py`, `mailer.py` | In-app notifications + email outbox; SMTP sending |
| `metrics.py` | Request counts/timing, error log with scrubbing |
| `flags.py` | Feature flags |
| `status.py` | Site status: normal / payments paused / maintenance, auto-pause on provider errors |
| `analytics.py` | Cookie-free journey counts |
| `backups.py` | Verified backups, restore test, DR drill |

**Rule:** a business rule lives in exactly one function in `services.py` (or `checks.py`). Routes may *pre-check* for a friendlier message, but the authoritative check is always repeated inside the transaction in `services`.

## 3. Key journeys

### Registration and login
`routes.signup` → password hashed (Werkzeug PBKDF2), 18+ check from date of birth, marketing/SMS opt-ins unticked by default and logged in `consent_log`, verification email (signed token, 3 days). `security.start_session` registers the device in `user_sessions`; changing the password revokes other devices (`pwv` in the cookie). Rate limits: per IP and per email (app) + per IP (nginx).

### Entering and paying (the money path)
1. **Add to basket** (`routes.basket_add`): checks the answer, per-person limit (`services.entrant_count`), picked numbers still free. The basket lives in the session — nothing is reserved yet.
2. **Checkout** (`services.reserve_checkout`, one `write_txn`): re-checks everything; allocates numbers (`_allocate`: picks or random free numbers); inserts `orders` and `tickets` with status **held**; spends site credit / deposited funds / cash first (ledger lines, ref `c<checkout>`); computes `cash_due`. An idempotency key stops a double-click creating two checkouts.
3. **Stripe Checkout** session created with `metadata.checkout_id`; the customer pays on Stripe's page.
4. **Webhook** `checkout.session.completed` (signature verified) → `fulfil_checkout` (one `write_txn`, idempotent):
   - amount must equal `cash_due`, competition still open, checkout still pending — otherwise `needs_refund`, nothing issued;
   - tickets → **issued**, orders → paid, instant prizes on those numbers locked to the ticket and paid to the wallet (games: paid on reveal), promo use counted, points awarded (once, by ref), referral settled.
   Repeated webhooks return the existing result. Credit-card payments are refunded and released (`_refuse_credit_card`).
5. **Expiry**: unpaid checkouts older than 45 minutes are released by the `expire_checkouts` job (tickets deleted, wallet spend returned). A payment arriving after that is flagged for refund.

### Competition lifecycle
`draft` → (pre-launch checks pass) → `live` → closing time passes → `close_competition`: waits for held tickets and unprocessed postal entries, then **freezes the entry list** (`entry_snapshots`, `locked_at`); triggers then forbid any ticket change → `run_draw` (automatic job or admin): draw-readiness checks, winner = `HMAC-SHA256(seed, entries_hash) mod n` over the frozen list (seed hash published before sales), permanent `draws` row, `prize_claims` row → `drawn` → claim workflow → `completed`. Redraws use attempt-numbered HMAC and keep the original. Cancelling refunds every order once (`refund_competition`).

### Instant wins
Winning numbers are fixed and sealed before sales (`instant_commitment` hash shown publicly). A bought ticket whose number matches wins; the result is fixed at payment, the reveal animation is cosmetic. Unrevealed game prizes are settled after 24 h or at close (`settle_unrevealed`).

### Free postal entry
Staff log an envelope as received (`receive_postal`) the day it arrives, then approve/reject (`process_postal`); the rules (deadline by arrival date, answer, 18+, per-person limit, sold out) decide, and accepted entries get a random ticket like any other. Closing waits for the queue.

### Wallet and withdrawals
Three balances per customer, each `SUM(credit_ledger.amount)` by kind: **cash** (withdrawable winnings), **credit** (spend-only promotional), **deposit** (card top-ups, refundable to card). Nothing overwrites a balance; every change is a ledger line with a reason and reference, and triggers make the ledger append-only. Withdrawal: two-step request → ledger debit (`w<id>`) → finance marks processing → paid (final) or returned (credit back).

### Admin operations
Roles (`perms.py`): Support, Competition manager, Finance, Administrator. MFA required; idle timeout 30 min; sensitive actions need a confirmation from the last 10 min; large wallet adjustments need an Administrator; every action audit-logged in a hash chain.

## 4. Background jobs (`jobs.py`)
All jobs are idempotent and safe to run concurrently from several processes (each does its work inside `write_txn`, and results are keyed so a second run finds nothing to do).

| Job | Every | Does |
|---|---|---|
| publish_scheduled | 30 s | Scheduled launches (pre-launch checks apply) |
| expire_checkouts | 60 s | Release unpaid reservations |
| close_competitions | 30 s | Freeze entry lists at closing |
| auto_draws | 60 s | Run due draws, announce winners |
| settle_unrevealed | 5 min | Pay unrevealed game prizes |
| email_outbox | 60 s | Send/retry emails (5 tries) |
| flag_rules | 15 min | Fraud/abuse review flags |
| health_alerts | 10 min | Email staff about failing health checks (max every 6 h per check) |
| watch_reminders | 30 min | Opt-in closing reminders |
| integrity | 6 h | Data-integrity checks |
| reconcile | 24 h | Stripe reconciliation |
| prune | 24 h | Retention clean-ups |

## 5. Safety nets, in layers
1. Application checks inside `write_txn`.
2. Database triggers and unique indexes (`db.py`): no overselling, no duplicate numbers, append-only ledgers and history, final states stay final, one payout per prize/refund/deposit.
3. Integrity job + tests asserting integrity after every test and crawl.
4. Reconciliation with Stripe; payment-reversal webhooks.
5. Audit hash chain; verified backups; DR drill.

## 6. Running it locally
```bash
pip install -r requirements.txt pytest
DATA_DIR=./data DEMO_PAYMENTS=1 ALLOW_DEV_KEY=1 ADMIN_MFA=0 flask --app wsgi run --debug
flask --app wsgi make-admin you@example.com
python tests/qa_crawl.py --serve 5000      # or: a seeded copy with sample competitions
```
`docs/TESTING.md` lists every test suite.
