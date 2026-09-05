# VENDORED CODE, DO NOT EDIT LIGHTLY.
#
# Source: github.com/alexander-constanza/infra-health-check
# Path in source repo: infra_health/checks.py
# Vendored at: version 0.1.0
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

"""Individual health checks against AWS resources and plain HTTP endpoints.

Each check function returns a CheckResult so the CLI and tests can treat
"the S3 bucket is unreachable" and "the EC2 instance isn't running" the
same way: a status, a human message, and enough detail to act on.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import boto3
import requests
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)


class Status(str, Enum):
    OK = "ok"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    detail: dict | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "detail": self.detail or {},
        }


def build_session(
    region: str | None = None,
    profile: str | None = None,
) -> boto3.Session | None:
    """Build a boto3 Session for an explicit region and/or profile.

    Returns None when neither is given, so callers fall back to boto3's own
    credential and region resolution rather than pinning anything.
    """
    if region is None and profile is None:
        return None
    kwargs = {}
    if region is not None:
        kwargs["region_name"] = region
    if profile is not None:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def with_retries(
    check: Callable[[], CheckResult],
    retries: int = 0,
    retry_delay: float = 1.0,
) -> CheckResult:
    """Run a check, retrying while it reports FAIL.

    A check passes if any attempt succeeds. UNKNOWN results are never
    retried: they mean "I could not determine this" (a malformed URL, a
    missing credential, a bad parameter), and repeating the same bad input
    cannot turn it into an answer. Only FAIL, which can be a transient blip,
    is worth another attempt.

    Exposed as a plain function so a library consumer gets retries without
    going through the CLI.
    """
    attempts = max(0, retries) + 1
    result = check()
    for attempt in range(1, attempts):
        if result.status != Status.FAIL:
            return result
        if retry_delay > 0:
            time.sleep(retry_delay)
        result = check()
        if result.status == Status.OK:
            detail = dict(result.detail or {})
            detail["attempts"] = attempt + 1
            result.detail = detail
    return result


def check_s3_bucket(
    bucket_name: str,
    session: boto3.Session | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> CheckResult:
    """Confirm an S3 bucket exists and is reachable with current credentials."""
    return with_retries(
        lambda: _check_s3_bucket_once(bucket_name, session),
        retries=retries,
        retry_delay=retry_delay,
    )


def _check_s3_bucket_once(bucket_name: str, session: boto3.Session | None) -> CheckResult:
    session = session or boto3.Session()
    name = f"s3:{bucket_name}"
    try:
        client = session.client("s3")
        client.head_bucket(Bucket=bucket_name)
        return CheckResult(name, Status.OK, f"Bucket '{bucket_name}' is reachable")
    except NoCredentialsError:
        return CheckResult(name, Status.UNKNOWN, "No AWS credentials configured")
    except EndpointConnectionError:
        return CheckResult(name, Status.FAIL, "Could not reach AWS S3 endpoint")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code in ("404", "NoSuchBucket"):
            return CheckResult(
                name, Status.FAIL, f"Bucket '{bucket_name}' does not exist", {"code": code}
            )
        if code in ("403", "AccessDenied"):
            return CheckResult(
                name, Status.FAIL, f"Access denied to bucket '{bucket_name}'", {"code": code}
            )
        if code in ("301", "PermanentRedirect"):
            return CheckResult(
                name,
                Status.UNKNOWN,
                f"Bucket '{bucket_name}' exists but is in a different region; "
                "retry with --region",
                {"code": code},
            )
        return CheckResult(name, Status.FAIL, f"Unexpected error: {code}", {"code": code})
    except ParamValidationError as exc:
        return CheckResult(name, Status.UNKNOWN, f"Invalid parameter: {exc}")
    except BotoCoreError as exc:
        return CheckResult(name, Status.UNKNOWN, f"AWS call failed: {type(exc).__name__}")


def check_ec2_instance(
    instance_id: str,
    session: boto3.Session | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> CheckResult:
    """Confirm an EC2 instance exists and report its current state."""
    return with_retries(
        lambda: _check_ec2_instance_once(instance_id, session),
        retries=retries,
        retry_delay=retry_delay,
    )


def _result_for_state(name: str, instance_id: str, state: str, detail: dict) -> CheckResult:
    if state == "running":
        return CheckResult(name, Status.OK, f"Instance '{instance_id}' is running", detail)
    return CheckResult(
        name, Status.FAIL, f"Instance '{instance_id}' is '{state}', not running", detail
    )


def _check_ec2_instance_once(instance_id: str, session: boto3.Session | None) -> CheckResult:
    session = session or boto3.Session()
    name = f"ec2:{instance_id}"
    try:
        client = session.client("ec2")
        resp = client.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            return CheckResult(name, Status.FAIL, f"Instance '{instance_id}' not found")

        instance = reservations[0]["Instances"][0]
        state = instance["State"]["Name"]
        detail = {"state": state, "instance_type": instance.get("InstanceType")}
        return _result_for_state(name, instance_id, state, detail)
    except NoCredentialsError:
        return CheckResult(name, Status.UNKNOWN, "No AWS credentials configured")
    except EndpointConnectionError:
        return CheckResult(name, Status.FAIL, "Could not reach AWS EC2 endpoint")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code == "InvalidInstanceID.NotFound":
            return CheckResult(
                name, Status.FAIL, f"Instance '{instance_id}' not found", {"code": code}
            )
        if code == "InvalidInstanceID.Malformed":
            return CheckResult(
                name, Status.UNKNOWN, f"Malformed instance id '{instance_id}'", {"code": code}
            )
        return CheckResult(name, Status.FAIL, f"Unexpected error: {code}", {"code": code})
    except ParamValidationError as exc:
        return CheckResult(name, Status.UNKNOWN, f"Invalid parameter: {exc}")
    except BotoCoreError as exc:
        return CheckResult(name, Status.UNKNOWN, f"AWS call failed: {type(exc).__name__}")


def check_http_endpoint(
    url: str,
    timeout_seconds: float = 5.0,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> CheckResult:
    """Confirm an HTTP endpoint responds with a 2xx status within timeout."""
    return with_retries(
        lambda: _check_http_endpoint_once(url, timeout_seconds),
        retries=retries,
        retry_delay=retry_delay,
    )


def _check_http_endpoint_once(url: str, timeout_seconds: float) -> CheckResult:
    name = f"http:{url}"
    try:
        resp = requests.get(url, timeout=timeout_seconds)
        detail = {
            "status_code": resp.status_code,
            "elapsed_ms": round(resp.elapsed.total_seconds() * 1000, 2),
        }
        if 200 <= resp.status_code < 300:
            return CheckResult(name, Status.OK, f"Responded {resp.status_code}", detail)
        return CheckResult(name, Status.FAIL, f"Responded {resp.status_code}", detail)
    except requests.exceptions.Timeout:
        return CheckResult(name, Status.FAIL, f"Timed out after {timeout_seconds}s")
    except requests.exceptions.TooManyRedirects:
        return CheckResult(name, Status.FAIL, "Too many redirects")
    except requests.exceptions.ConnectionError:
        return CheckResult(name, Status.FAIL, "Connection failed")
    except requests.exceptions.MissingSchema as exc:
        return CheckResult(name, Status.UNKNOWN, f"Invalid URL: {exc}")
    except requests.exceptions.RequestException as exc:
        return CheckResult(name, Status.UNKNOWN, f"Request failed: {type(exc).__name__}")
