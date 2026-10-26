from __future__ import annotations

import pytest

from scripts.verify_relo import (
    _check_deployment_vars,
    _check_spec_size_budget,
    lint_specification,
    lint_work_packages,
    main,
    verify,
)


def test_verify_relo_is_read_only_and_reports_expected_checks(tmp_path) -> None:
    root = tmp_path
    (root / "specs").mkdir()
    (root / "governance").mkdir()
    config = root / "fixture-program.yaml"
    config.write_text(
        "ai:\n  blurb_deployment: ${VERTEX_AI_DEPLOYMENT}\n  exec_summary_deployment: ${VERTEX_EXEC_DEPLOYMENT}\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text('addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8")
    (root / "specs" / "relo.md").write_text("**Version:** 1.0\n\n- **v1.0 — today:** note\n\n## Table of Contents\n\n## 1. One\n", encoding="utf-8")
    (root / "governance" / "relo-work-packages.yaml").write_text("schema_version: '1'\nwork_items: []\n", encoding="utf-8")
    _write_budgets(root, line_status="ratified", line_value=100, obligation_status="ratified", obligation_value=10)
    telemetry = root / "fixture-telemetry.jsonl"
    telemetry.write_text('{"feature":"fixture"}\n', encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    results = verify(root, spec_path=root / "specs" / "relo.md", program_config=config, telemetry_paths=(telemetry,))

    assert [result.id for result in results] == [
        "RELO-W0.9a",
        "RELO-W0.7a",
        "RELO-W0.10a",
        "RELO-SPEC-LINT",
        "RELO-WORK-PACKAGES",
        "RELO-SPEC-SIZE-BUDGET",
    ]
    assert all(result.passed for result in results)
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_deployment_check_fails_on_malformed_yaml(tmp_path) -> None:
    # A text-only deployment check misses invalid YAML indentation.
    config = tmp_path / "program.yaml"
    config.write_text(
        "ai:\n  budget_usd_per_run: 0.5\n    blurb_deployment: ${VERTEX_AI_DEPLOYMENT}\n    exec_summary_deployment: ${VERTEX_EXEC_DEPLOYMENT}\n",
        encoding="utf-8",
    )

    result = _check_deployment_vars(config)

    assert result.passed is False
    assert "not valid YAML" in result.detail


def test_deployment_check_fails_on_wrong_deployment_value(tmp_path) -> None:
    config = tmp_path / "program.yaml"
    config.write_text(
        "ai:\n  blurb_deployment: gpt-hardcoded\n  exec_summary_deployment: ${VERTEX_EXEC_DEPLOYMENT}\n",
        encoding="utf-8",
    )

    result = _check_deployment_vars(config)

    assert result.passed is False
    assert "unexpected values" in result.detail


def _write_budgets(root, *, line_status: str, line_value, obligation_status: str, obligation_value) -> None:
    (root / "governance").mkdir(exist_ok=True)
    (root / "governance" / "nfr-budgets.yaml").write_text(
        "schema_version: '1'\n"
        "budgets:\n"
        "  - id: relo-spec-line-count-budget\n"
        "    area: Specification\n"
        "    description: d\n"
        "    budget_class: periodic-governance-review\n"
        "    direction: ceiling\n"
        "    unit: lines\n"
        f"    value: {line_value}\n"
        f"    status: {line_status}\n"
        "    ratified_at: null\n"
        "    ratified_by: null\n"
        "  - id: relo-spec-obligation-count-budget\n"
        "    area: Specification\n"
        "    description: d\n"
        "    budget_class: periodic-governance-review\n"
        "    direction: ceiling\n"
        "    unit: obligation-IDs\n"
        f"    value: {obligation_value}\n"
        f"    status: {obligation_status}\n"
        "    ratified_at: null\n"
        "    ratified_by: null\n",
        encoding="utf-8",
    )


def test_spec_size_budget_passes_within_ratified_ceilings(tmp_path) -> None:
    spec = tmp_path / "relo.md"
    spec.write_text("line1\nline2\n| RELO-W0.1 | item |\n", encoding="utf-8")
    _write_budgets(tmp_path, line_status="ratified", line_value=100, obligation_status="ratified", obligation_value=10)

    result = _check_spec_size_budget(spec, repo_root=tmp_path)

    assert result.passed is True


def test_spec_size_budget_fails_over_ratified_line_ceiling(tmp_path) -> None:
    spec = tmp_path / "relo.md"
    spec.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _write_budgets(tmp_path, line_status="ratified", line_value=2, obligation_status="ratified", obligation_value=10)

    result = _check_spec_size_budget(spec, repo_root=tmp_path)

    assert result.passed is False
    assert "line count" in result.detail


def test_spec_size_budget_fails_over_ratified_obligation_ceiling(tmp_path) -> None:
    spec = tmp_path / "relo.md"
    spec.write_text("| RELO-W0.1 | item |\n| RELO-W0.2 | item |\n", encoding="utf-8")
    _write_budgets(tmp_path, line_status="ratified", line_value=100, obligation_status="ratified", obligation_value=1)

    result = _check_spec_size_budget(spec, repo_root=tmp_path)

    assert result.passed is False
    assert "obligation count" in result.detail


def test_spec_size_budget_is_unavailable_when_still_a_candidate(tmp_path) -> None:
    spec = tmp_path / "relo.md"
    spec.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _write_budgets(tmp_path, line_status="candidate", line_value=1, obligation_status="candidate", obligation_value=0)

    result = _check_spec_size_budget(spec, repo_root=tmp_path)

    assert result.passed is False
    assert "not assessed" in result.detail


def test_spec_size_budget_fails_when_no_registry_present(tmp_path) -> None:
    spec = tmp_path / "relo.md"
    spec.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = _check_spec_size_budget(spec, repo_root=tmp_path)

    assert result.passed is False


def test_cli_requires_explicit_repository_and_specification() -> None:
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


def test_generic_verifier_does_not_select_programs_or_telemetry(tmp_path) -> None:
    spec = tmp_path / "fixture.md"
    spec.write_text("**Version:** 1.0\n- **v1.0 — fixture:** note\n", encoding="utf-8")
    results = verify(tmp_path, spec_path=spec)
    assert {result.id for result in results} == {
        "RELO-W0.7a", "RELO-SPEC-LINT", "RELO-WORK-PACKAGES", "RELO-SPEC-SIZE-BUDGET",
    }
    assert not all(result.passed for result in results)


def test_explicit_missing_telemetry_fails_instead_of_implying_clean_data(tmp_path) -> None:
    results = verify(tmp_path, spec_path=tmp_path / "fixture.md", telemetry_paths=(tmp_path / "missing.jsonl",))
    assert next(result for result in results if result.id == "RELO-W0.10a").passed is False


def test_spec_budget_uses_explicit_operator_registry(tmp_path) -> None:
    spec = tmp_path / "fixture.md"
    spec.write_text("one\ntwo\n", encoding="utf-8")
    operator = tmp_path / "operator"
    operator.mkdir()
    _write_budgets(operator, line_status="ratified", line_value=1, obligation_status="ratified", obligation_value=10)

    result = _check_spec_size_budget(
        spec, repo_root=tmp_path, budgets_path=operator / "governance" / "nfr-budgets.yaml",
    )

    assert result.passed is False
    assert "line count" in result.detail


def test_spec_lint_reports_version_citation_and_duplicate_section_defects(tmp_path) -> None:
    spec = tmp_path / "relo.md"
    spec.write_text(
        "**Version:** 2.0\n\n- **v1.0 — today:** note\n\n## 1. One\n\n## 1. Duplicate\n\nSee §2.0.\n",
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "header version" in result.detail
    assert "duplicate section" in result.detail
    assert "unresolved section" in result.detail


def test_work_package_lint_rejects_missing_cards_and_unresolved_agent_paths(tmp_path) -> None:
    (tmp_path / "governance").mkdir()
    spec = tmp_path / "relo.md"
    spec.write_text(
        "### 11.1 Phase RELO-0\n\n| RELO-W0.1 | item |\n\n### 11.2 Phase RELO-1\n\n| RELO-W1.1 | item |\n\n### 11.3 Next\n\n**RELO-D4**\n",
        encoding="utf-8",
    )
    (tmp_path / "governance" / "relo-work-packages.yaml").write_text(
        "schema_version: '1'\nwork_items:\n  - id: RELO-W0.1\n    class: AGENT_READY\n    depends_on: []\n    read_first: [missing.py]\n    change: [missing.py]\n    acceptance: [test]\n    forbidden: [no]\n    rollback: undo\n",
        encoding="utf-8",
    )

    result = lint_work_packages(tmp_path, spec_path=spec)

    assert result.passed is False
    assert "missing work-package card" in result.detail
    assert "unresolved file path" in result.detail
