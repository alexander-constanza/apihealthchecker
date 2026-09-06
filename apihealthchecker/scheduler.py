"""Background scheduler: runs due checks on their interval.

## The multi-worker problem, and how this solves it

Under gunicorn the app is imported once per worker process. Anything started at
import time therefore starts N times, once per worker, and a scheduler thread is
the worst possible thing to duplicate: every monitor gets checked N times per
interval, the results table fills with near-duplicate rows, and the target
endpoints see N times the traffic they were told to expect.

This is the same class of bug the api-debugging-toolkit RUNBOOK documents in its
`/simulate/db-down` note: per-process state does not behave the way it looks
like it does once a service is horizontally scaled. The lesson there was learned
by accident. Here it is designed for.

The fix is a lease in the database, which is the one thing every worker shares:

1. On startup each worker tries to claim the single row in `scheduler_lock`.
2. The claim succeeds only if the row does not exist, or its heartbeat is older
   than LEASE_TIMEOUT_SECONDS (the previous owner died).
3. The winner runs the scheduler loop and rewrites its heartbeat every tick.
   The losers serve requests and stand by: they retry the claim every
   STANDBY_RETRY_SECONDS and never run checks until they win.
4. If the owner dies, its heartbeat goes stale and the next standby to retry
   takes over within LEASE_TIMEOUT_SECONDS. No operator action, no leader
   election service.
5. On a clean shutdown the owner deletes its lease row, so the process that
   replaces it claims immediately instead of waiting out the timeout.

Why the losers stand by rather than give up: found on the first CI deploy to
Fly. A deploy replaces the machine, so the new process started while the dead
one's heartbeat was eight seconds old, declined the lease once, and never
looked again. Checks stopped, /health stayed green, and nothing would have
recovered it short of a restart. A single attempt at startup is only correct
when the process that holds the lease is guaranteed to outlive you.

The claim is a single transaction, so two workers racing to start cannot both
win: SQLite serializes the write and the loser sees the row the winner just
committed.

Two honest caveats:

- This is a lease, not a distributed lock with fencing tokens. Between a stale
  heartbeat and the old owner's next tick there is a window in which two loops
  could briefly overlap. The consequence is a duplicate row in a results table,
  which is acceptable; if it were a payment it would not be, and this design
  would be wrong for that.
- APP_ROLE=web disables the scheduler outright for a process. That is the
  explicit override for anyone who would rather run the scheduler as its own
  process than rely on the lease at all.

The deployed configuration takes the simplest defensible option a third way:
fly.toml and the Dockerfile run one gunicorn worker with several threads, so
there is exactly one process and the lease has nothing to arbitrate. The lease
exists so that scaling to two workers is a config change and not an incident.
"""
import atexit
import logging
import os
import secrets
import socket
import threading
import time

from sqlalchemy.exc import SQLAlchemyError

from apihealthchecker.db import Monitor, SchedulerLock, SessionLocal, utcnow
from apihealthchecker.retention import prune_results
from apihealthchecker.runner import run_monitors
from apihealthchecker.sandbox import expire_sandbox_monitors

logger = logging.getLogger("apihealthchecker")

# How long a heartbeat stays valid. Must be comfortably more than TICK_SECONDS
# or a healthy owner would look dead to itself.
LEASE_TIMEOUT_SECONDS = 90

# How often the loop wakes to look for due monitors. This is not the check
# interval: monitors have their own, and this is just the resolution at which
# they are noticed.
TICK_SECONDS = 5

# How often a process that does not own the lease retries the claim. A dead
# owner is noticed within LEASE_TIMEOUT_SECONDS plus this, so it should be small
# next to the timeout but not so small that idle workers hammer the lock row.
STANDBY_RETRY_SECONDS = 15

# How often the owner deletes results older than RETENTION_DAYS. Once an hour
# is plenty: the table grows by a few rows a minute, and the first tick after
# a start prunes immediately so a deploy never waits an hour to catch up.
PRUNE_INTERVAL_SECONDS = 3600

LOCK_ROW_ID = 1

# Generated once per process. Host and pid alone are not unique enough: on Fly
# the HOSTNAME variable is unset and gunicorn's first worker gets the same pid
# in every container, so the process that replaced the dead owner after a
# deploy produced the identical id and reclaimed the lease as a "renewal". It
# worked, but only by coincidence, and the same coincidence would let two live
# processes both believe they own the lease.
_PROCESS_TOKEN = secrets.token_hex(3)


def _owner_id() -> str:
    host = (
        os.environ.get("FLY_MACHINE_ID")
        or os.environ.get("HOSTNAME")
        or socket.gethostname().split(".")[0]
    )
    return f"{host}:{os.getpid()}:{_PROCESS_TOKEN}"


