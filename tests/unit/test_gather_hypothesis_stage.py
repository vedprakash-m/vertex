"""Direct coverage for the extracted hypothesis proposer gather stage."""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.commands.gather_pipeline import hypothesis_stage
from src.core.models_v2 import ClaimEntry
from src.core.reality_store import RealityStore


def test_run_hypothesis_proposer_stage_returns_empty_when_no_claims(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")

    result = hypothesis_stage.run_hypothesis_proposer_stage(
        "acme",
        claims=(),
        proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        store=store,
    )

    assert result == ()


def test_run_hypothesis_proposer_stage_creates_hypothesis_for_eligible_claim(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")

    result = hypothesis_stage.run_hypothesis_proposer_stage(
        "acme",
        claims=(
            ClaimEntry(
                id="claim-1",
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Expected by 2026-06-01",
                entity_refs=("WI:1234",),
                claim_date=date(2026, 5, 20),
                owner_alias="priya",
                due_date=date(2026, 6, 1),
            ),
        ),
        proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        store=store,
    )

    assert len(result) == 1
    assert getattr(result[0], "linked_claim_id", None) == "claim-1"
