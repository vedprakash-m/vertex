"""specs/people.md Phase 0c: tests for the alias-only legacy-affiliation
scan (src/core/people_legacy_affiliation.py)."""

from __future__ import annotations

from pathlib import Path

from src.core.people_legacy_affiliation import (
    RELATION_CHARTER_STAKEHOLDER,
    RELATION_RACI_ACCOUNTABLE,
    RELATION_RACI_RESPONSIBLE,
    RELATION_WORKSTREAM_ROUTING,
    discover_program_ids,
    find_alias_edges,
    find_cross_program_overlaps,
    scan_all_legacy_affiliations,
    scan_program_legacy_affiliations,
)


def _write_program(programs_root: Path, program_id: str, *, charter_alias: str | None = None, legacy_top_level_alias: str | None = None) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    lines = ['schema_version: "1.0"', f'id: "{program_id}"', f'name: "{program_id.title()}"']
    if charter_alias:
        lines += ["charter:", "  stakeholder_register:", f"    - alias: {charter_alias}"]
    if legacy_top_level_alias:
        lines += ["stakeholder_register:", f"  - alias: {legacy_top_level_alias}"]
    (program_dir / "program.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_workstreams(programs_root: Path, program_id: str, *, accountable: str | None = None, responsible: list[str] | None = None) -> None:
    program_dir = programs_root / program_id
    lines = ['schema_version: "1.0"', "workstreams:", '  - id: "ws1"', '    name: "Workstream One"']
    if accountable:
        lines.append(f"    accountable_owner: {accountable}")
    if responsible:
        lines.append("    responsible_owners:")
        lines += [f"      - {alias}" for alias in responsible]
    (program_dir / "workstreams.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_workstream_registry(programs_root: Path, program_id: str, *, alias: str) -> None:
    program_dir = programs_root / program_id
    content = (
        'schema_version: "1.0"\n'
        "workstreams:\n"
        '  - id: "ws1"\n'
        '    name: "Workstream One"\n'
        '    lifecycle_state: active\n'
        "    stakeholders:\n"
        f'      - name: "Sample Person"\n'
        '        role: "lead"\n'
        f"        alias: {alias}\n"
    )
    (program_dir / "workstream_registry.yaml").write_text(content, encoding="utf-8")


def test_discover_program_ids_finds_only_dirs_with_program_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="sample_owner")
    (programs_root / "_templates").mkdir(parents=True)
    (programs_root / "not_a_program").mkdir(parents=True)

    ids = discover_program_ids(programs_root=programs_root)

    assert ids == ("acme",)


def test_scan_program_legacy_affiliations_covers_charter_workstream_and_registry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="sample_owner")
    _write_workstreams(programs_root, "acme", accountable="sample_owner", responsible=["sample_other"])
    _write_workstream_registry(programs_root, "acme", alias="sample_owner")

    edges = scan_program_legacy_affiliations("acme", programs_root=programs_root)

    relation_types = {edge.relation_type for edge in edges}
    assert RELATION_CHARTER_STAKEHOLDER in relation_types
    assert RELATION_RACI_ACCOUNTABLE in relation_types
    assert RELATION_RACI_RESPONSIBLE in relation_types
    assert RELATION_WORKSTREAM_ROUTING in relation_types
    assert all(edge.program_id == "acme" for edge in edges)


def test_charter_and_legacy_top_level_stakeholder_register_both_scanned(tmp_path: Path) -> None:
    # specs/people.md gap #4 / STK-04: dual-read during migration.
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="sample_owner", legacy_top_level_alias="sample_legacy")

    edges = scan_program_legacy_affiliations("acme", programs_root=programs_root)

    aliases = {edge.alias for edge in edges}
    assert "sample_owner" in aliases
    assert "sample_legacy" in aliases
    sources = {edge.source_path for edge in edges}
    assert any("charter.stakeholder_register" in s for s in sources)
    assert any("legacy top-level" in s for s in sources)


def test_scan_program_legacy_affiliations_missing_files_degrade_to_empty(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme")  # No charter, no workstreams.yaml, no workstream_registry.yaml.

    edges = scan_program_legacy_affiliations("acme", programs_root=programs_root)

    assert edges == ()


def test_find_alias_edges_is_case_and_whitespace_normalized(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="Sample_Owner")

    edges = find_alias_edges("  sample_owner  ", programs_root=programs_root)

    assert len(edges) == 1
    assert edges[0].alias == "Sample_Owner"  # Original casing preserved for display.


def test_find_alias_edges_across_multiple_programs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="sample_owner")
    _write_program(programs_root, "fabrikam", charter_alias="sample_owner")

    edges = find_alias_edges("sample_owner", programs_root=programs_root)

    assert {edge.program_id for edge in edges} == {"acme", "fabrikam"}


def test_scan_all_legacy_affiliations_aggregates_every_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="sample_owner")
    _write_program(programs_root, "fabrikam", charter_alias="sample_other")

    edges = scan_all_legacy_affiliations(programs_root=programs_root)

    assert {edge.program_id for edge in edges} == {"acme", "fabrikam"}


def test_find_cross_program_overlaps_requires_at_least_two_programs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="shared_person")
    _write_program(programs_root, "fabrikam", charter_alias="shared_person")
    _write_program(programs_root, "nova", charter_alias="solo_person")

    overlaps = find_cross_program_overlaps(programs_root=programs_root)

    aliases = {alias for alias, _edges in overlaps}
    assert "shared_person" in aliases
    assert "solo_person" not in aliases


def test_find_cross_program_overlaps_scoped_to_one_program_still_shows_other_appearances(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="shared_person")
    _write_program(programs_root, "fabrikam", charter_alias="shared_person")
    _write_program(programs_root, "nova", charter_alias="unrelated_person")

    overlaps = find_cross_program_overlaps(programs_root=programs_root, program_id="acme")

    assert len(overlaps) == 1
    alias, edges = overlaps[0]
    assert alias == "shared_person"
    assert {edge.program_id for edge in edges} == {"acme", "fabrikam"}  # Both shown, not just acme.


def test_find_cross_program_overlaps_program_filter_excludes_non_matching_aliases(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program(programs_root, "acme", charter_alias="acme_only_shared")
    _write_program(programs_root, "fabrikam", charter_alias="acme_only_shared")
    _write_program(programs_root, "nova", charter_alias="nova_shared")
    _write_program(programs_root, "contoso", charter_alias="nova_shared")

    overlaps = find_cross_program_overlaps(programs_root=programs_root, program_id="acme")

    aliases = {alias for alias, _edges in overlaps}
    assert aliases == {"acme_only_shared"}
