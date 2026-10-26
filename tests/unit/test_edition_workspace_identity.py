from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.core.edition_resolver as resolver
from src.core.config_loader import load_report_bundle, load_report_config
from src.core.config_loader_v2 import load_edition_bundle
from src.core.exceptions import ConfigError


def _edition(root: Path, owner: str, *, program_id: str | None = None, edition_id: str = "weekly") -> Path:
    path = root / owner / "editions" / f"{edition_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"id: {edition_id}\nprogram_id: {program_id or owner}\n"
        "name: Synthetic edition\ntype: detailed\naltitude: detailed\ncadence: weekly\n",
        encoding="utf-8",
    )
    return path


def _files(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("lookup", [
    resolver.find_edition_yaml, resolver.resolve_edition_paths, resolver.resolve_edition,
    resolver.get_program_output_dir, load_edition_bundle, load_report_config, load_report_bundle,
])
def test_ambiguous_canonical_edition_fails_before_reading_any_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup: Callable[..., object],
) -> None:
    _edition(tmp_path, "alpha")
    _edition(tmp_path, "beta")
    before = _files(tmp_path)
    reader = Mock(side_effect=AssertionError("Ambiguous lookup must not read either edition"))
    monkeypatch.setattr(resolver, "load_yaml_mapping", reader)

    with pytest.raises(ConfigError, match="Ambiguous edition"):
        lookup("weekly", programs_root=tmp_path)

    reader.assert_not_called()
    assert _files(tmp_path) == before


def test_ambiguous_template_lookup_does_not_choose_first_template(tmp_path: Path) -> None:
    _edition(tmp_path / "_templates", "alpha", program_id="template:alpha")
    _edition(tmp_path / "_templates", "beta", program_id="template:beta")
    with pytest.raises(ConfigError, match="Ambiguous edition"):
        resolver.find_edition_yaml("weekly", programs_root=tmp_path)


@pytest.mark.parametrize("edition_id", [
    "", "  ", ".", "..", "../weekly", r"..\weekly", "/weekly", r"C:\weekly",
    "*", "week?", "[ab]", "week\nly", "weekly\0",
])
def test_lookup_rejects_non_bare_identifiers_before_globbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edition_id: str,
) -> None:
    glob = Mock(side_effect=AssertionError("Invalid identifier must not query the filesystem"))
    monkeypatch.setattr(Path, "glob", glob)
    with pytest.raises(ConfigError, match="edition identifier"):
        resolver.find_edition_yaml(edition_id, programs_root=tmp_path)
    glob.assert_not_called()


@pytest.mark.parametrize("lookup", [
    resolver.resolve_edition_paths, resolver.resolve_edition, resolver.get_program_output_dir,
    load_edition_bundle, load_report_config, load_report_bundle,
])
@pytest.mark.parametrize("program_id", ["beta", "../outside", r"..\outside", "/outside", "template:alpha"])
def test_canonical_edition_cannot_redirect_program_reads_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup: Callable[..., object], program_id: str,
) -> None:
    path = _edition(tmp_path, "alpha", program_id=program_id)
    before = _files(tmp_path)
    original_reader = resolver.load_yaml_mapping
    reader = Mock(side_effect=original_reader)
    monkeypatch.setattr(resolver, "load_yaml_mapping", reader)

    with pytest.raises(ConfigError, match="does not match.*owning program|Invalid program identifier/reference"):
        lookup("weekly", programs_root=tmp_path)

    assert [call.args[0] for call in reader.call_args_list] == [path]
    assert _files(tmp_path) == before


def test_template_edition_must_bind_to_its_template_program(tmp_path: Path) -> None:
    _edition(tmp_path / "_templates", "alpha", program_id="alpha")
    with pytest.raises(ConfigError, match="owning program"):
        resolver.resolve_edition_paths("weekly", programs_root=tmp_path)


