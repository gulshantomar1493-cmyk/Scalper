# MarketScalper — Production Deployment Runbook

Personal, single-user (multi-device) production workstation. **Decision-support
only — the platform never places orders.** One Linux VPS, three moving parts:

```
   browser (any device)  ──HTTPS/WSS──►  nginx  ──http/ws──►  marketscalper  ──►  PostgreSQL 16
   (frontend, served by nginx)          (TLS front door)      (single systemd process)   (local)
```

The backend binds `127.0.0.1` only. **nginx is the sole public listener.** Every
data route requires the D3 Bearer token. Optimised for simplicity, reliability,
and low cost: no Redis, no queues, no workers, no extra services.

---

## 1. Prerequisites (system packages)

Debian/Ubuntu example (any provider — the choice is never committed):

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git rsync \
                    postgresql-16 nginx certbot python3-certbot-nginx curl
```

Requirements: Python **3.12+**, PostgreSQL **16**, nginx, certbot, git, rsync.

---

## 2. PostgreSQL

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE marketscalper LOGIN PASSWORD 'CHANGE_ME_STRONG';
CREATE DATABASE marketscalper OWNER marketscalper;
SQL
```

DSN (goes in the env file, step 3):
`postgresql://marketscalper:CHANGE_ME_STRONG@127.0.0.1:5432/marketscalper`

Apply migrations **in order** (they are manual by design — never auto-run):

```bash
for f in database/migrations/0*.sql; do
  psql "postgresql://marketscalper:...@127.0.0.1:5432/marketscalper" -v ON_ERROR_STOP=1 -f "$f"
done
```

See `database/README.md` for the migration list.

---

## 3. Application (backend + frontend)

```bash
git clone <repo> ~/marketscalper && cd ~/marketscalper
sudo bash deployment/deploy.sh          # creates user, venv, installs, systemd unit
```

The first run creates `/etc/marketscalper/env` from the template and stops.
Fill the secrets (mode 600, never in git):

```bash
sudo nano /etc/marketscalper/env
#   MARKETSCALPER_DB_DSN=postgresql://marketscalper:...@127.0.0.1:5432/marketscalper
#   MARKETSCALPER_API_TOKEN=<long random string — generate with: openssl rand -hex 32>
#   MARKETSCALPER_API_HOST=127.0.0.1      # keep — backend must not be public
#   MARKETSCALPER_API_PORT=8000
#   MARKETSCALPER_EQUITY_USD=10000        # display-only position sizing
sudo bash deployment/deploy.sh          # re-run: starts the service
```

Verify the process:

```bash
systemctl status marketscalper
curl -s http://127.0.0.1:8000/health           # {"status":"ok"}
curl -s http://127.0.0.1:8000/health/ready      # {"status":"ready","db":"ok"}
```

---

## 4. Reverse proxy + HTTPS (the public front door)

```bash
sudo cp deployment/nginx.conf /etc/nginx/sites-available/marketscalper
sudo ln -s /etc/nginx/sites-available/marketscalper /etc/nginx/sites-enabled/
sudo nano /etc/nginx/sites-available/marketscalper   # replace YOUR_DOMAIN (3x)
sudo mkdir -p /var/www/certbot
sudo certbot --nginx -d your.domain                  # provisions + wires TLS certs
sudo nginx -t && sudo systemctl reload nginx
```

certbot installs a **renewal timer automatically** — certs renew unattended.

Then open **`https://your.domain/`** on any device. The frontend is same-origin,
so it needs no `?api=` param and the client automatically uses `wss://`. Enter
the API token at the in-app prompt (preferred over `?token=` in the URL — see
Security). Timeframe, theme, and beginner mode persist per device.

**No domain?** Drop the TLS `server` block from `nginx.conf` and serve the
`listen 80` block on the VPS IP over a VPN/private network. HTTP is acceptable
only on a trusted private IP; the token still gates every data route.

---

## 5. Data safety — backups (Phase D)

Automated daily backups via a systemd timer (custom-format `pg_dump`, 14-day
retention, mode-600 archives in `/var/backups/marketscalper`):

```bash
sudo systemctl enable --now marketscalper-backup.timer
systemctl list-timers marketscalper-backup.timer      # confirm next run
sudo bash deployment/backup.sh                         # run one now
```

**Prove a backup is restorable (non-destructive):**

```bash
sudo bash deployment/restore.sh --test /var/backups/marketscalper/marketscalper-YYYYMMDD-HHMMSS.dump
#   -> restores into a throwaway db, checks core tables, drops it, prints PASS
```

**Restore for real (DESTRUCTIVE — overwrites the live DB):**

```bash
sudo systemctl stop marketscalper
sudo bash deployment/restore.sh --yes /var/backups/marketscalper/<archive>.dump
sudo systemctl start marketscalper
```

Everything is in the one database, so one archive covers all of it: **candle
data, replay data (same candles), journal, analytics, recommendations.**
Off-site copy (recommended): `rsync`/`rclone` `/var/backups/marketscalper` to
remote storage on your own schedule.

---

## 6. Monitoring (Phase E — lightweight)

