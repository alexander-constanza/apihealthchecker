"""Retention: delete check results older than RETENTION_DAYS.

`check_results` only ever grows. At the demo's rate (eight monitors, once a
minute each) that is about 11,500 rows a day, which SQLite handles for years,
but a real deployment with more monitors or shorter intervals needs a bound. The
scheduler calls `prune_results` once an hour from its tick.

This is deletion, not rollup. Rows older than the window are gone, and nothing
downsampled replaces them, so "uptime over the last year" is not a question this
database can answer once the window is shorter than a year. A rollup table is
the obvious next step and was left out deliberately: it is a second write path
with its own consistency questions, and a demo does not need it.

RETENTION_DAYS=0 disables pruning entirely.
"""
import logging
import os
from datetime import timedelta

from sqlalchemy import delete

from apihealthchecker.db import CheckResultRow, SessionLocal, utcnow

logger = logging.getLogger("apihealthchecker")

DEFAULT_RETENTION_DAYS = 30


def retention_days() -> int:
    """RETENTION_DAYS from the environment, with the default for unset or bad values.

    A value that does not parse falls back to the default rather than to "keep
    everything": a typo in a config should not silently turn the bound off.
    """
    raw = os.environ.get("RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("retention_days_invalid", extra={"value": raw})
        return DEFAULT_RETENTION_DAYS
    return max(value, 0)


def prune_results(session=None, now=None, days: int | None = None) -> int:
    """Delete results older than the retention window. Returns the row count.

    The cutoff is compared against `checked_at` directly. The existing index
    leads with `monitor_id`, so this is a scan, which at hourly frequency and
    this table size costs nothing worth an extra index. Revisit if monitors
    number in the hundreds.
    """
    days = retention_days() if days is None else days
    if days <= 0:
        return 0

    now = now or utcnow()
    cutoff = now - timedelta(days=days)

    owns_session = session is None
    session = session or SessionLocal()
    try:
        result = session.execute(delete(CheckResultRow).where(CheckResultRow.checked_at < cutoff))
        session.commit()
        deleted = result.rowcount or 0
        if deleted:
            logger.info(
                "results_pruned",
                extra={"deleted": deleted, "retention_days": days, "cutoff": cutoff.isoformat()},
            )
        return deleted
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
