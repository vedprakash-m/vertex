from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from scripts.verify_relo import assess_closeout, main

_SPEC = """**Version:** 1.0.0

## 5. Requirements

### 5.5 Functional requirements

| ID | Requirement |
|---|---|
| **RELO-FR-01** | Trace work definitions to requirements. |
| **RELO-FR-02** | Report unresolved real-world evidence. |

### 5.6 Nonfunctional requirements

| ID | Requirement |
|---|---|
| **RELO-NFR-01** | Require complete measurement packets. |

## 11. Work definitions

### 11.1 Phase zero

| ID | Work item |
|---|---|
| RELO-W0.12 | Reconcile traceability. |
| RELO-W0.13 | Define current evidence records. |
| RELO-W0.14 | Define NFR evidence completeness. |

## 19. Traceability

### 19.2 Requirement-to-delivery traceability

| Requirement | Work items | Gate | Primary test/evidence | Phase |
|---|---|---|---|---|
| RELO-FR-01 | W0.12 | QG-01 | generated traceability | RELO-0 |
| RELO-FR-02 | W0.13 | QG-02 | staffing/economics packet | RELO-0 |
| RELO-NFR-01 | W0.14 | QG-03 | NFR packet | RELO-0 |
"""

_BUDGETS = """schema_version: "1"
budgets:
  - id: synthetic-latency
    area: Synthetic
    description: Synthetic numeric budget.
    budget_class: microbenchmark
    direction: ceiling
    unit: ms
    value: null
    status: candidate
    ratified_at: null
    ratified_by: null
  - id: synthetic-policy
    area: Synthetic
    description: Synthetic qualitative budget.
    budget_class: periodic-governance-review
    direction: qualitative
    unit: null
    value: null
    status: candidate
    ratified_at: null
    ratified_by: null
"""

_PENDING_PACKET = """schema_version: "1"
staffing:
  status: pending
  as_of: null
  evidence_refs: []
  approval:
    status: pending
    approved_by: null
    approved_at: null
economics:
  status: pending
  as_of: null
  window_start: null
  window_end: null
  workload_profile: null
  evidence_refs: []
  approval:
    status: pending
    approved_by: null
    approved_at: null
nfr_measurements: []
"""


def _root(tmp_path: Path, *, spec: str = _SPEC) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    (root / "governance").mkdir(parents=True)
    spec_path = root / "spec.md"
    budgets_path = root / "governance" / "nfr-budgets.yaml"
    spec_path.write_text(spec, encoding="utf-8")
    budgets_path.write_text(_BUDGETS, encoding="utf-8")
    return root, spec_path, budgets_path


def _supplied_packet(root: Path) -> Path:
    packet = {
        "schema_version": "1",
        "staffing": {
            "status": "supplied",
            "as_of": "2000-01-01",
            "evidence_refs": ["synthetic:staffing-record"],
            "approval": {
                "status": "approved",
                "approved_by": "Synthetic approver",
                "approved_at": "2000-01-02",
            },
        },
        "economics": {
            "status": "supplied",
            "as_of": "2000-01-01",
            "window_start": "1999-12-25",
            "window_end": "2000-01-01",
            "workload_profile": "synthetic-workload",
            "evidence_refs": ["synthetic:economics-record"],
            "approval": {
                "status": "approved",
                "approved_by": "Synthetic approver",
                "approved_at": "2000-01-02",
            },
        },
        "nfr_measurements": [
            {
                "budget_id": "synthetic-latency",
                "status": "supplied",
                "as_of": "2000-01-01",
                "workload_profile": "synthetic-workload",
                "verification_method": "synthetic-method",
                "observed_value": 1,
                "unit": "ms",
                "evidence_refs": ["synthetic:numeric-measurement"],
                "approval": {
                    "status": "approved",
                    "approved_by": "Synthetic approver",
                    "approved_at": "2000-01-02",
                },
            },
            {
                "budget_id": "synthetic-policy",
                "status": "supplied",
                "as_of": "2000-01-01",
                "workload_profile": "synthetic-policy-scope",
                "verification_method": "synthetic-review",
                "policy_result": "synthetic-policy-result",
                "evidence_refs": ["synthetic:policy-measurement"],
                "approval": {
                    "status": "approved",
                    "approved_by": "Synthetic approver",
                    "approved_at": "2000-01-02",
                },
            },
        ],
    }
    path = root / "closeout.yaml"
    path.write_text(yaml.safe_dump(packet, sort_keys=False), encoding="utf-8")
    return path


