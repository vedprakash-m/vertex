from __future__ import annotations
from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck
from src.commands.doctor_checks.kb_checks import knowledge_predicate_registry_check, run_kb_doctor


def test_run_kb_doctor_fails_when_programs_and_editions_are_missing(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    labels = [check.label for check in report.checks]
    assert "Knowledge" in labels
    assert "Editions" in labels
    assert "Saved Queries" in labels


def test_knowledge_predicate_registry_check_warns_when_threshold_exceeded(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.doctor_checks.kb_checks.predicate_count", lambda: 101)

    check = knowledge_predicate_registry_check()

    assert check.label == "Knowledge Predicates"
    assert check.status == "warn"
    assert "exceeds the review threshold 100" in check.detail
