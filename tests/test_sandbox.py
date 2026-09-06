"""Sandbox tests: visitors can add within the cap, cannot touch what they did
not add, and what they add goes away on time. The operator is exempt."""
from datetime import UTC, datetime, timedelta

import pytest
import responses

from apihealthchecker.db import CheckResultRow, Monitor, SandboxEntry, utcnow
from apihealthchecker.sandbox import (
    additions_remaining,
    expire_sandbox_monitors,
    is_public_hostname,
    sandbox_enabled,
    visitor_may,
)
from apihealthchecker.scheduler import Scheduler, acquire_lease

TOKEN = "operator-token"
NEW = {"name": "Visitor's site", "target": "https://visitor.example.org/health"}


@pytest.fixture()
def sandbox(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("SANDBOX", "1")
    monkeypatch.delenv("SANDBOX_MAX_PER_DAY", raising=False)
    monkeypatch.delenv("SANDBOX_TTL_HOURS", raising=False)


def bearer():
    return {"Authorization": f"Bearer {TOKEN}"}


def _seeded(session):
    """A monitor that was not added by a visitor, standing in for the seeded eight."""
    row = Monitor(name="Seeded", target="https://seeded.example.org", type="http")
    session.add(row)
    session.commit()
    return row.id


def test_sandbox_needs_a_token(monkeypatch):
    monkeypatch.setenv("SANDBOX", "1")
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert sandbox_enabled() is False
    monkeypatch.setenv("API_TOKEN", TOKEN)
    assert sandbox_enabled() is True


def test_visitor_allowlist_is_exact():
    assert visitor_may("POST", "/api/monitors")
    assert visitor_may("DELETE", "/api/monitors/3")
    assert visitor_may("POST", "/api/monitors/3/check")
    assert not visitor_may("POST", "/api/does-not-exist")
    assert not visitor_may("PUT", "/api/monitors/3")


def test_visitor_can_add_and_the_monitor_is_marked(client, sandbox):
    resp = client.post("/api/monitors", json=NEW)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["sandbox"]["expires_at"] is not None

    listed = client.get("/api/monitors").get_json()["monitors"]
    assert listed[0]["sandbox"]["expires_at"] == body["sandbox"]["expires_at"]


def test_operator_additions_are_not_sandboxed(client, sandbox):
    body = client.post("/api/monitors", json=NEW, headers=bearer()).get_json()
    assert body["sandbox"] is None


def test_cap_is_three_per_day_and_counts_deletions(client, sandbox):
    ids = []
    for i in range(3):
        resp = client.post("/api/monitors", json={**NEW, "name": f"v{i}"})
        assert resp.status_code == 201
        ids.append(resp.get_json()["id"])

    resp = client.post("/api/monitors", json={**NEW, "name": "v4"})
    assert resp.status_code == 429
    assert resp.get_json()["error"] == "sandbox_limit"

    # Deleting one does not free a slot: the cap counts creations.
    assert client.delete(f"/api/monitors/{ids[0]}").status_code == 200
    assert client.post("/api/monitors", json={**NEW, "name": "v5"}).status_code == 429

    # The operator is not capped.
    resp = client.post("/api/monitors", json={**NEW, "name": "op"}, headers=bearer())
    assert resp.status_code == 201


def test_cap_resets_at_midnight_utc(session, sandbox):
    now = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    yesterday = datetime(2026, 9, 5, 23, 59, tzinfo=UTC)
    today = datetime(2026, 9, 6, 0, 1, tzinfo=UTC)
    session.add_all(
        [
            SandboxEntry(monitor_id=None, created_at=yesterday, expires_at=yesterday),
            SandboxEntry(monitor_id=None, created_at=today, expires_at=today),
        ]
    )
    session.commit()
    assert additions_remaining(session, now=now) == 2
    assert additions_remaining(session, now=datetime(2026, 9, 7, 0, 0, tzinfo=UTC)) == 3


def test_status_says_when_the_cap_resets(client, sandbox):
    sb = client.get("/api/status").get_json()["sandbox"]
    assert sb["resets_at"].endswith("T00:00:00+00:00")
    assert sb["resets_at"] > utcnow().isoformat()


def test_status_reports_the_sandbox_rules(client, sandbox):
    sb = client.get("/api/status").get_json()["sandbox"]
    assert sb["enabled"] is True
    assert sb["max_per_day"] == 3
    assert sb["additions_remaining"] == 3
    assert sb["ttl_hours"] == 24
    client.post("/api/monitors", json=NEW)
    assert client.get("/api/status").get_json()["sandbox"]["additions_remaining"] == 2


def test_status_reports_sandbox_off(client, monkeypatch):
    monkeypatch.delenv("SANDBOX", raising=False)
    assert client.get("/api/status").get_json()["sandbox"] == {"enabled": False}
    assert client.get("/health").get_json()["sandbox"]["enabled"] is False


def test_visitor_cannot_delete_a_seeded_monitor(client, session, sandbox):
    seeded = _seeded(session)
    resp = client.delete(f"/api/monitors/{seeded}")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "protected"
    assert session.get(Monitor, seeded) is not None


def test_operator_can_delete_a_seeded_monitor(client, session, sandbox):
    seeded = _seeded(session)
    assert client.delete(f"/api/monitors/{seeded}", headers=bearer()).status_code == 200


def test_visitor_can_delete_a_visitor_monitor(client, sandbox):
    created = client.post("/api/monitors", json=NEW).get_json()
    assert client.delete(f"/api/monitors/{created['id']}").status_code == 200


@responses.activate
def test_visitor_can_check_now_on_anything(client, session, sandbox):
    seeded = _seeded(session)
    responses.add(responses.GET, "https://seeded.example.org", status=200)
    assert client.post(f"/api/monitors/{seeded}/check").status_code == 201


def test_unknown_write_paths_still_need_the_token(client, sandbox):
    assert client.post("/api/does-not-exist").status_code == 401


@pytest.mark.parametrize(
    "target",
    [
        "http://localhost/health",
        "http://127.0.0.1:8080/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://db.internal/",
        "http://printer.local/",
        "http://intranet/",
    ],
)
def test_visitor_targets_must_be_public(client, sandbox, target):
    resp = client.post("/api/monitors", json={"name": "x", "target": target})
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "target"


def test_operator_may_target_private_hosts(client, sandbox):
    resp = client.post(
        "/api/monitors", json={"name": "x", "target": "http://10.0.0.5/"}, headers=bearer()
    )
    assert resp.status_code == 201


def test_public_hostname_helper():
    assert is_public_hostname("https://example.org/")
    assert is_public_hostname("https://8.8.8.8/")
    assert not is_public_hostname("https://172.16.0.1/")
    assert not is_public_hostname("not a url")


def test_visitor_interval_floor(client, sandbox):
    resp = client.post("/api/monitors", json={**NEW, "interval_seconds": 10})
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "interval_seconds"
    assert client.post("/api/monitors", json={**NEW, "interval_seconds": 60}).status_code == 201


def test_expiry_removes_the_monitor_and_its_history(client, session, sandbox):
    created = client.post("/api/monitors", json=NEW).get_json()
    session.add(CheckResultRow(monitor_id=created["id"], status="ok", message=""))
    session.commit()

    assert expire_sandbox_monitors(session=session, now=utcnow()) == 0
    later = utcnow() + timedelta(hours=24, minutes=1)
    assert expire_sandbox_monitors(session=session, now=later) == 1

    assert session.get(Monitor, created["id"]) is None
    assert session.query(CheckResultRow).count() == 0
    # The entry survives with no monitor, so it still counts toward the cap.
    entry = session.query(SandboxEntry).one()
    assert entry.monitor_id is None
    assert additions_remaining(session) == 2


def test_scheduler_tick_expires_sandbox_monitors(client, session, sandbox):
    created = client.post("/api/monitors", json=NEW).get_json()
    monitor_id = created["id"]
    scheduler = Scheduler()
    now = utcnow()
    acquire_lease(session=session, owner=scheduler.owner, now=now)
    scheduler.owns_lease = True

    scheduler.tick(now=now + timedelta(hours=25))
    session.expire_all()
    assert session.get(Monitor, monitor_id) is None


def test_ttl_and_cap_are_configurable(client, sandbox, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_PER_DAY", "1")
    monkeypatch.setenv("SANDBOX_TTL_HOURS", "2")
    first = client.post("/api/monitors", json=NEW)
    assert first.status_code == 201
    assert client.post("/api/monitors", json={**NEW, "name": "second"}).status_code == 429
    sb = client.get("/api/status").get_json()["sandbox"]
    assert sb["ttl_hours"] == 2 and sb["max_per_day"] == 1
