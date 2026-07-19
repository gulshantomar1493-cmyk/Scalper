#!/usr/bin/env bash
# MarketScalper database backup (production, Phase D — data safety).
#
# Dumps the ENTIRE database (candles, signals, recommendations, journal,
# analytics source rows — everything) to a compressed, restorable pg_dump
# custom-format archive, then prunes archives older than the retention window.
#
# Runs unattended from marketscalper-backup.timer (daily) or by hand:
#   sudo bash deployment/backup.sh
#
# Restore any archive with: deployment/restore.sh <archive>   (see that script).
#
# The DSN is read from the same env file the service uses — no secret is ever
# passed on the command line or written into git.

set -euo pipefail

ENV_FILE="${MARKETSCALPER_ENV_FILE:-/etc/marketscalper/env}"
BACKUP_DIR="${MARKETSCALPER_BACKUP_DIR:-/var/backups/marketscalper}"
RETENTION_DAYS="${MARKETSCALPER_BACKUP_RETENTION_DAYS:-14}"

# Resolve the DSN: explicit env wins, else source the service env file.
if [ -z "${MARKETSCALPER_DB_DSN:-}" ] && [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
fi
if [ -z "${MARKETSCALPER_DB_DSN:-}" ]; then
    echo "[backup] ERROR: MARKETSCALPER_DB_DSN not set (env or $ENV_FILE)" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/marketscalper-$STAMP.dump"

echo "[backup] dumping -> $OUT"
# --format=custom: compressed + selectively restorable via pg_restore.
pg_dump --format=custom --no-owner --file="$OUT" "$MARKETSCALPER_DB_DSN"
chmod 600 "$OUT"
SIZE="$(du -h "$OUT" | cut -f1)"
echo "[backup] wrote $OUT ($SIZE)"

# Retention: drop archives older than RETENTION_DAYS.
DELETED="$(find "$BACKUP_DIR" -name 'marketscalper-*.dump' -type f \
            -mtime "+$RETENTION_DAYS" -print -delete | wc -l)"
echo "[backup] retention: kept last $RETENTION_DAYS days, pruned $DELETED old archive(s)"
echo "[backup] done. Location: $BACKUP_DIR"
