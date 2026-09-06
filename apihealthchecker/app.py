"""The Flask service: REST API, status page, and scheduler startup.

The request-lifecycle patterns here (request-id propagation, JSON access logs,
the HTTPException-vs-Exception handler split, the app factory) are ported from
github.com/alexander-constanza/api-debugging-toolkit. See the comments on each.
"""
import contextlib
import logging
import os
import re
import secrets
import time
import uuid

from flask import Flask, g, jsonify, render_template, request
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from apihealthchecker.auth import (
    api_token,
    request_is_authorized,
    request_needs_token,
    write_token_required,
)
from apihealthchecker.classifier import worst_severity
from apihealthchecker.db import (
    CheckResultRow,
    DailyRollup,
    Monitor,
    SessionLocal,
    _iso,
    check_db_connection,
    init_db,
)
from apihealthchecker.logging_config import configure_logging
from apihealthchecker.notifier import webhook_format, webhook_url
from apihealthchecker.rollup import uptime_for
from apihealthchecker.runner import run_single
from apihealthchecker.sandbox import (
    check_cooldown_remaining,
    claim_slot,
    create_lock,
    is_protected,
    max_per_day,
    record_manual_check,
    register_sandbox_monitor,
    sandbox_enabled,
    sandbox_entries_for,
    sandbox_requested,
    sandbox_view,
    visitor_may,
    visitor_rejection,
)
from apihealthchecker.scheduler import start_scheduler
from apihealthchecker.seed import seed_monitors
from apihealthchecker.validation import parse_limit, validate_monitor

logger = logging.getLogger("apihealthchecker")

# How many recent results the UI draws in each monitor's history strip.
SPARKLINE_POINTS = 30

MAX_BODY_BYTES = 64 * 1024

# SQLite row ids are signed 64-bit. A path id above that reaches the driver as
# a Python int it cannot bind and comes back as a 500. It is a 404.
MAX_ROW_ID = 2**63 - 1

# An inbound X-Request-Id is echoed on the response and written to the logs.
# Anything outside this set is replaced rather than passed through: a newline
# in a header value is a response-splitting attempt, or at best a crash.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,200}")


def create_app(start_background_scheduler: bool | None = None) -> Flask:
    """Application factory.

    Ported from the toolkit's create_app: importing this module has no side
    effects, so tests build an app without a scheduler thread and gunicorn gets
    a fresh one per worker.

    `start_background_scheduler` defaults to reading the environment. Tests pass
    False explicitly, which is why it is a parameter and not just an env lookup:
    a test suite that has to set an environment variable to avoid starting
    threads will eventually forget to.
    """
    configure_logging()
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    # The largest legitimate body is a monitor definition, a few hundred bytes.
    # Anything bigger is refused with 413 before it is read into memory or
    # handed to the JSON parser.
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    init_db()

    if os.environ.get("SEED_ON_START", "0") in ("1", "true", "True"):
        seed_monitors()

    if sandbox_requested() and api_token() is None:
        # Without a token every request is the operator, so there is nobody
        # for the sandbox rules to apply to. Say so rather than half-enable.
        logger.warning("sandbox_needs_token")

    _register_lifecycle(app)
    _register_api(app)
    _register_errors(app)
    _register_cli(app)

    if start_background_scheduler is None:
        start_background_scheduler = os.environ.get("RUN_SCHEDULER", "1") not in (
            "0",
            "false",
            "False",
        )
    if start_background_scheduler:
        # Every process gets a scheduler object. The one that wins the lease
        # runs checks; the others sit in standby and retry. /health reports
        # which this process is. See scheduler.py.
        app.extensions["scheduler"] = start_scheduler()

    return app


