# apihealthchecker on one page

A monitoring service that checks endpoints on a schedule, classifies failures,
keeps history, alerts on change, and runs on Fly.io with CI/CD from GitHub.
Live: https://apihealthchecker.fly.dev/

## Flow chart

```mermaid
flowchart TD
    subgraph upstream["Upstream repos, reused not rewritten"]
        IHC["infra-health-check<br/><i>check engine, vendored verbatim</i>"]
        ADT["api-debugging-toolkit<br/><i>app factory, JSON logs, request ids, error split</i>"]
        TTA["ticket-triage-assistant<br/><i>scored keyword classifier</i>"]
    end

    subgraph clients["Clients"]
        VIS["Visitor browser<br/><i>reads everything, sandbox writes</i>"]
        OPR["Operator<br/><i>bearer API_TOKEN, no limits</i>"]
        CURL["curl / scripts<br/><i>REST API</i>"]
    end

    subgraph web["Flask app (one gunicorn worker, 8 threads)"]
        BR["before_request<br/><i>request id, auth hook, sandbox allowlist, CSP nonce</i>"]
        ROUTES["Routes<br/><i>status page, /health, /api/monitors, /api/status, rollups, history</i>"]
        AR["after_request<br/><i>CSP + security headers, structured log line</i>"]
        SB["sandbox.py<br/><i>public host check, DNS resolve, slot cap, cooldown, expiry</i>"]
        AUTH["auth.py<br/><i>constant time token compare</i>"]
    end

    subgraph sched["Scheduler thread (one per deployment)"]
        LEASE["Lease in scheduler_lock<br/><i>standby retry, atexit release, unique owner id</i>"]
        TICK["tick every 5s<br/><i>due monitors, sandbox expiry</i>"]
        MAINT["hourly maintenance<br/><i>rollup, then prune</i>"]
    end

    subgraph pipeline["One check, end to end"]
        RUN["runner.py<br/><i>rows to engine entries, records results</i>"]
        ENG["engine.run_checks<br/><i>threaded HTTP / S3 / EC2 checks</i>"]
        CLS["classifier.py<br/><i>category + severity, most-severe wins</i>"]
        NOTI["notifier.py<br/><i>transition detection, webhook POST after commit</i>"]
    end

    subgraph db["SQLite on Fly volume /data"]
        MON[("monitors")]
        RES[("check_results<br/><i>30 day retention</i>")]
        ROLL[("daily_rollups<br/><i>uptime over 90 days</i>")]
        LOCK[("scheduler_lock")]
        SENT[("sandbox_entries<br/><i>expiry per visitor monitor</i>")]
        SLOT[("sandbox_slots<br/><i>unique (day, slot) = the cap</i>")]
    end

    subgraph gh["GitHub"]
        TESTS["tests.yml<br/><i>ruff + 250 tests on every push</i>"]
        DEPLOY["deploy.yml<br/><i>tests, then flyctl deploy remote build</i>"]
        BACKUP["backup.yml<br/><i>nightly sqlite .backup, 30 day artifact</i>"]
        ALERTS["alerts.yml<br/><i>repository_dispatch to issue open / close</i>"]
        ISSUE["Issue + email<br/><i>the pager</i>"]
    end

    subgraph fly["Fly.io"]
        MACH["Machine, lhr<br/><i>auto_stop off, health check /health</i>"]
        VOL["Volume<br/><i>daily snapshots, 5 days</i>"]
        SEC["Secrets<br/><i>API_TOKEN, webhook URL, token, format</i>"]
    end

    IHC --> ENG
    ADT --> BR
    TTA --> CLS

    VIS --> BR
    OPR --> BR
    CURL --> BR
    BR --> AUTH --> SB --> ROUTES --> AR
    ROUTES --> MON
    ROUTES --> RES
    ROUTES --> ROLL
    ROUTES -->|check now| RUN

    LEASE --> LOCK
    LEASE --> TICK --> RUN
    TICK --> SENT
    TICK --> MAINT --> ROLL
    MAINT -->|prune| RES

    RUN --> ENG --> CLS --> RES
    RUN --> NOTI
    NOTI -->|JSON or github format| ALERTS --> ISSUE
    SB --> SLOT
    SB --> SENT

    DEPLOY --> MACH
    MACH --- VOL
    SEC --> MACH
    BACKUP -->|ssh + sftp| VOL
    TESTS -.-> DEPLOY
```

