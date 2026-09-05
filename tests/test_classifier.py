"""Classifier tests: infrastructure vocabulary, scoring, and severity ordering."""
import pytest

from apihealthchecker.classifier import (
    RulesBasedClassifier,
    classify_failure,
    worst_severity,
)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Connection failed", "connectivity"),
        ("Timed out after 5.0s", "timeout"),
        ("Could not resolve host", "dns"),
        ("certificate verify failed", "tls"),
        ("Too many redirects", "connectivity"),
        ("Access denied to bucket 'uploads'", "authorization"),
        ("Invalid URL: no schema supplied", "configuration"),
    ],
)
def test_category_from_message(message, expected):
    assert classify_failure(message).category == expected


@pytest.mark.parametrize(
    "code,category",
    [
        (500, "server_error"),
        (503, "server_error"),
        (404, "not_found"),
        (429, "rate_limited"),
        (403, "authorization"),
        (401, "authorization"),
    ],
)
def test_category_from_status_code_in_detail(code, category):
    """The engine reports the code as an int in detail, which no keyword would
    match. _build_text renders it into the same vocabulary the messages use."""
    result = classify_failure(f"Responded {code}", {"status_code": code})
    assert result.category == category


@pytest.mark.parametrize(
    "message,severity",
    [
        ("Connection failed", "critical"),
        ("Could not resolve host", "critical"),
        ("Responded 503", "critical"),
        ("Timed out after 5.0s", "high"),
        ("certificate verify failed", "high"),
        ("Access denied", "high"),
        ("Responded 429", "medium"),
        ("Responded 404", "medium"),
        ("Responded 405", "low"),
    ],
)
def test_severity_from_message(message, severity):
    assert classify_failure(message).severity == severity


def test_severity_is_most_severe_not_most_frequent():
    """Ported from ticket-triage: one outage word outranks several mild ones.
    A first-match or a most-frequent rule would report medium here."""
    message = "Responded 404. Responded 404 again. Connection failed."
    assert classify_failure(message).severity == "critical"


def test_category_is_most_hit_not_first_match():
    """Several connectivity words beat one passing status-code mention."""
    message = "Connection refused, connection reset, could not reach the host. Responded 404."
    assert classify_failure(message).category == "connectivity"


def test_unmatched_message_is_unclassified_and_low():
    """No evidence means saying so, not guessing a plausible label."""
    result = classify_failure("Something entirely unfamiliar happened")
    assert result.category == "unclassified"
    assert result.severity == "low"
    assert result.confidence == 0.0


def test_classifier_used_is_recorded():
    assert classify_failure("Connection failed").classifier_used == "rules"


def test_confidence_describes_the_category_only():
    result = classify_failure("Connection refused")
    assert 0.0 < result.confidence <= 1.0


def test_negation_suppresses_a_hit():
    result = RulesBasedClassifier().classify("The host did not time out")
    assert result.category != "timeout"


def test_negation_does_not_leak_across_sentences():
    """A negation in one sentence must not suppress a keyword in the next."""
    result = RulesBasedClassifier().classify("Not a DNS problem. Connection refused.")
    assert result.category == "connectivity"


def test_whole_word_matching():
    """'dns' must not match inside an unrelated longer word."""
    result = RulesBasedClassifier().classify("The dnsmasqfoo daemon logged something")
    assert result.category == "unclassified"


def test_empty_message_does_not_raise():
    result = classify_failure("", None)
    assert result.category == "unclassified"


def test_non_dict_detail_is_ignored():
    result = classify_failure("Connection failed", detail=None)
    assert result.category == "connectivity"


def test_boolean_status_code_is_not_treated_as_a_number():
    """bool is a subclass of int; True would otherwise render as 'responded 1'."""
    result = classify_failure("Unusual", {"status_code": True})
    assert result.category == "unclassified"


def test_detail_values_contribute_to_classification():
    result = classify_failure("Unexpected error", {"code": "AccessDenied"})
    assert result.category == "authorization"


def test_classification_to_dict_is_serializable():
    payload = classify_failure("Connection failed").to_dict()
    assert set(payload) == {"category", "severity", "classifier_used", "confidence"}


def test_worst_severity_picks_most_severe():
    assert worst_severity(["low", "critical", "medium"]) == "critical"


def test_worst_severity_ignores_unknown_labels():
    """A severity read back from an old row must never break the rollup."""
    assert worst_severity(["bogus", "high"]) == "high"


def test_worst_severity_of_nothing_is_none():
    assert worst_severity([]) is None
    assert worst_severity(["bogus"]) is None
