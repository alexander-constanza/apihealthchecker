"""apihealthchecker: a monitoring service composed from three portfolio repos.

The check engine is vendored from infra-health-check, the service patterns
(structured logging, request ids, error-handler split, app factory) are ported
from api-debugging-toolkit, and the failure classifier is ported from
ticket-triage-assistant with an infrastructure vocabulary. See README.md for the
composition diagram and the provenance of each piece.
"""
__version__ = "0.1.0"
