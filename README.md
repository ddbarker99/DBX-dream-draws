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
