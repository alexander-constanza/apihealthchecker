"""Rollup tests: a day is summarised correctly, survives pruning, and the
uptime figure says what it covers."""
from datetime import UTC, datetime, timedelta

from apihealthchecker.db import CheckResultRow, DailyRollup, utcnow
from apihealthchecker.retention import prune_results
from apihealthchecker.rollup import rollup_results, uptime_for
from apihealthchecker.scheduler import Scheduler, acquire_lease


def _rollup(monitor_id, day, **counts):
    return DailyRollup(monitor_id=monitor_id, day=day, **counts)


def _row(monitor_id, status, when, latency=None):
    return CheckResultRow(
        monitor_id=monitor_id, status=status, message="", checked_at=when, latency_ms=latency
    )


def test_rollup_counts_and_latency_per_day(session, monitor):
    d1 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    d2 = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    session.add_all(
        [
            _row(monitor.id, "ok", d1, 100.0),
            _row(monitor.id, "ok", d1 + timedelta(minutes=1), 300.0),
            _row(monitor.id, "fail", d1 + timedelta(minutes=2), None),
            _row(monitor.id, "unknown", d2, None),
            _row(monitor.id, "ok", d2 + timedelta(minutes=1), 50.0),
        ]
    )
    session.commit()

    assert rollup_results(session=session, now=d2) == 2
    rows = {r.day: r for r in session.query(DailyRollup).all()}
    assert rows["2026-09-05"].checks == 3
    assert rows["2026-09-05"].ok == 2
    assert rows["2026-09-05"].fail == 1
    assert rows["2026-09-05"].unknown == 0
    assert rows["2026-09-05"].latency_avg_ms == 200.0
    assert rows["2026-09-05"].latency_max_ms == 300.0
    assert rows["2026-09-06"].unknown == 1
    assert rows["2026-09-06"].ok == 1


def test_rollup_is_idempotent_and_updates_in_place(session, monitor):
    when = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    session.add(_row(monitor.id, "ok", when, 10.0))
    session.commit()
    rollup_results(session=session, now=when)
    session.add(_row(monitor.id, "fail", when + timedelta(minutes=1), 20.0))
    session.commit()
    rollup_results(session=session, now=when)

    rows = session.query(DailyRollup).all()
    assert len(rows) == 1
    assert (rows[0].checks, rows[0].ok, rows[0].fail) == (2, 1, 1)


def test_rollup_survives_pruning(session, monitor):
    """The whole point: a day summarised before its rows are deleted stays."""
    old = utcnow() - timedelta(days=40)
    session.add_all([_row(monitor.id, "ok", old, 5.0), _row(monitor.id, "fail", old, 5.0)])
    session.commit()

    rollup_results(session=session)
    assert prune_results(session=session, days=30) == 2
    assert session.query(CheckResultRow).count() == 0
    rows = session.query(DailyRollup).all()
    assert len(rows) == 1 and rows[0].checks == 2


def test_uptime_excludes_unknown_and_reports_span(session, monitor):
    today = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            _rollup(monitor.id, "2026-09-05", checks=10, ok=9, fail=1, unknown=0),
            _rollup(monitor.id, "2026-09-06", checks=10, ok=8, fail=0, unknown=2),
        ]
    )
    session.commit()

    u = uptime_for(session, [monitor.id], days=90, now=today)[monitor.id]
    assert u["days_with_data"] == 2
    assert u["checks"] == 20
    assert (u["ok"], u["fail"], u["unknown"]) == (17, 1, 2)
    assert u["percent"] == round(17 * 100 / 18, 2)


def test_uptime_window_excludes_old_days(session, monitor):
    today = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            _rollup(monitor.id, "2026-05-01", checks=5, ok=0, fail=5, unknown=0),
            _rollup(monitor.id, "2026-09-06", checks=5, ok=5, fail=0, unknown=0),
        ]
    )
    session.commit()
    u = uptime_for(session, [monitor.id], days=90, now=today)[monitor.id]
    assert u["percent"] == 100.0 and u["days_with_data"] == 1


def test_uptime_is_none_with_nothing_to_divide(session, monitor):
    session.add(_rollup(monitor.id, "2026-09-06", checks=3, ok=0, fail=0, unknown=3))
    session.commit()
    assert uptime_for(session, [monitor.id])[monitor.id]["percent"] is None
    assert uptime_for(session, []) == {}


def test_rollups_cascade_with_the_monitor(client, session, monitor):
    session.add(_rollup(monitor.id, "2026-09-06", checks=1, ok=1, fail=0, unknown=0))
    session.commit()
    monitor_id = monitor.id
    assert client.delete(f"/api/monitors/{monitor_id}").status_code == 200
    assert session.query(DailyRollup).count() == 0


def test_monitor_list_and_rollup_endpoint_carry_uptime(client, session, monitor):
    session.add(_rollup(monitor.id, "2026-09-06", checks=4, ok=3, fail=1, unknown=0))
    session.commit()
    monitor_id = monitor.id
    listed = client.get("/api/monitors").get_json()["monitors"][0]
    assert listed["uptime"]["percent"] == 75.0

    body = client.get(f"/api/monitors/{monitor_id}/rollups").get_json()
    assert body["uptime"]["percent"] == 75.0
    assert body["days"][0]["day"] == "2026-09-06"
    assert client.get("/api/monitors/99999/rollups").status_code == 404


def test_monitor_without_rollups_has_null_uptime(client, monitor):
    assert client.get("/api/monitors").get_json()["monitors"][0]["uptime"] is None


def test_scheduler_maintenance_rolls_up_before_pruning(session, monitor, monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "30")
    monitor_id = monitor.id
    old = utcnow() - timedelta(days=40)
    session.add(_row(monitor_id, "ok", old, 1.0))
    session.commit()

    scheduler = Scheduler()
    now = utcnow()
    acquire_lease(session=session, owner=scheduler.owner, now=now)
    scheduler.owns_lease = True
    assert scheduler._maybe_prune(now=now) == 1

    session.expire_all()
    assert session.query(CheckResultRow).count() == 0
    assert session.query(DailyRollup).filter_by(monitor_id=monitor_id).count() == 1
