from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from scripts import program_inventory
from src.commands import config
from src.core import edition_resolver, workspace_paths
from src.core.exceptions import ConfigError


@pytest.mark.parametrize("content", [
    b"[]", b"false", b"null", b"", b"\xff", b"secret: [PRIVATE-YAML", b"date: 2026-13-01",
])
def test_present_invalid_config_is_not_a_mapping_or_raw_parser_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        config._load_yaml_mapping(path)
    assert "PRIVATE-YAML" not in str(caught.value)
    assert path.read_bytes() == content


@pytest.mark.parametrize("required", [False, True])
def test_schema_assessment_distinguishes_missing_from_failed_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool,
) -> None:
    path = tmp_path / "program.yaml"
    target = config._build_schema_target("program", path, required=required)
    assert config._assess_config_schema(target).status == ("fail" if required else "warn")
    path.write_text("schema_version: '3.0'\n", encoding="utf-8")
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == path:
            raise PermissionError("PRIVATE-ACCESS")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    result = config._assess_config_schema(target)
    assert result.status == "fail"
    assert "PermissionError" in result.detail
    assert "PRIVATE-ACCESS" not in result.detail


def test_schema_assessment_reads_instead_of_trusting_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "program.yaml"
    path.write_text("schema_version: '3.0'\n", encoding="utf-8")
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    assert config._assess_config_schema(config._build_schema_target("program", path)).status == "ok"
    assert config._maybe_load_mapping(path) == {"schema_version": "3.0"}


@pytest.mark.parametrize("version", ["\u00b2.0", "3.\u00b2", "PRIVATE-VERSION", "3.0.0"])
def test_invalid_schema_versions_are_explicit_not_conversion_errors(tmp_path: Path, version: str) -> None:
    path = tmp_path / "program.yaml"
    path.write_text("schema_version: " + json.dumps(version), encoding="utf-8")
    result = config._assess_config_schema(config._build_schema_target("program", path))
    assert result.status == "fail"
    assert "PRIVATE-VERSION" not in result.detail
    assert result.version is None


@pytest.mark.parametrize("identifier", ["..", "../outside", "nested\\child", "Q:outside", "bad\nid", "alias.", "alias ", "name*"])
def test_shared_identifier_validation_rejects_aliases_before_path_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identifier: str,
) -> None:
    assert workspace_paths.is_bare_identifier(identifier) is False
    with monkeypatch.context() as scoped:
        original_stat = Path.stat

        def inspect(self: Path, *, follow_symlinks: bool = True):
            if self.is_relative_to(tmp_path):
                pytest.fail("Invalid selector must fail before inspecting metadata.")
            return original_stat(self, follow_symlinks=follow_symlinks)

        scoped.setattr(Path, "stat", inspect)
        scoped.setattr(Path, "exists", lambda _self: pytest.fail("Invalid selector must fail before probing paths."))
        scoped.setattr(Path, "glob", lambda *args, **kwargs: pytest.fail("Invalid selector must fail before discovery."))
        for operation in (
            lambda: config._load_program_document(identifier, programs_root=tmp_path),
            lambda: config._resolve_schema_targets(edition=None, program=identifier, programs_root=tmp_path),
            lambda: config._resolve_program_schema_migration_edition(edition=None, program=identifier, programs_root=tmp_path),
        ):
            with pytest.raises(typer.BadParameter):
                operation()
        with pytest.raises(ConfigError):
            edition_resolver.find_edition_yaml(identifier, programs_root=tmp_path)
        with pytest.raises(ValueError):
            program_inventory.inventory_program(identifier, tmp_path, root_only=True)


def test_valid_identifiers_and_version_policy_remain_compatible(tmp_path: Path) -> None:
    for identifier in ("fixture", "fixture-weekly", "_templates", "program_2"):
        assert workspace_paths.is_bare_identifier(identifier) is True
    path = tmp_path / "program.yaml"
    for version, expected in (("3.0", "ok"), ("3.1", "warn"), ("2.0", "fail")):
        path.write_text(f"schema_version: '{version}'\n", encoding="utf-8")
        assert config._assess_config_schema(config._build_schema_target("program", path)).status == expected


