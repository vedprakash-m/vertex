from __future__ import annotations

from pathlib import Path

from src.commands.gather_pipeline.m365_workstream_profile_stage import augment_m365_workstream_profiles
from src.core.models_v2 import Workstream


def test_augment_m365_workstream_profiles_adds_dependency_owner_aliases(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: "2.0"
id: acme
name: Adventure + DD on PF
people:
  - email: lidavidson@example.com
    display_name: James Davidson
    role: dependency_owner
    workstreams: [Store rollout]
""".strip(),
        encoding="utf-8",
    )
    workstreams = (
        Workstream(id="acme", name="Store rollout"),
    )

    augmented = augment_m365_workstream_profiles(
        "acme",
        workstreams=workstreams,
        programs_root=programs_root,
    )

    assert augmented[0].aliases == ("lidavidson", "James Davidson")