def test_closeout_generates_requirement_backlinks_without_private_data(tmp_path: Path) -> None:
    root, spec, budgets = _root(tmp_path)

    report = assess_closeout(root, spec_path=spec, budgets_path=budgets)

    assert report["terminal_state"] is None
    assert report["assessment"] == "not_assessed"
    assert report["traceability"] == {
        "scope": ["RELO-W0.12", "RELO-W0.13", "RELO-W0.14"],
        "defined_work_items": ["RELO-W0.12", "RELO-W0.13", "RELO-W0.14"],
        "requirement_to_work_items": {
            "RELO-FR-01": ["RELO-W0.12"],
            "RELO-FR-02": ["RELO-W0.13"],
            "RELO-NFR-01": ["RELO-W0.14"],
        },
        "work_item_to_requirements": {
            "RELO-W0.12": ["RELO-FR-01"],
            "RELO-W0.13": ["RELO-FR-02"],
            "RELO-W0.14": ["RELO-NFR-01"],
        },
        "missing_work_item_definitions": [],
        "work_items_without_requirement_backlinks": [],
        "complete": True,
    }
    assert report["evidence_packet"]["complete"] is False
    assert [item["id"] for item in report["evidence_packet"]["remaining"]] == [
        "RELO-W0.13.staffing-current",
        "RELO-W0.13.economics-current",
        "RELO-W0.14.nfr.synthetic-latency",
        "RELO-W0.14.nfr.synthetic-policy",
    ]


def test_closeout_reports_only_exact_scope_backlink_gap(tmp_path: Path) -> None:
    root, spec, budgets = _root(
        tmp_path,
        spec=_SPEC.replace(
            "| RELO-NFR-01 | W0.14 | QG-03 | NFR packet | RELO-0 |",
            "| RELO-NFR-01 | W0.13 | QG-03 | NFR packet | RELO-0 |",
        ),
    )

    report = assess_closeout(root, spec_path=spec, budgets_path=budgets)

    assert report["traceability"]["work_items_without_requirement_backlinks"] == [
        "RELO-W0.14"
    ]
    assert report["traceability"]["complete"] is False


def test_closeout_traceability_unreadable_spec_is_unavailable(tmp_path: Path) -> None:
    root, spec, budgets = _root(tmp_path)
    spec.write_bytes(b"\xff\xfe")

    report = assess_closeout(root, spec_path=spec, budgets_path=budgets)

    assert "read failed (UnicodeDecodeError)" in report["traceability"]["unavailable"]


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        (
            _SPEC.replace(
                "|---|---|\n| RELO-W0.12 |",
                "|bad|bad|\n| RELO-W0.12 |",
            ),
            "phase work-item definition table is missing or malformed",
        ),
        (
            _SPEC.split("## 19. Traceability", maxsplit=1)[0],
            "no '### 19.2' traceability section",
        ),
        (
            _SPEC.replace(
                "| Requirement | Work items | Gate | Primary test/evidence | Phase |",
                "| Requirement | Work items | Gate | Other evidence | Phase |",
            ),
            "requirement traceability table is missing or malformed",
        ),
        (
            _SPEC.replace(
                "| RELO-FR-01 | W0.12 | QG-01 | generated traceability | RELO-0 |",
                "| RELO-FR-01 | W0.12 | QG-01 |  | RELO-0 |",
            ),
            "requirement traceability row is missing or malformed",
        ),
        (
            _SPEC.replace(" | W0.12 |", " | W0.12.invalid |", 1),
            "requirement traceability work references are invalid",
        ),
        (
            _SPEC.replace(
                "| RELO-W0.14 | Define NFR evidence completeness. |",
                "| RELO-W0.14 | Define NFR evidence completeness. |\n"
                "| RELO-W1.1 | Unscoped work. |",
            ).replace(" | W0.12 |", " | W1.1 |", 1),
            "work_items_without_requirement_backlinks",
        ),
        (
            _SPEC.replace(
                "| RELO-FR-01 | W0.12 | QG-01 | generated traceability | RELO-0 |",
                "| narrative row | W0.12 | prose | generated traceability | RELO-0 |\n"
                "| RELO-FR-01 | W0.12 | QG-01 | generated traceability | RELO-0 |",
            ),
            "complete",
        ),
    ],
)
def test_closeout_traceability_rejects_malformed_or_unscoped_input(
    tmp_path: Path, content: str, finding: str,
) -> None:
    root, spec, budgets = _root(tmp_path, spec=content)

    report = assess_closeout(root, spec_path=spec, budgets_path=budgets)

    traceability = report["traceability"]
    if finding == "work_items_without_requirement_backlinks":
        assert traceability[finding] == ["RELO-W0.12"]
    elif finding == "complete":
        assert traceability[finding] is True
    else:
        assert finding in traceability["unavailable"]


