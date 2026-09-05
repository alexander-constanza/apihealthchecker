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
scheduler is running in that process), `scheduler_lease_declined` (a second
worker correctly stood down), `check_recorded` (one result written),
`request_completed` (one HTTP request, with `path`, `status` and `duration_ms`).

### Verifying the volume actually persisted

The point of the volume is that this survives a deploy:

```bash
curl https://<app-name>.fly.dev/api/status    # note monitor_count and last_check_at
fly deploy
curl https://<app-name>.fly.dev/api/status    # same monitors, history intact
```

If `monitor_count` reset, the volume is not mounted where `DB_PATH` points. Check
with:

```bash
fly ssh console -C "ls -la /data"
```

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
- Volumes are not backed up by default. `fly volumes snapshots list <volume-id>`
  shows what automatic snapshots exist.

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
