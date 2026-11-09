from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands import report_scorecards
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType
from src.core.dependency_graph import save_dependencies
from src.core.program_reality import ProgramReality


def test_load_reachable_dependency_network_ignores_unrelated_action_loader_failures(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_dependencies(programs_root)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("actions broken")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    dependencies = report_scorecards._load_reachable_dependency_network("demo", programs_root=programs_root)

    assert {dependency.id for dependency in dependencies} == {"dep-demo-shared", "dep-shared-infra"}
    with pytest.raises(ConfigError, match="actions broken"):
        ProgramReality.load("demo", programs_root=programs_root)


@pytest.mark.parametrize(
    ("mode", "family_modes"),
    [("primary", None), ("legacy", {"workitem.state": "primary"})],
)
def test_dependency_network_keeps_primary_fact_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, family_modes: dict[str, str] | None,
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    programs_root = tmp_path / "programs"
    _seed_dependencies(programs_root)
    for program_id in ("demo", "shared"):
        save_fact_sor_state(
            program_id, mode=mode, family_modes=family_modes,
            recorded_at=datetime.now(timezone.utc), programs_root=programs_root,
        )
        (programs_root / program_id / "dependencies.yaml").write_text("invalid: [", encoding="utf-8")

    dependencies = report_scorecards._load_reachable_dependency_network("demo", programs_root=programs_root)

    assert {dependency.id for dependency in dependencies} == {"dep-demo-shared", "dep-shared-infra"}


def test_dependency_network_rejects_invalid_dependencies(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "dependencies.yaml").write_text("invalid: [", encoding="utf-8")

    with pytest.raises(ConfigError):
        report_scorecards._load_reachable_dependency_network("demo", programs_root=programs_root)


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
