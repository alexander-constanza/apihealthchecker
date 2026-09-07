# The tailscale binaries are copied out of the official image rather than
# installed from a package repository, which is the pattern Tailscale
# documents for containers. It pins the version to a tag and keeps the runtime
# image free of an apt source and its keyring.
FROM docker.io/tailscale/tailscale:stable AS tailscale

FROM python:3.12-slim

# Non-root from here on. A monitoring service makes outbound requests to
# arbitrary operator-supplied URLs, so it is exactly the kind of process that
# should not be running as root.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apihealthchecker ./apihealthchecker

# The SQLite file lives here. On Fly this path is a mounted volume (see
# fly.toml); without one the database is inside the container filesystem and is
# lost on every deploy.
ENV DB_PATH=/data/apihealthchecker.db \
    PORT=8080 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown appuser:appuser /data

# The tunnel. Inert unless TAILSCALE_AUTHKEY is set at runtime: with no key the
# entrypoint starts nothing and the image behaves exactly as it did before.
# TAILSCALE_BIN and TAILSCALE_SOCKET are read by the engine's tailscale check,
# which cannot assume the CLI is on PATH under its usual name or that the
# daemon put its socket in the privileged default location.
COPY --from=tailscale /usr/local/bin/tailscaled /app/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /app/tailscale
COPY entrypoint.sh /app/entrypoint.sh
# Explicit mode rather than whatever the build context happened to carry. COPY
# preserves the source file's permissions, so an entrypoint that is 0700 and
# owned by root is unreadable to appuser and the container dies on start with
# nothing but "permission denied". Setting it here makes the image independent
# of the checkout's file modes.
RUN chmod 0755 /app/entrypoint.sh /app/tailscale /app/tailscaled
ENV TAILSCALE_BIN=/app/tailscale \
    TAILSCALE_SOCKET=/tmp/tailscaled.sock

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8080') + '/health', timeout=4).status == 200 else 1)"

# The entrypoint brings up the tunnel if there is one to bring up and then
# execs gunicorn with the same flags this line used to carry directly.
#
# One worker, several threads. Deliberate: with a single process the scheduler
# lease has nothing to arbitrate, so exactly one scheduler runs. Threads keep a
# slow check from starving the status page, which is the failure mode the
# api-debugging-toolkit RUNBOOK documents in its section 7.
#
# The lease in scheduler.py means raising --workers is safe: the extra workers
# serve requests and decline the lease rather than each starting their own
# scheduler loop.
CMD ["/app/entrypoint.sh"]
