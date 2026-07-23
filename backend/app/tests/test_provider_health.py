from __future__ import annotations

from app.services import provider_health as ph


def setup_function() -> None:
    ph.reset_for_tests()


def test_classify_error_categories() -> None:
    assert ph.classify_error("timeout waiting") == ph.ErrorClass.TRANSIENT
    assert ph.classify_error("HTTP 429 rate limit") == ph.ErrorClass.RATE_LIMIT
    assert ph.classify_error("unauthorized 401") == ph.ErrorClass.CREDENTIAL
    assert ph.classify_error("tools are not supported") == ph.ErrorClass.CAPABILITY
    assert ph.classify_error("invalid parameter temperature") == ph.ErrorClass.CAPABILITY


def test_capability_errors_do_not_cool() -> None:
    ph.record_failure(9, "cap-model", "does not support tools")
    h = ph.get_health(9, "cap-model")
    assert h["last_error_class"] == "capability"
    assert h["state"] == "healthy"
    assert h["consecutive_failures"] == 0


def test_cooldown_backoff_and_cap() -> None:
    ph.record_failure(1, "m1", "timeout")
    h1 = ph.get_health(1, "m1")
    assert h1["state"] == "cooling"
    assert h1["cooldown_remaining_seconds"] > 0
    for _ in range(8):
        ph.record_failure(1, "m1", "timeout")
    h = ph.get_health(1, "m1")
    assert h["cooldown_remaining_seconds"] <= 600


def test_credential_errors_do_not_cool() -> None:
    ph.record_failure(2, "m2", "401 unauthorized")
    h = ph.get_health(2, "m2")
    assert h["last_error_class"] == "credential"
    assert h["state"] == "healthy"


def test_success_clears_cooldown() -> None:
    ph.record_failure(3, "m3", "timeout")
    ph.record_success(3, "m3")
    h = ph.get_health(3, "m3")
    assert h["state"] == "healthy"
    assert h["consecutive_failures"] == 0


def test_liveness_source_skipped() -> None:
    ph.record_failure(4, "m4", "timeout", source="liveness_probe")
    h = ph.get_health(4, "m4")
    assert h["state"] == "healthy"


def test_sort_puts_cooling_last() -> None:
    ph.record_failure(1, "a", "timeout")
    ordered = ph.sort_provider_candidates([(1, "a"), (2, "b")])
    assert ordered[0] == (2, "b")
    assert ordered[1] == (1, "a")
