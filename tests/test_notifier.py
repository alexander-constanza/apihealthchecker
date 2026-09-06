"""Notifier tests: which transitions alert, what is sent, and that a bad webhook
never breaks a check."""
import json

import responses

from apihealthchecker.db import CheckResultRow, Monitor, utcnow
from apihealthchecker.notifier import build_payload, transitions, webhook_url
from apihealthchecker.runner import run_monitors

HOOK = "https://hooks.example.org/alerts"


def _seed_result(session, monitor, status):
    row = CheckResultRow(monitor_id=monitor.id, status=status, message="", checked_at=utcnow())
    session.add(row)
    session.commit()
    return row


def _hook_calls():
    return [c for c in responses.calls if c.request.url == HOOK]


def test_webhook_url_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert webhook_url() is None
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "   ")
    assert webhook_url() is None


def test_transitions_ignore_repeats_and_a_first_ok():
    ok = CheckResultRow(monitor_id=1, status="ok")
    fail = CheckResultRow(monitor_id=2, status="fail")
    first_ok = CheckResultRow(monitor_id=3, status="ok")
    first_fail = CheckResultRow(monitor_id=4, status="fail")
    previous = {1: "ok", 2: "fail", 3: None, 4: None}

    assert transitions(previous, [ok, fail, first_ok, first_fail]) == [(None, first_fail)]


def test_transitions_report_both_directions():
    down = CheckResultRow(monitor_id=1, status="fail")
    up = CheckResultRow(monitor_id=2, status="ok")
    assert transitions({1: "ok", 2: "fail"}, [down, up]) == [("ok", down), ("fail", up)]


@responses.activate
def test_failure_posts_monitor_failed(session, monitor, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    _seed_result(session, monitor, "ok")
    responses.add(responses.GET, monitor.target, status=503)
    responses.add(responses.POST, HOOK, status=200)

    rows = run_monitors([monitor], session=session)

    calls = _hook_calls()
    assert len(calls) == 1
    body = json.loads(calls[0].request.body)
    assert body["event"] == "monitor_failed"
    assert body["previous_status"] == "ok"
    assert body["status"] == "fail"
    assert body["monitor"]["id"] == monitor.id
    assert body["result"]["id"] == rows[0].id
    assert body["result"]["severity"] == "critical"
    assert body["result"]["category"] == "server_error"


@responses.activate
def test_recovery_posts_monitor_recovered(session, monitor, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    _seed_result(session, monitor, "fail")
    responses.add(responses.GET, monitor.target, status=200)
    responses.add(responses.POST, HOOK, status=200)

    run_monitors([monitor], session=session)

    body = json.loads(_hook_calls()[0].request.body)
    assert body["event"] == "monitor_recovered"
    assert body["previous_status"] == "fail"


@responses.activate
def test_a_monitor_that_stays_down_alerts_once(session, monitor, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    _seed_result(session, monitor, "fail")
    responses.add(responses.GET, monitor.target, status=503)
    responses.add(responses.POST, HOOK, status=200)

    run_monitors([monitor], session=session)
    run_monitors([monitor], session=session)

    assert _hook_calls() == []


@responses.activate
def test_nothing_is_posted_without_a_webhook(session, monitor, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    _seed_result(session, monitor, "ok")
    responses.add(responses.GET, monitor.target, status=503)

    rows = run_monitors([monitor], session=session)

    assert rows[0].status == "fail"
    assert _hook_calls() == []


@responses.activate
def test_a_failing_webhook_does_not_lose_the_result(session, monitor, monkeypatch):
    """The result is committed before the webhook is called, so a dead
    webhook costs an alert, never a row."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    _seed_result(session, monitor, "ok")
    responses.add(responses.GET, monitor.target, status=503)
    responses.add(responses.POST, HOOK, status=500)

    rows = run_monitors([monitor], session=session)

    assert rows[0].status == "fail"
    assert session.query(CheckResultRow).count() == 2


@responses.activate
def test_an_unreachable_webhook_does_not_raise(session, monitor, monkeypatch):
    # No responses.add for HOOK: the library raises ConnectionError, which is
    # what a real dead host would do.
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    _seed_result(session, monitor, "ok")
    responses.add(responses.GET, monitor.target, status=503)

    rows = run_monitors([monitor], session=session)
    assert rows[0].status == "fail"


@responses.activate
def test_first_result_that_fails_alerts(session, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    new = Monitor(name="new", target="https://new.example.org", type="http")
    session.add(new)
    session.commit()
    responses.add(responses.GET, new.target, status=404)
    responses.add(responses.POST, HOOK, status=200)

    run_monitors([new], session=session)

    body = json.loads(_hook_calls()[0].request.body)
    assert body["event"] == "monitor_failed"
    assert body["previous_status"] is None


def test_payload_shape(session, monitor):
    row = _seed_result(session, monitor, "unknown")
    session.refresh(row)
    payload = build_payload(row, "ok")
    assert set(payload) == {"event", "previous_status", "status", "monitor", "result", "sent_at"}
    assert payload["event"] == "monitor_unknown"


def test_health_reports_whether_alerting_is_configured(client, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert client.get("/health").get_json()["alerting"]["webhook_configured"] is False
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    assert client.get("/health").get_json()["alerting"]["webhook_configured"] is True
