"""Contract tests for ADF-W0.18: ledger event registration.

Covers the three done-check dimensions named in specs/arch-data-fix.md
Appendix C: schema validation, replay (hash-chain), and zone-boundary write
ownership for the sixteen Appendix A.2 event payload contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.ledger.event_log import (
    ConfidenceTier,
    TemporalConfidence,
    build_event_envelope,
    verify_event_log,
    write_event,
)
from src.core.ledger.event_type_registry import (
    LEDGER_EVENT_REGISTRY,
    EventDisposition,
    lookup_event_spec,
)
from src.core.ledger.event_types import get_registered_event_types, is_known_event_type, validate_event_payload
from src.core.ledger.source_refs import OperatorAssertionRef

_MINIMAL_PAYLOADS: dict[str, dict[str, object]] = {
    "value.workflow_started.v1": {
        "measurement_id": "m1", "edition_id": "xpf_weekly", "workflow": "weekly_issue",
        "mode": "vertex", "actor": "operator", "started_at": "2026-07-12T00:00:00Z",
    },
    "value.workflow_completed.v1": {
        "measurement_id": "m1", "edition_id": "xpf_weekly", "workflow": "weekly_issue",
        "mode": "vertex", "actor": "operator", "completed_at": "2026-07-12T00:10:00Z",
        "active_seconds": 60.0, "machine_wait_seconds": 5.0, "external_wait_seconds": 0.0,
        "review_seconds": 30.0, "manual_acquisition_seconds": 0.0,
    },
    "value.manual_step_attested.v1": {
        "measurement_id": "m1", "step": "review", "seconds": 30.0,
        "attested_by": "operator", "attested_at": "2026-07-12T00:05:00Z",
    },
    "value.review_edit_recorded.v1": {
        "proposal_class": "risk", "proposal_id": "p1", "outcome": "accept",
        "review_seconds": 12.0, "edit_magnitude": 0.0, "reviewer": "operator", "artifact_ref": "issue:123",
    },
    "value.gap_closed.v1": {
        "gap_id": "gap1", "closed_by": "run", "evidence_refs": ["event:abc"], "closed_at": "2026-07-12T00:00:00Z",
    },
    "quality.confirmed_defect_prevented.v1": {
        "gate_id": "QG-33", "artifact_ref": "issue:123", "defect_summary": "budget exceeded",
        "confirmed_by": "operator", "evidence_refs": ["event:abc"],
    },
    "source.acquisition_completed.v1": {
        "acquisition_id": "acq1", "channel": "ado", "run_id": "run1", "completeness": "complete",
        "watermark_before": "2026-07-01T00:00:00Z", "watermark_after": "2026-07-12T00:00:00Z",
        "provider_summary": {"pages": 1},
    },
    "operation.trace_linked.v1": {
        "correlation_id": "corr1", "workflow_id": "wf1", "run_id": "run1",
        "stage": "acquisition", "ref_type": "signal", "ref_id": "sig1",
    },
    "decision.outcome_recorded.v1": {
        "decision_id": "dec1", "outcome": "accepted", "recorded_by": "operator", "evidence_refs": ["event:abc"],
    },
    "action.closed.v1": {
        "action_id": "act1", "closed_state": "done", "closed_by": "operator", "evidence_refs": ["event:abc"],
    },
    "ai.run_lifecycle.v1": {
        "ai_run_id": "run1", "feature": "risk_proposal", "state": "requested", "prompt_version": "v1",
        "policy_version": "1", "model_deployment": "gpt-4o-mini", "context_manifest_ref": "manifest:1",
    },
    "ai.release_decision.v1": {
        "ai_run_id": "run1", "terminal": "released", "reason": "validated", "validator_finding_count": 0,
    },
    "ai.application_receipt.v1": {
        "ai_run_id": "run1", "receipt": "applied",
    },
    "actuation.intent_created.v1": {
        "operation_intent_id": "intent1", "idempotency_key": "vertex-intent-1", "operation_type": "create_task",
        "target_identity": "ado:One", "proposal_id": "prop1", "approval_event_ref": "event:approval1",
    },
    "actuation.receipt_recorded.v1": {
        "operation_intent_id": "intent1", "receipt_state": "succeeded", "provider_summary": {"status": 200},
    },
    "actuation.duplicate_prevented.v1": {
        "operation_intent_id": "intent1", "detection": "preflight_search", "evidence": "matched existing tag",
    },
}

_ADF_PREFIXES = (
    "value.", "quality.", "source.", "operation.", "action.", "ai.", "actuation.", "decision.outcome_recorded.",
)


def test_all_sixteen_a2_event_types_are_registered() -> None:
    assert set(_MINIMAL_PAYLOADS) <= get_registered_event_types()
    assert len(_MINIMAL_PAYLOADS) == 16


@pytest.mark.parametrize("event_type,payload", sorted(_MINIMAL_PAYLOADS.items()))
def test_schema_validates_minimal_payload(event_type: str, payload: dict[str, object]) -> None:
    assert is_known_event_type(event_type)
    validate_event_payload(event_type, payload)  # must not raise


def test_schema_rejects_missing_required_field() -> None:
    payload = dict(_MINIMAL_PAYLOADS["ai.run_lifecycle.v1"])
    del payload["feature"]
    with pytest.raises(ValueError, match="missing required field"):
        validate_event_payload("ai.run_lifecycle.v1", payload)


def test_schema_rejects_wrong_field_type() -> None:
    payload = dict(_MINIMAL_PAYLOADS["ai.release_decision.v1"])
    payload["validator_finding_count"] = "not-an-int"
    with pytest.raises(ValueError, match="must be an integer"):
        validate_event_payload("ai.release_decision.v1", payload)


def test_decision_outcome_recorded_does_not_bridge_to_tpm_decision_family() -> None:
    spec = lookup_event_spec("decision.outcome_recorded.v1")
    assert spec is not None
    assert spec.disposition is EventDisposition.PASSTHROUGH
    assert spec.bridge_appender_name is None

    # The pre-existing TPM decision family is untouched by the override.
    tpm_spec = lookup_event_spec("decision.made.v1")
    assert tpm_spec is not None
    assert tpm_spec.disposition is EventDisposition.PROJECTABLE
    assert tpm_spec.bridge_appender_name == "append_bridged_decision_event"


@pytest.mark.parametrize("prefix", _ADF_PREFIXES)
def test_new_prefixes_resolve_to_passthrough(prefix: str) -> None:
    spec = lookup_event_spec(prefix + "example_event.v1")
    assert spec is not None
    assert spec.disposition is EventDisposition.PASSTHROUGH


def test_adf_registry_entries_declare_zone_a_only_ownership() -> None:
    adf_specs = [spec for spec in LEDGER_EVENT_REGISTRY if spec.prefix in _ADF_PREFIXES]
    assert len(adf_specs) == len(_ADF_PREFIXES)
    for spec in adf_specs:
        assert spec.owner_module is not None, spec.prefix
        assert spec.owner_module == "src.core" or spec.owner_module.startswith("src.core."), (
            f"{spec.prefix} owner_module {spec.owner_module!r} is not Zone-A-only"
        )
        assert not spec.owner_module.startswith(("src.ai", "src.m365", "src.commands"))


def test_replay_hash_chain_after_writing_new_event_types(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    source_ref = OperatorAssertionRef(
        asserted_by="operator",
        asserted_at=datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc),
        vault_hash="sha256:vault-adf",
    )

    for event_type in ("ai.run_lifecycle.v1", "actuation.intent_created.v1", "value.workflow_started.v1"):
        envelope = build_event_envelope(
            program_id="fixture_prog",
            event_type=event_type,
            occurred_at=datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 7, 12, 0, 0, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="adf_w0_18_test",
            payload=_MINIMAL_PAYLOADS[event_type],
            source_ref=source_ref,
        )
        write_event(envelope, programs_root=programs_root)

    verification = verify_event_log("fixture_prog", programs_root=programs_root)
    assert verification.ok, verification
