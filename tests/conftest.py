"""Shared fixtures.

DATABASE_URL is set before any application module is imported, so every test
runs against an in-memory SQLite database and never touches a file on disk.
"""
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# No test starts a scheduler thread implicitly. Anything exercising the
# scheduler drives it a tick at a time, synchronously.
os.environ["RUN_SCHEDULER"] = "0"
os.environ["SEED_ON_START"] = "0"

import pytest  # noqa: E402

from apihealthchecker.app import create_app  # noqa: E402
from apihealthchecker.db import Base, Monitor, SessionLocal, engine  # noqa: E402


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """The sandbox hostname check resolves names. Tests make no network calls,
    so every name resolves to one public address unless a test says otherwise."""
    monkeypatch.setattr("apihealthchecker.sandbox._resolve", lambda host: ["93.184.216.34"])


@pytest.fixture(autouse=True)
def fresh_cooldowns(monkeypatch):
    """The check-now cooldown table is per process. Start each test with it empty."""
    monkeypatch.setattr("apihealthchecker.sandbox._manual_checks", {})


@pytest.fixture()
def app():
    flask_app = create_app(start_background_scheduler=False)
    yield flask_app
    SessionLocal.remove()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def session(app):
    """A session bound to the same in-memory database the app uses."""
    del app
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture()
def monitor(session):
    """One saved HTTP monitor to hang tests off."""
    row = Monitor(
        name="Example service",
        target="https://api.example.org/health",
        type="http",
        interval_seconds=60,
        enabled=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
