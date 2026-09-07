# VENDORED CODE, DO NOT EDIT LIGHTLY.
#
# Source: github.com/alexander-constanza/infra-health-check
# Path in source repo: infra_health/config.py
# Vendored at: version 0.2.0
#
# Why vendored rather than installed as a dependency:
# infra-health-check is a portfolio CLI, not a package published to PyPI, so
# `pip install infra-health-check` does not resolve from a fresh clone or
# inside a Docker build. Rather than depend on a private index or a git URL
# that would break this repo's build the moment the other repo moved, the
# check engine is copied in verbatim. This repo stays a single deployable
# unit with a plain requirements.txt.
#
# What changed from the original: only the import paths, rewritten from
# `infra_health.*` to `apihealthchecker.engine.*`. The check logic, the
# three-state Status (ok / fail / unknown), the retry semantics and the
# thread-pool runner are unmodified upstream code. Upstream is the place to
# fix check behaviour; changes should be made there and re-vendored.
#
# Regenerate with: python3 scripts/vendor_engine.py

"""Load a batch of checks from a YAML file, so a whole stack can be
checked in one command instead of one resource at a time.

Example file:

    checks:
      - type: s3
        bucket_name: my-app-uploads
      - type: ec2
        instance_id: i-0123456789abcdef0
      - type: http
        url: https://my-app.example.com/health
      - type: tailscale_path
        peer: ec2-api

Each entry may also carry `region` and `profile` (AWS checks) to override
whatever the ambient environment resolves to. An http entry may carry
`proxy` to route that one request through a proxy, which is how a private
endpoint reachable only over a tunnel gets checked from a container that
has no tunnel device of its own.
"""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from apihealthchecker.engine.checks import (
    CheckResult,
    Status,
    build_session,
    check_ec2_instance,
    check_http_endpoint,
    check_s3_bucket,
)
from apihealthchecker.engine.tailscale import DEFAULT_TIMEOUT_SECONDS as TAILSCALE_TIMEOUT_SECONDS
from apihealthchecker.engine.tailscale import check_tailscale_path

DEFAULT_MAX_WORKERS = 8


def _s3_handler(entry: dict, ctx: dict) -> CheckResult:
    session = build_session(
        entry.get("region", ctx.get("region")),
        entry.get("profile", ctx.get("profile")),
    )
    return check_s3_bucket(
        entry["bucket_name"],
        session=session,
        retries=ctx.get("retries", 0),
        retry_delay=ctx.get("retry_delay", 1.0),
    )


def _ec2_handler(entry: dict, ctx: dict) -> CheckResult:
    session = build_session(
        entry.get("region", ctx.get("region")),
        entry.get("profile", ctx.get("profile")),
    )
    return check_ec2_instance(
        entry["instance_id"],
        session=session,
        retries=ctx.get("retries", 0),
        retry_delay=ctx.get("retry_delay", 1.0),
    )


def _http_handler(entry: dict, ctx: dict) -> CheckResult:
    return check_http_endpoint(
        entry["url"],
        timeout_seconds=entry.get("timeout", 5.0),
        retries=ctx.get("retries", 0),
        retry_delay=ctx.get("retry_delay", 1.0),
        proxy=entry.get("proxy", ctx.get("proxy")),
    )


def _tailscale_path_handler(entry: dict, ctx: dict) -> CheckResult:
    """Check the quality of the path to one tailnet peer.

    Unlike the AWS checks this one takes nothing from the shared context
    except the retry settings: the tailnet it reads is whichever one the
    local daemon is joined to, and there is no per-entry equivalent of a
    region to override.
    """
    return check_tailscale_path(
        entry["peer"],
        require_direct=entry.get("require_direct", True),
        warm=entry.get("warm", True),
        diagnose=entry.get("diagnose", True),
        timeout_seconds=entry.get("timeout", TAILSCALE_TIMEOUT_SECONDS),
        retries=ctx.get("retries", 0),
        retry_delay=ctx.get("retry_delay", 1.0),
    )


_CHECK_DISPATCH: dict[str, Callable[[dict, dict], CheckResult]] = {
    "s3": _s3_handler,
    "ec2": _ec2_handler,
    "http": _http_handler,
    "tailscale_path": _tailscale_path_handler,
}


def _run_one(index: int, entry: Any, ctx: dict) -> CheckResult:
    """Run a single config entry, converting any shape problem into UNKNOWN."""
    if not isinstance(entry, dict):
        return CheckResult(
            f"config:[{index}]",
            Status.UNKNOWN,
            f"Check entry must be a mapping, got {type(entry).__name__}",
        )

    check_type = entry.get("type")
    handler = _CHECK_DISPATCH.get(check_type)
    if handler is None:
        return CheckResult(
            f"config:[{index}]",
            Status.UNKNOWN,
            f"Unknown check type '{check_type}'",
        )
    try:
        return handler(entry, ctx)
    except KeyError as exc:
        return CheckResult(
            f"config:[{index}]:{check_type}",
            Status.UNKNOWN,
            f"Missing required field {exc}",
        )


def run_checks(
    entries: list[Any],
    max_workers: int | None = None,
    sequential: bool = False,
    region: str | None = None,
    profile: str | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
    proxy: str | None = None,
) -> list[CheckResult]:
    """Run a list of parsed check entries and return results in input order.

    Checks are pure I/O (network round trips), so a thread pool is the right
    tool: it overlaps the waiting without any shared mutable state. Results
    are collected in submission order so output stays deterministic and
    diffable regardless of which check finishes first.
    """
    ctx = {
        "region": region,
        "profile": profile,
        "retries": retries,
        "retry_delay": retry_delay,
        "proxy": proxy,
    }
    if not entries:
        return []

    if sequential or len(entries) == 1:
        return [_run_one(i, entry, ctx) for i, entry in enumerate(entries)]

    workers = max_workers if max_workers is not None else DEFAULT_MAX_WORKERS
    workers = max(1, min(workers, len(entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_one, i, entry, ctx) for i, entry in enumerate(entries)]
        return [f.result() for f in futures]


def load_checks_from_file(
    path: str,
    max_workers: int | None = None,
    sequential: bool = False,
    region: str | None = None,
    profile: str | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
    proxy: str | None = None,
) -> list[CheckResult]:
    """Parse a YAML check file and run everything in it.

    Any problem with the file itself (missing, unparseable, wrong shape) comes
    back as an UNKNOWN CheckResult rather than an exception, so a bad config is
    reported the same way as an unreachable resource: as a result the caller
    can print and act on.
    """
    try:
        with Path(path).open() as f:
            config: Any = yaml.safe_load(f)
    except FileNotFoundError:
        return [CheckResult("config", Status.UNKNOWN, f"Config file not found: {path}")]
    except OSError as exc:
        return [CheckResult("config", Status.UNKNOWN, f"Could not read {path}: {exc}")]
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return [CheckResult("config", Status.UNKNOWN, f"Invalid YAML in {path}: {problem}")]

    if config is None:
        config = {}

    if not isinstance(config, dict):
        return [
            CheckResult(
                "config",
                Status.UNKNOWN,
                f"Config root must be a mapping, got {type(config).__name__}",
            )
        ]

    checks = config.get("checks", [])
    if checks is None:
        checks = []

    if not isinstance(checks, list):
        return [
            CheckResult(
                "config",
                Status.UNKNOWN,
                f"'checks' must be a list, got {type(checks).__name__}",
            )
        ]

    return run_checks(
        checks,
        max_workers=max_workers,
        sequential=sequential,
        region=region,
        profile=profile,
        retries=retries,
        retry_delay=retry_delay,
        proxy=proxy,
    )
