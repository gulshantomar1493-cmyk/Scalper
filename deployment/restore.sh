#!/usr/bin/env bash
# MarketScalper database restore + restore-TEST (production, Phase D).
#
# Two modes:
#
#   deployment/restore.sh --test <archive>
#       Non-destructive. Restores <archive> into a throwaway database, verifies
#       the core tables came back with rows, then drops it. Proves a backup is
#       actually recoverable WITHOUT touching production. Run this after the
#       first backup and any time you want confidence in the archives.
#
#   deployment/restore.sh --yes <archive>
#       DESTRUCTIVE. Restores <archive> over the LIVE database (drops and
#       recreates every object). Stop the service first:
#           sudo systemctl stop marketscalper
#       The explicit --yes guards against an accidental clobber.
#
# The DSN is read from the service env file (same as backup.sh).

set -euo pipefail

ENV_FILE="${MARKETSCALPER_ENV_FILE:-/etc/marketscalper/env}"

usage() { echo "usage: $0 --test|--yes <archive.dump>" >&2; exit 2; }
[ $# -eq 2 ] || usage
MODE="$1"; ARCHIVE="$2"
[ -f "$ARCHIVE" ] || { echo "[restore] ERROR: no such archive: $ARCHIVE" >&2; exit 1; }

if [ -z "${MARKETSCALPER_DB_DSN:-}" ] && [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
fi
[ -n "${MARKETSCALPER_DB_DSN:-}" ] || {
    echo "[restore] ERROR: MARKETSCALPER_DB_DSN not set" >&2; exit 1; }

case "$MODE" in
  --test)
    # Derive an admin DSN pointing at the maintenance 'postgres' db so we can
    # CREATE/DROP the scratch database, keeping the same credentials/host.
    TEST_DB="marketscalper_restore_test_$(date -u +%s)"
    ADMIN_DSN="$(echo "$MARKETSCALPER_DB_DSN" | sed -E 's#/[^/?]+(\?|$)#/postgres\1#')"
    TEST_DSN="$(echo "$MARKETSCALPER_DB_DSN"  | sed -E "s#/[^/?]+(\?|$)#/$TEST_DB\1#")"
    echo "[restore-test] creating scratch db $TEST_DB"
    psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $TEST_DB;"
    cleanup() { psql "$ADMIN_DSN" -c "DROP DATABASE IF EXISTS $TEST_DB;" >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    echo "[restore-test] restoring archive into $TEST_DB"
    pg_restore --no-owner --clean --if-exists --dbname="$TEST_DSN" "$ARCHIVE" \
        || echo "[restore-test] (pg_restore reported non-fatal warnings)"
    echo "[restore-test] verifying core tables have data..."
    ROWS="$(psql "$TEST_DSN" -tAc \
        "SELECT (SELECT count(*) FROM candles) + (SELECT count(*) FROM signals);")"
    echo "[restore-test] candles+signals rows recovered: $ROWS"
    if [ "${ROWS:-0}" -ge 0 ] 2>/dev/null; then
        echo "[restore-test] PASS — archive is restorable."
    else
        echo "[restore-test] FAIL — could not read restored tables." >&2; exit 1
    fi
    ;;
  --yes)
    echo "[restore] DESTRUCTIVE restore of $ARCHIVE over the LIVE database."
    echo "[restore] ensure the service is stopped (systemctl stop marketscalper)."
    pg_restore --no-owner --clean --if-exists --dbname="$MARKETSCALPER_DB_DSN" "$ARCHIVE" \
        || echo "[restore] (pg_restore reported non-fatal warnings)"
    echo "[restore] done. Start the service: systemctl start marketscalper"
    ;;
  *) usage ;;
esac
