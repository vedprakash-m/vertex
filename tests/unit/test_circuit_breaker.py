from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerState


def test_circuit_breaker_opens_after_three_failures(tmp_path) -> None:
    breaker = CircuitBreaker(state_path=tmp_path / ".ado_breaker.json")
    now = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    breaker.record_failure(now=now)
    breaker.record_failure(now=now + timedelta(minutes=1))

    state = breaker.get_state()
    assert state.state == CircuitBreakerState.CLOSED
    assert state.failure_count == 2
    assert breaker.should_allow_request(now=now + timedelta(minutes=2)) == (True, False)

    breaker.record_failure(now=now + timedelta(minutes=2))

    state = breaker.get_state()
    assert state.state == CircuitBreakerState.OPEN
    assert state.failure_count == 3
    assert breaker.should_allow_request(now=now + timedelta(minutes=3)) == (False, False)


def test_circuit_breaker_allows_single_probe_after_timeout_and_closes_on_success(tmp_path) -> None:
    breaker = CircuitBreaker(state_path=tmp_path / ".ado_breaker.json", recovery_timeout=timedelta(minutes=30))
    now = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    breaker.record_failure(now=now)
    breaker.record_failure(now=now + timedelta(minutes=1))
    breaker.record_failure(now=now + timedelta(minutes=2))

    assert breaker.should_allow_request(now=now + timedelta(minutes=20)) == (False, False)

    allow_request, is_probe = breaker.should_allow_request(now=now + timedelta(minutes=40))
    assert (allow_request, is_probe) == (True, True)
    assert breaker.get_state().state == CircuitBreakerState.HALF_OPEN
    assert breaker.should_allow_request(now=now + timedelta(minutes=41)) == (False, False)

    breaker.record_success(is_probe=True, now=now + timedelta(minutes=42))

    state = breaker.get_state()
    assert state.state == CircuitBreakerState.CLOSED
    assert state.failure_count == 0
    assert state.last_success_at == now + timedelta(minutes=42)


def test_circuit_breaker_reopens_when_probe_fails(tmp_path) -> None:
    breaker = CircuitBreaker(state_path=tmp_path / ".ado_breaker.json", recovery_timeout=timedelta(minutes=5))
    now = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    breaker.record_failure(now=now)
    breaker.record_failure(now=now + timedelta(minutes=1))
    breaker.record_failure(now=now + timedelta(minutes=2))

    allow_request, is_probe = breaker.should_allow_request(now=now + timedelta(minutes=10))
    assert (allow_request, is_probe) == (True, True)

    breaker.record_failure(is_probe=True, now=now + timedelta(minutes=11))

    state = breaker.get_state()
    assert state.state == CircuitBreakerState.OPEN
    assert state.failure_count == 0
    assert breaker.should_allow_request(now=now + timedelta(minutes=12)) == (False, False)