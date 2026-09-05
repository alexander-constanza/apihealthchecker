# PORTED PATTERN.
#
# Source: github.com/alexander-constanza/api-debugging-toolkit
# Path in source repo: app/validation.py
#
# The rules are this repo's own (monitors, not orders), but the shape is the
# toolkit's and deliberately so: validation lives outside the route handlers, a
# rejection names the field at fault, and the 400/422 split is meaningful.
"""Request payload validation for the monitors API.

Every rejection carries a machine-readable "error" code plus the specific field
at fault, because "400 Bad Request" with no detail is the most common reason a
support ticket bounces back and forth for a day.

The status codes are deliberate:
- 400 means the request itself is wrong (unparseable, not an object, or a
  required field is absent).
- 422 means the request parsed fine but a value is semantically invalid.

Nothing here raises. Every bad input returns an error tuple the caller turns
into a response, so no client payload can reach the generic 500 handler.
"""
from __future__ import annotations

from urllib.parse import urlparse

VALID_TYPES = ("http", "s3", "ec2")

MIN_INTERVAL_SECONDS = 10
MAX_INTERVAL_SECONDS = 86400
DEFAULT_INTERVAL_SECONDS = 60

MAX_NAME_LENGTH = 200
MAX_TARGET_LENGTH = 2000


def validate_monitor(data: dict) -> tuple[dict | None, tuple[dict, int] | None]:
    """Validate a monitor-creation payload.

    Returns (cleaned, None) on success or (None, (body, status)) on failure, so
    the caller can jsonify the error body directly.

    An explicit null counts as missing rather than as a bad value: a client
    sending {"target": null} has the same underlying bug as one that omitted the
    key, and reporting it as missing_fields points them at it faster.
    """
    missing = [f for f in ("name", "target") if data.get(f) is None]
    if missing:
        return None, ({"error": "missing_fields", "fields": missing}, 400)

    for field, limit in (("name", MAX_NAME_LENGTH), ("target", MAX_TARGET_LENGTH)):
        value = data[field]
        if not isinstance(value, str) or not value.strip():
            return None, (
                {
                    "error": "invalid_field",
                    "field": field,
                    "message": f"{field} must be a non-empty string",
                },
                422,
            )
        if len(value.strip()) > limit:
            return None, (
                {
                    "error": "invalid_field",
                    "field": field,
                    "message": f"{field} must be at most {limit} characters",
                },
                422,
            )

    monitor_type = data.get("type", "http")
    if monitor_type is None:
        monitor_type = "http"
    if not isinstance(monitor_type, str) or monitor_type not in VALID_TYPES:
        return None, (
            {
                "error": "invalid_field",
                "field": "type",
                "message": f"type must be one of {', '.join(VALID_TYPES)}",
            },
            422,
        )

    target = data["target"].strip()
    if monitor_type == "http":
        target_error = _validate_http_target(target)
        if target_error is not None:
            return None, target_error

    interval = data.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if interval is None:
        interval = DEFAULT_INTERVAL_SECONDS
    # bool is a subclass of int, so True would otherwise be stored as 1 second.
    if isinstance(interval, bool) or not isinstance(interval, int):
        return None, (
            {
                "error": "invalid_field",
                "field": "interval_seconds",
                "message": "interval_seconds must be an integer",
            },
            422,
        )
    if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        return None, (
            {
                "error": "invalid_field",
                "field": "interval_seconds",
                "message": (
                    f"interval_seconds must be between {MIN_INTERVAL_SECONDS} "
                    f"and {MAX_INTERVAL_SECONDS}"
                ),
            },
            422,
        )

    enabled = data.get("enabled", True)
    if enabled is None:
        enabled = True
    if not isinstance(enabled, bool):
        return None, (
            {
                "error": "invalid_field",
                "field": "enabled",
                "message": "enabled must be a boolean",
            },
            422,
        )

    return {
        "name": data["name"].strip(),
        "target": target,
        "type": monitor_type,
        "interval_seconds": interval,
        "enabled": enabled,
    }, None


def _validate_http_target(target: str) -> tuple[dict, int] | None:
    """Reject a URL the check engine could only ever report as UNKNOWN.

    Catching this at creation time is the difference between a monitor that
    tells an operator something and one that sits on the status page reporting
    "unknown" forever because it was typed wrong.
    """
    try:
        parsed = urlparse(target)
    except ValueError:
        # urlparse raises on a few malformed inputs, notably a bad IPv6 literal.
        parsed = None

    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return (
            {
                "error": "invalid_field",
                "field": "target",
                "message": "target must be an absolute http:// or https:// URL",
            },
            422,
        )
    return None


def parse_limit(raw: str | None, default: int = 50, maximum: int = 500) -> tuple[int | None, str]:
    """Parse a ?limit= query parameter.

    Returns (value, "") on success or (None, message) on failure. A query
    parameter is client input like any other, so a non-numeric value is a 400
    with an explanation, never a 500 from an uncaught ValueError.
    """
    if raw is None or raw == "":
        return default, ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"'{raw}' is not an integer"
    if value < 1:
        return None, "limit must be at least 1"
    return min(value, maximum), ""
