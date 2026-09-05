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

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8080') + '/health', timeout=4).status == 200 else 1)"

# One worker, several threads. Deliberate: with a single process the scheduler
# lease has nothing to arbitrate, so exactly one scheduler runs. Threads keep a
# slow check from starving the status page, which is the failure mode the
# api-debugging-toolkit RUNBOOK documents in its section 7.
#
# The lease in scheduler.py means raising --workers is safe: the extra workers
# serve requests and decline the lease rather than each starting their own
# scheduler loop.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 60 --access-logfile - 'apihealthchecker.app:create_app()'"]
