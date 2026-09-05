"""API endpoint tests: routing, validation, and the error-handler split."""
import responses


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["dependencies"]["database"] == "ok"


def test_health_reports_scheduler_state(client):
    body = client.get("/health").get_json()
    # The test app never claims the lease, so it must not claim to be running one.
    assert body["scheduler"]["running_in_this_process"] is False


def test_every_response_carries_request_id(client):
    resp = client.get("/health")
    assert resp.headers.get("X-Request-Id")


def test_inbound_request_id_is_propagated(client):
    resp = client.get("/health", headers={"X-Request-Id": "trace-abc-123"})
    assert resp.headers["X-Request-Id"] == "trace-abc-123"


def test_unknown_path_is_404_not_500(client):
    """The single most important test in the file. A catch-all Exception
    handler without an HTTPException handler in front of it turns this into a
    500, which lies to the client and inflates the error-rate metric."""
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not_found"


def test_wrong_method_is_405_not_500(client):
    resp = client.put("/api/monitors")
    assert resp.status_code == 405
    assert resp.get_json()["error"] == "method_not_allowed"


def test_unparseable_path_param_is_404(client):
    """<int:monitor_id> cannot match a word, so werkzeug 404s at routing."""
    resp = client.get("/api/monitors/not-a-number/history")
    assert resp.status_code == 404


def test_list_monitors_empty(client):
    body = client.get("/api/monitors").get_json()
    assert body["monitors"] == []