| Signal            | How                                                             |
|-------------------|----------------------------------------------------------------|
| App alive         | `GET /health` → `{"status":"ok"}`                              |
| App + DB ready    | `GET /health/ready` → `200 {"status":"ready","db":"ok"}` / `503`|
| Uptime + DB       | `bash deployment/healthcheck.sh` (exit 0/1)                     |
| Feed-gap alerts   | `journalctl -u marketscalper | grep 'ALERT feed gap'` (built-in)|
| Daily stats       | `journalctl -u marketscalper | grep 'daily'` (UTC-midnight snapshot) |
| Errors            | `journalctl -u marketscalper -p err`                           |

Optional auto-restart on failed health (cron):

```cron
*/5 * * * * /opt/marketscalper/deployment/healthcheck.sh || systemctl restart marketscalper
```

Or point any free external uptime monitor at `https://your.domain/health/ready`.
The app already logs feed-gap alerts and a daily performance snapshot to the
journal — no extra observability stack needed.

**In-app operations** — the Live top bar shows a scanner-status pill (never
idle), the **Activity** tab shows the live scan feed, and **Settings →
Operations** shows a full dashboard: feed / scanner / database / backfill /
uptime + per-symbol last candle & data coverage. Backed by `GET /ops` (Bearer,
read-only), so a script can consume the same JSON.

**Times display in IST** (Asia/Kolkata) everywhere in the UI; all internals
(DB, feed, scheduler, backend) stay UTC regardless of the VPS timezone.

**Alerts (optional)** — from **Settings → Notifications / Telegram** the owner
enables desktop notifications (browser, works when the tab is unfocused) and/or
Telegram (works when the app is closed). Telegram setup: create a bot via
@BotFather, message it once, paste the token, click **Verify** — the chat id is
auto-detected. The bot token is written to `backend/runtime_settings.json`
(git-ignored, mode-600, NOT in the DB/backups) — see Security.

---

## 7. Operations

```bash
systemctl {status|restart|stop|start} marketscalper
journalctl -u marketscalper -f              # live logs
journalctl -u marketscalper --since today
```

**Update to a new version:**

```bash
cd ~/marketscalper && git pull
# apply any NEW database/migrations/*.sql first (in order), then:
sudo bash deployment/deploy.sh              # rsync + reinstall + restart
```

Graceful shutdown is built in: SIGTERM (from `systemctl stop`) drains the feed,
sampler, and pool cleanly (exit 0).

---

## 8. Rollback

The app is stateless code over an append-only database, so rollback = redeploy
the previous commit; data is untouched.

```bash
cd ~/marketscalper
git log --oneline -n 10                      # find the last-good commit
git checkout <good-commit>
sudo bash deployment/deploy.sh               # redeploy that revision
systemctl status marketscalper && curl -s http://127.0.0.1:8000/health/ready
```

If a bad **migration** is involved: restore the most recent pre-change backup
(§5, `--yes`) after checking out the matching code. Because migrations are
manual and additive, a code-only rollback is the common case and needs no DB
change. Keep the previous archive until a new version is proven.

---

## 9. Security posture

- **No secrets in git** — DSN + API token live only in `/etc/marketscalper/env`
  (600); the Telegram bot token (if used) lives only in
  `backend/runtime_settings.json` (git-ignored, mode-600, kept out of the DB and
  its backups). `GET /settings` never returns the token.
- **Auth** — single static Bearer token (D3), constant-time compared. REST via
  `Authorization: Bearer`; WS via `?token=` at handshake.
- **Token in URL** — the WS handshake needs `?token=`; nginx logs the path
  without the query string so it never lands in access logs. Prefer entering
  the token at the in-app prompt rather than bookmarking `?token=…`.
- **Backend not public** — binds `127.0.0.1`; nginx is the only listener.
- **Hardened service** — `NoNewPrivileges`, `ProtectSystem=strict`, private
  tmp/devices, restricted address families (see `marketscalper.service`).
- **HTTPS everywhere** — TLS 1.2/1.3, HSTS, `nosniff`, `SAMEORIGIN`, a CSP, and
  `no-referrer`. Backend has API docs/OpenAPI disabled (no unauthenticated schema).
- **CORS** — `*` origins with credentials **off**; the token (never a cookie)
  is the only gate, so `*` cannot leak authenticated data.

---

## 10. Known limitations (single-user scope, by design)

- **One user, one token.** No accounts/sessions/RBAC — every device shares the
  token. Rotate by editing the env file and restarting.
- **Chart library from CDN.** The frontend loads Lightweight Charts from unpkg.
  If unpkg is unreachable the chart won't render (data + API are unaffected). To
  remove the dependency, vendor `lightweight-charts.standalone.production.js`
  into `frontend/` and point the `<script>` in `index.html` at it, then tighten
  the CSP `script-src` to `'self'`.
- **Manual migrations.** Intentional — apply new `database/migrations/*.sql` in
  order before starting the updated service.
- **Single VPS.** No HA/failover; recovery = restore the latest backup on a new
  box and redeploy. Fine for a personal workstation; keep an off-site backup copy.
```
