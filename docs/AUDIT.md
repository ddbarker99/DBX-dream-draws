# DBX Dream Draws — site audit and rebuild record

Audit date: 3 October 2026. This file records what the site contains, what was changed during the rebuild, and what still needs a decision or an outside check. It is generated from the live route map (`app.url_map`), not from memory, so every URL the application answers is listed.

## 1. URL inventory

**Indexed?** Yes = in the sitemap and indexable · No = `noindex` and/or blocked in `robots.txt` · n/a = not a page.
**Action:** Keep, Merge, Redirect or Delete.

### Public pages

| URL | Page title (pattern) | Purpose | Template | Indexed? | Action | Replacement URL |
|---|---|---|---|---|---|---|
| `/` | DBX Dream Draws — UK prize competitions… | Home: hero, live competitions, Instant Wins, how it works, winners, trust, FAQs | `home.html` | Yes | Keep (rebuilt) | — |
| `/?tab=…`, `/?q=…` | — | Old home-page filters | — | No | Redirect 301 | `/competitions?tab=…` |
| `/competitions` | Prize competitions — enter today | The single competitions listing; filters All live / Ending soon / New / With instant prizes / category | `competitions.html` | Yes | Keep | — |
| `/competitions?tab=<filter>` | `<Filter>` competitions | Filtered listing (own canonical) | `competitions.html` | Yes | Keep | — |
| `/competitions?q=…` | — | Search results | `competitions.html` | No | Keep | — |
| `/c/<slug>` | Win `<prize>` for `<price>` / `<game>` instant win game / `<prize>` — draw result | One template for every prize draw and instant-win game; becomes the results page once drawn | `competition.html` | Yes while live; drawn draws for 180 days; cancelled and closed games No | Keep (rebuilt) | — |
| `/c/<slug>/entries` | Entry list | Public entry list for verifying a draw | `entries.html` | No | Keep | — |
| `/c/<slug>/numbers` | — | JSON for the number picker | — | n/a | Keep | — |
| `/instant-wins` | Instant win games | Games lobby | `games.html` | Yes | Keep | — |
| `/games` | — | Old games URL | — | No | Redirect 301 | `/instant-wins` |
| `/winners` | Winners | Winners, with Draw results and Live draws tabs | `winners.html` | Yes | Keep | — |
| `/results` | — | Old results URL | — | No | Redirect 301 | `/winners?tab=results` |
| `/live` | — | Old live-draws URL | — | No | Redirect 301 | `/winners?tab=live` |
| `/how-it-works` | How it works | Plain-English overview | `how.html` | Yes | Keep | — |
| `/free-entry` | Free postal entry | Exact postal entry instructions, limits, rejection reasons | `free_entry.html` | Yes | Keep (rewritten) | — |
| `/fair-draws` | Fair draws | How draws are made and checked | `fair.html` | Yes | Keep | — |
| `/faq` | FAQs | Common questions | `faq.html` | Yes | Keep | — |
| `/about` | About us | Who runs the site | `about.html` | Yes | Keep | — |
| `/contact` | Contact us | Contact form | `contact.html` | Yes | Keep | — |
| `/responsible-play` | Responsible play | Limits, breaks, support | `responsible.html` | Yes | Keep | — |
| `/terms` | Terms and conditions | Rules | `terms.html` | Yes | Keep (updated to match the software) | — |
| `/privacy` | Privacy policy | Data protection | `privacy.html` | Yes | Keep | — |
| `/cookies` | Cookie policy | Cookies | `cookies.html` | Yes | Keep (updated) | — |
| `/complaints` | Complaints | Complaints procedure | `complaints.html` | Yes | Keep | — |
| `/login` | Log in | — | `login.html` | Yes | Keep | — |
| `/signup` | Create an account | — | `signup.html` | Yes | Keep (field-level errors) | — |
| `/forgot` | Reset your password | — | `forgot.html` | No | Keep (rate limited) | — |
| `/reset/<token>` | Choose a new password | Single-use, 1-hour link | `reset.html` | No | Keep | — |
| `/verify/<token>` | — | Email confirmation link (3 days) | — | No | Keep | — |
| `/r/<code>` | — | Referral link | — | No | Keep | — |

### Logged-in pages (all `noindex`, `Cache-Control: no-store`)

