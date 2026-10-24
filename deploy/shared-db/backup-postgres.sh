#!/bin/sh
# Run on the old host through its loopback-only SSH database tunnel.
set -eu
umask 077
: "${SPARK_BACKUP_DIR:?}"
: "${SPARK_DATABASE_NAME:?}"
: "${SPARK_PG_ENV:?}"
client=postgres:18.6-alpine
mkdir -p "$SPARK_BACKUP_DIR"
chmod 700 "$SPARK_BACKUP_DIR"
exec 9>"$SPARK_BACKUP_DIR/.backup.lock"
flock -n 9 || exit 0
available=$(df -Pk "$SPARK_BACKUP_DIR" | awk 'NR==2 {print $4}')
[ "$available" -ge 2097152 ] || { echo 'Backup refused: less than 2 GiB free' >&2; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$SPARK_BACKUP_DIR/spark-$stamp.dump.partial"
final="$SPARK_BACKUP_DIR/spark-$stamp.dump"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
docker run --rm --network host --env-file "$SPARK_PG_ENV" "$client" pg_dump -h 127.0.0.1 -p 15432 -U spark -d "$SPARK_DATABASE_NAME" -Fc > "$temporary"
docker run --rm -i --network none "$client" pg_restore --list < "$temporary" >/dev/null
mv -- "$temporary" "$final"
printf 'Backup completed: %s\n' "$final"
# Only delete generated dumps in the dedicated folder, after a new dump succeeds.
find "$SPARK_BACKUP_DIR" -maxdepth 1 -type f -name 'spark-????????T??????Z.dump' -mtime +2 -delete
