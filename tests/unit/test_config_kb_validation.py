from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from src.commands import config
from src.core import kb_updates
from src.core.exceptions import ConfigError


def _seed(root: Path) -> Path:
    program = root / "fixture"
    (program / "knowledge").mkdir(parents=True)
    (program / "program.yaml").write_text(
        "schema_version: '3.0'\nid: fixture\nname: Fixture\n", encoding="utf-8",
    )
    (program / "knowledge" / "people_directory.yaml").write_text(
        "schema_version: '1.0'\npeople:\n  - alias: alice\n", encoding="utf-8",
    )
    (program / "workstreams.yaml").write_text(
        "schema_version: '2.0'\nworkstreams:\n  - id: delivery\n    name: Delivery\n    pm_owner: alice\n"
        "    raci:\n      accountable: alice\n", encoding="utf-8",
    )
    return program


@pytest.mark.parametrize("document,content", [
    ("workstreams.yaml", "schema_version: '2.0'\nworkstreams:\n  - id: delivery\n    name: Delivery\n    raci:\n      accountable: unknown\n"),
    ("scorecards.yaml", "schema_version: '2.0'\nscorecards:\n  - name: Status\n    dimensions:\n      - name: Scope\n        workstream_id: unknown\n"),
    ("knowledge/golden_queries.yaml", "schema_version: '1.0'\nqueries:\n  - id: metric\n    workstream_ids: [unknown]\n"),
    ("workstream_registry.yaml", "[]"),
    ("workstream_registry.yaml", "workstreams: false"),
    ("workstream_registry.yaml", "workstreams: [false]"),
    ("knowledge/people_directory.yaml", "schema_version: '1.0'\npeople: [false]"),
    ("knowledge/people_directory.yaml", "people: []"),
    ("knowledge/people_directory.yaml", "schema_version: '9.0'\npeople: []"),
    ("knowledge/people_directory.yaml", "schema_version: '1.0'\npeople: false"),
    ("knowledge/people_directory.yaml", "secret: [PRIVATE-CONTENT"),
    ("workstreams.yaml", "date: 2026-13-01"),
])
def test_config_validation_fails_for_bad_kb_not_just_schema_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: str, content: str,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    (program / document).write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 1, result.output + repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["kb_validation"]["status"] == "fail"
    assert "PRIVATE-CONTENT" not in result.stdout + result.stderr