def _register_lifecycle(app: Flask) -> None:
    """Request-id propagation and JSON access logging.

    Ported from api-debugging-toolkit. The one addition: an inbound
    X-Request-Id is honoured rather than always minted fresh, so a request id
    survives a proxy hop and still ties a client report to these log lines.
    """

    @app.before_request
    def start_request():
        incoming = request.headers.get("X-Request-Id", "")
        g.request_id = incoming if REQUEST_ID_PATTERN.fullmatch(incoming) else str(uuid.uuid4())
        g.start_time = time.time()
        g.csp_nonce = secrets.token_urlsafe(16)
        logger.info(
            "request_started",
            extra={
                "request_id": g.request_id,
                "method": request.method,
                "path": request.path,
            },
        )

    @app.before_request
    def require_token_for_writes():
        """Refuse writes without the token, before any route runs. See auth.py.

        In sandbox mode a visitor may make a short allowlist of writes without
        it; the routes apply the sandbox rules to those. `g.operator` tells
        them which kind of caller this is.
        """
        g.operator = request_is_authorized()
        if not request_needs_token() or g.operator:
            return None
        if sandbox_enabled() and visitor_may(request.method, request.path):
            return None
        logger.warning(
            "request_unauthorized",
            extra={"request_id": g.request_id, "method": request.method, "path": request.path},
        )
        response = jsonify(
            {
                "error": "unauthorized",
                "message": "This endpoint requires Authorization: Bearer <API_TOKEN>",
            }
        )
        response.status_code = 401
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.after_request
    def log_response(response):
        duration_ms = round((time.time() - g.get("start_time", time.time())) * 1000, 2)
        logger.info(
            "request_completed",
            extra={
                "request_id": g.get("request_id", "unknown"),
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        response.headers["X-Request-Id"] = g.get("request_id", "unknown")
        # The page is inline CSS and JS by design (it must render offline), so
        # the policy allows exactly the inline blocks carrying this request's
        # nonce and nothing else: no other scripts, no framing, no forms to
        # other origins, and fetch only to this origin.
        nonce = g.get("csp_nonce", "")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.teardown_appcontext
    def remove_session(exception=None):
        del exception
        # scoped_session keeps a session per thread. Without this a long-lived
        # gunicorn thread would hold a stale one across requests and start
        # serving data from a transaction that began minutes ago.
        SessionLocal.remove()


def _json_body() -> tuple[dict | None, tuple[dict, int] | None]:
    """Parse a request body as a JSON object, or return an error tuple.

    silent=True so unparseable JSON is a 400 with an explanation rather than a
    werkzeug BadRequest that would surface as a bare error page.
    """
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        # silent=True swallows bad JSON but not a body nested deeper than the
        # parser's stack. A kilobyte of "[" is enough to get here.
        return None, (
            {"error": "invalid_json", "message": "Request body is nested too deeply"},
            400,
        )
    if data is None:
        return None, ({"error": "invalid_json", "message": "Request body must be valid JSON"}, 400)
    if not isinstance(data, dict):
        return None, (
            {"error": "invalid_json", "message": "Request body must be a JSON object"},
            400,
        )
    return data, None


def _latest_results(session, monitor_ids: list[int], per_monitor: int = SPARKLINE_POINTS) -> dict:
    """Newest `per_monitor` results for each monitor, newest first.

    One query for every monitor rather than one query per monitor: the status
    page renders every monitor at once, and a per-monitor query there is the
    classic N+1 that makes a dashboard slow exactly when it has most to show.
    """
    if not monitor_ids:
        return {}

    ranked = (
        select(
            CheckResultRow,
            func.row_number()
            .over(
                partition_by=CheckResultRow.monitor_id,
                order_by=CheckResultRow.checked_at.desc(),
            )
            .label("rank"),
        )
        .where(CheckResultRow.monitor_id.in_(monitor_ids))
        .subquery()
    )

    rows = session.execute(
        select(CheckResultRow)
        .join(ranked, ranked.c.id == CheckResultRow.id)
        .where(ranked.c.rank <= per_monitor)
        .order_by(CheckResultRow.monitor_id, CheckResultRow.checked_at.desc())
    ).scalars().all()

    grouped: dict[int, list] = {mid: [] for mid in monitor_ids}
    for row in rows:
        grouped.setdefault(row.monitor_id, []).append(row)
    return grouped


def _get_monitor(session, monitor_id: int):
    """A monitor by id, or None for a missing one or an id the database cannot hold."""
    if monitor_id > MAX_ROW_ID:
        return None
    return session.get(Monitor, monitor_id)


def _monitor_view(monitor: Monitor, results: list, sandbox_entry=None, uptime=None) -> dict:
    """A monitor plus its latest state and recent history, as the UI wants it."""
    latest = results[0] if results else None
    view = monitor.to_dict()
    view["sandbox"] = {"expires_at": _iso(sandbox_entry.expires_at)} if sandbox_entry else None
    view["uptime"] = uptime
    view["status"] = latest.status if latest else "pending"
    view["message"] = latest.message if latest else "No check recorded yet"
    view["category"] = latest.category if latest else None
    view["severity"] = latest.severity if latest else None
    view["latency_ms"] = latest.latency_ms if latest else None
    # Via to_dict so the UTC timezone is reattached; see db._iso.
    view["last_checked_at"] = latest.to_dict()["checked_at"] if latest else None
    # Oldest first so the sparkline reads left to right like a timeline.
    view["history"] = [
        {"status": r.status, "latency_ms": r.latency_ms, "checked_at": r.to_dict()["checked_at"]}
        for r in reversed(results)
    ]
    return view


def _register_api(app: Flask) -> None:
    @app.route("/", methods=["GET"])
    def index():
        return render_template("index.html", csp_nonce=g.csp_nonce)

    @app.route("/health", methods=["GET"])
    def health():
        """Liveness plus dependency reachability.

        Ported from the toolkit: a process that is up but cannot reach its
        database is still an outage from the caller's point of view, so this
        returns 503 rather than a cheerful 200.
        """
        db_ok = check_db_connection()
        scheduler = app.extensions.get("scheduler")
        return jsonify(
            {
                "status": "ok" if db_ok else "degraded",
                "dependencies": {"database": "ok" if db_ok else "unreachable"},
                "scheduler": {
                    "running_in_this_process": bool(scheduler and scheduler.owns_lease),
                    "owner": scheduler.owner if scheduler else None,
                },
                "alerting": {
                    "webhook_configured": webhook_url() is not None,
                    "format": webhook_format(),
                },
                "auth": {"write_token_required": write_token_required()},
                "sandbox": {"enabled": sandbox_enabled()},
            }
        ), (200 if db_ok else 503)

    @app.route("/api/monitors", methods=["GET"])
    def list_monitors():
        session = SessionLocal()
        monitors = session.execute(select(Monitor).order_by(Monitor.id)).scalars().all()
        ids = [m.id for m in monitors]
        grouped = _latest_results(session, ids)
        entries = sandbox_entries_for(session, ids) if sandbox_enabled() else {}
        uptimes = uptime_for(session, ids)
        return jsonify(
            {
                "monitors": [
                    _monitor_view(m, grouped.get(m.id, []), entries.get(m.id), uptimes.get(m.id))
                    for m in monitors
                ]
            }
        )

    @app.route("/api/monitors", methods=["POST"])
    def create_monitor():
        data, error = _json_body()
        if error is not None:
            body, status = error
            return jsonify(body), status

        cleaned, error = validate_monitor(data)
        if error is not None:
            body, status = error
            return jsonify(body), status

        session = SessionLocal()
        visitor = sandbox_enabled() and not g.get("operator", True)
        if visitor:
            rejection = visitor_rejection(cleaned)
            if rejection is not None:
                body, status = rejection
                return jsonify(body), status
        # Visitors take the lock so the cap check and the insert are one step.
        with create_lock if visitor else contextlib.nullcontext():
            if visitor and not claim_slot(session):
                return jsonify(
                    {
                        "error": "sandbox_limit",
                        "message": (
                            f"The sandbox accepts {max_per_day()} new monitors per day. "
                            "The count resets at midnight UTC."
                        ),
                    }
                ), 429
            try:
                monitor = Monitor(**cleaned)
                session.add(monitor)
                session.flush()
                entry = register_sandbox_monitor(session, monitor) if visitor else None
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                logger.exception(
                    "monitor_create_failed", extra={"request_id": g.get("request_id", "unknown")}
                )
                return jsonify({"error": "database_error"}), 500
        try:
            session.refresh(monitor)
            logger.info(
                "monitor_created",
                extra={
                    "monitor_id": monitor.id,
                    "sandbox": visitor,
                    "request_id": g.get("request_id", "unknown"),
                },
            )
            body = monitor.to_dict()
            body["sandbox"] = {"expires_at": _iso(entry.expires_at)} if entry else None
            return jsonify(body), 201
        except SQLAlchemyError:
            session.rollback()
            logger.exception(
                "monitor_create_failed", extra={"request_id": g.get("request_id", "unknown")}
            )
            return jsonify({"error": "database_error"}), 500

    @app.route("/api/monitors/<int:monitor_id>", methods=["DELETE"])
    def delete_monitor(monitor_id: int):
        session = SessionLocal()
        monitor = _get_monitor(session, monitor_id)
        if monitor is None:
            return jsonify(
                {"error": "not_found", "message": f"No monitor with id {monitor_id}"}
            ), 404
        if sandbox_enabled() and not g.get("operator", True) and is_protected(session, monitor_id):
            return jsonify(
                {
                    "error": "protected",
                    "message": "This monitor is part of the demo. In sandbox mode visitors "
                    "can only delete monitors that visitors added.",
                }
            ), 403
        try:
            session.delete(monitor)
            session.commit()
            logger.info("monitor_deleted", extra={"monitor_id": monitor_id})
            return jsonify({"deleted": monitor_id}), 200
        except SQLAlchemyError:
            session.rollback()
            logger.exception("monitor_delete_failed", extra={"monitor_id": monitor_id})
            return jsonify({"error": "database_error"}), 500

    @app.route("/api/monitors/<int:monitor_id>/check", methods=["POST"])
    def check_monitor(monitor_id: int):
        session = SessionLocal()
        monitor = _get_monitor(session, monitor_id)
        if monitor is None:
            return jsonify(
                {"error": "not_found", "message": f"No monitor with id {monitor_id}"}
            ), 404
        if sandbox_enabled() and not g.get("operator", True):
            wait = check_cooldown_remaining(monitor_id)
            if wait:
                response = jsonify(
                    {
                        "error": "cooldown",
                        "message": f"This monitor was checked recently. Try again in {wait}s.",
                        "retry_after_seconds": wait,
                    }
                )
                response.status_code = 429
                response.headers["Retry-After"] = str(wait)
                return response
            record_manual_check(monitor_id)
        try:
            row = run_single(monitor, session=session)
        except SQLAlchemyError:
            logger.exception("check_persist_failed", extra={"monitor_id": monitor_id})
            return jsonify({"error": "database_error"}), 500
        if row is None:
            return jsonify({"error": "check_failed", "message": "Check produced no result"}), 500
        return jsonify(row.to_dict()), 201

    @app.route("/api/monitors/<int:monitor_id>/rollups", methods=["GET"])
    def monitor_rollups(monitor_id: int):
        """One row per day, newest first, kept after the results are pruned."""
        session = SessionLocal()
        monitor = _get_monitor(session, monitor_id)
        if monitor is None:
            return jsonify(
                {"error": "not_found", "message": f"No monitor with id {monitor_id}"}
            ), 404
        rows = session.execute(
            select(DailyRollup)
            .where(DailyRollup.monitor_id == monitor_id)
            .order_by(DailyRollup.day.desc())
        ).scalars().all()
        return jsonify(
            {
                "monitor": monitor.to_dict(),
                "uptime": uptime_for(session, [monitor_id]).get(monitor_id),
                "days": [r.to_dict() for r in rows],
            }
        )

    @app.route("/api/monitors/<int:monitor_id>/history", methods=["GET"])
    def monitor_history(monitor_id: int):
        limit, message = parse_limit(request.args.get("limit"))
        if limit is None:
            return jsonify(
                {"error": "invalid_parameter", "parameter": "limit", "message": message}
            ), 400

        session = SessionLocal()
        monitor = _get_monitor(session, monitor_id)
        if monitor is None:
            return jsonify(
                {"error": "not_found", "message": f"No monitor with id {monitor_id}"}
            ), 404

        rows = session.execute(
            select(CheckResultRow)
            .where(CheckResultRow.monitor_id == monitor_id)
            .order_by(CheckResultRow.checked_at.desc(), CheckResultRow.id.desc())
            .limit(limit)
        ).scalars().all()

        return jsonify(
            {
                "monitor": monitor.to_dict(),
                "count": len(rows),
                "results": [r.to_dict() for r in rows],
            }
        )

    @app.route("/api/status", methods=["GET"])
    def status_rollup():
        """Overall rollup: counts by status, worst severity, last check time.

        "unknown" is counted separately from "fail" and never folded into it.
        The engine's three-state Status distinguishes "this is broken" from "I
        could not determine this", and collapsing them here would throw away
        the one signal that tells an operator whether to fix the service or fix
        the monitor.
        """
        session = SessionLocal()
        monitors = session.execute(select(Monitor)).scalars().all()
        grouped = _latest_results(session, [m.id for m in monitors], per_monitor=1)

        counts = {"ok": 0, "fail": 0, "unknown": 0, "pending": 0}
        severities = []
        last_checked = None

        for monitor in monitors:
            results = grouped.get(monitor.id, [])
            if not results:
                counts["pending"] += 1
                continue
            latest = results[0]
            counts[latest.status] = counts.get(latest.status, 0) + 1
            if latest.severity:
                severities.append(latest.severity)
            stamp = latest.to_dict()["checked_at"]
            if last_checked is None or (stamp or "") > last_checked:
                last_checked = stamp

        if counts["fail"]:
            overall = "fail"
        elif counts["unknown"]:
            overall = "unknown"
        elif counts["ok"]:
            overall = "ok"
        else:
            overall = "pending"

        return jsonify(
            {
                "overall_status": overall,
                "monitor_count": len(monitors),
                "counts": counts,
                "worst_severity": worst_severity(severities),
                "last_check_at": last_checked,
                "sandbox": sandbox_view(session),
            }
        )


def _register_errors(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def handle_http_exception(err: HTTPException):
        """Client-side errors answered as what they actually are.

        Ported from api-debugging-toolkit, and the single most important handler
        in the file. Without it the catch-all below swallows every werkzeug
        HTTPException and turns a routine 404 into a 500: the client is told the
        server broke, and the service's own error-rate metric climbs on what was
        really a typo'd URL. In a monitoring tool that is doubly bad, because
        the thing watching this service would alert on it.
        """
        return jsonify(
            {
                "error": (err.name or "error").lower().replace(" ", "_"),
                "message": err.description,
                "request_id": g.get("request_id", "unknown"),
            }
        ), (err.code or 500)

    @app.errorhandler(Exception)
    def handle_uncaught(err: Exception):
        del err  # logged via exc_info; Flask requires the parameter
        logger.exception(
            "unhandled_exception", extra={"request_id": g.get("request_id", "unknown")}
        )
        return jsonify(
            {"error": "internal_server_error", "request_id": g.get("request_id", "unknown")}
        ), 500


def _register_cli(app: Flask) -> None:
    @app.cli.command("seed")
    def seed_command():
        """Insert the demo monitors. Idempotent, safe to run more than once."""
        added = seed_monitors()
        print(f"Seeded {added} new monitor(s).")

    @app.cli.command("check-now")
    def check_now_command():
        """Run every enabled monitor once, right now, and record the results."""
        from apihealthchecker.scheduler import run_due_checks

        rows = run_due_checks()
        print(f"Recorded {len(rows)} result(s).")


if __name__ == "__main__":
    create_app().run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
