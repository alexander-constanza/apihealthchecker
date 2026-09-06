"""Auth tests: writes need the token when one is set, reads never do, and unset
means open. The token is read from the environment per request, so monkeypatch
is enough and no app has to be rebuilt."""
import pytest

from apihealthchecker.auth import api_token, request_is_authorized, write_token_required

TOKEN = "s3cr3t-token-for-tests"
NEW = {"name": "x", "target": "https://x.example.org"}


@pytest.fixture()
def protected(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)


@pytest.fixture()
def open_writes(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_api_token_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "   ")
    assert api_token() is None
    assert write_token_required() is False


def test_create_without_token_is_401(client, protected):
    resp = client.post("/api/monitors", json=NEW)
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/api/monitors").get_json()["monitors"] == []


def test_create_with_wrong_token_is_401(client, protected):
    assert client.post("/api/monitors", json=NEW, headers=bearer("nope")).status_code == 401


def test_create_with_wrong_scheme_is_401(client, protected):
    resp = client.post("/api/monitors", json=NEW, headers={"Authorization": f"Basic {TOKEN}"})
    assert resp.status_code == 401


def test_create_with_token_succeeds(client, protected):
    assert client.post("/api/monitors", json=NEW, headers=bearer()).status_code == 201


def test_delete_and_check_now_are_protected(client, protected):
    created = client.post("/api/monitors", json=NEW, headers=bearer()).get_json()
    assert client.post(f"/api/monitors/{created['id']}/check").status_code == 401
    assert client.delete(f"/api/monitors/{created['id']}").status_code == 401
    assert client.delete(f"/api/monitors/{created['id']}", headers=bearer()).status_code == 200


def test_reads_stay_open(client, protected):
    for path in ("/", "/health", "/api/status", "/api/monitors", "/api/monitors/1/history"):
        assert client.get(path).status_code in (200, 404), path


def test_unset_means_open(client, open_writes):
    assert client.post("/api/monitors", json=NEW).status_code == 201


def test_unauthorized_wins_over_validation(client, protected):
    """The token is checked before the body is looked at, so an attacker
    cannot probe validation rules without one."""
    resp = client.post("/api/monitors", data="not json", content_type="application/json")
    assert resp.status_code == 401


def test_unauthorized_wins_over_404(client, protected):
    assert client.delete("/api/monitors/99999").status_code == 401


def test_unknown_write_paths_under_api_fail_closed(client, protected):
    """A write endpoint that does not exist yet is still behind the token."""
    assert client.post("/api/does-not-exist").status_code == 401


def test_health_reports_auth_mode(client, protected, monkeypatch):
    assert client.get("/health").get_json()["auth"]["write_token_required"] is True
    monkeypatch.delenv("API_TOKEN")
    assert client.get("/health").get_json()["auth"]["write_token_required"] is False


def test_request_is_authorized_helper(app, protected):
    with app.test_request_context("/api/monitors", method="POST", headers=bearer()):
        assert request_is_authorized() is True
    with app.test_request_context("/api/monitors", method="POST", headers=bearer("x")):
        assert request_is_authorized() is False
