# Database

One SQLite file (`/data/prizes.db` in the containers, `data/prizes.db` on the server). Schema: `app/schema.sql` plus additive upgrades in `app/db.py` (`MIGRATIONS`), protective triggers (`POST_TRIGGERS`) and uniqueness rules (`CONSTRAINTS`). Money is always integer **pence**; times are UTC ISO strings (`2026-10-03T16:00:00Z`).

## How the main records relate

```
users ─┬─< checkouts ─< orders >── competitions ─┬─< tickets (exclusive number per competition)
       │      │                                  ├─< instant_prizes ── (won by) ──> tickets
       │      └─ cash_due paid by Stripe          ├─< postal_entries ── (accepted) ──> tickets
       ├─< credit_ledger  (every wallet movement)  ├─< entry_snapshots (frozen entry list at close)
       ├─< withdrawals ── ledger line w<id>        ├─< draws (permanent; winner = a ticket) ─< prize_claims ─< claim_events
       ├─< deposits ── ledger line d<id>           └─ winner_ticket_id → tickets (only via the latest draw)
       ├─< points_ledger      ├─< notifications     ├─< cases ─< case_notes
       ├─< user_sessions      ├─< watchlist         └─< referrals (referrer, referred)
```

- **Customer → orders → tickets.** A *checkout* is one payment (one basket); it has one *order* per competition; each order has *tickets*. A ticket is an exclusive number in one competition (`UNIQUE(competition_id, number)`), status `held` while paying, `issued` once paid. Free postal entries create tickets with `postal_entry_id` instead of `order_id`.
- **Payments.** `checkouts.cash_due` is what the card was charged; `credit_used`, `deposit_used`, `cash_used` are wallet funds spent (each a negative ledger line, ref `c<checkout>`). `stripe_session_id` / `payment_intent` link to Stripe. Status: pending → paid | expired | credit_refused | needs_refund → refunded.
- **Wallet.** No balance column: a balance is `SUM(credit_ledger.amount)` per `kind` (`cash` withdrawable, `credit` spend-only, `deposit` refundable top-ups). The `ref` says what caused each line:

  | ref | Meaning | Unique? |
  |---|---|---|
  | `c<checkout>` | spent at checkout (−) / returned when unpaid (+) | once per kind and direction |
  | `ip<prize>` | instant prize paid | once |
  | `refund-o<order>[-credit\|-deposit]` | refund on cancellation | once |
  | `w<withdrawal>` | withdrawal requested (−) / returned (+) | once each |
  | `d<deposit>` / `dr<deposit>-<n>` | card deposit / refund to card | once |
  | `u<user>` | referral bonus for that friend | once |
  | `admin<staff id>` | manual adjustment (reason required) | — |
- **Prizes.** `instant_prizes` are created (and sealed by hash) before sales; `ticket_id` is set when won and never changes. Main-draw prizes: `draws` (one row per draw or redraw, with the full entry list, seed and result) → `prize_claims` (workflow status) → `claim_events` (history).
- **Free entries.** `postal_entries` record every envelope (received date, outcome, reason); accepted ones point to their ticket.
- **Points.** `users.points` is a cache of `SUM(points_ledger.points)`; the integrity job checks they match.
- **Audit.** `audit_log` is append-only and hash-chained (`prev_hash`, `row_hash`).