| URL | Purpose | Template | Action | Replacement URL |
|---|---|---|---|---|
| `/account` | Overview: active entries, upcoming draws, recent transactions, balances, points | `account.html` | Keep (rebuilt) | — |
| `/account?tab=entries[&show=active\|won\|previous]` | My entries | `account.html` | Keep (new filters) | — |
| `/account?tab=wins` | Wins and prizes | `account.html` | Keep | — |
| `/account?tab=transactions` | Orders and wallet activity | `account.html` | New (merges old Orders) | — |
| `/account?tab=wallet` | Balances, withdraw, add funds | `account.html` | Keep (rebuilt) | — |
| `/account?tab=points` | DBX Points and referrals | `account.html` | New name | — |
| `/account?tab=safer` | Spending limits and breaks | `account.html` | New (split from Settings) | — |
| `/account?tab=profile` | Profile, password, data | `account.html` | New (split from Settings) | — |
| `/account?tab=orders` | Old Orders tab | — | Redirect 301 | `?tab=transactions` |
| `/account?tab=rewards`, `refer` | Old Rewards tab | — | Redirect 301 | `?tab=points` |
| `/account?tab=settings` | Old Settings tab | — | Redirect 301 | `?tab=safer` |
| `/account?tab=tickets` | Old Tickets tab | — | Redirect 301 | `?tab=entries` |
| `/account/orders/<id>` | Order detail (owner only) | `order.html` | Keep | — |
| `/account/withdrawals/<id>` | Withdrawal status (owner only) | `withdrawal.html` | New | — |
| `/basket` | Basket | `basket.html` | Keep (reworked) | — |
| `/checkout/<id>/pay` | Resume the one payment for a checkout (double-click / refresh) | `checkout_wait.html` | New | — |
| `/checkout/<id>/done` | Confirmation / processing / failed states | `checkout_done.html` | Keep (reworked) | — |
| `/checkout/<id>/cancel` | Stripe cancel return; restores basket | — | Keep | — |
| `/checkout/<id>/demo-pay` | Test-mode payment page (404 when Stripe is on) | `demo_pay.html` | Keep (test only) | — |
| `/deposit/<id>/done`, `/cancel`, `/demo-pay` | Wallet deposits | `deposit_*.html` | Keep | — |
| `/play/<slug>` | Reveal instant-win plays | `play.html` | Keep | — |

POST-only endpoints (no page): `/basket/add`, `/basket/update`, `/basket/remove`, `/basket/promo`, `/basket/checkout`, `/account/limits`, `/account/exclude`, `/account/withdraw`, `/account/deposit`, `/account/deposit/refund`, `/account/profile`, `/account/redeem`, `/account/resend-verification`, `/free-play/<slug>`, `/play/reveal/<id>`, `/play/<slug>/reveal-all`, `/logout`, `/stripe/webhook`.

Utility: `/robots.txt`, `/sitemap.xml`, `/manifest.webmanifest`, `/sw.js`, `/static/…`, `/uploads/…`.

### Admin (`/admin/…`, all `noindex`, 404 to non-admins)

| URL | Purpose | Who |
|---|---|---|
| `/admin/` | Competitions dashboard, to-do counts, setup checklist | Staff + owner |
| `/admin/competitions/new`, `/<id>`, `/<id>/edit` | Create, view, edit; postal entries; draw record; history | Staff + owner |
| `/admin/competitions/<id>/status`, `/draw`, `/postal`, `/instant`, `/winner`, `/export.csv`, `/delete` | Publish/cancel, run draw, record envelopes, instant prizes, winner story (consent required), export, delete (not drawn) | Staff + owner |
| `/admin/instant/<id>/fulfilled` | Mark physical prize sent | Staff + owner |
| `/admin/games/*` | Starter/random/free daily games | Staff + owner |
| `/admin/settings` | Announcement and live bar | Staff + owner |
| `/admin/audit` | Permanent audit log | Staff + owner |
| `/admin/payouts`, `/payouts/export.csv`, `/refunds/<id>/done`, `/deposit-refunds/<id>/done` | Withdrawals, refunds | **Owner only** |
| `/admin/users`, `/users/<id>` | Users, wallet adjustments, admin roles | **Owner only** |
| `/admin/promos` | Promo codes | **Owner only** |
| `/admin/stats` | Revenue | **Owner only** |
| `/admin/start-fresh` | Wipe test data (backs up first, logged) | **Owner only** |

### Duplicates and obsolete URLs found

