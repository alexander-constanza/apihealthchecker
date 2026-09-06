"""The bridge between the check engine and the database.

This module is where the three repos actually meet: it takes monitors out of
persistence, hands them to infra-health-check's `run_checks` as the config
entries that function already understands, classifies whatever comes back with
the ported ticket-triage scorer, and writes the results down.

It contains no checking logic and no HTTP logic of its own. That is the point.
The one addition is a call to the notifier after each batch is committed, so a
status change becomes a webhook without the runner knowing what a webhook is.
"""
import logging
import time

from apihealthchecker.classifier import classify_failure
from apihealthchecker.db import CheckResultRow, Monitor, SessionLocal, utcnow
from apihealthchecker.engine import Status, run_checks
from apihealthchecker.notifier import notify_transitions, previous_statuses
from apihealthchecker.sandbox import sandbox_enabled, sandbox_entries_for

logger = logging.getLogger("apihealthchecker")

DEFAULT_MAX_WORKERS = 8
DEFAULT_TIMEOUT_SECONDS = 10.0


def monitor_to_entry(monitor: Monitor, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Render a Monitor row as an infra-health-check config entry.

    The engine's `run_checks` takes the same list of dicts its YAML config
    parses into, which is exactly the seam that makes it reusable here: this
    service stores monitors in a table instead of a file and the engine neither
    knows nor cares.
    """
    if monitor.type == "s3":
        return {"type": "s3", "bucket_name": monitor.target}
    if monitor.type == "ec2":
        return {"type": "ec2", "instance_id": monitor.target}
    return {"type": "http", "url": monitor.target, "timeout": timeout_seconds}


def _latency_from(result_detail: dict | None, wall_ms: float | None) -> float | None:
    """Prefer the engine's own measurement, fall back to wall-clock.

    The HTTP check reports `elapsed_ms` from the response itself, which excludes
    scheduling overhead and is the honest number. AWS checks do not report one,
    so the batch wall-clock is used and is at least the right order of
    magnitude.
    """
    if isinstance(result_detail, dict):
        elapsed = result_detail.get("elapsed_ms")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            return round(float(elapsed), 2)
    return wall_ms


def record_result(session, monitor_id: int, result, wall_ms: float | None = None):
    """Persist one engine CheckResult against a monitor, classifying failures.

    Only failures are classified. An OK check has nothing to triage, and an
    UNKNOWN one means "I could not determine this", which is a different
    statement from "this is broken": labelling it with a severity would put a
    configuration problem on the status page as though it were an outage. The
    distinction is inherited from the engine's three-state Status and is kept
    all the way through to the UI.
    """
    detail = dict(result.detail or {})
    status = result.status.value if isinstance(result.status, Status) else str(result.status)

    category = None
    severity = None
    if status == Status.FAIL.value:
        classification = classify_failure(result.message, detail)
        category = classification.category
        severity = classification.severity
        detail["classifier_used"] = classification.classifier_used
        detail["classifier_confidence"] = classification.confidence

    row = CheckResultRow(
        monitor_id=monitor_id,
        status=status,
        message=result.message,
        detail=detail,
        category=category,
        severity=severity,
        latency_ms=_latency_from(result.detail, wall_ms),
        checked_at=utcnow(),
    )
    session.add(row)

    logger.info(
        "check_recorded",
        extra={
            "monitor_id": monitor_id,
            "status": status,
            "category": category,
            "severity": severity,
            "latency_ms": row.latency_ms,
        },
    )
    return row


def run_monitors(monitors: list[Monitor], session=None, max_workers: int = DEFAULT_MAX_WORKERS):
    """Run a batch of monitors concurrently and record every result.

    Concurrency comes from the engine's thread pool, not from anything written
    here. Returns the persisted rows in the same order as the monitors given.
    """
    if not monitors:
        return []

    owns_session = session is None
    session = session or SessionLocal()
    try:
        entries = [monitor_to_entry(m) for m in monitors]
        monitor_ids = [m.id for m in monitors]
        # Read before writing: once the new rows are in, "previous" is gone.
        previous = previous_statuses(session, monitor_ids)
        # Visitor monitors never reach the webhook. A visitor could otherwise
        # point one at a target that flaps and page the operator once a minute.
        muted = set(sandbox_entries_for(session, monitor_ids)) if sandbox_enabled() else set()

        started = time.monotonic()
        results = run_checks(entries, max_workers=max_workers)
        wall_ms = round((time.monotonic() - started) * 1000, 2)

        rows = [
            record_result(session, monitor_id, result, wall_ms=wall_ms)
            # strict=True: the engine returns one result per entry in input
            # order. If that ever stopped holding, silently zipping to the
            # shorter list would attribute results to the wrong monitors, which
            # is worse than raising.
            for monitor_id, result in zip(monitor_ids, results, strict=True)
        ]
        session.commit()
        for row in rows:
            session.refresh(row)
    except Exception:
        session.rollback()
        raise
    else:
        # After the commit, on purpose. The webhook is the one network call in
        # this module that is not a check, and it must not be able to fail a
        # write or hold the transaction open while it waits.
        notify_transitions(previous, rows, muted=muted)
        return rows
    finally:
        if owns_session:
            session.close()


def run_single(monitor: Monitor, session=None):
    """Run one monitor now. Returns the persisted row."""
    rows = run_monitors([monitor], session=session, max_workers=1)
    return rows[0] if rows else None
