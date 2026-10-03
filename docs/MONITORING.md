# Monitoring and alerting

What watches DBX, what it alerts on, and who hears about it. Alerts go to `SUPPORT_EMAIL` — make sure someone reads that inbox.

## Outside the server (catches "the whole thing is down")
| Check | Set up | Alerts |
|---|---|---|
| **UptimeRobot** (or similar), every 5 min | Monitor `https://YOURDOMAIN/healthz/deep` (keyword `"ok": true`) and `https://YOURDOMAIN/` | Email/SMS/app to the owner |
| **GitHub uptime workflow**, every 15 min | Repository → Settings → Variables → `SITE_URL=https://YOURDOMAIN`. Runs `tests/synthetic_check.py`: homepage, competitions, login, free entry, competition API, a live competition page, deep health; flags slow pages (> 3 s) | Failed workflow email to repository watchers |

`/healthz/deep` reports (ok/not-ok only, no figures or personal data): database writable, background jobs running, competitions query, disk space, payments open. It returns 503 if a critical part fails.

## Inside the app (System health → emailed when red, at most every 6 h per check)
| Check | Red when |
|---|---|
| Card payments | ≥ 70% of card checkouts in 2 h failed |
| Payment success rate | Completion in the last 6 h is below 60% of the 7-day norm |
| Email sending | SMTP missing, failures in the last hour, or emails waiting > 15 min |
| Automatic draws | A draw is more than an hour overdue |
| Draw safety checks | A draw was blocked by the readiness checks in the last 24 h |
| Background jobs | A job hasn't run on schedule or is failing |
| Withdrawals | A withdrawal has waited more than 48 h |
| Site speed & errors | Today: 95% of pages slower than 1 s, or > 1% of requests failing (after 200 requests) |
| Database errors | ≥ 5 database errors in an hour |
| Server errors | Any new unresolved error in 24 h (also emailed immediately the first time it happens) |
| Data integrity | Any impossible condition found by the 6-hourly check |
| Stripe reconciliation | Mismatches waiting, or it hasn't run for 2 days |
| Disk space, Database, Backups, DR drill | Low disk, failed integrity check, no verified backup in 2 days, drill failed or > 100 days old |
| Unusual activity | Spikes in refunds, withdrawals, sign-ups or instant prizes |

Plus immediate emails for: chargebacks and refunds made in Stripe (Flags → Payment reversal), new admin sign-in from an unknown device, deposits that couldn't be refunded automatically.

## Error tracking
Every server error is stored once per kind (Admin → Targets → Server errors) with the code location, stack trace, page (no query string), method, release and at most the account number — emails, card/bank numbers, sort codes, passwords and keys are removed before storing or emailing. The first occurrence of a new kind is emailed to `SUPPORT_EMAIL`.

## Production metrics (Admin → Targets)
- **Operations right now:** card payments completed (24 h vs 7 days), abandoned/failed checkouts, refunds waiting, overdue draws, failed/waiting emails, withdrawals waiting and median time to pay, reconciliation flags, requests and speed today, server errors.
- **Agreed targets** (docs/TARGETS.md): checkout completion, mobile gap, payment problems, support load, error rate, server time, plus uptime/PageSpeed readings you record.

## Who does what
| Signal | First responder | Procedure |
|---|---|---|
| Site down / deep health red | Developer on call | RUNBOOK §1 |
| Payments failing | Owner + developer | RUNBOOK §2 |
| Draw overdue / blocked | Competition manager | RUNBOOK §3, §9 |
| Data integrity / audit chain | Developer + owner | RUNBOOK §10, §15 |
| Reconciliation / reversal | Finance | RUNBOOK §11, §16 |
| New server error | Developer | Fix, deploy, mark fixed |
