from __future__ import annotations

from pathlib import Path

import pytest
from typer import BadParameter

from src.commands.confirm_stages.draft_loaders import load_optional_yaml_mapping, load_readiness_gate_settings


def _settings(root: Path) -> tuple[bool, int | None]:
    return load_readiness_gate_settings(
        edition_name="fixture", program_id="sample",
        editions_root=root / "editions", programs_root=root / "programs",
    )


def _write(root: Path, source: str, content: bytes) -> Path:
    path = (
        root / "programs" / "sample" / "program.yaml" if source == "program"
        else root / "editions" / "fixture.yaml"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("source", ["program", "edition"])
@pytest.mark.parametrize("content", [
    b"gate: [PRIVATE-YAML", b"\xff", b"[]", b"false", b"null", b"", b"date: 2026-13-01",
])
def test_invalid_config_never_looks_like_disabled_readiness(tmp_path: Path, source: str, content: bytes) -> None:
    path = _write(tmp_path, source, content)
    with pytest.raises(BadParameter) as caught:
        _settings(tmp_path)
    assert "PRIVATE-YAML" not in str(caught.value)
    assert path.read_bytes() == content


@pytest.mark.parametrize("source", ["program", "edition"])
def test_read_errors_cannot_select_defaults_or_another_edition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    path = _write(tmp_path, source, b"{}")
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == path:
            raise PermissionError("PRIVATE-ACCESS")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(BadParameter) as caught:
        _settings(tmp_path)
    assert "PermissionError" in str(caught.value)
    assert "PRIVATE-ACCESS" not in str(caught.value)


@pytest.mark.parametrize("source", ["program", "edition"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_unexpected_configuration_reader_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, error_type: type[Exception],
) -> None:
    path = _write(tmp_path, source, b"{}")
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == path:
            raise error_type("unexpected implementation failure")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(error_type, match="unexpected implementation"):
        _settings(tmp_path)


@pytest.mark.parametrize("source", ["program", "edition"])
def test_config_reads_do_not_rely_on_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    path = _write(tmp_path, source, b"readiness:\n  gate: true\n" if source == "program" else b"readiness_gate: true\n")
    original = Path.exists

    def exists(self: Path) -> bool:
        return False if self == path else original(self)

    monkeypatch.setattr(Path, "exists", exists)
    assert _settings(tmp_path) == (True, None)


@pytest.mark.parametrize("setting", [
    b"readiness: []", b"readiness: false", b"readiness:\n  gate: 'false'",
    b"readiness:\n  gate: 0", b"readiness:\n  snapshot_max_age_days: invalid",
    b"readiness:\n  snapshot_max_age_days: true",
    b'readiness:\n  snapshot_max_age_days: "\\u00b2"',
])
def test_present_invalid_program_settings_are_not_optional(tmp_path: Path, setting: bytes) -> None:
    _write(tmp_path, "program", setting)
    with pytest.raises(BadParameter):
        _settings(tmp_path)


@pytest.mark.parametrize("setting", [
    b"readiness_gate: 'false'", b"readiness_gate: 1",
    b"readiness_snapshot_max_age_days: invalid", b"readiness_snapshot_max_age_days: true",
    b'readiness_snapshot_max_age_days: "\\u00b2"',
])
def test_present_invalid_edition_settings_are_not_optional(tmp_path: Path, setting: bytes) -> None:
    _write(tmp_path, "edition", setting)
    with pytest.raises(BadParameter):
        _settings(tmp_path)


def test_optional_absence_is_distinct_from_a_valid_empty_mapping(tmp_path: Path) -> None:
    assert load_optional_yaml_mapping(tmp_path / "missing.yaml") is None
    assert _settings(tmp_path) == (False, None)
    path = _write(tmp_path, "program", b"{}")
    assert load_optional_yaml_mapping(path) == {}
    assert _settings(tmp_path) == (False, None)


def test_flat_edition_absence_uses_program_tree_but_empty_mapping_does_not(tmp_path: Path) -> None:
    nested = tmp_path / "programs" / "sample" / "editions" / "fixture.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text("readiness_gate: true\nreadiness_snapshot_max_age_days: 9\n", encoding="utf-8")
    assert _settings(tmp_path) == (True, 9)
    _write(tmp_path, "edition", b"{}")
    assert _settings(tmp_path) == (False, None)


@pytest.mark.parametrize("age", ["0", "5", "5.0", "' 5 '"])
def test_valid_program_age_retains_precedence_and_coercions(tmp_path: Path, age: str) -> None:
    _write(tmp_path, "program", f"readiness:\n  gate: true\n  snapshot_max_age_days: {age}\n".encode())
    _write(tmp_path, "edition", b"readiness_gate: false\nreadiness_snapshot_max_age_days: 9\n")
    assert _settings(tmp_path) == (True, 0 if age == "0" else 5)


@pytest.mark.parametrize("content", [b"{}", b"readiness: null", b"readiness:\n  gate: null\n  snapshot_max_age_days: null"])
def test_null_optional_program_fields_allow_edition_settings(tmp_path: Path, content: bytes) -> None:
    _write(tmp_path, "program", content)
    _write(tmp_path, "edition", b"readiness_gate: true\nreadiness_snapshot_max_age_days: 9\n")
    assert _settings(tmp_path) == (True, 9)


def test_same_edition_readiness_settings_remain_workspace_scoped(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write(first, "program", b"readiness:\n  gate: true\n  snapshot_max_age_days: 5\n")
    _write(second, "program", b"readiness:\n  gate: false\n  snapshot_max_age_days: 9\n")
    assert _settings(first) == (True, 5)
    assert _settings(second) == (False, 9)
