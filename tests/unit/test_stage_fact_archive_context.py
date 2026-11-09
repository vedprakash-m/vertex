from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest

from src.core import program_fact_store
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.pipeline import StageContext
from src.core.program_fact_store import (
    ProgramFactInput, ProgramFactSnapshot, ProgramFactStore, build_legacy_program_fact_snapshot,
)
from src.core.stages import action_stage, milestone_stage, risk_stage, sor_gated_load, validation_stage


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
    snapshot = ProgramFactSnapshot("fixture", datetime(2026, 10, 24, tzinfo=timezone.utc), ())
    loader = Mock(return_value=snapshot)
    monkeypatch.setattr(milestone_stage, "build_legacy_program_fact_snapshot", loader)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    milestones, dependencies, warnings, lineage, legacy_rollback = milestone_stage._load_milestones_via_reality(
        "fixture", programs_root=tmp_path / "programs",
        load_program_reality=Mock(side_effect=RuntimeError("Synthetic reality failure")), **archive_options,
    )
    assert milestones == dependencies == ()
    assert lineage == {}
    assert legacy_rollback is True
    assert "audited rollback flag" in warnings[0]
    assert loader.call_args_list == [
        call("fixture", programs_root=tmp_path / "programs", fact_types=(fact_type,), **archive_options)
        for fact_type in ("milestone.entry", "dependency.link")
    ]
    assert not list(tmp_path.iterdir())


def _milestone_rollback_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *, explicit_archive: bool = True,
) -> tuple[StageContext, Path]:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    programs_root = tmp_path / "selected" / "programs"
    program_dir = programs_root / "fixture"
    for root, label in ((programs_root, "Authored"), (tmp_path / "decoy" / "programs", "Decoy")):
        directory = root / "fixture"
        directory.mkdir(parents=True)
        (directory / "milestones.yaml").write_text(
            "schema_version: '1.0'\nmilestones:\n"
            "  - id: milestone-1\n"
            f"    name: {label} milestone\n"
            "    target_date: '2030-01-01'\n    owner_alias: operator\n"
            "    status: on_track\n    exit_criteria: []\n"
            "    linked_workstream_ids: []\n    linked_work_item_ids: []\n",
            encoding="utf-8",
        )
        (directory / "dependencies.yaml").write_text(
            "schema_version: '1.0'\ndependencies:\n"
            "  - id: dependency-1\n    from_milestone_id: milestone-1\n"
            "    to_milestone_id: milestone-2\n    dependency_type: blocks\n"
            f"    risk_if_broken: {label} dependency\n"
            "    status: active\n",
            encoding="utf-8",
        )
    authored = build_legacy_program_fact_snapshot(
        "fixture", programs_root=programs_root,
        fact_types=("milestone.entry", "dependency.link"),
    )
    assert len(authored.facts) == 2
    store = ProgramFactStore("fixture")
    for fact in authored.facts:
        payload = dict(fact.payload)
        payload["name" if fact.fact_type == "milestone.entry" else "risk_if_broken"] = "Stored conflict"
        store.append_fact(ProgramFactInput(
            fact_type=fact.fact_type, entity_refs=fact.entity_refs, payload=payload,
            scope=fact.scope, natural_key=fact.natural_key,
        ))
    save_fact_sor_state(
        "fixture", mode=mode,
        family_modes={"workitem.state": "primary"} if mode != "primary" else {},
        recorded_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    ctx = StageContext(
        edition_name="fixture_weekly", programs_root=programs_root,
        archive_root=tmp_path / "selected" / "archive" if explicit_archive else None,
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="fixture")),
        data_as_of=datetime(2026, 10, 24, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(
            load_program_reality=Mock(side_effect=RuntimeError("Synthetic reality failure")),
        ),
    )
    return ctx, program_dir


@pytest.mark.parametrize("mode", ["legacy", "shadow", "primary"])
@pytest.mark.parametrize("explicit_archive", [False, True])
@pytest.mark.parametrize("failure_point", ["load", "milestones", "dependencies"])
def test_audited_milestone_rollback_uses_authored_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, explicit_archive: bool, failure_point: str,
) -> None:
    ctx, program_dir = _milestone_rollback_context(
        tmp_path, monkeypatch, mode, explicit_archive=explicit_archive,
    )
    if failure_point != "load":
        loader = ctx.stage_support.load_program_reality
        loader.side_effect = None
        loader.return_value = SimpleNamespace(
            milestones=Mock(
                side_effect=RuntimeError("Synthetic milestone failure") if failure_point == "milestones" else None,
                return_value=(),
            ),
            dependencies=Mock(
                side_effect=RuntimeError("Synthetic dependency failure") if failure_point == "dependencies" else None,
                return_value=(),
            ),
        )
    source_bytes = tuple((program_dir / name).read_bytes() for name in ("milestones.yaml", "dependencies.yaml"))
    critical_path = Mock(wraps=milestone_stage.build_critical_path)
    monkeypatch.setattr(milestone_stage, "build_critical_path", critical_path)

    result = milestone_stage.MilestoneStage().execute(ctx)

    assert [milestone.name for milestone in result.milestones] == ["Authored milestone"]
    dependencies = critical_path.call_args.args[1]
    assert [dependency.risk_if_broken for dependency in dependencies] == ["Authored dependency"]
    assert result.milestone_lineage == {}
    assert result.report_reality is None
    assert len(result.milestone_warnings) == 1
    assert "audited rollback flag" in result.milestone_warnings[0]
    assert dict(sor_gated_load.report_family_selectors(result))["workitem.state"] == "primary"
    assert validation_stage._build_family_read_paths_metadata(result) == {
        "milestone": "legacy", "dependency": "legacy",
    }
    assert tuple((program_dir / name).read_bytes() for name in ("milestones.yaml", "dependencies.yaml")) == source_bytes


