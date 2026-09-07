# VENDORED CODE, DO NOT EDIT LIGHTLY.
#
# Source: github.com/alexander-constanza/infra-health-check
# Path in source repo: infra_health/tailscale.py
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

"""Path-quality checks for a Tailscale tailnet.

Every other check in this package answers "is the resource there". This one
answers "how is the traffic getting there", which for a mesh VPN is a
different question with a different failure mode. Two nodes can be fully
connected, pass every reachability test an HTTP check can make, and still be
routing every packet through a relay on another continent because a NAT
between them will not cooperate. Nothing is down. Everything is slow, and no
endpoint check will ever say so.

Tailscale calls the relay path DERP. A direct path is peer to peer over UDP;
a relayed path goes out to a DERP server and back, adding that server's round
trip to every packet in both directions. The distinction is visible in
`tailscale status --json` as the peer's CurAddr field: a non-empty CurAddr is
the address of a live direct path, and an empty one means traffic is going
through the DERP region named in Relay.

The three states map onto this tool's existing Status the way they do
everywhere else:

- OK       a direct path exists, or a relayed one when require_direct=False
- FAIL     the peer is absent from the tailnet, offline, or relayed when the
           caller asked for direct
- UNKNOWN  the CLI is missing, the daemon is not running, or no session has
           been established yet, which is not the same statement as "the path
           is bad"

Nothing here parses human-readable CLI output for its answer. The verdict
comes from the JSON status document; `tailscale ping` is used only to give
the path something to carry, and `tailscale netcheck` only to explain a
verdict that has already been reached.
"""
import json
import os
import re
import subprocess
from datetime import datetime, timezone

from apihealthchecker.engine.checks import CheckResult, Status, with_retries

DEFAULT_TIMEOUT_SECONDS = 10.0

# How many pings to send when warming a path. `tailscale ping` stops early as
# soon as the connection becomes direct, so this is an upper bound on patience
# rather than a fixed cost.
DEFAULT_WARM_COUNT = 5

# With no traffic at all, a peer's path information is whatever was true when
# it last spoke. Older than this and it describes history, not the connection.
DEFAULT_STALE_AFTER_SECONDS = 300.0

# Go marshals a zero time.Time as this. Tailscale uses it for "this peer has
# never completed a handshake", which is what an idle peer looks like.
ZERO_TIME = "0001-01-01T00:00:00Z"

# Go marshals time.Duration as an integer count of nanoseconds, which is what
# netcheck's RegionLatency map holds.
_NS_PER_MS = 1_000_000

# Go emits RFC 3339 with up to nine fractional digits. datetime.fromisoformat
# accepts three or six and rejects everything else, so the fraction is
# normalised before parsing rather than after failing.
_FRACTION = re.compile(r"\.(\d{1,9})")


def _binary() -> str:
    """The tailscale CLI to invoke.

    Overridable because the binary is not always on PATH as `tailscale`: a
    container that copies it out of the official image puts it wherever the
    Dockerfile says, and that path is a deployment detail this module should
    not know.
    """
    return os.environ.get("TAILSCALE_BIN") or "tailscale"


def _argv(args: list[str]) -> list[str]:
    """Build the full argv, including a socket override when one is set.

    tailscaled running unprivileged cannot create its socket in the default
    location, so a userspace deployment passes --socket to both halves. The
    CLI needs the same value the daemon was started with.
    """
    argv = [_binary()]
    socket_path = os.environ.get("TAILSCALE_SOCKET")
    if socket_path:
        argv.append(f"--socket={socket_path}")
    argv.extend(args)
    return argv


def _run(args: list[str], timeout_seconds: float) -> tuple[object | None, str | None]:
    """Run a tailscale subcommand. Returns (completed_process, error_message).

    Never raises. Every way the CLI itself can fail comes back as a message,
    because none of them are statements about the peer: a missing binary is a
    packaging problem and a hung daemon is a local problem, and reporting
    either as "the connection is bad" would send an operator to the wrong end
    of the link.
    """
    argv = _argv(args)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return None, (
            f"tailscale CLI not found (looked for '{_binary()}'; "
            "set TAILSCALE_BIN to override)"
        )
    except PermissionError:
        return None, f"Not permitted to execute '{_binary()}'"
    except subprocess.TimeoutExpired:
        return None, f"'tailscale {args[0]}' timed out after {timeout_seconds}s"
    except OSError as exc:
        return None, f"Could not run tailscale: {type(exc).__name__}"
    return completed, None


def _first_line(completed) -> str:
    text = (getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "").strip()
    lines = text.splitlines()
    return lines[0] if lines else f"exit {completed.returncode}"


