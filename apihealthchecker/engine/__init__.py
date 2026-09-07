"""The check engine, vendored from infra-health-check.

This package is the check-running half of apihealthchecker. It is a verbatim
copy of the public API that github.com/alexander-constanza/infra-health-check
deliberately exposes for a consumer exactly like this one: a service that runs
those checks on a schedule.

Nothing in this service reimplements checking. The scheduler and the REST API
call `run_checks` and the individual `check_*` functions and then persist the
`CheckResult` objects they hand back. See the header comment in checks.py for
why the code is vendored instead of pip-installed.
"""
from apihealthchecker.engine.checks import (
    CheckResult,
    Status,
    build_session,
    check_ec2_instance,
    check_http_endpoint,
    check_s3_bucket,
    with_retries,
)
from apihealthchecker.engine.config import run_checks
from apihealthchecker.engine.tailscale import check_tailscale_path, diagnose_path

__all__ = [
    "CheckResult",
    "Status",
    "build_session",
    "check_ec2_instance",
    "check_http_endpoint",
    "check_s3_bucket",
    "check_tailscale_path",
    "diagnose_path",
    "run_checks",
    "with_retries",
]
