from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.knowledge_candidate_store import KnowledgeCandidate, KnowledgeCandidateDecisionRecord, ProposedKnowledgeClaim, append_candidate, append_triage_decision
from src.core.knowledge_claim_store import KnowledgeClaimRevision, append_claim_revision, claim_ids_referencing_vault_hash, find_claim_redaction_by_id, find_claim_revision_by_id, load_all_claim_revisions, load_scoped_claim_revisions, redact_claim_revision, resolve_knowledge_context, summarize_knowledge_status
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import EmailRef, KnowledgeDocumentRef, LTDeckRef, OperatorAssertionRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())


def test_resolve_knowledge_context_prefers_confidence_before_scope() -> None:
    context = resolve_knowledge_context(
        ["sku_generation:gen9"],
        scope_chain=("program:acme", "domain:storage-platform", "org"),
        revisions=(
            KnowledgeClaimRevision(
                claim_id="01PROGRAM",
                scope="program:acme",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                valid_until=None,
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                confidence=ConfidenceTier.AI_EXTRACTED,
                source_ref=_deck_ref(),
                supersedes=None,
                natural_key="program:acme/sku_generation:gen9/first_deployment",
            ),
            KnowledgeClaimRevision(
                claim_id="01DOMAIN",
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H1",
                valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                valid_until=None,
                recorded_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
                supersedes=None,
                natural_key="domain:storage-platform/sku_generation:gen9/first_deployment",
            ),
        ),
        projection_coverage={"sku_generation:gen9": "absent"},
        as_of=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    entry = context.entry("sku_generation:gen9")

    assert entry is not None
    assert entry.projection_coverage == "absent"
    assert entry.claims[0].claim_id == "01DOMAIN"
    assert entry.claims[0].value == "2025-H1"


def test_resolve_knowledge_context_retains_wider_override_annotations_and_tombstones() -> None:
    context = resolve_knowledge_context(
        ["sku_generation:gen9"],
        scope_chain=("program:acme", "domain:storage-platform", "org"),
        revisions=(
            KnowledgeClaimRevision(
                claim_id="01DOMAIN",
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="launch_blocker",
                value="wingtip",
                valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                valid_until=None,
                recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=_deck_ref(),
                supersedes=None,
                natural_key="domain:storage-platform/sku_generation:gen9/launch_blocker",
            ),
            KnowledgeClaimRevision(
                claim_id="01PROGRAM",
                scope="program:acme",
                subject="sku_generation:gen9",
                predicate="launch_blocker",
                value=None,
                valid_from=datetime(2025, 1, 15, tzinfo=timezone.utc),
                valid_until=None,
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
                supersedes=None,
                natural_key="program:acme/sku_generation:gen9/launch_blocker",
            ),
        ),
        projection_coverage={"sku_generation:gen9": "present"},
        as_of=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    claim = context.entry("sku_generation:gen9").claims[0]

    assert claim.claim_id == "01PROGRAM"
    assert claim.tombstoned is True
    assert claim.value is None
    assert claim.overridden_claim_ids == ("01DOMAIN",)


def test_append_claim_revision_round_trips_and_supersedes_prior_value(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    first = append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    second = append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-Q4",
        valid_from=datetime(2025, 8, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    revisions = load_scoped_claim_revisions(("domain:storage-platform",), knowledge_root=knowledge_root)

    assert [revision.claim_id for revision in revisions] == [first.claim_id, second.claim_id]
    assert second.supersedes == first.claim_id


def test_append_claim_revision_rejects_external_origin_source_ref_without_vault_hash(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    with pytest.raises(ValueError, match="vault_hash"):
        append_claim_revision(
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            source_ref=EmailRef(
                subject="Escalation",
                sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                sender="owner@example.com",
                message_id="msg-1",
            ),
            knowledge_root=knowledge_root,
            recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_find_claim_revision_by_id_searches_scopes(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    revision = append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    found = find_claim_revision_by_id(revision.claim_id, knowledge_root=knowledge_root)

    assert found is not None
    assert found.claim_id == revision.claim_id


def test_redact_claim_revision_removes_revision_from_normal_reads(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    revision = append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    record = redact_claim_revision(
        revision.claim_id,
        knowledge_root=knowledge_root,
        actor="operator",
        reason="pii cleanup",
        redacted_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    revisions = load_scoped_claim_revisions(("domain:storage-platform",), knowledge_root=knowledge_root)
    found_active = find_claim_revision_by_id(revision.claim_id, knowledge_root=knowledge_root)
    found_raw = find_claim_revision_by_id(revision.claim_id, knowledge_root=knowledge_root, include_redacted=True)
    stored_record = find_claim_redaction_by_id(revision.claim_id, knowledge_root=knowledge_root)

    assert revisions == ()
    assert found_active is None
    assert found_raw is not None
    assert found_raw.value is None
    assert record.claim_id == revision.claim_id
    assert stored_record is not None
    assert stored_record.reason == "pii cleanup"


def test_claim_ids_referencing_vault_hash_finds_active_claims_only(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    revision = append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=KnowledgeDocumentRef(
            vault_hash="sha256:abc123",
            original_filename="doc.md",
            origin_kind="local_path",
            origin_path="Q:/tmp/doc.md",
            origin_url=None,
            ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            section="line:1",
        ),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert claim_ids_referencing_vault_hash("sha256:abc123", knowledge_root=knowledge_root) == (revision.claim_id,)

    redact_claim_revision(
        revision.claim_id,
        knowledge_root=knowledge_root,
        actor="operator",
        reason="pii cleanup",
        redacted_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert claim_ids_referencing_vault_hash("sha256:abc123", knowledge_root=knowledge_root) == ()


def test_load_all_claim_revisions_excludes_redacted_by_default(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    revision = append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    redact_claim_revision(
        revision.claim_id,
        knowledge_root=knowledge_root,
        actor="operator",
        reason="pii cleanup",
        redacted_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert load_all_claim_revisions(knowledge_root=knowledge_root) == ()
    assert len(load_all_claim_revisions(knowledge_root=knowledge_root, include_redacted=True)) == 1


def test_summarize_knowledge_status_reports_scope_candidate_and_vault_counts(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="launch_blocker",
        value=None,
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    candidates_dir = knowledge_root / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    append_candidate(
        KnowledgeCandidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            proposed_claim=ProposedKnowledgeClaim(
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
                valid_until=None,
            ),
            proposed_confidence=ConfidenceTier.AI_EXTRACTED.value,
            source_ref=OperatorAssertionRef(
                asserted_by="operator",
                asserted_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            ),
            pipeline="extract",
            extraction_confidence=0.91,
            entity_resolution=(),
            dedupe_key="domain:storage-platform/sku_generation:gen9/first_deployment",
            source_document_key="operator_assertion:operator:2026-01-03",
            corroborating_refs=(),
            batch_id="batch-1",
            created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        ),
        programs_root=knowledge_root.parent / "programs",
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-0",
            kind="approved",
            decided_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
            triage_actor="operator",
        ),
        programs_root=knowledge_root.parent / "programs",
    )
    vault_dir = knowledge_root / "vault" / "ab"
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "abc123").write_text("payload", encoding="utf-8")
    (vault_dir / "abc123.meta.json").write_text("{}", encoding="utf-8")

    summary = summarize_knowledge_status(knowledge_root=knowledge_root)

    assert summary.pending_candidate_count == 1
    assert summary.triaged_candidate_count == 1
    assert summary.vault.file_count == 1
    assert summary.vault.missing_meta_count == 0
    assert summary.vault.hash_mismatch_count == 1
    assert len(summary.scopes) == 1
    assert summary.scopes[0].scope == "domain:storage-platform"
    assert summary.scopes[0].active_claim_count == 1
    assert summary.scopes[0].tombstoned_claim_count == 1
