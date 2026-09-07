"""Persistence and runner tests: what gets stored, and in what shape."""
import responses

from apihealthchecker.db import CheckResultRow, Monitor, utcnow
from apihealthchecker.engine import CheckResult, Status
from apihealthchecker.runner import (
    monitor_to_entry,
    record_result,
    run_monitors,
    run_single,
    tailnet_proxy_for,
)
from apihealthchecker.seed import SEED_MONITORS, seed_monitors


def test_monitor_to_entry_http():
    entry = monitor_to_entry(Monitor(name="x", target="https://x.org", type="http"))
    assert entry["type"] == "http"
    assert entry["url"] == "https://x.org"
    assert "timeout" in entry


def test_monitor_to_entry_s3():
    entry = monitor_to_entry(Monitor(name="x", target="my-bucket", type="s3"))
    assert entry == {"type": "s3", "bucket_name": "my-bucket"}


def test_monitor_to_entry_ec2():
    entry = monitor_to_entry(Monitor(name="x", target="i-0123", type="ec2"))
    assert entry == {"type": "ec2", "instance_id": "i-0123"}


def test_record_result_stores_an_ok_check(session, monitor):
    result = CheckResult("http:x", Status.OK, "Responded 200", {"status_code": 200})
    row = record_result(session, monitor.id, result)
    session.commit()

    assert row.status == "ok"
    assert row.category is None
    assert row.severity is None


def test_record_result_classifies_a_failure(session, monitor):
    result = CheckResult("http:x", Status.FAIL, "Connection failed", None)
    row = record_result(session, monitor.id, result)
    session.commit()

    assert row.status == "fail"
    assert row.category == "connectivity"
    assert row.severity == "critical"
    assert row.detail["classifier_used"] == "rules"


def test_unknown_results_are_not_classified(session, monitor):
    """UNKNOWN means "could not determine". Giving it a severity would put a
    config problem on the status page as though it were an outage."""
    result = CheckResult("http:x", Status.UNKNOWN, "Invalid URL: no schema", None)
    row = record_result(session, monitor.id, result)
    session.commit()

    assert row.status == "unknown"
    assert row.category is None
    assert row.severity is None


def test_latency_prefers_the_engines_own_measurement(session, monitor):
    result = CheckResult("http:x", Status.OK, "Responded 200", {"elapsed_ms": 42.5})
    row = record_result(session, monitor.id, result, wall_ms=999.0)
    assert row.latency_ms == 42.5


def test_latency_falls_back_to_wall_clock(session, monitor):
    result = CheckResult("s3:b", Status.OK, "Bucket is reachable", {})
    row = record_result(session, monitor.id, result, wall_ms=17.0)
    assert row.latency_ms == 17.0


@responses.activate
def test_run_single_persists_a_row(session, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=200)
    row = run_single(monitor, session=session)

    assert row.id is not None
    assert session.query(CheckResultRow).filter_by(monitor_id=monitor.id).count() == 1


@responses.activate
def test_run_monitors_returns_rows_in_input_order(session):
    first = Monitor(name="a", target="https://a.example.org", type="http")
    second = Monitor(name="b", target="https://b.example.org", type="http")
    session.add_all([first, second])
    session.commit()

    responses.add(responses.GET, "https://a.example.org", status=200)
    responses.add(responses.GET, "https://b.example.org", status=500)

    rows = run_monitors([first, second], session=session)
    assert [r.monitor_id for r in rows] == [first.id, second.id]
    assert rows[0].status == "ok"
    assert rows[1].status == "fail"


def test_run_monitors_with_nothing_to_do(session):
    assert run_monitors([], session=session) == []


def test_result_to_dict_shape(session, monitor):
    row = CheckResultRow(
        monitor_id=monitor.id, status="ok", message="Responded 200", detail={"status_code": 200}
    )
    session.add(row)
    session.commit()

    payload = row.to_dict()
    assert set(payload) == {
        "id", "monitor_id", "status", "message", "detail",
        "category", "severity", "latency_ms", "checked_at",
    }


def test_timestamps_are_serialized_as_utc(session, monitor):
    row = CheckResultRow(monitor_id=monitor.id, status="ok", message="", checked_at=utcnow())
    session.add(row)
    session.commit()

    stamp = row.to_dict()["checked_at"]
    assert stamp.endswith("+00:00")


