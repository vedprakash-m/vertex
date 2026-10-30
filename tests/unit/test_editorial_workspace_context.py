from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import metric_registry, narrative_store, quality_gates, reality_store
from src.core.exceptions import ConfigError
from src.core.models import FreshnessReport
from src.core.quality_gates import editorial


def test_editorial_command_reads_explicit_root_overrides(tmp_path: Path, monkeypatch) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root
    from src.commands import editor
    from src.core import overrides_store
    import typer

    root = _selected_artifact_root(tmp_path)
    document = overrides_store.OverridesDocument(issue_number=7, top_3_now=(), scorecards=(), edition_intro="selected")
    selected = root / "fixture" / "overrides" / "issue_007.yaml"
    selected.parent.mkdir()
    selected.write_text(overrides_store._render_overrides_document(document), encoding="utf-8")
    monkeypatch.setattr(editor, "load_editorial_rules", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(editor, "load_persona_registry", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(editor, "_load_published_baseline", lambda *_args, **_kwargs: None)
    runner = Mock(return_value=None)
    monkeypatch.setattr(editor, "run_persona_checks", runner)
    with pytest.raises(typer.Exit) as exit_info:
        editor.editor_report_command(
            edition="fixture_weekly", issue=7, output_format="json", programs_root=root, reports_root=tmp_path / "reports",
        )
    assert exit_info.value.exit_code == 0
    assert runner.call_args.kwargs["overrides"] == document
    assert not (tmp_path / "reports").exists()


def test_explicit_narrative_placeholder_dependencies_use_selected_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root
    from src.core import section_proposal_store

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "unrelated" / "reports"
    loader = Mock(return_value={})
    reality = Mock()
    hints = Mock(return_value=())
    monkeypatch.setattr(metric_registry, "load_metric_definition_map", loader)
    monkeypatch.setattr(reality_store, "RealityStore", reality)
    monkeypatch.setattr(section_proposal_store, "load_hint_proposals", hints)
    narratives = {"summary.md": "Synthetic narrative"}
    assert narrative_store.inject_narrative_placeholders(
        "fixture_weekly", 7, narratives, reports, programs_root=root,
    ) == narratives
    loader.assert_called_once_with(metrics_root=root.parent / "knowledge" / "metrics")
    reality.assert_called_once_with("fixture", db_root=root.parent / "vertex-db")
    hints.assert_called_once_with("fixture", 7, programs_root=root)
    assert not reports.parent.exists()


@pytest.mark.parametrize("scope", ["alpha", "beta"])
def test_narrative_metrics_use_selected_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    reports_root = tmp_path / scope / "reports"
    monkeypatch.setattr(
        "src.core.edition_resolver.resolve_edition_paths",
        Mock(return_value=SimpleNamespace(
            program_id="fixture", program_dir=reports_root.parent / "programs" / "fixture",
        )),
    )
    loader = Mock(return_value={})
    monkeypatch.setattr(metric_registry, "load_metric_definition_map", loader)
    monkeypatch.setattr(reality_store, "RealityStore", Mock())
    narratives = {"summary.md": "Synthetic narrative"}
    assert narrative_store.inject_narrative_placeholders(
        "fixture", 7, narratives, reports_root=reports_root,
    ) == narratives
    loader.assert_called_once_with(metrics_root=reports_root.parent / "knowledge" / "metrics")


def test_exec_summary_gate_uses_selected_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = Mock(return_value=[])
    monkeypatch.setattr(
        "src.core.exec_summary_diff_engine.check_exec_summary_staleness", checker,
    )
    reports_root = tmp_path / "selected" / "reports"
    result = editorial.evaluate_exec_summary_staleness_gate(
        "fixture", 7, reports_root=reports_root,
    )
    assert result.passed
    checker.assert_called_once_with(edition="fixture", issue_number=7, reports_root=reports_root)
    assert not list(tmp_path.iterdir())


def test_direct_exec_summary_gate_preserves_omitted_root_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = Mock(return_value=[])
    monkeypatch.setattr(
        "src.core.exec_summary_diff_engine.check_exec_summary_staleness", checker,
    )
    assert editorial.evaluate_exec_summary_staleness_gate("fixture", 7).passed
    checker.assert_called_once_with(
        edition="fixture", issue_number=7, reports_root=narrative_store.REPORTS_ROOT,
    )


@pytest.mark.parametrize("edition,issue", [(None, 7), ("fixture", None)])
def test_missing_exec_summary_identity_skips_before_access(
    monkeypatch: pytest.MonkeyPatch, edition: str | None, issue: int | None,
) -> None:
    checker = Mock(side_effect=AssertionError("No narrative access without identity"))
    monkeypatch.setattr(
        "src.core.exec_summary_diff_engine.check_exec_summary_staleness", checker,
    )
    assert editorial.evaluate_exec_summary_staleness_gate(edition, issue).passed
    checker.assert_not_called()


@pytest.mark.parametrize("count", [1, 2])
def test_selected_exec_summary_staleness_remains_a_failed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    findings = [
        SimpleNamespace(is_stale=True, workstream_id=f"fixture_{index}")
        for index in range(count)
    ]
    checker = Mock(return_value=findings)
    monkeypatch.setattr(
        "src.core.exec_summary_diff_engine.check_exec_summary_staleness", checker,
    )
    result = editorial.evaluate_exec_summary_staleness_gate(
        "fixture", 7, reports_root=tmp_path / "reports",
    )
    assert not result.passed
    assert result.exit_code == 1
    assert all(finding.workstream_id in result.message for finding in findings)


def test_selected_exec_summary_error_remains_an_explicit_failed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.core.exec_summary_diff_engine.check_exec_summary_staleness",
        Mock(side_effect=ConfigError("Synthetic narrative failure")),
    )
    result = editorial.evaluate_exec_summary_staleness_gate(
        "fixture", 7, reports_root=tmp_path / "reports",
    )
    assert not result.passed
    assert result.exit_code == 1
    assert "Synthetic narrative failure" in result.message


@pytest.mark.parametrize("explicit_db", [False, True])
@pytest.mark.parametrize("placeholder", [False, True])
def test_metric_gate_uses_selected_or_explicitly_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_db: bool, placeholder: bool,
) -> None:
    programs_root = tmp_path / "selected" / "programs"
    expected = programs_root.parent / "vertex-db" / "fixture" / "vertex.sqlite3"
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    if explicit_db:
        expected = tmp_path / "configured" / "vertex.sqlite3"
        monkeypatch.setenv("VERTEX_DB_PATH", str(expected))
    factory = Mock(wraps=reality_store.RealityStore)
    monkeypatch.setattr(reality_store, "RealityStore", factory)
    result = editorial.evaluate_metric_injection_and_ado_hygiene_gate(
        program_id="fixture",
        narratives=("<!-- vertex:metric: fixture_metric -->",) if placeholder else (),
        programs_root=programs_root,
    )
    assert result.passed is (not placeholder)
    if placeholder:
        assert "cannot be resolved from reality_store: fixture_metric" in result.message
    factory.assert_called_once_with("fixture", db_root=expected)
    assert expected.is_file()


