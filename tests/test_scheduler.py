"""Scheduler tests.

Time is passed in explicitly rather than slept through: every function that
cares about "now" takes it as an argument, so a lease can be aged by an hour
without the suite taking an hour. No test here sleeps or starts a thread that
outlives it.
"""
from datetime import timedelta

import responses

from apihealthchecker.db import CheckResultRow, Monitor, SchedulerLock, utcnow
from apihealthchecker.scheduler import (
    LEASE_TIMEOUT_SECONDS,
    LOCK_ROW_ID,
    Scheduler,
    acquire_lease,
    due_monitors,
    heartbeat,
    release_lease,
    run_due_checks,
    scheduler_enabled,
    start_scheduler,
)


def test_lease_is_acquired_when_unclaimed(session):
    assert acquire_lease(session=session, owner="worker-1") is True
    assert session.get(SchedulerLock, LOCK_ROW_ID).owner == "worker-1"


def test_second_worker_is_refused_a_fresh_lease(session):
    """The whole point: two gunicorn workers must not both run a scheduler."""
    acquire_lease(session=session, owner="worker-1")
    assert acquire_lease(session=session, owner="worker-2") is False


def test_owner_can_reclaim_its_own_lease(session):
    acquire_lease(session=session, owner="worker-1")
    assert acquire_lease(session=session, owner="worker-1") is True


def test_stale_lease_is_taken_over(session):
    """If the owning process dies, another takes over within the timeout,
    with no operator action and no leader-election service."""
    now = utcnow()
    acquire_lease(session=session, owner="dead-worker", now=now)

    later = now + timedelta(seconds=LEASE_TIMEOUT_SECONDS + 5)
    assert acquire_lease(session=session, owner="worker-2", now=later) is True
    assert session.get(SchedulerLock, LOCK_ROW_ID).owner == "worker-2"


def test_fresh_lease_is_not_taken_over(session):
    now = utcnow()
    acquire_lease(session=session, owner="worker-1", now=now)

    slightly_later = now + timedelta(seconds=LEASE_TIMEOUT_SECONDS - 5)
    assert acquire_lease(session=session, owner="worker-2", now=slightly_later) is False


def test_heartbeat_refreshes_the_lease(session):
    now = utcnow()
    acquire_lease(session=session, owner="worker-1", now=now)

    later = now + timedelta(seconds=30)
    assert heartbeat(session=session, owner="worker-1", now=later) is True
    assert session.get(SchedulerLock, LOCK_ROW_ID).heartbeat_at is not None


def test_heartbeat_fails_for_a_non_owner(session):
    """A loop that finds this False must stop, or there are two schedulers."""
    acquire_lease(session=session, owner="worker-1")
    assert heartbeat(session=session, owner="worker-2") is False


def test_heartbeat_fails_when_no_lease_exists(session):
    assert heartbeat(session=session, owner="worker-1") is False


def test_release_lease_allows_immediate_takeover(session):
    acquire_lease(session=session, owner="worker-1")
    release_lease(session=session, owner="worker-1")
    assert session.get(SchedulerLock, LOCK_ROW_ID) is None
    assert acquire_lease(session=session, owner="worker-2") is True


def test_release_lease_is_a_noop_for_a_non_owner(session):
    acquire_lease(session=session, owner="worker-1")
    release_lease(session=session, owner="worker-2")
    assert session.get(SchedulerLock, LOCK_ROW_ID).owner == "worker-1"


def test_scheduler_enabled_respects_app_role(monkeypatch):
    monkeypatch.setenv("APP_ROLE", "web")
    assert scheduler_enabled() is False


