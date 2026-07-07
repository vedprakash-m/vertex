from __future__ import annotations

from pathlib import Path

from src.commands import report_scorecards
from src.core.exceptions import ConfigError
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType
from src.core.dependency_graph import save_dependencies


def test_load_reachable_dependency_network_ignores_unrelated_action_loader_failures(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_dependencies(programs_root)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("actions broken")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    dependencies = report_scorecards._load_reachable_dependency_network("demo", programs_root=programs_root)

    assert {dependency.id for dependency in dependencies} == {"dep-demo-shared", "dep-shared-infra"}


def _seed_dependencies(programs_root: Path) -> None:
    save_dependencies(
        "demo",
        (
            Dependency(
                id="dep-demo-shared",
                from_program_id="demo",
                from_workstream_id="ws-demo",
                from_item_id=101,
                from_milestone_id=None,
                to_program_id="shared",
                to_workstream_id="ws-shared",
                to_item_id=202,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Shared deliverable blocks demo.",
                mitigation=None,
                status=DependencyStatus.ACTIVE,
                owner_alias="owner-demo",
            ),
        ),
        programs_root=programs_root,
    )
    save_dependencies(
        "shared",
        (
            Dependency(
                id="dep-shared-infra",
                from_program_id="shared",
                from_workstream_id="ws-shared",
                from_item_id=303,
                from_milestone_id=None,
                to_program_id="infra",
                to_workstream_id="ws-infra",
                to_item_id=404,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Infra dependency blocks shared.",
                mitigation=None,
                status=DependencyStatus.ACTIVE,
                owner_alias="owner-shared",
            ),
        ),
        programs_root=programs_root,
    )
