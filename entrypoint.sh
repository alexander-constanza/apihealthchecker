#!/bin/sh
# Container entrypoint.
#
# Starts tailscaled before the app when, and only when, TAILSCALE_AUTHKEY is
# present. With no key the container behaves exactly as it did before the
# tunnel existed, which is what keeps a plain `docker run` of this image
# working for anyone who has never heard of a tailnet.
#
# Userspace networking, not a tun device: Fly machines and this container's
# non-root user both make /dev/net/tun unavailable, and userspace mode needs
# neither it nor NET_ADMIN. The cost is that the app's own sockets do not see
# the tailnet, which is why tailscaled also opens an HTTP proxy: only the
# monitors whose target is a tailnet address are sent through it (see
# tailnet_proxy_for in runner.py), so a dead tunnel cannot take the public
# checks down with it.
#
# State is kept in memory rather than on the volume. The auth key should be an
# ephemeral, pre-approved, tagged one, which means the node registers itself on
# boot and removes itself on shutdown. A monitoring service that redeploys
# often would otherwise leave a trail of dead machines in the admin console,
# and every one of them would match the ACL rules meant for the live one.
set -e

TAILSCALE_SOCKET="${TAILSCALE_SOCKET:-/tmp/tailscaled.sock}"
export TAILSCALE_SOCKET

start_tailscale() {
  /app/tailscaled \
    --tun=userspace-networking \
    --state=mem: \
    --socket="${TAILSCALE_SOCKET}" \
    --outbound-http-proxy-listen=localhost:1055 &

  # tailscale up fails outright if the daemon is not listening yet, and the
  # daemon takes a moment. Poll rather than sleep a guessed number of seconds.
  i=0
  while [ ! -S "${TAILSCALE_SOCKET}" ]; do
    i=$((i + 1))
    if [ "$i" -gt 100 ]; then
      echo "tailscale: daemon socket never appeared at ${TAILSCALE_SOCKET}" >&2
      return 1
    fi
    sleep 0.1
  done

  /app/tailscale --socket="${TAILSCALE_SOCKET}" up \
    --auth-key="${TAILSCALE_AUTHKEY}" \
    --hostname="${TAILSCALE_HOSTNAME:-apihealthchecker}" \
    --accept-routes
}

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  if start_tailscale; then
    export TAILNET_HTTP_PROXY="${TAILNET_HTTP_PROXY:-http://localhost:1055}"
    echo "tailscale: joined as ${TAILSCALE_HOSTNAME:-apihealthchecker}"
    # Print the state into the deploy logs. Without this, a node that joined and
    # a node that silently did not look identical from outside, and the only way
    # to tell them apart is to shell into a machine that may already be gone.
    /app/tailscale --socket="${TAILSCALE_SOCKET}" status --peers=false || true
  else
    # Deliberately not fatal. A monitoring service that refuses to start
    # because its VPN is unhappy has turned one degraded check into a total
    # outage of the thing that tells you about outages. The tailnet monitors
    # will report UNKNOWN, which is the honest signal and is already
    # distinguished from FAIL everywhere downstream.
    unset TAILNET_HTTP_PROXY
    echo "tailscale: could not join the tailnet, continuing without it" >&2
  fi
else
  unset TAILNET_HTTP_PROXY
fi

exec gunicorn \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 1 \
  --threads 8 \
  --timeout 60 \
  --access-logfile - \
  'apihealthchecker.app:create_app()'
