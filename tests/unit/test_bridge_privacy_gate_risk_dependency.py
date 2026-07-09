"""Track M regression tests (specs/fix-data-flow.md §6.13 / PR-14).

Confirms the bridge's composite privacy gate (`_projection_privacy_gate`,
AG-11, `src/commands/ledger.py`) provides sufficient coverage for the risk
and dependency families specifically — the two families Track B just
migrated onto `ProgramReality`, and the volume increase Track A's
default-on bridge flip (ADR-0011) makes more consequential.

The gate itself is family-agnostic by construction: it runs
`run_local_checks(canonical_json(envelope.payload), ...)` on every
`OPERATOR_CONFIRMED` envelope immediately before dispatch to any family's
appender (`_maybe_bridge_event_to_fact_store`), regardless of `event_type`.
No new privacy subsystem or per-family logic is required — this track's
job is to confirm that fact directly for risk/dependency payload shapes,
not just the milestone-shaped payload the original AG-11 tests used.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.source_refs import EmailRef

NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def _envelope(*, event_type: str, payload: dict, confidence: ConfidenceTier) -> EventEnvelope:
    return EventEnvelope(
        event_id="evt-privacy-1",
        program_id="xpf",
        event_type=event_type,
        occurred_at=NOW,
        recorded_at=NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=confidence,
        actor="operator@example.com",
        payload=payload,
        source_ref=EmailRef(subject="s", sent_at=NOW, sender="a@b.com", message_id="m1", vault_hash="sha256:v1"),
    )


def test_privacy_gate_blocks_credential_in_accepted_risk_fact() -> None:
    from src.commands.ledger import _projection_privacy_gate

    verdict = _projection_privacy_gate(
        _envelope(
            event_type="risk.raised.v1",
            payload={
                "risk_id": "risk:secret-leak",
                "title": "Vendor delay",
                "severity": "high",
                # An AWS-looking secret key smuggled into a risk description.
                "description": "creds: AKIAIOSFODNN7EXAMPLE/wJalrXUtnFEMI/K7MDENG/bPxRfiCY",
            },
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
    )
    assert verdict is False, "credential hit in an ACCEPTED risk fact must block bridge projection"


def test_privacy_gate_passes_clean_accepted_risk_fact() -> None:
    from src.commands.ledger import _projection_privacy_gate

    verdict = _projection_privacy_gate(
        _envelope(
            event_type="risk.raised.v1",
            payload={
                "risk_id": "risk:clean",
                "title": "Vendor delay",
                "severity": "medium",
                "description": "Vendor is one sprint behind on the integration milestone.",
            },
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
    )
    assert verdict is True


def test_privacy_gate_blocks_credential_in_accepted_dependency_fact() -> None:
    from src.commands.ledger import _projection_privacy_gate

    verdict = _projection_privacy_gate(
        _envelope(
            event_type="dependency.declared.v1",
            payload={
                "dependency_id": "dependency:secret-leak",
                "from_entity": "workstream:a",
                "to_entity": "workstream:b",
                "description": "blocked pending creds: AKIAIOSFODNN7EXAMPLE/wJalrXUtnFEMI/K7MDENG/bPxRfiCY",
            },
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
    )
    assert verdict is False, "credential hit in an ACCEPTED dependency fact must block bridge projection"


def test_privacy_gate_passes_clean_accepted_dependency_fact() -> None:
    from src.commands.ledger import _projection_privacy_gate

    verdict = _projection_privacy_gate(
        _envelope(
            event_type="dependency.declared.v1",
            payload={
                "dependency_id": "dependency:clean",
                "from_entity": "workstream:a",
                "to_entity": "workstream:b",
                "description": "Deployment blocked on platform team sign-off.",
            },
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
    )
    assert verdict is True


def test_privacy_gate_does_not_scope_proposed_risk_facts() -> None:
    """PROPOSED (non-OPERATOR_CONFIRMED) facts are not the trust root -- the
    ingest-time gate owns them, matching the existing milestone-family
    contract (`test_activation_gates_v1_24.py::test_credential_in_proposed_fact_passes`).
    Confirmed here for risk specifically, not just milestone."""
    from src.commands.ledger import _projection_privacy_gate

    verdict = _projection_privacy_gate(
        _envelope(
            event_type="risk.raised.v1",
            payload={
                "risk_id": "risk:proposed",
                "title": "Vendor delay",
                "severity": "high",
                "description": "creds: AKIAIOSFODNN7EXAMPLE/secret",
            },
            confidence=ConfidenceTier.AI_EXTRACTED,
        )
    )
    assert verdict is True
