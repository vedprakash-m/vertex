from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.verify_relo as verify_relo
from scripts.verify_relo import assess_terminal_state, main

_SPEC = """**Status:** Proposed

## 13. Rollout

### 13.6 Terminal states

## 20. Open Decisions

| ID | Decision | Recommendation | Deadline |
|---|---|---|---|
| **RELO-D1** | ~~Durable?~~ **RESOLVED 2026-07-28:** no | x | Resolved |
| **RELO-D2** | ~~Route?~~ **RESOLVED 2026-07-28 (interim):** WorkIQ | x | Resolved (interim) |
| **RELO-D16** | Product-vision gate owner? | PRD | Before RELO-6 exit |

## 21. Next
"""

_LEDGER = """schema_version: "1"
work_items:
  - id: RELO-W0.1
    phase: RELO-0
    class: AGENT_READY
    status: completed
  - id: RELO-W0.2
    phase: RELO-0
    class: NEEDS_JUDGMENT
    status: in_progress
  - id: RELO-W6.12
    phase: RELO-6
    class: NEEDS_JUDGMENT
    status: planned
  - id: RELO-W6.12a
    phase: RELO-6
    class: AGENT_READY
    status: some_future_status
"""

_MATRIX = """schema_version: "1"
sources:
  - source: ado
    mandatory: true
    scheduled:
      principal_mode: none
  - source: icm
    mandatory: false
    scheduled:
      principal_mode: none
"""

_BUDGETS = """schema_version: "1"
budgets:
  - id: ratified-one
    budget_class: integration
    direction: ceiling
    status: ratified
  - id: candidate-one
    budget_class: integration
    direction: ceiling
    status: candidate
"""


def _root(tmp_path: Path, **overrides: str | None) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "governance").mkdir(parents=True)
    files = {
        "governance/relo-work-packages.yaml": _LEDGER,
        "governance/relo-authority-matrix.yaml": _MATRIX,
        "governance/nfr-budgets.yaml": _BUDGETS,
        "spec.md": _SPEC,
    }
    files.update(overrides)
    for relative, content in files.items():
        if content is not None:
            (root / relative).write_text(content, encoding="utf-8")
    return root, root / "spec.md"


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    ("relative", "content", "section", "guidance"),
    [
        ("governance/relo-work-packages.yaml", None, "work_ledger", "read failed (FileNotFoundError)"),
        ("governance/relo-work-packages.yaml", "a: [", "work_ledger", "parse failed"),
        ("governance/relo-work-packages.yaml", "schema_version: '2'\nwork_items: []\n", "work_ledger", "schema_version must be '1'"),
        ("governance/relo-work-packages.yaml", "schema_version: '1'\nwork_items: {}\n", "work_ledger", "work_items must be a list"),
        ("governance/relo-work-packages.yaml", "schema_version: '1'\nwork_items: [x]\n", "work_ledger", "work item 1 must be a mapping"),
        ("governance/relo-work-packages.yaml", "schema_version: '1'\nwork_items:\n  - {id: A, phase: P, class: C, status: ''}\n", "work_ledger", "work item 1 needs non-empty string status"),
        ("governance/relo-work-packages.yaml", "schema_version: '1'\nwork_items:\n  - {id: A, phase: P, status: s}\n", "work_ledger", "work item 1 needs non-empty string class"),
        ("governance/relo-work-packages.yaml", "schema_version: '1'\nwork_items:\n  - {id: A, phase: P, class: C, status: s}\n  - {id: A, phase: P, class: C, status: s}\n", "work_ledger", "duplicate work item ID(s): A"),
        ("governance/relo-authority-matrix.yaml", None, "source_authority", "read failed (FileNotFoundError)"),
        ("governance/relo-authority-matrix.yaml", "sources: {}\n", "source_authority", "sources must be a list"),
        ("governance/relo-authority-matrix.yaml", "sources:\n  - {mandatory: true, scheduled: {principal_mode: none}}\n", "source_authority", "source 1 needs a non-empty string source"),
        ("governance/relo-authority-matrix.yaml", "sources:\n  - {source: ado, mandatory: true}\n", "source_authority", "source 1 needs scheduled.principal_mode"),
        ("governance/relo-authority-matrix.yaml", "sources:\n  - {source: ado, mandatory: yes-ish, scheduled: {principal_mode: none}}\n", "source_authority", "source 1 needs boolean mandatory"),
        ("governance/relo-authority-matrix.yaml", "sources:\n  - {source: ado, mandatory: true, scheduled: {principal_mode: none}}\n  - {source: ado, mandatory: true, scheduled: {principal_mode: none}}\n", "source_authority", "duplicate source(s): ado"),
        ("governance/nfr-budgets.yaml", None, "budgets", "load failed (ConfigError)"),
        ("governance/nfr-budgets.yaml", "schema_version: '1'\nbudgets:\n  - {id: x, budget_class: integration, direction: ceiling, status: SECRET-VALUE}\n", "budgets", "load failed (ConfigError)"),
        ("spec.md", "## 19. Other\n", "decisions", "no '## 20.' decisions section"),
        ("spec.md", "## 20. Open Decisions\n\nnone\n", "decisions", "no decision rows"),
    ],
)
def test_invalid_input_is_explicitly_unavailable_without_payload(tmp_path, relative, content, section, guidance) -> None:
    root, spec = _root(tmp_path, **{relative: content})
    if content is None:
        (root / relative).unlink(missing_ok=True)

    report = assess_terminal_state(root, spec_path=spec)

    assert report["terminal_state"] is None
    assert report["assessment"] == "not_assessed"
    unavailable = report[section]["unavailable"]
    assert guidance in unavailable
    assert "SECRET-VALUE" not in unavailable
    assert str(tmp_path) not in unavailable
    assert main(["--repo-root", str(root), "--spec", str(spec), "--terminal-state"]) == 1


