"""Daily rollups: what survives retention.

`check_results` is pruned after RETENTION_DAYS, so a question like "what was
this monitor's uptime over the last 90 days" has no rows to answer it from.
This module writes one row per monitor per day into `daily_rollups` with the
counts and latency for that day, and those rows are kept. The scheduler's
hourly maintenance calls `rollup_results` first and `prune_results` second, so
a day is summarised before its rows go.

Every day that still has rows is recomputed each time, today included. That
is a handful of small aggregate queries an hour, and it means a day's row is
never stale by more than an hour and never wrong after a late write. It also
means today's uptime figure lags by up to an hour, which the page says.

Uptime is ok / (ok + fail). Unknown results are reported but left out of the
ratio on purpose: "I could not determine this" is not an outage, and folding
it in would let a broken monitor drag a healthy service's number down.
"""
import logging
from datetime import timedelta

from sqlalchemy import case, func, select

from apihealthchecker.db import CheckResultRow, DailyRollup, SessionLocal, utcnow

logger = logging.getLogger("apihealthchecker")

UPTIME_WINDOW_DAYS = 90


def _rounded(value):
    return round(float(value), 2) if value is not None else None


def _is(status: str):
    return case((CheckResultRow.status == status, 1), else_=0)


def _day_expr():
    # SQLite's date() on a stored timestamp string gives YYYY-MM-DD, and every
    # stored timestamp is UTC, so the day boundary is midnight UTC.
    return func.date(CheckResultRow.checked_at)


def rollup_results(session=None, now=None) -> int:
    """Recompute every (monitor, day) that has rows. Returns rows written."""
    now = now or utcnow()
    owns_session = session is None
    session = session or SessionLocal()
    try:
        day = _day_expr().label("day")
        aggregates = session.execute(
            select(
                CheckResultRow.monitor_id,
                day,
                func.count().label("checks"),
                # case(), not sum(status == 'ok'): the comparison is typed as a
                # boolean and SQLAlchemy would hand the sum back as True.
                func.sum(_is("ok")).label("ok"),
                func.sum(_is("fail")).label("fail"),
                func.sum(_is("unknown")).label("unknown"),
                func.avg(CheckResultRow.latency_ms).label("latency_avg"),
                func.max(CheckResultRow.latency_ms).label("latency_max"),
            ).group_by(CheckResultRow.monitor_id, day)
        ).all()

        existing = {
            (row.monitor_id, row.day): row
            for row in session.execute(select(DailyRollup)).scalars()
        }
        written = 0
        for agg in aggregates:
            key = (agg.monitor_id, str(agg.day))
            row = existing.get(key)
            if row is None:
                row = DailyRollup(monitor_id=agg.monitor_id, day=str(agg.day))
                session.add(row)
            row.checks = int(agg.checks or 0)
            row.ok = int(agg.ok or 0)
            row.fail = int(agg.fail or 0)
            row.unknown = int(agg.unknown or 0)
            row.latency_avg_ms = _rounded(agg.latency_avg)
            row.latency_max_ms = _rounded(agg.latency_max)
            row.updated_at = now
            written += 1
        session.commit()
        if written:
            logger.info("rollup_written", extra={"rows": written})
        return written
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def uptime_for(
    session, monitor_ids: list[int], days: int = UPTIME_WINDOW_DAYS, now=None
) -> dict:
    """Uptime over the last `days` UTC days per monitor, from the rollups.

    Returns {monitor_id: {...}} with a percent that is None when there is
    nothing to divide by. `days_with_data` is the real span the number covers,
    which on a young deployment is shorter than the window and should be said.
    """
    if not monitor_ids:
        return {}
    now = now or utcnow()
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = session.execute(
        select(DailyRollup).where(
            DailyRollup.monitor_id.in_(monitor_ids), DailyRollup.day >= start
        )
    ).scalars()

    out = {}
    for row in rows:
        entry = out.setdefault(
            row.monitor_id,
            {
                "window_days": days,
                "days_with_data": 0,
                "checks": 0,
                "ok": 0,
                "fail": 0,
                "unknown": 0,
            },
        )
        entry["days_with_data"] += 1
        entry["checks"] += row.checks
        entry["ok"] += row.ok
        entry["fail"] += row.fail
        entry["unknown"] += row.unknown
    for entry in out.values():
        counted = entry["ok"] + entry["fail"]
        entry["percent"] = round(entry["ok"] * 100.0 / counted, 2) if counted else None
    return out
