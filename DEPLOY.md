# Deploying apihealthchecker

Two hosts are configured: Fly.io (`fly.toml`) and Render (`render.yaml`). Fly is
the primary set of instructions below.

The one thing to get right on either host is the **persistent volume**. This
service stores everything in a SQLite file. Without a volume that file lives in
the container filesystem, which is replaced on every deploy, and the entire
check history disappears each time you ship. The app reads its database path
from `DB_PATH`, and that path must sit inside the mounted volume.

## Fly.io

### 1. Install flyctl

```bash
# macOS
brew install flyctl

# Linux
curl -L https://fly.io/install.sh | sh

# Windows (PowerShell)
powershell -Command "iwr https://fly.io/install.sh -useb | iex"
```

Confirm it is on your PATH:

```bash
flyctl version
```

### 2. Sign up or log in

```bash
fly auth signup     # new account
fly auth login      # existing account
```

Fly asks for a payment card at signup even on the free allowance. See the
caveats at the end.

### 3. Create the app without deploying

```bash
cd apihealthchecker
fly launch --no-deploy
```

Answer the prompts as follows:

- **App name**: `apihealthchecker`, or pick your own. If you choose a different
  name, change the `app = ` line in `fly.toml` to match.
- **Region**: anything close to you. `fly.toml` sets `lhr` (London).
- **Postgres / Redis / any other database**: **No**. This service uses SQLite on
  a volume.
- **"Would you like to overwrite fly.toml?"**: **No**. The committed `fly.toml`
  already has the volume mount, the `DB_PATH` that matches it, and the health
  check. A generated one has none of those.

If a prompt is answered wrong, edit `fly.toml` rather than rerunning launch.

### 4. Create the volume

This is the step that keeps your data. The volume name must match the `source`
in the `[[mounts]]` block of `fly.toml`, and the region must match the machine's
region.

```bash
fly volumes create apihealthchecker_data --region lhr --size 1
```

Confirm it exists:

```bash
fly volumes list
```

The mapping to be sure of, all three of these must agree:

| Where | Setting | Value |
|---|---|---|
| `fly volumes create` | volume name | `apihealthchecker_data` |
| `fly.toml` `[[mounts]]` | `source` | `apihealthchecker_data` |
| `fly.toml` `[[mounts]]` | `destination` | `/data` |
| `fly.toml` `[env]` | `DB_PATH` | `/data/apihealthchecker.db` |

`DB_PATH` must be a file **inside** `/data`. If it is not, the app runs happily
and loses its database on every deploy.

### 5. Deploy

```bash
fly deploy
```

The first deploy builds the image, so it takes a few minutes.

### 6. Get the URL

```bash
fly status          # shows the hostname and machine state
fly open            # opens the status page in a browser
```

The URL is `https://<app-name>.fly.dev`. Verify it end to end:

```bash
curl https://<app-name>.fly.dev/health
curl https://<app-name>.fly.dev/api/status
```

`/health` returning `200 {"status": "ok"}` means the process is up and can reach
its database.

### 7. Seed the demo monitors

`fly.toml` sets `SEED_ON_START=1`, so the demo monitors are inserted on first
boot and the scheduler starts checking them within a few seconds. Seeding is
idempotent, so restarts do not duplicate them.

To seed manually instead, set `SEED_ON_START=0` and run:

```bash
fly ssh console -C "flask --app apihealthchecker.app seed"
```

### Checking logs

```bash
fly logs                      # live tail
fly logs --no-tail            # recent lines and exit
```

Every line is JSON with real keys, so filter on them rather than reading prose:

```bash
fly logs --no-tail | grep check_recorded
fly logs --no-tail | grep '"status": 500'
fly logs --no-tail | grep scheduler_
```

Useful messages: `scheduler_started` and `scheduler_lease_acquired` (the
scheduler is running in that process), `scheduler_lease_declined` and
`scheduler_standby` (a second worker correctly stood down and is retrying),
`check_recorded` (one result written), `monitor_status_changed` (a monitor
flipped between ok, fail and unknown), `alert_sent` and `alert_failed` (the
webhook accepted or refused that flip), `results_pruned` (retention ran and
deleted something), `request_completed` (one HTTP request, with `path`,
`status` and `duration_ms`).