## Tables
| Table | Columns | References |
|---|---|---|
| `audit_log` | `id`, `created_at`, `actor_id`, `actor_email`, `action`, `target`, `detail`, `prev_hash`, `row_hash` | — |
| `case_notes` | `id`, `case_id`, `created_at`, `actor_id`, `kind`, `body` | case_id→cases.id |
| `cases` | `id`, `user_id`, `name`, `email`, `topic`, `status`, `competition_id`, `checkout_id`, `assigned_to`, `resolution`, `created_at`, `updated_at`, `priority`, `withdrawal_id`, `locked_by`, `locked_at`, `reason` | user_id→users.id |
| `checkouts` | `id`, `user_id`, `subtotal`, `promo_id`, `promo_discount`, `credit_used`, `cash_due`, `status`, `stripe_session_id`, `created_at`, `paid_at`, `cash_used`, `deposit_used`, `idem_key`, `pay_url`, `device`, `payment_intent` | promo_id→promo_codes.id, user_id→users.id |
| `claim_events` | `id`, `claim_id`, `created_at`, `actor_id`, `status`, `note`, `evidence` | claim_id→prize_claims.id |
| `competitions` | `id`, `slug`, `title`, `description`, `image`, `cash_alternative`, `ticket_price`, `max_tickets`, `max_per_user`, `ends_at`, `question`, `answer_a`, `answer_b`, `answer_c`, `correct`, `status`, `seed`, `seed_hash`, `entries_hash`, `winner_ticket_id`, `drawn_at`, `created_at`, `category`, `featured`, `prize_value`, `discount_tiers`, `live_url`, `instant_salt`, `instant_hash`, `winner_photo`, `winner_quote`, `game_type`, `auto_draw`, `free_daily`, `starts_at`, `scheduled`, `winner_consent_at`, `winner_consent_by`, `locked_at`, `purging`, `completed_at`, `question_mode` | — |
| `consent_log` | `id`, `user_id`, `channel`, `granted`, `source`, `created_at`, `ip` | user_id→users.id |
| `credit_ledger` | `id`, `user_id`, `amount`, `reason`, `ref`, `created_at`, `kind` | user_id→users.id |
| `deposits` | `id`, `user_id`, `amount`, `status`, `stripe_session_id`, `payment_intent`, `refunded`, `created_at`, `paid_at` | user_id→users.id |
| `draws` | `id`, `competition_id`, `drawn_at`, `method`, `run_by`, `seed`, `seed_hash`, `entries_hash`, `entry_count`, `winning_index`, `winning_number`, `winning_ticket_id`, `entries`, `redraw_of`, `reason`, `winner_user_id`, `snapshot_id` | — |
| `entry_snapshots` | `id`, `competition_id`, `taken_at`, `entry_count`, `paid_count`, `postal_count`, `entries_hash`, `entries` | — |
| `error_log` | `id`, `signature`, `first_at`, `last_at`, `count`, `endpoint`, `path`, `error`, `trace`, `resolved_at` | — |
| `feature_flags` | `key`, `state`, `updated_at`, `updated_by` | — |
| `flags` | `id`, `kind`, `subject`, `detail`, `status`, `created_at`, `reviewed_at`, `reviewed_by`, `note` | — |
| `funnel_counts` | `day`, `step`, `device`, `comp_id`, `n` | — |
| `instant_prizes` | `id`, `competition_id`, `title`, `value`, `credit_amount`, `number`, `ticket_id`, `won_at`, `fulfilled`, `prize_type` | ticket_id→tickets.id, competition_id→competitions.id |
| `job_runs` | `id`, `job`, `started_at`, `finished_at`, `ok`, `changed`, `error` | — |
| `job_status` | `job`, `last_started`, `last_ok`, `last_error_at`, `last_error`, `last_changed` | — |
| `maintenance_unlock` | `id`, `reason` | — |
| `notifications` | `id`, `user_id`, `email`, `kind`, `title`, `body`, `link`, `dedupe_key`, `created_at`, `read_at`, `email_status`, `email_tries`, `email_error`, `sent_at`, `email_payload` | user_id→users.id |
| `orders` | `id`, `user_id`, `competition_id`, `quantity`, `amount`, `status`, `stripe_session_id`, `created_at`, `paid_at`, `checkout_id`, `discount` | checkout_id→checkouts.id, competition_id→competitions.id, user_id→users.id |
| `password_resets` | `token_hash`, `user_id`, `expires_at` | user_id→users.id |
| `points_ledger` | `id`, `user_id`, `points`, `reason`, `ref`, `created_at` | user_id→users.id |
| `postal_entries` | `id`, `competition_id`, `name`, `email`, `address`, `answer_correct`, `added_by`, `created_at`, `received_at`, `user_id`, `status`, `reject_reason`, `dob`, `phone` | user_id→users.id, added_by→users.id, competition_id→competitions.id |
| `prize_claims` | `id`, `competition_id`, `draw_id`, `ticket_id`, `user_id`, `status`, `prize_choice`, `created_at`, `updated_at` | — |
| `promo_codes` | `id`, `code`, `percent`, `fixed`, `min_spend`, `max_uses`, `per_user`, `uses`, `expires_at`, `active`, `created_at` | — |
| `referrals` | `id`, `referrer_id`, `referred_id`, `created_at`, `status`, `reason`, `rewarded_at` | referred_id→users.id, referrer_id→users.id |
| `request_stats` | `day`, `metric`, `n` | — |
| `settings` | `key`, `value` | — |
| `tickets` | `id`, `competition_id`, `number`, `user_id`, `order_id`, `postal_entry_id`, `created_at`, `status`, `revealed_at` | postal_entry_id→postal_entries.id, order_id→orders.id, user_id→users.id, competition_id→competitions.id |
| `user_sessions` | `sid`, `user_id`, `created_at`, `last_seen`, `ip`, `agent`, `revoked_at` | user_id→users.id |
| `users` | `id`, `email`, `name`, `password_hash`, `dob`, `is_admin`, `monthly_limit`, `pending_limit`, `pending_limit_at`, `excluded_until`, `created_at`, `referral_code`, `referred_by`, `daily_limit`, `weekly_limit`, `marketing`, `pending_daily`, `pending_weekly`, `phone`, `email_verified`, `points`, `points_lifetime`, `admin_role`, `mfa_secret`, `mfa_enabled`, `mfa_recovery`, `marketing_sms`, `reminder_emails` | referred_by→users.id |
| `watchlist` | `user_id`, `competition_id`, `created_at`, `remind_close`, `remind_result` | user_id→users.id |
| `withdrawals` | `id`, `user_id`, `amount`, `status`, `note`, `created_at`, `done_at`, `method`, `account_name`, `sort_code`, `account_number`, `paypal_email` | user_id→users.id |

