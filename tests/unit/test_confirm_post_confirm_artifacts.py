"""Direct coverage for extracted confirm post-confirm artifact helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.commands.confirm_stages import post_confirm_artifacts as artifacts_module
from src.core.config_loader import EditorialRules
from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import ConfigError
from src.core.models import ReportData
from src.core.ncfl_models import ContextUpdateProposal

EDITION = "acme_weekly"
PROGRAM_ID = "acme"


def _edition_output(programs_root: Path) -> Path:
    return get_program_output_dir(EDITION, programs_root=programs_root)


def test_record_review_tracking_returns_none_when_artifact_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    result = artifacts_module.record_review_tracking(
        edition_name=EDITION,
        issue_number=1,
        draft_state={},
        report=cast(ReportData, SimpleNamespace()),
        programs_root=programs_root,
    )

    assert result == (None, None, None)


def test_record_review_tracking_warns_on_invalid_payload(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    issue_dir = _edition_output(programs_root) / "issue_001"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue_001.review.json").write_text(json.dumps(["bad"]), encoding="utf-8")

    path, summary, warning = artifacts_module.record_review_tracking(
        edition_name=EDITION,
        issue_number=1,
        draft_state={},
        report=cast(ReportData, SimpleNamespace()),
        programs_root=programs_root,
    )

    assert path is None
    assert summary is None
    assert warning is not None


def test_record_review_tracking_warns_on_unknown_rendered_kusto_query_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    issue_dir = _edition_output(programs_root) / "issue_001"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue_001.review.json").write_text(
        json.dumps(
            {
                "issue_number": 1,
                "info_messages": [],
                "review_report": {
                    "issue_number": 1,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": ["fabricated-query"],
            }
        ),
        encoding="utf-8",
    )

    path, summary, warning = artifacts_module.record_review_tracking(
        edition_name=EDITION,
        issue_number=1,
        draft_state={"kusto_sections": [{"query_id": "velocity-p50"}]},
        report=cast(ReportData, SimpleNamespace()),
        programs_root=programs_root,
    )

    assert path is None
    assert summary is None
    assert warning is not None
    assert "unknown ids: fabricated-query" in warning


def test_record_review_tracking_warns_on_unknown_reviewed_section_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    issue_dir = _edition_output(programs_root) / "issue_001"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue_001.review.json").write_text(
        json.dumps(
            {
                "issue_number": 1,
                "info_messages": [],
                "review_report": {
                    "issue_number": 1,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {"ws:fabricated": "invented section"},
                "rendered_kusto_query_ids": [],
            }
        ),
        encoding="utf-8",
    )

    report: Any = SimpleNamespace(exec_summary_text="Summary", workstream_blurbs={"deployment": "Deployment text"})

    path, summary, warning = artifacts_module.record_review_tracking(
        edition_name=EDITION,
        issue_number=1,
        draft_state={},
        report=cast(ReportData, report),
        programs_root=programs_root,
    )

    assert path is None
    assert summary is None
    assert warning is not None
    assert "unknown section ids: ws:fabricated" in warning


def test_record_learning_distillation_writes_outputs(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    issue_dir = _edition_output(programs_root) / "issue_001"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue_001.review_tracking.json").write_text("{}", encoding="utf-8")

    class _FakeDistiller:
        def distill(self, *, editorial_rules, tracking_reports):
            del editorial_rules, tracking_reports
            return {"proposal_count": 1}

    monkeypatch.setattr(artifacts_module, "load_tracking_reports", lambda root: ({"issue_number": 1},))
    monkeypatch.setattr(artifacts_module, "build_default_learning_distiller", lambda *, trace_context: _FakeDistiller())
    monkeypatch.setattr(artifacts_module, "render_learning_markdown", lambda distillation: f"md:{distillation['proposal_count']}")
    monkeypatch.setattr(artifacts_module, "render_learning_summary", lambda distillation: "summary")

    def _write_text(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path

    def _write_json(path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    monkeypatch.setattr(artifacts_module, "_write_output_text", _write_text)
    monkeypatch.setattr(artifacts_module, "_write_output_json", _write_json)

    md_path, json_path, summary, warning = artifacts_module.record_learning_distillation(
        edition_name=EDITION,
        issue_number=1,
        editorial_rules=cast(EditorialRules, SimpleNamespace()),
        programs_root=programs_root,
    )

    assert md_path is not None and md_path.exists()
    assert json_path is not None and json_path.exists()
    assert summary == "summary"
    assert warning is None


@pytest.mark.parametrize("selected", [False, True])
def test_record_edit_patterns_for_v2_appends_patterns(monkeypatch, tmp_path: Path, selected: bool) -> None:
    captured: dict[str, object] = {}
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    selected_root = tmp_path / "selected" / "programs"
    if selected:
        selected_edition = selected_root / "acme" / "editions" / f"{EDITION}.yaml"
        selected_edition.parent.mkdir(parents=True)
        selected_edition.write_text("program_id: acme\n", encoding="utf-8")
    expected_root = selected_root if selected else reports_root.parent / "programs"

    def resolve_edition(*args, **kwargs):
        captured["resolved_programs_root"] = kwargs["programs_root"]
        return SimpleNamespace(program=SimpleNamespace(id=PROGRAM_ID))

    monkeypatch.setattr(artifacts_module, "resolve_edition", resolve_edition)
    monkeypatch.setattr(
        artifacts_module,
        "build_edit_patterns",
        lambda **kwargs: ("pattern-1",),
    )
    monkeypatch.setattr(
        artifacts_module,
        "append_edit_patterns",
        lambda program_id, patterns, *, programs_root: captured.update(
            program_id=program_id,
            patterns=patterns,
            programs_root=programs_root,
        ),
    )

    report: Any = SimpleNamespace(exec_summary_text="confirmed", workstream_blurbs={})

    warning = artifacts_module.record_edit_patterns_for_v2(
        edition_name=EDITION,
        issue_number=1,
        draft_state={"exec_summary_text": "draft", "workstream_blurbs": {}, "ai_prompt_versions": {}, "ai_confidences": {}},
        report=cast(ReportData, report),
        confirmed_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        **({"programs_root": selected_root} if selected else {}),
    )

    assert warning is None
    assert captured["program_id"] == PROGRAM_ID
    assert captured["patterns"] == ("pattern-1",)
    assert captured["resolved_programs_root"] == expected_root
    assert captured["programs_root"] == expected_root


@pytest.mark.parametrize("root_case", ["missing", "relative", "legacy_decoy"])
def test_record_edit_patterns_rejects_invalid_explicit_root_before_reads_or_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_case: str,
) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    if root_case == "missing":
        selected_root = tmp_path / "selected" / "missing-programs"
    elif root_case == "relative":
        selected_root = Path("selected-relative-programs")
    else:
        selected_root = tmp_path / "selected" / "programs"
        selected_root.mkdir(parents=True)
        decoy_editions = selected_root.parent / "editions"
        decoy_editions.mkdir()
        (decoy_editions / f"{EDITION}.yaml").write_text("program_id: acme\n", encoding="utf-8")

    resolver_calls = []
    artifact_calls = []
    monkeypatch.setattr(
        artifacts_module,
        "resolve_edition",
        lambda *args, **kwargs: resolver_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        artifacts_module,
        "build_edit_patterns",
        lambda **kwargs: artifact_calls.append(("build", kwargs)) or ("pattern-1",),
    )
    monkeypatch.setattr(
        artifacts_module,
        "append_edit_patterns",
        lambda *args, **kwargs: artifact_calls.append(("append", args, kwargs)),
    )

    with pytest.raises(ConfigError):
        artifacts_module.record_edit_patterns_for_v2(
            edition_name=EDITION,
            issue_number=1,
            draft_state={},
            report=cast(ReportData, SimpleNamespace(exec_summary_text="confirmed", workstream_blurbs={})),
            confirmed_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
            reports_root=reports_root,
            programs_root=selected_root,
        )

    assert resolver_calls == []
    assert artifact_calls == []


def test_record_workstream_associations_warns_on_non_object_entries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    issue_dir = _edition_output(programs_root) / "issue_001"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue_001.workstream_associations.json").write_text(
        json.dumps(
            [
                {
                    "recorded_at": "2026-06-06T12:00:00+00:00",
                    "edition": EDITION,
                    "issue_number": 1,
                    "workstream_id": "deployment",
                    "source_type": "review",
                    "source_slice_id": None,
                    "section_id": "ws:deployment",
                    "work_item_id": 123,
                    "note": "kept",
                },
                "bad-entry",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        artifacts_module,
        "append_workstream_association_records",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not append malformed association payloads")),
    )

    path, warning = artifacts_module.record_workstream_associations(
        edition_name=EDITION,
        issue_number=1,
        program_id=PROGRAM_ID,
        programs_root=programs_root,
    )

    assert path is None
    assert warning is not None
    assert "contains non-object entries" in warning


def test_record_workstream_associations_warns_on_mismatched_issue_number(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    issue_dir = _edition_output(programs_root) / "issue_001"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue_001.workstream_associations.json").write_text(
        json.dumps(
            [
                {
                    "recorded_at": "2026-06-06T12:00:00+00:00",
                    "edition": EDITION,
                    "issue_number": 2,
                    "workstream_id": "deployment",
                    "source_type": "review",
                    "source_slice_id": None,
                    "section_id": "ws:deployment",
                    "work_item_id": 123,
                    "note": "kept",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        artifacts_module,
        "append_workstream_association_records",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not append mismatched association payloads")),
    )

    path, warning = artifacts_module.record_workstream_associations(
        edition_name=EDITION,
        issue_number=1,
        program_id=PROGRAM_ID,
        programs_root=programs_root,
    )

    assert path is None
    assert warning is not None
    assert "different issue number" in warning


def test_record_ncfl_proposals_stages_extracted_proposals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root = tmp_path / "reports"
    proposal = ContextUpdateProposal(
        proposal_id="prop-1",
        program_id=PROGRAM_ID,
        issue_number=1,
        edition_id=EDITION,
        source_type="confirmed_overrides",
        extracted_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        extractor_version="1.0.0",
        source_artifact="overrides/issue_001.yaml",
        source_field="scorecards.delivery.control-plane.risk",
        extraction_method="overrides_yaml",
        target_store="risk_register",
        target_key="control-plane",
        target_field="dimension_risk_level",
        source_value="high",
        current_value="medium",
        current_value_hash="abc",
        confidence="high",
        batch_eligible=True,
        extraction_method_rationale="test",
        conflict_key="conflict-1",
    )
    monkeypatch.setattr(artifacts_module, "extract_proposals", lambda *args, **kwargs: (proposal,))

    path, count = artifacts_module.record_ncfl_proposals(
        edition_name=EDITION,
        issue_number=1,
        program_id=PROGRAM_ID,
        reports_root=reports_root,
        programs_root=programs_root,
    )

    assert count == 1
    assert path is not None and path.exists()