## What we did, why, how

**Starting point.** Working service, but CI red on every run, no licence
file, CD never exercised, and a scheduler that died silently on deploy.

**Delivered in one working day, all deployed and verified in production**

- CI/CD: found the root cause of every red run (`pytest` vs `python -m pytest`
  path difference), fixed with one config line, pinned actions to current
  majors and a third party action to a commit, set the Fly deploy token, first
  green deploy end to end.
- Incident found by that deploy: scheduler declined the lease and never retried.
  Root cause analysis from logs, hot fix by restart, permanent fix with standby
  retry, atexit lease release and unique owner ids. Handover verified: zero gap.
- Persistence proven: check history survived seven CI deploys, row counts and
  oldest timestamp compared before and after.
- Backups: Fly snapshots documented, consistent copy via SQLite online backup
  API, nightly GitHub Actions artifact, restore runbook, restore actually
  performed once (see below).
- Retention and rollups: hourly prune, daily rollup table, uptime over 90 days
  on every card, unknown excluded from the ratio and the reason written down.
- Alerting: neutral JSON webhook on status transitions, one alert per outage,
  sent after commit so a dead receiver never loses data. GitHub made the
  receiver: failure opens an issue, recovery closes it, email for free.
- Auth: bearer token on writes, checked in one `before_request` hook so new
  endpoints fail closed, reads open, token asked once in the browser.
- Sandbox mode: visitors add up to three monitors a day, removed after 24
  hours, seeded monitors protected, public targets only, check-now cooldown,
  cap enforced by a unique index so it holds across processes.
- Hardening pass: attacked the service on purpose, nine findings fixed with a
  test each, two left open and documented (redirect follow and DNS rebinding
  in vendored code). CSP with nonces, body limits, header validation.
- Documentation: README as source of truth, DEPLOY.md runbook with verify
  commands for every feature, plain language walkthrough for newcomers,
  screenshots captured from the live page, real alert payload as received.

**Errors handled and what they taught**

- Deploy killed the scheduler while health stayed green: a health check that
  does not check the thing you care about is decoration.
- Owner id collided across deploys (same pid in every container): correctness
  by coincidence is a bug waiting.
- Alert failure log leaked the webhook URL via a traceback: a webhook URL is a
  credential.
- Wrong token pasted twice, then a GitHub token pasted into chat: two tokens
  with different jobs need to be named clearly, and an exposed token gets
  rotated the same hour.
- A verification probe deleted a seeded monitor because a secret had not
  actually been set: read the state flag before sending any write, and keep
  a fresh backup before touching production. Restored from the backup copy.
- Race test passed locally 15 times and failed in CI twice: an in-memory
  database on one shared connection is not a concurrency model; the test now
  uses a real file and a connection per thread, like production.
- Summing a boolean comparison in SQLAlchemy returned `True`, not a count.

**Design decisions, each with a reason in the README**

- SQLite on a volume, not Postgres: one machine, a few checks a minute.
- Lease, not distributed lock: duplicate row acceptable, payment would not be.
- New tables rather than new columns: `create_all` adds tables, not columns,
  so no migration on the live database.
- One shared token, not accounts: an auth proxy's job past this size.
- Generic webhook, not a Slack integration: adapters over favourites.

**Verification habit**

- Every change: ruff, 250 tests, push, watch CI, deploy, curl the live service,
  read logs, compare before and after.

## Keywords for the roles this was built for

Implementation Engineer: deployment, CI/CD, integration, REST API, webhooks,
configuration, migrations avoided by design, customer-facing documentation.

Technical Support Engineer: troubleshooting from logs, root cause analysis,
runbooks, incident timeline, restore from backup, structured logging,
health checks, reproducing a CI-only failure.

Forward Deployed Engineer: shipping to a live environment daily, security
hardening, secrets handling, token rotation, sandboxing untrusted input,
uptime reporting, alert routing into the tools a team already uses.
