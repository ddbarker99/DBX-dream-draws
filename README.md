# PrizeHub v2: prize competition site

A UK prize competition site built with Flask, SQLite and Stripe Checkout. It runs in Docker behind nginx.

## Features

### For players

- **Competitions:** a featured hero, category tabs, "Instant wins" and "Ending soon" filters, and a live countdown on every card.
- **Card badges:** almost gone, just launched, instant wins left and multi-buy deals.
- **Choosing tickets:** **lucky dip** (with quick picks of 5, 10, 25, 50 or 100), or **pick your own numbers** from a grid that shows which numbers are taken.
- **Basket:** buy across several competitions in one payment, with multi-buy discounts, promo codes and wallet credit.
- **Instant wins:** prizes are tied to secret ticket numbers. Site-credit prizes land in the winner's wallet immediately, and the order confirmation page plays an animated ticket reveal.
- **Wallet:** credit from instant wins, referrals or admin adjustments. Players can spend it or request a withdrawal to their bank (minimum £10).
- **Referral scheme:** each player gets a personal link. They earn credit when a friend they refer makes a first purchase.
- **Transparency pages:**
  - Public entry lists, with names shortened (e.g. "Darren B.").
  - Draw results.
  - A winners page with real stats.
  - Odds shown on every competition.
  - Live draw links and a red "WE'RE LIVE" bar across the site.
- **Provably fair:** each main draw's seed and each competition's instant-win numbers are sealed with a hash before launch and revealed afterwards.
- **Safer play:** daily, weekly and monthly spend limits (raising one takes 72 hours), breaks from 24 hours to 1 year, an 18+ check, and links to GamCare and BeGambleAware.
- **Accounts:** password reset by email, email confirmations, and a marketing opt-in.
- **Installable:** players can add the site to their phone's home screen like an app.
- **Information pages:** FAQ, responsible play, complaints, privacy, terms, free entry, and how draws work.

### For you (Admin)

- **Dashboard:** revenue, tickets and users, plus to-do counters (ready to draw, prizes to send, withdrawals, refunds).
- **Competitions:**
  - Create, duplicate and edit them; set the category, prize value, featured flag and live draw link.
  - Set multi-buy tiers such as `10:10, 25:15`.
  - Add instant-win prizes (wallet credit or physical). They're numbered at random and sealed.
  - Add postal entries, run the draw, and add a winner photo and quote.
  - Export entries as CSV.
- **Payouts & prizes:** mark physical instant prizes as sent, pay or reject withdrawals, and handle refunds.
- **Users:** search, view history, adjust wallets and grant admin access.
- **Promo codes:** percentage or fixed amount off, minimum spend, usage limits and expiry.
- **Site settings:** announcement bar and "We're LIVE" bar, which can also be posted to Discord.
- **Discord:** new competitions, winners and live streams are posted automatically if you set a webhook.

## Cash winnings, withdrawals & loyalty (v4)

The wallet has two balances:

- **Cash** comes from instant cash wins and refunds. Players can withdraw it to a UK bank account or PayPal from £5, once their email address is verified.
- **Site credit** comes from referrals, DBX Points and promotions. It can be spent but not withdrawn, and it's used first at checkout.

To pay withdrawals, open **Admin → Payouts**. Export the pending ones as a CSV, pay them from your bank, then click **Paid**. Bank details are masked once a withdrawal is paid.

**Email verification:** new players get a confirmation link, and they need to verify before they can withdraw or claim free plays. **Set up SMTP in `.env`**, or players won't receive the link. You can also mark a player verified yourself from **Admin → Users**.

Other features in this version:

- **Daily free game:** go to **Admin → Launch free daily game**. Every verified member gets one free play a day.
- **DBX Points:** players earn 1 point per £1 paid by card, with Silver, Gold and Diamond tiers earning more. 100 points = £1 credit.
- **Automatic draws:** main draws run themselves when the timer ends, then email the winner and post to Discord. Untick the option on a competition for a live draw.
- **Cancelling a competition** refunds every entrant to their cash balance automatically.
- **Stats:** **Admin → 📈 Stats** shows revenue for the last 30 days, top competitions, and how much each game has actually paid back.

## Instant win games (10p, 40p, 50p, £1, £5)

To add the five starter games, open **Admin → Add starter instant-win games**. It creates:

- a 10p Penny Scratch;
- a 40p Spin & Win;
- a 50p Mystery Box;
- a £1 Golden Scratch;
- a £5 High Roller Spin.

Each one comes with a sealed table of wallet-credit prizes and pays back about 45–50% if it sells out.

To make your own game, open **New competition**, set **Type** to a game, save it, then add prizes under **Game prizes**.

The admin page shows each game's prize pool, odds and payback percentage.

