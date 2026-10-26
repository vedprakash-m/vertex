from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.pipeline import StageContext
from src.core.stages import action_stage, milestone_stage, risk_stage, sor_gated_load


@pytest.mark.parametrize(("module", "reader", "fact_type"), [
    (action_stage, "_load_current_actions", "action.item"),
    (risk_stage, "_load_current_risks", "risk.entry"),
    (milestone_stage, "_load_current_milestones", "milestone.entry"),
    (milestone_stage, "_load_current_dependencies", "dependency.link"),
])
@pytest.mark.parametrize("explicit", [False, True])
def test_staged_fact_readers_forward_only_explicit_archive_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, reader: str, fact_type: str, explicit: bool,
) -> None:
    loader = Mock(side_effect=RuntimeError("Selected fact boundary"))
    monkeypatch.setattr(module, "load_program_facts", loader)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    with pytest.raises(RuntimeError, match="Selected fact boundary"):
        getattr(module, reader)("fixture", programs_root=tmp_path / "programs", **archive_options)
    loader.assert_called_once_with(
        "fixture", programs_root=tmp_path / "programs", fact_types=(fact_type,), **archive_options,
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("stage", ["action", "risk", "milestone"])
def test_stages_forward_selected_archive_to_legacy_family_loaders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, stage: str,
) -> None:
    ctx = StageContext(
        programs_root=tmp_path / "programs", archive_root=tmp_path / "archive" if explicit else None,
        data_as_of=datetime(2026, 10, 24, tzinfo=timezone.utc),
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="fixture")),
    )
    loaders = []
    if stage == "action":
        loader = Mock(return_value=())
        monkeypatch.setattr(action_stage, "_load_current_actions", loader)
        loaders.append(loader)
        instance = action_stage.ActionStage()
    elif stage == "risk":
        loader = Mock(return_value=())
        monkeypatch.setattr(risk_stage, "_load_current_risks", loader)
        monkeypatch.setattr(sor_gated_load, "resolve_family_sor_mode", Mock(return_value="legacy"))
        loaders.append(loader)
        instance = risk_stage.RiskStage()
    else:
        monkeypatch.setattr(milestone_stage, "resolve_family_sor_mode", Mock(return_value="legacy"))
        for name in ("_load_current_milestones", "_load_current_dependencies"):
            loader = Mock(return_value=())
            monkeypatch.setattr(milestone_stage, name, loader)
            loaders.append(loader)
        instance = milestone_stage.MilestoneStage()
    instance.execute(ctx)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    for loader in loaders:
        loader.assert_called_once_with("fixture", programs_root=tmp_path / "programs", **archive_options)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("explicit", [False, True])
def test_audited_milestone_rollback_preserves_archive_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    loaders = [Mock(return_value=()), Mock(return_value=())]
    for name, loader in zip(("_load_current_milestones", "_load_current_dependencies"), loaders, strict=True):
        monkeypatch.setattr(milestone_stage, name, loader)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    milestones, dependencies, warnings, lineage = milestone_stage._load_milestones_via_reality(
        "fixture", programs_root=tmp_path / "programs",
        load_program_reality=Mock(side_effect=RuntimeError("Synthetic reality failure")), **archive_options,
    )
    assert milestones == dependencies == ()
    assert lineage == {}
    assert "audited rollback flag" in warnings[0]
    for loader in loaders:
        loader.assert_called_once_with("fixture", programs_root=tmp_path / "programs", **archive_options)
    assert not list(tmp_path.iterdir())
