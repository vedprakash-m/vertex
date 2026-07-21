from __future__ import annotations

from pathlib import Path

from src.core.program_lifecycle import assess_program_lifecycle, is_program_active


def _seed_program(programs_root: Path, program_id: str, *, with_edition: bool = True, archived: bool | None = None, lifecycle_status: str | None = None) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    lines = ['schema_version: "3.0"', f'id: "{program_id}"', f'name: "{program_id.title()}"']
    if archived is not None:
        lines.append(f"archived: {str(archived).lower()}")
    if lifecycle_status is not None:
        lines.append(f'lifecycle_status: "{lifecycle_status}"')
    (program_dir / "program.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if with_edition:
        editions_dir = program_dir / "editions"
        editions_dir.mkdir(parents=True, exist_ok=True)
        (editions_dir / f"{program_id}_weekly.yaml").write_text('schema_version: "2.0"\n', encoding="utf-8")


def test_program_with_yaml_and_edition_is_active(tmp_path: Path) -> None:
    _seed_program(tmp_path, "acme")

    assert is_program_active("acme", programs_root=tmp_path) is True


def test_program_missing_program_yaml_is_not_active(tmp_path: Path) -> None:
    assert is_program_active("ghost", programs_root=tmp_path) is False


def test_program_with_no_configured_edition_is_not_active(tmp_path: Path) -> None:
    _seed_program(tmp_path, "acme", with_edition=False)

    assert is_program_active("acme", programs_root=tmp_path) is False


def test_program_with_archived_true_marker_is_not_active(tmp_path: Path) -> None:
    _seed_program(tmp_path, "acme", archived=True)

    assessment = assess_program_lifecycle("acme", programs_root=tmp_path)

    assert assessment.status == "archived"
    assert assessment.archive_marker_present is True
    assert is_program_active("acme", programs_root=tmp_path) is False


def test_program_with_lifecycle_status_archived_marker_is_not_active(tmp_path: Path) -> None:
    _seed_program(tmp_path, "acme", lifecycle_status="archived")

    assert is_program_active("acme", programs_root=tmp_path) is False


def test_program_with_archived_false_marker_is_active(tmp_path: Path) -> None:
    _seed_program(tmp_path, "acme", archived=False)

    assert is_program_active("acme", programs_root=tmp_path) is True


def test_assess_program_lifecycle_never_raises_on_malformed_yaml(tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("not: valid: yaml: [", encoding="utf-8")

    assessment = assess_program_lifecycle("acme", programs_root=tmp_path)

    assert assessment.status == "archived"
    assert assessment.has_program_yaml is True
