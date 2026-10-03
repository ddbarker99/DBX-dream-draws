# DBX Dream Draws — emergency procedures

Decide calmly, from this page, not under pressure. Every procedure ends with **record what happened** (Admin → Audit log shows what the system did; add your notes to a support case or the competition's history).

**First look:** Admin → Control Centre (needs attention + system health) and Admin → Health (job runs, failed emails). Public uptime check: `https://YOURDOMAIN/healthz`.

**Golden rules**
1. Never edit the database by hand to change entries or a result. Draw records, entry snapshots and the audit log are protected by triggers for a reason.
2. Never run a draw early, and never "re-run" a draw to get a different winner. A redraw is only for a winner who can't receive the prize under the terms, and it records a reason publicly.
3. If unsure, **pause payments** (Admin → Settings → Site status) — it stops new money coming in while you think, and customers see your message.
4. Tell customers what's happening (status message, Discord, email) — silence damages trust more than delay.

---

## 1. The website goes down shortly before a draw

Draws don't need the website to be up — the `worker` container runs them. But the entry list is only frozen once the closing time has passed **and** all in-progress payments and postal entries are resolved.

1. Check containers: `docker compose ps`. Restart: `docker compose up -d`.
2. Check logs: `docker compose logs --tail=200 web worker`.
3. If the site was down **before** the closing time, customers couldn't enter for part of the period. Decide with the terms (section 22): either let the draw run at the advertised time (normal) or, if the outage was long and material, postpone — **only** by editing the closing time *before* it passes (a live competition's closing time can't be moved into the past, and every change is logged old → new) and announcing it. Never change the time after it has passed.
4. Once back: Control Centre → "Draws due" should show the competition; the worker draws it within a minute.

## 2. The payment provider (Stripe) fails

Symptoms: "Card payments" health check red, customers report errors, Control Centre shows failed checkouts.

1. The site **pauses card payments automatically** after 3 provider errors in 10 minutes (for 15 minutes) and shows customers a message. To pause for longer: Admin → Settings → Site status → *Pause payments*, with a message.
2. Check https://status.stripe.com.
3. Customers' numbers are only reserved while they pay; failed checkouts release them automatically. Nobody is charged twice: every checkout has a one-time key and Stripe confirmations are idempotent.
4. Payments that arrive late or don't match are flagged **Refund due** (Admin → Payouts). Refund them in Stripe, then mark them refunded on that page.
5. When Stripe recovers: set Site status back to *Normal*.

## 3. A draw job fails or a draw is overdue

Symptoms: "Automatic draws" health check red ("overdue"), Control Centre "Competitions past closing that haven't closed".

1. Admin → Health → recent job runs: read the error.
2. Most common cause: **postal entries still waiting** for that competition. Process them in Admin → Postal. The competition then closes and draws automatically.
3. Second cause: a checkout still within its reservation. Wait — it resolves within 45 minutes of starting.
4. If the job keeps failing with an error: run the draw manually from the competition's admin page (Competition manager or Administrator, with MFA). It uses the same frozen snapshot and method and is recorded as *manual*.
5. If the draw refuses because "the entry list doesn't match the closing snapshot": **stop**. Don't draw. Pause payments, restore the latest verified backup to a scratch copy (`flask --app wsgi restore-test data/backups/<file>`) and investigate with a developer. Announce a short delay.

## 4. Entries become temporarily unavailable (entering is broken)

1. Pause payments with a clear message.
2. Check `docker compose logs web`, disk space (Health → Disk space), database (Health → Database integrity).
3. If the database integrity check fails: put the site in **maintenance mode**, stop the containers, copy `data/prizes.db` aside, restore the latest backup that passed its restore test (see section 7), restart, and reconcile any payments taken since the backup using Admin → Finance → payments CSV and Stripe's dashboard.

## 5. An incorrect result is displayed

First establish *what* is wrong.

- **The page shows the wrong winner/number but the draw record is right** (display bug): put the site in maintenance mode if it's public-facing, fix and redeploy, then post a correction. The draw record is the truth.
- **The draw itself was made from the wrong entries or the winner is ineligible** (e.g. an entry that broke the rules): don't edit anything. If the winner can't receive the prize under the terms, mark their claim *Forfeited* with notes and evidence, then **Redraw** with the reason. The original draw and the reason stay public.
- **Something else** (a systemic fault affecting fairness): pause payments, preserve evidence (backup now: `./backup.sh`), contact your solicitor, and follow terms section 22 ("restore entrants to the position they should have been in"). Possible remedies include a re-run with the regulator's/solicitor's agreement, or refunds. Record every decision in a support case linked to the competition.

## 6. Emails stop sending

Health → Email sending red. Emails are queued and retried automatically (up to 5 times); nothing is lost while it's down.

1. Check SMTP settings in `.env` and the mailbox provider's status.
2. Health → Failed emails shows the error. Once fixed, press *Run jobs now* — the outbox retries.
3. Customers can still see everything in their account's notification centre.

## 7. Restoring from backup

Backups run nightly (`./backup.sh`) and each one is **restore-tested** (integrity check, opens with the current app, row counts, every draw recomputed). Only use a backup that passed — `backups.log` and Admin → Audit log (`backup.verified`) show which.

```bash
docker compose stop web worker
cp data/prizes.db data/prizes-broken-$(date +%F-%H%M).db         # keep the broken copy
cp data/backups/prizes-YYYYMMDD-HHMMSS.db data/prizes.db
tar xzf data/backups/files-YYYYMMDD-HHMMSS.tar.gz -C data            # uploads + evidence
docker compose up -d
```
Then: reconcile payments made after the backup time against Stripe (Admin → Finance), and tell affected customers.

## 8. Suspected account takeover or staff account compromise

1. Admin → Customers → the account → note recent activity on the **timeline** (sign-ins, withdrawals, payout destination changes).
2. Revoke admin access (Administrator only) if it's a staff account; reset their password (signs out every device) and re-enrol MFA.
3. Hold any pending withdrawals for the account (don't press Paid) while you contact the customer on known details.
4. Audit log shows every action that account took.

## 9. A draw is blocked by the readiness checks

The draw never runs while a blocking check fails; the reason appears on the competition's admin page (**Draw-readiness checks**) and once in the audit log (`draw.blocked`).
1. *Reservations still open / postal entries waiting:* wait for checkouts to finish (max 45 min) or process the postal queue. The draw then runs automatically.
2. *Frozen list missing / doesn't match the tickets, paid ticket without a paid order, ticket without an owner:* **stop.** These can't happen through the app; something changed the database directly. Don't draw. Take a copy of `data/prizes.db`, check Admin → System health → Data integrity, and restore the latest verified backup into staging to compare (section 7). Announce a short delay on socials if the draw time passes.

## 10. "Data integrity" is failing on System health

The integrity job (every 6 hours) found a condition that should be impossible (duplicate ticket, negative balance, prize paid twice, winner not matching the draw record, broken audit chain…).
1. Read the exact problem on System health. Don't "fix" rows by hand — wallet and points histories are append-only by design.
2. Money problems: correct with an adjustment (Admin → Customers → the customer → wallet adjustment, with a reason). The original line stays.
3. **Audit log chain broken** means a past entry was changed or removed outside the app. Treat as a security incident (section 8): secure the server, keep a copy of the database as evidence, and compare with the latest backup.

## 11. Stripe reconciliation mismatch (Flags → Reconciliation)

Every day the worker compares the last 3 days of Stripe payments and refunds with our records.
- *Paid at Stripe but nothing issued:* the webhook was missed. Refund the customer in Stripe (or issue entries by hand only if the competition is still open), then mark the flag reviewed with what you did.
- *Amount differs / no matching checkout:* check the payment in Stripe's dashboard; refund if in doubt.
- *Refunded at Stripe but entries still valid:* someone refunded in Stripe directly. Cancel/refund the entries in the app so the draw doesn't include them, or record why they stand.
- *Paid here but not at Stripe:* serious — check the checkout in Stripe; if it truly wasn't paid, contact an administrator before the draw.

## 12. A feature misbehaves on the live site

Admin → Settings → **Features**: switch it **Off** (or **Staff only** to keep testing it yourself). Takes effect immediately, no deploy; the change is in the audit log. Covers: Help & support centre, Save & remind me, wallet deposits, refer a friend, site search.

## 13. A customer's balance looks wrong

Balances are never stored as a single number: each is the sum of the customer's wallet history (Admin → Customers → the customer → Timeline). So "wrong" means either a missing/extra history line or a misunderstanding.

1. Open the customer's **timeline** and read every wallet line with its reason and reference (order, prize, refund, withdrawal, deposit). Compare with what the customer expected — most cases are cash vs site credit confusion (tag the case "Cash vs site credit").
2. Check Admin → System health → **Data integrity**: it would show negative balances, prizes paid twice, deposits or withdrawals without a matching line.
3. If money really is missing or extra: correct it with a **wallet adjustment** (needs a reason; over £100 needs an Administrator; you'll be asked to confirm it's you). Never delete or edit a history line — the database refuses anyway.
4. If several customers are affected at once: pause payments, take a backup (`./backup.sh`), and treat it as a bug — check Admin → Targets → Server errors and the recent release.
5. Reply to the customer with what happened and the adjustment reference. Tag the case.

## 14. Customer information may have been exposed (data breach)

Examples: a page showed another customer's details, an export was sent to the wrong person, a laptop with exports was lost, the server or an admin account was accessed by someone else.

1. **Contain** within the hour: revoke the access (password reset signs out every device; remove admin roles; rotate keys in `.env` — Stripe, SMTP, SECRET_KEY — and restart), put the site in maintenance mode if the leak is ongoing.
2. **Preserve evidence**: `./backup.sh`, copy `docker compose logs` to a safe place, export the audit log (Admin → Audit log → Export CSV). Don't delete anything.
3. **Assess**: whose data, which fields (`docs/PRIVACY-DATA-AUDIT.md` lists what's stored), how many people, how long, likely consequences.
4. **Report to the ICO within 72 hours** of becoming aware if it's likely to risk people's rights and freedoms (ico.org.uk → report a breach). If unsure, report — late reporting is worse. Record the decision either way.
5. **Tell affected customers without undue delay** if the risk is high (e.g. bank details, ID documents): what happened, what data, what you've done, what they should do.
6. Fix the cause, add a test that would have caught it, and record everything in a support case marked "complaint/incident".

## 15. Security incident (server, admin account or payment keys compromised)

1. Pause payments and turn on maintenance mode.
2. Rotate every secret: Stripe secret key and webhook secret (Stripe dashboard → Developers), SMTP password, `SECRET_KEY` (signs everyone out), the VPS root/SSH keys, Hostinger account password + 2FA.
3. Revoke admin access for any affected account; re-enrol MFA.
4. Check the audit log (verify the chain: Admin → Audit log → Verify), Flags → Payment reversal / Reconciliation, and Stripe's dashboard for refunds or payouts you didn't make.
5. If the server itself may be compromised, **rebuild it from clean** following `docs/DISASTER-RECOVERY.md` §4 using a backup from before the incident, rather than trying to clean it.
6. Follow §14 for customer data. Consider an independent security review before reopening.

## 16. Chargeback or refund made directly in Stripe

You'll get an email and a **Payment reversal** flag (Admin → Flags) as soon as Stripe tells us.
1. Open the flag: it names the checkout/deposit and customer.
2. Chargeback: gather evidence (order, tickets, terms acceptance, IP/device from the timeline) and respond in Stripe within its deadline. If the entries should no longer count and the draw hasn't happened, cancel/refund through the app so records match.
3. A refund done in Stripe's dashboard doesn't remove entries or wallet credit by itself — decide, then use the app (refund, adjustment) so both sides agree. Mark the flag reviewed with what you did.

## 17. Something serious is wrong with payments, ticket allocation or a competition (emergency lock)
1. **Admin → More → Emergency lock** (or the link in the Control Centre's System health box). Say briefly what's wrong
   (audit log only) and press **Stop all purchases now**. Tick *hold automatic draws* if a draw could be affected.
2. Customers can still log in, see tickets and results, withdraw and contact support. They see a calm message and the
   **/status** page says entries are paused — they are never told why.
3. Payments already completed on Stripe are still recorded; refund any that shouldn't stand from the order page.
4. Investigate (integrity checks on System health, Risk dashboard, audit log). Fix, then **Lift the lock**. Overdue
   automatic draws run within a minute, with their readiness checks.
5. If customers were affected, tell them (announcement or email) and record what happened on the Development board.

## 18. An after-draw check failed
You'll get an email and the Risk dashboard shows **Act now**. Don't announce the result or pay the prize yet.
1. Open **Admin → Reports → Risk dashboard → After-draw checks** — it lists which check failed.
2. Compare the competition's draw record (public page `/c/<slug>/draw`) with the entries page in admin.
3. If the winner record is wrong but the draw itself reproduces, correct the record with the developer; if the draw
   doesn't reproduce, use the emergency lock (section 17) and treat it as a security incident (section 15).
4. When resolved, press **Mark looked into** (recorded in the audit log).

## 19. A customer quotes an error reference (DBX-XXXXXX)
Paste it into the admin **Search** box. You'll see when it happened, the page, the account and the technical detail
(personal data removed). Fix or mark it fixed; reply to the customer.

## 20. A large adjustment, redraw or admin-access change needs approving
These need a second administrator (four-eye approval). The request shows in the Control Centre's **Approvals waiting**
queue and under **More → Approvals**. Check the reason against the customer's history or the draw record before
approving — approving carries the action out immediately. You can't approve your own request.

## Contacts to keep up to date
- Hosting provider support: ______________________
- Stripe support: https://support.stripe.com
- Email provider: ______________________
- Solicitor: ______________________
- Developer on call: ______________________
