from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from dataclasses import replace

import pytest

from src.commands.review_full import _build_context_revision_rows
from src.commands.triage import TriageArtifacts, _build_triage_payload, render_triage_output
from src.core.context_proposal_review import ContextProposalReviewRow, load_pending_context_proposal_rows
from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import stage_extracted_proposals
from src.core.triage import ReadinessAssessment, TriageReport, render_triage_report


def _proposal(
    proposal_id: str,
    *,
    issue_number: int,
    target_store: str,
    conflict_key: str,
) -> ContextUpdateProposal:
    return ContextUpdateProposal(
        proposal_id=proposal_id, program_id="contoso", issue_number=issue_number, edition_id="contoso_weekly",
        source_type="confirmed_overrides", extracted_at=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
        extractor_version="1.0.0", source_artifact="overrides/issue_001.yaml", source_field="scorecards.delivery.risk",
        extraction_method="overrides_yaml", target_store=target_store, target_key="delivery", target_field="risk_level",
        source_value="high", current_value="medium", current_value_hash="current-hash", confidence="high",
        batch_eligible=True, extraction_method_rationale="test fixture", conflict_key=conflict_key,
    )


def test_pending_rows_include_review_information_and_manual_only_command(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    writable = _proposal("proposal-writable", issue_number=7, target_store="risk_register", conflict_key="risk:delivery")
    registry = _proposal("proposal-registry", issue_number=8, target_store="workstream_registry", conflict_key="registry:delivery")
    conflicting_registry = _proposal("proposal-registry-conflict", issue_number=9, target_store="workstream_registry", conflict_key="registry:delivery")
    stage_extracted_proposals("contoso", 7, (writable,), programs_root=programs_root)
    stage_extracted_proposals("contoso", 8, (registry,), programs_root=programs_root)
    stage_extracted_proposals("contoso", 9, (conflicting_registry,), programs_root=programs_root)

    rows = {row.proposal_id: row for row in load_pending_context_proposal_rows("contoso", programs_root=programs_root)}

    assert rows["proposal-writable"].target == "risk_register.delivery.risk_level"
    assert rows["proposal-writable"].current_hash_label == "current-hash"
    assert rows["proposal-writable"].evidence == "overrides/issue_001.yaml:scorecards.delivery.risk"
    assert rows["proposal-writable"].conflict_state == "no cross-issue conflict"
    assert rows["proposal-writable"].next_command == "vertex context proposals --edition contoso_weekly --issue 7"
    assert "proposal-registry-conflict" in rows["proposal-registry"].conflict_state
    assert rows["proposal-registry"].next_command == (
        "vertex context manual-diff --edition contoso_weekly --issue 8 --proposal-id proposal-registry"
    )


def test_cockpit_surfaces_each_pending_context_revision(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _proposal("proposal-1", issue_number=7, target_store="risk_register", conflict_key="risk:delivery")
    stage_extracted_proposals("contoso", 7, (proposal,), programs_root=programs_root)

    snapshot = build_cockpit_snapshot("contoso", programs_root=programs_root, now=datetime(2026, 7, 21, tzinfo=timezone.utc))

    finding = next(item for item in snapshot.findings if item.finding_id == "trust.context_proposal.proposal-1")
    assert finding.status == "warn"
    assert "current-hash" in finding.detail
    assert finding.next_command == "vertex context proposals --edition contoso_weekly --issue 7"


def test_triage_rendering_includes_context_revision_review_data() -> None:
    row = ContextProposalReviewRow(
        proposal_id="proposal-1", edition_id="contoso_weekly", issue_number=78,
        target="workstream_registry.delivery.owner", proposed_value="new-owner", current_value_hash="abc",
        evidence="source.yaml:owner", conflict_state="no cross-issue conflict",
        next_command="vertex context manual-diff --edition contoso_weekly --issue 78 --proposal-id proposal-1",
    )
    report = TriageReport(
        edition_name="contoso_weekly", issue_number=78, program_id="contoso",
        readiness=ReadinessAssessment(100, 100, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1),
        blockers=(), needs_attention=(), milestones=(), risks=(), actions=(), decisions=(), assumptions=(),
        cross_program_cascades=(), active_issues=(), coverage_gaps=(), ready=(), coverage_gap_window_days=14,
        context_proposals=(row,),
    )

    rendered = render_triage_report(report)

    assert "CONTEXT REVISIONS:" in rendered
    assert "workstream_registry.delivery.owner" in rendered
    assert "vertex context manual-diff" in rendered


def test_reviewer_context_rows_keep_manual_registry_boundary(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _proposal("proposal-registry", issue_number=8, target_store="workstream_registry", conflict_key="registry:delivery")
    stage_extracted_proposals("contoso", 8, (proposal,), programs_root=programs_root)

    rows = _build_context_revision_rows("contoso", programs_root=programs_root)

    assert len(rows) == 1
    assert rows[0].title == "proposal-registry · workstream_registry.delivery.risk_level"
    assert "current-hash" in (rows[0].detail or "")
    assert "vertex context manual-diff" in rows[0].summary


def test_triage_json_and_csv_keep_context_revision_fields() -> None:
    row = ContextProposalReviewRow(
        proposal_id="proposal-1", edition_id="contoso_weekly", issue_number=78,
        target="risk_register.delivery.risk_level", proposed_value="high", current_value_hash="abc",
        evidence="source.yaml:risk", conflict_state="no cross-issue conflict",
        next_command="vertex context proposals --edition contoso_weekly --issue 78",
    )
    report = TriageReport(
        edition_name="contoso_weekly", issue_number=78, program_id="contoso",
        readiness=ReadinessAssessment(100, 100, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1),
        blockers=(), needs_attention=(), milestones=(), risks=(), actions=(), decisions=(), assumptions=(),
        cross_program_cascades=(), active_issues=(), coverage_gaps=(), ready=(), coverage_gap_window_days=14,
        context_proposals=(row,),
    )
    artifacts = TriageArtifacts(report=report, exit_code=0)

    payload = _build_triage_payload(artifacts)
    csv_output = render_triage_output(artifacts, format="csv")

    assert payload["counts"]["context_revisions"] == 1
    assert payload["context_revisions"][0]["next_command"] == row.next_command
    assert "context_revision" in csv_output
    assert "current_value_hash" in csv_output


def test_reply_review_projection_exposes_unverified_origin_and_intake_baseline(tmp_path: Path) -> None:
    proposal = replace(
        _proposal("reply-1", issue_number=1, target_store="workstream_registry", conflict_key="registry:why"),
        source_type="context_gap_reply", target_field="deep_context.why", batch_eligible=False,
        extraction_method_rationale="Observed From owner@example.com and Reference sol-1 are unverified; intake-time baseline only.",
    )
    stage_extracted_proposals("contoso", 1, (proposal,), programs_root=tmp_path)
    row, = load_pending_context_proposal_rows("contoso", programs_root=tmp_path)
    assert "unverified" in row.evidence
    assert "intake-time" in row.evidence
    assert "manual-diff" in row.next_command


def test_maintenance_projection_reads_once_preserves_history_and_revision_api(tmp_path: Path, monkeypatch) -> None:
    import src.core.context_proposal_review as review
    from src.core.context_gap_store import ContextGapRecord
    from unittest.mock import Mock

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    observed = datetime(2026, 10, 1, tzinfo=timezone.utc)
    gap = ContextGapRecord(observed, "doctor --context", "contoso", "delivery", "deep_context.why",
                           "quality_degraded", "Purpose missing", "high")
    load = Mock(return_value=[gap, gap])
    monkeypatch.setattr(review, "load_context_gaps", load, raising=False)
    root = tmp_path / "programs"
    registry = root / "contoso" / "workstream_registry.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("workstreams:\n  - id: delivery\n    deep_context:\n      why: ''\n", encoding="utf-8")
    before = registry.read_bytes()
    projection = review.load_context_maintenance_review("contoso", programs_root=root, as_of=now)
    load.assert_called_once_with("contoso", programs_root=root)
    assert projection.evaluated_at == now
    assert projection.proposals == load_pending_context_proposal_rows("contoso", programs_root=root)
    assert projection.gap_diagnostic is None
    assert len(projection.gaps) == 1
    assert projection.gaps[0].count == 2
    assert projection.gaps[0].first_seen == observed
    assert registry.read_bytes() == before
    registry.write_text("workstreams:\n  - id: delivery\n    deep_context:\n      why: repaired\n", encoding="utf-8")
    assert review.load_context_maintenance_review("contoso", programs_root=root, as_of=now).gaps == ()
    registry.write_bytes(before)
    assert len(review.load_context_maintenance_review("contoso", programs_root=root, as_of=now).gaps) == 1


def test_maintenance_projection_corrupt_history_is_safe_and_keeps_proposals(tmp_path: Path) -> None:
    import src.core.context_proposal_review as review

    proposal = _proposal("pending", issue_number=8, target_store="workstream_registry", conflict_key="registry")
    stage_extracted_proposals("contoso", 8, (proposal,), programs_root=tmp_path)
    path = tmp_path / "contoso" / "_feedback" / "context_gaps.jsonl"
    path.parent.mkdir()
    path.write_text("{private_canary: invalid\n", encoding="utf-8")
    before = path.read_bytes()
    projection = review.load_context_maintenance_review(
        "contoso", programs_root=tmp_path, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    assert projection.gaps == ()
    assert projection.gap_diagnostic is not None and "unavailable" in projection.gap_diagnostic.lower()
    assert "private_canary" not in projection.gap_diagnostic and str(path) not in projection.gap_diagnostic
    assert len(projection.proposals) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["null", "missing", "timestamp", "lane"])
@pytest.mark.parametrize("placement", ["before", "after"])
def test_maintenance_invalid_record_preserves_pending_proposals_and_no_partial_gaps(
    tmp_path: Path, damage: str, placement: str,
) -> None:
    import src.core.context_proposal_review as review

    root = tmp_path / "selected"
    proposal = _proposal("pending", issue_number=8, target_store="workstream_registry", conflict_key="registry")
    stage_extracted_proposals("contoso", 8, (proposal,), programs_root=root)
    history = root / "contoso" / "_feedback" / "context_gaps.jsonl"
    history.parent.mkdir()
    valid = {
        "ts": "2026-10-01T00:00:00Z", "feature": "synthetic", "program": "contoso",
        "lane": None, "field": "purpose", "severity": "quality_degraded",
        "message": "Valid observation", "impact_estimate": "high",
    }
    damaged = {
        "null": None,
        "missing": {"message": "synthetic_canary"},
        "timestamp": dict(valid, ts=None, message="synthetic_canary"),
        "lane": dict(valid, lane=["synthetic_canary"]),
    }[damage]
    rows = (damaged, valid) if placement == "before" else (valid, damaged)
    history.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    projection = review.load_context_maintenance_review(
        "contoso", programs_root=root, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )

    assert [row.proposal_id for row in projection.proposals] == ["pending"]
    assert projection.proposal_diagnostic is None and projection.gaps == ()
    assert projection.gap_diagnostic == "Context gap history unavailable; inspect and repair it before retrying."
    assert "synthetic_canary" not in repr(projection) and str(root) not in repr(projection)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("registry", [
    None,
    "workstreams: [\nsynthetic_canary\n",
    "workstreams: !synthetic_canary []\n",
    "workstreams: synthetic_canary\n",
    "workstreams: [{id: another, deep_context: {}}]\n",
    "workstreams: [{id: delivery, deep_context: [synthetic_canary]}]\n",
])
def test_maintenance_registry_failure_preserves_pending_proposals_and_history(
    tmp_path: Path, registry: str | None,
) -> None:
    import src.core.context_proposal_review as review

    root = tmp_path / "selected"
    program = root / "contoso"
    feedback = program / "_feedback"
    feedback.mkdir(parents=True)
    history = feedback / "context_gaps.jsonl"
    history.write_text(json.dumps({
        "ts": "2026-10-01T00:00:00Z", "feature": "doctor --context", "program": "contoso",
        "lane": "delivery", "field": "deep_context.why", "severity": "quality_degraded",
        "message": "Synthetic observation", "impact_estimate": "high",
    }) + "\n", encoding="utf-8")
    if registry is not None:
        (program / "workstream_registry.yaml").write_text(registry, encoding="utf-8")
    decoy = tmp_path / "decoy" / "contoso" / "workstream_registry.yaml"
    decoy.parent.mkdir(parents=True)
    decoy.write_text(
        "workstreams: [{id: delivery, deep_context: {why: Decoy}}]\n", encoding="utf-8",
    )
    proposal = _proposal("pending", issue_number=8, target_store="workstream_registry", conflict_key="registry")
    stage_extracted_proposals("contoso", 8, (proposal,), programs_root=root)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    projection = review.load_context_maintenance_review(
        "contoso", programs_root=root, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )

    assert projection.gaps == ()
    assert projection.gap_diagnostic == (
        "Context gap ranking unavailable; inspect the selected workstream_registry.yaml "
        "and recorded gap targets before retrying."
    )
    assert projection.proposal_diagnostic is None
    assert len(projection.proposals) == 1 and projection.proposals[0].proposal_id == "pending"
    assert "synthetic_canary" not in projection.gap_diagnostic and str(tmp_path) not in projection.gap_diagnostic
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("history", [
    "foreign-only", "foreign-structural", "foreign-first", "foreign-last", "selected-only",
])
def test_maintenance_rejects_foreign_observations_before_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str,
) -> None:
    from unittest.mock import Mock
    import src.core.context_proposal_review as review

    root = tmp_path / "programs"
    program = root / "contoso"
    feedback = program / "_feedback"
    feedback.mkdir(parents=True)
    (program / "workstream_registry.yaml").write_text(
        "workstreams: [{id: delivery, deep_context: {why: ''}}]\n", encoding="utf-8",
    )
    foreign_registry = root / "foreign_program" / "workstream_registry.yaml"
    foreign_registry.parent.mkdir()
    foreign_registry.write_text(
        "workstreams: [{id: delivery, deep_context: {why: populated}}]\n", encoding="utf-8",
    )
    selected = {
        "ts": "2026-10-01T00:00:00Z", "feature": "doctor --context", "program": "contoso",
        "lane": "delivery", "field": "deep_context.why", "severity": "quality_degraded",
        "message": "Selected observation", "impact_estimate": "high",
    }
    foreign = dict(selected, program="foreign_program", message="synthetic_foreign_canary")
    if history == "foreign-only":
        foreign.update(feature="synthetic", lane=None, field="purpose")
    records = {
        "foreign-only": [foreign],
        "foreign-structural": [foreign],
        "foreign-first": [foreign, selected],
        "foreign-last": [selected, foreign],
        "selected-only": [selected],
    }[history]
    (feedback / "context_gaps.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )
    proposal = _proposal("pending", issue_number=8, target_store="workstream_registry", conflict_key="registry")
    stage_extracted_proposals("contoso", 8, (proposal,), programs_root=root)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    rank = Mock(wraps=review.rank_context_gaps)
    monkeypatch.setattr(review, "rank_context_gaps", rank)

    projection = review.load_context_maintenance_review(
        "contoso", programs_root=root, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )

    assert len(projection.proposals) == 1
    if history == "selected-only":
        assert projection.gap_diagnostic is None and len(projection.gaps) == 1
        rank.assert_called_once()
    else:
        assert projection.gaps == ()
        assert projection.gap_diagnostic == (
            "Context gap history unavailable: observations contain a foreign program."
        )
        assert "synthetic_foreign_canary" not in repr(projection)
        rank.assert_not_called()
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("error", [OSError("synthetic_canary"), ValueError("synthetic_canary")])
def test_expected_ranking_error_is_not_mislabeled_as_history_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    import src.core.context_proposal_review as review

    proposal = _proposal("pending", issue_number=8, target_store="workstream_registry", conflict_key="registry")
    stage_extracted_proposals("contoso", 8, (proposal,), programs_root=tmp_path)
    monkeypatch.setattr(review, "rank_context_gaps", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    projection = review.load_context_maintenance_review(
        "contoso", programs_root=tmp_path, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )

    assert len(projection.proposals) == 1 and projection.gaps == ()
    assert projection.gap_diagnostic == (
        "Context gap ranking unavailable; inspect the selected workstream_registry.yaml "
        "and recorded gap targets before retrying."
    )


def test_corrupt_proposals_and_unusable_registry_keep_separate_diagnostics(tmp_path: Path) -> None:
    import src.core.context_proposal_review as review

    program = tmp_path / "contoso"
    proposals = program / "context_proposals"
    proposals.mkdir(parents=True)
    queue = proposals / "issue_001.proposals.json"
    queue.write_text("{synthetic_canary: invalid", encoding="utf-8")
    registry = program / "workstream_registry.yaml"
    registry.write_text("workstreams: [\nsynthetic_canary\n", encoding="utf-8")
    feedback = program / "_feedback"
    feedback.mkdir()
    history = feedback / "context_gaps.jsonl"
    history.write_text(json.dumps({
        "ts": "2026-10-01T00:00:00Z", "feature": "doctor --context", "program": "contoso",
        "lane": "delivery", "field": "deep_context.why", "severity": "quality_degraded",
        "message": "Synthetic observation", "impact_estimate": "high",
    }) + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (queue, registry, history)}

    projection = review.load_context_maintenance_review(
        "contoso", programs_root=tmp_path, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )

    assert projection.proposals == () and projection.gaps == ()
    assert projection.proposal_diagnostic == (
        "Context proposal history unavailable; inspect and repair it before retrying."
    )
    assert projection.gap_diagnostic == (
        "Context gap ranking unavailable; inspect the selected workstream_registry.yaml "
        "and recorded gap targets before retrying."
    )
    assert "synthetic_canary" not in repr(projection) and str(tmp_path) not in repr(projection)
    assert {path: path.read_bytes() for path in (queue, registry, history)} == before


def test_reviewer_projects_active_gap_without_candidate_and_corrupt_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "contoso" / "_feedback" / "context_gaps.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"ts":"2026-10-01T00:00:00Z","feature":"synthetic","program":"contoso","lane":"delivery",'
        '"field":"purpose","severity":"quality_degraded","message":"Purpose missing","impact_estimate":"high"}\n',
        encoding="utf-8",
    )
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = _build_context_revision_rows("contoso", programs_root=tmp_path, as_of=now)
    assert len(rows) == 1
    assert "Purpose missing" in rows[0].summary
    assert "unknown" in rows[0].summary and "observation" in rows[0].summary
    assert "2026-10-01" in rows[0].summary and "2026-11-01" in rows[0].summary
    path.write_text("{private_canary:invalid", encoding="utf-8")
    rows = _build_context_revision_rows("contoso", programs_root=tmp_path, as_of=now)
    assert len(rows) == 1 and "unavailable" in rows[0].summary.lower()
    assert "private_canary" not in rows[0].summary


def test_maintenance_unreadable_history_and_selected_root_controls(tmp_path: Path, monkeypatch) -> None:
    import src.core.context_proposal_review as review
    from unittest.mock import Mock
    from datetime import timedelta

    now = datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=2)))
    root = tmp_path / "selected"
    other = tmp_path / "other" / "contoso" / "_feedback"
    other.mkdir(parents=True)
    (other / "context_gaps.jsonl").write_text("{invalid", encoding="utf-8")
    projection = review.load_context_maintenance_review("contoso", programs_root=root, as_of=now)
    assert projection.gaps == () and projection.gap_diagnostic is None
    assert projection.evaluated_at == now.astimezone(timezone.utc)
    load = Mock(side_effect=OSError("private_canary"))
    monkeypatch.setattr(review, "load_context_gaps", load)
    projection = review.load_context_maintenance_review("contoso", programs_root=root, as_of=now)
    assert "unavailable" in projection.gap_diagnostic and "private_canary" not in projection.gap_diagnostic
    rows = _build_context_revision_rows("contoso", programs_root=root, as_of=now, edition_name="contoso_weekly")
    assert len(rows) == 1 and "--edition contoso_weekly" in rows[0].summary


