"""Write protection: one shared token, checked on every request that changes state.

The README said "put it behind an auth proxy before exposing it anywhere that
matters". This is the smaller thing that makes the demo itself safe to leave
on the internet: set API_TOKEN and every POST and DELETE under /api needs
`Authorization: Bearer <token>`. Reads stay open, because a status page that
needs a login is not a status page.

Design choices, and why:

- **One token, not accounts.** Two people who can both add monitors do not
  need to be distinguishable from each other on a monitoring tool this size.
  Accounts, roles and sessions are the auth proxy's job, and the moment they
  are needed this token is the wrong tool.
- **Checked in one place, before routing.** A `before_request` hook that looks
  at the method and the path, rather than a decorator on each write route.
  A decorator is forgotten on the next route; the hook fails closed for any
  write endpoint that does not exist yet.
- **Unset means open.** A fresh clone must run with no configuration, and a
  developer's local instance has nothing worth protecting. The deployed demo
  sets the token as a Fly secret. `/health` reports which mode is active.
- **Constant-time comparison.** `hmac.compare_digest`, so a wrong token takes
  as long to reject as a nearly right one.

The status page keeps the token in the browser's localStorage after asking
for it once, which is fine for an operator's own browser and would not be
fine for a shared kiosk. That trade-off is in the README.
"""
import hmac
import os

from flask import request

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def api_token() -> str | None:
    """API_TOKEN from the environment, or None when writes are open."""
    token = os.environ.get("API_TOKEN", "").strip()
    return token or None


def write_token_required() -> bool:
    return api_token() is not None


def request_needs_token() -> bool:
    """Whether the current request is a write to the API."""
    return request.method in WRITE_METHODS and request.path.startswith("/api/")


def presented_token() -> str | None:
    """The bearer token on the current request, or None if there is not one."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def request_is_authorized() -> bool:
    """True if writes are open, or the request carries the right token."""
    expected = api_token()
    if expected is None:
        return True
    presented = presented_token()
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())
