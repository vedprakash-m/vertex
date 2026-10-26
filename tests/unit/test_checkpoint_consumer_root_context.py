from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src.commands import edit, evidence, facts, publish_gate
from src.core.quality_gates import state_authority
from tests.support.synthetic_report_source import build_synthetic_report_source


@pytest.mark.parametrize(("function", "callee", "keywords"), [
    (evidence._load_lineage_context, "load_bundle", ("programs_root",)),
    (evidence._load_lineage_context, "load_trusted_baseline_issue", ("programs_root",)),
    (edit.run_edit, "_prepare_edit_target", ("programs_root",)),
    (edit._prepare_edit_target, "_resolve_edit_section_target", ("programs_root",)),
    (edit._prepare_edit_target, "_scaffold_content", ("programs_root",)),
    (edit._resolve_edit_section_target, "load_bundle", ("programs_root",)),
    (edit._scaffold_content, "load_bundle", ("programs_root",)),
    (publish_gate.publish_gate_command, "confirm_issue", ("reports_root", "archive_root", "programs_root")),
    (publish_gate.publish_gate_command, "_load_persona_signal_failures", ("programs_root",)),
    (facts.run_facts_parity_check, "load_skipped_issues_for_program", ("programs_root",)),
    (facts.facts_parity_check, "run_facts_parity_check", ("programs_root", "editions_root", "archive_root")),
    (state_authority.find_stray_fact_store_databases, "get_program_reality_db_path", ("programs_root",)),
    (state_authority.evaluate_state_authority_gate, "get_program_reality_db_path", ("programs_root",)),
])
def test_consumers_forward_selected_root_names(function, callee: str, keywords: tuple[str, ...]) -> None:
    tree = ast.parse(inspect.getsource(function))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == callee
    ]
    assert calls
    for call in calls:
        arguments = {keyword.arg: keyword.value for keyword in call.keywords}
        for keyword in keywords:
            argument = arguments.get(keyword)
            assert isinstance(argument, ast.Name), f"{function.__name__}: {callee} must forward {keyword}"
            assert argument.id == (
                keyword.upper()
                if function in (publish_gate.publish_gate_command, facts.facts_parity_check)
                else keyword
            )


@pytest.mark.parametrize("section", ["exec_summary", "deployment_readiness"])
def test_edit_scaffolds_from_selected_programs_without_sibling_assumption(
    tmp_path: Path, section: str,
) -> None:
    source = build_synthetic_report_source(tmp_path / "selected")
    reports_root = tmp_path / "independent" / "reports"
    path, section_id, created = edit._prepare_edit_target(
        edition_name="acme_weekly", section=section, issue_number=1,
        reports_root=reports_root, programs_root=source / "programs",
    )
    assert created
    assert section_id == section
    assert path.is_relative_to(reports_root)
    assert "vertex:scaffold" in path.read_text(encoding="utf-8")


def test_state_authority_uses_selected_canonical_root_without_explicit_db_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    programs_root = tmp_path / "selected" / "programs"
    stray = programs_root / "fixture" / "vertex.sqlite3"
    stray.parent.mkdir(parents=True)
    stray.touch()
    evaluation = state_authority.evaluate_state_authority_gate("fixture", programs_root=programs_root)
    assert not evaluation.passed
    assert not evaluation.forceable
    assert str(programs_root.parent / "vertex-db" / "fixture" / "vertex.sqlite3") in evaluation.message
    assert "programs_root_relative" in evaluation.message
    with pytest.raises(state_authority.StateAuthorityAmbiguousError):
        state_authority.assert_state_authority_or_raise("fixture", programs_root=programs_root)
