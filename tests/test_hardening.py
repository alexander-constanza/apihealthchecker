"""Hardening tests, each one a case that an adversarial pass found broken or
missing: loopback spelled in ways `ipaddress` does not parse, ids the database
cannot hold, header injection, parser recursion, the cap under concurrency,
check-now as an outbound request source, sandbox monitors reaching the
webhook, and the response headers a browser needs to refuse the rest."""
import json
import threading

import pytest
import responses

from apihealthchecker.db import CheckResultRow, Monitor, utcnow
from apihealthchecker.sandbox import (
    check_cooldown_remaining,
    is_public_hostname,
    record_manual_check,
)

TOKEN = "operator-token"
HOOK = "https://hooks.example.org/alerts"
PUBLIC = "https://visitor.example.org/"


@pytest.fixture()
def sandbox(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("SANDBOX", "1")


def bearer():
    return {"Authorization": f"Bearer {TOKEN}"}


def _ok_row(monitor_id):
    return CheckResultRow(monitor_id=monitor_id, status="ok", message="", checked_at=utcnow())


# ---- hostnames -------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://127.1/",
        "http://127.0.1/",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://017700000001/",
        "http://0/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://100.64.0.1/",
    ],
)
def test_loopback_and_reserved_in_every_spelling_are_rejected(target):
    assert is_public_hostname(target) is False


def test_userinfo_does_not_confuse_the_host_check():
    assert is_public_hostname("http://example.org@127.0.0.1/") is False
    assert is_public_hostname("http://127.0.0.1:80@example.org/") is True


def test_a_public_name_that_resolves_privately_is_rejected(monkeypatch):
    monkeypatch.setattr("apihealthchecker.sandbox._resolve", lambda host: ["169.254.169.254"])
    assert is_public_hostname("http://169.254.169.254.nip.io/") is False


def test_a_name_with_one_private_address_among_public_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "apihealthchecker.sandbox._resolve", lambda host: ["93.184.216.34", "10.0.0.1"]
    )
    assert is_public_hostname("http://mixed.example.org/") is False


def test_a_name_that_does_not_resolve_is_allowed(monkeypatch):
    """It fails as a check, which is harmless, and is exactly the seeded DNS demo."""
    monkeypatch.setattr("apihealthchecker.sandbox._resolve", lambda host: [])
    assert is_public_hostname("https://this-host-does-not-exist-xyz123.com/") is True


def test_visitors_may_only_add_http_monitors(client, sandbox):
    resp = client.post("/api/monitors", json={"name": "b", "target": "my.bucket", "type": "s3"})
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "type"