def test_create_monitor(client):
    resp = client.post(
        "/api/monitors",
        json={"name": "GitHub", "target": "https://api.github.com", "interval_seconds": 120},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "GitHub"
    assert body["type"] == "http"
    assert body["interval_seconds"] == 120
    assert body["enabled"] is True
    assert body["id"] > 0


def test_created_monitor_appears_in_list(client):
    client.post("/api/monitors", json={"name": "GitHub", "target": "https://api.github.com"})
    body = client.get("/api/monitors").get_json()
    assert len(body["monitors"]) == 1
    assert body["monitors"][0]["status"] == "pending"
    assert body["monitors"][0]["history"] == []


def test_create_rejects_non_dict_body(client):
    resp = client.post("/api/monitors", json=["not", "a", "dict"])
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"


def test_create_rejects_unparseable_json(client):
    resp = client.post(
        "/api/monitors", data="{not json", content_type="application/json"
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"


def test_create_rejects_missing_fields(client):
    resp = client.post("/api/monitors", json={})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "missing_fields"
    assert set(body["fields"]) == {"name", "target"}


def test_create_treats_explicit_null_as_missing(client):
    resp = client.post("/api/monitors", json={"name": "x", "target": None})
    assert resp.status_code == 400
    assert resp.get_json()["fields"] == ["target"]


def test_create_rejects_wrong_type_for_name(client):
    resp = client.post("/api/monitors", json={"name": {"a": 1}, "target": "https://x.org"})
    assert resp.status_code == 422
    assert resp.get_json()["field"] == "name"


def test_create_rejects_relative_url(client):
    resp = client.post("/api/monitors", json={"name": "x", "target": "/health"})
    assert resp.status_code == 422
    assert resp.get_json()["field"] == "target"


def test_create_rejects_bad_interval(client):
    resp = client.post(
        "/api/monitors", json={"name": "x", "target": "https://x.org", "interval_seconds": 1}
    )
    assert resp.status_code == 422
    assert resp.get_json()["field"] == "interval_seconds"


def test_create_rejects_boolean_interval(client):
    """bool is a subclass of int, so True would otherwise be stored as 1 second."""
    resp = client.post(
        "/api/monitors", json={"name": "x", "target": "https://x.org", "interval_seconds": True}
    )
    assert resp.status_code == 422


def test_create_rejects_unknown_monitor_type(client):
    resp = client.post(
        "/api/monitors", json={"name": "x", "target": "https://x.org", "type": "lambda"}
    )
    assert resp.status_code == 422
    assert resp.get_json()["field"] == "type"


def test_delete_monitor(client):
    created = client.post(
        "/api/monitors", json={"name": "x", "target": "https://x.org"}
    ).get_json()
    resp = client.delete(f"/api/monitors/{created['id']}")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == created["id"]
    assert client.get("/api/monitors").get_json()["monitors"] == []


def test_delete_missing_monitor_is_404(client):
    resp = client.delete("/api/monitors/99999")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not_found"


def test_check_missing_monitor_is_404(client):
    resp = client.post("/api/monitors/99999/check")
    assert resp.status_code == 404


def test_history_missing_monitor_is_404(client):
    resp = client.get("/api/monitors/99999/history")
    assert resp.status_code == 404


def test_history_rejects_non_numeric_limit(client, monitor):
    """A bad query parameter is client input: a 400 with an explanation,
    never an uncaught ValueError surfacing as a 500."""
    resp = client.get(f"/api/monitors/{monitor.id}/history?limit=abc")
    assert resp.status_code == 400
    assert resp.get_json()["parameter"] == "limit"


def test_history_rejects_zero_limit(client, monitor):
    resp = client.get(f"/api/monitors/{monitor.id}/history?limit=0")
    assert resp.status_code == 400


@responses.activate
def test_check_now_records_a_passing_result(client, monitor):
    responses.add(responses.GET, "https://api.example.org/health", json={"ok": True}, status=200)

    resp = client.post(f"/api/monitors/{monitor.id}/check")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["category"] is None
    assert body["severity"] is None


@responses.activate
def test_check_now_classifies_a_failure(client, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=503)

    body = client.post(f"/api/monitors/{monitor.id}/check").get_json()
    assert body["status"] == "fail"
    assert body["category"] == "server_error"
    assert body["severity"] == "critical"
    assert body["detail"]["classifier_used"] == "rules"


@responses.activate
def test_history_returns_recorded_results_newest_first(client, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=200)
    # Read the id once: the app removes its scoped session after each request,
    # which detaches ORM objects held across them. That teardown is correct
    # behaviour, so the test works with the id rather than the instance.
    monitor_id = monitor.id
    for _ in range(3):
        client.post(f"/api/monitors/{monitor_id}/check")

    body = client.get(f"/api/monitors/{monitor_id}/history").get_json()
    assert body["count"] == 3
    assert body["monitor"]["id"] == monitor_id
    stamps = [r["checked_at"] for r in body["results"]]
    assert stamps == sorted(stamps, reverse=True)


@responses.activate
def test_history_honours_limit(client, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=200)
    monitor_id = monitor.id
    for _ in range(4):
        client.post(f"/api/monitors/{monitor_id}/check")

    body = client.get(f"/api/monitors/{monitor_id}/history?limit=2").get_json()
    assert body["count"] == 2


def test_status_rollup_when_empty(client):
    body = client.get("/api/status").get_json()
    assert body["overall_status"] == "pending"
    assert body["monitor_count"] == 0
    assert body["worst_severity"] is None


@responses.activate
def test_status_rollup_counts_and_worst_severity(client, monitor):
    responses.add(responses.GET, "https://api.example.org/health", status=500)
    client.post(f"/api/monitors/{monitor.id}/check")

    body = client.get("/api/status").get_json()
    assert body["overall_status"] == "fail"
    assert body["counts"]["fail"] == 1
    assert body["worst_severity"] == "critical"
    assert body["last_check_at"] is not None


@responses.activate
def test_status_keeps_unknown_separate_from_fail(client, session):
    """An UNKNOWN check means "could not determine", not "broken". Folding it
    into the failure count would tell an operator to fix the wrong thing."""
    from apihealthchecker.db import CheckResultRow, Monitor

    row = Monitor(name="Bad config", target="https://x.invalid", type="http")
    session.add(row)
    session.commit()
    session.add(CheckResultRow(monitor_id=row.id, status="unknown", message="Invalid URL"))
    session.commit()

    body = client.get("/api/status").get_json()
    assert body["counts"]["unknown"] == 1
    assert body["counts"]["fail"] == 0
    assert body["overall_status"] == "unknown"


@responses.activate
def test_deleting_a_monitor_removes_its_results(client, monitor, session):
    from apihealthchecker.db import CheckResultRow

    responses.add(responses.GET, "https://api.example.org/health", status=200)
    monitor_id = monitor.id
    client.post(f"/api/monitors/{monitor_id}/check")

    client.delete(f"/api/monitors/{monitor_id}")
    session.expire_all()
    remaining = session.query(CheckResultRow).filter_by(monitor_id=monitor_id).count()
    assert remaining == 0