def test_metric_gate_without_program_does_not_open_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = Mock(side_effect=AssertionError("No database without a program"))
    monkeypatch.setattr(reality_store, "RealityStore", factory)
    assert editorial.evaluate_metric_injection_and_ado_hygiene_gate(
        program_id=None, narratives=(),
    ).passed
    factory.assert_not_called()


def test_selected_metric_database_error_remains_an_explicit_failed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    factory = Mock(side_effect=ConfigError("Synthetic database failure"))
    monkeypatch.setattr(reality_store, "RealityStore", factory)
    result = editorial.evaluate_metric_injection_and_ado_hygiene_gate(
        program_id="fixture", narratives=(), programs_root=tmp_path / "programs",
    )
    assert not result.passed
    assert result.exit_code == 1
    assert "Synthetic database failure" in result.message
    factory.assert_called_once_with(
        "fixture", db_root=tmp_path / "vertex-db" / "fixture" / "vertex.sqlite3",
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("explicit_reports", [False, True])
def test_phase_1b_forwards_selected_editorial_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_reports: bool,
) -> None:
    programs_root = tmp_path / "selected" / "programs"
    reports_root = tmp_path / "selected-reports" if explicit_reports else programs_root.parent / "reports"
    summary_gate = Mock(return_value=SimpleNamespace())
    metric_gate = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(quality_gates, "_evaluate_exec_summary_staleness_gate", summary_gate)
    monkeypatch.setattr(quality_gates, "_evaluate_metric_injection_and_ado_hygiene_gate", metric_gate)
    quality_gates.evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(7, (), 0, 0, 0), edition_name="fixture", issue_number=7,
        programs_root=programs_root, reports_root=reports_root if explicit_reports else None,
    )
    summary_gate.assert_called_once_with(
        edition_name="fixture", issue_number=7, reports_root=reports_root,
    )
    metric_gate.assert_called_once_with(
        program_id=None, narratives=(), items=(), programs_root=programs_root,
    )


@pytest.mark.parametrize("relative_path", [
    Path("src", "core", "stages", "validation_stage.py"),
    Path("src", "commands", "confirm.py"),
    Path("src", "commands", "triage.py"),
    Path("src", "commands", "report_pipeline", "assemble_stage.py"),
])
def test_production_gate_callers_forward_reports_root(relative_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "evaluate_phase_1b_gates"
    ]
    assert calls
    assert all(any(keyword.arg == "reports_root" for keyword in call.keywords) for call in calls)