| Finding | Resolution |
|---|---|
| `/games`, `/results`, `/live`, `/?tab=` duplicated newer pages | Already 301-redirected; kept. |
| Account tabs renamed (`orders`, `rewards`, `settings`, `tickets`) | 301 to their new homes. |
| Footer "Ending soon" duplicated a competitions filter link | Removed from footer; filter still on `/competitions`. |
| Header showed a combined wallet total and a separate Log out button | Cash-only pill; Log out moved into account area and mobile menu. |
| Competition cards used "View" for both closed and drawn draws, games used "Play" | One wording set: **Enter now** (draws), **Play now** (games), **See result** (drawn), **View details** (closed). |
| No orphaned templates or dead routes found | Every template is reached by a route; the crawler found no broken links across 224 pages. |

**Finished competitions** are not deleted: a drawn draw's page becomes its results page (winning ticket, draw record, revealed seed, entry list) and stays in the sitemap for 180 days. Drawn competitions cannot be deleted from admin.

## 2. Promise vs behaviour (legal pages and the software)

| Promise (where) | Behaviour before | Now |
|---|---|---|
| Numbers reserved for 30 minutes (Terms §6, basket) | Stripe session 30 min, numbers held 45 min, terms said "up to 30" | Terms describe both accurately; late payments refunded and no entry issued |
| Draw goes ahead at the closing time; not brought forward (Terms §7, competition page, FAQ) | Admin could draw early once sold out | Draws refuse to run before the advertised closing time |
| Per-person limit covers paid + free entries combined (Terms §4–5, free entry page) | Postal entries weren't counted at all | Counted by matching account and by email; enforced at basket, checkout and postal entry |
| Late / non-compliant postal entries rejected (Terms §3, free entry page) | Admin could add after closing; no record of rejections except wrong answers | Every envelope recorded with arrival date and outcome; late, wrong, under-18, over-limit and sold-out entries rejected with a reason |
| Free entries win instant prizes on the same basis, credited automatically (Terms §4, §10) | Postal instant prizes always manual | Credited automatically when matched to an account; otherwise listed in Payouts |
| Full refund if cancelled (Terms §21) | Refunded the pre-promo price, and site credit came back as withdrawable cash | Refunds exactly what was paid, each part in the form it was paid; one transaction with the cancellation |
| Lowering a limit is immediate; raising takes 72 hours (Responsible play) | A pending raise survived a later lowering and still applied after 72 hours | Saving limits cancels any pending raise; raises only start after 72 hours, server-side |
| A break blocks every paid route (Responsible play) | A checkout already started could still be paid | Starting a break releases checkouts and deposits in progress; late payments are refunded |
| Winner photos/testimonials only with permission (Terms §20, Privacy §9) | Not recorded | Admin must confirm permission; recorded with time and admin; not shown without it |
| Draw records kept so results can be verified (Terms §8) | Only hashes on the competition row | Permanent snapshot of every eligible entry, method and winner; database triggers prevent edits |
| One strictly necessary cookie (Privacy, Cookies) | True, but fonts loaded from Google | Verified: one session cookie, no client storage; fonts self-hosted, so no third-party requests |

## 3. Security and integrity changes

