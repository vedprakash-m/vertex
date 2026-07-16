from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.dependency_scout import (
    DependencyProposal,
    DependencyProposalStatus,
    dependency_proposal_to_dependency,
    load_dependency_proposals,
    merge_dependency_proposals,
    save_dependency_proposals,
    scout_dependency_proposals,
)
from src.core.models import Confidence, RiskLevel, SnapshotItem
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, Signal, SignalReviewDecision, TrajectoryPoint, Workstream


def test_dependency_proposal_to_dependency_carries_evidence_refs_forward() -> None:
    # ADF-W2.4/W2.5: a real bug -- evidence_refs (the scout-derived lineage
    # this proposal was built from) was being silently dropped at accept
    # time, meaning every accepted scout-derived dependency looked exactly
    # like a hand-authored one (evidence_refs=()) with no traceability back
    # to the signals that justified it.
    proposal = DependencyProposal(
        id="dep-proposal-1", program_id="acme", from_workstream_id="ws-1", to_workstream_id="ws-2",
        from_item_id=101, to_item_id=202, from_item_title="Item A", to_item_title="Item B",
        suggested_dependency_type=DependencyType.BLOCKS, rationale="Repeated co-mention.",
        evidence_refs=("sig-1", "sig-2"), detection_method="co_mention", occurrence_count=3,
        first_seen_at=datetime(2026, 7, 1, tzinfo=timezone.utc), last_seen_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    dependency = dependency_proposal_to_dependency(proposal)

    assert dependency.evidence_refs == ("sig-1", "sig-2")


def test_scout_dependency_proposals_detects_repeated_cross_workstream_co_mentions() -> None:
    proposals = scout_dependency_proposals(
        program_id="acme",
        signals=(
            _signal("sig-1", entity_refs=("WI#101", "WI#202")),
            _signal("sig-2", entity_refs=("WI#202", "WI#101")),
            _signal("sig-3", entity_refs=("WI#101", "WI#202", "noise")),
        ),
        review_states={
            "sig-1": _review("sig-1"),
            "sig-2": _review("sig-2"),
            "sig-3": _review("sig-3"),
        },
        snapshot_items=(
            SnapshotItem(
                id=101,
                type="Feature",
                title="UD chunking",
                state="Active",
                assigned_to=None,
                area_path="Acme\\UD",
                target_date=None,
                risk_level=RiskLevel.MEDIUM,
                tags=[],
            ),
            SnapshotItem(
                id=202,
                type="Feature",
                title="Fabrikam buildouts",
                state="Active",
                assigned_to=None,
                area_path="Acme\\Fabrikam",
                target_date=None,
                risk_level=RiskLevel.MEDIUM,
                tags=[],
            ),
        ),
        workstreams=(
            Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),
            Workstream(id="fabrikam", name="Fabrikam", area_paths=("Acme\\Fabrikam",)),
        ),
        existing_dependencies=(),
        as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        lookback_days=30,
        min_occurrences=3,
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.id == "dep-proposal-co-mention-101-202"
    assert proposal.from_workstream_id == "fabrikam"
    assert proposal.to_workstream_id == "ud"
    assert proposal.suggested_dependency_type == DependencyType.SHARES_RESOURCE
    assert proposal.occurrence_count == 3
    assert proposal.evidence_refs == ("sig-1", "sig-2", "sig-3")


def test_scout_dependency_proposals_detects_comment_language_and_suppresses_plain_co_mention_duplicate() -> None:
    proposals = scout_dependency_proposals(
        program_id="acme",
        signals=(
            _signal("sig-1", entity_refs=("WI#101", "WI#202"), text="WI#101 is blocked by WI#202."),
            _signal("sig-2", entity_refs=("WI#202", "WI#101"), text="Still waiting on WI#202 before WI#101 can move."),
        ),
        review_states={
            "sig-1": _review("sig-1"),
            "sig-2": _review("sig-2"),
        },
        snapshot_items=(
            SnapshotItem(
                id=101,
                type="Feature",
                title="UD chunking",
                state="Active",
                assigned_to=None,
                area_path="Acme\\UD",
                target_date=None,
                risk_level=RiskLevel.MEDIUM,
                tags=[],
            ),
            SnapshotItem(
                id=202,
                type="Feature",
                title="Fabrikam buildouts",
                state="Active",
                assigned_to=None,
                area_path="Acme\\Fabrikam",
                target_date=None,
                risk_level=RiskLevel.MEDIUM,
                tags=[],
            ),
        ),
        workstreams=(
            Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),
            Workstream(id="fabrikam", name="Fabrikam", area_paths=("Acme\\Fabrikam",)),
        ),
        existing_dependencies=(),
        as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        lookback_days=30,
        min_occurrences=3,
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.id == "dep-proposal-comment-language-101-202"
    assert proposal.detection_method == "comment_language"
    assert proposal.occurrence_count == 2
    assert proposal.evidence_refs == ("sig-1", "sig-2")
    assert "blocked by" in proposal.rationale
    assert "waiting on" in proposal.rationale


def test_scout_dependency_proposals_skips_existing_item_pair_dependencies() -> None:
    proposals = scout_dependency_proposals(
        program_id="acme",
        signals=(
            _signal("sig-1", entity_refs=("WI#101", "WI#202")),
            _signal("sig-2", entity_refs=("WI#101", "WI#202")),
            _signal("sig-3", entity_refs=("WI#101", "WI#202")),
        ),
        review_states={
            "sig-1": _review("sig-1"),
            "sig-2": _review("sig-2"),
            "sig-3": _review("sig-3"),
        },
        snapshot_items=(
            SnapshotItem(101, "Feature", "UD chunking", "Active", None, "Acme\\UD", None, RiskLevel.MEDIUM, []),
            SnapshotItem(202, "Feature", "Fabrikam buildouts", "Active", None, "Acme\\Fabrikam", None, RiskLevel.MEDIUM, []),
        ),
        workstreams=(
            Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),
            Workstream(id="fabrikam", name="Fabrikam", area_paths=("Acme\\Fabrikam",)),
        ),
        existing_dependencies=(
            Dependency(
                id="dep-existing",
                from_program_id="acme",
                from_workstream_id="ud",
                from_item_id=101,
                from_milestone_id=None,
                to_program_id="acme",
                to_workstream_id="fabrikam",
                to_item_id=202,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Already tracked.",
                mitigation=None,
                status=DependencyStatus.ACTIVE,
                owner_alias=None,
            ),
        ),
        as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        lookback_days=30,
        min_occurrences=3,
    )

    assert proposals == ()


