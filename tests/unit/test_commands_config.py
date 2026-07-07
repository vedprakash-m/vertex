from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.core.claim_extraction_calibration_store import ClaimExtractionCalibrationRecord, append_claim_extraction_calibration_record


runner = CliRunner()


def test_config_get_reports_default_for_missing_key(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["config", "get", "catchup.enabled", "--program", "acme"])

    assert result.exit_code == 0
    assert "acme catchup.enabled = true (default; programs/acme/program.yaml)" in result.stdout


def test_config_set_persists_allowed_value_and_upgrades_schema(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["config", "set", "catchup.enabled", "false", "--program", "acme"])

    assert result.exit_code == 0
    assert "Updated acme catchup.enabled from true to false in programs/acme/program.yaml." in result.stdout
    payload = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "3.0"
    assert payload["catchup"]["enabled"] is False


def test_config_set_dry_run_skips_write(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        ["config", "set", "readiness.snapshot_max_age_days", "14", "--program", "acme", "--dry-run"],
    )

    assert result.exit_code == 0
    assert (
        "Dry-run: would set acme readiness.snapshot_max_age_days from 7 to 14 in programs/acme/program.yaml."
        in result.stdout
    )
    payload = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2.0"
    assert "readiness" not in payload


def test_config_set_rejects_invalid_values(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["config", "set", "salience.min_weight", "1.5", "--program", "acme"])

    assert result.exit_code == 2
    assert "Invalid value for salience.min_weight" in result.output
    payload = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2.0"
    assert "salience" not in payload


def test_config_set_blocks_claim_extractor_promotion(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["config", "set", "ai.claim_extractor.mode", "production", "--program", "acme"])

    assert result.exit_code != 0
    assert "0/20 calibration confirm(s) recorded" in result.output


