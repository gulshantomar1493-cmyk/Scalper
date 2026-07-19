#!/usr/bin/env bash
# MarketScalper lightweight health/uptime check (production, Phase E).
#
# Probes the readiness endpoint (liveness + a real DB round-trip) and exits
# 0 healthy / 1 unhealthy. No heavy observability stack — this one script
# covers "app up?" and "database reachable?".
#
# Use it any of three ways:
#   * by hand:            bash deployment/healthcheck.sh
#   * from cron/timer:    */5 * * * * /opt/marketscalper/deployment/healthcheck.sh || systemctl restart marketscalper
#   * external uptime:    point a free uptime monitor at https://YOUR_DOMAIN/health/ready
#
# Checks the LOCAL backend by default; override to probe through the proxy:
#   MARKETSCALPER_HEALTH_URL=https://YOUR_DOMAIN/health/ready bash deployment/healthcheck.sh

set -euo pipefail

URL="${MARKETSCALPER_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"

code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$URL" || echo 000)"
if [ "$code" = "200" ]; then
    echo "[health] OK — $URL ($code)"
    exit 0
fi
echo "[health] UNHEALTHY — $URL returned $code" >&2
exit 1
