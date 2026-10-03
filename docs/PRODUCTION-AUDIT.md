# Production technical audit (Phase 4)

Date: 3 October 2026 · Scope: the whole production system · Method: code review of every module, automated tests written to attack each area (`tests/`), load and concurrency testing, failure injection. For each area: how it works, what was verified, what was fixed in this phase, and what risk remains.

Legend: ✅ verified by automated tests · 🔧 fixed/added in Phase 4 · ⚠️ residual risk / action for the owner

## Summary
DBX is a single Flask application with one SQLite database, a background worker and nginx in front, on one VPS. Money and competition outcomes are protected in three layers — checks inside exclusive transactions, database triggers/unique rules, and continuous integrity/reconciliation checks — and every money-critical journey is covered end to end by tests that run on every change. The main residual risks are operational (single server, owner-run monitoring) and need an independent penetration test before high-volume launch.

## Areas

### Frontend
Server-rendered pages, self-hosted fonts/scripts, no third-party requests. ✅ crawler (no broken links/errors, labels, headings) · ✅ keyboard + axe-core (WCAG AA) at 390/1280 px · ✅ performance budgets · 🔧 account page heading/tabs regression fixed and now tested · ⚠️ inline scripts mean the CSP allows `'unsafe-inline'`.

### Backend & APIs
Blueprints: public, admin, control, security. Public JSON: 🔧 `/api/competitions` (public data only, no seeds), `/healthz`, 🔧 `/healthz/deep`. All POSTs need a CSRF token (webhook uses Stripe signatures). ✅ `SecurityTests` (IDOR, escalation, injection, CSRF, open redirect, headers). 🔧 one authoritative implementation per rule confirmed (pricing `line_price`, allocation `_allocate`, per-person limit `entrant_count`); dead function and unused 250 KB image removed.

### Database
SQLite WAL, `write_txn()` = `BEGIN IMMEDIATE`. 🔧 New triggers: no overselling, ticket numbers in range, issued tickets stay issued, valid wallet entries, positive withdrawals/orders, paid checkouts/deposits and completed withdrawals final, won instant prizes final, draws only with a valid winner, prize-claim history and processed postal decisions permanent. 🔧 Unique rules: each prize/refund/deposit/withdrawal/points award once, one claim per draw, one prize per ticket, one ticket per postal entry. ✅ `HardeningTests.test_database_refuses_impossible_states`. Rules that existing data already breaks are skipped and reported (not a start-up failure). ⚠️ single writer — see CAPACITY.md for when to move to PostgreSQL.

### Authentication
PBKDF2 passwords, 18+ check, email verification, per-IP and per-email lockout, device sessions with "sign out everywhere", password change revokes other devices. 🔧 nginx-level rate limit on login/sign-up/reset/MFA (shared across workers). ✅ lockout survives spoofed `X-Forwarded-For`.

### Admin area
Roles (Support, Competition manager, Finance, Administrator); TOTP MFA required; new-device sign-in alerts. 🔧 Sensitive actions (wallet adjustments, payouts, refunds, cancellations, draws, redraws, prize claims, deletes, promos, feature flags) require a password/MFA confirmation from the last 10 minutes; admin access times out after 30 minutes idle; adjustments require a reason and over £100 an Administrator. ✅ `AdminHardeningTests`. Every action in a hash-chained audit log.

### Payments (Stripe)
Hosted Checkout; one idempotency key per basket; webhook signature verified; amount must match; credit cards refunded automatically; late/mismatched payments flagged for refund. 🔧 payment intent stored per checkout; chargebacks and dashboard refunds flagged and emailed immediately; manual refunds recorded as `refunded` (previously overwritten to `expired`). ✅ `MoneyJourneyTests`: success with 3 duplicate webhooks, async failure, expiry, late payment, wrong amount, cancelled competition, interrupted session, chargebacks. ✅ daily reconciliation.

### Wallet
Balances are sums of an append-only ledger (triggers); every movement has a reason and a unique reference. 🔧 integrity checks now also cover points vs history, withdrawals/deposits/prizes without matching ledger lines. 🔧 bug fixed: "Start fresh → wallets" left points history behind. ✅ concurrent withdrawals can't overdraw.

