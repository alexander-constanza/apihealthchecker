"""Tests for the served status page.

The UI is a single self-contained template, so what is worth asserting is that
it renders, that it carries the elements the JavaScript expects to find, and
that it stays offline-capable: no external stylesheet, script or font.
"""
import re


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"


def test_index_contains_the_mount_points_the_script_targets(client):
    html = client.get("/").get_data(as_text=True)
    for element_id in ("rollup", "monitors", "add-form", "notice"):
        assert f'id="{element_id}"' in html


def test_index_contains_the_add_monitor_form_fields(client):
    html = client.get("/").get_data(as_text=True)
    for field in ("name", "target", "interval_seconds"):
        assert f'name="{field}"' in html


def test_index_calls_the_api_endpoints(client):
    html = client.get("/").get_data(as_text=True)
    assert "/api/status" in html
    assert "/api/monitors" in html


def test_index_has_no_external_resources(client):
    """No CDN, no external font, no remote script. The page has to render on a
    machine with no internet access, which for a monitoring tool is not a
    hypothetical situation."""
    html = client.get("/").get_data(as_text=True)
    external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html)
    # Repo links in the footer are fine; loaded resources are not.
    loaded = [
        url for url in external
        if not url.startswith("https://github.com/alexander-constanza/")
    ]
    assert loaded == []


def test_index_supports_dark_mode(client):
    html = client.get("/").get_data(as_text=True)
    assert "prefers-color-scheme: dark" in html


def test_index_auto_refreshes(client):
    html = client.get("/").get_data(as_text=True)
    assert "setInterval(refresh" in html
    assert "REFRESH_MS = 15000" in html


def test_index_styles_every_status_state(client):
    html = client.get("/").get_data(as_text=True)
    for state in ("ok", "fail", "unknown", "pending"):
        assert f".badge.{state}" in html


def test_index_contains_no_dashes_in_copy(client):
    """Repo-wide style rule, asserted where it is most visible.

    The characters are written as escapes rather than literals so that this
    file, which is the one place that has to name them, does not itself contain
    the codepoints a repo-wide sweep is looking for.
    """
    html = client.get("/").get_data(as_text=True)
    assert "\u2014" not in html, "em dash in UI copy"
    assert "\u2013" not in html, "en dash in UI copy"
