from __future__ import annotations

from datetime import date, datetime, timezone

from src.commands.gather_pipeline.evidence_extraction_stage import persist_evidence
from src.commands.workiq_evidence_safety import UnsafeWorkIQEvidenceError, sanitize_workiq_evidence
from src.core.evidence_models import EtaRecord, SourceRef, VerificationState, WorkstreamEvidence
from src.core.evidence_store import (
    aggregate_approved_sources,
    load_approved_evidence_by_lane,
    load_evidence_records,
    load_latest_evidence_by_source,
)
from src.core.models import RiskLevel
from src.core.workiq_freshness import mark_workiq_freshness_success, workiq_thread_freshness_hash
from src.m365.workiq_ask_support import DiscoveryRequest
from src.m365.workiq_retriever import retrieve_workiq_threads


def _evidence(source_id: str, *, risk: RiskLevel = RiskLevel.LOW, excerpt: str = "source quote") -> WorkstreamEvidence:
    return WorkstreamEvidence(
        lane_id="lane-a",
        synthesized_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        risk_level=risk,
        etas=(EtaRecord("ship", date(2026, 7, 1), "owner", "open"),),
        blocking_items=("ADO:12345",),
        owners=("owner@example.net",),
        source_refs=(SourceRef(
            "workiq_email",
            "thread evidence",
            date(2026, 6, 20),
            "sender@example.net",
            "https://outlook.office.com/mail/id/abc",
            extraction_method="two_hop",
            canonical_id=source_id,
        ),),
        raw_excerpts=(excerpt,),
        confidence=0.8,
        narrative_summary="A concise source-grounded update.",
        verification_state=VerificationState.MODEL_SELF_ATTESTED,
    )


def test_source_ref_round_trips_new_provenance_fields(tmp_path) -> None:
    assert persist_evidence(_evidence("thread-1"), program_id="demo", programs_root=tmp_path, backing_signal_ids=("s1",))
    record = load_evidence_records("demo", programs_root=tmp_path)[0]
    ref = record.evidence.source_refs[0]
    assert ref.canonical_id == "thread-1"
    assert ref.extraction_method == "two_hop"
    assert record.evidence.privacy_classification == "confidential"


def test_source_keyed_reader_preserves_threads_and_approval_is_per_source(tmp_path) -> None:
    assert persist_evidence(_evidence("thread-1"), program_id="demo", programs_root=tmp_path, backing_signal_ids=("s1",))
    assert persist_evidence(_evidence("thread-2", risk=RiskLevel.HIGH), program_id="demo", programs_root=tmp_path, backing_signal_ids=("s2",))
    assert set(load_latest_evidence_by_source("demo", programs_root=tmp_path)) == {
        ("lane-a", "thread-1"), ("lane-a", "thread-2"),
    }
    approved = load_approved_evidence_by_lane("demo", programs_root=tmp_path, approved_signal_ids={"s1"})
    assert len(approved["lane-a"].source_refs) == 1
    assert approved["lane-a"].source_refs[0].canonical_id == "thread-1"
    assert approved["lane-a"].verification_state is VerificationState.HUMAN_VERIFIED


def test_aggregator_uses_highest_risk_minimum_confidence_and_all_sources() -> None:
    first = _evidence("thread-1", risk=RiskLevel.LOW)
    second = _evidence("thread-2", risk=RiskLevel.BLOCKED)
    combined = aggregate_approved_sources((first, second))
    assert combined.risk_level is RiskLevel.BLOCKED
    assert combined.confidence == 0.8
    assert {ref.canonical_id for ref in combined.source_refs} == {"thread-1", "thread-2"}


def test_privacy_scrubs_pii_and_credentials_fail_closed(tmp_path) -> None:
    safe = sanitize_workiq_evidence(_evidence("thread-1"))
    assert "example.net" not in safe.owners[0]
    unsafe = _evidence("thread-secret", excerpt="password=do-not-store")
    try:
        sanitize_workiq_evidence(unsafe)
    except UnsafeWorkIQEvidenceError as exc:
        assert exc.signal_types == ("password",)
    else:
        raise AssertionError("credential-bearing evidence must be quarantined")
    assert not persist_evidence(unsafe, program_id="demo", programs_root=tmp_path)
    quarantine = (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(encoding="utf-8")
    assert "password" in quarantine
    assert "do-not-store" not in quarantine


def test_freshness_hash_is_stable_and_changes_with_source_revision() -> None:
    first = workiq_thread_freshness_hash(conversation_id="c1", message_count=3, newest_message_identity="m3")
    assert first == workiq_thread_freshness_hash(conversation_id="c1", message_count=3, newest_message_identity="m3")
    assert first != workiq_thread_freshness_hash(conversation_id="c1", message_count=4, newest_message_identity="m4")


def test_retriever_is_bounded_source_keyed_and_skips_unchanged(tmp_path) -> None:
    class Bridge:
        def __init__(self) -> None:
            self.calls = 0

        def ask_workiq(self, question, *, use_cache=False):
            self.calls += 1
            if "Which of my emails" in question:
                return {"response": '{"emails":[{"id":"m1","conversationId":"c1","subject":"Status",'
                        '"from":"sender@example.com","receivedDateTime":"2026-06-20T10:00:00Z",'
                        '"bodyPreview":"Update"}]}'}
            return {"response": '{"risk_level":"low","etas":[],"blocking_items":[],"owners":[],'
                    '"raw_excerpts":["verbatim"],"narrative_summary":"Update","confidence":0.8}'}

    bridge = Bridge()
    request = DiscoveryRequest("Lane A", ("alpha",), date(2026, 6, 1), date(2026, 6, 21), 8)
    cache_path = tmp_path / "freshness.json"
    first = retrieve_workiq_threads(
        bridge=bridge, request=request, top_k=1, max_calls=2,
        max_wall_clock_seconds=30, cache_path=cache_path,
    )
    assert first.calls_made == 2
    assert first.enrichments[0].source_id == "c1"
    mark_workiq_freshness_success(cache_path, "c1")
    second = retrieve_workiq_threads(
        bridge=bridge, request=request, top_k=1, max_calls=2,
        max_wall_clock_seconds=30, cache_path=cache_path,
    )
    assert second.calls_made == 1
    assert second.skipped_unchanged == 1
    assert second.enrichments == ()
