"""Webhook alerting on status transitions.

The classified severity was always described as "the hook that would feed a
notifier". This is that notifier, kept deliberately small: one generic HTTP
POST with a JSON body to ALERT_WEBHOOK_URL whenever a monitor's status changes.

What counts as a transition: the newest stored result for the monitor differs in
status from the one just recorded. ok -> fail fires `monitor_failed`, fail -> ok
fires `monitor_recovered`, anything -> unknown fires `monitor_unknown`. A first
result that is ok fires nothing (there is nothing to say), a first result that
is not ok fires (a brand new monitor that fails is news). Repeats do not fire,
so a monitor that stays down sends one alert, not one per minute.

What this is not: a Slack, PagerDuty or email integration. Those each want their
own payload shape, and picking one would make the others second class. Point it
at anything that accepts JSON, or at a small adapter in front of the thing you
actually page with. There are no retries and no queue: a webhook that is down
when the transition happens misses it, and the miss is logged as `alert_failed`.

Every transition is also logged as `monitor_status_changed` whether or not a
webhook is configured, so the history of flips is in the logs either way.
"""
import logging
import os

import requests
from sqlalchemy import select

from apihealthchecker.db import CheckResultRow, _iso, utcnow

logger = logging.getLogger("apihealthchecker")

# Short on purpose. This runs inside the scheduler tick, and a webhook that
# takes ten seconds to answer would delay every check behind it.
ALERT_TIMEOUT_SECONDS = 5.0

EVENT_BY_STATUS = {
    "fail": "monitor_failed",
    "ok": "monitor_recovered",
    "unknown": "monitor_unknown",
}


def webhook_url() -> str | None:
    """ALERT_WEBHOOK_URL from the environment, or None when alerting is off."""
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    return url or None


def latest_status(session, monitor_id: int) -> str | None:
    """Status of the newest stored result for a monitor, or None if it has none."""
    return session.execute(
        select(CheckResultRow.status)
        .where(CheckResultRow.monitor_id == monitor_id)
        .order_by(CheckResultRow.checked_at.desc(), CheckResultRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def previous_statuses(session, monitor_ids: list[int]) -> dict[int, str | None]:
    """Newest status per monitor, read before the new results are written."""
    return {monitor_id: latest_status(session, monitor_id) for monitor_id in monitor_ids}


def transitions(previous: dict[int, str | None], rows: list[CheckResultRow]) -> list[tuple]:
    """Pairs of (previous_status, row) for every row whose status changed."""
    changed = []
    for row in rows:
        before = previous.get(row.monitor_id)
        if before == row.status:
            continue
        if before is None and row.status == "ok":
            continue
        changed.append((before, row))
    return changed


def build_payload(row: CheckResultRow, previous_status: str | None) -> dict:
    return {
        "event": EVENT_BY_STATUS.get(row.status, "monitor_changed"),
        "previous_status": previous_status,
        "status": row.status,
        "monitor": row.monitor.to_dict(),
        "result": row.to_dict(),
        "sent_at": _iso(utcnow()),
    }


def send_alert(payload: dict, url: str | None = None, timeout: float = ALERT_TIMEOUT_SECONDS):
    """POST one payload. True if the webhook accepted it. Never raises."""
    url = url or webhook_url()
    if not url:
        return False
    fields = {"event": payload["event"], "monitor_id": payload["monitor"]["id"]}
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        # No exc_info here on purpose. A requests traceback quotes the URL,
        # and a webhook URL is usually a bearer token in disguise. The class
        # name and status code are enough to tell a dead host from a 404.
        status_code = exc.response.status_code if exc.response is not None else None
        logger.warning(
            "alert_failed",
            extra={**fields, "error": type(exc).__name__, "status_code": status_code},
        )
        return False
    logger.info("alert_sent", extra={**fields, "status_code": response.status_code})
    return True


def notify_transitions(
    previous: dict[int, str | None], rows: list[CheckResultRow], muted: set[int] | None = None
) -> int:
    """Log every transition and POST each one to the webhook. Returns the count sent.

    Called after the rows are committed, so an alert is only ever sent for a
    result that is actually stored, and a slow or failing webhook cannot roll
    a result back. Monitors in `muted` are logged but not sent: the runner
    passes the sandbox monitors, which strangers control.
    """
    url = webhook_url()
    muted = muted or set()
    sent = 0
    for before, row in transitions(previous, rows):
        is_muted = row.monitor_id in muted
        logger.info(
            "monitor_status_changed",
            extra={
                "monitor_id": row.monitor_id,
                "previous_status": before,
                "status": row.status,
                "severity": row.severity,
                "category": row.category,
                "alert_muted": is_muted,
            },
        )
        if url and not is_muted and send_alert(build_payload(row, before), url=url):
            sent += 1
    return sent