def test_config_set_allows_claim_extractor_promotion_when_trust_gates_pass(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)
    for issue_number in range(1, 21):
        append_claim_extraction_calibration_record(
            ClaimExtractionCalibrationRecord(
                program_id="acme",
                issue_number=issue_number,
                recorded_at=_recorded_at(issue_number),
                mode="calibration",
                ai_claim_count=10,
                regex_claim_count=9,
                shared_claim_count=9,
                ai_only_count=1,
                regex_only_count=0,
                agreement_rate=0.9,
            ),
            programs_root=programs_root,
        )

    result = runner.invoke(
        app,
        ["config", "set", "ai.claim_extractor.mode", "production", "--program", "acme"],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "Claim extraction production promotion check:" in result.output
    assert "Recent agreement (last 10 cycles): 90%" in result.output
    assert "Average disagreement count (last 10 cycles): 1.00" in result.output
    payload = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert payload["ai"]["claim_extractor"]["mode"] == "production"


def test_config_set_blocks_claim_extractor_promotion_when_disagreement_pressure_is_high(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)
    for issue_number in range(1, 21):
        append_claim_extraction_calibration_record(
            ClaimExtractionCalibrationRecord(
                program_id="acme",
                issue_number=issue_number,
                recorded_at=_recorded_at(issue_number),
                mode="calibration",
                ai_claim_count=12,
                regex_claim_count=12,
                shared_claim_count=9,
                ai_only_count=2,
                regex_only_count=1,
                agreement_rate=0.9,
            ),
            programs_root=programs_root,
        )

    result = runner.invoke(app, ["config", "set", "ai.claim_extractor.mode", "production", "--program", "acme"])

    assert result.exit_code != 0
    normalized_output = " ".join(result.output.split())
    assert "disagreement" in normalized_output
    assert "current=3.00" in normalized_output


def test_config_validate_reports_schema_drift(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    _seed_edition_and_contracts(programs_root, edition_schema_version="2.1", readiness_schema_version="1.2")
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["config", "validate", "--edition", "acme_weekly", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["program"] == "acme"
    assessments = {entry["name"]: entry for entry in payload["assessments"]}
    assert assessments["edition"]["status"] == "warn"
    assert assessments["edition"]["version"] == "2.1"
    assert assessments["program"]["status"] == "fail"
    assert assessments["program"]["version"] == "2.0"
    assert assessments["readiness"]["status"] == "warn"
    assert assessments["readiness"]["version"] == "1.2"


def test_config_migrate_dry_run_reports_changes_without_writing(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    _seed_edition_and_contracts(programs_root, edition_schema_version="2.1", readiness_schema_version="1.2")
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    calls: list[str] = []

    def _fake_run_onboard_migrate_v3(edition_name: str, reports_root: Path | None = None):
        calls.append(edition_name)
        return None

    monkeypatch.setattr("src.commands.config.run_onboard_migrate_v3", _fake_run_onboard_migrate_v3)

    result = runner.invoke(app, ["config", "migrate", "--edition", "acme_weekly", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry-run: would perform the following config schema migrations:" in result.stdout
    assert "run onboard V3 migration for acme_weekly" in result.stdout
    assert "normalize edition schema_version to 2.0" in result.stdout
    assert "normalize readiness schema_version to 1.0" in result.stdout
    assert calls == []
    program_doc = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    readiness_doc = yaml.safe_load((programs_root / "acme" / "readiness.yaml").read_text(encoding="utf-8"))
    edition_doc = yaml.safe_load((programs_root.parent / "editions" / "acme_weekly.yaml").read_text(encoding="utf-8"))
    assert program_doc["schema_version"] == "2.0"
    assert readiness_doc["schema_version"] == "1.2"
    assert edition_doc["schema_version"] == "2.1"


def test_config_migrate_applies_program_and_minor_schema_updates(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program(tmp_path)
    _seed_edition_and_contracts(programs_root, edition_schema_version="2.1", readiness_schema_version="1.2")
    monkeypatch.setattr("src.commands.config.PROGRAMS_ROOT", programs_root)

    calls: list[tuple[str, Path | None]] = []

    def _fake_run_onboard_migrate_v3(edition_name: str, reports_root: Path | None = None):
        calls.append((edition_name, reports_root))
        program_path = programs_root / "acme" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["schema_version"] = "3.0"
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")
        return None

    monkeypatch.setattr("src.commands.config.run_onboard_migrate_v3", _fake_run_onboard_migrate_v3)

    result = runner.invoke(app, ["config", "migrate", "--edition", "acme_weekly"])

    assert result.exit_code == 0
    assert "Applied config schema migrations:" in result.stdout
    assert calls == [("acme_weekly", programs_root.parent / "reports")]
    program_doc = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    readiness_doc = yaml.safe_load((programs_root / "acme" / "readiness.yaml").read_text(encoding="utf-8"))
    edition_doc = yaml.safe_load((programs_root.parent / "editions" / "acme_weekly.yaml").read_text(encoding="utf-8"))
    assert program_doc["schema_version"] == "3.0"
    assert readiness_doc["schema_version"] == "1.0"
    assert edition_doc["schema_version"] == "2.0"

    validate_result = runner.invoke(app, ["config", "validate", "--edition", "acme_weekly", "--format", "json"])
    assert validate_result.exit_code == 0
    payload = json.loads(validate_result.stdout)
    assert payload["status"] == "ok"


def _seed_program(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '2.0'",
                "id: acme",
                "name: Acme",
                "maturity_level: 2",
                "storage_backend: file",
                "ado:",
                "  organization: example",
                "  project: Example",
                "  area_paths: []",
                "  work_item_types: []",
                "  excluded_states: []",
                "  date_window_days: 14",
                "ai:",
                "  enabled: false",
                "  budget_usd_per_run: 0.0",
                "  blurb_deployment: null",
                "  blurb_backup_deployment: null",
                "  exec_summary_deployment: null",
                "  exec_summary_backup_deployment: null",
                "  temperature: null",
                "kusto:",
                "  enabled: false",
                "  queries: []",
                "m365:",
                "  enabled: false",
                "  prefer_agency: false",
                "  workiq:",
                "    newsletter_search: null",
                "    feedback_search: null",
                "    teams_search: null",
                "  bluebird:",
                "    teams_channels: []",
                "    lookback_days: 7",
                "  offline:",
                "    newsletter_dir: null",
                "    transcript_dir: null",
            )
        ),
        encoding="utf-8",
    )
    return programs_root


def _seed_edition_and_contracts(
    programs_root: Path,
    *,
    edition_schema_version: str,
    readiness_schema_version: str,
) -> None:
    editions_root = programs_root.parent / "editions"
    editions_root.mkdir(parents=True, exist_ok=True)
    (editions_root / "acme_weekly.yaml").write_text(
        "\n".join(
            (
                f"schema_version: '{edition_schema_version}'",
                "id: acme_weekly",
                "program_id: acme",
                "name: Acme Weekly",
                "type: detailed",
                "altitude: helicopter",
                "cadence: weekly",
            )
        ),
        encoding="utf-8",
    )
    program_dir = programs_root / "acme"
    (program_dir / "readiness.yaml").write_text(
        "\n".join(
            (
                f"schema_version: '{readiness_schema_version}'",
                "snapshot_max_age_days: 7",
                "dimensions: {}",
            )
        ),
        encoding="utf-8",
    )
    for filename in ("template_contract.yaml", "slice_contracts.yaml", "chapter_contract.yaml"):
        (program_dir / filename).write_text("schema_version: '1.0'\n", encoding="utf-8")


def _recorded_at(issue_number: int) -> datetime:
    day = min(issue_number, 28)
    return datetime(2026, 5, day, 10, 0, tzinfo=timezone.utc)