### Alerting

Alerting is off until `ALERT_WEBHOOK_URL` is set. It is a URL that receives a
JSON POST on every status change (see "Alerting and retention" in the README
for the payload). Set it as a secret rather than in `fly.toml`, since webhook
URLs usually carry a token:

```bash
fly secrets set ALERT_WEBHOOK_URL=https://example.org/hooks/abc123
```

Setting a secret restarts the machine. Confirm it took, without exposing it:

```bash
curl https://<app-name>.fly.dev/health      # "alerting": {"webhook_configured": true}
```

To see one fire without waiting for something real to break, add a monitor
that fails, check it now, then remove it. A monitor's first result alerts if it
is not ok, so this produces exactly one `monitor_failed`:

```bash
BASE=https://<app-name>.fly.dev
ID=$(curl -s -X POST $BASE/api/monitors -H 'Content-Type: application/json' \
  -d '{"name": "Alert demo", "target": "https://httpbin.org/status/503", "interval_seconds": 300}' \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -X POST $BASE/api/monitors/$ID/check
fly logs --no-tail | grep -E "monitor_status_changed|alert_"
curl -s -X DELETE $BASE/api/monitors/$ID
```

`alert_sent` with `status_code: 200` means the receiver took it. `alert_failed`
carries the exception class and the status code, and deliberately not the URL,
since a webhook URL is usually a token. A 404 there almost always means the
secret was pasted wrong.

Retention needs nothing set. `RETENTION_DAYS` defaults to 30 and can be changed
in the `[env]` block of `fly.toml`.

### Protecting writes

Without `API_TOKEN`, anyone who can reach the service can add, delete and
trigger monitors. That is fine on a laptop and not fine on a public URL. Make
a token and set it as a secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
fly secrets set API_TOKEN=<the value printed>
```

The machine restarts. Confirm the mode, then confirm a write is refused
without the token and accepted with it:

```bash
curl -s https://<app-name>.fly.dev/health            # "auth": {"write_token_required": true}
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<app-name>.fly.dev/api/monitors/1/check
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<app-name>.fly.dev/api/monitors/1/check \
  -H "Authorization: Bearer <the value printed>"
```

Expect `401` then `201`. Reads (`/`, `/health`, `/api/status`, `/api/monitors`,
history) never need it. The status page asks for the token the first time a
visitor tries to change something and keeps it in that browser's localStorage.

To rotate, set a new value with the same command. To open writes again,
`fly secrets unset API_TOKEN`.

### Sandbox mode

With the token set, visitors can only look. To let them try adding a monitor
without being able to damage the demo, turn on sandbox mode. It is not a
secret, so it lives in `fly.toml`:

```toml
[env]
  SANDBOX = "1"
