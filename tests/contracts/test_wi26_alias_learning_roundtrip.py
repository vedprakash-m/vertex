"""WI-2.6 contract: unresolved entity_ref count surfaces in triage SIGNAL QUALITY.

Round-trip test: facts with unresolved entity_refs → collect_unresolved_entity_refs
detects them → count appears in rendered TriageReport.

Config floor test: empty/absent entities.yaml → system works gracefully (zero unresolved,
no exception).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.entity_registry import EntityRegistry
from src.core.program_reality import CanonicalEntity
from src.core.signal_normalizer import collect_unresolved_entity_refs
from src.core.triage import ReadinessAssessment, TriageReport, render_triage_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(canonical_id: str, alias: str) -> EntityRegistry:
    entity = CanonicalEntity(
        entity_id=canonical_id,
        entity_type="workstream",
        canonical_name="Known Entity",
        aliases=(alias,),
        scope="program",
    )
    return EntityRegistry(program_entities=(entity,), org_entities=())


def _make_snapshot_direct(facts: list) -> MagicMock:
    snapshot = MagicMock()
    snapshot.facts = facts
    return snapshot


def _make_fact(entity_refs: tuple[str, ...]) -> MagicMock:
    fact = MagicMock()
    fact.entity_refs = entity_refs
    fact.fact_id = "pf_test"
    return fact


def _minimal_readiness() -> ReadinessAssessment:
    return ReadinessAssessment(
        score=100,
        quality_gate_pass_rate=100,
        quality_gate_passed=0,
        quality_gate_total=0,
        unreviewed_signal_count=0,
        missing_narrative_count=0,
        missing_override_count=0,
        coverage_gap_count=0,
        written_narrative_count=0,
        total_narrative_count=0,
        set_override_count=0,
        total_override_count=0,
    )


def _minimal_triage_report(**overrides: Any) -> TriageReport:
    defaults: dict[str, Any] = dict(
        edition_name="test_edition",
        issue_number=1,
        program_id="test_prog",
        readiness=_minimal_readiness(),
        blockers=(),
        needs_attention=(),
        milestones=(),
        risks=(),
        actions=(),
        decisions=(),
        assumptions=(),
        cross_program_cascades=(),
        active_issues=(),
        coverage_gaps=(),
        ready=(),
        coverage_gap_window_days=14,
    )
    defaults.update(overrides)
    return TriageReport(**defaults)


# ---------------------------------------------------------------------------
# collect_unresolved_entity_refs tests
# ---------------------------------------------------------------------------

class TestCollectUnresolvedEntityRefs:
    def test_unresolvable_ref_is_detected(self) -> None:
        """Entity_refs not in registry → returned as unresolved."""
        fact = _make_fact(("known-alias", "xyzzy-q4f7-unrelated"))
        snapshot = _make_snapshot_direct([fact])

        registry = _make_registry("entity_001", "known-alias")
        unresolved = collect_unresolved_entity_refs(snapshot, registry)

        assert "xyzzy-q4f7-unrelated" in unresolved
        assert "known-alias" not in unresolved

    def test_all_resolved_gives_empty_set(self) -> None:
        """All entity_refs in registry → frozenset is empty."""
        fact = _make_fact(("known-alias",))
        snapshot = _make_snapshot_direct([fact])

        registry = _make_registry("entity_001", "known-alias")
        unresolved = collect_unresolved_entity_refs(snapshot, registry)

        assert unresolved == frozenset()

    def test_empty_snapshot_gives_empty_set(self) -> None:
        """No facts → no unresolved refs."""
        snapshot = _make_snapshot_direct([])
        registry = EntityRegistry(program_entities=(), org_entities=())
        unresolved = collect_unresolved_entity_refs(snapshot, registry)
        assert unresolved == frozenset()

    def test_empty_registry_marks_all_as_unresolved(self) -> None:
        """Empty registry (no entities loaded) → all entity_refs are unresolved."""
        fact = _make_fact(("ws-alpha", "ws-beta"))
        snapshot = _make_snapshot_direct([fact])

        registry = EntityRegistry(program_entities=(), org_entities=())
        unresolved = collect_unresolved_entity_refs(snapshot, registry)

        assert "ws-alpha" in unresolved
        assert "ws-beta" in unresolved

    def test_fact_with_empty_entity_refs_not_counted(self) -> None:
        """Facts with no entity_refs don't contribute to unresolved count."""
        fact = _make_fact(())
        snapshot = _make_snapshot_direct([fact])

        registry = EntityRegistry(program_entities=(), org_entities=())
        unresolved = collect_unresolved_entity_refs(snapshot, registry)

        assert unresolved == frozenset()

    def test_config_floor_absent_entities_yaml(self, tmp_path: Path) -> None:
        """EntityRegistry.load() with no entities.yaml → empty registry → no exception."""
        (tmp_path / "my_program").mkdir(parents=True)
        registry = EntityRegistry.load("my_program", programs_root=tmp_path)

        fact = _make_fact(("some-ref",))
        snapshot = _make_snapshot_direct([fact])

        unresolved = collect_unresolved_entity_refs(snapshot, registry)
        # "some-ref" is unresolved because registry has no entities — no exception
        assert "some-ref" in unresolved


# ---------------------------------------------------------------------------
# TriageReport SIGNAL QUALITY rendering tests
# ---------------------------------------------------------------------------

class TestTriageReportUnresolvedCount:
    def test_unresolved_count_appears_in_signal_quality(self) -> None:
        """unresolved_entity_ref_count > 0 → surfaces in SIGNAL QUALITY section of rendered report."""
        report = _minimal_triage_report(
            unresolved_entity_ref_count=5,
            auto_approved_signal_count=3,
        )
        rendered = render_triage_report(report)
        assert "SIGNAL QUALITY:" in rendered
        assert "unresolved entity ref(s)" in rendered

    def test_zero_unresolved_omitted_from_signal_quality(self) -> None:
        """All-zero signal quality counters → SIGNAL QUALITY section omitted entirely."""
        report = _minimal_triage_report(
            unresolved_entity_ref_count=0,
            auto_approved_signal_count=0,
            provisional_signal_count=0,
            material_conflict_count=0,
        )
        rendered = render_triage_report(report)
        assert "unresolved entity ref" not in rendered

    def test_unresolved_note_absent_when_only_auto_approved(self) -> None:
        """unresolved_entity_ref_count=0 → 'unresolved entity ref' note absent even with other counts."""
        report = _minimal_triage_report(
            unresolved_entity_ref_count=0,
            auto_approved_signal_count=2,
        )
        rendered = render_triage_report(report)
        assert "SIGNAL QUALITY:" in rendered
        assert "unresolved entity ref" not in rendered