def test_pending_packet_names_every_real_data_and_approval_obligation(tmp_path: Path) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = root / "pending.yaml"
    packet.write_text(_PENDING_PACKET, encoding="utf-8")

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    remaining = report["evidence_packet"]["remaining"]
    assert all(item["requires_real_data"] is True for item in remaining)
    assert all(item["requires_owner_approval"] is True for item in remaining)
    assert [item["missing"] for item in remaining[:2]] == [
        ["as_of", "evidence_refs", "approval"],
        [
            "as_of",
            "window_start",
            "window_end",
            "workload_profile",
            "evidence_refs",
            "approval",
        ],
    ]
    assert report["evidence_packet"]["complete"] is False
    assert report["assessment"] == "not_assessed"


def test_supplied_packet_is_structurally_complete_but_not_qualification(
    tmp_path: Path,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = _supplied_packet(root)

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    assert report["evidence_packet"]["complete"] is True
    assert report["evidence_packet"]["remaining"] == []
    assert report["terminal_state"] is None
    assert report["assessment"] == "not_assessed"
    assert "qualification" not in report["evidence_packet"]
    assert "Synthetic approver" not in json.dumps(report)
    assert "synthetic:economics-record" not in json.dumps(report)


def test_public_packet_template_is_value_free_and_reports_current_budget_items(
    tmp_path: Path,
) -> None:
    root, spec, budgets = _root(tmp_path)
    template = (
        Path(__file__).resolve().parents[2]
        / "governance"
        / "templates"
        / "relo-w0-closeout-evidence.yaml"
    )

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=template,
    )

    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert document["staffing"]["status"] == "pending"
    assert document["staffing"]["as_of"] is None
    assert document["economics"]["status"] == "pending"
    assert document["nfr_measurements"] == []
    assert len(report["evidence_packet"]["remaining"]) == 4
    assert report["evidence_packet"]["complete"] is False


