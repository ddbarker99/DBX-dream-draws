# Releasing safely: tests, staging, production

Nobody experiments with competition or payment logic on the live site. Every change goes **tests → staging → production**.

## 1. Automated tests (every change)

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests/test_app.py     # 125+ tests: registration, login, ticket allocation, limits, closing,
                                          # postal entries, checkout, wallets, discounts, refunds, withdrawals,
                                          # instant wins, draws/redraws, permissions, MFA, notifications, backups
python tests/qa_crawl.py                  # every page as guest / player / admin: no errors, broken links, SEO gaps
```
These also run automatically on GitHub for every push and pull request (`.github/workflows/tests.yml`). **Don't deploy a red build.**

## 2. Staging (every release)

A second copy of the site on the same server, with its own data, Stripe **test** keys and `STAGING=1`:
- a red "STAGING" bar on every page, `noindex` everywhere,
- **every email goes to SUPPORT_EMAIL** instead of the customer (subject starts `[STAGING]`).

One-off setup:
```bash
cd /opt/prizecomp
cp .env .env.staging
nano .env.staging      # SITE_URL=https://staging.YOURDOMAIN, STRIPE_SECRET_KEY=sk_test_..., STRIPE_WEBHOOK_SECRET (test endpoint),
                       # a different SECRET_KEY, SIGNUP_RATE_LIMIT=1000 (for load tests)
docker compose -p dbx-staging -f docker-compose.staging.yml up -d --build
```
Add an nginx server block for `staging.YOURDOMAIN` → `127.0.0.1:8100` (copy `deploy/nginx-prizecomp.conf`), protected with HTTP basic auth so the public can't find it.

Each release:
1. Upload the new code into a separate folder (e.g. `/opt/prizecomp-staging-src`), or check out the release branch there.
2. `docker compose -p dbx-staging -f docker-compose.staging.yml up -d --build`
3. Optionally load a copy of production data: `cp data/backups/<latest verified>.db data-staging/prizes.db` (staging emails only go to staff, but still treat it as personal data).
4. Work through `docs/QA-CHECKLIST.md` — at minimum sections 2–6, 9 and 14 — using Stripe test cards.
5. Before a big launch or a popular closing: `python tests/load_test.py https://staging.YOURDOMAIN --users 200 --seconds 120 --slug <competition> --buyers 50 --answer <a|b|c>` → must say PASS (no errors, p95 under 2s, only one buyer got the contested number).

## 3. Production

```bash
./backup.sh                               # verified backup immediately before deploying
# upload/unzip the same code that passed staging, then:
docker compose up -d --build
```
Then the 2-minute smoke test in `QA-CHECKLIST.md` §14, and watch Control Centre → System health for 15 minutes.

**Rolling back:** redeploy the previous zip/commit. The database upgrades itself forwards only (new columns/tables); older code ignores them, so rolling back the code is safe without restoring data.