**How games stay legal:** every game has a fixed number of plays, and the winning plays are chosen at random and sealed before launch. The scratch, spin or box only reveals a result that was already decided. Every game also has a skill question and the free postal route, the same as any prize competition. A random result decided at the moment of each play would be gambling and need a Gambling Commission licence, which is why games don't work that way.

Wins are credited to the player's wallet when they reveal them. Unrevealed wins are credited automatically after 24 hours or when the game closes.

## Upgrading from v1 (your current server)

Your existing data is kept. The database upgrades itself on start.

```powershell
# on your PC (PowerShell)
scp "$HOME\Downloads\prizecomp.zip" root@187.124.208.90:/opt/
```

```bash
# on the server
cd /opt
cp -r prizecomp/data prizecomp-data-backup-$(date +%F)   # safety copy
unzip -o prizecomp.zip
cd prizecomp
rm -f Caddyfile
docker compose up -d --build
```

Then add the new optional settings to `.env` (see `.env.example`), especially the email settings, and run `docker compose up -d` again.

## Fresh install (nginx already on the server)

```bash
cd /opt && unzip prizecomp.zip && cd prizecomp
cp .env.example .env
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')|" .env
nano .env                       # set SITE_URL etc.
mkdir -p data && chown -R 1000:1000 data
docker compose up -d --build
cp deploy/nginx-prizecomp.conf /etc/nginx/sites-available/prizecomp   # edit server_name if needed
ln -s /etc/nginx/sites-available/prizecomp /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d YOUR.DOMAIN --redirect
```

No account gets admin access automatically. After signing up, give yourself admin from the server:

```bash
docker compose exec web flask --app wsgi make-admin you@example.com
docker compose exec web flask --app wsgi list-admins
docker compose exec web flask --app wsgi remove-admin someone@example.com
```

## Email (recommended)

With Hostinger email, set these in `.env`:

- `SMTP_HOST=smtp.hostinger.com`
- `SMTP_PORT=465`
- `SMTP_USER` and `SMTP_PASSWORD`: your mailbox and its password
- `MAIL_FROM`: the same mailbox

Without SMTP, emails are only written to `docker compose logs`, and password resets won't reach users.

## Stripe

