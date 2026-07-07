"""WI-1.3: Contract tests for ProgramReality authority (§6.1, §6.12).

Tests:
1. ratchet_single_io_point — only load() touches disk (AST scan)
2. ratchet_entity_raises — entity() raises NotImplementedError pre-WI-2.0
3. ratchet_static_truth — management facts → HUMAN_CONFIRMED; rest → RAW_OBSERVED
4. to_dict_envelope_version — to_dict() carries reality_schema_version="1"
5. diff_non_replayable_families — legacy SoR mode lists all management families
6. any_provisional_propagation — any_provisional() is the only sanctioned path
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.program_reality import (
    FactAssessment,
    GapRecord,
    ProgramProjection,
    LedgerTimelineEntry,
    ProgramReality,
    RealityDelta,
    any_provisional,
)
from src.core.knowledge_claim_store import KnowledgeClaimRevision
from src.core.truth_levels import TruthLevel
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef

_REALITY_MODULE = Path("src/core/program_reality.py")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_assessment(truth_level: TruthLevel = TruthLevel.RAW_OBSERVED, provisional: bool = False) -> FactAssessment:
    return FactAssessment(
        record=None,
        fact_id="test-id",
        truth_level=truth_level,
        disputed=False,
        stale=False,
        provisional_inputs=provisional,
        evidence=(),
    )


def _mock_snapshot():
    """Build a minimal ProgramFactSnapshot mock."""
    snapshot = MagicMock()
    snapshot.program_id = "test_program"
    snapshot.facts = []
    return snapshot


def _mock_program_reality(sor_mode: str = "legacy") -> ProgramReality:
    """Build a ProgramReality directly without touching disk."""
    return ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode=sor_mode,
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
    )


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=__import__("datetime").datetime(2025, 3, 20, tzinfo=__import__("datetime").timezone.utc).date())


# ---------------------------------------------------------------------------
# 1. Single I/O point: only load() should call disk-touching functions
# ---------------------------------------------------------------------------

def test_ratchet_single_io_point():
    """AST scan: only load() in program_reality.py may call load_program_facts."""
    source = _REALITY_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    disk_fn = "load_program_facts"
    violations: list[str] = []

    class _DiskCallVisitor(ast.NodeVisitor):
        def __init__(self, outer_func: str | None = None):
            self.outer_func = outer_func

        def visit_FunctionDef(self, node: ast.FunctionDef):
            inner = _DiskCallVisitor(outer_func=node.name)
            inner.generic_visit(node)
            violations.extend(inner.violations())

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_Call(self, node: ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name == disk_fn and self.outer_func not in (None, "load"):
                violations.append(f"  {disk_fn}() called from function '{self.outer_func}'")
            self.generic_visit(node)

        def violations(self):
            return violations

    visitor = _DiskCallVisitor()
    visitor.generic_visit(tree)

    assert not violations, (
        f"{disk_fn}() may only be called from load(). Found violations:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 2. entity() is wired post-WI-2.0 — returns None when not found
# ---------------------------------------------------------------------------

def test_entity_returns_none_when_not_found():
    """entity() returns None for an unresolvable ref post-WI-2.0."""
    pr = _mock_program_reality()
    # With empty entity registry, any ref returns None
    result = pr.entity("nonexistent-person")
    assert result is None


# ---------------------------------------------------------------------------
# 3. Static truth derivation (Phase 1 rules)
# ---------------------------------------------------------------------------

def test_ratchet_static_truth_management_families():
    """Management fact types map to HUMAN_CONFIRMED in Phase 1."""
    from src.core.program_reality import _derive_truth_level_phase1, _MANAGEMENT_FACT_TYPES

    for ft in _MANAGEMENT_FACT_TYPES:
        level = _derive_truth_level_phase1(ft)
        assert level == TruthLevel.HUMAN_CONFIRMED, (
            f"Management fact type '{ft}' should be HUMAN_CONFIRMED, got {level}"
        )


def test_ratchet_static_truth_non_management():
    """Non-management fact types map to RAW_OBSERVED in Phase 1."""
    from src.core.program_reality import _derive_truth_level_phase1

    non_management = [
        "signal.observation",
        "metric.observation",
        "approval.signal",
        "context.note",
    ]
    for ft in non_management:
        level = _derive_truth_level_phase1(ft)
        assert level == TruthLevel.RAW_OBSERVED, (
            f"Non-management fact type '{ft}' should be RAW_OBSERVED, got {level}"
        )


# ---------------------------------------------------------------------------
# 4. to_dict() envelope version
# ---------------------------------------------------------------------------

def test_to_dict_envelope_version():
    """to_dict() must carry reality_schema_version="1"."""
    pr = _mock_program_reality()
    result = pr.to_dict()
    assert result.get("reality_schema_version") == "1", (
        f"to_dict() must include reality_schema_version='1', got: {result.get('reality_schema_version')!r}"
    )
    assert "program_id" in result
    assert "as_of" in result
    assert "sor_mode" in result
    assert "domains" in result


# ---------------------------------------------------------------------------
# 5. diff() non_replayable_families in legacy mode
# ---------------------------------------------------------------------------

def test_diff_non_replayable_families_legacy():
    """diff() with legacy SoR mode must list all management families."""
    from src.core.program_reality import _MANAGEMENT_FACT_TYPES

    pr1 = _mock_program_reality(sor_mode="legacy")
    pr2 = _mock_program_reality(sor_mode="legacy")
    delta = pr1.diff(pr2)

    assert isinstance(delta, RealityDelta)
    assert set(delta.non_replayable_families) == _MANAGEMENT_FACT_TYPES, (
        f"Legacy mode diff must list all management families in non_replayable_families.\n"
        f"Expected: {sorted(_MANAGEMENT_FACT_TYPES)}\n"
        f"Got:      {sorted(delta.non_replayable_families)}"
    )


def test_diff_non_replayable_families_primary():
    """diff() with primary SoR mode must have empty non_replayable_families."""
    pr1 = _mock_program_reality(sor_mode="primary")
    pr2 = _mock_program_reality(sor_mode="primary")
    delta = pr1.diff(pr2)
    assert delta.non_replayable_families == (), (
        f"Primary mode diff should have empty non_replayable_families, got: {delta.non_replayable_families}"
    )


# ---------------------------------------------------------------------------
# 6. any_provisional() is the single sanctioned propagation path
# ---------------------------------------------------------------------------

def test_any_provisional_true_when_any_provisional():
    """any_provisional() returns True if ANY input has provisional_inputs=True."""
    a1 = _make_assessment(provisional=False)
    a2 = _make_assessment(provisional=True)
    a3 = _make_assessment(provisional=False)
    assert any_provisional(a1, a2, a3) is True


def test_load_preserves_fact_provenance_for_projected_records(monkeypatch, tmp_path) -> None:
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        FactReviewState,
        ProgramFactRevision,
        ProgramFactSnapshot,
    )

    as_of = __import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc)
    snapshot = ProgramFactSnapshot(
        program_id="test_program",
        as_of=as_of,
        facts=(
            _fact("action.item", "action-1", as_of),
            _fact("risk.entry", "risk-1", as_of),
            _fact("decision.entry", "decision-1", as_of),
            _fact("dependency.link", "dep-1", as_of),
            _fact("milestone.entry", "milestone-1", as_of),
            _fact("assumption.entry", "assumption-1", as_of),
            _fact("workstream.entry", "ws-1", as_of),
            _fact("claim.entry", "claim-1", as_of),
        ),
    )

    monkeypatch.setattr("src.core.program_reality.load_program_facts", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("src.core.program_reality.resolve_fact_sor_mode", lambda **_kwargs: "legacy", raising=False)
    monkeypatch.setattr("src.core.program_reality.project_action_items", lambda _snapshot: (SimpleNamespace(id="action-1"),))
    monkeypatch.setattr("src.core.program_reality.project_risk_entries", lambda _snapshot: (SimpleNamespace(id="risk-1"),))
    monkeypatch.setattr("src.core.program_reality.project_decision_entries", lambda _snapshot: (SimpleNamespace(id="decision-1"),))
    monkeypatch.setattr("src.core.program_reality.project_dependencies", lambda _snapshot: (SimpleNamespace(id="dep-1"),))
    monkeypatch.setattr("src.core.program_reality.project_milestones", lambda _snapshot: (SimpleNamespace(id="milestone-1"),))
    monkeypatch.setattr("src.core.program_reality.project_assumptions", lambda _snapshot: (SimpleNamespace(id="assumption-1"),))
    monkeypatch.setattr("src.core.program_reality.project_workstreams", lambda _snapshot: (SimpleNamespace(id="ws-1"),))
    monkeypatch.setattr("src.core.program_reality.project_claim_entries", lambda _snapshot: (SimpleNamespace(id="claim-1"),))

    reality = ProgramReality.load("test_program", programs_root=tmp_path / "programs")

    for assessment in (
        *reality.actions(),
        *reality.risks(),
        *reality.decisions(),
        *reality.dependencies(),
        *reality.milestones(),
        *reality.assumptions(),
        *reality.workstreams(),
        *reality.claims(),
    ):
        assert assessment.fact_id is not None
        assert assessment.evidence


def test_ledger_gaps_surface_through_facade_and_attention() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_gaps=(
            GapRecord(
                event_id="01GAP",
                pipeline="lt_deck",
                gap_kind="missing_series_registration",
                detail="Missing series registration for LT deck ingest.",
                window_start=None,
                window_end=None,
                acknowledged=False,
            ),
            GapRecord(
                event_id="01ACKED",
                pipeline="newsletter",
                gap_kind="yield_zero",
                detail="No output for expected weekly window.",
                window_start=None,
                window_end=None,
                acknowledged=True,
            ),
        ),
    )

    assert tuple(gap.event_id for gap in pr.ledger_gaps()) == ("01GAP",)
    assert tuple(gap.event_id for gap in pr.ledger_gaps(unacknowledged_only=False)) == ("01GAP", "01ACKED")

    items = pr.attention()
    assert any("Pipeline 'lt_deck' gap 'missing_series_registration'" in item.description for item in items)


def test_ledger_as_of_surfaces_historical_projection_without_disk_reads() -> None:
    early = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=__import__("datetime").datetime(2025, 1, 5, tzinfo=__import__("datetime").timezone.utc),
        recorded_at=__import__("datetime").datetime(2025, 1, 6, tzinfo=__import__("datetime").timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    backfilled = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=__import__("datetime").datetime(2024, 12, 15, tzinfo=__import__("datetime").timezone.utc),
        recorded_at=__import__("datetime").datetime(2026, 1, 15, tzinfo=__import__("datetime").timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r2", "title": "Historic risk", "severity": "medium"},
        source_ref=_deck_ref(),
    )
    pr = ProgramReality(
        program_id="acme",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_event_log=(early, backfilled),
    )

    visible = pr.ledger_as_of(__import__("datetime").datetime(2025, 1, 31, tzinfo=__import__("datetime").timezone.utc), knowledge_as_of=__import__("datetime").datetime(2026, 12, 31, tzinfo=__import__("datetime").timezone.utc))
    hidden = pr.ledger_as_of(__import__("datetime").datetime(2025, 1, 31, tzinfo=__import__("datetime").timezone.utc), knowledge_as_of=__import__("datetime").datetime(2025, 12, 31, tzinfo=__import__("datetime").timezone.utc))

    assert isinstance(visible, ProgramProjection)
    assert [row["risk_id"] for row in visible.table("proj_risk")] == ["risk:r1", "risk:r2"]
    assert [row["risk_id"] for row in hidden.table("proj_risk")] == ["risk:r1"]


def test_ledger_timeline_surfaces_shadow_annotations() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_events=(
            LedgerTimelineEntry(
                event_id="01EARLY",
                event_type="risk.raised.v1",
                occurred_at=__import__("datetime").datetime(2025, 1, 5, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2025, 1, 6, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="exact",
                source_document_key="lt_deck:deck.pptx:2025-03-20:9",
                shadowed_by="01LATE",
                superseded_by=None,
            ),
            LedgerTimelineEntry(
                event_id="01LATE",
                event_type="risk.raised.v1",
                occurred_at=__import__("datetime").datetime(2025, 1, 7, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2025, 1, 8, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="exact",
                source_document_key="lt_deck:deck.pptx:2025-03-20:9",
                shadowed_by=None,
                superseded_by=None,
            ),
        ),
        _ledger_entity_event_ids={"risk:r1": ("01EARLY", "01LATE")},
    )

    timeline = pr.ledger_timeline("risk:r1")

    assert timeline[0].shadowed_by == "01LATE"
    assert timeline[1].shadowed_by is None


def test_ledger_timeline_surfaces_orphan_annotations() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_events=(
            LedgerTimelineEntry(
                event_id="01CREATE",
                event_type="risk.raised.v1",
                occurred_at=__import__("datetime").datetime(2025, 1, 5, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2025, 1, 6, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="exact",
                source_document_key="lt_deck:deck.pptx:2025-03-20:9",
                orphaned_by=None,
                shadowed_by=None,
                superseded_by="01TOMBSTONE",
            ),
            LedgerTimelineEntry(
                event_id="01UPDATE",
                event_type="risk.status_changed.v1",
                occurred_at=__import__("datetime").datetime(2025, 1, 7, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2025, 1, 8, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="exact",
                source_document_key="lt_deck:deck.pptx:2025-03-20:9",
                orphaned_by="01TOMBSTONE",
                shadowed_by=None,
                superseded_by=None,
            ),
        ),
        _ledger_entity_event_ids={"risk:r1": ("01CREATE", "01UPDATE")},
    )

    timeline = pr.ledger_timeline("risk:r1")

    assert timeline[0].orphaned_by is None
    assert timeline[1].orphaned_by == "01TOMBSTONE"


def test_attention_surfaces_orphaned_ledger_events() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_events=(
            LedgerTimelineEntry(
                event_id="01UPDATE",
                event_type="risk.status_changed.v1",
                occurred_at=__import__("datetime").datetime(2025, 1, 7, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2025, 1, 8, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="exact",
                source_document_key="lt_deck:deck.pptx:2025-03-20:9",
                orphaned_by="01TOMBSTONE",
                shadowed_by=None,
                superseded_by=None,
            ),
        ),
    )

    items = pr.attention()

    orphan_items = [item for item in items if "orphaned event" in item.description]
    assert len(orphan_items) == 1
    assert orphan_items[0].kind == "structural_gap"
    assert orphan_items[0].action_hint.startswith("Review orphaned ledger events")


def test_attention_surfaces_expiring_ledger_field_locks() -> None:
    from src.core.program_reality import LedgerFieldLockRecord

    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_expiring_locks=(
            LedgerFieldLockRecord(
                entity_id="milestone:m1",
                field="target_date",
                valid_until=__import__("datetime").datetime(2026, 6, 15, tzinfo=__import__("datetime").timezone.utc),
            ),
        ),
    )

    items = pr.attention()

    expiry_items = [item for item in items if item.kind == "override_recertification_due"]
    assert len(expiry_items) == 1
    assert "expire within 7 days" in expiry_items[0].description
    assert expiry_items[0].action_hint == "Re-confirm the expiring lock or release it before expiry."


def test_attention_surfaces_write_order_tiebreak_ledger_reviews() -> None:
    from src.core.program_reality import LedgerShadowReviewRecord

    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_shadow_reviews=(
            LedgerShadowReviewRecord(
                event_id="01LOSER",
                shadowed_by="01WINNER",
                field_name="title",
            ),
        ),
    )

    items = pr.attention()

    review_items = [item for item in items if item.kind == "ledger_conflict_review"]
    assert len(review_items) == 1
    assert "write-order tiebreaks" in review_items[0].description
    assert review_items[0].action_hint == "Review the tied ledger evidence and confirm the winning value."


def test_attention_surfaces_stale_operator_assertion_reviews() -> None:
    from src.core.program_reality import LedgerStaleOperatorAssertionRecord

    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_stale_operator_assertions=(
            LedgerStaleOperatorAssertionRecord(
                event_id="01SOURCE",
                shadowed_by="01ASSERTION",
                field_name="title",
                asserted_at=__import__("datetime").datetime(2026, 4, 1, tzinfo=__import__("datetime").timezone.utc),
            ),
        ),
    )

    items = pr.attention()

    review_items = [item for item in items if item.kind == "operator_assertion_stale"]
    assert len(review_items) == 1
    assert "older than 30 days" in review_items[0].description
    assert review_items[0].action_hint == "Confirm the operator assertion or let it expire."


def test_attention_surfaces_weaker_temporal_confidence_reviews() -> None:
    from src.core.program_reality import LedgerTemporalConfidenceReviewRecord

    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_temporal_reviews=(
            LedgerTemporalConfidenceReviewRecord(
                event_id="01EXACT",
                shadowed_by="01APPROX",
                field_name="target_date",
                winner_temporal_confidence="approximate",
                loser_temporal_confidence="exact",
            ),
        ),
    )

    items = pr.attention()

    review_items = [item for item in items if item.kind == "temporal_confidence_review"]
    assert len(review_items) == 1
    assert "weaker temporal confidence" in review_items[0].description
    assert review_items[0].action_hint == "Review the date certainty and confirm the winning value ordering."


def test_attention_surfaces_latest_live_stale_claim_citations() -> None:
    from src.core.program_reality import KnowledgeClaimFreshnessRecord

    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _knowledge_claim_freshness=KnowledgeClaimFreshnessRecord(
            issue_number=77,
            claim_ids=("01CLAIM", "02CLAIM"),
        ),
    )

    items = pr.attention()

    review_items = [item for item in items if item.kind == "claim_freshness"]
    assert len(review_items) == 1
    assert "issue 077" in review_items[0].description
    assert "expired/stale claim" in review_items[0].description
    assert review_items[0].action_hint == "Refresh or remove stale claim citations from the persisted proposal evidence."


def test_load_surfaces_latest_confirmed_archive_stale_claim_citations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    from src.core.program_fact_store import ProgramFactSnapshot

    as_of = datetime(2026, 6, 11, tzinfo=timezone.utc)
    snapshot = ProgramFactSnapshot(program_id="test_program", as_of=as_of, facts=())

    monkeypatch.setattr("src.core.program_fact_store.resolve_fact_sor_mode", lambda **_kwargs: "legacy")
    monkeypatch.setattr("src.core.program_reality.load_program_facts", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("src.core.program_reality.project_action_items", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_risk_entries", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_decision_entries", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_dependencies", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_milestones", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_assumptions", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_workstreams", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.project_claim_entries", lambda _snapshot: ())
    monkeypatch.setattr("src.core.program_reality.load_triage_decisions", lambda *_args, **_kwargs: ())
    monkeypatch.setattr("src.core.program_reality.read_events", lambda *_args, **_kwargs: ())
    monkeypatch.setattr("src.core.program_reality.load_program_knowledge_scopes", lambda *_args, **_kwargs: ())
    monkeypatch.setattr("src.core.program_reality.load_program_knowledge_claims", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        "src.core.program_reality.summarize_knowledge_status",
        lambda **_kwargs: SimpleNamespace(vault=SimpleNamespace(hash_mismatch_count=0)),
    )
    monkeypatch.setattr(
        "src.core.program_reality.summarize_knowledge_vault_integrity",
        lambda **_kwargs: SimpleNamespace(issue_records=lambda: []),
    )
    monkeypatch.setattr("src.core.program_reality.project_events_to_memory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("src.core.program_reality.load_entity_event_ids", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("src.core.program_reality.load_indexed_events", lambda *_args, **_kwargs: ())
    monkeypatch.setattr("src.core.program_reality.find_latest_confirmed_entry", lambda _index: SimpleNamespace(issue_number=12))
    monkeypatch.setattr("src.core.program_reality.read_archive_index", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr("src.core.program_reality.load_archived_stale_claim_ids", lambda _path: ("01ARCH", "02ARCH"))

    reality = ProgramReality.load(
        "test_program",
        programs_root=tmp_path / "programs",
        edition_name="demo_weekly",
        archive_root=tmp_path / "archive",
    )

    items = reality.attention()

    review_items = [item for item in items if item.kind == "claim_freshness"]
    assert len(review_items) == 1
    assert "Latest confirmed issue 012" in review_items[0].description
    assert "01ARCH, 02ARCH" in review_items[0].description
    assert review_items[0].action_hint == "Refresh or remove stale claim citations from the archived accepted proposal evidence."


def test_attention_surfaces_knowledge_vault_hash_mismatches() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _knowledge_vault_hash_mismatch_count=2,
    )

    items = pr.attention()

    review_items = [item for item in items if item.kind == "knowledge_vault_integrity"]
    assert len(review_items) == 1
    assert "integrity issues" in review_items[0].description
    assert "2 file(s) with content hash mismatches" in review_items[0].description
    assert review_items[0].action_hint == "Repair or re-ingest the affected knowledge-vault entries before relying on knowledge claims."


def test_attention_surfaces_knowledge_vault_source_registry_gaps() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 6, 11, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _knowledge_vault_integrity_issues=(
            {"kind": "missing_source_record", "count": 1},
        ),
    )

    items = pr.attention()

    review_items = [item for item in items if item.kind == "knowledge_vault_integrity"]
    assert len(review_items) == 1
    assert "dangling source registry record" in review_items[0].description


def test_knowledge_context_surfaces_period_correct_resolved_claims() -> None:
    pr = ProgramReality(
        program_id="acme",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime(2026, 2, 1, tzinfo=__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _knowledge_scope_chain=("program:acme", "domain:storage-platform", "org"),
        _knowledge_claim_revisions=(
            KnowledgeClaimRevision(
                claim_id="01DOMAIN",
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H1",
                valid_from=__import__("datetime").datetime(2025, 1, 1, tzinfo=__import__("datetime").timezone.utc),
                valid_until=None,
                recorded_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)),
                supersedes=None,
                natural_key="domain:storage-platform/sku_generation:gen9/first_deployment",
            ),
        ),
    )

    context = pr.knowledge_context(("sku_generation:gen9",), as_of=__import__("datetime").datetime(2026, 2, 1, tzinfo=__import__("datetime").timezone.utc))

    entry = context.entry("sku_generation:gen9")
    assert entry is not None
    assert entry.projection_coverage == "absent"
    assert entry.claims[0].claim_id == "01DOMAIN"
    assert entry.claims[0].value == "2025-H1"


def test_ledger_timeline_surfaces_entity_events_in_occurred_order() -> None:
    pr = ProgramReality(
        program_id="test_program",
        snapshot=_mock_snapshot(),
        sor_mode="legacy",
        as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _ledger_events=(
            LedgerTimelineEntry(
                event_id="02LATE",
                event_type="milestone.date_revised.v1",
                occurred_at=__import__("datetime").datetime(2025, 2, 1, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="approximate",
                source_document_key="lt_deck:2025-02",
                superseded_by=None,
            ),
            LedgerTimelineEntry(
                event_id="01EARLY",
                event_type="milestone.created.v1",
                occurred_at=__import__("datetime").datetime(2025, 1, 1, tzinfo=__import__("datetime").timezone.utc),
                recorded_at=__import__("datetime").datetime(2025, 1, 2, tzinfo=__import__("datetime").timezone.utc),
                actor="import",
                confidence="source_authoritative",
                temporal_confidence="exact",
                source_document_key="lt_deck:2025-01",
                superseded_by=None,
            ),
        ),
        _ledger_entity_event_ids={"milestone:m1": ("02LATE", "01EARLY")},
    )

    timeline = pr.ledger_timeline("milestone:m1")

    assert tuple(entry.event_id for entry in timeline) == ("01EARLY", "02LATE")


def _fact(fact_type: str, entity_ref: str, recorded_at):
    from src.core.program_fact_store import FactLifecycleState, FactPrecedence, FactReviewState, ProgramFactRevision

    return ProgramFactRevision(
        revision_id=f"rev-{entity_ref}",
        fact_id=f"fact-{entity_ref}",
        program_id="test_program",
        natural_key=entity_ref,
        fact_type=fact_type,
        scope="program",
        entity_refs=(entity_ref,),
        payload={"id": entity_ref},
        source_signal_ids=(f"signal-{entity_ref}",),
        confidence="high",
        precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=recorded_at,
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="test",
    )


def test_any_provisional_false_when_none_provisional():
    """any_provisional() returns False if NO input has provisional_inputs=True."""
    a1 = _make_assessment(provisional=False)
    a2 = _make_assessment(provisional=False)
    assert any_provisional(a1, a2) is False


def test_any_provisional_empty():
    """any_provisional() returns False for empty input (vacuously False)."""
    assert any_provisional() is False


# ---------------------------------------------------------------------------
# 7. Registry entry exists for program_reality
# ---------------------------------------------------------------------------

def test_program_reality_in_state_reader_registry():
    """state_reader_registry must have an entry for 'program_reality'."""
    from src.core.state_reader_registry import STATE_READER_REGISTRY

    assert "program_reality" in STATE_READER_REGISTRY, (
        "state_reader_registry.STATE_READER_REGISTRY must have a 'program_reality' entry (WI-1.1)."
    )
    entry = STATE_READER_REGISTRY["program_reality"]
    assert entry.owner_module == "src.core.program_reality"
    assert "ProgramReality" in entry.reader_symbols


# ---------------------------------------------------------------------------
# WI-3.4: INV-16 quote guard, any_provisional call-sites, conflicts, provisional-inputs
# ---------------------------------------------------------------------------


def test_inv16_provisional_flag_exists_on_attention_item():
    """AttentionItem must carry a provisional_inputs field (INV-16)."""
    from src.core.program_reality import AttentionItem, AttentionKind

    item = AttentionItem(
        kind=AttentionKind.DISPUTED_FACT,
        priority=1,
        record=None,
        description="test",
        action_hint="",
        provisional_inputs=True,
    )
    assert item.provisional_inputs is True


def test_inv16_provisional_inputs_propagated_via_any_provisional():
    """any_provisional() is the ONLY sanctioned path for provisional flag propagation.

    The test seeds two assessments: one provisional, one not. Verifies that
    any_provisional() returns True only when a provisional is present (INV-16).
    """
    non_prov = _make_assessment(provisional=False)
    prov = _make_assessment(provisional=True)

    assert any_provisional(non_prov) is False
    assert any_provisional(prov) is True
    assert any_provisional(non_prov, prov) is True
    assert any_provisional(non_prov, non_prov) is False


def test_conflicts_returns_empty_for_empty_snapshot():
    """conflicts() returns empty tuple when no fact.conflict facts exist."""
    pr = _mock_program_reality()
    assert pr.conflicts() == ()


def test_conflicts_surfaces_unresolved_conflict():
    """conflicts() returns RealityConflict for unresolved fact.conflict facts."""
    from unittest.mock import MagicMock, patch
    from datetime import datetime, timezone
    from src.core.program_fact_store import (
        FactLifecycleState, FactPrecedence, FactReviewState, ProgramFactRevision, ProgramFactSnapshot
    )
    from src.core.program_reality import ProgramReality, RealityConflict

    conflict_fact = ProgramFactRevision(
        revision_id="rev_conf",
        fact_id="pf_conf",
        program_id="test_program",
        natural_key="conflict:action:A",
        fact_type="fact.conflict",
        scope="program",
        entity_refs=("action:A",),
        payload={
            "family": "action",
            "description": "ADO says done, Teams says in-progress",
            "resolved": False,
            "is_material": True,
        },
        source_signal_ids=(),
        confidence=None,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=datetime.now(timezone.utc),
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="test",
    )
    snap = ProgramFactSnapshot(
        program_id="test_program",
        as_of=datetime.now(timezone.utc),
        facts=(conflict_fact,),
    )
    pr = ProgramReality(
        program_id="test_program",
        snapshot=snap,
        sor_mode="legacy",
        as_of=datetime.now(timezone.utc),
        _entity_fact_index={},
        _actions=(),
        _risks=(),
        _decisions=(),
        _dependencies=(),
        _milestones=(),
        _assumptions=(),
        _workstreams=(),
        _claims=(),
    )
    conflicts = pr.conflicts(open_only=True)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert isinstance(c, RealityConflict)
    assert c.open is True
    assert c.family == "action"


def test_conflicts_excludes_resolved_when_open_only():
    """conflicts(open_only=True) excludes resolved fact.conflict facts."""
    from datetime import datetime, timezone
    from src.core.program_fact_store import (
        FactLifecycleState, FactPrecedence, FactReviewState, ProgramFactRevision, ProgramFactSnapshot
    )
    from src.core.program_reality import ProgramReality

    resolved_fact = ProgramFactRevision(
        revision_id="rev_res",
        fact_id="pf_res",
        program_id="test_program",
        natural_key="conflict:action:B",
        fact_type="fact.conflict",
        scope="program",
        entity_refs=("action:B",),
        payload={"family": "action", "description": "resolved", "resolved": True},
        source_signal_ids=(),
        confidence=None,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=datetime.now(timezone.utc),
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="test",
    )
    snap = ProgramFactSnapshot(
        program_id="test_program",
        as_of=datetime.now(timezone.utc),
        facts=(resolved_fact,),
    )
    pr = ProgramReality(
        program_id="test_program",
        snapshot=snap,
        sor_mode="legacy",
        as_of=datetime.now(timezone.utc),
        _entity_fact_index={},
        _actions=(), _risks=(), _decisions=(), _dependencies=(),
        _milestones=(), _assumptions=(), _workstreams=(), _claims=(),
    )
    assert pr.conflicts(open_only=True) == ()
    # open_only=False returns it
    assert len(pr.conflicts(open_only=False)) == 1


# ---------------------------------------------------------------------------
# WI-1.2: Ratchet — projection commands must not call load_program_facts
# ---------------------------------------------------------------------------

def test_ratchet_projection_commands_no_direct_load_program_facts():
    """AST ratchet: risk/triage/report commands must not call load_program_facts().

    WI-1.2: Projection commands (risk, triage, report) must route all fact I/O through
    ProgramReality.load() — the single sanctioned I/O point for projections.
    Direct load_program_facts() calls in these commands indicate a regression.
    """
    _PROJECTION_COMMANDS = [
        Path("src/commands/risks.py"),
        Path("src/commands/triage.py"),
        Path("src/commands/report_ai.py"),
        Path("src/commands/report_deck.py"),
        Path("src/commands/report_health.py"),
        Path("src/commands/report_lookback.py"),
        Path("src/commands/report_scorecards.py"),
        Path("src/commands/deck_companion.py"),
    ]
    violations: list[str] = []
    for cmd_path in _PROJECTION_COMMANDS:
        source = cmd_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name == "load_program_facts":
                    violations.append(f"  {cmd_path}: direct load_program_facts() call found")

    assert not violations, (
        "WI-1.2 ratchet: projection commands must use ProgramReality.load(), "
        "not direct load_program_facts().\nViolations:\n" + "\n".join(violations)
    )
