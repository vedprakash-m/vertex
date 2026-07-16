"""ADF-W2.4/W2.5: unit tests for src/core/fact_lineage_coverage.py."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.core.fact_lineage_coverage as lineage
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
        program_id="acme",
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
    snapshot = ProgramFactSnapshot(program_id="acme", as_of=datetime(2026, 6, 1, tzinfo=timezone.utc), facts=facts)
    load_facts = Mock(return_value=snapshot)
    monkeypatch.setattr(lineage, "load_program_facts", load_facts)

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
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

    report = compute_lineage_coverage("acme", programs_root=programs_root, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    assert report.total_count == 5
    assert report.lineaged_count == 2
    assert report.waived_count == 1
    assert report.defect_count == 2
    assert set(report.sample_defect_natural_keys) == {"defect-1", "defect-2"}
    assert report.coverage_ratio == 2 / 5
    load_facts.assert_called_once_with("acme", programs_root=programs_root, archive_root=tmp_path / "archive")


def test_compute_lineage_coverage_empty_program_has_none_ratio(monkeypatch, tmp_path: Path) -> None:
    from src.core.program_fact_store import ProgramFactSnapshot

    snapshot = ProgramFactSnapshot(program_id="acme", as_of=datetime(2026, 6, 1, tzinfo=timezone.utc), facts=())
    load_facts = Mock(return_value=snapshot)
    monkeypatch.setattr(lineage, "load_program_facts", load_facts)

    report = compute_lineage_coverage("acme", programs_root=tmp_path / "programs")
    assert report.total_count == 0
    assert report.coverage_ratio is None
    load_facts.assert_called_once_with("acme", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive")


@pytest.mark.parametrize("explicit_archive", [False, True])
def test_lineage_real_loader_keeps_selected_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_archive: bool,
) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "synthetic"
    edition_dir = program_dir / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / "synthetic_weekly.yaml").write_text(
        "id: synthetic_weekly\nprogram_id: synthetic\n", encoding="utf-8",
    )
    index_payload = {
        "edition": "synthetic_weekly",
        "issues": [{
            "issue_number": 1,
            "generated_at": "2026-06-01T00:00:00+00:00",
            "kind": "skipped",
            "reason": "Synthetic skipped issue",
        }],
    }
    implicit_index = program_dir / "archive" / "synthetic_weekly" / "index.json"
    implicit_index.parent.mkdir(parents=True)
    implicit_index.write_text(json.dumps(index_payload), encoding="utf-8")
    archive_root = tmp_path / "selected-history" / "archive"
    override_index = archive_root / "synthetic_weekly" / "index.json"
    override_index.parent.mkdir(parents=True)
    override_index.write_text(json.dumps({**index_payload, "issues": []}), encoding="utf-8")
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    load_facts = Mock(wraps=lineage.load_program_facts)
    monkeypatch.setattr(lineage, "load_program_facts", load_facts)
    report = (
        compute_lineage_coverage("synthetic", programs_root=programs_root, archive_root=archive_root)
        if explicit_archive
        else compute_lineage_coverage("synthetic", programs_root=programs_root)
    )

    assert report.total_count == (0 if explicit_archive else 1)
    assert report.defect_count == (0 if explicit_archive else 1)
    load_facts.assert_called_once_with(
        "synthetic",
        programs_root=programs_root,
        archive_root=archive_root if explicit_archive else tmp_path / "archive",
    )


def test_lineage_default_archive_matches_existing_layout() -> None:
    from src.core.snapshot_store import ARCHIVE_ROOT

    assert lineage.PROGRAMS_ROOT.parent / "archive" == ARCHIVE_ROOT