Deprecated (kept so older releases can still run; not used by current code): `orders.stripe_session_id` (from v1, superseded by `checkouts`). Don't reuse it.

## Rules enforced by the database itself
Triggers (refuse the change with an error):
- `audit_log`: `audit_no_delete`
- `audit_log`: `audit_no_update`
- `checkouts`: `checkouts_final`
- `claim_events`: `claim_events_no_delete`
- `claim_events`: `claim_events_no_update`
- `competitions`: `comp_lock_permanent`
- `competitions`: `comp_result_locked`
- `credit_ledger`: `ledger_no_delete`
- `credit_ledger`: `ledger_no_update`
- `credit_ledger`: `ledger_valid`
- `deposits`: `deposits_final`
- `draws`: `draws_no_delete`
- `draws`: `draws_no_update`
- `draws`: `draws_valid`
- `entry_snapshots`: `snapshots_no_delete`
- `entry_snapshots`: `snapshots_no_update`
- `instant_prizes`: `instant_prize_final`
- `orders`: `orders_valid`
- `points_ledger`: `points_no_delete`
- `points_ledger`: `points_no_update`
- `postal_entries`: `postal_decision_final`
- `tickets`: `tickets_locked_delete`
- `tickets`: `tickets_locked_insert`
- `tickets`: `tickets_locked_update`
- `tickets`: `tickets_no_oversell`
- `tickets`: `tickets_status_valid`
- `tickets`: `tickets_valid`
- `withdrawals`: `withdrawals_final`
- `withdrawals`: `withdrawals_valid`


Unique indexes (created at start-up; if old data breaks one it's skipped and reported in System health → Data integrity): `ux_ledger_once`, `ux_points_once`, `ux_deposit_session`, `ux_claim_per_draw`, `ux_prize_per_ticket`, `ux_ticket_per_postal`, `ux_draw_winner`, `ux_withdrawal_ledger`, plus the table-level `UNIQUE` constraints (ticket numbers, emails, slugs, Stripe sessions, notification dedupe keys, referrals).

## Regenerating this file's tables
The table and trigger lists above are generated: run `python tests/schema_doc.py` after any schema change and paste the output here in the same commit.
