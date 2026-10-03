# DBX Dream Draws — QA checklist

Run this before every release, and in full before switching Stripe to live keys. **Auto** means an automated test covers it (`python -m pytest tests/test_app.py`, plus `python tests/qa_crawl.py` for links, titles, labels and errors on every page). **Manual** means a person must do it in a real browser, on a real phone, or with a real payment.

Record results in the last column: ✅ pass · ❌ fail (with a note) · — not applicable this release.

## 0. Automated gates (must be green before anything else)

| Check | How | Result |
|---|---|---|
| Unit and integration tests | `python -m pytest -q tests/test_app.py` | |
| Crawl every page as guest, player and admin: no 4xx/5xx, no broken links, one `<h1>`, meta description, labelled fields, image alt text, no duplicate titles | `python tests/qa_crawl.py` → `NO PROBLEMS FOUND` | |
| Accessibility (axe-core, WCAG 2.2 AA rules) and keyboard-only walkthrough at 390px and 1280px | `AXE_JS=… python tests/a11y_keyboard.py` → `NO PROBLEMS FOUND` | |
| Performance budgets | `python tests/perf_budget.py` → `ALL WITHIN BUDGET` | |
| Security attack tests | part of the unit tests (`SecurityTests`) | |

## 1. Browsers and devices (manual)

Repeat the journeys in sections 2–6 on each:

| Browser / device | Width | Result |
|---|---|---|
| Chrome (desktop) | 1440 and 1280 | |
| Safari (Mac) | 1280 | |
| Edge (Windows) | 1280 | |
| Safari (iPhone) | 390 | |
| Chrome (Android) | 360–412 | |

For each: no horizontal scrolling; header, basket and menu reachable one-handed; sticky "Enter now" bar never covers the form or the footer; tap targets at least 44px; menu opens, closes with Escape, and keyboard focus is visible.

## 2. Logged-out visitor

| Check | Type | Result |
|---|---|---|
| Home explains what the site is within the first screen; one main button (View competitions) | Manual | |
| Main menu: Home, Competitions, Instant Wins, Winners, How It Works, Free Entry — same on every page; mobile menu matches | Manual | |
| Competitions: Live / Ending soon / New / instant prizes filters; closed draws shown separately | Auto + manual | |
| Competition page shows prize, price, draw date, availability, per-person limit and the entry form without scrolling past large images (phone) | Manual | |
| Free entry is visible next to the paid route on every competition page | Auto | |
| Add to basket without an account, then sign up — basket is kept | Auto | |
| Old URLs redirect: `/games`, `/results`, `/live`, `/?tab=ending` | Auto | |

## 3. New customer

| Check | Type | Result |
|---|---|---|
| Sign-up errors appear next to each field and in a summary (missing surname, bad email, short password, under 18, terms not ticked) | Auto + manual | |
| Password rules shown before submitting | Manual | |
| Verification email arrives; link confirms; expired link explains what to do | Manual (needs SMTP) | |
| Daily free play needs a confirmed email | Auto | |

## 4. Entering and paying

| Check | Type | Result |
|---|---|---|
| Lucky dip: quantity × price = total updates live; multi-buy discount shown | Manual | |
| Pick numbers: taken numbers crossed out; picking a number someone else just bought is refused **when adding**, not at checkout | Auto | |
| Number taken while sitting in the basket: basket flags it and blocks checkout until removed | Auto | |
| Per-person limit: picker capped at remaining allowance; postal entries count too | Auto | |
| Basket shows each line (entries, unit price, subtotal), discounts, site credit / deposited funds / cash used, and the amount due | Manual | |
| Double-click "Pay": one checkout, one set of reserved numbers, one payment page | Auto + manual | |
| Button shows "Taking you to payment…" and can't be pressed again | Manual | |
| Refresh / Back during payment: no duplicate order; returning to `/checkout/<id>/pay` resumes the same payment | Auto + manual | |
| Cancel on Stripe: "you haven't been charged", basket restored | Auto | |
| Leave payment for 30+ minutes: reservation ends, numbers released, page explains, basket kept | Manual (Stripe test) | |
| Successful payment: "Your entries are confirmed" only after the server has confirmed it; shows reference, competitions, ticket numbers, amount paid, draw dates, link to My entries | Auto + manual | |
| Webhook arrives before/after the redirect, or twice: tickets issued once | Auto | |
| Amount reported by Stripe differs from the amount asked: nothing issued, flagged for refund | Auto | |
| Failed / declined card (Stripe test card `4000 0000 0000 0002`): useful message, basket kept | Manual (Stripe test) | |
| Credit card (Stripe test credit card): refunded automatically, no tickets, email sent | Manual (Stripe test) | |
| Confirmation email matches the screen | Manual (needs SMTP) | |
| Payment provider down (bad key): "you have not been charged", basket kept | Auto | |

