#!/bin/sh
# Panel backups for the `backup` service of compose.yaml: a database dump and an archive of the
# uploaded files, named by the UTC time; only the newest LAMNEZIA_BACKUP_KEEP of each are kept.
#
#   backup.sh        a backup now and then every LAMNEZIA_BACKUP_INTERVAL seconds
#   backup.sh once   one backup, the exit code tells whether it worked
#
# pg_dump reads the connection from PGHOST, PGUSER, PGPASSWORD and PGDATABASE.
set -u
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DATA_DIR="${DATA_DIR:-/data}"
INTERVAL="${LAMNEZIA_BACKUP_INTERVAL:-86400}"
KEEP="${LAMNEZIA_BACKUP_KEEP:-14}"
cd "$BACKUP_DIR" || exit 1

# A file gets its final name only when it is complete, so a backup cut short is never rotated
# in place of a good one.
backup() {
    stamp=$(date -u +%Y%m%d-%H%M%S)
    rm -f .*.tmp
    pg_dump --format=custom --file=".db.tmp" &&
        mv ".db.tmp" "db-$stamp.dump" &&
        tar -czf ".media.tmp" -C "$DATA_DIR" media &&
        mv ".media.tmp" "media-$stamp.tar.gz" || return 1
    rotate db '\.dump'
    rotate media '\.tar\.gz'
    echo "backup $stamp is saved"
}

# Delete all but the newest $KEEP files named <kind>-<stamp><extension>.
rotate() {
    ls -1 | grep -E "^$1-[0-9]{8}-[0-9]{6}$2\$" | sort -r |
        tail -n "+$((KEEP + 1))" | while read -r name; do rm -f -- "$name"; done
}

if [ "${1:-}" = once ]; then
    backup
    exit
fi
while :; do
    backup || echo "backup failed, the previous ones are kept" >&2
    sleep "$INTERVAL"
done