# ---- ids, bodies, headers ---------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("delete", "/api/monitors/99999999999999999999999"),
    ("post", "/api/monitors/99999999999999999999999/check"),
    ("get", "/api/monitors/99999999999999999999999/history"),
])
def test_ids_beyond_64_bits_are_404_not_500(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 404


def test_oversized_body_is_413(client):
    body = json.dumps({"name": "a" * 70000, "target": PUBLIC})
    resp = client.post("/api/monitors", data=body, content_type="application/json")
    assert resp.status_code == 413
    assert resp.get_json()["error"] == "request_entity_too_large"


def test_deeply_nested_json_is_400_not_500(client):
    body = "[" * 5000 + "]" * 5000
    resp = client.post("/api/monitors", data=body, content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"


@pytest.mark.parametrize("name", ["a\u0000b", "a\u001b[31mb", "line\nbreak", "tab\tname"])
def test_control_characters_are_rejected(client, name):
    resp = client.post("/api/monitors", json={"name": name, "target": PUBLIC})
    assert resp.status_code == 422
    assert resp.get_json()["field"] == "name"


def test_request_id_pattern_refuses_newlines():
    """HTTP parsers refuse a raw header with a newline before the app sees it,
    and werkzeug's test client refuses to build one, so the pattern is tested
    directly: it is the only thing between an inbound value and a response
    header."""
    from apihealthchecker.app import REQUEST_ID_PATTERN

    # fullmatch, not match: with `$` a trailing newline would slip through.
    assert REQUEST_ID_PATTERN.fullmatch("abc\r\nX-Injected: 1") is None
    assert REQUEST_ID_PATTERN.fullmatch("abc\n") is None
    assert REQUEST_ID_PATTERN.fullmatch("abc") is not None


def test_request_id_with_spaces_or_unicode_is_replaced(client):
    for bad in ("a b", "éé", "x" * 201):
        echoed = client.get("/health", headers={"X-Request-Id": bad}).headers["X-Request-Id"]
        assert echoed != bad


def test_request_id_in_the_safe_set_is_echoed(client):
    resp = client.get("/health", headers={"X-Request-Id": "trace-1.2:abc_X"})
    assert resp.headers["X-Request-Id"] == "trace-1.2:abc_X"


def test_security_headers_on_every_response(client):
    for path in ("/", "/health", "/api/status"):
        resp = client.get(path)
        csp = resp.headers["Content-Security-Policy"]
        assert csp.startswith("default-src 'none'")
        assert "'nonce-" in csp
        assert "'unsafe-inline'" not in csp
        assert "frame-ancestors 'none'" in csp
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_page_inline_blocks_carry_the_response_nonce(client):
    resp = client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
    html = resp.get_data(as_text=True)
    assert f'<style nonce="{nonce}">' in html
    assert f'<script nonce="{nonce}">' in html
    assert 'style="' not in html


def test_nonce_changes_per_request(client):
    first = client.get("/").headers["Content-Security-Policy"]
    second = client.get("/").headers["Content-Security-Policy"]
    assert first != second


# ---- cap under concurrency --------------------------------------------------


def test_cap_holds_under_concurrent_visitors(app, sandbox, monkeypatch):
    """Twelve visitors hit create at the same instant. Exactly three succeed,
    the rest get a clean 429, and nothing gets a 500 from lock contention.

    The test database is in memory on one shared connection (StaticPool), so
    a request's session teardown would roll back another thread's transaction
    mid-flight. That is a property of the test setup, not of the file-backed
    database in production where each thread has its own connection, so the
    teardown is a no-op for the duration of this test."""
    from apihealthchecker.db import SessionLocal

    monkeypatch.setattr(SessionLocal, "remove", lambda: None)
    results = []
    barrier = threading.Barrier(12)

    def visitor(i):
        c = app.test_client()
        barrier.wait()
        payload = {"name": f"v{i}", "target": PUBLIC, "interval_seconds": 60}
        results.append(c.post("/api/monitors", json=payload).status_code)

    threads = [threading.Thread(target=visitor, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 3
    assert results.count(429) == 9
    assert app.test_client().get("/api/status").get_json()["sandbox"]["additions_remaining"] == 0


# ---- check-now cooldown -----------------------------------------------------


def test_cooldown_helper_counts_down():
    assert check_cooldown_remaining(42, now=100.0) == 0
    record_manual_check(42, now=100.0)
    assert check_cooldown_remaining(42, now=100.0) == 30
    assert check_cooldown_remaining(42, now=129.5) == 1
    assert check_cooldown_remaining(42, now=130.0) == 0


@responses.activate
def test_visitor_check_now_has_a_cooldown_and_the_operator_does_not(client, session, sandbox):
    monitor = Monitor(name="s", target="https://seeded.example.org", type="http")
    session.add(monitor)
    session.commit()
    monitor_id = monitor.id  # read before a request closes the scoped session
    responses.add(responses.GET, "https://seeded.example.org", status=200)

    assert client.post(f"/api/monitors/{monitor_id}/check").status_code == 201
    second = client.post(f"/api/monitors/{monitor_id}/check")
    assert second.status_code == 429
    assert second.get_json()["error"] == "cooldown"
    assert int(second.headers["Retry-After"]) >= 1
    assert client.post(f"/api/monitors/{monitor_id}/check", headers=bearer()).status_code == 201


# ---- sandbox monitors never reach the webhook -------------------------------


@responses.activate
def test_sandbox_monitor_transitions_are_logged_but_not_sent(
    client, session, sandbox, monkeypatch, caplog
):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    created = client.post("/api/monitors", json={"name": "v", "target": PUBLIC}).get_json()
    session.add(_ok_row(created["id"]))
    session.commit()
    responses.add(responses.GET, PUBLIC, status=503)
    responses.add(responses.POST, HOOK, status=200)

    with caplog.at_level("INFO", logger="apihealthchecker"):
        client.post(f"/api/monitors/{created['id']}/check", headers=bearer())

    assert [c for c in responses.calls if c.request.url == HOOK] == []
    changed = [r for r in caplog.records if r.getMessage() == "monitor_status_changed"]
    assert len(changed) == 1 and changed[0].alert_muted is True


@responses.activate
def test_operator_monitor_transitions_still_reach_the_webhook(
    client, session, sandbox, monkeypatch
):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    created = client.post(
        "/api/monitors", json={"name": "op", "target": PUBLIC}, headers=bearer()
    ).get_json()
    session.add(_ok_row(created["id"]))
    session.commit()
    responses.add(responses.GET, PUBLIC, status=503)
    responses.add(responses.POST, HOOK, status=200)

    client.post(f"/api/monitors/{created['id']}/check", headers=bearer())
    assert len([c for c in responses.calls if c.request.url == HOOK]) == 1