def load_status(timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[dict | None, str | None]:
    """Read and parse `tailscale status --json`.

    Returns (status, None) or (None, reason). Exposed rather than private so a
    caller wanting several checks against one tailnet can read the document
    once instead of forking a process per peer.
    """
    completed, error = _run(["status", "--json"], timeout_seconds)
    if error is not None:
        return None, error
    if completed.returncode != 0:
        return None, f"tailscale status failed: {_first_line(completed)}"
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return None, f"Could not parse tailscale status JSON: {exc.msg}"
    if not isinstance(status, dict):
        return None, f"tailscale status returned {type(status).__name__}, expected an object"
    return status, None


def _peers(status: dict) -> list[dict]:
    return [node for node in (status.get("Peer") or {}).values() if isinstance(node, dict)]


def find_peer(status: dict, peer: str) -> dict | None:
    """Match a peer by hostname, MagicDNS name, first DNS label or Tailscale IP.

    All four are things an operator reasonably calls "the node", and which one
    they reach for depends on whether they last looked at the admin console, a
    config file or a ping. Matching all four keeps the check from failing for
    the uninteresting reason that the name was spelled the other correct way.
    """
    wanted = peer.strip().rstrip(".").lower()
    if not wanted:
        return None
    for node in _peers(status):
        hostname = (node.get("HostName") or "").lower()
        dns_name = (node.get("DNSName") or "").rstrip(".").lower()
        first_label = dns_name.split(".")[0] if dns_name else ""
        addresses = {str(ip).lower() for ip in (node.get("TailscaleIPs") or [])}
        if wanted in addresses or wanted in {hostname, dns_name, first_label} - {""}:
            return node
    return None


def _normalize_timestamp(raw: str) -> str:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)


def _handshake_age_seconds(node: dict) -> float | None:
    """Seconds since the last WireGuard handshake, or None if there never was one.

    None is the load-bearing case. A handshake happens over a relayed path as
    readily as a direct one, so its absence means no session exists at all,
    which is the difference between "this connection is relayed" and "this
    connection has not happened yet".
    """
    raw = node.get("LastHandshake")
    if not isinstance(raw, str) or not raw or raw == ZERO_TIME:
        return None
    try:
        stamp = datetime.fromisoformat(_normalize_timestamp(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - stamp).total_seconds(), 1)


def _interpret_netcheck(udp, varies, port_mapping: dict) -> str:
    """Name the most likely reason a direct path is unavailable from this host.

    A reading of the flags above it, not something Tailscale reports. It is
    here because the raw flags tell an operator nothing they can act on, and
    the question after "why is this relayed" is always "whose NAT, and can I
    do anything about it".
    """
    if udp is False:
        return (
            "UDP is blocked on this network. No direct path is possible from here, "
            "so every connection will relay through DERP until that changes."
        )
    if varies is True:
        return (
            "Hard NAT on this side: the external port varies by destination, so an "
            "address discovered for one peer does not work for another. A direct path "
            "needs the far side to be easier to reach, or a port mapping here."
        )
    if port_mapping and all(value is False for value in port_mapping.values()):
        return (
            "No port-mapping protocol (UPnP, NAT-PMP or PCP) is available, so a direct "
            "path depends entirely on endpoint discovery succeeding through both NATs."
        )
    return (
        "No single blocker identified on this side. The path may still upgrade to direct "
        "once more traffic has flowed, or the far side may be the constraint."
    )


def _summarize_netcheck(report: dict) -> dict:
    udp = report.get("UDP")
    varies = report.get("MappingVariesByDestIP")
    preferred = report.get("PreferredDERP")
    port_mapping = {key.lower(): report.get(key) for key in ("UPnP", "PMP", "PCP")}

    latency_ms = None
    latencies = report.get("RegionLatency")
    if isinstance(latencies, dict) and preferred is not None:
        raw = latencies.get(str(preferred), latencies.get(preferred))
        if isinstance(raw, int) and not isinstance(raw, bool):
            latency_ms = round(raw / _NS_PER_MS, 2)

    return {
        "available": True,
        "udp": udp,
        "ipv4": report.get("IPv4"),
        "ipv6": report.get("IPv6"),
        "mapping_varies_by_dest_ip": varies,
        "port_mapping": port_mapping,
        "preferred_derp": preferred,
        "preferred_derp_latency_ms": latency_ms,
        "interpretation": _interpret_netcheck(udp, varies, port_mapping),
    }


