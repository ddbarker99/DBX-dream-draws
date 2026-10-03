# Third-party dependencies

Everything DBX relies on that we don't run ourselves: what it does, what happens if it's unavailable, and how to replace it. Review yearly and whenever one is added.

## Services

| Provider | What it does for us | If it's unavailable | How we notice | Replacing it |
|---|---|---|---|---|
| **Stripe** (payments) | Card / Apple Pay / Google Pay checkout pages, refunds, webhooks, card-type check (credit cards refused) | Customers can't pay. After 3 errors in 10 min the site **pauses payments automatically** with a message; baskets are kept; nothing is charged twice; reservations expire; late payments are flagged for refund. Browsing, accounts, draws, withdrawals continue. | Health → Card payments, Payment success rate; status.stripe.com | Another hosted-checkout provider (e.g. Checkout.com, Adyen, PayPal). Touch points: `app/payments.py` (create session, refund, verify webhook, card funding, list sessions/refunds) and the webhook route in `routes.py`. Store their payment id in `checkouts.payment_intent`. |
| **SMTP email** (e.g. Hostinger mail) | Verification, password reset, order confirmations, results, withdrawals, staff alerts | Emails queue and retry 5 times; in-app notifications still appear; purchases unaffected. Password resets can't reach customers. | Health → Email sending; failed-email count on Targets | Any SMTP service (Postmark, SES, Mailgun): change `SMTP_*` in `.env`. Set SPF/DKIM for the domain. |
| **Hostinger VPS** (hosting) | Runs nginx, Docker, the app and the database | Site down. Draws are late but never wrong (they run when it's back). | UptimeRobot on `/healthz/deep`; GitHub uptime workflow | Any VPS: follow `docs/DISASTER-RECOVERY.md` §4 with the off-site backup. |
| **Domain registrar + DNS** | `dbxdreamdraws.co.uk` | Site unreachable by name | Uptime monitor | Transfer domain; keep registrar login with 2FA in the password manager. |
| **Let's Encrypt** (TLS certificates via certbot) | HTTPS | Certificates expire after 90 days if renewal fails → browser warnings | certbot's renewal timer; uptime monitor sees TLS errors | Any CA; or a proxy such as Cloudflare in front. |
| **Off-site backup storage** (rclone remote, `BACKUP_REMOTE`) | Copies of nightly backups away from the VPS | Backups stay only on the server (risk if the server is lost) | `backup.sh` output / cron mail | Any rclone-supported storage (Backblaze B2, S3, Google Drive). |
| **Discord webhook** (optional) | Posts new competitions and results | Announcements don't post; nothing else affected | Logs | Remove `DISCORD_WEBHOOK_URL` or swap for another webhook. |
| **UptimeRobot / GitHub Actions** (monitoring) | Outside checks of the live site | We'd hear about outages from customers instead | — | Any HTTP monitor (Better Stack, Pingdom). |
| **GitHub** | Code, CI, uptime workflow | Can't run CI; site unaffected | — | Any git host + CI. |

No analytics, advertising, chat, font or CDN services are used: fonts, scripts and images are self-hosted, so pages load nothing from third parties (see the Content-Security-Policy).

## Software (inside the container)

| Package | Version pin | Purpose | Notes |
|---|---|---|---|
| Python | 3.12 (Docker `python:3.12-slim`) | Runtime | Security updates: rebuild the image monthly (`docker compose build --pull`). |
| Flask | 3.1.* | Web framework (includes Werkzeug, Jinja2, itsdangerous, click) | Werkzeug provides password hashing. |
| gunicorn | 23.* | Production web server (3 processes × 4 threads) | |
| requests | 2.32.* | Stripe and Discord HTTP calls | |
| Pillow | 11.* | Resizing uploaded photos | Only processes images staff upload. |
| SQLite | bundled with Python | Database | WAL mode; single writer. See CAPACITY.md for when to move to PostgreSQL. |
| nginx, certbot, Docker | from Ubuntu | Reverse proxy, TLS, containers | Keep the VPS patched: `apt update && apt upgrade` monthly. |

Test-only: pytest, Playwright + Chromium, axe-core (downloaded in CI).

Check for vulnerable packages before each release: `pip install pip-audit && pip-audit -r requirements.txt`.
