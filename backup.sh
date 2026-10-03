#!/bin/sh
# Nightly: back up the database + uploads/evidence, then PROVE the backup restores (integrity, migrations,
# draw recomputation). Add to cron:   0 3 * * * cd /opt/prizecomp && ./backup.sh >> backups.log 2>&1
# Off-site copy (recommended): set BACKUP_REMOTE to an rclone remote, e.g. BACKUP_REMOTE=b2:dbx-backups
set -e
docker compose exec -T web flask --app wsgi backup
if [ -n "$BACKUP_REMOTE" ] && command -v rclone >/dev/null 2>&1; then
  rclone copy data/backups "$BACKUP_REMOTE" --max-age 25h && echo "Copied to $BACKUP_REMOTE"
fi
