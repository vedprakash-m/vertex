"""WI-1.5: Tests for fact schema registry (valid/invalid/warn-vs-strict).

Covers all 13 new fact types from the §6.3 fact-type table.
"""
from __future__ import annotations

import pytest

from src.core.fact_schema_registry import (
    FactValidationResult,
    get_registered_fact_types,
    is_known_fact_type,
    validate_fact_payload,
)


# ---------------------------------------------------------------------------
# Completeness: all 13 new fact types must be registered
# ---------------------------------------------------------------------------

_REQUIRED_FACT_TYPES = frozenset({
    "signal.observation",
    "fact.corroboration",
    "fact.conflict",
    "fact.conflict_resolved",
    "fact.reconfirmation",
    "fact.source_sync",
    "trust.source_score",
    "trust.bootstrap_grant",
    "entity.alias",
    "commitment.entry",
    "action.proposal",
    "action.executed",
    "action.failed",
})


def test_all_required_fact_types_registered():
    registered = get_registered_fact_types()
    missing = _REQUIRED_FACT_TYPES - registered
    assert not missing, f"Missing fact types: {sorted(missing)}"


def test_is_known_fact_type_true():
    assert is_known_fact_type("signal.observation") is True


def test_is_known_fact_type_false():
    assert is_known_fact_type("nonexistent.type") is False


# ---------------------------------------------------------------------------
# signal.observation
# ---------------------------------------------------------------------------

def test_signal_observation_valid():
    r = validate_fact_payload("signal.observation", {
        "source": "ado",
        "signal_id": "wi-12345",
        "signal_class": "bug",
        "title": "Login crash",
        "observed_at": "2024-01-01T00:00:00Z",
    })
    assert r.valid
    assert r.errors == ()


def test_signal_observation_missing_required():
    r = validate_fact_payload("signal.observation", {"source": "ado"})
    assert not r.valid
    assert any("signal_id" in e for e in r.errors)
    assert any("signal_class" in e for e in r.errors)


def test_signal_observation_warn_missing_optional():
    r = validate_fact_payload("signal.observation", {
        "source": "ado",
        "signal_id": "s1",
        "signal_class": "bug",
    })
    assert r.valid  # warnings don't fail in non-strict mode
    assert any("title" in w or "observed_at" in w for w in r.warnings)


def test_signal_observation_strict_warns_become_errors():
    r = validate_fact_payload("signal.observation", {
        "source": "ado",
        "signal_id": "s1",
        "signal_class": "bug",
    }, strict=True)
    assert not r.valid  # strict: warnings → errors
    assert r.warnings == ()  # warnings consumed into errors
    assert len(r.errors) >= 1


# ---------------------------------------------------------------------------
# fact.conflict_resolved (v3.1 — new in WI-1.5)
# ---------------------------------------------------------------------------

def test_fact_conflict_resolved_valid():
    r = validate_fact_payload("fact.conflict_resolved", {
        "conflict_id": "c-001",
        "resolution_reason": "ADO confirmed",
        "resolved_by": "tpm@example.com",
        "resolution_authority": "human",
        "resolved_at": "2024-06-01T12:00:00Z",
    })
    assert r.valid
    assert r.errors == ()
    assert r.warnings == ()


def test_fact_conflict_resolved_missing_required():
    r = validate_fact_payload("fact.conflict_resolved", {
        "conflict_id": "c-001",
    })
    assert not r.valid
    assert any("resolution_reason" in e for e in r.errors)
    assert any("resolved_by" in e for e in r.errors)


def test_fact_conflict_resolved_warn_optional():
    r = validate_fact_payload("fact.conflict_resolved", {
        "conflict_id": "c-001",
        "resolution_reason": "confirmed",
        "resolved_by": "tpm@example.com",
    })
    assert r.valid
    assert any("resolution_authority" in w or "resolved_at" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# commitment.entry — enum validation
# ---------------------------------------------------------------------------

def test_commitment_entry_valid_inbound():
    r = validate_fact_payload("commitment.entry", {
        "commitment_id": "cm-001",
        "title": "Deliver by Q2",
        "dri": "eng@example.com",
        "due_date": "2024-06-30",
        "direction": "inbound",
    })
    assert r.valid


def test_commitment_entry_valid_outbound():
    r = validate_fact_payload("commitment.entry", {
        "commitment_id": "cm-002",
        "title": "Ship feature X",
        "dri": "pm@example.com",
        "due_date": "2024-07-15",
        "direction": "outbound",
    })
    assert r.valid


def test_commitment_entry_invalid_direction():
    r = validate_fact_payload("commitment.entry", {
        "commitment_id": "cm-003",
        "title": "Something",
        "dri": "eng@example.com",
        "due_date": "2024-08-01",
        "direction": "sideways",  # invalid
    })
    assert not r.valid
    assert any("direction" in e for e in r.errors)


# ---------------------------------------------------------------------------
# trust.source_score
# ---------------------------------------------------------------------------

def test_trust_source_score_valid():
    r = validate_fact_payload("trust.source_score", {
        "source": "ado",
        "signal_class": "bug",
        "score": 0.85,
        "breaker_verdict": "pass",
        "computed_at": "2024-06-01T00:00:00Z",
        "sample_count": 120,
    })
    assert r.valid
    assert r.errors == ()
    assert r.warnings == ()


def test_trust_source_score_missing_required():
    r = validate_fact_payload("trust.source_score", {"source": "ado"})
    assert not r.valid


# ---------------------------------------------------------------------------
# action.failed (v3.2 new type)
# ---------------------------------------------------------------------------

def test_action_failed_valid():
    r = validate_fact_payload("action.failed", {
        "proposal_id": "prop-001",
        "adapter": "ado",
        "operation": "close_work_item",
        "failure_reason": "Permission denied",
        "failed_at": "2024-06-10T14:00:00Z",
        "terminal": True,
    })
    assert r.valid


def test_action_failed_missing_required():
    r = validate_fact_payload("action.failed", {
        "proposal_id": "prop-002",
    })
    assert not r.valid
    assert any("adapter" in e for e in r.errors)
    assert any("failure_reason" in e for e in r.errors)


# ---------------------------------------------------------------------------
# Unknown fact type — should return valid with a warning
# ---------------------------------------------------------------------------

def test_unknown_fact_type_returns_valid_with_warning():
    r = validate_fact_payload("unknown.custom.type", {"foo": "bar"})
    assert r.valid  # unknown types pass with warning
    assert any("No schema registered" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# FactValidationResult.ok() helper
# ---------------------------------------------------------------------------

def test_validation_result_ok_true():
    r = FactValidationResult(fact_type="x", valid=True, errors=(), warnings=())
    assert r.ok() is True


def test_validation_result_ok_false_when_errors():
    r = FactValidationResult(fact_type="x", valid=False, errors=("bad",), warnings=())
    assert r.ok() is False


def test_validation_result_ok_false_when_valid_but_has_errors():
    # Edge case: valid=True but errors present (shouldn't happen in practice)
    r = FactValidationResult(fact_type="x", valid=True, errors=("e",), warnings=())
    assert r.ok() is False
