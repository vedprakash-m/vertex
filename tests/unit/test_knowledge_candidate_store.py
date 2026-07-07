from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.knowledge_candidate_store import KnowledgeCandidateDecisionRecord, KnowledgeCandidateEntityResolution, active_candidates, append_candidate, append_triage_decision, build_candidate, load_pending_candidates, load_triage_decisions
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import OperatorAssertionRef


def test_active_candidates_excludes_final_decisions(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    candidate = build_candidate(
        candidate_id="cand-1",
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        pipeline="extract",
        extraction_confidence=0.8,
        entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
        corroborating_refs=(),
        batch_id="batch-1",
    )

    append_candidate(candidate, programs_root=programs_root)
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-1",
            kind="rejected",
            decided_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            triage_actor="operator",
        ),
        programs_root=programs_root,
    )

    assert load_pending_candidates(programs_root=programs_root)[0].candidate_id == "cand-1"
    assert load_triage_decisions(programs_root=programs_root)[0].kind == "rejected"
    assert active_candidates(programs_root=programs_root) == ()


def test_active_candidates_can_filter_by_batch_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = build_candidate(
        candidate_id="cand-1",
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        pipeline="extract",
        extraction_confidence=0.8,
        entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
        corroborating_refs=(),
        batch_id="batch-1",
    )
    second = build_candidate(
        candidate_id="cand-2",
        scope="domain:storage-platform",
        subject="sku_generation:gen10",
        predicate="first_deployment",
        value="2026-H1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        pipeline="extract",
        extraction_confidence=0.7,
        entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen10", resolved_entity_id="sku_generation:gen10", match_kind="exact", score=1.0),),
        corroborating_refs=(),
        batch_id="batch-2",
    )

    append_candidate(first, programs_root=programs_root)
    append_candidate(second, programs_root=programs_root)

    assert [candidate.candidate_id for candidate in active_candidates(programs_root=programs_root, batch_id="batch-1")] == ["cand-1"]


def test_load_pending_candidates_round_trips_created_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    created_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
    candidate = build_candidate(
        candidate_id="cand-1",
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        pipeline="extract",
        extraction_confidence=0.8,
        entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
        corroborating_refs=(),
        batch_id="batch-1",
        created_at=created_at,
    )

    append_candidate(candidate, programs_root=programs_root)

    loaded = load_pending_candidates(programs_root=programs_root)
    assert len(loaded) == 1
    assert loaded[0].created_at == created_at