## 5. Returning customer, wallet and withdrawals

| Check | Type | Result |
|---|---|---|
| Overview shows active entries, upcoming draws, recent transactions, cash balance, site credit and points | Manual | |
| My entries: Active / Won / Previous; each links to the competition or its result; empty states offer a next step | Auto + manual | |
| Customer with wallet credit: credit used first at checkout; can untick to pay by card | Auto | |
| Cash vs site credit vs deposited funds clearly labelled; only cash is withdrawable | Auto + manual | |
| Withdrawal: minimum and ID note shown up front → review step shows details → request → status page (Requested → Processing → Paid) | Auto + manual | |
| Withdrawal over cash balance, bad sort code, unverified email: clear error, nothing deducted | Auto | |
| Admin returns a withdrawal: money back in cash balance, status "Returned", email sent | Auto | |
| Another customer can't open your order or withdrawal URL (404) | Auto | |

## 6. Responsible play

| Check | Type | Result |
|---|---|---|
| Lower a limit: applies to the very next checkout | Auto | |
| Raise a limit: doesn't apply for 72 hours (server-side); page shows what's waiting and when | Auto | |
| Raise, then lower before 72 hours: the raise is cancelled | Auto | |
| Take a break: checkout, deposits, free play and instant-win purchases all refused; a checkout already at Stripe is released and a late payment refunded | Auto | |
| On a break: can still log in, see entries, withdraw cash | Manual | |

## 7. Instant wins

| Check | Type | Result |
|---|---|---|
| Before buying: price, prizes, number of winning plays, per-person limit and free entry visible | Manual | |
| Result is fixed before the animation: refresh mid-reveal shows the same result; revealing twice never pays twice | Auto | |
| Unrevealed prizes are paid after 24 hours or when the game closes | Auto | |
| Animation off (OS "reduce motion"): results still shown | Manual | |

## 8. Free entry (postal)

| Check | Type | Result |
|---|---|---|
| Free entry page lists exactly what to write, where to send, deadline, limits and rejection reasons | Manual | |
| Admin records an envelope: accepted gets a random ticket and email; matched account sees it in My entries | Auto | |
| Arrived after closing / wrong answer / under 18 / over the per-person limit / sold out: rejected with that reason, logged | Auto | |
| Future arrival date refused | Auto | |
| Every envelope appears in the postal log with who recorded it, and in the audit log | Auto | |

## 9. Draws, winners and integrity

| Check | Type | Result |
|---|---|---|
| Draw refused before the advertised closing time, even if sold out | Auto | |
| Draw waits while checkouts are still being paid | Auto | |
| Draw record stores every eligible entry number, method, who ran it and the winner; can't be edited | Auto | |
| Recompute the winner from the public entry list and revealed seed using the Fair Draws code | Auto + manual | |
| Drawn competition can't be deleted; result fields can't be changed | Auto | |
| Winner photo/quote hidden until permission is recorded | Auto | |
| Winner email arrives; winners page and results tab update | Manual (needs SMTP) | |
| Close a competition while someone has numbers reserved: their payment still completes within the reservation, draw waits | Manual | |
| Two people select the same number at the same moment: only one gets it, the other is told immediately | Auto (concurrency test) + manual | |

## 10. Cancellations and refunds

| Check | Type | Result |
|---|---|---|
| Cancel a competition: every entrant refunded exactly what they paid, card/cash as cash, site credit as credit; running it twice doesn't double-refund | Auto | |
| Payment completed after cancellation: flagged for refund, no tickets | Auto | |
| Refund of unspent deposits goes back to the card | Auto | |

