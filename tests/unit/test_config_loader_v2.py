from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.core import config_loader_v2 as config_loader_v2_module
from src.core.models_v2 import Dependency, DependencyScheduleStatus, DependencyStatus, DependencyType


def test_build_key_dependency_chain_document_uses_program_facts(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    dependency = Dependency(
        id="dep-1",
        from_program_id="acme",
        from_workstream_id="velocity",
        from_item_id=1001,
        from_milestone_id=None,
        to_program_id="fabrikam",
        to_workstream_id="buildouts",
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Fabrikam buildout planning depends on Acme readiness.",
        mitigation=None,
        status=DependencyStatus.BROKEN,
        owner_alias="owner",
        resolution_path=None,
        planned_resolution_date=None,
        schedule_status=DependencyScheduleStatus.BLOCKED,
    )
    captured: list[tuple[str, tuple[str, ...], Path]] = []

    monkeypatch.setattr(
        config_loader_v2_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.append((program_id, fact_types, programs_root)) or snapshot,
    )
    monkeypatch.setattr(
        config_loader_v2_module,
        "project_dependencies",
        lambda loaded_snapshot: (dependency,) if loaded_snapshot is snapshot else (),
    )

    resolved = SimpleNamespace(
        raw_program={},
        paths=SimpleNamespace(program_id="acme", program_dir=tmp_path / "programs" / "acme"),
    )

    document = config_loader_v2_module._build_key_dependency_chain_document(resolved)

    assert document == [
        {
            "from_item": config_loader_v2_module.dependency_source_label(dependency),
            "to_item": config_loader_v2_module.dependency_target_label(dependency),
            "impact": "Fabrikam buildout planning depends on Acme readiness.",
        }
    ]
    assert captured == [("acme", ("dependency.link",), tmp_path / "programs")]