def diagnose_path(timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run `tailscale netcheck` and summarise why a direct path may be unavailable.

    A failed netcheck is reported inside the diagnosis rather than raised: the
    verdict on the path has already been reached by this point, and losing it
    because the explanation could not be gathered would be the wrong trade.
    """
    completed, error = _run(["netcheck", "--format=json"], timeout_seconds)
    if error is not None:
        return {"available": False, "reason": error}
    if completed.returncode != 0:
        return {"available": False, "reason": f"netcheck failed: {_first_line(completed)}"}
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"available": False, "reason": f"Could not parse netcheck JSON: {exc.msg}"}
    if not isinstance(report, dict):
        return {"available": False, "reason": "netcheck did not return an object"}
    return _summarize_netcheck(report)


def _warm_path(peer: str, timeout_seconds: float) -> str | None:
    """Send a few pings so there is a live path to classify.

    `tailscale ping` stops as soon as the connection becomes direct, which is
    exactly the behaviour wanted: it gives NAT traversal the couple of seconds
    it needs instead of judging the link on its relayed first packet, which is
    how a healthy path gets reported as a bad one.
    """
    completed, error = _run(["ping", "-c", str(DEFAULT_WARM_COUNT), peer], timeout_seconds)
    if error is not None:
        return error
    output = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()
    return output[-1] if output else None


def _peer_detail(node: dict, status: dict) -> dict:
    return {
        "peer": node.get("HostName"),
        "dns_name": (node.get("DNSName") or "").rstrip("."),
        "tailscale_ips": node.get("TailscaleIPs") or [],
        "os": node.get("OS"),
        "tags": node.get("Tags") or [],
        "relay_region": node.get("Relay") or None,
        "last_handshake_age_seconds": _handshake_age_seconds(node),
        "rx_bytes": node.get("RxBytes"),
        "tx_bytes": node.get("TxBytes"),
        "from": (status.get("Self") or {}).get("HostName"),
        "tailnet": status.get("MagicDNSSuffix"),
    }


def check_tailscale_path(
    peer: str,
    require_direct: bool = True,
    warm: bool = True,
    diagnose: bool = True,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> CheckResult:
    """Confirm traffic to a tailnet peer takes a direct path rather than a relay.

    Retries earn their place here for a reason the other checks do not share.
    A path that starts relayed frequently becomes direct on its own once NAT
    traversal completes, so asking again a second later is a genuinely
    different measurement rather than the same question repeated.
    """
    return with_retries(
        lambda: _check_tailscale_path_once(
            peer, require_direct, warm, diagnose, timeout_seconds, stale_after_seconds
        ),
        retries=retries,
        retry_delay=retry_delay,
    )


def _check_tailscale_path_once(
    peer: str,
    require_direct: bool,
    warm: bool,
    diagnose: bool,
    timeout_seconds: float,
    stale_after_seconds: float,
) -> CheckResult:
    name = f"tailscale:{peer}"

    status, error = load_status(timeout_seconds)
    if error is not None:
        return CheckResult(name, Status.UNKNOWN, error)

    backend = status.get("BackendState")
    if backend != "Running":
        return CheckResult(
            name,
            Status.UNKNOWN,
            f"tailscaled is not running (BackendState '{backend or 'unknown'}')",
            {"backend_state": backend},
        )

    node = find_peer(status, peer)
    if node is None:
        known = sorted(n.get("HostName") or "" for n in _peers(status))
        return CheckResult(
            name,
            Status.FAIL,
            f"Peer '{peer}' is not in this tailnet",
            {"known_peers": [k for k in known if k], "tailnet": status.get("MagicDNSSuffix")},
        )

    if not node.get("Online", False):
        return CheckResult(
            name, Status.FAIL, f"Peer '{peer}' is offline", _peer_detail(node, status)
        )

    ping_line = None
    if warm:
        ping_line = _warm_path(peer, timeout_seconds)
        status, error = load_status(timeout_seconds)
        if error is not None:
            return CheckResult(name, Status.UNKNOWN, error)
        node = find_peer(status, peer) or node

    detail = _peer_detail(node, status)
    if ping_line:
        detail["ping"] = ping_line

    cur_addr = (node.get("CurAddr") or "").strip()
    relay = (node.get("Relay") or "").strip()
    handshake_age = detail["last_handshake_age_seconds"]

    if cur_addr:
        detail["path"] = "direct"
        detail["via"] = cur_addr
        return CheckResult(name, Status.OK, f"Direct path to '{peer}' via {cur_addr}", detail)

    if handshake_age is None:
        detail["path"] = "idle"
        return CheckResult(
            name,
            Status.UNKNOWN,
            f"No session with '{peer}' yet, so there is no path to classify",
            detail,
        )

    if handshake_age > stale_after_seconds:
        detail["path"] = "stale"
        return CheckResult(
            name,
            Status.UNKNOWN,
            f"Last handshake with '{peer}' was {handshake_age}s ago; "
            "the path information describes an old session",
            detail,
        )

    detail["path"] = "relay"
    detail["via"] = f"DERP({relay})" if relay else "DERP"
    if diagnose:
        detail["diagnosis"] = diagnose_path(timeout_seconds)

    where = f" via DERP({relay})" if relay else " via a DERP relay"
    if not require_direct:
        return CheckResult(name, Status.OK, f"Relayed path to '{peer}'{where}", detail)
    return CheckResult(
        name, Status.FAIL, f"Path to '{peer}' is relayed{where}, not direct", detail
    )
