# Personal data audit

What DBX Dream Draws stores, where, who receives it, how long it's kept, and how requests are handled. Generated from the actual database schema (`app/schema.sql` + migrations in `app/db.py`) on 3 October 2026. **Update this file whenever a table, field, third party or retention rule changes**, and keep `/privacy` and `/cookies` consistent with it.

## Where data lives

- One SQLite database: `data/prizes.db` on the VPS (inside the `./data` volume).
- Uploaded images: `data/uploads/` (competition and winner photos — public).
- Prize evidence: `data/evidence/` (ID, delivery proof — **never public**, only admins with the *prizes* permission can open it).
- Backups: `data/backups/` (nightly, 30 kept), plus your off-site copy if `BACKUP_REMOTE` is set. Backups contain everything above.
- Server logs: Docker container logs (gunicorn access log with IP addresses; email bodies when SMTP isn't configured).

## What we store

| Data | Table / place | Why | Kept for |
|---|---|---|---|
| Name, email, date of birth, password hash, phone (optional), marketing choice, referral code, email-verified flag | `users` | Account, 18+ check, contacting winners | Life of the account; on closure, kept as long as needed for draws/financial records (see below), otherwise deleted or anonymised |
| Spending limits, breaks, pending limit changes | `users`, `audit_log` | Responsible play | Life of the account + 1 year |
| Admin MFA secret (encrypted at rest only by disk), hashed recovery codes, admin role | `users` | Admin security | While an admin |
| Device sessions: IP address, browser string, sign-in and last-seen times | `user_sessions` | Signing out other devices, security alerts, fraud review | **Deleted 90 days after last use** (automatic, `prune` job) |
| Orders, checkouts, Stripe session IDs, amounts, card-funding outcome (not card numbers) | `checkouts`, `orders`, `deposits` | Contract, accounting, refunds | 6 years (tax/accounting) |
| Tickets and entry numbers | `tickets` | Running draws, publishing entry lists | Permanent for drawn competitions (draw integrity); deleted with test competitions |
| Frozen entry lists, draw records | `entry_snapshots`, `draws` | Proving draws were fair | Permanent (ticket numbers and account IDs only) |
| Wallet movements | `credit_ledger` | Balances, accounting | 6 years |
| Withdrawals: bank name, sort code, account number **or** PayPal email | `withdrawals` | Paying winnings | Full details until paid; then masked to the last digits automatically; record 6 years |
| Postal entries: name, email, address, phone, date of birth, answer, arrival date, outcome | `postal_entries` | Free entry route, eligibility, limits | 2 years after the competition, then delete the address/phone/DOB |
| Prize claims: status, notes, evidence files | `prize_claims`, `claim_events`, `data/evidence` | Verifying winners, delivering prizes | 6 years (evidence of prize fulfilment) |
| Notifications (title, message, delivery status) | `notifications` | Customer notification centre, email delivery tracking | **Read notifications deleted after 1 year** (automatic) |
| Support cases: name, email, messages, internal notes | `cases`, `case_notes` | Answering and tracking support requests, complaints | 3 years after resolution |
| Review flags (e.g. shared phone numbers) | `flags` | Fraud and abuse prevention (legitimate interest) | 2 years after review |
| Audit log (who did what, including staff viewing a customer's timeline; each row hash-chained to the previous one) | `audit_log` | Security and accountability | Permanent (append-only by design) |
| Saved competitions | `watchlist` | Customer feature | Life of the account |
| DBX Points history, referrals | `points_ledger`, `referrals` | Loyalty scheme | Life of the account |
| Feature switches, maintenance lock | `feature_flags`, `maintenance_unlock` | Operating the site | Kept; contain **no personal data** (feature name, state, staff id who changed it) |
| Journey counts | `funnel_counts` | Understanding drop-off | Kept; contains **no personal data** (day, step, device type, competition only) |
| Winner photo and quote | `competitions.winner_photo/quote` + `winner_consent_at/by` | Publicity | Only with recorded consent; removed on request |

## Cookies and similar technologies

Verified in the code (no `localStorage`, `sessionStorage`, IndexedDB, analytics or advertising scripts):
- **One cookie: `session`** (strictly necessary). Holds the login, basket, CSRF token and device-session ID. 30 days or until log out.
- A service worker for "add to home screen" that caches nothing personal.
- Fonts and images are self-hosted, so browsing sends nothing to third parties.
- Journey analytics are counted on the server without cookies or identifiers, so no consent banner is needed. **If analytics or advertising tools are ever added, update `/cookies` and add a consent banner first.**

## Who receives personal data

| Recipient | What | Why |
|---|---|---|
| Stripe | Email, payment details (entered on Stripe's page), amounts | Card payments, refunds, card-type checks |
| Email provider (SMTP, e.g. Hostinger) | Name, email, message contents | Sending account and transactional emails |
| Hosting provider (VPS) | Everything, as the infrastructure host | Running the site |
| Your bank / PayPal | Name, account details, amount | Paying withdrawals (exported CSV) |
| Discord (if configured) | Winner's first name and surname initial, ticket number | Announcing results |
| Off-site backup storage (if configured) | Full backups | Disaster recovery |

## Requests from customers

- **Access (copy of their data):** staff open the customer's **timeline** (Admin → Customers → Timeline), which lists every order, entry, wallet movement, prize, withdrawal, responsible-play change, support case and sign-in. Export it and send it within one month. Opening the timeline is itself recorded in the audit log.
- **Correction:** name/email changes via a support case after checking identity.
- **Deletion / account closure:** pay out any cash balance (and refund unspent deposits to the card) first. Then delete the profile fields that aren't needed. Keep records the law or draw integrity requires: tickets in drawn competitions, financial records for 6 years, and the audit log. Replace the name/email on those with "Closed account #id". Record the request and outcome in a support case.
- **Marketing opt-out:** account settings or the unsubscribe link; transactional messages continue.

## Retention jobs

Automatic (`prune` job, daily): device sessions 90 days after last use, read notifications after 1 year, job history after 90 days.
Manual (quarterly, until automated): postal entry personal details 2 years after the competition, resolved support cases after 3 years, reviewed flags after 2 years.
