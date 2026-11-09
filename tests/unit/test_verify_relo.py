from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

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


@pytest.mark.parametrize("wrapper", ["<!--\n{}\n-->", "```text\n{}\n```"])
def test_requirement_table_scope_ignores_hidden_requirement_rows(tmp_path, wrapper) -> None:
    hidden = wrapper.format("| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |")
    spec = _write_traceability_spec(
        tmp_path, "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n" + hidden,
    )
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert "missing requirement traceability row(s): RELO-NFR-09" in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize("wrapper", ["<!--\n{}\n-->", "```text\n{}\n```"])
@pytest.mark.parametrize(("definition", "row", "missing"), [
    (
        "| RELO-W9.9 | hidden work item |",
        "| RELO-FR-03 | W9.9 | QG-04 | evidence | RELO-2 |",
        "RELO-W9.9",
    ),
    (
        "| **RELO-QG-99 — Hidden** | hidden gate |",
        "| RELO-FR-03 | W2.1 | QG-99 | evidence | RELO-2 |",
        "RELO-QG-99",
    ),
    (
        "| **RELO-FR-99** | hidden requirement |",
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-FR-99 | W2.1 | QG-04 | evidence | RELO-2 |",
        "RELO-FR-99",
    ),
])
def test_requirement_table_scope_ignores_hidden_definitions(tmp_path, wrapper, definition, row, missing) -> None:
    spec = _write_traceability_spec(
        tmp_path, row + "\n| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )
    content = spec.read_text(encoding="utf-8").replace(
        "## 16 Gates\n", wrapper.format(definition) + "\n## 16 Gates\n",
    )
    spec.write_text(content, encoding="utf-8")
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert f"unresolved traceability reference(s): {missing}" in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize("wrapper", ["<!--\n{}\n-->", "```text\n{}\n```"])
def test_requirement_table_scope_ignores_hidden_invalid_rows(tmp_path, wrapper) -> None:
    hidden = wrapper.format("| RELO-FR-99 | W9.9 | QG-99 | evidence | RELO-2 |")
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |\n" + hidden,
    )
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


def test_requirement_table_scope_does_not_count_an_unrelated_table(tmp_path) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n\n"
        "| Other | Work | Gate | Evidence | Phase |\n|---|---|---|---|---|\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "missing requirement traceability row(s): RELO-NFR-09" in result.detail


def test_requirement_table_scope_ignores_invalid_rows_in_an_unrelated_table(tmp_path) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |\n\n"
        "| Other | Work | Gate | Evidence | Phase |\n|---|---|---|---|---|\n"
        "| RELO-FR-99 | W9.9 | QG-99 | evidence | RELO-2 |",
    )

    assert lint_specification(spec).passed is True


@pytest.mark.parametrize(("header", "separator"), [
    ("| Other | Work items | Gate | Primary test/evidence | Phase |", "|---|---|---|---|---|"),
    ("| Requirement | Work items | Gate | Primary test/evidence | Phase | Extra |", "|---|---|---|---|---|---|"),
    ("| Requirement | Work items | Gate | Primary test/evidence | Phase |", ""),
    ("| Requirement | Work items | Gate | Primary test/evidence | Phase |", "|---|---|invalid|---|---|"),
])
def test_requirement_table_scope_rejects_invalid_header(tmp_path, header, separator) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )
    content = spec.read_text(encoding="utf-8").replace(
        "| Requirement | Work items | Gate | Primary test/evidence | Phase |\n|---|---|---|---|---|\n",
        header + "\n" + separator + "\n",
    )
    spec.write_text(content, encoding="utf-8")

    result = lint_specification(spec)

    assert result.passed is False
    assert "requirement traceability table is missing or malformed" in result.detail


@pytest.mark.parametrize("line_ending", [
    pytest.param("\n", id="lf"),
    pytest.param("\r\n", id="crlf"),
])
@pytest.mark.parametrize("aligned", [False, True])
def test_requirement_table_scope_accepts_visible_complete_table(tmp_path, line_ending, aligned) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1–W2.2 | QG-04–QG-05 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | Non-QG gate prose | evidence | RELO-6 |",
    )
    content = spec.read_text(encoding="utf-8").replace(
        "| Requirement | Work items | Gate | Primary test/evidence | Phase |\n",
        "```text\n| RELO-FR-99 | W9.9 | QG-99 | ignored | RELO-2 |\n```\n"
        "| Requirement | Work items | Gate | Primary test/evidence | Phase |\n",
    )
    if aligned:
        content = content.replace("|---|---|---|---|---|", "|:---|---:|:---:|---|---|")
    spec.write_bytes(content.replace("\n", line_ending).encode("utf-8"))
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


