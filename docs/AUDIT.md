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
