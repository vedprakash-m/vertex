from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

import src.core.edition_resolver as resolver
from src.core.config_loader import load_report_bundle, load_report_config
from src.core.config_loader_v2 import load_edition_bundle
from src.core.exceptions import ConfigError


INVALID_REFERENCES = (
    "", " ", ".", "..", "../outside", r"..\outside", "/outside", r"\outside",
    r"C:\outside", "C:outside", r"\\host\share", "alpha/../beta", r"alpha\..\beta",
    "alpha.", "alpha ", "*", "alpha?", "[ab]", "alpha:stream", "alpha\nbeta",
    "alpha\t", "alpha\0", "template:", "template: ", "template:..",
    "template:../outside", r"template:..\outside", "template:/outside",
    "template:C:outside", "template:alpha.", "template:alpha ", "template:*",
    "template:alpha\n", "template:template:alpha",
)


@pytest.mark.parametrize("reference", INVALID_REFERENCES)
def test_program_reference_is_rejected_before_path_construction(reference: str) -> None:
    root = MagicMock(spec=Path)

    with pytest.raises(ConfigError, match="program.*(identifier|reference)"):
        resolver._program_dir_for_reference(reference, programs_root=root)

    root.__truediv__.assert_not_called()


@pytest.mark.parametrize("reference", INVALID_REFERENCES)
def test_program_reference_normalization_does_not_hide_invalid_values(reference: str) -> None:
    with pytest.raises(ConfigError, match="program.*(identifier|reference)"):
        resolver._normalized_program_id(reference)


@pytest.mark.parametrize("lookup", [
    resolver.resolve_edition_paths, resolver.resolve_edition, resolver.get_program_output_dir,
    load_edition_bundle, load_report_config, load_report_bundle,
])
@pytest.mark.parametrize("reference", [
    "../outside", r"..\outside", "/outside", "C:outside", "alpha.", "alpha ",
    "*", "alpha\n", "template:../outside", "template:alpha ",
])
def test_legacy_edition_rejects_reference_before_program_reads_or_output_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    lookup: Callable[..., object], reference: str,
) -> None:
    root = tmp_path / "programs"
    editions = tmp_path / "editions"
    editions.mkdir()
    edition = editions / "weekly.yaml"
    edition.write_text(
        f"id: weekly\nprogram_id: {json.dumps(reference)}\n", encoding="utf-8",
    )
    original_reader = resolver.load_yaml_mapping

    def read_only_edition(path: Path) -> dict[str, object]:
        assert path == edition, "Invalid reference reached a program YAML read"
        return original_reader(path)

    reader = Mock(side_effect=read_only_edition)
    output = Mock(side_effect=AssertionError("Invalid reference reached output probing"))
    monkeypatch.setattr(resolver, "load_yaml_mapping", reader)
    monkeypatch.setattr(resolver, "_resolve_output_dir", output)

    with pytest.raises(ConfigError, match="program.*(identifier|reference)"):
        lookup("weekly", programs_root=root)

    reader.assert_called_once_with(edition)
    output.assert_not_called()
    assert not root.exists()


@pytest.mark.parametrize("reference", ["../outside", "C:outside", "alpha.", "template:../outside"])
def test_program_listing_rejects_reference_before_filesystem_access(
    reference: str,
) -> None:
    root = MagicMock(spec=Path)
    root.__truediv__.side_effect = AssertionError("Invalid reference reached filesystem")

    with pytest.raises(ConfigError, match="program.*(identifier|reference)"):
        resolver.list_editions_for_program(reference, programs_root=root)

    root.__truediv__.assert_not_called()


@pytest.mark.parametrize("reference,parts,normalized", [
    ("alpha", ("alpha",), "alpha"),
    ("Legacy_ID-1.2", ("Legacy_ID-1.2",), "Legacy_ID-1.2"),
    ("program name", ("program name",), "program name"),
    ("template:alpha", ("_templates", "alpha"), "alpha"),
    ("template:Legacy_ID-1.2", ("_templates", "Legacy_ID-1.2"), "Legacy_ID-1.2"),
])
def test_supported_program_references_preserve_existing_layouts(
    tmp_path: Path, reference: str, parts: tuple[str, ...], normalized: str,
) -> None:
    assert resolver._normalized_program_id(reference) == normalized
    assert resolver._program_dir_for_reference(reference, programs_root=tmp_path) == tmp_path.joinpath(*parts)


@pytest.mark.parametrize("reference,parts", [
    ("Legacy_ID-1.2", ("Legacy_ID-1.2",)),
    ("template:alpha", ("_templates", "alpha")),
])
@pytest.mark.parametrize("explicit", [False, True])
def test_valid_legacy_reference_remains_compatible(
    tmp_path: Path, reference: str, parts: tuple[str, ...], explicit: bool,
) -> None:
    root = tmp_path / "programs"
    editions = tmp_path / ("declared_editions" if explicit else "editions")
    editions.mkdir()
    edition = editions / "weekly.yaml"
    edition.write_text(f"id: weekly\nprogram_id: {reference}\n", encoding="utf-8")

    paths = resolver.resolve_edition_paths(
        "weekly", programs_root=root, editions_root=editions if explicit else None,
    )

    assert paths is not None
    assert paths.program_dir == root.joinpath(*parts)
    assert paths.program_id == parts[-1]
    assert paths.publications_dir == root.joinpath(*parts, "publications", "weekly")
