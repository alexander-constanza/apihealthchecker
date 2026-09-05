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
   The losers serve requests and never start a loop.
4. If the owner dies, its heartbeat goes stale and the next worker to look takes
   over within LEASE_TIMEOUT_SECONDS. No operator action, no leader election
   service.

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
import logging
import os
import threading
import time

from sqlalchemy.exc import SQLAlchemyError

from apihealthchecker.db import Monitor, SchedulerLock, SessionLocal, utcnow
from apihealthchecker.runner import run_monitors

logger = logging.getLogger("apihealthchecker")

# How long a heartbeat stays valid. Must be comfortably more than TICK_SECONDS
# or a healthy owner would look dead to itself.
LEASE_TIMEOUT_SECONDS = 90

# How often the loop wakes to look for due monitors. This is not the check
# interval: monitors have their own, and this is just the resolution at which
# they are noticed.
TICK_SECONDS = 5

LOCK_ROW_ID = 1


def _owner_id() -> str:
    return f"{os.environ.get('HOSTNAME', 'local')}:{os.getpid()}"


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

    Claimable when no row exists, when this process already owns it (so a
    restart of the same pid reclaims cleanly), or when the current owner's
    heartbeat has gone stale.
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

    def __init__(self, tick_seconds: float = TICK_SECONDS):
        self.tick_seconds = tick_seconds
        self.owner = _owner_id()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.owns_lease = False

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
            return run_due_checks(now=now)
        except Exception:
            logger.exception("scheduler_tick_failed", extra={"owner": self.owner})
            return []

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            if not self.owns_lease:
                return
            self._stop.wait(self.tick_seconds)

    def start(self) -> bool:
        """Claim the lease and start the thread. False if another process owns it."""
        if not scheduler_enabled():
            logger.info("scheduler_disabled", extra={"owner": self.owner})
            return False
        if not acquire_lease(owner=self.owner):
            return False

        self.owns_lease = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="apihealthchecker-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("scheduler_started", extra={"owner": self.owner, "tick_s": self.tick_seconds})
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if self.owns_lease:
            release_lease(owner=self.owner)
            self.owns_lease = False
        self._thread = None


def start_scheduler(tick_seconds: float = TICK_SECONDS) -> Scheduler | None:
    """Start the scheduler if this process wins the lease, else return None."""
    scheduler = Scheduler(tick_seconds=tick_seconds)
    if scheduler.start():
        return scheduler
    return None


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
