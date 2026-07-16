"""ADF-W2.4/W2.5: unit tests for src/core/fact_lineage_coverage.py."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.fact_lineage_coverage import classify_fact_lineage, compute_lineage_coverage, has_fact_provenance
from src.core.fact_lineage_waiver_store import FactLineageWaiver
from src.core.program_fact_store import FactLifecycleState, FactPrecedence, FactReviewState, ProgramFactRevision


def _fact(
    natural_key: str,
    *,
    source_signal_ids: tuple[str, ...] = (),
    domain_event_id: str | None = None,
    candidate_id: str | None = None,
    source_document_key: str | None = None,
    evidence_ref: str | None = None,
    source_hash: str | None = None,
) -> ProgramFactRevision:
    return ProgramFactRevision(
        revision_id=f"rev-{natural_key}",
        fact_id=f"fact-{natural_key}",
        program_id="xpf",
        natural_key=natural_key,
        fact_type="risk",
        scope="program",
        entity_refs=(natural_key,),
        payload={},
        source_signal_ids=source_signal_ids,
        confidence="high",
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="test",
        domain_event_id=domain_event_id,
        candidate_id=candidate_id,
        source_document_key=source_document_key,
        evidence_ref=evidence_ref,
        source_hash=source_hash,
    )


def test_has_fact_provenance_via_source_signal_ids() -> None:
    assert has_fact_provenance(_fact("k1", source_signal_ids=("sig-1",)))


def test_has_fact_provenance_via_envelope_field() -> None:
    assert has_fact_provenance(_fact("k1", domain_event_id="evt-1"))
    assert has_fact_provenance(_fact("k2", candidate_id="cand-1"))
    assert has_fact_provenance(_fact("k3", source_document_key="hash-1"))
    assert has_fact_provenance(_fact("k4", evidence_ref="vault-1"))
    assert has_fact_provenance(_fact("k5", source_hash="hash-2"))


def test_no_provenance_when_nothing_populated() -> None:
    assert not has_fact_provenance(_fact("k1"))


def test_classify_lineaged() -> None:
    state = classify_fact_lineage(_fact("k1", source_signal_ids=("sig-1",)), waivers_by_natural_key={}, as_of=date(2026, 6, 1))
    assert state == "lineaged"


def test_classify_waived_when_active_waiver_present() -> None:
    waiver = FactLineageWaiver(
        natural_key="k1", owner="alice", reason="legacy", granted=date(2026, 1, 1), expires=date(2026, 12, 31)
    )
    state = classify_fact_lineage(_fact("k1"), waivers_by_natural_key={"k1": waiver}, as_of=date(2026, 6, 1))
    assert state == "waived"


def test_classify_defect_when_waiver_expired() -> None:
    waiver = FactLineageWaiver(
        natural_key="k1", owner="alice", reason="legacy", granted=date(2025, 1, 1), expires=date(2025, 12, 31)
    )
    state = classify_fact_lineage(_fact("k1"), waivers_by_natural_key={"k1": waiver}, as_of=date(2026, 6, 1))
    assert state == "defect"


def test_classify_defect_when_no_provenance_and_no_waiver() -> None:
    state = classify_fact_lineage(_fact("k1"), waivers_by_natural_key={}, as_of=date(2026, 6, 1))
    assert state == "defect"


def test_compute_lineage_coverage_splits_three_ways(monkeypatch, tmp_path: Path) -> None:
    from src.core.program_fact_store import ProgramFactSnapshot

    facts = (
        _fact("lineaged-1", source_signal_ids=("sig-1",)),
        _fact("lineaged-2", domain_event_id="evt-1"),
        _fact("waived-1"),
        _fact("defect-1"),
        _fact("defect-2"),
    )
    snapshot = ProgramFactSnapshot(program_id="xpf", as_of=datetime(2026, 6, 1, tzinfo=timezone.utc), facts=facts)
    monkeypatch.setattr("src.core.fact_lineage_coverage.load_program_facts", lambda program_id, **kwargs: snapshot)

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "fact_lineage_waivers.yaml").write_text(
        """
schema_version: "1.0"
waivers:
  - natural_key: "waived-1"
    owner: "alice"
    reason: "legacy"
    granted: "2026-01-01"
    expires: "2026-12-31"
""".strip(),
        encoding="utf-8",
    )

    report = compute_lineage_coverage("xpf", programs_root=programs_root, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    assert report.total_count == 5
    assert report.lineaged_count == 2
    assert report.waived_count == 1
    assert report.defect_count == 2
    assert set(report.sample_defect_natural_keys) == {"defect-1", "defect-2"}
    assert report.coverage_ratio == 2 / 5


def test_compute_lineage_coverage_empty_program_has_none_ratio(monkeypatch, tmp_path: Path) -> None:
    from src.core.program_fact_store import ProgramFactSnapshot

    snapshot = ProgramFactSnapshot(program_id="xpf", as_of=datetime(2026, 6, 1, tzinfo=timezone.utc), facts=())
    monkeypatch.setattr("src.core.fact_lineage_coverage.load_program_facts", lambda program_id, **kwargs: snapshot)

    report = compute_lineage_coverage("xpf", programs_root=tmp_path / "programs")
    assert report.total_count == 0
    assert report.coverage_ratio is None
