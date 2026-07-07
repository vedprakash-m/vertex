from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.hypothesis_models import Hypothesis, HypothesisKind, HypothesisStatus
from src.core.hypothesis_proposers import ClaimProposer, run_registered_hypothesis_proposers
from src.core.models_v2 import ClaimEntry
from src.core.reality_store import RealityStore


def test_claim_proposer_creates_delivery_date_hypothesis_from_open_claim(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    created = ClaimProposer().propose(
        store=store,
        claims=(
            ClaimEntry(
                id="claim-001",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Pilot rollout completes by 2026-06-15.",
                entity_refs=("WI:12345",),
                claim_date=date(2026, 5, 20),
                owner_alias="pm.owner",
                due_date=date(2026, 6, 15),
            ),
        ),
        proposed_at=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
    )

    stored = store.get_hypothesis(created[0].id)

    assert len(created) == 1
    assert stored is not None
    assert stored.short_id == "H-001"
    assert stored.kind is HypothesisKind.DELIVERY_DATE
    assert stored.status is HypothesisStatus.PROPOSED
    assert stored.expected_value == "2026-06-15"
    assert stored.linked_claim_id == "claim-001"
    assert stored.linked_ado_item_id == 12345


def test_claim_proposer_skips_claims_with_existing_linked_hypothesis(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="Pilot rollout completes by 2026-06-15.",
            expected_value="2026-06-15",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="vertex/claim_proposer",
            proposed_at=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.PROPOSED,
            linked_claim_id="claim-001",
            linked_ado_item_id=12345,
        )
    )

    created = ClaimProposer().propose(
        store=store,
        claims=(
            ClaimEntry(
                id="claim-001",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Pilot rollout completes by 2026-06-15.",
                entity_refs=("WI:12345",),
                claim_date=date(2026, 5, 20),
                owner_alias="pm.owner",
                due_date=date(2026, 6, 15),
            ),
        ),
        proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert created == ()
    assert len(store.list_hypotheses()) == 1


def test_claim_proposer_reproposes_after_rejection_cooldown(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="Pilot rollout completes by 2026-06-15.",
            expected_value="2026-06-15",
            as_of_date=date(2026, 5, 1),
            telemetry_assertion_id=None,
            proposed_by="vertex/claim_proposer",
            proposed_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.REJECTED,
            linked_claim_id="claim-001",
            linked_ado_item_id=12345,
        )
    )
    store.set_hypothesis_state(
        "hyp-001",
        HypothesisStatus.REJECTED,
        datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc),
        actor="owner.pm",
        reason="needs_more_evidence",
    )

    created = ClaimProposer().propose(
        store=store,
        claims=(
            ClaimEntry(
                id="claim-001",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Pilot rollout completes by 2026-06-15.",
                entity_refs=("WI:12345",),
                claim_date=date(2026, 5, 20),
                owner_alias="pm.owner",
                due_date=date(2026, 6, 15),
            ),
        ),
        proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert len(created) == 1
    assert created[0].short_id == "H-002"
    assert created[0].status is HypothesisStatus.PROPOSED
    assert len(store.list_hypotheses()) == 2


def test_claim_proposer_skips_recently_rejected_claims_within_cooldown(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="Pilot rollout completes by 2026-06-15.",
            expected_value="2026-06-15",
            as_of_date=date(2026, 5, 18),
            telemetry_assertion_id=None,
            proposed_by="vertex/claim_proposer",
            proposed_at=datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.REJECTED,
            linked_claim_id="claim-001",
            linked_ado_item_id=12345,
        )
    )
    store.set_hypothesis_state(
        "hyp-001",
        HypothesisStatus.REJECTED,
        datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
        actor="owner.pm",
        reason="needs_more_evidence",
    )

    created = ClaimProposer().propose(
        store=store,
        claims=(
            ClaimEntry(
                id="claim-001",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Pilot rollout completes by 2026-06-15.",
                entity_refs=("WI:12345",),
                claim_date=date(2026, 5, 20),
                owner_alias="pm.owner",
                due_date=date(2026, 6, 15),
            ),
        ),
        proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert created == ()
    assert len(store.list_hypotheses()) == 1


def test_registered_hypothesis_proposers_runs_claim_proposer_registry(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    created = run_registered_hypothesis_proposers(
        store=store,
        claims=(
            ClaimEntry(
                id="claim-001",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Pilot rollout completes by 2026-06-15.",
                entity_refs=("WI:12345",),
                claim_date=date(2026, 5, 20),
                owner_alias="pm.owner",
                due_date=date(2026, 6, 15),
            ),
        ),
        proposed_at=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
    )

    assert len(created) == 1
    assert created[0].linked_claim_id == "claim-001"