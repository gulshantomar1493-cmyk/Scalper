#!/usr/bin/env bash
# MarketScalper deploy script (roadmap P0.27; Decision D4).
# Run ON the single Linux server, from a checkout of this repository.
# Provider-agnostic: assumes only systemd, Python 3.12+ and PostgreSQL.
#
# Usage: sudo bash deployment/deploy.sh

set -euo pipefail

APP_DIR=/opt/marketscalper
ENV_FILE=/etc/marketscalper/env
SERVICE=marketscalper.service
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "[deploy] 1/6 app user + directories"
id -u marketscalper >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin marketscalper
mkdir -p "$APP_DIR" /var/log/marketscalper /etc/marketscalper
chown marketscalper:marketscalper /var/log/marketscalper

echo "[deploy] 2/6 sync application code -> $APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude 'backend/config.yaml' --exclude 'logs' \
  "$REPO_DIR/" "$APP_DIR/"
chown -R marketscalper:marketscalper "$APP_DIR"

echo "[deploy] 3/6 virtualenv + install (minimum-version deps)"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -e "$APP_DIR"

echo "[deploy] 4/6 environment file"
if [ ! -f "$ENV_FILE" ]; then
  install -m 600 "$REPO_DIR/deployment/env.example" "$ENV_FILE"
  echo "[deploy] !! $ENV_FILE created from template — fill MARKETSCALPER_DB_DSN"
  echo "[deploy] !! and MARKETSCALPER_API_TOKEN, then re-run this script."
  exit 1
fi

echo "[deploy] 5/6 database migrations are MANUAL by design — apply any new"
echo "[deploy]     files from database/migrations/ in order with psql before"
echo "[deploy]     the service starts (see database/README.md)."

echo "[deploy] 6/6 systemd unit + (re)start"
install -m 644 "$REPO_DIR/deployment/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
systemctl --no-pager --lines=5 status "$SERVICE" || true

echo "[deploy] done. Frontend: open frontend/index.html?api=HOST:PORT&token=TOKEN"