def test_unreadable_spec_bytes_are_unavailable(tmp_path) -> None:
    root, spec = _root(tmp_path)
    spec.write_bytes(b"\xff\xfe\x00bad")

    report = assess_terminal_state(root, spec_path=spec)

    assert report["decisions"] == {"unavailable": "spec.md: read failed (UnicodeDecodeError)"}


def test_budget_loader_import_failure_is_unavailable(tmp_path, monkeypatch) -> None:
    root, spec = _root(tmp_path)
    monkeypatch.setitem(sys.modules, "src.core.nfr_budgets", None)

    report = assess_terminal_state(root, spec_path=spec)

    assert report["budgets"] == {"unavailable": "governance/nfr-budgets.yaml: loader import failed (ModuleNotFoundError)"}


def test_unexpected_errors_propagate(tmp_path, monkeypatch) -> None:
    root, spec = _root(tmp_path)

    def broken(_text):
        raise RuntimeError("bug")

    monkeypatch.setattr(verify_relo.yaml, "safe_load", broken)

    with pytest.raises(RuntimeError):
        assess_terminal_state(root, spec_path=spec)


def test_valid_inputs_report_observable_counts_without_terminal_state(tmp_path) -> None:
    root, spec = _root(tmp_path)

    report = assess_terminal_state(root, spec_path=spec)

    assert report["terminal_state"] is None
    assert report["assessment"] == "not_assessed"
    ledger = report["work_ledger"]
    assert ledger["by_status"] == {"completed": 1, "in_progress": 1, "planned": 1, "some_future_status": 1}
    assert ledger["by_phase"] == {"RELO-0": {"completed": 1, "in_progress": 1}, "RELO-6": {"planned": 1, "some_future_status": 1}}
    assert ledger["by_class"] == {"AGENT_READY": 2, "NEEDS_JUDGMENT": 2}
    assert ledger["code_complete"] == 1
    assert ledger["is_certification"] is False
    assert ledger["relo6"] == [
        {"id": "RELO-W6.12", "class": "NEEDS_JUDGMENT", "status": "planned"},
        {"id": "RELO-W6.12a", "class": "AGENT_READY", "status": "some_future_status"},
    ]
    assert report["source_authority"] == {"mandatory_sources": ["ado"], "scheduled_principal_modes": {"ado": "none", "icm": "none"}}
    assert report["budgets"] == {"ratified": 1, "unratified": ["candidate-one"]}
    assert report["decisions"] == {"open": ["RELO-D16"], "interim": ["RELO-D2"]}
    assert any(item["spec"] == "§20 RELO-D16" for item in report["missing_inputs"])
    assert any("§13.6a" in item["spec"] for item in report["missing_inputs"])
    assert report["limitations"]


