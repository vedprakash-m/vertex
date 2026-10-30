from __future__ import annotations

import json

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


@pytest.mark.parametrize("column", [1, 2, 3, 4])
def test_spec_lint_rejects_empty_declared_traceability_cells(tmp_path, column) -> None:
    columns = ["RELO-FR-03", "W2.1, W2.2", "QG-04", "relation contract", "RELO-2"]
    columns[column] = " "
    spec = _write_traceability_spec(tmp_path, "| " + " | ".join(columns) + " |")
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-FR-03" in result.detail
    assert "five nonempty columns" in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize("row", [
    "| RELO-NFR-01 | W0.8 | QG-07 | evidence |",
    "| RELO-NFR-01 | W0.8 | QG-07 | evidence | RELO-0 | extra |",
    "| RELO-NFR-01 | W0.8 | QG-07 | evidence | RELO-0",
])
def test_spec_lint_rejects_malformed_declared_traceability_rows(tmp_path, row) -> None:
    result = lint_specification(_write_traceability_spec(tmp_path, row))

    assert result.passed is False
    assert "RELO-NFR-01" in result.detail
    assert "five nonempty columns" in result.detail


def test_spec_lint_preserves_traceability_prose_ranges_and_section_scope(tmp_path) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1, W2.2 | QG-04 | `test_ado_relation_contract.py` + inventory | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5–W6.6 | Architecture/deletion ratchets | caller-aware audit | RELO-6 |\n"
        "### 19.3 Other traceability\n"
        "| RELO-FR-03 | intentionally different table shape |",
    )

    assert lint_specification(spec).passed is True


@pytest.mark.parametrize(("rows", "missing"), [
    ("| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |", ["RELO-NFR-09"]),
    ("| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |", ["RELO-FR-03"]),
    ("", ["RELO-FR-03", "RELO-NFR-09"]),
    (
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "RELO-NFR-09 is mentioned here.\n"
        "### 19.3 Other traceability\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
        ["RELO-NFR-09"],
    ),
])
def test_spec_lint_rejects_missing_defined_requirement_rows(tmp_path, rows, missing) -> None:
    spec = _write_traceability_spec(tmp_path, rows)
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert "missing requirement traceability row(s): " + ", ".join(missing) in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize(("row", "missing"), [
    ("| RELO-FR-99 | W2.1 | QG-04 | evidence | RELO-2 |", "RELO-FR-99"),
    ("| RELO-FR-03 | W2.99 | QG-04 | evidence | RELO-2 |", "RELO-W2.99"),
    ("| RELO-FR-03 | W2.1 | QG-99 | evidence | RELO-2 |", "RELO-QG-99"),
    ("| RELO-FR-03 | W2.1-W2.3 | QG-04 | evidence | RELO-2 |", "RELO-W2.3"),
    ("| RELO-FR-03 | W2.1 | QG-04–QG-06 | evidence | RELO-2 |", "RELO-QG-06"),
])
def test_spec_lint_rejects_unresolved_explicit_trace_references(tmp_path, row, missing) -> None:
    spec = _write_traceability_spec(tmp_path, row)
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert missing in result.detail
    assert "unresolved traceability reference" in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize("reference", [
    "W2.2-W2.1",
    "W2.1-W3.1",
    "W2.1-QG-04",
    "W0.1a-W0.1b",
    "W0.1-W0.1a",
    "QG-05–QG-04",
])
def test_spec_lint_rejects_invalid_trace_reference_ranges(tmp_path, reference) -> None:
    spec = _write_traceability_spec(
        tmp_path, f"| RELO-FR-03 | {reference} | QG-04 | evidence | RELO-2 |",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "invalid traceability range" in result.detail


@pytest.mark.parametrize(("work", "gate"), [
    ("W2.1–W2.2", "QG-04-QG-05"),
    ("RELO-W2.1-RELO-W2.2", "RELO-QG-04–RELO-QG-05"),
    ("RELO-W0.1a, W2.1-W2.1", "All"),
    ("W2.1", "Migration gates"),
])
def test_spec_lint_resolves_trace_shorthand_ranges_children_and_gate_prose(tmp_path, work, gate) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | {work} | {gate} | named evidence not a file | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | Architecture/deletion ratchets | audit | RELO-6 |",
    )

    assert lint_specification(spec).passed is True


def test_traceability_rows_do_not_define_their_own_requirement_or_work(tmp_path) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-99 | W2.99 | QG-99 | evidence | RELO-2 |\n"
        "| RELO-FR-03 | W2.99 | QG-99 | evidence | RELO-2 |",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-FR-99" in result.detail
    assert "RELO-W2.99" in result.detail
    assert "RELO-QG-99" in result.detail


def test_traceability_range_requires_interior_definitions(tmp_path) -> None:
    spec = _write_traceability_spec(
        tmp_path, "| RELO-FR-03 | W2.1-W2.3 | QG-04 | evidence | RELO-2 |",
    )
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "| RELO-W2.2 | endpoints |", "| RELO-W2.3 | endpoints |",
        ),
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "unresolved traceability reference: RELO-W2.2" in result.detail


@pytest.mark.parametrize("gate", ["", "QG-99", "QG-04"])
def test_verifier_cli_rejects_invalid_traceability_without_mutation(tmp_path, capsys, gate) -> None:
    spec = _write_traceability_spec(
        tmp_path, f"| RELO-FR-03 | W2.1, W2.2 | {gate} | relation contract | RELO-2 |",
    )
    (tmp_path / "pyproject.toml").write_text(
        'addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8",
    )
    _write_budgets(
        tmp_path, line_status="ratified", line_value=100,
        obligation_status="ratified", obligation_value=10,
    )
    (tmp_path / "governance" / "relo-work-packages.yaml").write_text(
        "schema_version: '1'\nwork_items: []\n", encoding="utf-8",
    )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    exit_code = main(["--repo-root", str(tmp_path), "--spec", str(spec), "--json"])

    results = json.loads(capsys.readouterr().out)
    assert exit_code == 6
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-SPEC-LINT"]
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def _write_traceability_spec(tmp_path, rows):
    spec = tmp_path / "relo.md"
    spec.write_text(
        "**Version:** 1.0\n\n- **v1.0 — synthetic:** note\n\n"
        "## Table of Contents\n## 5 Requirements\n"
        "| **RELO-FR-03** | relation semantics |\n"
        "| **RELO-NFR-09** | architecture |\n"
        "## 11 Work\n"
        "| RELO-W0.1a | child |\n"
        "| RELO-W2.1 | relations |\n"
        "| RELO-W2.2 | endpoints |\n"
        "| RELO-W6.5 | deletion |\n"
        "| RELO-W6.6 | retirement |\n"
        "## 16 Gates\n"
        "| **RELO-QG-04 — Dependency integrity** | check |\n"
        "| **RELO-QG-05 — Truth consistency** | check |\n"
        "## 19 Traceability\n"
        "### 19.2 Requirement-to-delivery traceability\n"
        "| Requirement | Work items | Gate | Primary test/evidence | Phase |\n"
        "|---|---|---|---|---|\n" + rows + "\n",
        encoding="utf-8",
    )
    return spec


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