@pytest.mark.parametrize("template", [False, True])
def test_unique_canonical_edition_paths_and_output_remain_compatible(tmp_path: Path, template: bool) -> None:
    root = tmp_path / "programs"
    program_id = "template:alpha" if template else "alpha"
    parent = root / "_templates" if template else root
    path = _edition(parent, "alpha", program_id=program_id)
    before = _files(tmp_path)

    paths = resolver.resolve_edition_paths("weekly", programs_root=root)

    assert paths is not None
    assert paths.program_id == "alpha"
    assert paths.edition_path == path
    assert paths.program_dir == parent / "alpha"
    assert paths.archive_dir == parent / "alpha" / "archive" / "weekly"
    assert paths.publications_dir == parent / "alpha" / "publications" / "weekly"
    assert resolver.get_program_output_dir("weekly", programs_root=root) == paths.publications_dir
    assert _files(tmp_path) == before


@pytest.mark.parametrize("explicit", [False, True])
def test_declared_legacy_layout_is_compatible(tmp_path: Path, explicit: bool) -> None:
    root = tmp_path / "programs"
    directory = tmp_path / ("declared_editions" if explicit else "editions")
    directory.mkdir()
    path = directory / "weekly.yaml"
    path.write_text("id: weekly\nprogram_id: alpha\n", encoding="utf-8")

    paths = resolver.resolve_edition_paths(
        "weekly", programs_root=root, editions_root=directory if explicit else None,
    )

    assert paths is not None
    assert paths.edition_path == path
    assert paths.program_dir == root / "alpha"


def test_existing_lookup_precedence_and_missing_result_remain_compatible(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    canonical = _edition(root, "alpha")
    _edition(root / "_templates", "template_alpha", program_id="template:template_alpha")
    legacy_dir = tmp_path / "editions"
    legacy_dir.mkdir()
    (legacy_dir / "weekly.yaml").write_text("program_id: legacy\n", encoding="utf-8")

    assert resolver.find_edition_yaml("weekly", programs_root=root) == canonical
    assert resolver.resolve_edition_paths("absent", programs_root=root) is None
    assert resolver.resolve_edition_paths(
        "absent", programs_root=root, editions_root=tmp_path / "declared_editions",
    ) is None
    assert resolver.get_program_output_dir("alpha", programs_root=root) == root / "alpha" / "publications" / "alpha"


@pytest.mark.parametrize("lookup", [resolver.resolve_edition_paths, resolver.get_program_output_dir])
@pytest.mark.parametrize("program_field", ["", "program_id: null\n", "program_id: 42\n", 'program_id: " "\n'])
def test_matched_edition_requires_program_identity_instead_of_output_fallback(
    tmp_path: Path, lookup: Callable[..., object], program_field: str,
) -> None:
    path = _edition(tmp_path, "alpha")
    path.write_text("id: weekly\n" + program_field, encoding="utf-8")
    with pytest.raises(ConfigError, match="program_id"):
        lookup("weekly", programs_root=tmp_path)


def test_same_named_program_and_edition_are_isolated_by_selected_workspace(tmp_path: Path) -> None:
    roots = (tmp_path / "workspace_a" / "programs", tmp_path / "workspace_b" / "programs")
    for root in roots:
        _edition(root, "alpha")

    paths = tuple(resolver.resolve_edition_paths("weekly", programs_root=root) for root in roots)

    for root, resolved in zip(roots, paths, strict=True):
        assert resolved is not None
        assert resolved.program_dir == root / "alpha"
        assert resolved.publications_dir.is_relative_to(root)
    assert paths[0] != paths[1]


def test_unique_canonical_edition_loads_through_actual_typed_bundle(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    _edition(root, "alpha")
    program = root / "alpha"
    (program / "program.yaml").write_text("id: alpha\nname: Synthetic program\n", encoding="utf-8")
    (program / "workstreams.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program / "scorecards.yaml").write_text("scorecards: []\n", encoding="utf-8")

    bundle = load_edition_bundle("weekly", programs_root=root)

    assert bundle is not None
    assert bundle.resolved.program.id == "alpha"
    assert bundle.resolved.edition.id == "weekly"
    assert bundle.resolved.paths.program_dir == program
    assert bundle.resolved.paths.publications_dir == program / "publications" / "weekly"