## 11. Admin

| Check | Type | Result |
|---|---|---|
| Staff account can run competitions, postal entries and draws but not payouts, users, promos, stats or start-fresh | Auto | |
| Every sensitive action appears in the audit log with who and when | Auto | |
| Editing a live competition's closing time into the past is refused; any change is logged old → new | Manual | |

## 12. Emails (manual, needs SMTP)

Trigger each and check subject, wording, links and that it renders on a phone: email confirmation · password reset · entries confirmed · instant-win games ready · winner · postal entry confirmed · deposit added · deposit refunded · withdrawal requested · withdrawal paid · withdrawal returned · credit card refused · contact form acknowledgement.

## 13. Account and privacy requests (manual)

| Check | Result |
|---|---|
| Password reset: link works once, expires after 1 hour, signs out other devices | |
| Expired session: next action asks to log in and returns to the same page | |
| Data access / account closure request via the contact form is answered within the policy's timescale | |

## 13b. Platform (admin, automation, reliability)

| Check | Type | Result |
|---|---|---|
| Admin sign-in asks for MFA; wrong code refused and logged; recovery code works once | Auto | |
| Support / Competition manager / Finance roles can only open their own areas | Auto | |
| Competition closes at its time only after payments and postal envelopes are resolved; entry list frozen (counts shown) | Auto | |
| Redraw needs a reason, excludes previous winners, recomputes by the published formula, original kept | Auto | |
| Prize claim moves through every status with notes/evidence; delivered → competition Completed | Auto | |
| Postal queue: receive → approve/reject; invalid entries can't be approved | Auto | |
| Control Centre shows today's money, entries, sign-ups, attention items and health | Auto + manual | |
| Finance report balances (sources = entry value); payments CSV matches Stripe's report line by line | Auto + manual (Stripe) | |
| Customer timeline lists orders, tickets, wallet, prizes, withdrawals, limits, cases; viewing is audited | Auto | |
| Flags raised for shared phones / failed payments; review and dismiss | Auto | |
| Contact form opens a case; reply emails the customer | Auto | |
| Notifications appear once each in the account even if a webhook repeats | Auto | |
| Pause payments / maintenance mode; automatic pause after provider errors | Auto | |
| `flask backup` passes its restore test; Health shows the verified time | Auto | |
| Staging banner, noindex, emails go to staff only | Auto | |
| Worker container running (`docker compose ps`); Health → Background jobs green | Manual | |
| Load test on staging passes before launches | Manual | |
| Publishing a competition with a missing description / image / question / postal address is refused with the reason; checklist shown | Auto | |
| Draw blocked (and logged once) when the frozen entry list is missing or doesn't match | Auto | |
| Audit log: Verify says intact; altering an entry outside the app is detected; filters and CSV export work | Auto | |
| Wallet / points history can't be edited or deleted; Start fresh still clears test data | Auto | |
| Integrity job: no problems on real data; a negative balance is reported on System health | Auto | |
| Stripe reconciliation: unfulfilled payment, unknown payment, refund done in Stripe, amount mismatch, paid-here-not-at-Stripe all flagged | Auto + manual (Stripe test) | |
| Feature flags: Off hides the feature and its links; Staff only shows it to admins; changes logged; Support role can't change them | Auto | |
| Support case: second staff member sees "X is working on this case" and can't reply until Take over (logged) | Auto | |
| Prize liability page totals match the competitions and wallets | Auto + manual | |
| `flask dr-drill` passes on the server; System health shows the drill | Manual (quarterly) | |
| Reports page figures for a test day match a hand count (and Stripe for card totals) | Auto + manual | |
| Targets page: a forced error appears in the error log and on System health | Auto | |

## 14. Production smoke test (after every deploy)

Click every link in the header, footer and mobile menu, and every button on: home, one competition, one game, basket, checkout (Stripe test mode on a staging copy), account (every tab), free entry, terms. Confirm `DEMO_PAYMENTS=0`, Stripe live keys, webhook secret and SMTP are set (the admin dashboard's setup checklist shows any that are missing).
