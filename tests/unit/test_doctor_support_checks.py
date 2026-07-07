from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands.doctor_checks.doctor_support_checks import (
    agency_cli_check,
    load_milestone_owner_aliases,
    mail_preview_check,
    template_check,
)
from src.core.exceptions import ConfigError
from src.m365.agency_bridge import AgencyCapabilities


def test_mail_preview_check_warns_when_graph_env_missing() -> None:
    check = mail_preview_check(environ={}, find_spec_fn=lambda _name: object())

    assert check.status == "warn"
    assert "GRAPH_TENANT_ID" in check.detail


def test_agency_cli_check_lists_detected_servers() -> None:
    check = agency_cli_check(
        AgencyCapabilities(
            available=True,
            has_workiq=True,
            has_ado=True,
            tier="msft",
        )
    )

    assert check.status == "ok"
    assert "workiq, ado" in check.detail


def test_template_check_reports_present_base_and_partials(tmp_path: Path) -> None:
    templates_root = tmp_path / "templates"
    partials_dir = templates_root / "partials"
    partials_dir.mkdir(parents=True)
    (templates_root / "base.email.j2").write_text("base", encoding="utf-8")
    (partials_dir / "card.j2").write_text("partial", encoding="utf-8")

    check = template_check(templates_root)

    assert check.status == "ok"
    assert check.detail == "base.email.j2 + 1 partials present"


def test_load_milestone_owner_aliases_reads_shared_people_directory(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    shared_people_path = tmp_path / "knowledge" / "people_directory.yaml"
    shared_people_path.parent.mkdir(parents=True)
    shared_people_path.write_text(
        'schema_version: "1.0"\npeople:\n  - alias: beta\n  - alias: alpha\n',
        encoding="utf-8",
    )

    assert load_milestone_owner_aliases("demo", programs_root=programs_root) == ("alpha", "beta")


def test_load_milestone_owner_aliases_rejects_non_mapping_people_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    shared_people_path = tmp_path / "knowledge" / "people_directory.yaml"
    shared_people_path.parent.mkdir(parents=True)
    shared_people_path.write_text(
        'schema_version: "1.0"\npeople:\n  - not-a-mapping\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be a mapping"):
        load_milestone_owner_aliases("demo", programs_root=programs_root)