@pytest.mark.parametrize("output_format", ["json", "human"])
def test_schema_validation_declares_its_limits_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_format: str,
) -> None:
    root = tmp_path / "programs"
    program = root / "fixture"
    program.mkdir(parents=True)
    path = program / "program.yaml"
    path.write_bytes(b"schema_version: '3.0'\nunknown_content: not-semantically-validated\n")
    before = path.read_bytes()
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(config.app, ["validate", "--program", "fixture", "--format", output_format])
    assert result.exit_code == 0, result.output + repr(result.exception)
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["validation_scope"] == "schema_versions_kb_and_metrics"
        assert payload["content_validated"] is False
        assert payload["references_validated"] is False
    else:
        assert "schema versions, existing KB checks and offline metric bindings" in result.stdout
        assert "references have not been validated" in result.stdout
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["get", "set", "validate-edition", "validate-edition-date"])
def test_config_cli_read_failures_are_safe_stderr_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    root = tmp_path / "programs"
    program = root / "fixture"
    program.mkdir(parents=True)
    path = program / "program.yaml"
    path.write_bytes(b"secret: [PRIVATE-CONTENT")
    monkeypatch.setattr(config, "PROGRAMS_ROOT", root)
    if command.startswith("validate-edition"):
        edition = program / "editions" / "weekly.yaml"
        edition.parent.mkdir()
        edition.write_bytes(
            b"id: weekly\nprogram_id: fixture\ndate: 2026-13-01" if command.endswith("-date")
            else b"id: weekly\nprogram_id: fixture\nsecret: [PRIVATE-CONTENT"
        )
        arguments = ["validate", "--edition", "weekly"]
    elif command == "get":
        arguments = ["get", "catchup.enabled", "--program", "fixture"]
    else:
        arguments = ["set", "catchup.enabled", "false", "--program", "fixture"]
    result = CliRunner().invoke(config.app, arguments)
    assert result.exit_code == 2
    assert result.stderr
    assert not result.stdout
    assert "PRIVATE-CONTENT" not in result.stderr
    assert path.read_bytes() == b"secret: [PRIVATE-CONTENT"


@pytest.mark.parametrize("value", [None, "", 3, False])
def test_missing_or_nonstring_schema_version_is_a_failure(tmp_path: Path, value: object) -> None:
    path = tmp_path / "program.yaml"
    path.write_text(json.dumps({"schema_version": value}), encoding="utf-8")
    result = config._assess_config_schema(config._build_schema_target("program", path))
    assert result.status == "fail"
    assert result.version is None


def test_optional_mapping_preserves_empty_objects_and_only_missing_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "optional.yaml"
    assert config._maybe_load_mapping(path) is None
    path.write_text("{}", encoding="utf-8")
    assert config._maybe_load_mapping(path) == {}
    path.write_text("null", encoding="utf-8")
    with pytest.raises(ConfigError):
        config._maybe_load_mapping(path)


def test_required_program_absence_has_a_cli_error(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter, match="missing program.yaml"):
        config._load_program_document("fixture", programs_root=tmp_path)


@pytest.mark.parametrize("condition", ["missing", "file", "inaccessible"])
def test_schema_target_directory_failures_are_not_unknown_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str,
) -> None:
    path = tmp_path / "fixture"
    if condition == "file":
        path.write_text("not a directory", encoding="utf-8")
    if condition == "inaccessible":
        original = Path.stat

        def inspect(self: Path, *, follow_symlinks: bool = True):
            if self == path:
                raise PermissionError("PRIVATE-ACCESS")
            return original(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", inspect)
    with pytest.raises(typer.BadParameter) as caught:
        config._resolve_schema_targets(edition=None, program="fixture", programs_root=tmp_path)
    assert "PRIVATE-ACCESS" not in str(caught.value)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_unexpected_reader_errors_are_not_disguised_as_yaml_or_optional_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    def read_text(self: Path, encoding: str | None = None) -> str:
        raise error_type("unexpected implementation failure")

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(error_type, match="unexpected implementation"):
        config._maybe_load_mapping(tmp_path / "optional.yaml")


@pytest.mark.parametrize("count", [0, 1, 2])
def test_migration_selector_preserves_single_edition_inference(tmp_path: Path, count: int) -> None:
    editions = tmp_path / "fixture" / "editions"
    editions.mkdir(parents=True)
    for index in range(count):
        (editions / f"weekly_{index}.yaml").write_text("{}", encoding="utf-8")
    if count == 1:
        assert config._resolve_program_schema_migration_edition(
            edition=None, program="fixture", programs_root=tmp_path,
        ) == "weekly_0"
    else:
        with pytest.raises(typer.BadParameter, match="no edition" if count == 0 else "multiple editions"):
            config._resolve_program_schema_migration_edition(
                edition=None, program="fixture", programs_root=tmp_path,
            )
    assert config._resolve_program_schema_migration_edition(
        edition=" chosen ", program=None, programs_root=tmp_path,
    ) == "chosen"


@pytest.mark.parametrize("edition,program", [(None, None), ("weekly", "fixture"), ("weekly", None)])
def test_schema_target_selection_requires_one_resolvable_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edition: str | None, program: str | None,
) -> None:
    monkeypatch.setattr(config, "resolve_edition_paths", lambda *args, **kwargs: None)
    with pytest.raises(typer.BadParameter):
        config._resolve_schema_targets(edition=edition, program=program, programs_root=tmp_path)