def test_corrupt_proposal_read_keeps_valid_gaps_and_distinct_diagnostic(tmp_path: Path) -> None:
    import pytest
    from src.core.context_proposal_review import load_context_maintenance_review

    proposals = tmp_path / "contoso" / "context_proposals"
    proposals.mkdir(parents=True)
    queue = proposals / "issue_001.proposals.json"
    queue.write_text("{private_canary: invalid", encoding="utf-8")
    feedback = tmp_path / "contoso" / "_feedback"
    feedback.mkdir()
    (feedback / "context_gaps.jsonl").write_text(
        '{"ts":"2026-10-01T00:00:00Z","feature":"synthetic","program":"contoso","lane":null,'
        '"field":"purpose","severity":"quality_degraded","message":"Purpose missing","impact_estimate":null}\n',
        encoding="utf-8",
    )
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        load_pending_context_proposal_rows("contoso", programs_root=tmp_path)
    projection = load_context_maintenance_review("contoso", programs_root=tmp_path, as_of=now)
    assert projection.proposals == () and projection.gap_diagnostic is None
    assert projection.proposal_diagnostic is not None and "proposal" in projection.proposal_diagnostic.lower()
    assert "private_canary" not in projection.proposal_diagnostic
    assert len(projection.gaps) == 1
    rows = _build_context_revision_rows("contoso", programs_root=tmp_path, as_of=now)
    assert len(rows) == 2
    assert any("proposal" in row.title.lower() and "unavailable" in row.title.lower() for row in rows)
    assert any("unknown impact" in row.summary for row in rows)
    assert not any("None impact" in row.summary for row in rows)
    queue.unlink()
    queue.mkdir()
    projection = load_context_maintenance_review("contoso", programs_root=tmp_path, as_of=now)
    assert projection.proposal_diagnostic is not None and len(projection.gaps) == 1


def test_unexpected_projection_errors_are_not_empty_consumer_success(tmp_path: Path, monkeypatch) -> None:
    import pytest
    from src.core.cockpit_builder import _build_context_proposal_findings
    from src.commands import review_full
    import src.core.context_proposal_review as projection

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic implementation failure")

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for loader in ("load_pending_context_proposal_rows", "load_context_gaps", "rank_context_gaps"):
        with monkeypatch.context() as patch:
            patch.setattr(projection, loader, fail)
            with pytest.raises(RuntimeError):
                _build_context_proposal_findings("contoso", programs_root=tmp_path, observed_at=now)
            with pytest.raises(RuntimeError):
                review_full._build_context_revision_rows("contoso", programs_root=tmp_path, as_of=now)