def test_merge_dependency_proposals_preserves_existing_status(tmp_path: Path) -> None:
    proposal = next(
        iter(
            scout_dependency_proposals(
                program_id="acme",
                signals=(
                    _signal("sig-1", entity_refs=("WI#101", "WI#202")),
                    _signal("sig-2", entity_refs=("WI#101", "WI#202")),
                    _signal("sig-3", entity_refs=("WI#101", "WI#202")),
                ),
                review_states={
                    "sig-1": _review("sig-1"),
                    "sig-2": _review("sig-2"),
                    "sig-3": _review("sig-3"),
                },
                snapshot_items=(
                    SnapshotItem(101, "Feature", "UD chunking", "Active", None, "Acme\\UD", None, RiskLevel.MEDIUM, []),
                    SnapshotItem(202, "Feature", "Fabrikam buildouts", "Active", None, "Acme\\Fabrikam", None, RiskLevel.MEDIUM, []),
                ),
                workstreams=(
                    Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),
                    Workstream(id="fabrikam", name="Fabrikam", area_paths=("Acme\\Fabrikam",)),
                ),
                existing_dependencies=(),
                as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                lookback_days=30,
                min_occurrences=3,
            )
        )
    )
    existing = (replace(proposal, status=DependencyProposalStatus.DISMISSED),)
    merged = merge_dependency_proposals(existing, (proposal,))
    save_dependency_proposals("acme", merged, programs_root=tmp_path / "programs")

    loaded = load_dependency_proposals("acme", programs_root=tmp_path / "programs")

    assert len(loaded) == 1
    assert loaded[0].status == DependencyProposalStatus.DISMISSED


