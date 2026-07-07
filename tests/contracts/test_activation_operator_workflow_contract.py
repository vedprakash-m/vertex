"""Activation operator workflow contract.

This is the executable scaffold for activation.md §6.15.6. The real AG-1 /
AG-10 proof still requires live XPF EMLs and a real report render, but the
platform must keep the operator path intact:

export EML -> gather/REV -> triage edit/approve -> report -> revoke -> re-report.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_activation_workflow_commands_cover_export_eml_to_rev_to_report_revoke() -> None:
    """The activation path keeps every operator step reachable and named."""
    rev_command = _read("src/commands/rev.py")
    ledger_command = _read("src/commands/ledger.py")
    report_command = _read("src/commands/report.py")

    assert "README.md" in rev_command and "export-import workflow" in rev_command
    assert '@triage_app.command("edit")' in ledger_command
    assert '@triage_app.command("approve")' in ledger_command
    assert '@triage_app.command("revoke")' in ledger_command
    assert "operator.correction.v1" in ledger_command
    assert "ProgramReality" in report_command


def test_activation_workflow_keeps_explain_lineage_and_rerender_contracts() -> None:
    """Triage and render surfaces preserve source/approval evidence."""
    ledger_command = _read("src/commands/ledger.py")
    report_deck_command = _read("src/commands/report_deck.py")
    milestone_template = _read("templates/partials/milestone_rows.j2")
    deck_template = _read("templates/archetypes/deck.j2")

    assert "why:" in ledger_command
    assert "extraction_rationale" in ledger_command
    assert "source_document_key" in ledger_command
    assert "approval_event_id" in report_deck_command
    assert "source_document_key" in report_deck_command
    assert "row.source_document_key" in milestone_template
    assert "row.source_document_key" in deck_template
    assert "row.approval_event_id" in deck_template