- Checkout: one-time key per basket view (double-click or refresh can't create two checkouts); the amount Stripe reports must equal the amount asked for; payments for a competition cancelled mid-payment are flagged for refund; promo usage caps count checkouts in progress.
- All prices, quantities, discounts, balances and limits are calculated server-side; the browser only sends choices.
- Ownership checks on orders, withdrawals, deposits and plays (IDs belonging to someone else return 404 — covered by tests).
- Password change signs out every other device; reset links are single-use, 1 hour.
- Rate limits: login (8 per 15 min per IP and per email), password reset emails (3 per hour per email, 10 per IP), sign-ups (10 per hour per IP), contact form.
- Owner vs staff admin roles: money, balances, refunds, users, promos, stats and resets are owner-only.
- Append-only audit log for draws, cancellations, edits (old → new values), postal entries, wallet adjustments, withdrawals, refunds, limits, breaks, password changes, admin access and site resets. Triggers stop rows being edited or deleted.
- CSRF on every POST; secure, HttpOnly, SameSite=Lax session cookie (secure flag via `docker-compose.yml`).

## 4. Still needs a decision or an outside check

These weren't something code could settle on its own:

1. **Solicitor review of Terms and Privacy.** They now match the software, but they are still templates (`_draft_notice.html` shows on them).
2. **Credit cards** are refused by refunding after payment. A Stripe Radar rule (`Block if :card_funding: = 'credit'`) would stop them before payment.
3. **Age checks** are date-of-birth only. Consider an age/ID verification provider before scaling, and decide the withdrawal amount above which ID is requested (the wallet page tells customers this may happen).
4. **Account closure and data requests** go through the contact form. A self-serve "close my account" needs a policy on cash balances and record retention first.
5. **Rate limits are per server process.** With several gunicorn workers, add an nginx `limit_req` on `/login`, `/forgot` and `/signup` as a backstop.
6. **Draw corrections.** Results can't be edited (by design). If a correction were ever legally required, it would be a manual, documented database operation; the policy for announcing it publicly should be written down.
7. **Sign-up reveals that an email already has an account.** Kept deliberately (customers otherwise can't tell why sign-up failed); login and password reset do not reveal it.
8. **Real-world payment testing.** Webhooks, duplicates, wrong amounts and late payments are covered by automated tests with simulated Stripe events. Run the Stripe test-mode checklist in `QA-CHECKLIST.md` against the live server before switching to live keys.

---

# Platform phase (automation, reliability, transparency, operations)

## New and changed URLs

| URL | Purpose | Indexed? | Notes |
|---|---|---|---|
| `/results` | Permanent, searchable archive of every completed draw | Yes (page 1) | Was a redirect; `/winners?tab=results` now 301s here |
| `/search?q=` | Search live competitions, instant wins and past results | No | Linked from the header and mobile menu |
| `/watch/<slug>` (POST) | Save / unsave a competition | n/a | |
| `/account/notifications` | Customer notification centre | No | Header bell shows unread count |
| `/account/sessions/revoke` (POST) | Sign out a device / all other devices | n/a | |
| `/healthz` | Uptime-monitor endpoint (200 / 503) | No | |
| `/admin/` | **DBX Control Centre** | No | Competitions list moved to `/admin/competitions` |
| `/admin/postal`, `/admin/postal/<id>` | Postal queue: approve / reject | No | |
| `/admin/prizes`, `/admin/prizes/<id>`, `/admin/evidence/<file>` | Winner claims workflow, private evidence | No | |
| `/admin/competitions/<id>/redraw` (POST) | Redraw with recorded reason | n/a | |
| `/admin/finance`, `/admin/finance/payments.csv` | Reconciliation and Stripe-matching export | No | Finance / Administrator |
| `/admin/customers/<id>/timeline` | Chronological customer history | No | Viewing is audited |
| `/admin/flags` | Fraud & abuse review queue | No | |
| `/admin/cases`, `/admin/cases/<id>` | Support case system | No | Contact form opens a case |
| `/admin/health` | Job runs, health checks, failed emails | No | |
| `/admin/analytics`, `/admin/performance` | Customer journey funnel; competition performance | No | |
| `/admin/mfa/`, `/admin/mfa/setup` | Admin two-step verification | No | |

## Brief item → where it's built

| # | Item | Where |
|---|---|---|
| 1 | Lifecycle Draft → … → Completed, entry list locked at close | `services.lifecycle_stage`, `close_competition`, `entry_snapshots`, lock triggers in `db.POST_TRIGGERS`, admin competition page stepper |
| 2 | Draw audit, redraw with reason, corrections recorded | `draws` (+ `redraw_of`, `reason`, `winner_user_id`, `snapshot_id`), `services.redraw`, trigger `comp_result_locked` |
| 3 | Postal entries first-class: receive → validate → approve/reject, same pool and limits | `receive_postal` / `process_postal`, `/admin/postal`, closing waits for envelopes |
| 4 | Operations dashboard | `/admin/` Control Centre |
| 5 | Financial reconciliation | `/admin/finance` (self-checking sources = entry value), payments CSV |
| 6 | Customer timelines | `/admin/customers/<id>/timeline` |
| 7 | Fraud & abuse flags (review, never automatic bans) | `control.run_flag_rules`, `/admin/flags`, `flag_rules` job |
| 8 | Prize management workflow | `prize_claims`, `claim_events`, `/admin/prizes` |
| 9 | Reliable notifications, no duplicates on retry | `notify.py` (dedupe keys, tracked outbox, retries), `email_outbox` job |
| 10 | In-app notification centre | `/account/notifications` |
| 11 | Winner/result archive | `/results`, permanent result pages, sitemap |
| 12 | Entry verification | "Find a ticket" in My entries; public lists show own tickets, first name + initial only, no paid/free label |
| 13 | Instant wins auditable | Result stored server-side at purchase (sealed numbers); reveal only displays it; refresh/other device shows the same result (existing tests) |
| 14 | Concurrency | `OpsTests` (40 people racing for one number, stampede on a 25-ticket competition), `tests/load_test.py` |
| 15 | Idempotent payments | Checkout one-time key, idempotent fulfilment, amount check, deduplicated notifications (tests) |
| 16 | Health monitoring and alerts | `jobs.health_checks`, `health_alerts` job (emails SUPPORT_EMAIL), Control Centre |
| 17 | Backups with restore testing | `flask backup` / `restore-test`, `backup.sh`, health check "Backups" |
| 18 | Staging | `STAGING=1`, `docker-compose.staging.yml`, `docs/STAGING.md` |
| 19 | Automated tests | 125 tests + crawler; GitHub Actions workflow |
| 20 | Role-based admin permissions | `perms.py`: Support, Competition manager, Finance, Administrator |
| 21 | Admin MFA, sessions, login alerts, logging | `security.py` |
| 22 | Journey analytics without tracking | `analytics.py`, `funnel_counts`, `/admin/analytics` |
| 23 | Competition performance | `/admin/performance` (period-over-period) |
| 24 | Search and discovery | `/search`, header search, competitions filters, `/results` |
| 25 | Watchlist | `watchlist`, reminders (email only with marketing consent) |
| 26 | Referrals | `referrals` table, clear rules, history in account, shared-connection flag |
| 27 | DBX Points history | `points_ledger`, account points history with values |
| 28 | Support case system | `cases`, `case_notes`, contact form integration |
| 29 | Site status / maintenance | `status.py`, automatic payment pause after provider errors |
| 30 | Observability | `job_runs`, `job_status`, audit log, Health page |
| 31 | Traffic spikes | Load test script, worker container (jobs off the request path), self-hosted assets |
| 32 | Emergency procedures | `docs/RUNBOOK.md` |
| 33 | Data/privacy audit | `docs/PRIVACY-DATA-AUDIT.md`, privacy policy updated, retention pruning job |
| 34 | Mechanics legal review, configurable mechanics | `docs/MECHANICS.md`, per-competition entry question mode + site default |
| 35 | Feature discipline | Every item above answers ease of use, trust, less manual work or reliability; nothing was added for its own sake |

## Still needs a decision or outside check (platform phase)

1. **Legal review of mechanics** (`docs/MECHANICS.md`), especially instant-win games and whether to keep the multiple-choice question.
2. **Off-site backups:** set `BACKUP_REMOTE` (rclone) so backups survive losing the VPS.
3. **Uptime monitor:** point an external service (e.g. UptimeRobot) at `/healthz` so you're alerted even if the server itself is down — the internal health alerts can't send if the server is off.
4. **Admin MFA enrolment:** every admin will be asked to enrol on next sign-in; keep recovery codes safe.
5. **Load test on staging** with production-like hardware before the first big launch.
6. **Retention clean-ups** marked "manual" in the privacy audit should be scheduled (quarterly).

# Phase 3 (customer experience, operations, quality)

## New URLs
| URL | Who | Purpose |
|---|---|---|
| `/account` (overview) | Customer | Personal dashboard: things needing action, active tickets, upcoming draws, recent results, balances, latest notifications |
| `/account?tab=entries&show=active\|upcoming\|winner\|completed` | Customer | My tickets with filters |
| `/account/tickets/<id>` | Owner only | Everything about one ticket: order, draw time, status, result, proof |
| `/account/notifications`, `…/<id>/open`, `…/read` | Customer | Notification centre, mark read, deep links |
| `/account/preferences` (POST), `/unsubscribe/<token>` | Customer | Essential vs optional email/SMS, reminder emails; one-click unsubscribe; consent logged |
| `/watch/<slug>` (POST) | Customer | Opt-in closing / result reminders |
| `/transparency` | Everyone | Transparency centre: fair draws, free entry, odds, results, complaints |
| `/support`, `/support/requests`, `/support/requests/<id>` | Customer | Help centre with issue types and order/ticket/withdrawal references; follow replies |
| `/admin/features` | Administrator | Feature flags |
| `/admin/liability` | Finance | Prize liability |
| `/admin/reports` | Finance | Business reports + CSV |
| `/admin/targets`, `/admin/errors/<id>/resolve` | Finance / Admin | Measurable targets, error log |
| `/admin/audit?…&verify=1&format=csv` | Admin | Audit filters, chain verification, export |

## Brief item → where it's built
| Brief item | Built in |
|---|---|
| Personalised dashboard | `routes.account`, `_attention()`, `templates/account.html` |
| My Tickets (Active / Upcoming draw / Winner / Completed) + ticket page | `_entry_groups()`, `routes.ticket_detail`, `ticket.html` |
| Live draw / result page states | `competition.html` (Open / Sold out / Draw pending with auto-refresh / Completed) |
| Notification centre | `notifications.html`, `routes.notification_open/read` |
| Communication preferences (privacy-compliant, no auto-subscribe) | `routes.preferences`, `set_consent()`, `consent_log`, signup opt-ins unticked |
| Transparency centre | `routes.transparency`, `transparency.html` |
| Opt-in reminders | `routes.watch`, `jobs._watch`, `admin.announce_draw` |
| Sold-out handling (never auto-add alternatives) | `competition.html` "Still open" panel — suggestions only |
| Empty states | account, tickets, notifications, support, results |
| Customer support centre | `routes.support_centre`, `SUPPORT_TOPICS` |
| Internal support dashboard (priority, history, notes, no double handling) | `control.cases/case_detail`, soft locks, take-over audited |
| Anomaly alerts | `jobs.anomalies()` → System health → alert email |
| Pre-launch validator | `checks.launch_checks()` gates publish and scheduled launch |
| Draw-readiness check | `checks.draw_checks()` in `services.run_draw` |
| Prize-liability dashboard | `control.liability` |
| Daily reconciliation with payment provider | `reconcile.py`, `payments.list_sessions/list_refunds`, job `reconcile` |
| Database integrity checks | `checks.integrity_problems()`, job `integrity`, asserted after every test and crawl |
| Immutable financial ledgers | triggers `ledger_no_*`, `points_no_*` (corrections are adjustments) |
| Feature flags | `flags.py`, `/admin/features` |
| Deployment dev → staging → prod, migration rollback | `docs/DEPLOY.md`, `docs/STAGING.md` |
| Automated regression suite | `tests/` + CI (`.github/workflows/tests.yml`) |
| Penetration / security testing (own systems) | `SecurityTests`, `docs/SECURITY-REVIEW.md` |
| Disaster recovery docs + exercises | `flask dr-drill`, `docs/DISASTER-RECOVERY.md` |
| Audit-log viewer with tamper protection | hash chain (`audit_hash`, `verify_audit_chain`), `/admin/audit` |
| Accessibility beyond automated scans | `tests/a11y_keyboard.py`, `docs/ACCESSIBILITY.md` (AT testing plan) |
| Performance budgets | `tests/perf_budget.py` (CI) |
| Analytics funnel (lawful/anonymous) | `analytics.py` (cookie-free counts), `/admin/analytics` |
| Business reporting | `/admin/reports`, `docs/REPORTING-DEFINITIONS.md` |
| Definition of Done | `docs/DEFINITION-OF-DONE.md` |
| Usability testing | `docs/USABILITY-TEST.md` |
| Measurable targets | `/admin/targets`, `metrics.py`, `docs/TARGETS.md` |

## Still needs a decision or outside check (Phase 3)
1. **Accountant:** agree `docs/REPORTING-DEFINITIONS.md` (especially revenue recognition and VAT/duty).
2. **Usability round 1** with 5–10 new users on their phones before adding more features.
3. **Assistive-technology testing** (VoiceOver, TalkBack, NVDA) — automated checks pass, but people must confirm.
4. **Independent penetration test** of staging before launch.
5. **First DR drill on the server** (`flask dr-drill`) and a yearly full rebuild exercise.
6. **Record outside measurements** (uptime, PageSpeed) on Admin → Targets monthly.

# Phase 4 (production hardening)
Full audit: `docs/PRODUCTION-AUDIT.md`. New URLs: `/healthz/deep`, `/api/competitions`, `/j/select/<id>` (anonymous journey count), `/admin/mfa/confirm` (step-up), `/admin/support-insights`, `/admin/errors/<id>/resolve`. New commands: `flask release-check`. New tests: `MoneyJourneyTests`, `HardeningTests`, `AdminHardeningTests`, `MonitoringTests`, `EvidenceTests`, `tests/stress_concurrency.py`, `tests/synthetic_check.py`.
