"""Sandbox mode: let anyone try the demo without letting anyone wreck it.

With API_TOKEN set, the deployed demo is read-only for visitors, which keeps
it intact and also makes the add-monitor form a decoration. Sandbox mode is
the middle ground. When SANDBOX=1:

- Anyone can add a monitor, up to SANDBOX_MAX_PER_DAY (3) per UTC day across
  all visitors, resetting at midnight UTC. The cap is global, not per
  visitor, because there is no honest way to tell visitors apart without
  accounts. Midnight rather than a rolling window so "try again tomorrow"
  means what it says and the page can print the reset time.
- A visitor's monitor is removed automatically SANDBOX_TTL_HOURS (24) after
  it was added, history and all.
- Visitors can delete only monitors that visitors added. The seeded eight and
  anything the operator added are protected.
- Visitors can press check-now on anything.
- A visitor's target must be a public hostname and check no more often than
  once a minute, because a monitor is an outbound request on a schedule, and
  the service should not be pointable at its own network or at someone else's
  at ten requests a minute.

The operator, meaning any request carrying the token, is exempt from all of
it: their monitors are permanent and unprotected only by the token.

Sandbox mode needs API_TOKEN. Without one every request is the operator and
there is nothing for the sandbox to distinguish, so `sandbox_enabled()` is
False and create_app logs a warning.

What this is not: abuse-proof. The cap is a courtesy limit that stops the demo
filling up, not rate limiting. The hostname check is by name and IP literal
only, so a public name that resolves to a private address is not caught. Both
are documented in the README.
"""
import ipaddress
import logging
import os
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select

from apihealthchecker.auth import api_token
from apihealthchecker.db import Monitor, SandboxEntry, SessionLocal, utcnow

logger = logging.getLogger("apihealthchecker")

DEFAULT_MAX_PER_DAY = 3
DEFAULT_TTL_HOURS = 24
MIN_VISITOR_INTERVAL_SECONDS = 60


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning("sandbox_setting_invalid", extra={"setting": name, "value": raw})
        return default


def sandbox_requested() -> bool:
    return os.environ.get("SANDBOX", "0") in ("1", "true", "True")


def sandbox_enabled() -> bool:
    """SANDBOX=1 and a token to tell visitors from the operator."""
    return sandbox_requested() and api_token() is not None


def max_per_day() -> int:
    return _int_env("SANDBOX_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)


def ttl_hours() -> int:
    return _int_env("SANDBOX_TTL_HOURS", DEFAULT_TTL_HOURS)


def visitor_may(method: str, path: str) -> bool:
    """Which write requests a visitor may make at all. The routes apply the rest.

    An allowlist of exact shapes, so a write endpoint added later is still
    behind the token until it is added here on purpose.
    """
    if method == "POST" and path == "/api/monitors":
        return True
    if method == "DELETE" and path.startswith("/api/monitors/"):
        return True
    if method == "POST" and path.startswith("/api/monitors/") and path.endswith("/check"):
        return True
    return False


def is_public_hostname(target: str) -> bool:
    """Reject the obvious ways of pointing the service at something private.

    IP literals are checked against the private, loopback, link-local and
    reserved ranges. Names are checked for localhost and the internal-only
    suffixes. A public name that resolves to a private address gets through;
    catching that needs a DNS lookup at validation time and again at check
    time, and is out of scope for a demo cap.
    """
    host = (urlsplit(target).hostname or "").rstrip(".").lower()
    if not host or host == "localhost":
        return False
    if host.endswith((".localhost", ".local", ".internal", ".home.arpa", ".lan")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        # A bare word like "intranet" or "db" is a network-local name.
        return "." in host


def visitor_rejection(cleaned: dict) -> tuple[dict, int] | None:
    """The extra rules a visitor's monitor has to meet, as a (body, status) error."""
    if not is_public_hostname(cleaned["target"]):
        return (
            {
                "error": "invalid_field",
                "field": "target",
                "message": "In sandbox mode a monitor must point at a public hostname",
            },
            400,
        )
    if cleaned["interval_seconds"] < MIN_VISITOR_INTERVAL_SECONDS:
        return (
            {
                "error": "invalid_field",
                "field": "interval_seconds",
                "message": (
                    "In sandbox mode the interval must be at least "
                    f"{MIN_VISITOR_INTERVAL_SECONDS} seconds"
                ),
            },
            400,
        )
    return None


def start_of_day(now=None):
    """Midnight UTC of the current day. Everything stored is UTC, so this is too."""
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def next_reset(now=None):
    return start_of_day(now) + timedelta(days=1)


def additions_today(session, now=None) -> int:
    """Visitor monitors created since midnight UTC, whether or not they still exist."""
    return session.execute(
        select(func.count())
        .select_from(SandboxEntry)
        .where(SandboxEntry.created_at >= start_of_day(now))
    ).scalar_one()


def additions_remaining(session, now=None) -> int:
    return max(max_per_day() - additions_today(session, now=now), 0)


def register_sandbox_monitor(session, monitor: Monitor, now=None) -> SandboxEntry:
    now = now or utcnow()
    entry = SandboxEntry(
        monitor_id=monitor.id, created_at=now, expires_at=now + timedelta(hours=ttl_hours())
    )
    session.add(entry)
    return entry


def sandbox_entries_for(session, monitor_ids: list[int]) -> dict[int, SandboxEntry]:
    if not monitor_ids:
        return {}
    rows = session.execute(
        select(SandboxEntry).where(SandboxEntry.monitor_id.in_(monitor_ids))
    ).scalars()
    return {row.monitor_id: row for row in rows}


def is_protected(session, monitor_id: int) -> bool:
    """A monitor a visitor may not delete: anything a visitor did not add."""
    return monitor_id not in sandbox_entries_for(session, [monitor_id])


def expire_sandbox_monitors(session=None, now=None) -> int:
    """Delete visitor monitors past their expiry. Returns how many went.

    Called from the scheduler's tick, so it runs in exactly one process and
    within seconds of the expiry rather than on the hour.
    """
    now = now or utcnow()
    owns_session = session is None
    session = session or SessionLocal()
    try:
        due = session.execute(
            select(SandboxEntry).where(
                SandboxEntry.monitor_id.is_not(None), SandboxEntry.expires_at <= now
            )
        ).scalars().all()
        removed = 0
        for entry in due:
            monitor = session.get(Monitor, entry.monitor_id)
            if monitor is not None:
                session.delete(monitor)
                removed += 1
            # The foreign key sets monitor_id to NULL on delete; mirror it here
            # so the ORM does not write the stale id back.
            entry.monitor_id = None
        if due:
            session.commit()
        if removed:
            logger.info("sandbox_monitors_expired", extra={"removed": removed})
        return removed
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def sandbox_view(session, now=None) -> dict:
    """What the status page needs to explain the rules to a visitor."""
    if not sandbox_enabled():
        return {"enabled": False}
    return {
        "enabled": True,
        "max_per_day": max_per_day(),
        "additions_remaining": additions_remaining(session, now=now),
        "resets_at": next_reset(now).isoformat(),
        "ttl_hours": ttl_hours(),
        "min_interval_seconds": MIN_VISITOR_INTERVAL_SECONDS,
    }