def scheduler_enabled() -> bool:
    """Whether this process should even try to claim the lease.

    APP_ROLE=web opts a process out entirely. Any other value (including unset)
    means "try", and the lease decides.
    """
    if os.environ.get("APP_ROLE", "").lower() == "web":
        return False
    return os.environ.get("SCHEDULER_ENABLED", "1") not in ("0", "false", "False")


def acquire_lease(session=None, owner: str | None = None, now=None) -> bool:
    """Try to claim the scheduler lease. True if this process owns it.

    Claimable when no row exists, when this process already owns it, or when
    the current owner's heartbeat has gone stale.
    """
    owner = owner or _owner_id()
    now = now or utcnow()

    owns_session = session is None
    session = session or SessionLocal()
    try:
        row = session.get(SchedulerLock, LOCK_ROW_ID)
        if row is None:
            session.add(SchedulerLock(id=LOCK_ROW_ID, owner=owner, heartbeat_at=now))
            session.commit()
            logger.info("scheduler_lease_acquired", extra={"owner": owner, "reason": "unclaimed"})
            return True

        age = _age_seconds(row.heartbeat_at, now)
        if row.owner == owner or age > LEASE_TIMEOUT_SECONDS:
            reason = "renewed" if row.owner == owner else "expired"
            row.owner = owner
            row.heartbeat_at = now
            session.commit()
            logger.info("scheduler_lease_acquired", extra={"owner": owner, "reason": reason})
            return True

        logger.info(
            "scheduler_lease_declined",
            extra={"owner": owner, "held_by": row.owner, "heartbeat_age_s": round(age, 1)},
        )
        return False
    except SQLAlchemyError:
        # Losing a race to another worker's INSERT lands here. Not owning the
        # lease is a normal outcome, not an error worth crashing a worker over.
        session.rollback()
        logger.warning("scheduler_lease_failed", extra={"owner": owner}, exc_info=True)
        return False
    finally:
        if owns_session:
            session.close()


def heartbeat(session=None, owner: str | None = None, now=None) -> bool:
    """Refresh the lease. False if this process no longer owns it.

    A loop that finds this False should stop: another process has taken over,
    and two schedulers is exactly what the lease exists to prevent.
    """
    owner = owner or _owner_id()
    now = now or utcnow()

    owns_session = session is None
    session = session or SessionLocal()
    try:
        row = session.get(SchedulerLock, LOCK_ROW_ID)
        if row is None or row.owner != owner:
            return False
        row.heartbeat_at = now
        session.commit()
        return True
    except SQLAlchemyError:
        session.rollback()
        logger.warning("scheduler_heartbeat_failed", extra={"owner": owner}, exc_info=True)
        return False
    finally:
        if owns_session:
            session.close()


def release_lease(session=None, owner: str | None = None) -> None:
    """Give up the lease so another process can take over immediately."""
    owner = owner or _owner_id()
    owns_session = session is None
    session = session or SessionLocal()
    try:
        row = session.get(SchedulerLock, LOCK_ROW_ID)
        if row is not None and row.owner == owner:
            session.delete(row)
            session.commit()
    except SQLAlchemyError:
        session.rollback()
    finally:
        if owns_session:
            session.close()


def _age_seconds(then, now) -> float:
    """Seconds between two timestamps, tolerating naive ones from SQLite."""
    if then is None:
        return float("inf")
    if then.tzinfo is None and now.tzinfo is not None:
        then = then.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and then.tzinfo is not None:
        now = now.replace(tzinfo=then.tzinfo)
    return (now - then).total_seconds()


def due_monitors(session, now=None) -> list[Monitor]:
    """Enabled monitors whose newest result is older than their interval.

    A monitor with no results at all is due immediately, which is what makes a
    freshly seeded database populate itself on the first tick instead of after
    one interval of showing nothing.
    """
    from sqlalchemy import func, select

    from apihealthchecker.db import CheckResultRow

    now = now or utcnow()

    last_seen = (
        select(
            CheckResultRow.monitor_id.label("monitor_id"),
            func.max(CheckResultRow.checked_at).label("last_checked_at"),
        )
        .group_by(CheckResultRow.monitor_id)
        .subquery()
    )

    rows = session.execute(
        select(Monitor, last_seen.c.last_checked_at)
        .outerjoin(last_seen, last_seen.c.monitor_id == Monitor.id)
        .where(Monitor.enabled.is_(True))
    ).all()

    due = []
    for monitor, last_checked_at in rows:
        if last_checked_at is None:
            due.append(monitor)
            continue
        if _age_seconds(last_checked_at, now) >= monitor.interval_seconds:
            due.append(monitor)
    return due


def run_due_checks(session=None, now=None) -> list:
    """Run every monitor that is currently due. Returns the recorded rows."""
    owns_session = session is None
    session = session or SessionLocal()
    try:
        monitors = due_monitors(session, now=now)
        if not monitors:
            return []
        logger.info("scheduler_tick", extra={"due_count": len(monitors)})
        return run_monitors(monitors, session=session)
    finally:
        if owns_session:
            session.close()