def test_supplied_nfr_packet_rejects_boolean_observation_and_wrong_unit(
    tmp_path: Path,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = _supplied_packet(root)
    document = yaml.safe_load(packet.read_text(encoding="utf-8"))
    document["nfr_measurements"][0]["observed_value"] = True
    document["nfr_measurements"][0]["unit"] = "s"
    packet.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    remaining = next(
        item
        for item in report["evidence_packet"]["remaining"]
        if item["id"] == "RELO-W0.14.nfr.synthetic-latency"
    )
    assert remaining["missing"] == ["observed_value", "unit"]
    assert report["evidence_packet"]["complete"] is False


def test_closeout_rejects_inverted_window_and_incomplete_measurement_fields(
    tmp_path: Path,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = _supplied_packet(root)
    document = yaml.safe_load(packet.read_text(encoding="utf-8"))
    document["economics"]["window_start"] = "2000-02-01"
    document["economics"]["window_end"] = "2000-01-01"
    document["nfr_measurements"][0]["as_of"] = "invalid"
    document["nfr_measurements"][0]["workload_profile"] = ""
    document["nfr_measurements"][0]["verification_method"] = None
    document["nfr_measurements"][0]["evidence_refs"] = []
    document["nfr_measurements"][0]["approval"] = {"status": "pending"}
    document["nfr_measurements"][1]["policy_result"] = ""
    packet.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    remaining = report["evidence_packet"]["remaining"]
    assert "window_end" in next(
        item["missing"]
        for item in remaining
        if item["category"] == "economics"
    )
    assert next(
        item["missing"]
        for item in remaining
        if item["id"] == "RELO-W0.14.nfr.synthetic-latency"
    ) == [
        "as_of",
        "workload_profile",
        "verification_method",
        "evidence_refs",
        "approval",
    ]
    assert "policy_result" in next(
        item["missing"]
        for item in remaining
        if item["id"] == "RELO-W0.14.nfr.synthetic-policy"
    )


def test_closeout_packet_rejects_nonmapping_sections_and_measurement_list(
    tmp_path: Path,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = root / "malformed-shape.yaml"
    packet.write_text(
        yaml.safe_dump({
            "schema_version": "1",
            "staffing": [],
            "economics": "secret",
            "nfr_measurements": {},
        }),
        encoding="utf-8",
    )

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    assert report["evidence_packet"]["findings"] == [
        "economics evidence is incomplete",
        "nfr_measurements must be a list",
        "staffing evidence is incomplete",
    ]
    assert "secret" not in json.dumps(report["evidence_packet"])


def test_closeout_packet_rejects_nonmapping_document(tmp_path: Path) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = root / "list-packet.yaml"
    packet.write_text("[]\n", encoding="utf-8")

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    assert report["evidence_packet"]["findings"] == [
        "economics evidence is incomplete",
        "evidence packet must be a mapping",
        "nfr_measurements must be a list",
        "schema_version must be '1'",
        "staffing evidence is incomplete",
    ]


def test_closeout_packet_rejects_duplicate_unknown_and_unidentified_nfr_rows(
    tmp_path: Path,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = _supplied_packet(root)
    document = yaml.safe_load(packet.read_text(encoding="utf-8"))
    duplicate = dict(document["nfr_measurements"][0])
    document["nfr_measurements"].extend([
        duplicate,
        {"status": "pending"},
        {"status": "pending", "budget_id": "secret-unknown-budget"},
    ])
    packet.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    findings = report["evidence_packet"]["findings"]
    assert "NFR evidence entry 4 is incomplete" in findings
    assert "NFR evidence contains a duplicate budget_id" in findings
    assert "NFR evidence contains unknown budget_id entries" in findings
    assert "secret-unknown-budget" not in json.dumps(report["evidence_packet"])


@pytest.mark.parametrize("content", [None, b"a: [", b"\xff"])
def test_closeout_packet_read_and_yaml_failures_are_unavailable(
    tmp_path: Path, content: bytes | None,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = root / "unavailable.yaml"
    if content is not None:
        packet.write_bytes(content)

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    assert "unavailable" in report["evidence_packet"]
    assert str(tmp_path) not in report["evidence_packet"]["unavailable"]


def test_closeout_budget_loader_failure_is_unavailable_without_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    root, spec, budgets = _root(tmp_path)
    monkeypatch.setitem(sys.modules, "src.core.nfr_budgets", None)

    report = assess_closeout(root, spec_path=spec, budgets_path=budgets)

    assert "loader import failed" in report["evidence_packet"]["unavailable"]
    assert "SECRET" not in json.dumps(report["evidence_packet"])


@pytest.mark.parametrize(
    ("mutation", "finding"),
    [
        ({"schema_version": "2"}, "schema_version must be '1'"),
        ({"staffing": {"status": "supplied"}}, "staffing evidence is incomplete"),
        ({"economics": {"status": "supplied"}}, "economics evidence is incomplete"),
        ({"nfr_measurements": [{"budget_id": "synthetic-latency", "status": "secret"}]},
         "NFR evidence entry 1 is incomplete"),
    ],
)
def test_invalid_closeout_packet_reports_content_free_findings(
    tmp_path: Path, mutation: dict, finding: str,
) -> None:
    root, spec, budgets = _root(tmp_path)
    document = yaml.safe_load(_PENDING_PACKET)
    document.update(mutation)
    packet = root / "invalid.yaml"
    packet.write_text(yaml.safe_dump(document), encoding="utf-8")

    report = assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )

    assert finding in report["evidence_packet"]["findings"]
    assert "secret" not in json.dumps(report["evidence_packet"])


def test_closeout_cli_is_read_only_json_compatible_and_never_certifies(
    tmp_path: Path, capsys,
) -> None:
    root, spec, budgets = _root(tmp_path)
    packet = _supplied_packet(root)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    result = main([
        "--repo-root", str(root),
        "--spec", str(spec),
        "--budgets", str(budgets),
        "--packet", str(packet),
        "--closeout",
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert result == 6
    assert payload == assess_closeout(
        root, spec_path=spec, budgets_path=budgets, evidence_packet_path=packet,
    )
    assert payload["terminal_state"] is None
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


def test_closeout_cli_reports_unavailable_input_as_exit_one(
    tmp_path: Path, capsys,
) -> None:
    root, spec, budgets = _root(tmp_path)
    budgets.write_bytes(b"\xff")

    assert main([
        "--repo-root", str(root),
        "--spec", str(spec),
        "--budgets", str(budgets),
        "--closeout",
        "--json",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert "unavailable" in report["evidence_packet"]


def test_closeout_human_output_lists_missing_items_and_limitations(
    tmp_path: Path, capsys,
) -> None:
    root, spec, budgets = _root(tmp_path)

    assert main([
        "--repo-root", str(root),
        "--spec", str(spec),
        "--budgets", str(budgets),
        "--closeout",
    ]) == 6
    output = capsys.readouterr().out
    assert output.startswith("NOT ASSESSED RELO-W0 closeout:")
    assert "traceability:" in output
    assert "MISSING RELO-W0.13.staffing-current:" in output
    assert "LIMITATION " in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--terminal-state", "--closeout"],
        ["--packet", "packet.yaml"],
    ],
)
def test_closeout_cli_rejects_incompatible_modes(
    tmp_path: Path, arguments: list[str], capsys,
) -> None:
    root, spec, _budgets = _root(tmp_path)

    with pytest.raises(SystemExit) as error:
        main(["--repo-root", str(root), "--spec", str(spec), *arguments])

    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err
