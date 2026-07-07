from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.knowledge_claim_store import append_claim_revision, load_scoped_claim_revisions
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, append_candidate, derive_candidate_dedupe_key, load_pending_candidates
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.source_refs import ADOWorkItemRef, AIInferenceRef, EmailRef, KnowledgeDocumentRef, KustoQueryRef, LTDeckRef, ManualEntryRef, MeetingTranscriptRef, NewsletterRef, OperatorAssertionRef, SharePointDocRef, TeamsMessageRef, WorkIQRef, source_document_key, source_ref_priority, source_ref_requires_vault_hash, validate_typed_source_ref


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REFS_PATH = REPO_ROOT / "src" / "core" / "ledger" / "source_refs.py"


def _source_ref_class_names() -> set[str]:
    tree = ast.parse(SOURCE_REFS_PATH.read_text(encoding="utf-8"), filename=str(SOURCE_REFS_PATH))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.endswith("Ref"):
            continue
        if any(
            (isinstance(decorator, ast.Name) and decorator.id == "dataclass")
            or (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == "dataclass")
            for decorator in node.decorator_list
        ):
            names.add(node.name)
    return names


def _sample_refs() -> dict[str, object]:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    today = now.date()
    return {
        "OperatorAssertionRef": OperatorAssertionRef(asserted_by="operator", asserted_at=now),
        "ADOWorkItemRef": ADOWorkItemRef(org="msft", project="One", work_item_id=12345),
        "KustoQueryRef": KustoQueryRef(cluster="cluster", database="db", query_id="q1", executed_at=now),
        "ManualEntryRef": ManualEntryRef(entered_by="operator", entered_at=now),
        "MeetingTranscriptRef": MeetingTranscriptRef(
            meeting_subject="Weekly Sync",
            meeting_date=today,
            transcript_path="series/instance/transcript",
            vault_hash="sha256:meeting-1",
        ),
        "LTDeckRef": LTDeckRef(file_path="docs/lt.pptx", deck_date=today),
        "NewsletterRef": NewsletterRef(file_path="docs/issue.eml", publication_date=today),
        "EmailRef": EmailRef(
            subject="Escalation",
            sent_at=now,
            sender="owner@example.com",
            message_id="msg-1",
            vault_hash="sha256:email-1",
        ),
        "TeamsMessageRef": TeamsMessageRef(
            posted_at=now,
            message_id="msg-1",
            thread_id="thread-1",
            vault_hash="sha256:teams-1",
        ),
        "SharePointDocRef": SharePointDocRef(
            site="sharepoint/site",
            doc_path="/docs/spec.docx",
            vault_hash="sha256:sp-1",
        ),
        "KnowledgeDocumentRef": KnowledgeDocumentRef(
            vault_hash="sha256:kb-1",
            original_filename="kb.md",
            origin_kind="local_path",
            origin_path="Q:/kb/kb.md",
            ingested_at=now,
        ),
        "WorkIQRef": WorkIQRef(
            artifact_id="artifact-1",
            artifact_kind="email",
            retrieved_at=now,
            vault_hash="sha256:workiq-1",
        ),
        "AIInferenceRef": AIInferenceRef(model="gpt-5.4", inputs=("event:1",), inference_at=now),
    }


def test_source_ref_runtime_variants_match_expected_contract() -> None:
    assert _source_ref_class_names() == {
        "OperatorAssertionRef",
        "ADOWorkItemRef",
        "KustoQueryRef",
        "ManualEntryRef",
        "MeetingTranscriptRef",
        "LTDeckRef",
        "NewsletterRef",
        "EmailRef",
        "TeamsMessageRef",
        "SharePointDocRef",
        "KnowledgeDocumentRef",
        "WorkIQRef",
        "AIInferenceRef",
    }


def test_source_ref_priority_and_vault_policy_cover_every_runtime_variant() -> None:
    refs = _sample_refs()
    mandatory_vault_types = {
        ref.ref_type
        for ref in refs.values()
        if source_ref_requires_vault_hash(ref)
    }

    assert set(refs) == _source_ref_class_names()
    assert all(source_ref_priority(ref) >= 1 for ref in refs.values())
    assert mandatory_vault_types == {
        "meeting_transcript",
        "email",
        "teams_message",
        "sharepoint_doc",
        "workiq",
    }


def test_all_runtime_source_ref_variants_pass_shared_validator_when_valid() -> None:
    for ref in _sample_refs().values():
        validate_typed_source_ref(ref)


def test_all_external_origin_runtime_variants_fail_shared_validator_without_vault_hash() -> None:
    refs = _sample_refs()

    for name, ref in refs.items():
        if not source_ref_requires_vault_hash(ref):
            continue
        with pytest.raises(ValueError, match="vault_hash"):
            validate_typed_source_ref(replace(ref, vault_hash=None))  # type: ignore[arg-type]


def test_all_runtime_source_ref_variants_cover_event_candidate_and_claim_write_paths(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    knowledge_root = tmp_path / "knowledge"
    programs_root = tmp_path / "programs"

    for index, ref in enumerate(_sample_refs().values(), start=1):
        envelope = build_event_envelope(
            program_id="acme",
            event_type="milestone.date_revised.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="contract",
            payload={"milestone_id": f"milestone:m{index}", "new_target_date": "2025-09-30"},
            source_ref=ref,
        )

        candidate = CandidateEvent(
            candidate_id=f"cand-{index}",
            program_id="acme",
            proposed_event_type="milestone.date_revised.v1",
            proposed_payload={"milestone_id": f"milestone:m{index}", "new_target_date": "2025-09-30"},
            proposed_occurred_at=now,
            proposed_temporal_confidence="approximate",
            proposed_confidence="ai_extracted",
            source_ref=ref,
            pipeline="contract",
            extraction_confidence=0.9,
            entity_resolution=(
                CandidateEntityResolution(raw_name=f"Milestone {index}", resolved_entity_id=f"milestone:m{index}", match_kind="exact", score=1.0),
            ),
            dedupe_key=derive_candidate_dedupe_key(source_document_key(ref), f"sha256:core-{index}"),
            dedupe_core_hash=f"sha256:core-{index}",
            source_document_key=source_document_key(ref),
            corroborating_refs=(),
            batch_id="batch-contract",
        )

        append_candidate(candidate, programs_root=programs_root)
        append_claim_revision(
            scope="domain:storage-platform",
            subject=f"sku_generation:gen{index}",
            predicate="first_deployment",
            value=f"2025-Q{(index % 4) + 1}",
            valid_from=now,
            valid_until=None,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            source_ref=ref,
            knowledge_root=knowledge_root,
            recorded_at=now,
        )

        assert envelope.source_ref.ref_type == ref.ref_type

    assert len(load_pending_candidates("acme", programs_root=programs_root)) == len(_sample_refs())
    assert len(load_scoped_claim_revisions(("domain:storage-platform",), knowledge_root=knowledge_root)) == len(_sample_refs())