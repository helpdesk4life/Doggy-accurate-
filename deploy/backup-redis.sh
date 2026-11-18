#!/usr/bin/env bash
#
# Back up the Mikywalk account store.
#
#   bash deploy/backup-redis.sh
#
# The persistent Redis volume holds every account, password hash, the SMTP
# password and the audit log. The geo index does not: it lives on `redis-geo`
# with persistence off, and is rebuilt from the next fix. So this backs up one
# container, not the whole stack.
#
# It snapshots with BGSAVE rather than tarring the volume. A tar of a live
# volume can capture the AOF mid-append, which restores as a torn command and
# leaves Redis refusing to start; BGSAVE writes a complete point-in-time RDB to
# /data/dump.rdb and renames it into place atomically.
#
# Exits non-zero on any failure, so a cron run that breaks is visible rather
# than silent. Prints one line per successful run.
#
# Override the defaults from the environment if the names ever change:
#   BACKUP_DIR=... CONTAINER=... KEEP_DAYS=... bash deploy/backup-redis.sh

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/thomas/backups}"
CONTAINER="${CONTAINER:-backend-redis-1}"
KEEP_DAYS="${KEEP_DAYS:-14}"

stamp="$(date +%F)"
target="$BACKUP_DIR/redis-$stamp.rdb.gz"
partial="$BACKUP_DIR/redis-$stamp.rdb"

mkdir -p "$BACKUP_DIR"

# find chdirs back to the directory it started in before exiting, and fails if
# that directory is unreadable. Under `sudo -u thomas` from a root shell the
# inherited cwd is /root, so the prune step would exit 1 and report a failed
# backup that had in fact succeeded. Start somewhere the script owns.
cd "$BACKUP_DIR"

# BGSAVE forks and returns immediately, so the wait below is not optional.
docker exec "$CONTAINER" redis-cli BGSAVE > /dev/null

in_progress=1
for _ in $(seq 1 60); do
    in_progress="$(docker exec "$CONTAINER" redis-cli --raw INFO persistence \
        | tr -d '\r' \
        | awk -F: '/^rdb_bgsave_in_progress:/{print $2}')"
    if [ "$in_progress" = "0" ]; then
        break
    fi
    sleep 1
done

if [ "$in_progress" != "0" ]; then
    echo "backup FAILED: BGSAVE still running after 60s" >&2
    exit 1
fi

docker cp "$CONTAINER:/data/dump.rdb" "$partial" > /dev/null

# An RDB starts with the magic "REDIS". Checking it catches the case that
# matters: a snapshot of an empty or missing dataset, which would otherwise be
# kept as a backup that restores to nothing.
if [ "$(head -c 5 "$partial")" != "REDIS" ]; then
    echo "backup FAILED: $partial is not an RDB file" >&2
    rm -f "$partial"
    exit 1
fi

# gzip -f writes $partial.gz, which is $target: no rename needed.
gzip -f "$partial"

find "$BACKUP_DIR" -name 'redis-*.rdb.gz' -type f -mtime "+$KEEP_DAYS" -delete

echo "$(date -Is) ok $target $(stat -c %s "$target")B"
