# PORTED CODE.
#
# Source: github.com/alexander-constanza/ticket-triage-assistant
# Path in source repo: app/classifier.py
#
# Why ported rather than imported: ticket-triage-assistant is a portfolio CLI,
# not a package on PyPI, so it cannot be a requirements.txt entry. The scoring
# approach is copied here and the vocabulary is replaced.
#
# What was kept from the original:
#   - hit counting rather than first-match, so several weak signals lose to one
#     strong one instead of whichever keyword happens to be first in the dict
#   - most-severe-wins for severity, most-frequent-wins for category, because
#     the two dimensions answer different questions
#   - confidence as the winning label's share of total hits, describing the
#     category only
#   - the auditable `classifier_used` field, so a stored result always records
#     which classifier produced it
#
# What was replaced: the keyword maps. The original speaks support-ticket
# language (invoice, refund, password reset). This one speaks infrastructure
# failure language (timeout, connection refused, DNS, 5xx, TLS), matched against
# the message and detail of a CheckResult rather than a customer's prose.
#
# What was dropped: the LLM and fallback classifiers. Classifying a check
# failure is a closed vocabulary problem over machine-generated strings, so the
# rules are exhaustive and a model would add a network call, a key and a
# non-deterministic answer for no gain.
"""Classification of failed checks into a category and a severity.

A raw FAIL tells an operator that something is wrong but not what kind of wrong,
and the difference matters: a DNS failure and a 404 are both failures, but one
is an outage and the other is usually a stale URL in the monitor config. This
turns the engine's message and detail into "critical / connectivity" so the
first triage decision can be made from the status page alone.
"""
import re
from dataclasses import dataclass

CATEGORIES = (
    "connectivity",
    "dns",
    "tls",
    "server_error",
    "client_error",
    "rate_limited",
    "authorization",
    "not_found",
    "timeout",
    "configuration",
    "unclassified",
)

# Ordered most severe first. _match_severity relies on this ordering.
SEVERITIES = ("critical", "high", "medium", "low")

_CATEGORY_KEYWORDS = {
    "timeout": ["timed out", "timeout", "timed-out", "deadline exceeded", "read timeout",
                "connect timeout"],
    "dns": ["dns", "name resolution", "nodename nor servname", "name or service not known",
            "getaddrinfo", "could not resolve", "unknown host", "nxdomain"],
    "connectivity": ["connection failed", "connection refused", "connection reset",
                     "connection aborted", "could not reach", "unreachable",
                     "network is unreachable", "no route to host", "connection error",
                     "too many redirects"],
    "tls": ["tls", "ssl", "certificate", "cert verify", "certificate verify failed",
            "handshake", "self signed", "self-signed", "hostname mismatch",
            "expired certificate"],
    "server_error": ["responded 500", "responded 502", "responded 503", "responded 504",
                     "internal server error", "bad gateway", "service unavailable",
                     "gateway timeout", "5xx"],
    # No "4xx" here, unlike server_error. Every 5xx is the same kind of problem
    # (the server broke), so the generic token adds signal. The 4xx range is the
    # opposite: 401, 404 and 429 have their own categories and their own
    # severities, and a generic token would tie with each of them on every
    # match. client_error is the residual bucket for the 4xx codes that have no
    # more specific home, so it only ever matches an explicit code.
    "client_error": ["responded 400", "responded 405", "responded 409", "responded 422",
                     "bad request", "method not allowed", "unprocessable"],
    "rate_limited": ["responded 429", "rate limit", "rate limited", "too many requests",
                     "throttled", "throttling", "slow down", "quota exceeded"],
    "authorization": ["responded 401", "responded 403", "access denied", "accessdenied",
                      "unauthorized", "forbidden", "no credentials", "invalid credentials",
                      "not authorized", "permission denied"],
    "not_found": ["responded 404", "not found", "nosuchbucket", "does not exist",
                  "invalidinstanceid.notfound"],
    "configuration": ["invalid url", "malformed", "invalid parameter", "missing required field",
                      "unknown check type", "must be a mapping", "different region",
                      "invalid instance id"],
}

_SEVERITY_KEYWORDS = {
    "critical": ["connection failed", "connection refused", "could not reach", "unreachable",
                 "dns", "name resolution", "getaddrinfo", "could not resolve", "nxdomain",
                 "responded 500", "responded 502", "responded 503", "responded 504",
                 "internal server error", "bad gateway", "service unavailable",
                 "gateway timeout", "no route to host", "outage", "down"],
    "high": ["timed out", "timeout", "certificate", "ssl", "tls", "handshake",
             "certificate verify failed", "responded 401", "responded 403", "access denied",
             "unauthorized", "forbidden", "no credentials", "connection reset",
             "connection aborted"],
    "medium": ["responded 429", "rate limit", "rate limited", "too many requests", "throttled",
               "responded 404", "not found", "does not exist", "too many redirects",
               "responded 400", "bad request", "responded 409"],
    "low": ["responded 405", "method not allowed", "responded 422", "unprocessable",
            "invalid url", "malformed", "invalid parameter", "missing required field",
            "different region"],
}