class Scheduler:
    """The scheduler thread and its lifecycle.

    Held as an object rather than a module-level thread so tests can drive a
    single tick synchronously, with no sleeping and no wall-clock dependence.
    """

    def __init__(
        self,
        tick_seconds: float = TICK_SECONDS,
        standby_retry_seconds: float = STANDBY_RETRY_SECONDS,
    ):
        self.tick_seconds = tick_seconds
        self.standby_retry_seconds = standby_retry_seconds
        self.owner = _owner_id()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.owns_lease = False
        self._last_prune_at = None

    def tick(self, now=None) -> list:
        """One iteration: refresh the lease, then run whatever is due.

        Every failure mode is contained here. A scheduler thread that dies on a
        transient database error stops monitoring silently, which is a worse
        outage than the one it was watching for.
        """
        try:
            if not heartbeat(owner=self.owner, now=now):
                self.owns_lease = False
                logger.warning("scheduler_lease_lost", extra={"owner": self.owner})
                return []
            rows = run_due_checks(now=now)
            expire_sandbox_monitors(now=now)
            self._maybe_prune(now=now)
            return rows
        except Exception:
            logger.exception("scheduler_tick_failed", extra={"owner": self.owner})
            return []

    def _maybe_prune(self, now=None) -> int:
        """Apply the retention policy, at most once per PRUNE_INTERVAL_SECONDS.

        Runs on the owner only, as part of its tick, so there is exactly one
        process deleting and it is the same one that is writing.
        """
        now = now or utcnow()
        if (
            self._last_prune_at is not None
            and _age_seconds(self._last_prune_at, now) < PRUNE_INTERVAL_SECONDS
        ):
            return 0
        self._last_prune_at = now
        return prune_results(now=now)

    def standby_tick(self, now=None) -> bool:
        """One standby iteration: retry the lease claim. True once this process owns it.

        Called instead of tick() while owns_lease is False. This is the whole
        difference between a process that recovers after a deploy and one that
        stays a bystander forever, so it is worth getting right: it must set
        owns_lease on success, must not raise (a database blip here should not
        kill the standby thread any more than it kills the running loop), and
        should log the promotion so the handover is visible in `fly logs`.
        """
        try:
            self.owns_lease = acquire_lease(owner=self.owner, now=now)
        except Exception:
            # acquire_lease already contains SQLAlchemyError. Anything else
            # would kill the standby thread, which recreates the bug this
            # method exists to fix, so it is logged and retried next round.
            logger.exception("scheduler_standby_failed", extra={"owner": self.owner})
            self.owns_lease = False
            return False
        if self.owns_lease:
            logger.info("scheduler_standby_promoted", extra={"owner": self.owner})
        return self.owns_lease

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.owns_lease:
                self.tick()
                self._stop.wait(self.tick_seconds)
            else:
                # Wait first: start() has just tried the claim, so an immediate
                # retry would only log the same refusal twice.
                self._stop.wait(self.standby_retry_seconds)
                if not self._stop.is_set():
                    self.standby_tick()

    def start(self) -> bool:
        """Start the thread. True if this process owns the lease right now.

        A process that does not win the lease still gets a thread, in standby:
        it retries the claim until it wins or is stopped. The return value says
        who owns the lease at startup, not whether the scheduler is running.
        """
        if not scheduler_enabled():
            logger.info("scheduler_disabled", extra={"owner": self.owner})
            return False

        self.owns_lease = acquire_lease(owner=self.owner)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="apihealthchecker-scheduler", daemon=True
        )
        self._thread.start()
        # gunicorn workers exit via sys.exit on SIGTERM, so atexit runs and the
        # lease row is deleted rather than left to age out over 90 seconds.
        atexit.register(self.stop)
        if self.owns_lease:
            logger.info(
                "scheduler_started", extra={"owner": self.owner, "tick_s": self.tick_seconds}
            )
        else:
            logger.info(
                "scheduler_standby",
                extra={"owner": self.owner, "retry_s": self.standby_retry_seconds},
            )
        return self.owns_lease

    def stop(self, timeout: float = 5.0) -> None:
        atexit.unregister(self.stop)
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if self.owns_lease:
            release_lease(owner=self.owner)
            self.owns_lease = False
        self._thread = None


def start_scheduler(tick_seconds: float = TICK_SECONDS) -> Scheduler | None:
    """Start the scheduler thread, running or in standby. None only if disabled."""
    scheduler = Scheduler(tick_seconds=tick_seconds)
    if not scheduler_enabled():
        logger.info("scheduler_disabled", extra={"owner": scheduler.owner})
        return None
    scheduler.start()
    return scheduler


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll until predicate is true or timeout elapses.

    Used by the end-to-end smoke check, not by the service itself.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