1. Get your secret key under **Developers → API keys**.
2. Under **Webhooks → Add endpoint**, use `https://YOUR.DOMAIN/stripe/webhook` with these events:
   - `checkout.session.completed`
   - `checkout.session.expired`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.async_payment_failed`
3. Put the key and the signing secret in `.env`, set `DEMO_PAYMENTS=0`, then run `docker compose up -d`.

The minimum card payment is 30p. Baskets paid entirely with wallet credit skip Stripe.

## Running a competition

1. Open **Admin → New competition**. It's saved as a draft.
2. Optionally add **instant wins** while it's still a draft. They lock as soon as any ticket exists.
3. Press **Publish**.
4. Log postal entries as they arrive.
5. When the competition ends or sells out, press **Run draw**. The winner is emailed and posted to Discord.
6. Contact the winner. If they're happy to, add their photo and a quote.

## Before taking real money

- Have a solicitor check `/terms` and `/privacy`. They're templates.
- Set a real `POSTAL_ADDRESS`, `SUPPORT_EMAIL` and `COMPANY_DETAILS`.
- The DCMS voluntary code bans **credit cards for instant-win competitions**. Stripe Checkout can't block credit cards by itself. Use a Stripe Radar rule (`Block if :card_funding: = 'credit'`; Radar for Fraud Teams), or don't run instant wins until you can.
- Age checks are self-declared (date of birth only). Consider adding an age or ID verification provider.

## Backups

```bash
./backup.sh
```

To run it every night at 3am, add this line with `crontab -e`:

```
0 3 * * * cd /opt/prizecomp && ./backup.sh
```

## Development

```bash
ALLOW_DEV_KEY=1 DEMO_PAYMENTS=1 flask --app wsgi run --debug
python -m unittest discover tests
```

## v5 overhaul (site structure)
- Menu: Competitions (`/competitions`) · Instant Wins (`/instant-wins`) · Winners (`/winners`, with Draw results and Live draws tabs) · How it works (`/how-it-works`).
- Old links still work: `/games`, `/results`, `/live` and `/?tab=` redirect permanently to the new pages.
- New pages: Contact form (`/contact`), Cookies (`/cookies`), order details (`/account/orders/<id>`).
- Basket: change quantities, apply a promo code and see the final card amount before paying. A cancelled payment puts the basket back.
- Account tabs: Overview · My entries · Wins · Wallet · Orders · Rewards · Settings.
- Admin dashboard shows a "Site setup" checklist until POSTAL_ADDRESS, COMPANY_DETAILS, SUPPORT_EMAIL, SMTP and Stripe are set.
- QA: `python3 -m unittest tests.test_app` and `python3 tests/qa_crawl.py` (crawls every page as guest/player/admin).

## v8 rebuild (integrity, journeys, navigation)
- **Read first:** `docs/AUDIT.md` (every URL, what changed, what still needs a decision) and `docs/QA-CHECKLIST.md` (run before every release).
- **Draws** only run after the advertised closing time and keep a permanent snapshot of every eligible entry; results, draw records and the audit log can't be edited (database triggers). Drawn competitions can't be deleted.
- **Postal entries:** record every envelope with the date it arrived. The system accepts or rejects it (late, wrong answer, under 18, over the per-person limit, sold out) and keeps the record. Entries whose email matches an account appear in that account.
- **Admin roles:** `make-admin` creates an **owner** (everything). From Admin → Users you can give someone **staff** access instead: competitions, postal entries, draws and prizes, but no payouts, wallets, users, promos, stats or resets. Existing admins stay owners.
- **Audit log:** Admin → Audit log shows every sensitive action and who did it.
- **Withdrawals:** mark a request **Processing** while you pay it, then **Paid — money sent** only once it's gone.
- The database upgrades itself on start; no manual steps.

## v9 platform (operations, automation, reliability)
- **Admin home is now the Control Centre** (`/admin/`): money today, entries, sign-ups, live and ending competitions, instant prizes left, and a list of everything that needs a person, plus system health.
- **Roles:** Support, Competition manager, Finance, Administrator (Admin → Customers → the person → Admin access). `make-admin` creates an Administrator.
- **Two-step verification** is required for every admin on their next sign-in (any authenticator app). Set `ADMIN_MFA=0` only for local development.
- **Worker container:** `docker compose up -d` now also starts `worker`, which closes competitions, runs draws, sends and retries emails, checks health and raises review flags every 20 seconds, even when nobody is browsing.
- **Postal entries:** log envelopes as *received* the day they arrive, then approve or reject them in Admin → Postal. A competition can't close or draw while envelopes are waiting.
- **Backups:** `./backup.sh` now runs `flask backup`, which restore-tests every backup. Set `BACKUP_REMOTE` for an off-site copy (rclone). Point an uptime monitor at `/healthz`.
- **Staging:** see `docs/STAGING.md`. **Emergencies:** `docs/RUNBOOK.md`. **Mechanics:** `docs/MECHANICS.md`. **Personal data:** `docs/PRIVACY-DATA-AUDIT.md`.

## v11 operations (Phase 3)
- **Pre-launch checks:** a competition can't go live (or launch on schedule) until its checklist passes — description, image (`REQUIRE_COMP_IMAGE=0` to relax), price, limits, closing time, question, instant prizes sealed, `POSTAL_ADDRESS` set, payments configured. The checklist is on the competition's admin page.
- **Draw-readiness checks** run before every draw (manual or automatic): closing time passed, no open reservations, postal queue processed, frozen entry list present and matching, every ticket paid and owned. A failure blocks the draw and is logged once.
- **Tamper-evident audit log:** each entry stores a SHA-256 of the previous one. Admin → Audit log → *Verify* recomputes the chain; it also has filters (who, action, target, text, dates) and CSV export.
- **Append-only money:** wallet and points histories can't be edited or deleted (database triggers); corrections are new adjustment lines. Draw records and frozen entry lists can't be deleted either. Only *Start fresh* (test data) can clear them.
- **Integrity job** (every 6 h) and **daily Stripe reconciliation** report to System health and Flags. **Unusual activity** (refund, withdrawal, sign-up or prize spikes) is checked continuously and emailed to `SUPPORT_EMAIL`.
- **Feature flags:** Admin → Settings → Features — Everyone / Staff only / Off, no deploy.
- **Support desk:** cases sorted by priority then waiting time; opening a case marks it as yours for 20 minutes so two people don't reply; *Take over* is logged; customer history alongside.
- **Prize liability:** Admin → Liability — prizes won but not delivered, instant prizes still to win, live draw prizes, and money held in wallets.
- **Security:** attack tests on every build (`docs/SECURITY-REVIEW.md`); CSP, HSTS and secure cookies on HTTPS.
- **Disaster recovery:** `docker compose exec web flask --app wsgi dr-drill` restores the newest backup into a clean copy and checks it (quarterly; shown on System health). Plan, RTO/RPO and roles: `docs/DISASTER-RECOVERY.md`.
- **Quality gates in CI:** tests, crawl, performance budgets, keyboard + axe-core walkthrough (`docs/TESTING.md`).
- **Reports & targets:** Admin → Reports (definitions for your accountant: `docs/REPORTING-DEFINITIONS.md`), Admin → Targets (checkout completion, payment problems, support load, errors, server time, plus uptime/PageSpeed readings you record) and the server-error log.
- **How we work:** `docs/DEPLOY.md` (dev → staging → prod, rollback), `docs/DEFINITION-OF-DONE.md`, `docs/USABILITY-TEST.md` — run the usability test before adding more features.

## v12 production hardening (Phase 4)
- **Read first:** `docs/PRODUCTION-AUDIT.md` (audit of every area), `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/DEPENDENCIES.md`, `docs/MONITORING.md`, `docs/CAPACITY.md`, `docs/PENTEST-SCOPE.md`, incident procedures in `docs/RUNBOOK.md` (§1–16), release checklist in `docs/DEPLOY.md`.
- **Database safety rules** stop overselling, double payouts and edits to final records even if the code had a bug.
- **Admin:** sensitive actions ask you to confirm it's you (password/MFA) if you haven't in 10 minutes; admin times out after 30 minutes idle; wallet adjustments over £100 need an Administrator (`ADMIN_IDLE_MINUTES`, `ADMIN_STEPUP_MINUTES`, `LARGE_ADJUSTMENT`).
- **Monitoring:** `/healthz/deep` for UptimeRobot; scheduled GitHub uptime check (set repository variable `SITE_URL`); new alerts for payment-rate drops, blocked draws, slow pages, database errors; immediate emails for new server errors and chargebacks.
- **Releases:** `docker compose exec web flask --app wsgi release-check` before and after every deploy.
- **Stripe webhook:** add the events `charge.refunded` and `charge.dispute.created`.
- **nginx:** updated `deploy/nginx-prizecomp.conf` (caching, gzip, login rate limit) — merge it with certbot's HTTPS lines.
- **Phase 5 groundwork:** support cases are tagged with what the customer needed (required to resolve); Admin → Cases → *Support insights* shows the top reasons, trend and where the fix belongs, next to the journey funnel (now including "started choosing entries").

## v13 customer experience & growth (Phase 6)
- **Customers:** personal homepage panel; My tickets grouped by draw date; results marked "You entered / You won"; a winner page to choose prize or cash, give delivery details and follow progress; draw calendar (`/draws`); typo-tolerant search incl. winners; points dashboard and non-spending milestones; security activity log; CSV downloads of orders/entries/transactions; one-tap feedback.
- **Staff:** Control Centre work queues; universal search; order page with single-line refunds (wallet or card); goodwill credit with limits (`GOODWILL_LIMIT`); promotions with scheduling and rules; scheduled announcements; content & legal pages editor with version history; versioned competition conditions per entry; winner share cards; daily and weekly summary emails (`REPORT_EMAILS`); release dashboard; checkout diagnostics, segments, experiments; feedback, support insights and development backlog. Admin menu grouped into Reports and More.
- **Staging demo:** `docker compose -p dbx-staging exec web flask --app wsgi demo-lifecycle` runs a whole competition with test money.
- **How we work from here:** `docs/SIMPLICITY-REVIEW.md` — measure → find a problem → investigate → improve → test → release → measure again.


## v14 trust, transparency & customer confidence (Phase 7)
- **Customers:** every competition shows its stage (Live → Closing soon → Closed → Draw pending → Draw complete → Prize delivered); every finished draw has a plain-English **draw record** (`/c/<slug>/draw`) with the frozen entry list to download, the checks run before and after the draw, and step-by-step instructions to recompute the winner; results filter by month and category; order pages are permanent **receipts** (reference DBX-000123); support forms arrive pre-filled with the order, competition or withdrawal; **Service status** page (`/status`); **Help Centre** with 12 searchable topics (`/faq`); one **account activity** history (`/account/activity`); **download all my data** (JSON); new-device and password-reset security notices; errors show a reference (DBX-XXXXXX) instead of technical detail.
- **Staff:** **four-eye approvals** for large wallet adjustments, redraws and admin-access changes (`four_eyes` setting: auto/on/off); **emergency lock** to stop all purchases while keeping accounts working (optionally holding automatic draws); **after-draw checks** with alerts; operations calendar; risk dashboard; customer emails log with retry; email previews; **compliance sign-off** per competition mechanic (new mechanics can't be published until signed off); written **retention rules** applied nightly; error-reference search.
- **Database:** wallet balances can't go negative (trigger); approvals and sign-offs are permanent.
- **How we decide what to build:** `docs/DEVELOPMENT-BOARD.md` (four columns, four questions). Next milestone: `docs/FIRST-100-USERS.md`.
- New docs: `docs/RETENTION.md`, `docs/NEW-MECHANIC-CHECKLIST.md`, RUNBOOK §17–20.
