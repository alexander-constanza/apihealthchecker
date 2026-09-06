"""Retention tests: what gets deleted, what is kept, and when the scheduler asks."""
from datetime import timedelta

from apihealthchecker.db import CheckResultRow, utcnow
from apihealthchecker.retention import DEFAULT_RETENTION_DAYS, prune_results, retention_days
from apihealthchecker.scheduler import PRUNE_INTERVAL_SECONDS, Scheduler, acquire_lease


def _row(monitor_id, age):
    return CheckResultRow(
        monitor_id=monitor_id, status="ok", message="Responded 200", checked_at=utcnow() - age
    )


def test_retention_days_defaults(monkeypatch):
    monkeypatch.delenv("RETENTION_DAYS", raising=False)
    assert retention_days() == DEFAULT_RETENTION_DAYS


def test_retention_days_reads_the_environment(monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "7")
    assert retention_days() == 7


def test_retention_days_falls_back_on_garbage(monkeypatch):
    """A typo must not silently mean "keep everything"."""
    monkeypatch.setenv("RETENTION_DAYS", "thirty")
    assert retention_days() == DEFAULT_RETENTION_DAYS


def test_retention_days_never_negative(monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "-5")
    assert retention_days() == 0


def test_prune_deletes_only_rows_older_than_the_window(session, monitor):
    session.add_all(
        [
            _row(monitor.id, timedelta(days=31)),
            _row(monitor.id, timedelta(days=29)),
            _row(monitor.id, timedelta(minutes=1)),
        ]
    )
    session.commit()

    assert prune_results(session=session, days=30) == 1
    remaining = session.query(CheckResultRow).count()
    assert remaining == 2


def test_prune_is_disabled_at_zero(session, monitor):
    session.add(_row(monitor.id, timedelta(days=400)))
    session.commit()

    assert prune_results(session=session, days=0) == 0
    assert session.query(CheckResultRow).count() == 1


def test_prune_reads_retention_from_the_environment(session, monitor, monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "1")
    session.add_all([_row(monitor.id, timedelta(days=2)), _row(monitor.id, timedelta(hours=1))])
    session.commit()

    assert prune_results(session=session) == 1


def test_scheduler_prunes_on_first_tick_then_hourly(session, monitor, monkeypatch):
    """The owner prunes as part of its tick: immediately on the first one, so a
    fresh deploy catches up, then at most once per PRUNE_INTERVAL_SECONDS."""
    monkeypatch.setenv("RETENTION_DAYS", "30")
    # prune_results opens and closes its own session, which is the scoped one,
    # so read the id before the monitor instance is detached.
    monitor_id = monitor.id
    scheduler = Scheduler()
    now = utcnow()
    acquire_lease(session=session, owner=scheduler.owner, now=now)
    scheduler.owns_lease = True

    assert scheduler._maybe_prune(now=now) == 0
    session.add(_row(monitor_id, timedelta(days=40)))
    session.commit()

    soon = now + timedelta(seconds=PRUNE_INTERVAL_SECONDS - 1)
    assert scheduler._maybe_prune(now=soon) == 0
    assert session.query(CheckResultRow).count() == 1

    later = now + timedelta(seconds=PRUNE_INTERVAL_SECONDS + 1)
    assert scheduler._maybe_prune(now=later) == 1
    assert session.query(CheckResultRow).count() == 0