@pytest.mark.parametrize("selector", ["program", "edition"])
def test_config_validation_exposes_bounded_scope_and_makes_no_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    if selector == "edition":
        (program / "editions").mkdir()
        (program / "editions" / "weekly.yaml").write_text(
            "schema_version: '2.0'\nid: weekly\nprogram_id: fixture\n", encoding="utf-8",
        )
    before = {path: path.read_bytes() for path in program.rglob("*") if path.is_file()}
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    with monkeypatch.context() as scoped:
        scoped.setattr(kb_updates, "TemporaryDirectory", lambda *args, **kwargs: pytest.fail("No temp copies."))
        scoped.setattr(Path, "write_text", lambda *args, **kwargs: pytest.fail("No validation writes."))
        result = CliRunner().invoke(
            config.app, ["validate", f"--{selector}", "fixture" if selector == "program" else "weekly", "--format", "json"],
        )
    assert result.exit_code == 0, result.output + repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["validation_scope"] == "schema_versions_kb_and_metrics"
    assert payload["kb_validation"]["status"] == "ok"
    assert "workstreams.yaml" in payload["kb_validation"]["scope"]
    assert "program.yaml" in payload["kb_validation"]["scope"]
    assert "workstream_registry.yaml" in payload["kb_validation"]["scope"]
    assert payload["content_validated"] is False
    assert payload["references_validated"] is False
    assert {path: path.read_bytes() for path in program.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("invalid_reference", [
    None, "milestone-owner", "milestone-workstream",
    "dependency-workstream", "dependency-milestone",
])
def test_config_validation_checks_authored_context_references_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_reference: str | None,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    workstreams = program / "workstreams.yaml"
    workstreams.write_text(
        workstreams.read_text(encoding="utf-8")
        + "  - id: release\n    name: Release\n    pm_owner: alice\n"
        "    raci:\n      accountable: alice\n",
        encoding="utf-8",
    )
    owner = "unknown" if invalid_reference == "milestone-owner" else "alice"
    linked = "unknown" if invalid_reference == "milestone-workstream" else "delivery"
    destination = "unknown" if invalid_reference == "dependency-workstream" else "release"
    milestone = "unknown" if invalid_reference == "dependency-milestone" else "launch"
    (program / "milestones.yaml").write_text(
        "schema_version: '1.0'\nmilestones:\n  - id: launch\n    name: Launch\n"
        f"    target_date: '2026-12-01'\n    owner_alias: {owner}\n"
        f"    status: on_track\n    linked_workstream_ids: [{linked}]\n",
        encoding="utf-8",
    )
    (program / "dependencies.yaml").write_text(
        "schema_version: '1.0'\ndependencies:\n  - id: handoff\n"
        "    from_workstream_id: delivery\n"
        f"    to_workstream_id: {destination}\n    from_milestone_id: {milestone}\n"
        "    dependency_type: blocks\n    risk_if_broken: Release blocked\n    status: active\n",
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in program.rglob("*") if path.is_file()}
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "write_text", lambda *args, **kwargs: pytest.fail("No validation writes."))
        result = CliRunner().invoke(
            config.app, ["validate", "--program", "fixture", "--format", "json"],
        )
    assert result.exit_code == (0 if invalid_reference is None else 1), result.output + repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["kb_validation"]["status"] == ("ok" if invalid_reference is None else "fail")
    assert {"milestones.yaml", "dependencies.yaml"} <= set(payload["kb_validation"]["scope"])
    assert payload["content_validated"] is False
    assert payload["references_validated"] is False
    assert {path: path.read_bytes() for path in program.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("operation", ["get", "validate", "migrate-dry-run", "set-dry-run"])
def test_installed_config_bypasses_unrelated_startup_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    cli = importlib.import_module("cli")
    root = tmp_path / "programs"
    program = _seed(root)
    (program / "editions").mkdir()
    (program / "editions" / "weekly.yaml").write_text("schema_version: '2.0'\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    hooks = [Mock(side_effect=AssertionError("Unrelated startup effect")) for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    arguments = {
        "get": ["get", "catchup.enabled"],
        "validate": ["validate"],
        "migrate-dry-run": ["migrate", "--dry-run"],
        "set-dry-run": ["set", "catchup.enabled", "false", "--dry-run"],
    }[operation]
    result = CliRunner().invoke(cli.app, ["config", *arguments, "--program", "fixture"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    for hook in hooks:
        hook.assert_not_called()


def test_optional_kb_failed_access_is_not_defaulted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = kb_updates._SUPPORTED_DOCUMENTS["knowledge/people_directory.yaml"]
    path = tmp_path / "people_directory.yaml"
    path.write_text("schema_version: '1.0'\npeople: []", encoding="utf-8")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert kb_updates._read_yaml_or_default(path, spec)["people"] == []
    with monkeypatch.context() as scoped:
        def denied(self: Path, encoding: str | None = None) -> str:
            raise PermissionError("PRIVATE-ACCESS")
        scoped.setattr(Path, "read_text", denied)
        with pytest.raises(ConfigError) as caught:
            kb_updates._read_yaml_or_default(path, spec)
    assert "PRIVATE-ACCESS" not in str(caught.value)


@pytest.mark.parametrize("error", [
    PermissionError("PRIVATE-ACCESS"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "PRIVATE-ENCODING"),
    ValueError("PRIVATE-VALUE"),
])
def test_expected_legacy_validator_errors_are_failed_safe_assessments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    root = tmp_path / "programs"
    _seed(root)
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(config, "validate_program_kb", Mock(side_effect=error))
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["kb_validation"]["status"] == "fail"
    assert "PRIVATE-" not in result.stdout + result.stderr


def test_unexpected_validator_implementation_errors_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    _seed(root)
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(config, "validate_program_kb", Mock(side_effect=RuntimeError("unexpected validator error")))
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture"])
    assert isinstance(result.exception, RuntimeError)
    assert not result.stdout


def test_kb_validation_reads_encrypted_profiles_without_rewriting_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import profile_encryption

    root = tmp_path / "programs"
    program = _seed(root)
    path = program / "knowledge" / "people_profiles.yaml"
    path.write_text("schema_version: '1.0'\nprofiles:\n  - alias: alice\n", encoding="utf-8")
    keys: dict[tuple[str, str], str] = {}
    keyring = Mock()
    keyring.get_password.side_effect = lambda service, user: keys.get((service, user))
    keyring.set_password.side_effect = lambda service, user, value: keys.__setitem__((service, user), value)
    monkeypatch.setattr(profile_encryption, "_get_keyring_backend", lambda: keyring)
    profile_encryption.encrypt_people_profiles_file(path)
    before = path.read_bytes()
    with monkeypatch.context() as scoped:
        scoped.setattr(kb_updates, "TemporaryDirectory", lambda *args, **kwargs: pytest.fail("No temp copies."))
        scoped.setattr(Path, "write_text", lambda *args, **kwargs: pytest.fail("No validation writes."))
        knowledge = kb_updates.validate_program_kb("fixture", programs_root=root)
    assert knowledge.people_profiles[0].alias == "alice"
    assert path.read_bytes() == before


def test_profile_loader_failure_does_not_expose_raw_private_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "people_profiles.yaml"
    path.write_text("schema_version: '1.0'\nprofiles: []", encoding="utf-8")
    monkeypatch.setattr(kb_updates, "load_people_profiles_document", Mock(side_effect=ConfigError("PRIVATE-PAYLOAD")))
    with pytest.raises(ConfigError) as caught:
        kb_updates._read_yaml_or_default(path, kb_updates._SUPPORTED_DOCUMENTS["knowledge/people_profiles.yaml"])
    assert "Cannot load profiles" in str(caught.value)
    assert "PRIVATE-PAYLOAD" not in str(caught.value)


@pytest.mark.parametrize("name,collection,loader", [
    ("people_directory.yaml", "people", "load_people_directory"),
    ("teams.yaml", "teams", "load_teams"),
])
def test_schema2_bridge_failure_is_not_an_optional_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, collection: str, loader: str,
) -> None:
    path = tmp_path / name
    path.write_text(f"schema_version: '2.0'\n{collection}: []", encoding="utf-8")
    monkeypatch.setattr(kb_updates, loader, Mock(return_value=None))
    with pytest.raises(ConfigError, match="Could not load shared"):
        kb_updates._read_yaml_or_default(path, kb_updates._SUPPORTED_DOCUMENTS[f"knowledge/{name}"])


def test_invalid_output_format_fails_before_reading_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PROGRAMS_ROOT", tmp_path / "absent")
    validator = Mock(side_effect=AssertionError("Invalid format must precede validation."))
    monkeypatch.setattr(config, "validate_program_kb", validator)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", "xml"])
    assert result.exit_code == 2
    assert result.stderr
    assert not result.stdout
    validator.assert_not_called()