def test_scheduler_enabled_respects_the_off_switch(monkeypatch):
    monkeypatch.delenv("APP_ROLE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    assert scheduler_enabled() is False


def test_scheduler_enabled_by_default(monkeypatch):
    monkeypatch.delenv("APP_ROLE", raising=False)
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    assert scheduler_enabled() is True


def test_a_never_checked_monitor_is_due_immediately(session, monitor):
    """A freshly seeded database populates on the first tick rather than
    showing nothing for a whole interval."""
    assert [m.id for m in due_monitors(session)] == [monitor.id]


def test_a_recently_checked_monitor_is_not_due(session, monitor):
    now = utcnow()
    session.add(CheckResultRow(monitor_id=monitor.id, status="ok", message="", checked_at=now))
    session.commit()

    assert due_monitors(session, now=now + timedelta(seconds=5)) == []


def test_a_monitor_becomes_due_after_its_interval(session, monitor):
    now = utcnow()
    session.add(CheckResultRow(monitor_id=monitor.id, status="ok", message="", checked_at=now))
    session.commit()

    later = now + timedelta(seconds=monitor.interval_seconds + 1)
    assert [m.id for m in due_monitors(session, now=later)] == [monitor.id]


def test_each_monitor_uses_its_own_interval(session):
    now = utcnow()
    fast = Monitor(name="fast", target="https://a.example.org", type="http", interval_seconds=30)
    slow = Monitor(name="slow", target="https://b.example.org", type="http", interval_seconds=600)
    session.add_all([fast, slow])
    session.commit()
    session.add_all([
        CheckResultRow(monitor_id=fast.id, status="ok", message="", checked_at=now),
        CheckResultRow(monitor_id=slow.id, status="ok", message="", checked_at=now),
    ])
    session.commit()

    later = now + timedelta(seconds=60)
    assert [m.id for m in due_monitors(session, now=later)] == [fast.id]


def test_disabled_monitors_are_never_due(session, monitor):
    monitor.enabled = False
    session.commit()
    assert due_monitors(session) == []


def test_only_the_newest_result_decides_whether_a_monitor_is_due(session, monitor):
    """An old result must not make a just-checked monitor look overdue."""
    now = utcnow()
    session.add_all([
        CheckResultRow(
            monitor_id=monitor.id, status="ok", message="",
            checked_at=now - timedelta(hours=2),
        ),
        CheckResultRow(monitor_id=monitor.id, status="ok", message="", checked_at=now),
    ])
    session.commit()

    assert due_monitors(session, now=now + timedelta(seconds=5)) == []


@responses.activate
def test_run_due_checks_records_results(session, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=200)
    rows = run_due_checks(session=session)

    assert len(rows) == 1
    assert rows[0].status == "ok"


def test_run_due_checks_with_nothing_due(session, monitor):
    now = utcnow()
    session.add(CheckResultRow(monitor_id=monitor.id, status="ok", message="", checked_at=now))
    session.commit()

    assert run_due_checks(session=session, now=now + timedelta(seconds=1)) == []


@responses.activate
def test_tick_runs_due_checks_when_the_lease_is_held(session, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=200)

    scheduler = Scheduler()
    acquire_lease(session=session, owner=scheduler.owner)
    scheduler.owns_lease = True

    assert len(scheduler.tick()) == 1


def test_tick_stops_when_the_lease_is_lost(session, monitor):
    """Another process took over. Continuing would mean two schedulers."""
    scheduler = Scheduler()
    acquire_lease(session=session, owner="someone-else")
    scheduler.owns_lease = True

    assert scheduler.tick() == []
    assert scheduler.owns_lease is False


def test_tick_survives_an_error_from_the_checks(session, monitor, monkeypatch):
    """A scheduler thread that dies on a transient error stops monitoring
    silently, which is a worse outage than the one it was watching for."""
    scheduler = Scheduler()
    acquire_lease(session=session, owner=scheduler.owner)
    scheduler.owns_lease = True

    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr("apihealthchecker.scheduler.run_due_checks", boom)
    assert scheduler.tick() == []


def test_scheduler_start_stands_by_when_another_process_holds_the_lease(session, monkeypatch):
    """Losing the lease at startup no longer means giving up. The process gets
    a standby thread that keeps retrying, which is what recovers monitoring
    after a deploy replaces the owner."""
    monkeypatch.delenv("APP_ROLE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "1")
    acquire_lease(session=session, owner="another-worker")

    scheduler = Scheduler(standby_retry_seconds=60)
    try:
        assert scheduler.start() is False
        assert scheduler.owns_lease is False
        assert scheduler._thread is not None and scheduler._thread.is_alive()
    finally:
        scheduler.stop()
    assert session.get(SchedulerLock, LOCK_ROW_ID).owner == "another-worker"


def test_standby_tick_declines_while_the_heartbeat_is_fresh(session):
    scheduler = Scheduler()
    acquire_lease(session=session, owner="another-worker")

    assert scheduler.standby_tick() is False
    assert scheduler.owns_lease is False


def test_standby_tick_takes_over_once_the_heartbeat_is_stale(session):
    """The deploy case: the old owner is dead, its heartbeat ages past the
    timeout, and the standby process promotes itself on its next retry."""
    scheduler = Scheduler()
    now = utcnow()
    acquire_lease(session=session, owner="dead-worker", now=now)

    later = now + timedelta(seconds=LEASE_TIMEOUT_SECONDS + 1)
    assert scheduler.standby_tick(now=later) is True
    assert scheduler.owns_lease is True
    session.expire_all()
    assert session.get(SchedulerLock, LOCK_ROW_ID).owner == scheduler.owner


def test_standby_tick_survives_an_error(monkeypatch):
    """An exception here would kill the standby thread, which is exactly the
    silent failure standby exists to prevent."""
    scheduler = Scheduler()

    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr("apihealthchecker.scheduler.acquire_lease", boom)
    assert scheduler.standby_tick() is False
    assert scheduler.owns_lease is False


def test_start_scheduler_returns_a_standby_scheduler_when_the_lease_is_held(session, monkeypatch):
    """create_app keeps the object either way, so /health can report whether
    this process is the one running checks."""
    monkeypatch.delenv("APP_ROLE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "1")
    acquire_lease(session=session, owner="another-worker")

    scheduler = start_scheduler()
    try:
        assert scheduler is not None
        assert scheduler.owns_lease is False
    finally:
        scheduler.stop()


def test_start_scheduler_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("APP_ROLE", "web")
    assert start_scheduler() is None


def test_stop_releases_the_lease(session, monkeypatch):
    """A clean shutdown deletes the lease row, so the process that replaces
    this one claims immediately instead of waiting out the timeout."""
    monkeypatch.delenv("APP_ROLE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "1")

    scheduler = Scheduler()
    assert scheduler.start() is True
    scheduler.stop()

    assert scheduler.owns_lease is False
    session.expire_all()
    assert session.get(SchedulerLock, LOCK_ROW_ID) is None


def test_scheduler_start_is_refused_when_disabled(monkeypatch):
    monkeypatch.setenv("APP_ROLE", "web")
    scheduler = Scheduler()
    assert scheduler.start() is False