def test_monitor_to_dict_shape(monitor):
    payload = monitor.to_dict()
    assert set(payload) == {
        "id", "name", "target", "type", "interval_seconds", "enabled", "created_at"
    }


def test_sqlite_foreign_keys_are_enforced(session):
    """SQLite leaves foreign key enforcement off by default, which would make
    the ondelete="CASCADE" on check_results a decorative comment and leave
    orphaned rows behind every deleted monitor."""
    from apihealthchecker.db import engine

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").fetchone()[0] == 1


def test_deleting_a_monitor_cascades_to_its_results(session, monitor):
    session.add(CheckResultRow(monitor_id=monitor.id, status="ok", message=""))
    session.commit()

    session.delete(monitor)
    session.commit()
    assert session.query(CheckResultRow).count() == 0


def test_seeding_inserts_the_demo_monitors(session):
    added = seed_monitors(session=session)
    assert added == len(SEED_MONITORS)
    assert session.query(Monitor).count() == len(SEED_MONITORS)


def test_seeding_is_idempotent(session):
    seed_monitors(session=session)
    assert seed_monitors(session=session) == 0
    assert session.query(Monitor).count() == len(SEED_MONITORS)


def test_seeding_does_not_touch_user_monitors(session):
    session.add(Monitor(name="mine", target="https://mine.example.org", type="http"))
    session.commit()

    seed_monitors(session=session)
    assert session.query(Monitor).filter_by(name="mine").count() == 1


def test_seed_targets_are_absolute_urls():
    for spec in SEED_MONITORS:
        assert spec["target"].startswith("https://")


def test_seed_includes_a_deliberate_failure_case():
    """A status page where everything is always green demonstrates nothing."""
    targets = [s["target"] for s in SEED_MONITORS]
    assert any("no-such-pkg" in t for t in targets)
    assert any("this-host-does-not-exist" in t for t in targets)


def test_monitor_to_entry_tailscale_path():
    entry = monitor_to_entry(Monitor(name="x", target="ec2-api", type="tailscale_path"))
    assert entry["type"] == "tailscale_path"
    assert entry["peer"] == "ec2-api"


def test_public_http_monitors_are_not_proxied(monkeypatch):
    """The tunnel must not sit on the critical path of every check.

    If it did, a dead tunnel would report the entire public internet as down,
    which is a worse failure than the one the tunnel was added to solve.
    """
    monkeypatch.setenv("TAILNET_HTTP_PROXY", "http://localhost:1055")
    entry = monitor_to_entry(Monitor(name="x", target="https://api.github.com", type="http"))
    assert entry["proxy"] is None


def test_tailnet_http_monitors_are_proxied(monkeypatch):
    monkeypatch.setenv("TAILNET_HTTP_PROXY", "http://localhost:1055")
    entry = monitor_to_entry(
        Monitor(name="x", target="http://ec2-api.tail1234.ts.net/health", type="http")
    )
    assert entry["proxy"] == "http://localhost:1055"


def test_tailnet_cgnat_addresses_are_proxied(monkeypatch):
    monkeypatch.setenv("TAILNET_HTTP_PROXY", "http://localhost:1055")
    assert tailnet_proxy_for("http://100.101.102.103:8080/health") == "http://localhost:1055"


def test_addresses_just_outside_the_tailscale_range_are_not_proxied(monkeypatch):
    """100.64.0.0/10 ends at 100.127.255.255. 100.128.0.0 is ordinary public
    space and must not be routed into the tunnel."""
    monkeypatch.setenv("TAILNET_HTTP_PROXY", "http://localhost:1055")
    assert tailnet_proxy_for("http://100.128.0.1/health") is None
    assert tailnet_proxy_for("http://100.63.255.255/health") is None
    assert tailnet_proxy_for("http://100.64.0.1/health") == "http://localhost:1055"


def test_nothing_is_proxied_when_no_proxy_is_configured(monkeypatch):
    monkeypatch.delenv("TAILNET_HTTP_PROXY", raising=False)
    assert tailnet_proxy_for("http://ec2-api.tail1234.ts.net/health") is None


def test_a_malformed_target_does_not_raise(monkeypatch):
    monkeypatch.setenv("TAILNET_HTTP_PROXY", "http://localhost:1055")
    assert tailnet_proxy_for("not a url") is None
    assert tailnet_proxy_for("") is None
