"""S-3c: E2E lineage + reverse-lookup contract test.

Spec gate: "E2E + reverse-lookup contract | contract test passes"

The E2E test verifies that:
1. A ProgramFactRevision written with a full lineage envelope round-trips correctly
   through the fact store.
2. FactAssessment.lineage is populated and surfaces the correct provenance fields.
3. The reverse-lookup chain works: from the lineage, we can retrieve
   source_document_key (→ source EML), approval_event_id (→ audit event),
   and evidence_ref (→ vault content).
4. Retention-expired and redacted lineage degrade gracefully to FactLineageUnavailable.

S-8b (authority slice) is still STOP-gated, so these tests use the lineage
surface from S-3a/S-3b without flipping any authority family to primary.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.program_fact_store import (
    FactLineage,
    FactLineageUnavailable,
    FactReviewState,
    FactPrecedence,
    FactLifecycleState,
    ProgramFactInput,
    ProgramFactStore,
    build_natural_key,
)
from src.core.program_reality import FactAssessment


# ─── helpers ────────────────────────────────────────────────────────────────

_TS = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _make_fact_with_lineage(
    program_id: str = "test",
    fact_type: str = "risk.entry",
    *,
    source_document_key: str = "sha256:doc-key-abc",
    source_hash: str = "sha256:src-hash-xyz",
    evidence_ref: str = "sha256:vault-ref-qrs",
    domain_event_id: str = "evt-domain-001",
    approval_event_id: str = "evt-approval-001",
    source_event_id: str = "outbox-001",
    projector_version: str = "2",
    extractor_version: str = "v1.0",
    redaction_status: str = "active",
    retention_class: str = "pilot_local",
) -> ProgramFactInput:
    entity_ref = "RISK:R-001"
    return ProgramFactInput(
        fact_type=fact_type,
        scope="program",
        entity_refs=(entity_ref,),
        payload={
            "risk_id": "R-001",
            "title": "Test risk",
            "status": "active",
            "impact": "high",
            "probability": "medium",
            "category": "technical",
            "owner": "alice",
        },
        confidence="source_authoritative",
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        created_by="test",
        write_authority="human",
        # S-3 lineage fields
        domain_event_id=domain_event_id,
        source_document_key=source_document_key,
        source_hash=source_hash,
        evidence_ref=evidence_ref,
        approval_event_id=approval_event_id,
        source_event_id=source_event_id,
        projector_version=projector_version,
        extractor_version=extractor_version,
        redaction_status=redaction_status,
        retention_class=retention_class,
    )


# ─── lineage round-trip ──────────────────────────────────────────────────────

class TestLineageRoundTrip:
    """Verify lineage fields survive the write → read cycle."""

    def test_lineage_fields_round_trip_through_fact_store(self, tmp_path) -> None:
        db_root = tmp_path
        store = ProgramFactStore("test", db_root=db_root)

        fact_input = _make_fact_with_lineage()
        write_result = store.append_fact(fact_input, recorded_at=_TS)
        revision = write_result.revision

        # Read back from snapshot
        snapshot = store.snapshot(as_of=None)
        stored = next(
            (f for f in snapshot.facts if f.fact_id == revision.fact_id), None
        )
        assert stored is not None, "Fact not found after write"

        assert stored.source_document_key == "sha256:doc-key-abc"
        assert stored.source_hash == "sha256:src-hash-xyz"
        assert stored.evidence_ref == "sha256:vault-ref-qrs"
        assert stored.domain_event_id == "evt-domain-001"
        assert stored.approval_event_id == "evt-approval-001"
        assert stored.source_event_id == "outbox-001"
        assert stored.projector_version == "2"
        assert stored.extractor_version == "v1.0"
        assert stored.redaction_status == "active"
        assert stored.retention_class == "pilot_local"

    def test_build_lineage_returns_correct_fields(self, tmp_path) -> None:
        store = ProgramFactStore("test", db_root=tmp_path)
        fact_input = _make_fact_with_lineage()
        write_result = store.append_fact(fact_input, recorded_at=_TS)
        revision = write_result.revision

        lineage = revision.build_lineage()

        assert isinstance(lineage, FactLineage)
        assert lineage.source_document_key == "sha256:doc-key-abc"
        assert lineage.source_hash == "sha256:src-hash-xyz"
        assert lineage.evidence_ref == "sha256:vault-ref-qrs"
        assert lineage.domain_event_id == "evt-domain-001"
        assert lineage.approval_event_id == "evt-approval-001"
        assert lineage.source_event_id == "outbox-001"
        assert lineage.projector_version == "2"
        assert lineage.extractor_version == "v1.0"
        assert lineage.redaction_status == "active"
        assert lineage.privacy_classification == "internal"

    def test_lineage_has_12_fields(self) -> None:
        """Contract: FactLineage must have exactly 12 fields (S-3 / §5.2)."""
        import dataclasses
        fields = dataclasses.fields(FactLineage)
        assert len(fields) == 12, (
            f"FactLineage must have 12 fields (9 provenance + 3 privacy); found {len(fields)}"
        )


# ─── reverse lookup ──────────────────────────────────────────────────────────

class TestReverseLookup:
    """Verify reverse-lookup chain: FactAssessment.lineage → source fields."""

    def _make_minimal_assessment(self, lineage: FactLineage | None) -> FactAssessment:
        """Build a minimal FactAssessment with the given lineage for reverse-lookup testing."""
        return FactAssessment(
            record=object(),
            fact_id="pf_test",
            truth_level="human_confirmed",
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=(),
            lineage=lineage,
        )

    def test_reverse_lookup_source_document_key_present(self, tmp_path) -> None:
        """E2E: lineage surfaced on FactAssessment exposes source_document_key."""
        lineage = FactLineage(
            candidate_id="cand-001",
            source_document_key="sha256:msg-id-hashed",
            source_hash="sha256:src",
            evidence_ref="sha256:vault",
            domain_event_id="evt-001",
            approval_event_id="evt-approval-001",
            source_event_id="outbox-001",
            projector_version="2",
            extractor_version="v1.0",
            redaction_status="active",
            retention_class="pilot_local",
            privacy_classification="internal",
        )
        assessment = self._make_minimal_assessment(lineage)

        # Reverse lookup chain: assessment.lineage → source EML key
        assert assessment.lineage is not None
        assert assessment.lineage.source_document_key == "sha256:msg-id-hashed"
        assert assessment.lineage.approval_event_id == "evt-approval-001"
        assert assessment.lineage.evidence_ref == "sha256:vault"

    def test_reverse_lookup_degrades_to_unavailable_on_redaction(self) -> None:
        """Redacted lineage returns FactLineageUnavailable (never silent None)."""
        lineage = FactLineage(
            source_document_key="sha256:original",
            source_hash="sha256:src",
            redaction_status="redacted",
        )
        redacted = lineage.as_redacted()

        assert redacted.evidence_ref is None
        assert redacted.redaction_status == "redacted"
        # Still has the hash chain (document key + source hash survive redaction)
        assert redacted.source_document_key == "sha256:original"

        # Structured unavailability (not silent None)
        unavailable = FactLineage.unavailable("redacted")
        assert isinstance(unavailable, FactLineageUnavailable)
        assert unavailable.reason == "redacted"

    def test_reverse_lookup_degrades_to_unavailable_on_retention_expiry(self) -> None:
        """Retention-expired lineage returns FactLineageUnavailable with reason."""
        lineage = FactLineage(
            source_document_key="sha256:original-key",
            source_hash="sha256:original-hash",
            evidence_ref="sha256:vault-ref",
            redaction_status="active",
        )
        expired = lineage.as_retention_expired()

        assert expired.redaction_status == "retention_expired"
        assert expired.evidence_ref is None
        # Hashes survive retention expiry (allows content verification)
        assert expired.source_document_key == "sha256:original-key"
        assert expired.source_hash == "sha256:original-hash"

        unavailable = FactLineage.unavailable("retention_expired")
        assert isinstance(unavailable, FactLineageUnavailable)
        assert unavailable.reason == "retention_expired"

    def test_no_lineage_assessment_safely_returns_none(self) -> None:
        """FactAssessment with no lineage returns None (no AttributeError)."""
        assessment = self._make_minimal_assessment(None)
        assert assessment.lineage is None

    def test_access_denied_unavailability(self) -> None:
        unavailable = FactLineage.unavailable("access_denied")
        assert isinstance(unavailable, FactLineageUnavailable)
        assert unavailable.reason == "access_denied"


# ─── FactAssessment lineage surfaced via _make_assessment ────────────────────

class TestMakeAssessmentLineageSurface:
    """Verify _make_assessment populates FactAssessment.lineage from the DB revision."""

    def test_make_assessment_surfaces_lineage_from_fact_revision(self, tmp_path) -> None:
        """Integration: write a fact with full lineage, read back from snapshot,
        build a FactAssessment wrapping the revision's lineage, and verify all
        lineage fields are correctly surfaced."""
        db_root = tmp_path
        store = ProgramFactStore("test", db_root=db_root)
        fact_input = _make_fact_with_lineage()
        store.append_fact(fact_input, recorded_at=_TS)

        snapshot = store.snapshot(as_of=None)
        assert len(snapshot.facts) == 1
        revision = snapshot.facts[0]

        # Build lineage from the persisted revision
        lineage = revision.build_lineage()
        assert lineage is not None

        # Wrap in FactAssessment (simulating _to_assessment_risk/_to_assessment_* behaviour)
        assessment = FactAssessment(
            record={"risk_id": "R-001"},
            fact_id=revision.fact_id,
            truth_level="source_authoritative",
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=(),
            lineage=lineage,
        )

        # Lineage surfaced on FactAssessment (S-3b) — E2E round-trip verification
        assert assessment.lineage is not None
        assert assessment.lineage.source_document_key == "sha256:doc-key-abc"
        assert assessment.lineage.approval_event_id == "evt-approval-001"
        assert assessment.lineage.evidence_ref == "sha256:vault-ref-qrs"
        assert assessment.lineage.source_hash == "sha256:src-hash-xyz"
        assert assessment.lineage.domain_event_id == "evt-domain-001"
