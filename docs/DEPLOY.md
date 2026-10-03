# Deploying: development → staging → production

Every change follows the same path. Nobody tries competition, payment or wallet changes on the live site first.

| Step | Where | Gate to pass before moving on |
|---|---|---|
| 1. Develop | your machine / a branch | `python -m pytest -q tests/test_app.py`, `python tests/qa_crawl.py`, `python tests/perf_budget.py` all green |
| 2. Review | GitHub pull request | CI green (tests, crawl, performance budgets, keyboard + axe walkthrough); someone other than the author has read the diff for anything touching money, draws or permissions |
| 3. Staging | `staging.YOURDOMAIN` (`docs/STAGING.md`) | QA checklist sections for the area changed, with Stripe **test** cards; load test before big launches |
| 4. Production | the live server | verified backup taken first; smoke test (§14 of the QA checklist); watch System health for 15 minutes |

## Production release, step by step

```bash
cd /opt/prizecomp
./backup.sh                                  # must print "Backup VERIFIED" — stop if it doesn't
git fetch && git checkout <release-tag>      # or unzip the exact build that passed staging
docker compose up -d --build                 # web + worker restart; the database upgrades itself on start
docker compose ps                            # web and worker "Up (healthy)"
curl -fsS https://YOURDOMAIN/healthz         # {"ok": true, ...}
```
Then: the smoke test (QA checklist §14), Admin → System health all green, Admin → Targets → no new server errors.

**Don't release:** within 2 hours of a draw, while a big competition is closing, or late on a Friday.
Use *Admin → Settings → Site status → Pause payments* if you need customers to stop buying during a risky fix.

## Database migrations and how to roll them back

The schema only ever changes **forwards and additively** (`app/db.py`):
- new tables (`CREATE TABLE IF NOT EXISTS`) and new columns (`ALTER TABLE … ADD COLUMN` with defaults),
- triggers that protect records (append-only ledgers, permanent draws),
- one-off data fills that are safe to run twice.

Nothing is renamed or dropped, so **older code keeps working against a newer database**. That makes the normal rollback simply:

```bash
git checkout <previous-tag> && docker compose up -d --build
```
No data restore is needed, and nothing customers did since the release is lost.

### Per-release notes

| Release | Schema change | Rolling back to the previous code |
|---|---|---|
| v11 (Phase 3 operations) | `audit_log.prev_hash/row_hash` (+ existing rows chained once), `feature_flags`, `maintenance_unlock`, `request_stats`, `error_log`; triggers `ledger_no_update/no_delete`, `points_no_update/no_delete`, `draws_no_delete`, `snapshots_no_delete` | Safe. Older code writes audit rows without hashes; they're chained automatically when v11+ starts again. **One exception:** the older *Start fresh → wallets* button will be refused by the ledger triggers. If you must use it on older code, first run the SQL below. |
| v10 (Phase 3 customer) | users/watchlist/cases columns, `consent_log` | Safe. |
| v9 (Phase 2) | sessions, snapshots, claims, flags, jobs… | Safe. |

Removing the v11 protections (only if rolling back *and* you need the old Start-fresh wallet wipe — then redeploy v11 to put them back):
```bash
docker compose exec web sqlite3 /app/data/prizes.db \
  "DROP TRIGGER ledger_no_update; DROP TRIGGER ledger_no_delete; DROP TRIGGER points_no_update; DROP TRIGGER points_no_delete;"
```

### When a migration itself fails
The app won't start (`docker compose logs web` shows the error) and **the database is unchanged**, because every migration runs inside one transaction. Roll back the code as above, then fix the migration on staging using a copy of the production backup.

### When you need to undo data, not code
Restore from backup is the last resort, because it loses everything since that backup. Follow `docs/RUNBOOK.md` §7 and `docs/DISASTER-RECOVERY.md`.

## Writing a migration (developers)
1. Add columns to `MIGRATIONS` / tables to `schema.sql`, always with a default; never rename or drop in the same release.
2. Data fills must be idempotent (safe to run twice) and go in `init_db`.
3. Add a row to the table above with the rollback note.
4. `tests/test_app.py::MigrationTest` upgrades a v1 database through every migration — keep it passing; add a test for any data fill.
5. Rehearse on staging with a copy of the latest production backup (`flask restore-test` then start staging on it).
