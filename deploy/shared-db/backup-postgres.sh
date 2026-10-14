#!/bin/sh
# Run on the OLD server through the encrypted tunnel; .pgpass must be mode 0600.
# Uses an independently installed pg_dump of the same major version as the DB.
set -eu
umask 077
: "${SPARK_BACKUP_DIR:?set a backup directory on the old server}"
mkdir -p "$SPARK_BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$SPARK_BACKUP_DIR/spark-$stamp.dump.partial"
final="$SPARK_BACKUP_DIR/spark-$stamp.dump"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
pg_dump -h 127.0.0.1 -p 15432 -U spark -d spark -Fc -f "$temporary"
pg_restore --list "$temporary" >/dev/null
mv -- "$temporary" "$final"
printf 'Backup completed: %s\n' "$final"
# No automatic deletion: retention must account for the old server's low space.
