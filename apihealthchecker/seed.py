"""Seed data: real endpoints, chosen so the demo shows real behaviour.

Every URL here was verified reachable from a sandboxed container before being
committed, and the set is deliberately mixed. Six of them return 200. One
returns a genuine 404 from a real service (a PyPI package that does not exist),
and one fails DNS resolution because the host genuinely does not exist. Those
last two are the interesting ones: a status page where everything is always
green proves nothing, and a demo that fakes its failures proves less. These
produce the real thing, including the classifier output that goes with it.

Seeding is idempotent and never automatic. It runs only via
`flask --app apihealthchecker.app seed` or by setting SEED_ON_START=1, because a
service that rewrites its own data on every boot is a service you cannot trust
with the data you put in it.
"""
import logging

from apihealthchecker.db import Monitor, SessionLocal

logger = logging.getLogger("apihealthchecker")

SEED_MONITORS = [
    {
        "name": "GitHub API root",
        "target": "https://api.github.com",
        "type": "http",
        "interval_seconds": 60,
    },
    {
        "name": "GitHub API rate limit",
        "target": "https://api.github.com/rate_limit",
        "type": "http",
        "interval_seconds": 120,
    },
    {
        "name": "PyPI web",
        "target": "https://pypi.org",
        "type": "http",
        "interval_seconds": 60,
    },
    {
        "name": "PyPI JSON API (requests)",
        "target": "https://pypi.org/pypi/requests/json",
        "type": "http",
        "interval_seconds": 120,
    },
    {
        "name": "npm registry (express)",
        "target": "https://registry.npmjs.org/express",
        "type": "http",
        "interval_seconds": 120,
    },
    {
        "name": "PyPI file host",
        "target": "https://files.pythonhosted.org",
        "type": "http",
        "interval_seconds": 180,
    },
    {
        # A real 404 from a real service. Demonstrates the not_found category
        # and, more usefully, that a 404 is classified medium rather than
        # critical: a monitor pointed at a URL that no longer exists is a
        # config problem, not an outage.
        "name": "PyPI JSON API (missing package)",
        "target": "https://pypi.org/pypi/no-such-pkg-xyz-999/json",
        "type": "http",
        "interval_seconds": 300,
    },
    {
        # A real DNS failure. Demonstrates the connectivity category at
        # critical severity, which is the one an operator should act on.
        "name": "Nonexistent host (DNS failure demo)",
        "target": "https://this-host-does-not-exist-xyz123.com",
        "type": "http",
        "interval_seconds": 300,
    },
]


def seed_monitors(session=None) -> int:
    """Insert any seed monitor not already present. Returns the number added.

    Idempotent by target: running it twice adds nothing the second time, and it
    never touches or overwrites a monitor the user created.
    """
    owns_session = session is None
    session = session or SessionLocal()
    try:
        existing = {t for (t,) in session.query(Monitor.target).all()}
        added = 0
        for spec in SEED_MONITORS:
            if spec["target"] in existing:
                continue
            session.add(Monitor(**spec))
            added += 1
        if added:
            session.commit()
        logger.info("seed_completed", extra={"added": added, "total": len(SEED_MONITORS)})
        return added
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