def test_scout_dependency_proposals_detects_eta_co_movement_without_signal_co_mentions() -> None:
    proposals = scout_dependency_proposals(
        program_id="acme",
        signals=(),
        review_states={},
        snapshot_items=(
            SnapshotItem(101, "Feature", "UD chunking", "Active", "alice", "Acme\\UD", date(2026, 6, 20), RiskLevel.MEDIUM, []),
            SnapshotItem(202, "Feature", "Fabrikam buildouts", "Active", "bob", "Acme\\Fabrikam", date(2026, 6, 21), RiskLevel.MEDIUM, []),
        ),
        workstreams=(
            Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),
            Workstream(id="fabrikam", name="Fabrikam", area_paths=("Acme\\Fabrikam",)),
        ),
        existing_dependencies=(),
        trajectories_by_item_id={
            101: (
                TrajectoryPoint(date(2026, 5, 1), "Active", "alice", date(2026, 6, 1), RiskLevel.MEDIUM, "Acme\\UD"),
                TrajectoryPoint(date(2026, 5, 5), "Active", "alice", date(2026, 6, 8), RiskLevel.MEDIUM, "Acme\\UD"),
                TrajectoryPoint(date(2026, 5, 20), "Active", "alice", date(2026, 6, 16), RiskLevel.MEDIUM, "Acme\\UD"),
            ),
            202: (
                TrajectoryPoint(date(2026, 5, 2), "Active", "bob", date(2026, 6, 2), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
                TrajectoryPoint(date(2026, 5, 8), "Active", "bob", date(2026, 6, 10), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
                TrajectoryPoint(date(2026, 5, 24), "Active", "bob", date(2026, 6, 18), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
            ),
        },
        as_of=datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc),
        lookback_days=30,
        min_occurrences=3,
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.id == "dep-proposal-eta-co-movement-101-202"
    assert proposal.detection_method == "eta_co_movement"
    assert proposal.occurrence_count == 2
    assert proposal.evidence_refs == (
        "trajectory:101:2026-05-05",
        "trajectory:101:2026-05-20",
        "trajectory:202:2026-05-08",
        "trajectory:202:2026-05-24",
    )


def test_scout_dependency_proposals_detects_owner_overlap_with_aligned_eta_slips() -> None:
    proposals = scout_dependency_proposals(
        program_id="acme",
        signals=(),
        review_states={},
        snapshot_items=(
            SnapshotItem(101, "Feature", "UD chunking", "Active", "alice", "Acme\\UD", date(2026, 6, 20), RiskLevel.MEDIUM, []),
            SnapshotItem(202, "Feature", "Fabrikam buildouts", "Active", "Alice", "Acme\\Fabrikam", date(2026, 6, 21), RiskLevel.MEDIUM, []),
        ),
        workstreams=(
            Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),
            Workstream(id="fabrikam", name="Fabrikam", area_paths=("Acme\\Fabrikam",)),
        ),
        existing_dependencies=(),
        trajectories_by_item_id={
            101: (
                TrajectoryPoint(date(2026, 5, 1), "Active", "alice", date(2026, 6, 1), RiskLevel.MEDIUM, "Acme\\UD"),
                TrajectoryPoint(date(2026, 5, 5), "Active", "alice", date(2026, 6, 8), RiskLevel.MEDIUM, "Acme\\UD"),
                TrajectoryPoint(date(2026, 5, 20), "Active", "alice", date(2026, 6, 16), RiskLevel.MEDIUM, "Acme\\UD"),
            ),
            202: (
                TrajectoryPoint(date(2026, 5, 2), "Active", "alice", date(2026, 6, 2), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
                TrajectoryPoint(date(2026, 5, 8), "Active", "alice", date(2026, 6, 10), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
                TrajectoryPoint(date(2026, 5, 24), "Active", "alice", date(2026, 6, 18), RiskLevel.MEDIUM, "Acme\\Fabrikam"),
            ),
        },
        as_of=datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc),
        lookback_days=30,
        min_occurrences=3,
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.id == "dep-proposal-owner-overlap-101-202"
    assert proposal.detection_method == "owner_overlap"
    assert proposal.occurrence_count == 2
    assert proposal.evidence_refs == (
        "owner:alice",
        "trajectory:101:2026-05-05",
        "trajectory:101:2026-05-20",
        "trajectory:202:2026-05-08",
        "trajectory:202:2026-05-24",
    )


def _signal(signal_id: str, *, entity_refs: tuple[str, ...], text: str = "Repeated dependency chatter.") -> Signal:
    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        source="workiq",
        program_id="acme",
        workstream_id="ud",
        entity_refs=entity_refs,
        text=text,
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata=None,
    )


def _review(signal_id: str) -> SignalReviewDecision:
    return SignalReviewDecision(
        signal_id=signal_id,
        decision="approved",
        reviewed_at=datetime(2026, 5, 20, 12, 30, tzinfo=timezone.utc),
        reviewed_by="owner",
    )