@pytest.mark.parametrize("missing", [("milestones.yaml",), ("dependencies.yaml",), ("milestones.yaml", "dependencies.yaml")])
def test_audited_milestone_rollback_records_legacy_when_sources_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...],
) -> None:
    ctx, program_dir = _milestone_rollback_context(tmp_path, monkeypatch, "primary")
    for name in missing:
        (program_dir / name).unlink()

    result = milestone_stage.MilestoneStage().execute(ctx)

    assert validation_stage._build_family_read_paths_metadata(result) == {
        "milestone": "legacy", "dependency": "legacy",
    }
    assert "audited rollback flag" in result.milestone_warnings[0]


@pytest.mark.parametrize("mode", ["legacy", "shadow", "primary"])
def test_audited_milestone_rollback_uses_authored_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    ctx, _ = _milestone_rollback_context(tmp_path, monkeypatch, mode)

    _, dependencies, _, _, legacy_rollback = milestone_stage._load_milestones_via_reality(
        "fixture", programs_root=ctx.programs_root, archive_root=ctx.archive_root,
        load_program_reality=ctx.stage_support.load_program_reality,
    )

    assert [dependency.risk_if_broken for dependency in dependencies] == ["Authored dependency"]
    assert legacy_rollback is True


@pytest.mark.parametrize("missing", ["milestones.yaml", "dependencies.yaml"])
def test_audited_milestone_rollback_does_not_replace_missing_authored_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    ctx, program_dir = _milestone_rollback_context(tmp_path, monkeypatch, "primary")
    (program_dir / missing).unlink()

    milestones, dependencies, warnings, lineage, legacy_rollback = milestone_stage._load_milestones_via_reality(
        "fixture", programs_root=ctx.programs_root, archive_root=ctx.archive_root,
        load_program_reality=ctx.stage_support.load_program_reality,
    )

    assert [item.name for item in milestones] == ([] if missing == "milestones.yaml" else ["Authored milestone"])
    assert [item.risk_if_broken for item in dependencies] == (
        [] if missing == "dependencies.yaml" else ["Authored dependency"]
    )
    assert "audited rollback flag" in warnings[0]
    assert lineage == {}
    assert legacy_rollback is True


@pytest.mark.parametrize("invalid", ["milestones.yaml", "dependencies.yaml"])
def test_audited_milestone_rollback_surfaces_invalid_authored_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    ctx, program_dir = _milestone_rollback_context(tmp_path, monkeypatch, "primary")
    path = program_dir / invalid
    path.write_text("schema_version: '2.0'\n", encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(ConfigError, match="schema_version"):
        milestone_stage.MilestoneStage().execute(ctx)

    assert path.read_bytes() == original


@pytest.mark.parametrize("builder", ["_build_milestone_entry_facts", "_build_dependency_facts"])
@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_audited_milestone_rollback_propagates_source_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: str, error_type: type[Exception],
) -> None:
    ctx, _ = _milestone_rollback_context(tmp_path, monkeypatch, "primary")
    failing = Mock(side_effect=error_type("Synthetic source error"))
    monkeypatch.setattr(program_fact_store, builder, failing)

    with pytest.raises(error_type, match="Synthetic source error"):
        milestone_stage.MilestoneStage().execute(ctx)

    failing.assert_called_once()


@pytest.mark.parametrize("case", ["success", "typed_error", "rollback_disabled"])
def test_milestone_nonrollback_paths_do_not_read_authored_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    ctx, _ = _milestone_rollback_context(tmp_path, monkeypatch, "primary")
    forbidden = Mock(side_effect=AssertionError("Unexpected authored source read"))
    monkeypatch.setattr(program_fact_store, "_build_milestone_entry_facts", forbidden)
    monkeypatch.setattr(program_fact_store, "_build_dependency_facts", forbidden)
    loader = ctx.stage_support.load_program_reality
    if case == "success":
        loader.side_effect = None
        loader.return_value = SimpleNamespace(milestones=lambda: (), dependencies=lambda: ())
    elif case == "typed_error":
        loader.side_effect = ConfigError("Synthetic typed reality error")
    else:
        monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK")

    if case == "rollback_disabled":
        with pytest.raises(ConfigError, match="audited legacy rollback"):
            milestone_stage.MilestoneStage().execute(ctx)
    else:
        result = milestone_stage.MilestoneStage().execute(ctx)
        assert result.milestones == ()
        assert bool(result.milestone_warnings) is (case == "typed_error")
        assert validation_stage._build_family_read_paths_metadata(result) == {
            "milestone": "reality", "dependency": "reality",
        }
    forbidden.assert_not_called()


def test_legacy_milestone_mode_keeps_current_reader_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _ = _milestone_rollback_context(tmp_path, monkeypatch, "legacy")
    save_fact_sor_state(
        "fixture", mode="legacy", recorded_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        programs_root=ctx.programs_root,
    )
    critical_path = Mock(wraps=milestone_stage.build_critical_path)
    monkeypatch.setattr(milestone_stage, "build_critical_path", critical_path)

    result = milestone_stage.MilestoneStage().execute(ctx)

    assert [milestone.name for milestone in result.milestones] == ["Stored conflict"]
    assert [dependency.risk_if_broken for dependency in critical_path.call_args.args[1]] == ["Stored conflict"]
    assert result.milestone_warnings == ()
    assert validation_stage._build_family_read_paths_metadata(result) == {
        "milestone": "legacy", "dependency": "legacy",
    }
    ctx.stage_support.load_program_reality.assert_not_called()