# Sentence terminators. Kept from the original: matching only ever looks back to
# the start of the current sentence so a negation cannot reach across one.
_SENTENCE_SPLIT = re.compile(r"[.!?;\n]")

_NEGATION_PATTERN = re.compile(r"\b(not|no|n't|never)\s+(\w+\s+){0,3}$")


@dataclass
class Classification:
    category: str
    severity: str
    classifier_used: str
    confidence: float | None = None

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "classifier_used": self.classifier_used,
            "confidence": self.confidence,
        }


class RulesBasedClassifier:
    """Deterministic keyword matcher over a check's message and detail.

    Category picks the most-hit label, so a message carrying several
    connectivity words outranks one passing mention of a status code. Severity
    picks the MOST SEVERE matching label rather than the most frequent one: one
    DNS failure outranks three mentions of a 404. Falls back to
    'unclassified' / 'low' when nothing matches, which is honest about having
    no evidence rather than guessing.
    """

    name = "rules"

    def classify(self, message: str, detail: dict | None = None) -> Classification:
        text = _build_text(message, detail)

        category, confidence = self._match(text, _CATEGORY_KEYWORDS)
        severity, _ = self._match_severity(text)

        # confidence describes the category only. The two dimensions have
        # independent evidence, so a single blended number would misreport a
        # category matched unambiguously just because no severity word appeared.
        if category is None:
            category, confidence = "unclassified", 0.0
        if severity is None:
            severity = "low"

        return Classification(
            category=category,
            severity=severity,
            classifier_used=self.name,
            confidence=round(confidence, 4),
        )

    @classmethod
    def _hit(cls, text: str, kw: str) -> bool:
        """True if kw appears in text as whole words and is not negated."""
        pattern = re.compile(r"\b" + re.escape(kw) + r"\b")
        for m in pattern.finditer(text):
            preceding = text[: m.start()]
            boundaries = list(_SENTENCE_SPLIT.finditer(preceding))
            if boundaries:
                preceding = preceding[boundaries[-1].end():]
            if _NEGATION_PATTERN.search(preceding):
                continue
            return True
        return False

    @classmethod
    def _scores(cls, text: str, keyword_map: dict) -> dict[str, int]:
        scores = {}
        for label, keywords in keyword_map.items():
            hits = sum(1 for kw in keywords if cls._hit(text, kw))
            if hits:
                scores[label] = hits
        return scores

    @classmethod
    def _match(cls, text: str, keyword_map: dict) -> tuple[str | None, float]:
        """Most-frequently-hit label, with its share of total hits as confidence."""
        scores = cls._scores(text, keyword_map)
        if not scores:
            return None, 0.0
        best = max(scores, key=scores.get)
        return best, scores[best] / sum(scores.values())

    @classmethod
    def _match_severity(cls, text: str) -> tuple[str | None, float]:
        """Most SEVERE matching label, not the most frequent one."""
        scores = cls._scores(text, _SEVERITY_KEYWORDS)
        if not scores:
            return None, 0.0
        best = min(scores, key=SEVERITIES.index)
        return best, scores[best] / sum(scores.values())


def _build_text(message: str, detail: dict | None) -> str:
    """Flatten a check's message and detail into one lowercase string.

    The detail dict carries the status code as an int (`{"status_code": 503}`),
    which no keyword would ever match on its own. Rendering it as "responded
    503" puts it in the same vocabulary the message uses, so a status code is
    classified whether the engine spelled it out or not.
    """
    parts = [str(message or "")]
    if isinstance(detail, dict):
        code = detail.get("status_code")
        if isinstance(code, int) and not isinstance(code, bool):
            parts.append(f"responded {code}")
            parts.append(f"{code // 100}xx")
        for key, value in detail.items():
            if key == "status_code":
                continue
            parts.append(f"{key} {value}")
    return " ".join(parts).lower()


_DEFAULT = RulesBasedClassifier()


def classify_failure(message: str, detail: dict | None = None) -> Classification:
    """Classify a failed check. The single entry point callers should use."""
    return _DEFAULT.classify(message, detail)


def worst_severity(severities) -> str | None:
    """Most severe label in an iterable, or None if it holds nothing known.

    Unknown strings are ignored rather than raising: a severity read back from
    an old row should never be able to break the status rollup.
    """
    known = [s for s in severities if s in SEVERITIES]
    if not known:
        return None
    return min(known, key=SEVERITIES.index)