### Competition engine & ticket allocation
Allocation inside `write_txn`; exclusive numbers by unique index; per-person limits including postal entries. ✅ 40 threads for one number → one owner; ✅ stampede never oversells; 🔧 ✅ multi-process stress test (separate processes like gunicorn workers) in CI; 🔧 database refuses overselling even if application code were wrong.

### Instant wins
Winning numbers sealed by a published hash before sales; result fixed at payment; reveal can't pay twice; unrevealed prizes settled. ✅ idempotent settlement under parallel job runs.

### Winner selection
Entry list frozen at close (snapshot + triggers); draw-readiness checks; HMAC-SHA256 over the frozen list with a pre-committed seed; permanent draw record with the full entry list; redraws attempt-numbered and reasoned. ✅ a crash mid-draw leaves no trace and the next job run draws once. ✅ running jobs 10× in 5 parallel threads → one draw, one claim, one notification.

### Free-entry processing
Receive → process with rules; closing waits for the queue. 🔧 processed decisions can't be altered.

### Withdrawals
Two-step request, ledger debit, finance processes. 🔧 completed withdrawals are final in the database; double-clicking "paid" is harmless. ✅ full journey test to "Paid".

### Email services
Outbox with retries (5), dedupe keys, staging redirect. ✅ an SMTP outage never blocks purchases; queued emails are sent when SMTP returns.

### Hosting
One VPS, Docker (web + worker), nginx, Let's Encrypt. App bound to 127.0.0.1; container runs as a non-root user. 🔧 nginx config: caching of static files and photos, gzip, auth rate limiting, `X-Forwarded-For` overwritten (anti-spoofing). ⚠️ single server = single point of failure; mitigated by verified off-site backups and a documented 4-hour rebuild.

### Backups & recovery
Nightly verified backups (restore-tested every time), off-site copy, `flask dr-drill` restores into a clean isolated copy and exercises it. RPO 24 h (less with pre-draw backups), RTO 4 h. ⚠️ owner must confirm the cron job and off-site copy are running and do the quarterly drill.

### Deployment pipeline
CI on every push: unit/integration tests, crawl, 🔧 concurrency stress, performance budgets, keyboard/axe. 🔧 `flask release-check` (settings, backups, integrity, health) and a written release checklist in DEPLOY.md; 🔧 `RELEASE` stamped into each build and shown in error reports. Migrations are additive with per-release rollback notes.

### Monitoring, error tracking, uptime
🔧 Health checks for payment-rate drops, blocked draws, slow pages/error rates, database errors; 🔧 errors grouped with scrubbed context and emailed on first occurrence; 🔧 `/healthz/deep`, synthetic checker and scheduled GitHub uptime workflow; 🔧 operations panel. See MONITORING.md. ⚠️ owner must set up UptimeRobot and the `SITE_URL` repository variable.

### Chaos testing (✅ `HardeningTests`)
| Failure | Result |
|---|---|
| Payment provider down at checkout | "Not charged", numbers released, basket kept; auto-pause after repeated errors |
| Email stops | Purchases unaffected; failures visible; retried later |
| Worker crashed | Jobs run from web requests; draws still happen |
| Database error mid-payment | Whole payment step rolled back; Stripe's retry completes it exactly once |
| Draw fails halfway | No draw, no winner, no claim recorded; next run draws once |
| Site restarts during checkout | A fresh app instance completes the payment from the webhook |
| Webhook delivered 3 times | Tickets, points and emails once |

## Owner actions before scaling up
1. Independent penetration test of staging (`docs/PENTEST-SCOPE.md`).
2. UptimeRobot on `/healthz/deep`; set the `SITE_URL` variable for the GitHub uptime workflow.
3. Install the updated nginx config (keep certbot's HTTPS lines — see DEPLOY notes).
4. Add `charge.refunded` and `charge.dispute.created` to the Stripe webhook's events.
5. Confirm nightly `./backup.sh` cron + off-site copy; run `flask dr-drill` quarterly.
6. Load test on staging before the first big launch (`docs/CAPACITY.md`).