def test_all_cards_completed_and_proposed_header_never_yield_a_terminal_state(tmp_path) -> None:
    ledger = "schema_version: '1'\nwork_items:\n" + "".join(
        f"  - {{id: RELO-W6.{n}, phase: RELO-6, class: AGENT_READY, status: completed}}\n" for n in range(1, 13)
    )
    root, spec = _root(tmp_path, **{"governance/relo-work-packages.yaml": ledger})

    report = assess_terminal_state(root, spec_path=spec)

    assert report["terminal_state"] is None
    assert report["work_ledger"]["code_complete"] == 12
    assert "PROPOSED" not in json.dumps(report)
    assert main(["--repo-root", str(root), "--spec", str(spec), "--terminal-state"]) == 6


def test_cli_text_and_json_match_preserve_bytes_and_skip_private_checks(tmp_path, monkeypatch, capsys) -> None:
    root, spec = _root(tmp_path)
    before = _snapshot(root)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("default verifier check executed")

    monkeypatch.setattr(verify_relo, "verify", forbidden)
    monkeypatch.setattr(verify_relo, "_check_deployment_vars", forbidden)
    monkeypatch.setattr(verify_relo, "_synthetic_count", forbidden)

    assert main(["--repo-root", str(root), "--spec", str(spec), "--terminal-state", "--json"]) == 6
    payload = json.loads(capsys.readouterr().out)
    assert main(["--repo-root", str(root), "--spec", str(spec), "--terminal-state"]) == 6
    text = capsys.readouterr().out

    assert payload == assess_terminal_state(root, spec_path=spec)
    assert text.splitlines()[0] == f"NOT ASSESSED terminal state: {payload['reason']}"
    for key in ("work_ledger", "source_authority", "budgets", "decisions"):
        assert f"{key}: {json.dumps(payload[key], sort_keys=True)}" in text
    for item in payload["missing_inputs"]:
        assert f"MISSING {item['input']} ({item['spec']})" in text
    for limitation in payload["limitations"]:
        assert f"LIMITATION {limitation}" in text
    assert _snapshot(root) == before


def test_default_cli_still_runs_existing_verify(tmp_path, monkeypatch, capsys) -> None:
    root, spec = _root(tmp_path)
    monkeypatch.setattr(verify_relo, "verify", lambda *_a, **_k: (verify_relo.CheckResult("X", True, "ok"),))

    assert main(["--repo-root", str(root), "--spec", str(spec)]) == 0
    assert capsys.readouterr().out == "PASS X: ok\n"


def test_terminal_cli_uses_explicit_budget_registry(tmp_path, capsys) -> None:
    root, spec = _root(tmp_path, **{"governance/nfr-budgets.yaml": None})
    registry = tmp_path / "operator-budgets.yaml"
    registry.write_text(_BUDGETS, encoding="utf-8")

    assert main(["--repo-root", str(root), "--spec", str(spec), "--budgets", str(registry), "--terminal-state", "--json"]) == 6
    assert json.loads(capsys.readouterr().out)["budgets"] == {"ratified": 1, "unratified": ["candidate-one"]}