@pytest.mark.parametrize("line_ending", [
    pytest.param("\n", id="lf"),
    pytest.param("\r\n", id="crlf"),
])
def test_requirement_table_scope_standalone_json_cli_preserves_inputs(tmp_path, line_ending) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "<!--\n| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |\n-->",
    )
    spec.write_bytes(spec.read_text(encoding="utf-8").replace("\n", line_ending).encode("utf-8"))
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

    process = subprocess.run(
        [
            sys.executable, "-B", str(Path(__file__).resolve().parents[2] / "scripts" / "verify_relo.py"),
            "--repo-root", str(tmp_path), "--spec", str(spec), "--json",
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert process.returncode == 6
    assert process.stderr == ""
    results = json.loads(process.stdout)
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-SPEC-LINT"]
    assert "missing requirement traceability row(s): RELO-NFR-09" in next(
        row["detail"] for row in results if row["id"] == "RELO-SPEC-LINT"
    )
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(("work", "gate"), [
    ("TBD", "QG-04"),
    ("No work assigned", "Migration gates"),
    ("QG-04", "QG-04"),
    ("Pending", "W2.1, QG-04"),
    ("RELO-FR-03", "All"),
    ("—", "All"),
])
def test_requirement_table_scope_requires_work_mapping_in_own_cell(tmp_path, work, gate) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | {work} | {gate} | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-FR-03" in result.detail
    assert "work-item reference" in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize(("work", "gate"), [
    ("W2.1", "All"),
    ("RELO-W0.1a", "Migration gates"),
    ("W2.1–W2.2", "QG-04"),
    ("W2.1, W6.5", "Non-QG gate prose"),
    ("See W2.1", "All"),
])
def test_requirement_table_scope_accepts_work_mapping_forms(tmp_path, work, gate) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | {work} | {gate} | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


@pytest.mark.parametrize("wrapper", ["<!--\n{}\n-->", "```text\n{}\n```"])
def test_requirement_table_scope_ignores_unmapped_hidden_and_unrelated_rows(tmp_path, wrapper) -> None:
    hidden = wrapper.format("| RELO-FR-03 | TBD | QG-04 | evidence | RELO-2 |")
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |\n" + hidden + "\n"
        "| Other | Work | Gate | Evidence | Phase |\n|---|---|---|---|---|\n"
        "| RELO-FR-03 | TBD | QG-04 | evidence | RELO-2 |",
    )
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


def test_requirement_table_scope_accepts_minimal_specification_without_table(tmp_path) -> None:
    spec = tmp_path / "minimal.md"
    spec.write_text("**Version:** 1.0\n\n- **v1.0 — fixture:** note\n", encoding="utf-8")
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


@pytest.mark.parametrize(("work", "gate"), [
    ("TBD", "QG-04"),
    ("Pending", "W2.1, QG-04"),
])
def test_requirement_table_scope_standalone_json_cli_rejects_missing_work_mapping(
    tmp_path, work, gate,
) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | {work} | {gate} | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
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

    process = subprocess.run(
        [
            sys.executable, "-B", str(Path(__file__).resolve().parents[2] / "scripts" / "verify_relo.py"),
            "--repo-root", str(tmp_path), "--spec", str(spec), "--json",
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert process.returncode == 6
    assert process.stderr == ""
    results = json.loads(process.stdout)
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-SPEC-LINT"]
    assert "work-item reference" in next(
        row["detail"] for row in results if row["id"] == "RELO-SPEC-LINT"
    )
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("requirement", ["RELO-FR-03", "RELO-NFR-09"])
@pytest.mark.parametrize("phase", [
    "TBD",
    "unassigned",
    "RELO-9",
    "RELO-0/9",
    "RELO-7",
    "RELO-0/3/7",
    "RELO-06",
    "RELO-2/",
    "RELO-3–1",
    "RELO-4-1",
    "RELO-0–9",
    "RELO-2x",
    "phase RELO-2",
    "RELO-0–3–4",
])
def test_requirement_table_scope_rejects_invalid_phase_mapping(tmp_path, requirement, phase) -> None:
    fr_phase = phase if requirement == "RELO-FR-03" else "RELO-2"
    nfr_phase = phase if requirement == "RELO-NFR-09" else "RELO-6"
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | W2.1 | QG-04 | evidence for RELO-2 | {fr_phase} |\n"
        f"| RELO-NFR-09 | W6.5 | All | evidence for RELO-6 | {nfr_phase} |",
    )
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert f"{requirement}: requirement mapping requires valid phase references" in result.detail
    assert phase not in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize("phase", [
    "RELO-0",
    "RELO-1",
    "RELO-2",
    "RELO-3",
    "RELO-4",
    "RELO-5",
    "RELO-6",
    "RELO-0/3",
    "RELO-0/1/5",
    "RELO-2/4/6",
    "RELO-0–3",
    "RELO-0-3",
    "RELO-0–6",
    "RELO-6–6",
])
def test_requirement_table_scope_accepts_phase_mapping_forms(tmp_path, phase) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | W2.1 | QG-04 | evidence | {phase} |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


@pytest.mark.parametrize("wrapper", ["<!--\n{}\n-->", "```text\n{}\n```"])
def test_requirement_table_scope_ignores_hidden_and_unrelated_invalid_phases(tmp_path, wrapper) -> None:
    hidden = wrapper.format("| RELO-FR-03 | W2.1 | QG-04 | evidence | TBD |")
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |\n" + hidden + "\n"
        "| Other | Work | Gate | Evidence | Phase |\n|---|---|---|---|---|\n"
        "| RELO-FR-03 | W2.1 | QG-04 | evidence | TBD |",
    )
    before = spec.read_bytes()

    assert lint_specification(spec).passed is True
    assert spec.read_bytes() == before


@pytest.mark.parametrize(("phase", "valid"), [
    ("TBD", False),
    ("RELO-9", False),
    ("RELO-6–0", False),
    ("RELO-0/3", True),
    ("RELO-0–3", True),
])
def test_requirement_table_scope_standalone_json_cli_validates_phase_mapping(
    tmp_path, phase, valid,
) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | W2.1 | QG-04 | evidence | {phase} |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
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

    process = subprocess.run(
        [
            sys.executable, "-B", str(Path(__file__).resolve().parents[2] / "scripts" / "verify_relo.py"),
            "--repo-root", str(tmp_path), "--spec", str(spec), "--json",
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert process.returncode == (0 if valid else 6)
    assert process.stderr == ""
    results = json.loads(process.stdout)
    lint = next(row for row in results if row["id"] == "RELO-SPEC-LINT")
    assert lint["passed"] is valid
    assert [row["id"] for row in results if not row["passed"]] == ([] if valid else ["RELO-SPEC-LINT"])
    if not valid:
        assert "requirement mapping requires valid phase references" in lint["detail"]
        assert phase not in lint["detail"]
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


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


@pytest.mark.parametrize(("work", "gate"), [
    ("W2.1.99", "QG-04"),
    ("RELO-W2.1.invalid", "QG-04"),
    ("W2.1..99", "QG-04"),
    ("W2.1–W2.2.99", "QG-04"),
    ("W0.1aa", "QG-04"),
    ("W2.1", "QG-04.99"),
    ("W2.1", "RELO-QG-04.invalid"),
    ("W2.1", "QG-04–QG-05..99"),
    ("W2.1", "QG-04invalid"),
])
def test_spec_lint_rejects_malformed_explicit_reference_suffixes(tmp_path, work, gate) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | {work} | {gate} | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert "unsupported traceability reference" in result.detail
    assert spec.read_bytes() == before


@pytest.mark.parametrize(("work", "gate"), [
    ("W2.1.", "QG-04."),
    ("RELO-W0.1a...", "Non-QG gate prose."),
    ("W2.1–W2.2.", "QG-04–QG-05."),
])
def test_spec_lint_accepts_periods_after_explicit_references(tmp_path, work, gate) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        f"| RELO-FR-03 | {work} | {gate} | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
    )

    assert lint_specification(spec).passed is True


def test_standalone_json_cli_rejects_malformed_explicit_reference(tmp_path) -> None:
    spec = _write_traceability_spec(
        tmp_path,
        "| RELO-FR-03 | W2.1.99 | QG-04 | evidence | RELO-2 |\n"
        "| RELO-NFR-09 | W6.5 | All | evidence | RELO-6 |",
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

    process = subprocess.run(
        [
            sys.executable, "-B", str(Path(__file__).resolve().parents[2] / "scripts" / "verify_relo.py"),
            "--repo-root", str(tmp_path), "--spec", str(spec), "--json",
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert process.returncode == 6
    assert process.stderr == ""
    results = json.loads(process.stdout)
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-SPEC-LINT"]
    assert "unsupported traceability reference" in next(
        row["detail"] for row in results if row["id"] == "RELO-SPEC-LINT"
    )
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


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


_GOAL_ROWS = (
    "| RELO-G1 | RELO-O1, RELO-O2 | RELO-FR-01–RELO-FR-02, RELO-NFR-01; RELO-F1/F2; W0.1–W0.2 |\n"
    "| RELO-G2 | RELO-O2–RELO-O3 | RELO-FR-02; W0.2 |"
)


def _write_goal_traceability_spec(tmp_path, rows=_GOAL_ROWS, *, objective_rows=None, extra=""):
    if objective_rows is None:
        objective_rows = (
            "| **RELO-O1** | one | evidence |\n"
            "| **RELO-O2** | two | evidence |\n"
            "| **RELO-O3** | three | evidence |\n"
        )
    spec = tmp_path / "relo.md"
    spec.write_text(
        "**Version:** 1.0\n\n- **v1.0 — synthetic:** note\n\n"
        "## Table of Contents\n"
        "## 3. Goals\n"
        "### 3.2 Goals\n"
        "| ID | Goal |\n|---|---|\n"
        "| **RELO-G1** | first |\n| **RELO-G2** | second |\n"
        "### 3.3 Objectives\n"
        "| ID | Objective | Required evidence |\n|---|---|---|\n"
        + objective_rows
        + "## 5. Requirements\n"
        "### 5.5 Functional requirements\n"
        "| ID | Requirement |\n|---|---|\n"
        "| **RELO-FR-01** | first |\n| **RELO-FR-02** | second |\n"
        "### 5.6 Non-functional requirements\n"
        "| ID | Requirement |\n|---|---|\n"
        "| **RELO-NFR-01 — Safety** | safety |\n"
        "## 8. Features\n"
        "### 8.1 RELO-F1 — First\n"
        "### 8.2 RELO-F2 — Second\n"
        "## 11. Plan\n"
        "### 11.1 Phase RELO-0\n"
        "| ID | Work item | Acceptance |\n|---|---|---|\n"
        "| RELO-W0.1 | first | evidence |\n| RELO-W0.2 | second | evidence |\n"
        "### 11.2 Phase RELO-1\n"
        "## 19. Traceability\n"
        "### 19.1 Goal and objective traceability\n"
        "| Goal | Objectives | Primary requirements/workstreams |\n"
        "|---|---|---|\n"
        + rows
        + "\n"
        + extra,
        encoding="utf-8",
    )
    return spec


@pytest.mark.parametrize(("rows", "finding"), [
    (
        "| RELO-G1 | RELO-O1–RELO-O3 | RELO-FR-01 |",
        "missing goal traceability row",
    ),
    (
        _GOAL_ROWS + "\n| RELO-G9 | RELO-O1 | RELO-FR-01 |",
        "RELO-G9",
    ),
    (
        _GOAL_ROWS.replace("RELO-O2–RELO-O3", "RELO-O2"),
        "unreferenced objective",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1, RELO-O9"),
        "RELO-O9",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-99; W0.2"),
        "RELO-FR-99",
    ),
    (
        _GOAL_ROWS.replace("RELO-NFR-01", "RELO-NFR-99"),
        "RELO-NFR-99",
    ),
    (
        _GOAL_ROWS.replace("RELO-F1/F2", "RELO-F1/F9"),
        "RELO-F9",
    ),
    (
        _GOAL_ROWS.replace("W0.2 |", "W0.99 |"),
        "RELO-W0.99",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", ""),
        "goal traceability row",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-F2; W0.2"),
        "requirement",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O3–RELO-O1"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1–RELO-FR-01"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "O1–Z9, RELO-O2"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1–RELO-O2–O3"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1—RELO-O3"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02–QG-01; W0.2"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02–; W0.2"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1, RELO-FR-01"),
        "objective",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02, RELO-O1; W0.2"),
        "requirement mapping contains an objective reference",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "O1invalid, RELO-O2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "FR-02invalid; W0.2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02, RELO-Z9; W0.2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1.99, RELO-O2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1..99, RELO-O2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-O2–RELO-O3", "RELO-O2–RELO-O3.invalid"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02.99; W0.2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-NFR-01", "RELO-NFR-01.invalid"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-F1/F2", "RELO-F1/F2.99"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("W0.1–W0.2", "W0.1.99–W0.2"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("W0.1–W0.2", "W0.1–W0.2.99"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("W0.1–W0.2", "W0.1–W0.2..99"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02; W0.2.invalid"),
        "unsupported",
    ),
    (
        _GOAL_ROWS.replace("W0.1–W0.2", "W0.1–W0.2a"),
        "invalid traceability range",
    ),
    (
        _GOAL_ROWS.replace("W0.1–W0.2", "W0.1–W0.3"),
        "RELO-W0.3",
    ),
    (
        _GOAL_ROWS + "\n| not-a-goal | RELO-O1 | RELO-FR-01 |",
        "goal traceability row",
    ),
    (
        _GOAL_ROWS.replace("RELO-FR-02; W0.2 |", "RELO-FR-02; W0.2"),
        "goal traceability row",
    ),
])
def test_goal_traceability_rejects_incomplete_or_invalid_mapping(tmp_path, rows, finding) -> None:
    spec = _write_goal_traceability_spec(tmp_path, rows)
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is False
    assert finding in result.detail
    assert spec.read_bytes() == before


def test_goal_traceability_rejects_undefined_interior_objective(tmp_path) -> None:
    spec = _write_goal_traceability_spec(
        tmp_path,
        _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1–RELO-O3"),
        objective_rows="| **RELO-O1** | one | evidence |\n| **RELO-O3** | three | evidence |\n",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-O2" in result.detail


def test_goal_traceability_rejects_undefined_interior_requirement(tmp_path) -> None:
    spec = _write_goal_traceability_spec(
        tmp_path, _GOAL_ROWS.replace("RELO-FR-01–RELO-FR-02", "RELO-FR-01–RELO-FR-03"),
    )
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "| **RELO-FR-02** | second |", "| **RELO-FR-03** | third |",
        ),
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-FR-02" in result.detail


def test_goal_traceability_does_not_count_objectives_on_unknown_goals(tmp_path) -> None:
    spec = _write_goal_traceability_spec(
        tmp_path,
        _GOAL_ROWS.replace("RELO-O2–RELO-O3", "RELO-O2")
        + "\n| RELO-G9 | RELO-O3 | RELO-FR-01 |",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-G9" in result.detail
    assert "unreferenced objective(s): RELO-O3" in result.detail


def test_goal_traceability_rejects_missing_table_header(tmp_path) -> None:
    spec = _write_goal_traceability_spec(tmp_path)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "| Goal | Objectives | Primary requirements/workstreams |",
            "| Reference | Objectives | Primary requirements/workstreams |",
        ),
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "goal traceability table" in result.detail


def test_goal_traceability_rejects_extra_header_column(tmp_path) -> None:
    spec = _write_goal_traceability_spec(tmp_path)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "| Goal | Objectives | Primary requirements/workstreams |\n|---|---|---|\n",
            "| Goal | Objectives | Primary requirements/workstreams | Extra |\n|---|---|---|---|\n",
        ),
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "goal traceability table" in result.detail


@pytest.mark.parametrize("separator", [
    "",
    "|---|---|---",
    "|---|---|",
    "|---|invalid|---|",
])
def test_goal_traceability_rejects_invalid_table_separator(tmp_path, separator) -> None:
    spec = _write_goal_traceability_spec(tmp_path)
    content = spec.read_text(encoding="utf-8")
    header = "| Goal | Objectives | Primary requirements/workstreams |\n"
    if separator:
        content = content.replace(header + "|---|---|---|\n", header + separator + "\n")
    else:
        content = content.split(header)[0] + header
    spec.write_text(content, encoding="utf-8")

    result = lint_specification(spec)

    assert result.passed is False
    assert "goal traceability table" in result.detail


@pytest.mark.parametrize(("row", "replacement", "finding"), [
    (
        "| **RELO-G2** | second |",
        "| **RELO-G2** | second |\n| **RELO-G1** | duplicate |",
        "duplicate goal definition",
    ),
    (
        "| **RELO-O3** | three | evidence |",
        "| **RELO-O3** | three | evidence |\n| **RELO-O1** | duplicate | evidence |",
        "duplicate objective definition",
    ),
    (
        "| **RELO-O3** | three | evidence |",
        "| invalid | three | evidence |",
        "objective definition table is missing or malformed",
    ),
])
def test_goal_traceability_rejects_invalid_definition_rows(tmp_path, row, replacement, finding) -> None:
    spec = _write_goal_traceability_spec(tmp_path)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(row, replacement),
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert finding in result.detail


@pytest.mark.parametrize("hidden", [
    "<!--\n| RELO-G2 | RELO-O3 | RELO-FR-02 |\n-->\n",
    "```markdown\n| RELO-G2 | RELO-O3 | RELO-FR-02 |\n```\n",
    "### 19.3 Other traceability\n| RELO-G2 | RELO-O3 | RELO-FR-02 |\n",
])
def test_goal_traceability_ignores_rows_outside_visible_table(tmp_path, hidden) -> None:
    spec = _write_goal_traceability_spec(
        tmp_path,
        "| RELO-G1 | RELO-O1–RELO-O3 | RELO-FR-01 |",
        extra=hidden,
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "missing goal traceability row" in result.detail


def test_goal_traceability_ignores_objectives_in_an_unrelated_table(tmp_path) -> None:
    spec = _write_goal_traceability_spec(
        tmp_path,
        _GOAL_ROWS.replace("RELO-O2–RELO-O3", "RELO-O2–RELO-O4"),
        objective_rows=(
            "| **RELO-O1** | one | evidence |\n"
            "| **RELO-O2** | two | evidence |\n"
            "| **RELO-O3** | three | evidence |\n\n"
            "| Other | Description |\n|---|---|\n"
            "| **RELO-O4** | not an objective definition |\n"
        ),
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-O4" in result.detail


@pytest.mark.parametrize(("heading", "replacement", "finding"), [
    ("## 3. Goals", "## 4. Unrelated", "goal definition table"),
    ("## 5. Requirements", "## 6. Unrelated", "RELO-FR-01"),
    ("## 11. Plan", "## 12. Unrelated", "RELO-W0.1"),
    ("## 19. Traceability", "## 18. Unrelated", "goal traceability table"),
])
def test_goal_traceability_rejects_sections_under_an_unrelated_parent(
    tmp_path, heading, replacement, finding,
) -> None:
    spec = _write_goal_traceability_spec(tmp_path)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(heading, replacement),
        encoding="utf-8",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert finding in result.detail


def test_goal_traceability_ignores_features_outside_feature_section(tmp_path) -> None:
    spec = _write_goal_traceability_spec(
        tmp_path,
        _GOAL_ROWS.replace("RELO-F1/F2", "RELO-F1/F9"),
        extra="\n## 20. Unrelated\n### 8.9 RELO-F9 — unrelated heading\n",
    )

    result = lint_specification(spec)

    assert result.passed is False
    assert "RELO-F9" in result.detail


def test_goal_traceability_accepts_defined_ranges_and_slash_shorthand(tmp_path) -> None:
    spec = _write_goal_traceability_spec(tmp_path)
    before = spec.read_bytes()

    result = lint_specification(spec)

    assert result.passed is True
    assert spec.read_bytes() == before


@pytest.mark.parametrize("rows", [
    _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1, RELO-O2."),
    _GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1, RELO-O2..."),
    _GOAL_ROWS.replace("RELO-F1/F2", "RELO-F1/F2."),
    _GOAL_ROWS.replace("W0.1–W0.2", "W0.1–W0.2."),
    _GOAL_ROWS.replace("RELO-FR-02; W0.2", "RELO-FR-02.; W0.2."),
])
def test_goal_traceability_accepts_terminal_periods(tmp_path, rows) -> None:
    spec = _write_goal_traceability_spec(tmp_path, rows)

    assert lint_specification(spec).passed is True


@pytest.mark.parametrize("rows", [
    "",
    _GOAL_ROWS.replace("RELO-O2–RELO-O3", "RELO-O2–RELO-O9"),
])
def test_verifier_cli_rejects_goal_traceability_without_mutation(tmp_path, capsys, rows) -> None:
    spec = _write_goal_traceability_spec(tmp_path, rows)
    (tmp_path / "pyproject.toml").write_text(
        'addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8",
    )
    _write_budgets(
        tmp_path, line_status="ratified", line_value=100,
        obligation_status="ratified", obligation_value=10,
    )
    (tmp_path / "governance" / "relo-work-packages.yaml").write_text(
        "schema_version: '1'\nwork_items:\n"
        "  - {id: RELO-W0.1, class: NEEDS_JUDGMENT, depends_on: []}\n"
        "  - {id: RELO-W0.2, class: NEEDS_JUDGMENT, depends_on: []}\n",
        encoding="utf-8",
    )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    exit_code = main(["--repo-root", str(tmp_path), "--spec", str(spec), "--json"])

    results = json.loads(capsys.readouterr().out)
    assert exit_code == 6
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-SPEC-LINT"]
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(("rows", "exit_code", "diagnostic"), [
    (_GOAL_ROWS, 0, None),
    (_GOAL_ROWS.replace("RELO-O2–RELO-O3", "RELO-O2–RELO-O9"), 6, "RELO-O4"),
    (_GOAL_ROWS.replace("RELO-O1, RELO-O2", "RELO-O1.99, RELO-O2"), 6, "unsupported objective"),
])
@pytest.mark.parametrize("line_ending", [
    pytest.param("\n", id="lf"),
    pytest.param("\r\n", id="crlf"),
])
def test_goal_traceability_standalone_json_cli_preserves_inputs(
    tmp_path, rows, exit_code, diagnostic, line_ending,
) -> None:
    spec = _write_goal_traceability_spec(tmp_path, rows)
    content = spec.read_text(encoding="utf-8").replace(
        "## 3. Goals\n", "```markdown\nignored example\n```\n## 3. Goals\n",
    )
    spec.write_bytes(content.replace("\n", line_ending).encode("utf-8"))
    (tmp_path / "pyproject.toml").write_text(
        'addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8",
    )
    _write_budgets(
        tmp_path, line_status="ratified", line_value=100,
        obligation_status="ratified", obligation_value=10,
    )
    (tmp_path / "governance" / "relo-work-packages.yaml").write_text(
        "schema_version: '1'\nwork_items:\n"
        "  - {id: RELO-W0.1, class: NEEDS_JUDGMENT, depends_on: []}\n"
        "  - {id: RELO-W0.2, class: NEEDS_JUDGMENT, depends_on: []}\n",
        encoding="utf-8",
    )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    process = subprocess.run(
        [
            sys.executable, "-B", str(Path(__file__).resolve().parents[2] / "scripts" / "verify_relo.py"),
            "--repo-root", str(tmp_path), "--spec", str(spec), "--json",
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert process.returncode == exit_code
    assert process.stderr == ""
    results = json.loads(process.stdout)
    assert [row["id"] for row in results if not row["passed"]] == (
        [] if exit_code == 0 else ["RELO-SPEC-LINT"]
    )
    if diagnostic is not None:
        assert diagnostic in next(row["detail"] for row in results if row["id"] == "RELO-SPEC-LINT")
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


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


def _ready_work_package(identifier="RELO-W0.1"):
    return {
        "id": identifier,
        "class": "AGENT_READY",
        "depends_on": [],
        "read_first": ["existing.py"],
        "change": ["existing.py"],
        "acceptance": ["test"],
        "forbidden": ["side effects"],
        "rollback": "undo",
    }


def _write_work_package_fixture(tmp_path, cards, identifiers=("RELO-W0.1",)):
    (tmp_path / "governance").mkdir(exist_ok=True)
    (tmp_path / "existing.py").write_text("# synthetic\n", encoding="utf-8")
    spec = tmp_path / "relo.md"
    rows = "".join(f"| {identifier} | item |\n" for identifier in identifiers)
    spec.write_text(
        "**Version:** 1.0\n\n- **v1.0 — synthetic:** note\n\n"
        "## Table of Contents\n"
        "### 11.1 Phase RELO-0\n" + rows
        + "### 11.2 Phase RELO-1\n### 11.3 Next\n**RELO-D1**\n",
        encoding="utf-8",
    )
    (tmp_path / "governance" / "relo-work-packages.yaml").write_text(
        json.dumps({"schema_version": "1", "work_items": cards}),
        encoding="utf-8",
    )
    return spec


@pytest.mark.parametrize(("field", "value", "diagnostic"), [
    ("id", None, "invalid id"),
    ("id", ["RELO-W0.1"], "invalid id"),
    ("class", [], "invalid class"),
    ("class", {"nested": "value"}, "invalid class"),
    ("depends_on", [None], "unresolved depends_on"),
    ("depends_on", ["RELO-W0.1", {}], "unresolved depends_on"),
    ("depends_on", [[]], "unresolved depends_on"),
    ("read_first", None, "read_first must contain one to three paths"),
    ("read_first", 1, "read_first must contain one to three paths"),
    ("read_first", {"existing.py": True}, "read_first must contain one to three paths"),
    ("change", None, "change must be a list of paths"),
    ("change", 1, "change must be a list of paths"),
    ("change", "existing.py", "change must be a list of paths"),
    ("change", {"existing.py": "not a list"}, "change must be a list of paths"),
])
def test_work_package_lint_rejects_malformed_field_shapes_without_mutation(
    tmp_path, field, value, diagnostic,
) -> None:
    card = _ready_work_package()
    card[field] = value
    spec = _write_work_package_fixture(tmp_path, [card])
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = lint_work_packages(tmp_path, spec_path=spec)

    assert result.id == "RELO-WORK-PACKAGES"
    assert result.passed is False
    assert diagnostic in result.detail
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("identifier", [None, []])
def test_work_package_lint_rejects_invalid_id_without_declared_cards(tmp_path, identifier) -> None:
    card = _ready_work_package(identifier)
    spec = _write_work_package_fixture(tmp_path, [card], identifiers=())

    result = lint_work_packages(tmp_path, spec_path=spec)

    assert result.passed is False
    assert "invalid id" in result.detail


def test_work_package_lint_preserves_valid_classes_and_aggregates_invalid_cards(tmp_path) -> None:
    ready = _ready_work_package()
    judgment = {"id": "RELO-W0.2", "class": "NEEDS_JUDGMENT", "depends_on": ["RELO-W0.1"]}
    decision = {
        "id": "RELO-W0.3",
        "class": "NEEDS_DECISION",
        "depends_on": ["RELO-W0.2"],
        "blocking_decisions": ["RELO-D1"],
    }
    spec = _write_work_package_fixture(
        tmp_path, [ready, judgment, decision], identifiers=("RELO-W0.1", "RELO-W0.2", "RELO-W0.3"),
    )
    spec.write_text(
        spec.read_text(encoding="utf-8")
        + "\n## 20. Open Decisions\n| ID | Decision |\n|---|---|\n| **RELO-D1** | fixture |\n",
        encoding="utf-8",
    )
    assert lint_work_packages(tmp_path, spec_path=spec).passed is True

    ready["class"] = []
    judgment["depends_on"] = [{}]
    decision["blocking_decisions"] = []
    (tmp_path / "governance" / "relo-work-packages.yaml").write_text(
        json.dumps({"schema_version": "1", "work_items": [ready, judgment, decision]}),
        encoding="utf-8",
    )
    result = lint_work_packages(tmp_path, spec_path=spec)

    assert result.passed is False
    assert "RELO-W0.1: invalid class" in result.detail
    assert "RELO-W0.2: unresolved depends_on" in result.detail
    assert "RELO-W0.3: NEEDS_DECISION must name" in result.detail


@pytest.mark.parametrize(("field", "value"), [
    ("depends_on", [{}]),
    ("change", {"existing.py": "not a list"}),
])
def test_verifier_cli_reports_malformed_work_packages_without_mutation(tmp_path, capsys, field, value) -> None:
    card = _ready_work_package()
    card[field] = value
    spec = _write_work_package_fixture(tmp_path, [card])
    (tmp_path / "pyproject.toml").write_text(
        'addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8",
    )
    _write_budgets(
        tmp_path, line_status="ratified", line_value=100,
        obligation_status="ratified", obligation_value=10,
    )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    exit_code = main(["--repo-root", str(tmp_path), "--spec", str(spec), "--json"])

    results = json.loads(capsys.readouterr().out)
    assert exit_code == 6
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-WORK-PACKAGES"]
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def _write_normal_verifier_fixture(tmp_path):
    spec = _write_work_package_fixture(tmp_path, [_ready_work_package()])
    (tmp_path / "pyproject.toml").write_text(
        'addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8",
    )
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text('{"feature":"fixture"}\n', encoding="utf-8")
    _write_budgets(
        tmp_path, line_status="ratified", line_value=100,
        obligation_status="ratified", obligation_value=10,
    )
    return spec, telemetry


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(("case", "blockers", "valid"), [
    ("valid_bold", ["RELO-D1"], True),
    ("valid_suffix", ["RELO-D6a"], True),
    ("valid_plain", ["RELO-D8"], True),
    ("valid_multiple", ["RELO-D1", "RELO-D6a", "RELO-D1"], True),
    ("valid_aligned_table", ["RELO-D1"], True),
    ("valid_final_row_without_newline", ["RELO-D1"], True),
    ("valid_comment_at_eof", ["RELO-D1"], True),
    ("blank", [""], False),
    ("whitespace", [" "], False),
    ("partial_id", ["RELO-D6"], False),
    ("outside_section", ["RELO-D4"], False),
    ("section_prose", ["RELO-D9"], False),
    ("second_column", ["RELO-D10"], False),
    ("after_section", ["RELO-D11"], False),
    ("later_table", ["RELO-D12"], False),
    ("unbalanced_markup", ["RELO-D13"], False),
    ("trailing_cell_text", ["RELO-D14"], False),
    ("mixed", ["RELO-D1", ""], False),
    ("mixed_unknown", ["RELO-D1", "SYNTHETIC-SECRET-MARKER"], False),
    ("padded", ["RELO-D1 "], False),
    ("missing_section", ["RELO-D1"], False),
    ("missing_table", ["RELO-D1"], False),
    ("missing_header", ["RELO-D1"], False),
    ("missing_separator", ["RELO-D1"], False),
    ("subsection_heading", ["RELO-D1"], False),
    ("fenced_section", ["RELO-D1"], False),
    ("commented_section", ["RELO-D1"], False),
    ("fenced_table", ["RELO-D1"], False),
    ("tilde_fenced_table", ["RELO-D1"], False),
    ("long_fenced_table", ["RELO-D1"], False),
    ("unclosed_fence", ["RELO-D1"], False),
    ("commented_table", ["RELO-D1"], False),
    ("unclosed_comment", ["RELO-D1"], False),
    ("valid_after_fenced_table", ["RELO-D1"], True),
    ("valid_after_commented_table", ["RELO-D1"], True),
    ("valid_after_comment_with_fence", ["RELO-D1"], True),
    ("valid_after_fence_with_comment", ["RELO-D1"], True),
    ("fenced_later_heading", ["RELO-D1"], True),
    ("malformed", [{"value": "SYNTHETIC-SECRET-MARKER"}], False),
    ("unknown", ["SYNTHETIC-SECRET-MARKER"], False),
    ("null_entry", [None], False),
    ("numeric_entry", [42], False),
    ("non_list", "RELO-D1", False),
    ("missing_value", None, False),
    ("empty_list", [], False),
])
def test_work_package_decisions_require_declared_section_20_ids(
    tmp_path, capsys, json_output, case, blockers, valid,
) -> None:
    root = tmp_path / "PRIVATE-PATH-MARKER"
    root.mkdir()
    spec, telemetry = _write_normal_verifier_fixture(root)
    content = spec.read_text(encoding="utf-8") + "\nRELO-D4 and RELO-D1 appear outside the decision section.\n"
    table = "| ID | Decision |\n|---|---|\n| **RELO-D1** | fixture |\n"
    if case == "fenced_section":
        content += "\n```markdown\n## 20. Open Decisions\n" + table + "```\n"
    elif case == "commented_section":
        content += "\n<!--\n## 20. Open Decisions\n" + table + "-->\n"
    elif case != "missing_section":
        content += "\n## 20.1 Other\n" if case == "subsection_heading" else "\n## 20. Open Decisions\n"
        if case == "missing_table":
            content += "RELO-D1 is mentioned without a table.\n"
        elif case == "missing_header":
            content += "| **RELO-D1** | fixture |\n"
        elif case == "missing_separator":
            content += "| ID | Decision |\n| **RELO-D1** | fixture |\n"
        elif case == "valid_aligned_table":
            content += "| ID | Decision | More |\n|:---|---:|:---:|\n| **RELO-D1** | fixture | more |\n"
        elif case == "valid_final_row_without_newline":
            content += table.rstrip("\n")
        elif case == "valid_comment_at_eof":
            content += table + "<!-- ignored -->"
        elif case == "fenced_table":
            content += "```markdown\n" + table + "```\n"
        elif case == "tilde_fenced_table":
            content += "~~~markdown\n" + table + "~~~\n"
        elif case == "long_fenced_table":
            content += "````markdown\n```\n" + table + "```\n````\n"
        elif case == "unclosed_fence":
            content += "```markdown\n" + table
        elif case == "commented_table":
            content += "<!--\n" + table + "-->\n"
        elif case == "unclosed_comment":
            content += "<!--\n" + table
        elif case in {"valid_after_fenced_table", "valid_after_commented_table"}:
            ignored_table = table.replace("RELO-D1", "RELO-D15")
            if case == "valid_after_fenced_table":
                content += "```markdown\n" + ignored_table + "```\n"
            else:
                content += "<!--\n" + ignored_table + "-->\n"
            content += table
        elif case == "valid_after_comment_with_fence":
            content += "<!--\n```markdown\n-->\n" + table
        elif case == "valid_after_fence_with_comment":
            content += "```markdown\n<!--\n```\n" + table
        elif case == "fenced_later_heading":
            content += "```markdown\n## 21. Next\n```\n" + table
        else:
            content += (
                "| ID | Decision |\n|---|---|\n"
                "| **RELO-D1** | fixture |\n"
                "| **RELO-D6a** | fixture |\n"
                "| RELO-D8 | fixture |\n"
                "| other column | **RELO-D10** |\n"
                "| **RELO-D13 | fixture |\n"
                "| **RELO-D14** extra | fixture |\n"
                "RELO-D9 is only mentioned in prose.\n"
                "| ID | Decision |\n|---|---|\n"
                "| **RELO-D12** | another table |\n"
                "## 21. Next\n| **RELO-D11** | outside section |\n"
            )
    spec.write_text(content, encoding="utf-8")
    card = {"id": "RELO-W0.1", "class": "NEEDS_DECISION", "depends_on": [], "blocking_decisions": blockers}
    if case == "missing_value":
        del card["blocking_decisions"]
    ledger = root / "governance" / "relo-work-packages.yaml"
    ledger.write_text(json.dumps({"schema_version": "1", "work_items": [card]}), encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    args = ["--repo-root", str(root), "--spec", str(spec), "--telemetry", str(telemetry)]
    if json_output:
        args.append("--json")

    exit_code = main(args)

    output = capsys.readouterr()
    failed = [] if valid else ["RELO-WORK-PACKAGES"]
    expected_checks = [
        "RELO-W0.7a", "RELO-W0.10a", "RELO-SPEC-LINT",
        "RELO-WORK-PACKAGES", "RELO-SPEC-SIZE-BUDGET",
    ]
    assert exit_code == (0 if valid else 6)
    if json_output:
        results = json.loads(output.out)
        assert [row["id"] for row in results] == expected_checks
        assert [row["id"] for row in results if not row["passed"]] == failed
        detail = next(row["detail"] for row in results if row["id"] == "RELO-WORK-PACKAGES")
    else:
        lines = output.out.splitlines()
        assert [line.split()[1].rstrip(":") for line in lines] == expected_checks
        assert [line.split()[1].rstrip(":") for line in lines if line.startswith("FAIL ")] == failed
        detail = output.out
    if not valid:
        assert "RELO-W0.1: NEEDS_DECISION must name existing §20 decision(s)" in detail
    assert "SYNTHETIC-SECRET-MARKER" not in output.out + output.err
    assert "PRIVATE-PATH-MARKER" not in output.out + output.err
    assert output.err == ""
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(("case", "diagnostics"), [
    ("valid", ()),
    ("invalid_class", ("RELO-W0.1: invalid class (not an allowed class)",)),
    ("non_string_class", ("RELO-W0.1: invalid class (must be a string)",)),
    ("unknown_id", (
        "unknown work-package card(s) at entries: 1",
        "<unknown card at entry 1>: invalid class (not an allowed class)",
    )),
    ("unknown_looks_like_id", ("unknown work-package card(s) at entries: 1",)),
    ("unknown_after_nonmapping", (
        "unknown work-package card(s) at entries: 2",
        "work-package card is not a mapping",
        "<unknown card at entry 2>: invalid class (not an allowed class)",
    )),
    ("duplicate_unknown", (
        "duplicate card ID(s) at entries: 2, 3",
        "unknown work-package card(s) at entries: 2, 3",
    )),
    ("duplicate_known", ("duplicate card ID(s) at entries: 1, 2",)),
    ("mixed_duplicates", (
        "duplicate card ID(s) at entries: 2, 3, 4, 5",
        "unknown work-package card(s) at entries: 4, 5",
        "work-package card is not a mapping",
    )),
    ("invalid_id", (
        "work-package card has invalid id",
        "<invalid id>: invalid class (not an allowed class)",
    )),
])
def test_work_package_identifier_and_class_diagnostics_do_not_disclose_values(
    tmp_path, capsys, json_output, case, diagnostics,
) -> None:
    root = tmp_path / "PRIVATE-PATH-MARKER"
    root.mkdir()
    spec, telemetry = _write_normal_verifier_fixture(root)
    marker = "SYNTHETIC-SECRET-MARKER"
    ready = _ready_work_package()
    unknown = {"id": marker, "class": "NEEDS_JUDGMENT", "depends_on": []}
    if case == "invalid_class":
        ready["class"] = marker
        cards = [ready]
    elif case == "non_string_class":
        ready["class"] = {"value": marker}
        cards = [ready]
    elif case == "unknown_id":
        cards = [{**unknown, "class": marker}]
    elif case == "unknown_looks_like_id":
        cards = [{**unknown, "id": "RELO-W91.12"}]
    elif case == "unknown_after_nonmapping":
        cards = [None, {**unknown, "class": marker}]
    elif case == "duplicate_unknown":
        cards = [ready, unknown, unknown.copy()]
    elif case == "duplicate_known":
        cards = [ready, ready.copy()]
    elif case == "mixed_duplicates":
        cards = [None, ready, ready.copy(), unknown, unknown.copy()]
    elif case == "invalid_id":
        cards = [{**unknown, "id": {"value": marker}, "class": marker}]
    else:
        cards = [ready]
    ledger = root / "governance" / "relo-work-packages.yaml"
    ledger.write_text(json.dumps({"schema_version": "1", "work_items": cards}), encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    args = ["--repo-root", str(root), "--spec", str(spec), "--telemetry", str(telemetry)]
    if json_output:
        args.append("--json")

    exit_code = main(args)

    output = capsys.readouterr()
    failed = [] if case == "valid" else ["RELO-WORK-PACKAGES"]
    assert exit_code == (0 if not failed else 6)
    if json_output:
        results = json.loads(output.out)
        assert [row["id"] for row in results if not row["passed"]] == failed
        detail = next(row["detail"] for row in results if row["id"] == "RELO-WORK-PACKAGES")
    else:
        assert [line.split()[1].rstrip(":") for line in output.out.splitlines() if line.startswith("FAIL ")] == failed
        detail = output.out
    for diagnostic in diagnostics:
        assert diagnostic in detail
    assert marker not in output.out + output.err
    assert "PRIVATE-PATH-MARKER" not in output.out + output.err
    if case == "unknown_looks_like_id":
        assert "RELO-W91.12" not in output.out + output.err
    assert output.err == ""
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(("target", "failed"), [
    (None, []),
    ("relo.md", ["RELO-SPEC-LINT", "RELO-WORK-PACKAGES", "RELO-SPEC-SIZE-BUDGET"]),
    (Path("governance") / "relo-work-packages.yaml", ["RELO-WORK-PACKAGES"]),
    ("pyproject.toml", ["RELO-W0.7a"]),
    ("telemetry.jsonl", ["RELO-W0.10a"]),
])
def test_normal_verifier_reports_undecodable_evidence_without_mutation(
    tmp_path, capsys, target, failed,
) -> None:
    spec, telemetry = _write_normal_verifier_fixture(tmp_path)
    if target is not None:
        (tmp_path / target).write_bytes(b"\xffPRIVATE-SENTINEL")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    exit_code = main([
        "--repo-root", str(tmp_path), "--spec", str(spec), "--telemetry", str(telemetry), "--json",
    ])

    output = capsys.readouterr()
    results = json.loads(output.out)
    assert exit_code == (6 if failed else 0)
    assert [row["id"] for row in results if not row["passed"]] == failed
    assert all("invalid UTF-8" in row["detail"] for row in results if not row["passed"])
    assert "PRIVATE-SENTINEL" not in output.out + output.err
    assert "0xff" not in output.out + output.err
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_normal_verifier_reports_multiple_undecodable_inputs(tmp_path, capsys) -> None:
    spec, telemetry = _write_normal_verifier_fixture(tmp_path)
    for path in (spec, telemetry, tmp_path / "pyproject.toml", tmp_path / "governance" / "relo-work-packages.yaml"):
        path.write_bytes(b"\xff")

    exit_code = main([
        "--repo-root", str(tmp_path), "--spec", str(spec), "--telemetry", str(telemetry), "--json",
    ])

    results = json.loads(capsys.readouterr().out)
    assert exit_code == 6
    assert [row["id"] for row in results if not row["passed"]] == [
        "RELO-W0.7a", "RELO-W0.10a", "RELO-SPEC-LINT", "RELO-WORK-PACKAGES", "RELO-SPEC-SIZE-BUDGET",
    ]


def test_normal_verifier_does_not_read_unselected_undecodable_telemetry(tmp_path, capsys) -> None:
    spec, telemetry = _write_normal_verifier_fixture(tmp_path)
    telemetry.write_bytes(b"\xffPRIVATE-SENTINEL")

    exit_code = main(["--repo-root", str(tmp_path), "--spec", str(spec), "--json"])

    results = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert all(row["passed"] for row in results)
    assert all(row["id"] != "RELO-W0.10a" for row in results)
    assert telemetry.read_bytes() == b"\xffPRIVATE-SENTINEL"


@pytest.mark.parametrize("target", [
    "relo.md", "relo-work-packages.yaml", "pyproject.toml", "telemetry.jsonl",
])
def test_normal_verifier_propagates_unexpected_read_errors(tmp_path, monkeypatch, target) -> None:
    spec, telemetry = _write_normal_verifier_fixture(tmp_path)
    read_text = Path.read_text

    def broken_read(path, *args, **kwargs):
        if path.name == target:
            raise RuntimeError("unexpected read failure")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken_read)

    with pytest.raises(RuntimeError, match="unexpected read failure"):
        main(["--repo-root", str(tmp_path), "--spec", str(spec), "--telemetry", str(telemetry), "--json"])


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(("case", "failed", "diagnostic"), [
    ("valid", [], None),
    ("spec_read", ["RELO-SPEC-LINT", "RELO-WORK-PACKAGES", "RELO-SPEC-SIZE-BUDGET"], "file read failed"),
    ("ledger_read", ["RELO-WORK-PACKAGES"], "file read failed"),
    ("baseline_read", ["RELO-W0.7a"], "file read failed"),
    ("telemetry_read", ["RELO-W0.10a"], "file read failed"),
    ("missing_telemetry", ["RELO-W0.10a"], "file read failed"),
    ("budget_read", ["RELO-SPEC-SIZE-BUDGET"], "file read failed"),
    ("budget_unexpected", ["RELO-SPEC-SIZE-BUDGET"], "check failed"),
    ("ledger_yaml", ["RELO-WORK-PACKAGES"], "invalid YAML"),
    ("budget_yaml", ["RELO-SPEC-SIZE-BUDGET"], "invalid YAML"),
    ("budget_utf8", ["RELO-SPEC-SIZE-BUDGET"], "invalid UTF-8"),
    ("budget_value", ["RELO-SPEC-SIZE-BUDGET"], "check failed"),
    ("budget_import", ["RELO-SPEC-SIZE-BUDGET"], "loader unavailable"),
    ("explicit_budget_read", ["RELO-SPEC-SIZE-BUDGET"], "file read failed"),
    ("explicit_budget_yaml", ["RELO-SPEC-SIZE-BUDGET"], "invalid YAML"),
    ("explicit_budget_valid", [], None),
])
def test_normal_verifier_evidence_errors_do_not_disclose_inputs(
    tmp_path, monkeypatch, capsys, json_output, case, failed, diagnostic,
) -> None:
    root = tmp_path / "PRIVATE-PATH-MARKER"
    root.mkdir()
    spec, telemetry = _write_normal_verifier_fixture(root)
    ledger = root / "governance" / "relo-work-packages.yaml"
    budget = root / "governance" / "nfr-budgets.yaml"
    if case.startswith("explicit_budget_"):
        default_budget = budget
        budget = root / "selected" / "budget.yaml"
        budget.parent.mkdir()
        budget.write_bytes(default_budget.read_bytes())
        if case == "explicit_budget_valid":
            default_budget.write_text("budgets: [SYNTHETIC-SECRET-MARKER\n", encoding="utf-8")
    if case == "ledger_yaml":
        ledger.write_text("schema_version: '1'\nwork_items: [SYNTHETIC-SECRET-MARKER\n", encoding="utf-8")
    elif case in {"budget_yaml", "explicit_budget_yaml"}:
        budget.write_text("schema_version: '1'\nbudgets: [SYNTHETIC-SECRET-MARKER\n", encoding="utf-8")
    elif case == "budget_utf8":
        budget.write_bytes(b"\xffSYNTHETIC-SECRET-MARKER")
    elif case == "budget_value":
        budget.write_text(
            "schema_version: '1'\nbudgets:\n  - id: SYNTHETIC-SECRET-MARKER\n"
            "    budget_class: invalid\n",
            encoding="utf-8",
        )
    elif case == "missing_telemetry":
        telemetry.unlink()

    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    targets = {
        "spec_read": spec,
        "ledger_read": ledger,
        "baseline_read": root / "pyproject.toml",
        "telemetry_read": telemetry,
        "budget_read": budget,
        "budget_unexpected": budget,
        "explicit_budget_read": budget,
    }
    if case in targets:
        read_text = Path.read_text

        def broken_read(path, *args, **kwargs):
            if path == targets[case]:
                error = RuntimeError if case == "budget_unexpected" else PermissionError
                raise error("SYNTHETIC-SECRET-MARKER PRIVATE-PATH-MARKER")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", broken_read)
    elif case == "budget_import":
        original_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "src.core.nfr_budgets":
                raise ImportError("SYNTHETIC-SECRET-MARKER PRIVATE-PATH-MARKER")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", broken_import)
    args = ["--repo-root", str(root), "--spec", str(spec), "--telemetry", str(telemetry)]
    if case.startswith("explicit_budget_"):
        args.extend(["--budgets", str(budget)])
    if json_output:
        args.append("--json")

    exit_code = main(args)

    output = capsys.readouterr()
    assert exit_code == (6 if failed else 0)
    if json_output:
        results = json.loads(output.out)
        assert [row["id"] for row in results if not row["passed"]] == failed
        if diagnostic:
            assert all(diagnostic in row["detail"] for row in results if not row["passed"])
    else:
        assert [line.split()[1].rstrip(":") for line in output.out.splitlines() if line.startswith("FAIL ")] == failed
        if diagnostic:
            assert diagnostic in output.out
    assert "SYNTHETIC-SECRET-MARKER" not in output.out + output.err
    assert "PRIVATE-PATH-MARKER" not in output.out + output.err
    assert "0xff" not in output.out + output.err
    assert output.err == ""
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(("case", "diagnostic"), [
    ("valid", None),
    ("mismatch", "unexpected values"),
    ("malformed_yaml", "not valid YAML"),
    ("invalid_utf8", "invalid UTF-8"),
    ("no_ai", "no `ai:` mapping"),
    ("unreadable", "Unable to read"),
])
def test_selected_program_config_diagnostics_do_not_disclose_inputs(
    tmp_path, monkeypatch, capsys, json_output, case, diagnostic,
) -> None:
    spec, telemetry = _write_normal_verifier_fixture(tmp_path)
    config_dir = tmp_path / "PRIVATE-PATH-MARKER"
    config_dir.mkdir()
    config = config_dir / "program.yaml"
    if case == "mismatch":
        config.write_text(
            "ai:\n  blurb_deployment: SYNTHETIC-SECRET-MARKER\n"
            "  exec_summary_deployment:\n    nested: SYNTHETIC-SECRET-MARKER\n",
            encoding="utf-8",
        )
    elif case == "malformed_yaml":
        config.write_text("ai:\n  blurb_deployment: [SYNTHETIC-SECRET-MARKER\n", encoding="utf-8")
    elif case == "invalid_utf8":
        config.write_bytes(b"\xffSYNTHETIC-SECRET-MARKER")
    elif case == "no_ai":
        config.write_text("other: SYNTHETIC-SECRET-MARKER\n", encoding="utf-8")
    else:
        config.write_text(
            "ai:\n  blurb_deployment: ${VERTEX_AI_DEPLOYMENT}\n"
            "  exec_summary_deployment: ${VERTEX_EXEC_DEPLOYMENT}\n",
            encoding="utf-8",
        )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    if case == "unreadable":
        read_text = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == config:
                raise PermissionError("SYNTHETIC-SECRET-MARKER")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    args = [
        "--repo-root", str(tmp_path), "--spec", str(spec),
        "--telemetry", str(telemetry), "--program-config", str(config),
    ]
    if json_output:
        args.append("--json")

    exit_code = main(args)

    output = capsys.readouterr()
    assert exit_code == (0 if diagnostic is None else 6)
    if json_output:
        results = json.loads(output.out)
        assert [row["id"] for row in results if not row["passed"]] == (
            [] if diagnostic is None else ["RELO-W0.9a"]
        )
    else:
        assert "PASS RELO-SPEC-LINT" in output.out
        assert ("FAIL RELO-W0.9a" in output.out) is (diagnostic is not None)
    if diagnostic is not None:
        assert diagnostic in output.out
    if case == "mismatch":
        assert "blurb_deployment" in output.out
        assert "exec_summary_deployment" in output.out
    assert "SYNTHETIC-SECRET-MARKER" not in output.out + output.err
    assert "PRIVATE-PATH-MARKER" not in output.out + output.err
    assert "0xff" not in output.out + output.err
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("json_output", [False, True])
def test_unselected_program_config_is_not_read(tmp_path, monkeypatch, capsys, json_output) -> None:
    spec, _ = _write_normal_verifier_fixture(tmp_path)
    config = tmp_path / "program.yaml"
    config.write_bytes(b"\xffSYNTHETIC-SECRET-MARKER")
    read_text = Path.read_text

    def unexpected_config_read(path, *args, **kwargs):
        if path == config:
            raise AssertionError("unselected program configuration was read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unexpected_config_read)
    args = ["--repo-root", str(tmp_path), "--spec", str(spec)]
    if json_output:
        args.append("--json")

    assert main(args) == 0

    output = capsys.readouterr()
    assert "RELO-W0.9a" not in output.out
    assert "SYNTHETIC-SECRET-MARKER" not in output.out + output.err
    assert config.read_bytes() == b"\xffSYNTHETIC-SECRET-MARKER"


@pytest.mark.parametrize("source", ["read", "parse"])
def test_selected_program_config_propagates_unexpected_error(tmp_path, monkeypatch, source) -> None:
    spec, _ = _write_normal_verifier_fixture(tmp_path)
    config = tmp_path / "program.yaml"
    config.write_text("ai: {}\n", encoding="utf-8")
    read_text = Path.read_text

    def broken_read(path, *args, **kwargs):
        if path == config:
            raise RuntimeError("unexpected failure")
        return read_text(path, *args, **kwargs)

    if source == "read":
        monkeypatch.setattr(Path, "read_text", broken_read)
    else:
        def broken_parse(*args, **kwargs):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr("scripts.verify_relo.yaml.safe_load", broken_parse)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        main(["--repo-root", str(tmp_path), "--spec", str(spec), "--program-config", str(config)])


def _write_work_package_path_fixture(tmp_path, field, kind):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "existing.py").write_text("# synthetic\n", encoding="utf-8")
    (root / "subdir").mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("# external synthetic\n", encoding="utf-8")
    paths = {
        "relative": "existing.py",
        "inside_absolute": str(root / "existing.py"),
        "inside_normalized": str(Path("subdir") / ".." / "existing.py"),
        "outside_absolute": str(outside),
        "outside_parent": str(Path("..") / "outside.py"),
    }
    if kind in {"inside_link", "outside_link"}:
        link = root / f"{kind}.py"
        target = root / "existing.py" if kind == "inside_link" else outside
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Synthetic file symlinks are unavailable")
        paths[kind] = link.name
    card = _ready_work_package()
    card[field] = [paths[kind]]
    spec = _write_work_package_fixture(root, [card])
    return root, spec, outside, paths[kind]


@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("kind", ["outside_absolute", "outside_parent", "outside_link"])
def test_work_package_lint_rejects_files_outside_repository(tmp_path, field, kind) -> None:
    root, spec, outside, supplied = _write_work_package_path_fixture(tmp_path, field, kind)
    before = outside.read_bytes()

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is False
    assert f"{field} path is outside selected repository" in result.detail
    assert supplied not in result.detail
    assert outside.read_bytes() == before


@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("kind", ["relative", "inside_absolute", "inside_normalized", "inside_link"])
def test_work_package_lint_accepts_files_inside_repository(tmp_path, field, kind) -> None:
    root, spec, _, _ = _write_work_package_path_fixture(tmp_path, field, kind)

    assert lint_work_packages(root, spec_path=spec).passed is True


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_work_package_lint_rejects_directory_paths(tmp_path, field) -> None:
    root, _, _, _ = _write_work_package_path_fixture(tmp_path, field, "relative")
    card = _ready_work_package()
    card[field] = ["subdir"]
    spec = _write_work_package_fixture(root, [card])

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is False
    assert "unresolved file path" in result.detail


@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("value", [None, 42, {}, []])
def test_work_package_lint_rejects_non_string_path_entries(tmp_path, field, value) -> None:
    card = _ready_work_package()
    card[field] = [value]
    spec = _write_work_package_fixture(tmp_path, [card])

    result = lint_work_packages(tmp_path, spec_path=spec)

    assert result.passed is False
    assert "unresolved file path" in result.detail


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("kind", [
    "valid", "missing_relative", "missing_absolute", "directory",
    "nested", "invalid_path", "multiple",
])
def test_work_package_path_diagnostics_do_not_disclose_values(
    tmp_path, capsys, json_output, field, kind,
) -> None:
    root = tmp_path / "PRIVATE-PATH-MARKER"
    root.mkdir()
    spec, telemetry = _write_normal_verifier_fixture(root)
    marker = "SYNTHETIC-SECRET-MARKER"
    (root / f"{marker}.py").write_text("# synthetic\n", encoding="utf-8")
    (root / marker).mkdir()
    paths = {
        "valid": [f"{marker}.py"],
        "missing_relative": [f"{marker}-missing.py"],
        "missing_absolute": [str(root / f"{marker}-missing.py")],
        "directory": [marker],
        "nested": [{"value": marker}],
        "invalid_path": [f"{marker}\0"],
        "multiple": ["existing.py", f"{marker}-missing.py", {"value": marker}],
    }
    card = _ready_work_package()
    card[field] = paths[kind]
    ledger = root / "governance" / "relo-work-packages.yaml"
    ledger.write_text(json.dumps({"schema_version": "1", "work_items": [card]}), encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    args = ["--repo-root", str(root), "--spec", str(spec), "--telemetry", str(telemetry)]
    if json_output:
        args.append("--json")

    exit_code = main(args)

    output = capsys.readouterr()
    failed = [] if kind == "valid" else ["RELO-WORK-PACKAGES"]
    assert exit_code == (0 if not failed else 6)
    if json_output:
        results = json.loads(output.out)
        assert [row["id"] for row in results if not row["passed"]] == failed
        detail = next(row["detail"] for row in results if row["id"] == "RELO-WORK-PACKAGES")
    else:
        assert [line.split()[1].rstrip(":") for line in output.out.splitlines() if line.startswith("FAIL ")] == failed
        detail = output.out
    for position in (() if kind == "valid" else (2, 3) if kind == "multiple" else (1,)):
        assert f"RELO-W0.1: unresolved file path in {field} at entry {position}" in detail
    assert marker not in output.out + output.err
    assert "PRIVATE-PATH-MARKER" not in output.out + output.err
    assert output.err == ""
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_unresolved_work_package_paths_preserve_other_card_findings(tmp_path, field) -> None:
    ready = _ready_work_package()
    ready[field] = [{"value": "SYNTHETIC-SECRET-MARKER"}]
    judgment = {"id": "RELO-W0.2", "class": "NEEDS_JUDGMENT", "depends_on": ["absent"]}
    spec = _write_work_package_fixture(
        tmp_path, [ready, judgment], identifiers=("RELO-W0.1", "RELO-W0.2"),
    )
    ledger = tmp_path / "governance" / "relo-work-packages.yaml"
    before = ledger.read_bytes()

    result = lint_work_packages(tmp_path, spec_path=spec)

    assert result.passed is False
    assert f"RELO-W0.1: unresolved file path in {field} at entry 1" in result.detail
    assert "RELO-W0.2: unresolved depends_on" in result.detail
    assert "SYNTHETIC-SECRET-MARKER" not in result.detail
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_work_package_path_check_propagates_unexpected_errors(tmp_path, monkeypatch, field) -> None:
    (tmp_path / "target.py").write_text("# synthetic\n", encoding="utf-8")
    card = _ready_work_package()
    card[field] = ["target.py"]
    spec = _write_work_package_fixture(tmp_path, [card])
    realpath = os.path.realpath

    def broken_realpath(path, *args, **kwargs):
        if Path(path).name == "target.py":
            raise RuntimeError("unexpected failure")
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", broken_realpath)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        lint_work_packages(tmp_path, spec_path=spec)


@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("kind", ["outside_absolute", "outside_parent"])
def test_work_package_lint_rejects_lexical_escape_before_resolving(
    tmp_path, monkeypatch, field, kind,
) -> None:
    root, spec, outside, _ = _write_work_package_path_fixture(tmp_path, field, kind)
    realpath = os.path.realpath

    def checked_realpath(path, *args, **kwargs):
        if Path(os.path.abspath(path)) == outside:
            raise AssertionError("outside path was resolved")
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", checked_realpath)

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is False
    assert f"{field} path is outside selected repository" in result.detail


@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("kind", ["relative", "inside_absolute", "inside_normalized", "linked_absolute"])
def test_work_package_lint_accepts_in_repository_paths_through_linked_root(tmp_path, field, kind) -> None:
    fixture_kind = "relative" if kind == "linked_absolute" else kind
    root, _, _, _ = _write_work_package_path_fixture(tmp_path, field, fixture_kind)
    link = tmp_path / "linked-repo"
    try:
        link.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Synthetic directory symlinks are unavailable")
    if kind == "linked_absolute":
        card = _ready_work_package()
        card[field] = [str(link / "existing.py")]
        _write_work_package_fixture(root, [card])

    result = lint_work_packages(link, spec_path=link / "relo.md")

    assert result.passed is True


@pytest.mark.parametrize("field", ["read_first", "change"])
@pytest.mark.parametrize("kind", ["outside_absolute", "outside_parent", "outside_link"])
def test_work_package_lint_rejects_outside_paths_through_linked_root(tmp_path, field, kind) -> None:
    root, _, _, supplied = _write_work_package_path_fixture(tmp_path, field, kind)
    link = tmp_path / "linked-repo"
    try:
        link.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Synthetic directory symlinks are unavailable")

    result = lint_work_packages(link, spec_path=link / "relo.md")

    assert result.passed is False
    assert f"{field} path is outside selected repository" in result.detail
    assert supplied not in result.detail


def _write_link_parent_fixture(tmp_path, field):
    root, _, _, _ = _write_work_package_path_fixture(tmp_path, field, "relative")
    external = tmp_path / "external"
    (external / "child").mkdir(parents=True)
    (external / "existing.py").write_text("# external synthetic\n", encoding="utf-8")
    try:
        (root / "link").symlink_to(external / "child", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Synthetic directory symlinks are unavailable")
    relative = str(Path("link") / ".." / "existing.py")
    card = _ready_work_package()
    card[field] = [relative]
    return root, _write_work_package_fixture(root, [card]), relative, external / "existing.py"


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_work_package_lint_uses_native_link_parent_resolution(tmp_path, field) -> None:
    root, spec, relative, _ = _write_link_parent_fixture(tmp_path, field)
    resolved = Path(os.path.realpath(root / relative, strict=True))

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is resolved.is_relative_to(Path(os.path.realpath(root)))


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_work_package_lint_checks_original_link_parent_target(tmp_path, monkeypatch, field) -> None:
    root, spec, relative, outside = _write_link_parent_fixture(tmp_path, field)
    original = root / relative
    realpath = os.path.realpath

    def posix_realpath(path, *args, **kwargs):
        if Path(path) == original:
            return str(outside)
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", posix_realpath)

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is False
    assert f"{field} path is outside selected repository" in result.detail
    assert relative not in result.detail


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_work_package_lint_rejects_missing_intermediate_component(tmp_path, monkeypatch, field) -> None:
    root, _, _, _ = _write_work_package_path_fixture(tmp_path, field, "relative")
    relative = str(Path("missing") / ".." / "existing.py")
    card = _ready_work_package()
    card[field] = [relative]
    spec = _write_work_package_fixture(root, [card])
    original = root / relative
    realpath = os.path.realpath

    def posix_realpath(path, *args, **kwargs):
        if Path(path) == original and kwargs.get("strict") is True:
            raise FileNotFoundError("missing intermediate component")
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", posix_realpath)

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is False
    assert "unresolved file path" in result.detail


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_work_package_lint_uses_native_missing_component_resolution(tmp_path, field) -> None:
    root, _, _, _ = _write_work_package_path_fixture(tmp_path, field, "relative")
    relative = str(Path("missing") / ".." / "existing.py")
    card = _ready_work_package()
    card[field] = [relative]
    spec = _write_work_package_fixture(root, [card])
    try:
        resolved = Path(os.path.realpath(root / relative, strict=True))
    except OSError:
        expected = False
    else:
        expected = resolved.is_relative_to(Path(os.path.realpath(root))) and resolved.is_file()

    assert lint_work_packages(root, spec_path=spec).passed is expected


@pytest.mark.parametrize("error", [OSError("unavailable"), ValueError("invalid")])
def test_work_package_lint_reports_path_resolution_failures(tmp_path, monkeypatch, error) -> None:
    root, spec, _, _ = _write_work_package_path_fixture(tmp_path, "change", "relative")
    realpath = os.path.realpath

    def failed_realpath(path, *args, **kwargs):
        if Path(os.path.abspath(path)) == root / "existing.py":
            raise error
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", failed_realpath)

    result = lint_work_packages(root, spec_path=spec)

    assert result.passed is False
    assert "unresolved file path" in result.detail


def test_work_package_lint_propagates_unexpected_path_resolution_error(tmp_path, monkeypatch) -> None:
    root, spec, _, _ = _write_work_package_path_fixture(tmp_path, "change", "relative")
    realpath = os.path.realpath

    def failed_realpath(path, *args, **kwargs):
        if Path(os.path.abspath(path)) == root / "existing.py":
            raise RuntimeError("unexpected failure")
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", failed_realpath)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        lint_work_packages(root, spec_path=spec)


@pytest.mark.parametrize("field", ["read_first", "change"])
def test_verifier_cli_rejects_outside_work_package_path_without_mutation(tmp_path, capsys, field) -> None:
    root, spec, outside, supplied = _write_work_package_path_fixture(tmp_path, field, "outside_parent")
    (root / "pyproject.toml").write_text(
        'addopts = "-m \'not perf\'"\nmarkers = ["perf: lane"]\n', encoding="utf-8",
    )
    _write_budgets(
        root, line_status="ratified", line_value=100,
        obligation_status="ratified", obligation_value=10,
    )
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    outside_before = outside.read_bytes()

    exit_code = main(["--repo-root", str(root), "--spec", str(spec), "--json"])

    output = capsys.readouterr()
    results = json.loads(output.out)
    assert exit_code == 6
    assert [row["id"] for row in results if not row["passed"]] == ["RELO-WORK-PACKAGES"]
    assert supplied not in output.out + output.err
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert outside.read_bytes() == outside_before
