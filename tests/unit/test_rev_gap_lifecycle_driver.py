"""REV-G6 gap-fill loop driver tests (P2-6).

Exercises ``_drive_gap_lifecycle`` (the helper wired into the REV pipeline's
verification step 9) directly with duck-typed staged/claim objects, plus one
full-cycle integration test that confirms the driver fires inside
``run_rev_cycle`` and that ``gap_transitions`` surfaces in the report.

Matching rule under test: a ``ContextGapRecord.metadata["event_types"]`` list
intersects the claim's material ``event_type``.

Transition semantics under test:
* open → filling  (candidate staged; verifier state irrelevant)
* filling → resolved  (only when the candidate reached a verified state:
  ``source_verified`` in-cycle or ``human_verified`` at triage)
* already-resolved gaps are not re-transitioned (no spurious log entry)
* non-matching gaps are untouched
* the driver is best-effort: an empty gap store returns 0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.ledger.gap_lifecycle import (
    GapLifecycleStore,
    GapMatchCriteria,
    GapStatus,
    ContextGapRecord,
)
from src.core.rev.pipeline import _drive_gap_lifecycle


NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _ref(vault_hash: str = "sha256:evidence-abc") -> SimpleNamespace:
    return SimpleNamespace(vault_hash=vault_hash)


def _staged(candidate_id: str, vault_hash: str = "sha256:evidence-abc") -> SimpleNamespace:
    return SimpleNamespace(candidate_id=candidate_id, evidence_refs=(_ref(vault_hash),))


def _claim(event_type: str) -> SimpleNamespace:
    return SimpleNamespace(event_type=event_type)


def _seed_gap(
    programs_root: Path,
    program_id: str,
    *,
    gap_id: str = "gap-1",
    event_types: list[str],
    status: str = GapStatus.OPEN.value,
) -> ContextGapRecord:
    # Merge into any existing store so multiple gaps per program persist.
    store = GapLifecycleStore.load(program_id, programs_root=programs_root)
    gap = ContextGapRecord(
        gap_id=gap_id,
        description="test gap",
        metadata={"event_types": event_types},
        status=status,
    )
    store.upsert(gap)
    store.save(program_id, programs_root=programs_root)
    return gap


class TestDriveGapLifecycleDirect:
    def test_open_to_filling_when_candidate_staged_unverified(self, tmp_path: Path) -> None:
        """A staged candidate advances an open gap to filling even when the
        verifier returns ``unverified`` (material claims need human triage)."""
        _seed_gap(tmp_path, "p1", event_types=["deployment.completed"])
        transitions = _drive_gap_lifecycle(
            program_id="p1",
            staged_list=[_staged("c1")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c1": "unverified"},
            programs_root=tmp_path,
        )
        gap = GapLifecycleStore.load("p1", programs_root=tmp_path).get("gap-1")
        assert gap is not None
        assert gap.status == GapStatus.FILLING.value
        assert transitions == 1  # one status transition (open→filling)

    def test_open_to_filling_to_resolved_when_source_verified(self, tmp_path: Path) -> None:
        """When the verifier returns a verified state, the gap advances all the
        way to resolved in the same cycle (two transitions)."""
        _seed_gap(tmp_path, "p2", event_types=["deployment.completed"])
        transitions = _drive_gap_lifecycle(
            program_id="p2",
            staged_list=[_staged("c2", "sha256:res-ref")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c2": "source_verified"},
            programs_root=tmp_path,
        )
        gap = GapLifecycleStore.load("p2", programs_root=tmp_path).get("gap-1")
        assert gap is not None
        assert gap.status == GapStatus.RESOLVED.value
        assert gap.resolution_evidence_ref == "sha256:res-ref"
        assert transitions == 2  # open→filling + filling→resolved

    def test_human_verified_also_resolves(self, tmp_path: Path) -> None:
        _seed_gap(tmp_path, "p3", event_types=["milestone.completed"])
        _drive_gap_lifecycle(
            program_id="p3",
            staged_list=[_staged("c3")],
            claims=(_claim("milestone.completed"),),
            verifier_results={"c3": "human_verified"},
            programs_root=tmp_path,
        )
        gap = GapLifecycleStore.load("p3", programs_root=tmp_path).get("gap-1")
        assert gap is not None
        assert gap.status == GapStatus.RESOLVED.value

    def test_already_resolved_gap_not_re_transitioned(self, tmp_path: Path) -> None:
        """A gap already resolved in a prior cycle must not get a spurious
        resolved→resolved transition entry, and contributes 0 transitions."""
        _seed_gap(
            tmp_path, "p4", event_types=["deployment.completed"],
            status=GapStatus.RESOLVED.value,
        )
        transitions = _drive_gap_lifecycle(
            program_id="p4",
            staged_list=[_staged("c4")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c4": "source_verified"},
            programs_root=tmp_path,
        )
        gap = GapLifecycleStore.load("p4", programs_root=tmp_path).get("gap-1")
        assert gap is not None
        assert gap.status == GapStatus.RESOLVED.value
        # No new transitions appended.
        assert transitions == 0
        assert len(gap.transitions) == 0

    def test_non_matching_gap_untouched(self, tmp_path: Path) -> None:
        """A gap tracking a different event type is not advanced."""
        _seed_gap(tmp_path, "p5", event_types=["incident.severity_changed"])
        transitions = _drive_gap_lifecycle(
            program_id="p5",
            staged_list=[_staged("c5")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c5": "source_verified"},
            programs_root=tmp_path,
        )
        gap = GapLifecycleStore.load("p5", programs_root=tmp_path).get("gap-1")
        assert gap is not None
        assert gap.status == GapStatus.OPEN.value
        assert transitions == 0

    def test_empty_gap_store_returns_zero(self, tmp_path: Path) -> None:
        """No tracked gaps → nothing to drive (the common case)."""
        transitions = _drive_gap_lifecycle(
            program_id="p-empty",
            staged_list=[_staged("c6")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c6": "source_verified"},
            programs_root=tmp_path,
        )
        assert transitions == 0

    def test_empty_staged_list_returns_zero(self, tmp_path: Path) -> None:
        _seed_gap(tmp_path, "p7", event_types=["deployment.completed"])
        transitions = _drive_gap_lifecycle(
            program_id="p7",
            staged_list=[],
            claims=(),
            verifier_results={},
            programs_root=tmp_path,
        )
        assert transitions == 0

    def test_reopened_gap_advances_to_filling(self, tmp_path: Path) -> None:
        """A reopened gap (resolved earlier, now relevant again) advances to
        filling when a fresh candidate stages."""
        _seed_gap(
            tmp_path, "p8", event_types=["deployment.completed"],
            status=GapStatus.REOPENED.value,
        )
        transitions = _drive_gap_lifecycle(
            program_id="p8",
            staged_list=[_staged("c8")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c8": "unverified"},
            programs_root=tmp_path,
        )
        gap = GapLifecycleStore.load("p8", programs_root=tmp_path).get("gap-1")
        assert gap is not None
        assert gap.status == GapStatus.FILLING.value
        assert transitions == 1

    def test_multiple_gaps_match_one_claim(self, tmp_path: Path) -> None:
        """Two gaps tracking the same event type both advance from one claim."""
        _seed_gap(tmp_path, "p9", gap_id="g-a", event_types=["deployment.completed"])
        _seed_gap(tmp_path, "p9", gap_id="g-b", event_types=["deployment.completed"])

        transitions = _drive_gap_lifecycle(
            program_id="p9",
            staged_list=[_staged("c9")],
            claims=(_claim("deployment.completed"),),
            verifier_results={"c9": "unverified"},
            programs_root=tmp_path,
        )
        store = GapLifecycleStore.load("p9", programs_root=tmp_path)
        assert store.get("g-a").status == GapStatus.FILLING.value
        assert store.get("g-b").status == GapStatus.FILLING.value
        assert transitions == 2  # one transition per matching gap


# ---------------------------------------------------------------------------
# W2-11: GapMatchCriteria, contradiction reopening, staleness reopening
# ---------------------------------------------------------------------------


def _resolved_gap_with_criteria(
    gap_id: str = "g-mc-1",
    *,
    entity_id: str = "MILESTONE:ms-001",
    fact_family: str = "milestone",
    fact_field: str = "status",
    expected_value: str = "complete",
    stale_after_days: int | None = None,
) -> ContextGapRecord:
    crit = GapMatchCriteria(
        program_id="p-mc",
        entity_id=entity_id,
        fact_family=fact_family,
        fact_field=fact_field,
        expected_value=expected_value,
        stale_after_days=stale_after_days,
    )
    gap = ContextGapRecord(
        gap_id=gap_id,
        description="milestone completion gap",
        match_criteria=crit,
    )
    gap.transition_to(GapStatus.RESOLVED, evidence_ref="sha256:ev-abc")
    return gap


class TestGapMatchCriteria:
    def test_matches_fact_exact(self) -> None:
        crit = GapMatchCriteria(
            program_id="p1", entity_id="MILESTONE:ms-1", fact_family="milestone"
        )
        assert crit.matches_fact("milestone", "MILESTONE:ms-1")

    def test_matches_fact_family_mismatch(self) -> None:
        crit = GapMatchCriteria(program_id="p1", fact_family="milestone")
        assert not crit.matches_fact("risk", "ANY")

    def test_matches_fact_entity_mismatch(self) -> None:
        crit = GapMatchCriteria(program_id="p1", entity_id="MILESTONE:ms-1", fact_family="milestone")
        assert not crit.matches_fact("milestone", "MILESTONE:ms-999")

    def test_matches_fact_none_entity_matches_any(self) -> None:
        crit = GapMatchCriteria(program_id="p1", fact_family="milestone")
        assert crit.matches_fact("milestone", "MILESTONE:ms-999")

    def test_is_contradicted_expected_value_matches(self) -> None:
        crit = GapMatchCriteria(
            program_id="p1", fact_family="milestone",
            fact_field="status", expected_value="complete",
        )
        assert not crit.is_contradicted_by({"status": "complete"})

    def test_is_contradicted_value_differs(self) -> None:
        crit = GapMatchCriteria(
            program_id="p1", fact_family="milestone",
            fact_field="status", expected_value="complete",
        )
        assert crit.is_contradicted_by({"status": "at_risk"})

    def test_is_contradicted_field_absent_is_not_contradiction(self) -> None:
        crit = GapMatchCriteria(
            program_id="p1", fact_family="milestone",
            fact_field="status", expected_value="complete",
        )
        assert not crit.is_contradicted_by({})

    def test_is_contradicted_no_field_configured_returns_false(self) -> None:
        crit = GapMatchCriteria(program_id="p1", fact_family="milestone")
        assert not crit.is_contradicted_by({"status": "at_risk"})

    def test_roundtrip_serialization(self) -> None:
        crit = GapMatchCriteria(
            program_id="px",
            entity_id="MILESTONE:ms-1",
            fact_family="milestone",
            fact_field="status",
            expected_value="complete",
            stale_after_days=30,
        )
        restored = GapMatchCriteria.from_dict(crit.to_dict())
        assert restored == crit


class TestContradictionReopening:
    def test_resolved_gap_reopens_on_contradiction(self) -> None:
        gap = _resolved_gap_with_criteria(expected_value="complete")
        assert gap.check_contradiction("milestone", "MILESTONE:ms-001", {"status": "at_risk"})

    def test_resolved_gap_not_reopened_when_consistent(self) -> None:
        gap = _resolved_gap_with_criteria(expected_value="complete")
        assert not gap.check_contradiction("milestone", "MILESTONE:ms-001", {"status": "complete"})

    def test_open_gap_never_flagged_as_contradiction(self) -> None:
        crit = GapMatchCriteria(
            program_id="p1", entity_id="MILESTONE:ms-001", fact_family="milestone",
            fact_field="status", expected_value="complete",
        )
        gap = ContextGapRecord(gap_id="g-open", description="open gap", match_criteria=crit)
        assert gap.status == GapStatus.OPEN.value
        assert not gap.check_contradiction("milestone", "MILESTONE:ms-001", {"status": "at_risk"})

    def test_entity_mismatch_not_flagged(self) -> None:
        gap = _resolved_gap_with_criteria(entity_id="MILESTONE:ms-001", expected_value="complete")
        assert not gap.check_contradiction("milestone", "MILESTONE:ms-999", {"status": "at_risk"})

    def test_evaluate_contradictions_returns_matching_gaps(self, tmp_path: Path) -> None:
        store = GapLifecycleStore()
        store.upsert(_resolved_gap_with_criteria("g1", expected_value="complete"))
        store.upsert(_resolved_gap_with_criteria("g2", entity_id="MILESTONE:ms-002", expected_value="complete"))
        hits = store.evaluate_contradictions("milestone", "MILESTONE:ms-001", {"status": "at_risk"})
        assert len(hits) == 1
        assert hits[0][0].gap_id == "g1"
        assert "at_risk" in hits[0][1]

    def test_gap_round_trips_with_match_criteria(self, tmp_path: Path) -> None:
        gap = _resolved_gap_with_criteria(stale_after_days=7)
        store = GapLifecycleStore()
        store.upsert(gap)
        store.save("p-rt", programs_root=tmp_path)
        loaded = GapLifecycleStore.load("p-rt", programs_root=tmp_path)
        restored = loaded.get(gap.gap_id)
        assert restored is not None
        assert restored.match_criteria is not None
        assert restored.match_criteria.stale_after_days == 7
        assert restored.match_criteria.expected_value == "complete"


class TestStalenessReopening:
    def test_is_stale_when_past_window(self) -> None:
        gap = _resolved_gap_with_criteria(stale_after_days=7)
        nine_days_later = gap.resolved_at + timedelta(days=9)
        assert gap.is_stale(nine_days_later)

    def test_is_not_stale_when_within_window(self) -> None:
        gap = _resolved_gap_with_criteria(stale_after_days=7)
        three_days_later = gap.resolved_at + timedelta(days=3)
        assert not gap.is_stale(three_days_later)

    def test_no_stale_after_days_never_stale(self) -> None:
        gap = _resolved_gap_with_criteria(stale_after_days=None)
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert not gap.is_stale(far_future)

    def test_open_gap_never_stale(self) -> None:
        crit = GapMatchCriteria(program_id="p1", fact_family="milestone", stale_after_days=1)
        gap = ContextGapRecord(gap_id="g-open", description="open", match_criteria=crit)
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert not gap.is_stale(far_future)

    def test_evaluate_stale_gaps_returns_expired(self) -> None:
        store = GapLifecycleStore()
        gap = _resolved_gap_with_criteria("g-stale", stale_after_days=7)
        store.upsert(gap)
        nine_days_later = gap.resolved_at + timedelta(days=9)
        hits = store.evaluate_stale_gaps(nine_days_later)
        assert len(hits) == 1
        assert hits[0][0].gap_id == "g-stale"
        assert "stale_after_days=7" in hits[0][1]

    def test_evaluate_stale_gaps_excludes_fresh(self) -> None:
        store = GapLifecycleStore()
        gap = _resolved_gap_with_criteria("g-fresh", stale_after_days=30)
        store.upsert(gap)
        one_day_later = gap.resolved_at + timedelta(days=1)
        hits = store.evaluate_stale_gaps(one_day_later)
        assert hits == []


class TestFindByCriteria:
    def test_finds_matching_gaps(self) -> None:
        store = GapLifecycleStore()
        gap_a = _resolved_gap_with_criteria("ga", entity_id="MILESTONE:ms-001", fact_family="milestone")
        gap_b = _resolved_gap_with_criteria("gb", entity_id="MILESTONE:ms-002", fact_family="milestone")
        gap_c = _resolved_gap_with_criteria("gc", entity_id="RISK:r-001", fact_family="risk")
        for g in (gap_a, gap_b, gap_c):
            store.upsert(g)
        hits = store.find_by_criteria("MILESTONE:ms-001", "milestone")
        assert len(hits) == 1
        assert hits[0].gap_id == "ga"

    def test_no_match_returns_empty(self) -> None:
        store = GapLifecycleStore()
        store.upsert(_resolved_gap_with_criteria("g1", fact_family="milestone"))
        hits = store.find_by_criteria("RISK:r-001", "risk")
        assert hits == ()

    def test_gaps_without_criteria_excluded(self) -> None:
        store = GapLifecycleStore()
        gap = ContextGapRecord(gap_id="g-legacy", description="legacy gap")
        store.upsert(gap)
        hits = store.find_by_criteria("MILESTONE:ms-001", "milestone")
        assert hits == ()