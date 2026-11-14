from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

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


@pytest.mark.parametrize("source_type", ["workiq_email", "workiq_transcript", "workiq_teams"])
@pytest.mark.parametrize("identity", [None, "", " ", "\t\n", 17])
def test_missing_workiq_identity_is_quarantined(
    tmp_path, source_type: str, identity: object,
) -> None:
    original = _evidence("native-thread")
    evidence = replace(
        original,
        source_refs=(replace(original.source_refs[0], source_type=source_type, canonical_id=identity),),
    )

    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)

    assert load_evidence_records("demo", programs_root=tmp_path) == ()
    quarantine = json.loads(
        (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(encoding="utf-8"),
    )
    assert quarantine["signal_types"] == ["missing_source_identity"]
    assert "source_ids" not in quarantine


@pytest.mark.parametrize("include_manual_ref", [False, True])
def test_explicit_workiq_origin_requires_workiq_ref(tmp_path, include_manual_ref: bool) -> None:
    manual = SourceRef("manual", "Manual note", None, None, canonical_id="manual-id")
    evidence = replace(_evidence("native-thread"), source_refs=(manual,) if include_manual_ref else ())

    with pytest.raises(UnsafeWorkIQEvidenceError, match="missing_source_identity"):
        sanitize_workiq_evidence(evidence)
    assert not persist_evidence(
        evidence, program_id="demo", programs_root=tmp_path, workiq_origin=True,
    )
    assert load_evidence_records("demo", programs_root=tmp_path) == ()
    quarantine = json.loads(
        (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(encoding="utf-8"),
    )
    assert quarantine["signal_types"] == ["missing_source_identity"]
    assert "source_ids" not in quarantine


@pytest.mark.parametrize("invalid_first", [False, True])
def test_mixed_workiq_refs_with_missing_identity_are_quarantined(tmp_path, invalid_first: bool) -> None:
    original = _evidence("private-source-id")
    valid = original.source_refs[0]
    invalid = replace(valid, source_type="workiq_transcript", canonical_id=" ")
    evidence = replace(
        original, source_refs=(invalid, valid) if invalid_first else (valid, invalid),
    )

    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)
    assert load_evidence_records("demo", programs_root=tmp_path) == ()
    quarantine = (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(
        encoding="utf-8",
    )
    assert "missing_source_identity" in quarantine
    assert "private-source-id" not in quarantine


def test_workiq_bundle_with_nonstring_manual_ref_identity_is_quarantined(tmp_path) -> None:
    original = _evidence("native-thread")
    manual = SourceRef("manual", "Manual note", None, None, canonical_id=17)
    evidence = replace(original, source_refs=(*original.source_refs, manual))

    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)
    assert load_evidence_records("demo", programs_root=tmp_path) == ()
    quarantine = json.loads(
        (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(encoding="utf-8"),
    )
    assert quarantine["signal_types"] == ["missing_source_identity"]


def test_quarantine_does_not_copy_source_identifiers(tmp_path) -> None:
    evidence = _evidence("private-source-id", excerpt="password=synthetic-secret")

    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)

    quarantine = (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(
        encoding="utf-8",
    )
    assert json.loads(quarantine)["signal_types"] == ["password"]
    assert "private-source-id" not in quarantine
    assert "synthetic-secret" not in quarantine


def test_existing_quarantine_records_are_not_rewritten(tmp_path) -> None:
    path = tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl"
    path.parent.mkdir(parents=True)
    retained = '{"source_ids":["retained-synthetic-id"]}\n'
    path.write_text(retained, encoding="utf-8")

    assert not persist_evidence(
        _evidence("new-private-id", excerpt="password=synthetic-secret"),
        program_id="demo", programs_root=tmp_path,
    )

    content = path.read_text(encoding="utf-8")
    assert content.startswith(retained)
    assert "new-private-id" not in content
    assert "source_ids" not in json.loads(content.splitlines()[1])


def test_existing_missing_identity_record_remains_unchanged(tmp_path) -> None:
    path = tmp_path / "demo" / "journal" / "evidence_store.jsonl"
    path.parent.mkdir(parents=True)
    retained = json.dumps({
        "lane_id": "lane-a",
        "synthesized_at": "2026-06-21T00:00:00+00:00",
        "risk_level": "LOW",
        "source_refs": [{"source_type": "workiq_email", "description": "retained"}],
    }) + "\n"
    path.write_text(retained, encoding="utf-8")

    records = load_evidence_records("demo", programs_root=tmp_path)

    assert len(records) == 1
    assert records[0].evidence.source_refs[0].canonical_id is None
    assert path.read_text(encoding="utf-8") == retained
    assert not (path.parent / "evidence_quarantine.jsonl").exists()


@pytest.mark.parametrize("include_manual_ref", [False, True])
def test_non_workiq_evidence_keeps_existing_writer_behavior(tmp_path, include_manual_ref: bool) -> None:
    manual = SourceRef("manual", "Manual note", None, None, canonical_id="manual-id")
    evidence = replace(_evidence("native-thread"), source_refs=(manual,) if include_manual_ref else ())

    assert persist_evidence(evidence, program_id="demo", programs_root=tmp_path)
    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)

    stored = load_evidence_records("demo", programs_root=tmp_path)[0].evidence
    assert stored.source_refs == evidence.source_refs
    assert stored.owners == evidence.owners
    assert stored.privacy_classification == "internal"
    assert not (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").exists()


def test_eta_text_is_scrubbed_without_changing_identifier_or_other_values(tmp_path) -> None:
    original = _evidence("thread-1")
    eta = EtaRecord(
        "Delivery owner@example.net", date(2026, 7, 1), "(206) 555-0101", "open",
        "1234567890",
    )
    evidence = replace(original, etas=(eta,))

    assert persist_evidence(evidence, program_id="demo", programs_root=tmp_path)

    stored = load_evidence_records("demo", programs_root=tmp_path)[0].evidence
    assert stored.etas[0].label == "Delivery [PII-FILTERED-EMAIL]"
    assert stored.etas[0].owner is not None
    assert "[PII-FILTERED-PHONE]" in stored.etas[0].owner
    assert "555-0101" not in stored.etas[0].owner
    assert stored.etas[0].ado_id == "1234567890"
    assert stored.etas[0].eta_date == eta.eta_date
    assert stored.etas[0].status == eta.status
    assert evidence.etas[0] == eta
    assert stored.source_refs[0].canonical_id == "thread-1"


@pytest.mark.parametrize(
    "field,value,signal_type",
    [
        ("label", "password=synthetic-secret", "password"),
        ("owner", "api_key=synthetic-secret", "client_secret"),
        ("label", "sensitivity: restricted", "sensitivity_marking"),
        ("owner", "do not distribute", "sensitivity_marking"),
    ],
)
def test_unsafe_eta_text_uses_existing_quarantine(
    tmp_path, field: str, value: str, signal_type: str,
) -> None:
    original = _evidence("thread-1")
    evidence = replace(original, etas=(replace(original.etas[0], **{field: value}),))

    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)

    assert load_evidence_records("demo", programs_root=tmp_path) == ()
    quarantine = (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(
        encoding="utf-8",
    )
    assert signal_type in quarantine
    assert value not in quarantine


@pytest.mark.parametrize(
    "field,value",
    [("label", None), ("label", 42), ("owner", []), ("owner", 42)],
)
def test_malformed_eta_text_is_quarantined(tmp_path, field: str, value: object) -> None:
    original = _evidence("thread-1")
    evidence = replace(original, etas=(replace(original.etas[0], **{field: value}),))

    assert not persist_evidence(evidence, program_id="demo", programs_root=tmp_path)
    assert load_evidence_records("demo", programs_root=tmp_path) == ()
    quarantine = (tmp_path / "demo" / "journal" / "evidence_quarantine.jsonl").read_text(
        encoding="utf-8",
    )
    assert "invalid_eta_text" in quarantine


def test_nullable_eta_owner_and_empty_label_remain_valid() -> None:
    original = _evidence("thread-1")
    evidence = replace(original, etas=(replace(original.etas[0], label="", owner=None),))

    sanitized = sanitize_workiq_evidence(evidence)

    assert sanitized.etas[0].label == ""
    assert sanitized.etas[0].owner is None


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
