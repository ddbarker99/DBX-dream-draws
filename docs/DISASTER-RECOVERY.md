# Disaster recovery

What we do if the server, the database or the hosting account is lost, how long it takes, how much data we could lose, and who does what. Practise it (§5) — a backup nobody has restored is a hope, not a plan.

## 1. Objectives

| | Target | How it's met |
|---|---|---|
| **RPO** — most data we could lose | **24 hours** worst case; normally less | Nightly verified backup (`./backup.sh` via cron) copied off-site (`BACKUP_REMOTE`). Run `./backup.sh` again just before every draw and every release to shrink the window when it matters most. |
| **RTO** — time to be back online | **4 hours** from deciding to restore | Documented rebuild below; the restore-and-check part takes seconds (`flask dr-drill` reports it); most of the time is a new server, DNS and TLS. |
| Draw integrity | No draw result is ever lost or changed | Draw records, frozen entry lists and the hash-chained audit log are in every backup and are re-verified by the restore test. |

If the data lost would include payments (orders paid after the last backup), Stripe still has them: after restoring, use Admin → Flags → *Reconciliation* (runs daily; press *Run jobs now* on System health to run it straight away) to list every payment that has no matching order, then refund or re-issue.

## 2. Roles

| Role | Who | Responsibilities |
|---|---|---|
| Incident lead | Owner | Decides to restore; contacts hosting; approves customer messages |
| Technical restorer | Developer on call | Runs the steps in §4; checks System health; reconciles with Stripe |
| Communications | Owner or support | Status update on socials and the site banner; answers support cases |
| Finance check | Finance role | Matches Stripe payments since the backup; refunds or re-issues |

Keep names and phone numbers in the RUNBOOK's contacts section.

## 3. What's in a backup

`data/backups/prizes-YYYYMMDD-HHMMSS.db` (the whole database) and `files-….tar.gz` (prize photos, winner photos, prize-claim evidence). `flask backup` restore-tests each one immediately: integrity check, opens with the current app version, every draw still recomputes to the same winner. The off-site copy (`BACKUP_REMOTE`, via rclone) is what saves you if the server is gone.

`.env` (keys and passwords) is **not** in backups. Keep a copy in a password manager.

## 4. Restoring onto a clean server

1. New VPS (Ubuntu LTS). Install Docker + the compose plugin, nginx, certbot.
2. Copy the code (git clone or the release zip) to `/opt/prizecomp`; restore `.env` from the password manager.
3. Fetch the newest backup pair from off-site storage into `/opt/prizecomp/data/backups/`.
4. Prove it before using it:
   ```bash
   docker compose run --rm web flask --app wsgi dr-drill --backup data/backups/prizes-<stamp>.db
   ```
   Must print `Disaster-recovery drill PASSED`.
5. Put it in place:
   ```bash
   cp data/backups/prizes-<stamp>.db data/prizes.db
   tar -xzf data/backups/files-<stamp>.tar.gz -C data/
   docker compose up -d --build
   ```
6. Set *Site status → Pause payments* until steps 7–8 are done.
7. Point DNS at the new server; nginx config from `deploy/`; `certbot --nginx`.
8. Stripe: webhook endpoint unchanged if the domain is the same. Check Admin → System health, then Flags → Reconciliation for payments made after the backup.
9. Reopen payments. Post an update. Record what happened in a support case or the runbook log.

## 5. Drills (every quarter, and after any big change)

```bash
docker compose exec web flask --app wsgi dr-drill
```
Restores the newest backup into an empty temporary folder, starts a **separate** copy of the site on it with email, payments and Discord switched off, loads the main pages and a live and a drawn competition, checks images, data integrity and the audit chain, and reports:
- whether it passed,
- the backup's age (your real RPO today),
- how long restore-and-check took.
The result is shown on Admin → System health ("Disaster-recovery drill"), which turns amber after 100 days.

Once a year, do the **full** exercise in §4 onto a throwaway VPS (staging is fine), time it end to end, and note the result below.

| Date | Type | Backup age | Time to service | Problems found | By |
|---|---|---|---|---|---|
| | | | | | |
