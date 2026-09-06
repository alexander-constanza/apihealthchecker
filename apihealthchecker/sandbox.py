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
import socket
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from apihealthchecker.auth import api_token
from apihealthchecker.db import Monitor, SandboxEntry, SandboxSlot, SessionLocal, utcnow

logger = logging.getLogger("apihealthchecker")

DEFAULT_MAX_PER_DAY = 3
DEFAULT_TTL_HOURS = 24
DEFAULT_CHECK_COOLDOWN_SECONDS = 30
MIN_VISITOR_INTERVAL_SECONDS = 60

# Two layers hold the cap. The unique index on sandbox_slots (day, slot) is
# the guarantee: the database refuses a second claim on the same number, from
# any process. This lock is the polite layer on top: within one process it
# stops visitors racing into that refusal and turns what would be a retry
# into a queue. Either alone would do; both together are cheap.
create_lock = threading.Lock()

# monitor id -> monotonic time of the last visitor check-now. Also per process.
_manual_checks: dict[int, float] = {}
_manual_checks_lock = threading.Lock()


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


def check_cooldown_seconds() -> int:
    return _int_env("SANDBOX_CHECK_COOLDOWN_SECONDS", DEFAULT_CHECK_COOLDOWN_SECONDS)


def check_cooldown_remaining(monitor_id: int, now: float | None = None) -> int:
    """Seconds until a visitor may check-now this monitor again. Zero means go.

    Check-now is an outbound request to someone else's server on demand. With
    no limit, a loop of POSTs turns the demo into a request source pointed at
    whatever the seeded monitors target, and it is the demo's IP that gets
    blocked for it.
    """
    now = time.monotonic() if now is None else now
    with _manual_checks_lock:
        last = _manual_checks.get(monitor_id)
    if last is None:
        return 0
    remaining = check_cooldown_seconds() - (now - last)
    return max(int(remaining + 0.999), 0)


def record_manual_check(monitor_id: int, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _manual_checks_lock:
        _manual_checks[monitor_id] = now
        if len(_manual_checks) > 1000:
            # Bounded: drop anything older than the cooldown.
            cutoff = now - check_cooldown_seconds()
            for key in [k for k, v in _manual_checks.items() if v < cutoff]:
                del _manual_checks[key]


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


def _ip_is_public(ip) -> bool:
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _parse_ip(host: str):
    """An IP address if the host is one, in any spelling the resolver would accept.

    `ipaddress` only takes dotted quads. The system resolver also takes
    `127.1`, `0x7f000001`, `2130706433` and `017700000001`, all of which are
    127.0.0.1, so a check that only used `ipaddress` was a loopback bypass.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _resolve(host: str) -> list[str]:
    """Addresses the host resolves to right now. Empty if it does not resolve."""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError, OSError):
        return []


def is_public_hostname(target: str) -> bool:
    """Reject the ways of pointing the service at something private.

    IP literals, in every spelling the resolver accepts, are checked against
    the private, loopback, link-local, multicast and reserved ranges. Names are
    checked for localhost and the internal-only suffixes, then resolved, and
    rejected if any address they resolve to is not public. A name that does
    not resolve is allowed: it fails as a check, which is harmless.

    What is left: DNS rebinding (a name that resolves publicly now and
    privately at check time), and a public target that redirects to a private
    address, since the vendored engine follows redirects. The response body is
    never stored, so what leaks in both cases is a status code.
    """
    host = (urlsplit(target).hostname or "").rstrip(".").lower()
    if not host or host == "localhost":
        return False
    if host.endswith((".localhost", ".local", ".internal", ".home.arpa", ".lan")):
        return False
    ip = _parse_ip(host)
    if ip is not None:
        return _ip_is_public(ip)
    if "." not in host:
        # A bare word like "intranet" or "db" is a network-local name.
        return False
    for resolved in _resolve(host):
        parsed = _parse_ip(resolved)
        if parsed is None or not _ip_is_public(parsed):
            return False
    return True


def visitor_rejection(cleaned: dict) -> tuple[dict, int] | None:
    """The extra rules a visitor's monitor has to meet, as a (body, status) error."""
    if cleaned.get("type", "http") != "http":
        return (
            {
                "error": "invalid_field",
                "field": "type",
                "message": "In sandbox mode only http monitors can be added",
            },
            400,
        )
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


def day_key(now=None) -> str:
    return start_of_day(now).strftime("%Y-%m-%d")


def additions_today(session, now=None) -> int:
    """Slots claimed since midnight UTC, whether or not the monitors still exist."""
    return session.execute(
        select(func.count()).select_from(SandboxSlot).where(SandboxSlot.day == day_key(now))
    ).scalar_one()


def claim_slot(session, now=None) -> bool:
    """Take the next numbered slot for today. False when the day is full.

    Count, then insert slot number `count`. If another process took that
    number in between, the unique index raises, the insert is rolled back,
    and the count is taken again. The slot is committed on its own before the
    monitor is written, so a create that fails after this point has still
    used a slot. That is the cheap direction to be wrong in.
    """
    limit = max_per_day()
    for _ in range(limit + 2):
        taken = additions_today(session, now=now)
        if taken >= limit:
            return False
        session.add(SandboxSlot(day=day_key(now), slot=taken, created_at=now or utcnow()))
        try:
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            logger.info("sandbox_slot_contended", extra={"slot": taken})
    return False


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
