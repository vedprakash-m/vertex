"""Direct coverage for the extracted integration intent-status helpers (D-13)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.commands.integration_intent_status import (
    _intent_match_confidence,
    _recompute_intent_status_with_conn,
)
from src.core.discovery_intent import SourceCandidateStatus, SourceIntentStatus


class _Store:
    def __init__(self, *, matches=(), candidates=(), attempts=()) -> None:
        self._matches = matches
        self._candidates = candidates
        self._attempts = attempts

    def get_intent_matches(self, candidate_id):  # noqa: ANN001
        return self._matches

    def list_candidates_for_intent_with_conn(self, conn, intent_id):  # noqa: ANN001
        return self._candidates

    def get_attempts_with_conn(self, conn, intent_id, exclude_expired=False):  # noqa: ANN001
        return self._attempts


def _match(intent_id: str, conf: float):
    return SimpleNamespace(intent_id=intent_id, match_confidence=conf)


def _cand(status: SourceCandidateStatus, conf: float = 0.5):
    return SimpleNamespace(status=status, confidence=conf)


def _attempt(outcome_name: str, expires_at=None):  # noqa: ANN001
    return SimpleNamespace(outcome=SimpleNamespace(name=outcome_name), expires_at=expires_at)


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_intent_match_confidence_found_and_default() -> None:
    store = _Store(matches=(_match("i1", 0.42), _match("i2", 0.9)))
    assert _intent_match_confidence(store, "c1", "i2") == 0.9
    assert _intent_match_confidence(store, "c1", "missing") == 1.0


def test_recompute_resolved_when_accepted() -> None:
    store = _Store(candidates=(_cand(SourceCandidateStatus.ACCEPTED),))
    assert _recompute_intent_status_with_conn(None, store, "i1", as_of=_NOW) is SourceIntentStatus.RESOLVED


def test_recompute_ambiguous_with_multiple_high_confidence_pending() -> None:
    store = _Store(candidates=(_cand(SourceCandidateStatus.PENDING, 0.8), _cand(SourceCandidateStatus.PENDING, 0.9)))
    assert _recompute_intent_status_with_conn(None, store, "i1", as_of=_NOW) is SourceIntentStatus.AMBIGUOUS


def test_recompute_candidate_found_with_single_pending() -> None:
    store = _Store(candidates=(_cand(SourceCandidateStatus.PENDING, 0.8),))
    assert _recompute_intent_status_with_conn(None, store, "i1", as_of=_NOW) is SourceIntentStatus.CANDIDATE_FOUND


def test_recompute_auth_blocked_attempt() -> None:
    store = _Store(attempts=(_attempt("AUTH_BLOCKED"),))
    assert _recompute_intent_status_with_conn(None, store, "i1", as_of=_NOW) is SourceIntentStatus.AUTH_BLOCKED


def test_recompute_declared_when_no_candidates_or_attempts() -> None:
    store = _Store()
    assert _recompute_intent_status_with_conn(None, store, "i1", as_of=_NOW) is SourceIntentStatus.DECLARED
