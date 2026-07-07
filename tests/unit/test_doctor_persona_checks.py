from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from src.commands.doctor import run_doctor
from src.commands.doctor_checks.persona_checks import run_persona_doctor


def _write_program(tmp_path: Path, program_id: str = "demo") -> tuple[Path, Path, Path]:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    reports_root = tmp_path / "reports"
    program_dir = programs_root / program_id
    (program_dir / "knowledge").mkdir(parents=True, exist_ok=True)
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text('schema_version: "2.0"\nid: demo\nname: Demo\n', encoding="utf-8")
    editions_root.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)
    return programs_root, editions_root, reports_root


def test_run_persona_doctor_warns_when_personas_yaml_missing(tmp_path: Path) -> None:
    programs_root, editions_root, reports_root = _write_program(tmp_path)

    report = run_persona_doctor(
        edition_name=None,
        programs_root=programs_root,
        editions_root=editions_root,
        reports_root=reports_root,
    )

    assert report.edition == "demo"
    assert len(report.checks) == 1
    assert report.checks[0].status == "warn"
    assert report.checks[0].detail == "No personas.yaml found — persona enforcement inactive."


def test_run_persona_doctor_appends_context_gap_for_stale_critical_check(monkeypatch, tmp_path: Path) -> None:
    programs_root, editions_root, reports_root = _write_program(tmp_path)
    personas_path = programs_root / "demo" / "knowledge" / "personas.yaml"
    personas_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "enforcement": {"mode": "enforce", "staleness_threshold_days": 30},
                "personas": [
                    {
                        "id": "owner_guard",
                        "priority": "critical",
                        "checks": [
                            {"id": "check_one", "type": "keyword_present", "scope": "summary", "updated_at": "2026-01-01"},
                            {"id": "check_two", "type": "keyword_present", "scope": "summary", "updated_at": "2026-01-01"},
                            {"id": "check_thr", "type": "keyword_present", "scope": "summary", "updated_at": "2026-01-01"},
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    captured: list[dict[str, object]] = []

    monkeypatch.setattr("src.commands.doctor_checks.persona_checks.find_spec", lambda _name: object())
    monkeypatch.setattr(
        "src.commands.doctor_checks.persona_checks.load_bundle",
        lambda *args, **kwargs: SimpleNamespace(editorial_rules=SimpleNamespace(structural_rules=[])),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.persona_checks.append_context_gap",
        lambda **kwargs: captured.append(kwargs),
    )

    report = run_persona_doctor(
        edition_name=None,
        programs_root=programs_root,
        editions_root=editions_root,
        reports_root=reports_root,
    )

    assert any("days stale" in check.detail for check in report.checks)
    assert captured
    assert captured[0]["program"] == "demo"
    assert captured[0]["field"] == "personas.check.updated_at"


def test_run_persona_doctor_requires_stays_within_same_persona(monkeypatch, tmp_path: Path) -> None:
    programs_root, editions_root, reports_root = _write_program(tmp_path)
    personas_path = programs_root / "demo" / "knowledge" / "personas.yaml"
    personas_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "enforcement": {"mode": "enforce", "staleness_threshold_days": 90},
                "personas": [
                    {
                        "id": "alpha_owner",
                        "priority": "normal",
                        "checks": [
                            {"id": "alpha_chk", "type": "keyword_present", "scope": "summary", "requires": ["beta_chk"]},
                        ],
                    },
                    {
                        "id": "beta_owner",
                        "priority": "normal",
                        "checks": [
                            {"id": "beta_chk", "type": "keyword_present", "scope": "summary"},
                        ],
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.doctor_checks.persona_checks.find_spec", lambda _name: object())
    monkeypatch.setattr(
        "src.commands.doctor_checks.persona_checks.load_bundle",
        lambda *args, **kwargs: SimpleNamespace(editorial_rules=SimpleNamespace(structural_rules=[])),
    )

    report = run_persona_doctor(
        edition_name=None,
        programs_root=programs_root,
        editions_root=editions_root,
        reports_root=reports_root,
    )

    assert any(
        check.status == "fail" and "requires 'beta_chk' not found in same persona" in check.detail
        for check in report.checks
    )


def test_run_doctor_personas_uses_extracted_persona_module(monkeypatch, tmp_path: Path) -> None:
    programs_root, editions_root, reports_root = _write_program(tmp_path)
    monkeypatch.setattr(
        "src.commands.doctor_checks.persona_checks.resolve_edition",
        lambda edition_name, **kwargs: SimpleNamespace(paths=SimpleNamespace(program_id="demo")),
    )

    report = run_doctor(
        personas=True,
        edition_name="demo_weekly",
        programs_root=programs_root,
        editions_root=editions_root,
        reports_root=reports_root,
    )

    assert report.edition == "demo"
    assert report.checks[0].detail == "No personas.yaml found — persona enforcement inactive."
