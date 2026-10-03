#!/bin/sh
# Safe live backup of the database + uploaded images. Run nightly with cron:
#   0 3 * * * cd /opt/prizecomp && ./backup.sh
set -e
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M)
docker compose exec -T web python -c "import sqlite3; s=sqlite3.connect('/data/prizes.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
tar czf "backups/prizes-$STAMP.tar.gz" -C data backup.db uploads
rm -f data/backup.db
ls -1t backups/prizes-*.tar.gz | tail -n +31 | xargs -r rm   # keep 30
echo "Saved backups/prizes-$STAMP.tar.gz"