```

The committed `fly.toml` has it on. It needs `API_TOKEN` to mean anything;
without one the app logs `sandbox_needs_token` and behaves as if it were off.
Rules and limits are in the README under "Sandbox mode". Confirm with:

```bash
curl -s https://<app-name>.fly.dev/api/status      # "sandbox": {"enabled": true, "additions_remaining": 3, ...}
```

Expired visitor monitors are removed by the scheduler within seconds of their
time, and logged as `sandbox_monitors_expired`.

### Verifying the volume actually persisted

The point of the volume is that this survives a deploy:

```bash
curl https://<app-name>.fly.dev/api/status    # note monitor_count and last_check_at
fly deploy
curl https://<app-name>.fly.dev/api/status    # same monitors, history intact
curl https://<app-name>.fly.dev/health        # running_in_this_process: true
```

If `monitor_count` reset, the volume is not mounted where `DB_PATH` points. Check
with:

```bash
fly ssh console -C "ls -la /data"
```

The `/health` line matters as much as the other two. A deploy replaces the
process that owns the scheduler lease, and the new one has to take it over
before any checks run. The handover is visible in the logs:

```bash
fly logs --no-tail | grep -E "scheduler_(lease|standby|started)"
```

Normally the old process releases the lease on shutdown and the new one logs
`scheduler_lease_acquired` with `reason=unclaimed`, then `scheduler_started`. If
the old process was killed rather than stopped, the new one logs
`scheduler_lease_declined` and `scheduler_standby`, retries, and logs
`scheduler_standby_promoted` once the old heartbeat is older than 90 seconds.
Either way `last_check_at` should move within about two minutes of the deploy.
If it does not, and `/health` still says `running_in_this_process: false`, the
scheduler is not running anywhere and the machine needs a restart.

### Free-tier caveats

- Fly requires a payment card at signup, including for the free allowance. Watch
  your usage on the dashboard.
- The free allowance covers a small number of `shared-cpu-1x` machines and a few
  GB of volume storage. This app is configured for one machine and a 1 GB volume
  to stay inside it.
- `auto_stop_machines` is deliberately **off** in `fly.toml`. It would suspend
  the machine between requests, which for a monitoring service means the
  scheduler stops and the history gets gaps that look like downtime. This costs
  more than a scale-to-zero app, so it is a deliberate trade.
- A volume is tied to one region and one machine. Scaling past one machine gives
  each its own volume and therefore its own separate database. That is the point
  at which SQLite should be swapped for Postgres. See "Scope and limitations" in
  the README.
- A volume is one copy of the data on one drive. Fly snapshots it daily, but
  see "Backups" below for what that does and does not cover.

### Backups

The database is one file on one volume, and a volume is one copy on one drive.
Fly's own documentation is direct about it: if that drive fails, the data is
gone. Three layers, from free to deliberate:

**Automatic snapshots.** Fly takes a snapshot of every volume once a day and
keeps five days by default. The first one appears within a day of the volume
being created. List them, and change the retention window, with:

```bash
fly volumes snapshots list <volume-id>
fly volumes update <volume-id> --snapshot-retention 14
```

A snapshot is a point-in-time copy from up to a day ago, stored by the same
provider in the same region. It covers a bad deploy or a fat-fingered delete.
It does not cover the provider, the region, or the account.

**Restore from a snapshot.** Snapshots restore into a new volume, not the
existing one:

```bash
fly volumes snapshots list <volume-id>            # pick a snapshot id
fly volumes create apihealthchecker_data --region lhr --size 1 --snapshot-id <snapshot-id>
```

Then attach the machine to the new volume, or destroy the machine and let
`fly deploy` create one against it. The volume name must still match
`fly.toml`.

**A copy you hold.** SQLite's online backup API produces a consistent copy
while the service is running, which a plain file copy of a live database does
not. Two commands, and the file lands on your machine:

```bash
fly ssh console -C "python -c \"import sqlite3; s = sqlite3.connect('/data/apihealthchecker.db'); d = sqlite3.connect('/tmp/backup.db'); s.backup(d); d.close()\""
fly ssh sftp get /tmp/backup.db ./apihealthchecker-$(date +%F).db
```

Open the result with any SQLite client, or point a local run at it with
`DB_PATH`. Verified against the live demo: the copy passes
`pragma integrity_check` and has the same row counts as the source.

There is no scheduled offsite backup. For a demo that is the right call. For
anything whose history matters, cron the two commands above somewhere that is
not Fly, or move to Postgres and use its tooling.

### Scaling past one machine

The scheduler holds a database lease, so extra web workers decline it rather
than each starting their own check loop (see `apihealthchecker/scheduler.py`).
Adding gunicorn workers is therefore safe:

```bash
# in the Dockerfile CMD
--workers 2 --threads 8
```

Adding **machines** is not, while the database is SQLite on a per-machine
volume. Do that only after moving to Postgres by setting `DATABASE_URL`, which
the app already supports and which takes precedence over `DB_PATH`.

## Render

```bash
# Push the repo to GitHub, then either:
#  - point Render at the repo and let it read render.yaml, or
render blueprint launch
```

`render.yaml` declares the disk (`mountPath: /data`) and `DB_PATH` inside it, so
the same persistence rule applies. Render's free instances spin down when idle,
which stops the scheduler; the `starter` plan is set for that reason.

## Local

```bash
docker compose up --build
open http://localhost:8080
```

`docker-compose.yml` mounts a named volume at `/data` for the same reason the
deploy targets do.
