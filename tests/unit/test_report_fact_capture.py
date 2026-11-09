from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path

import pytest

import src.commands.report as report_module
from src.commands.report import generate_report_draft
from src.commands.report_pipeline.assemble_stage import _pin_program_fact_snapshot
from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import ConfigError
from src.core.forecast_engine import ETAForecast
from src.core.models import Confidence, RiskLevel
from src.core.gather_run_manifest import QueryResultEntry
from src.core.manifest_writer import hash_jsonable
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_confirm import _isolate_fact_store_db as _isolate_fact_store_db
from tests.unit.test_commands_report import _sample_items
from tests.support.synthetic_report_source import synthetic_report_root as repo_root
from tests.unit.test_report_gather_run_lineage import _commit_gather_run


@pytest.mark.parametrize("change", ["addition", "replacement", "content"])
def test_ordinary_report_rejects_fact_change_during_acquisition(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    original = store.append_fact(
        ProgramFactInput(
            fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
            payload={"decision": "hold"}, natural_key="baseline",
        ), recorded_at=as_of,
    )

    def load_items(bundle, timestamp):
        if change == "content":
            with store._connect() as connection:
                connection.execute(
                    "UPDATE program_fact_revisions SET payload_json = ? WHERE revision_id = ?",
                    ('{"decision":"ship"}', original.revision.revision_id),
                )
        else:
            store.append_fact(
                ProgramFactInput(
                    fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
                    payload={"decision": "ship"},
                    natural_key="baseline" if change == "replacement" else "new-decision",
                ), recorded_at=as_of,
            )
        return _sample_items(timestamp), 0

    output_dir = get_program_output_dir("acme_weekly", programs_root=programs_root)
    prior_artifact = output_dir / "issue_001" / "issue_001.html"
    prior_artifact.parent.mkdir(parents=True, exist_ok=True)
    prior_artifact.write_text("previous reviewed artifact", encoding="utf-8")

    with pytest.raises(ConfigError, match="(?i)fact.*(changed|drift)"):
        generate_report_draft(
            edition_name="acme_weekly",
            reports_root=reports_root,
            programs_root=programs_root,
            archive_root=tmp_path / "archive",
            as_of=as_of,
            work_item_loader=load_items,
            open_browser=False,
        )

    assert prior_artifact.read_text(encoding="utf-8") == "previous reviewed artifact"
    assert not (prior_artifact.parent / "issue_001.draft.json").exists()
    with store._connect() as connection:
        pins = connection.execute("SELECT snapshot_id FROM program_fact_snapshot_pins").fetchall()
    assert len(pins) == 1
    if change == "content":
        with pytest.raises(ConfigError, match="content"):
            store.detect_drift(pins[0][0])
    else:
        assert len(store.detect_drift(pins[0][0])) == 1


@pytest.mark.parametrize("with_gather", [False, True])
def test_ordinary_report_persists_the_pre_acquisition_pin(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    with_gather: bool,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    observed_pins: list[str] = []
    selected = None
    if with_gather:
        selected = _commit_gather_run(programs_root, query_results=(QueryResultEntry(
            query_id="query", scope_id="scope", wiql_hash="declared-query",
            captured_at=datetime(2026, 5, 4, tzinfo=timezone.utc), raw_count=1,
            membership_ids=("999999",), membership_hash="observed-membership", cap_reached=False,
            completeness_state="full",
        ),))

    def load_items(bundle, timestamp):
        with store._connect() as connection:
            pins = connection.execute("SELECT snapshot_id FROM program_fact_snapshot_pins").fetchall()
        assert len(pins) == 1
        observed_pins.append(pins[0][0])
        return _sample_items(timestamp), 0

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=load_items,
        open_browser=False,
    )

    output_dir = get_program_output_dir("acme_weekly", programs_root=programs_root)
    draft = json.loads((output_dir / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    assert [draft["program_fact_snapshot"]["snapshot_id"]] == observed_pins
    manifest = json.loads((output_dir / "issue_001" / "issue_001.manifest.json").read_text(encoding="utf-8"))
    capture = manifest["metadata"]["run_evidence_snapshot"]
    assert capture["sha256"] == draft["run_evidence_snapshot_sha256"]
    assert capture["fact_snapshot_id"] == observed_pins[0]
    assert capture["fetched_item_ids"] == sorted(item["id"] for item in draft["items"])
    assert capture["report_snapshot_hash"] == manifest["snapshot_hash"]
    assert capture["capture_state"] == "unqualified_capture"
    assert "effective_policy_uncaptured" in capture["limitations"]
    assert capture["gather_run_id"] == (selected.run_id if selected else None)
    observation = manifest["metadata"]["program_verdict_observation"]
    assert observation["mode"] == "observe"
    assert observation["inputs"]["capture"] == capture
    assert observation["inputs_sha256"] == hash_jsonable(observation["inputs"])
    assert observation["verdict"]["snapshot_id"] == capture["sha256"]
    assert observation["verdict"]["program_id"] == "acme"
    assert observation["verdict"]["confidence"] == "none"
    assert observation["verdict"]["completion_phase"] == "unknown"
    assert "nonfact_inputs_unsealed" in {gap["code"] for gap in observation["verdict"]["evidence_gaps"]}
    if selected:
        assert capture["source_scopes"] == [{"query_id": "query", "scope_id": "scope", "query_hash": "declared-query"}]
        assert 999999 not in capture["fetched_item_ids"]
        assert "gather_fetch_join_unknown" in capture["limitations"]
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM program_fact_snapshot_pins").fetchone()[0] == 1


@pytest.mark.parametrize("damage", [
    "retained", "gather", "rendered", "forecast", "foreign_risk", "duplicate_risk",
    "foreign_milestone", "duplicate_milestone", "conflicting_judgment", "conflicting_workitem",
])
def test_invalid_capture_stops_before_output_directory_creation(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    execute = report_module._OutputStage.execute
    output = get_program_output_dir("acme_weekly", programs_root=root)

    def corrupt(stage, ctx):
        if damage == "retained":
            ctx = replace(ctx, fact_snapshot=None)
        elif damage == "gather":
            ctx = replace(ctx, gather_run_id="wrong")
        elif damage == "rendered":
            ctx = replace(ctx, render_state=replace(
                ctx.render_state, snapshot=replace(ctx.render_state.snapshot, items=()),
            ))
        elif damage == "forecast":
            item, *_ = ctx.render_state.snapshot.items
            ctx = replace(ctx, eta_forecasts={item.id: ETAForecast(
                work_item_id=item.id, ado_target_date=item.target_date,
                predicted_target_date=None, confidence=Confidence.HIGH,
                slip_probability=float("nan"), reasoning="Synthetic", prior_slips=0,
            )})
        elif damage in {"foreign_risk", "duplicate_risk"}:
            from tests.unit.test_program_verdict import _risk

            risk = _risk(program_id="foreign" if damage == "foreign_risk" else "acme")
            ctx = replace(ctx, risks=(risk, risk) if damage == "duplicate_risk" else (risk,))
        elif damage in {"conflicting_judgment", "conflicting_workitem"}:
            marker, scope, selectors = ctx.report_reality_scope
            family = "judgment" if damage == "conflicting_judgment" else "workitem.state"
            current = dict(selectors)[family]
            other = "legacy" if current != "legacy" else "primary"
            ctx = replace(ctx, report_reality_scope=(
                marker, scope, tuple(sorted((*selectors, (family, other)))),
            ))
        else:
            from tests.unit.test_program_verdict import _milestone

            milestone = replace(_milestone(), program_id="foreign" if damage == "foreign_milestone" else "acme")
            ctx = replace(ctx, milestones=(milestone, milestone) if damage == "duplicate_milestone" else (milestone,))
        before = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
        with pytest.raises(ConfigError):
            execute(stage, ctx)
        assert {path: path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
        assert not (output / "issue_001").exists()
        raise RuntimeError("verified prewrite refusal")

    monkeypatch.setattr(report_module._OutputStage, "execute", corrupt)
    with pytest.raises(RuntimeError, match="verified prewrite"):
        generate_report_draft(
            edition_name="acme_weekly", reports_root=reports, programs_root=root,
            archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
        )


def test_unresolved_ordinary_output_preserves_absent_capture_compatibility(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    execute = report_module._OutputStage.execute

    def legacy_output(stage, ctx):
        return execute(stage, replace(ctx, resolved_v2=None, signal_context=None))

    monkeypatch.setattr(report_module._OutputStage, "execute", legacy_output)
    result = generate_report_draft(
        edition_name="acme_weekly", reports_root=reports, programs_root=root,
        archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
    )
    assert "run_evidence_snapshot" not in result.manifest.metadata
    assert "program_verdict_observation" not in result.manifest.metadata
    assert "program_verdict_comparison" not in result.manifest.metadata
    draft = json.loads((result.manifest_path.parent / "issue_001.draft.json").read_text(encoding="utf-8"))
    assert draft["run_evidence_snapshot_sha256"] is None


@pytest.mark.parametrize("with_forecast", [False, True])
def test_ordinary_report_compares_the_rendered_health_summary_without_promotion(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_forecast: bool,
) -> None:
    from dataclasses import asdict

    from src.commands.confirm_stages.draft_loaders import load_confirm_draft_manifest
    from src.core.stages import render_stage

    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    build_health = render_stage._build_health_summary_with_risk_fallback
    output = report_module._OutputStage.execute
    health_summaries = []
    rendered = []

    def retain_health(*args, **kwargs):
        health = build_health(*args, **kwargs)
        health_summaries.append(health)
        return health

    def capture_output(stage, ctx):
        rendered.append((
            ctx.render_state.html_body, ctx.render_state.markdown_body,
            dict(ctx.validation_state.manifest.qg_results),
        ))
        if with_forecast:
            item = next(item for item in ctx.render_state.snapshot.items if item.target_date is not None)
            ctx = replace(ctx, eta_forecasts={item.id: ETAForecast(
                work_item_id=item.id, ado_target_date=item.target_date,
                predicted_target_date=item.target_date, confidence=Confidence.HIGH,
                slip_probability=0.8, reasoning="Synthetic", prior_slips=0,
            )})
        return output(stage, ctx)

    monkeypatch.setattr(render_stage, "_build_health_summary_with_risk_fallback", retain_health)
    monkeypatch.setattr(report_module._OutputStage, "execute", capture_output)
    result = generate_report_draft(
        edition_name="acme_weekly", reports_root=reports, programs_root=root,
        archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (
            tuple(replace(item, risk_level=RiskLevel.LOW, state="Active") for item in _sample_items(timestamp)), 0,
        ),
        open_browser=False,
    )

    health, = health_summaries
    observation = result.manifest.metadata["program_verdict_observation"]
    comparison = result.manifest.metadata["program_verdict_comparison"]
    assert comparison.report_risk == health.overall_risk
    assert comparison.report_health_state == health.health_state
    assert comparison.verdict_risk == observation.verdict.health
    assert comparison.risk_values_equal == (health.overall_risk == observation.verdict.health)
    assert comparison.capture_sha256 == observation.verdict.snapshot_id
    assert comparison.observation_inputs_sha256 == observation.inputs_sha256
    persisted = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert persisted["metadata"]["program_verdict_comparison"] == asdict(comparison)
    draft = json.loads((result.manifest_path.parent / "issue_001.draft.json").read_text(encoding="utf-8"))
    captured = load_confirm_draft_manifest("acme_weekly", 1, draft_state=draft, programs_root=root)
    assert captured.manifest_id == result.manifest.manifest_id
    assert captured.run_evidence_snapshot is not None
    assert captured.run_evidence_snapshot.sha256 == comparison.capture_sha256
    assert (result.html_body, result.markdown_body) == rendered[0][:2]
    assert result.manifest.qg_results == rendered[0][2]
    assert not any("verdict" in gate for gate in result.manifest.qg_results)
    assert comparison.schema_version not in result.html_body
    assert comparison.schema_version not in result.markdown_body
    if with_forecast:
        assert observation.verdict.schedule_risk.value == "high"
        assert comparison.report_risk != comparison.verdict_risk
        assert comparison.risk_values_equal is False


@pytest.mark.parametrize(("probability", "historical_target", "expected"), [
    (0.5, False, "medium"), (0.8, False, "high"), (1.0, True, "medium"),
])
def test_ordinary_report_observes_forecast_floor_without_promoting_it(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    probability: float, historical_target: bool, expected: str,
) -> None:
    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    execute = report_module._OutputStage.execute

    def observe_forecast(stage, ctx):
        from datetime import timedelta

        item = next(item for item in ctx.render_state.snapshot.items if item.target_date is not None)
        return execute(stage, replace(ctx, eta_forecasts={item.id: ETAForecast(
            work_item_id=item.id,
            ado_target_date=item.target_date + timedelta(days=1) if historical_target else item.target_date,
            predicted_target_date=item.target_date, confidence=Confidence.HIGH,
            slip_probability=probability, reasoning="Synthetic", prior_slips=0,
        )}))

    monkeypatch.setattr(report_module._OutputStage, "execute", observe_forecast)
    result = generate_report_draft(
        edition_name="acme_weekly", reports_root=reports, programs_root=root,
        archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
    )
    observation = result.manifest.metadata["program_verdict_observation"]
    assert observation.verdict.schedule_risk.value == expected
    assert observation.inputs.forecasts[0].slip_probability == probability
    assert observation.verdict.confidence == Confidence.NONE
    assert observation.mode == "observe"
    if historical_target:
        assert "forecast_target_changed" in {gap.code for gap in observation.verdict.evidence_gaps}
        assert "milestone_at_risk" in {reason.code for reason in observation.verdict.reasons}
        assert not any(reason.code.startswith("miss_probability_") for reason in observation.verdict.reasons)
    assert not any("verdict" in gate for gate in result.manifest.qg_results)
    assert observation.verdict.policy_version not in result.html_body
    assert observation.verdict.policy_version not in result.markdown_body


@pytest.mark.parametrize("assessed", [False, True])
def test_ordinary_report_verdict_retains_loaded_risks_without_reload(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assessed: bool,
) -> None:
    from tests.unit.test_program_verdict import _risk, _risk_assessment

    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    risk = _risk(program_id="acme")
    assessments = (_risk_assessment(risk),) if assessed else None
    execute = report_module._OutputStage.execute

    def observe_loaded_risk(stage, ctx):
        def unexpected_reload(*args, **kwargs):
            raise AssertionError("Verdict must not reload risks at output")

        with monkeypatch.context() as scoped:
            scoped.setattr("src.core.program_reality.ProgramReality.load", unexpected_reload)
            scoped.setattr("src.core.risk_register_engine.load_risk_register", unexpected_reload)
            return execute(stage, replace(
                ctx, risks=(risk,), risk_assessments=assessments,
                risk_warnings=("Synthetic retained diagnostic",),
            ))

    monkeypatch.setattr(report_module._OutputStage, "execute", observe_loaded_risk)
    result = generate_report_draft(
        edition_name="acme_weekly", reports_root=reports, programs_root=root,
        archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
    )
    observation = result.manifest.metadata["program_verdict_observation"]
    assert observation.inputs.risks == (risk,)
    assert observation.inputs.risk_assessments == assessments
    assert observation.inputs.risk_warnings == ("Synthetic retained diagnostic",)
    ask, = observation.verdict.leadership_asks
    assert ask.entity_refs == ("RISK:risk-1",)
    assert bool(ask.evidence_refs) is assessed
    persisted = json.loads(result.manifest_path.read_text(encoding="utf-8"))["metadata"]["program_verdict_observation"]
    assert persisted["inputs_sha256"] == hash_jsonable(persisted["inputs"])
    assert persisted["verdict"]["leadership_asks"][0]["missing_fields"] == ["owner", "mitigation_eta"]
    assert observation.verdict.confidence == Confidence.NONE
    assert "Assign owner" not in result.html_body
    assert not any("verdict" in gate for gate in result.manifest.qg_results)


@pytest.mark.parametrize(("mode", "kind"), [
    ("legacy", "strategic"), ("primary", "strategic"),
    ("legacy", "candidate"), ("primary", "candidate"),
    ("legacy", "hygiene"), ("primary", "hygiene"),
])
def test_real_risk_stage_feeds_observation_through_ordinary_report(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, kind: str,
) -> None:
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.risk_register_engine import _risk_fact_payload
    from src.core.stages.render_stage import RenderStage
    from src.core.stages.risk_stage import RiskStage
    from tests.unit.test_program_verdict import _risk

    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    risk = _risk(program_id="acme", kind=kind)
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    store.append_fact(ProgramFactInput(
        fact_type="risk.entry", scope="program", entity_refs=("RISK:risk-1",),
        payload=_risk_fact_payload(risk), natural_key="risk:risk-1",
    ), recorded_at=as_of)
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"judgment": mode},
        recorded_at=as_of, recorded_by="synthetic", programs_root=root,
    )
    selected = []
    rendered_health = []
    execute = RiskStage.execute
    render = RenderStage.execute

    def retain_selected(stage, ctx):
        result = execute(stage, ctx)
        selected.append(result)
        return result

    def retain_rendered_health(stage, ctx):
        result = render(stage, ctx)
        rendered_health.append(result.render_state.health)
        return result

    monkeypatch.setattr(RiskStage, "execute", retain_selected)
    monkeypatch.setattr(RenderStage, "execute", retain_rendered_health)
    result = generate_report_draft(
        edition_name="acme_weekly", reports_root=reports, programs_root=root,
        archive_root=tmp_path / "archive", as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (
            tuple(replace(item, risk_level=RiskLevel.LOW, state="Active") for item in _sample_items(timestamp)), 0,
        ),
        open_browser=False,
    )
    stage, = selected
    observed = result.manifest.metadata["program_verdict_observation"]
    assert len(stage.risks) == 1
    assert observed.inputs.risks == stage.risks
    assert observed.inputs.risk_assessments == stage.risk_assessments
    assert observed.inputs.stale_risk_ids == stage.stale_risk_ids
    assert (observed.inputs.risk_assessments is None) is (mode == "legacy")
    assert observed.inputs.risks[0].kind == kind
    if kind == "strategic":
        ask, = observed.verdict.leadership_asks
        assert ask.entity_refs == ("RISK:risk-1",)
        assert ask.missing_fields == ("owner", "mitigation_eta")
        assert observed.verdict.health.value == "high"
        comparison = result.manifest.metadata["program_verdict_comparison"]
        assert comparison.report_risk == rendered_health[0].overall_risk
        assert comparison.report_health_state == rendered_health[0].health_state
        assert comparison.report_risk != RiskLevel.HIGH
        assert comparison.verdict_risk == RiskLevel.HIGH
        assert comparison.risk_values_equal is False
    else:
        assert observed.verdict.leadership_asks == ()
        assert "risk_register_high_floor" not in {reason.code for reason in observed.verdict.reasons}
    assert len(observed.inputs.capture.binding_declarations) >= 1


@pytest.mark.parametrize("mode", ["legacy", "primary"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_real_milestone_stage_feeds_observation_without_output_reread(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, offset: int,
) -> None:
    from dataclasses import asdict
    from datetime import date, timedelta
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.stages.milestone_stage import MilestoneStage
    from tests.unit.test_program_verdict import _milestone

    reports = stage_v2_report_workspace(repo_root, tmp_path)
    root = reports.parent / "programs"
    disable_kusto_in_report_copy(reports)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    milestone = replace(_milestone(target_date=date(2026, 5, 5) + timedelta(days=offset)), program_id="acme")
    payload = asdict(milestone)
    payload["status"] = milestone.status.value
    payload["target_date"] = milestone.target_date.isoformat()
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    store.append_fact(ProgramFactInput(
        fact_type="milestone.entry", scope="program", entity_refs=(f"MILESTONE:{milestone.id}",),
        payload=payload, natural_key=f"milestone:{milestone.id}",
    ), recorded_at=as_of)
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"workitem.state": mode},
        recorded_at=as_of, recorded_by="synthetic", programs_root=root,
    )
    selected = []
    stage_execute = MilestoneStage.execute
    output_execute = report_module._OutputStage.execute

    def retain_selected(stage, ctx):
        result = stage_execute(stage, ctx)
        selected.append(result)
        return result

    def no_reread(stage, ctx):
        def unexpected(*args, **kwargs):
            raise AssertionError("Milestone observer cannot reload at output")

        with monkeypatch.context() as scoped:
            scoped.setattr("src.core.program_reality.ProgramReality.load", unexpected)
            scoped.setattr("src.core.stages.milestone_stage._load_current_milestones", unexpected)
            return output_execute(stage, ctx)

    monkeypatch.setattr(MilestoneStage, "execute", retain_selected)
    monkeypatch.setattr(report_module._OutputStage, "execute", no_reread)
    result = generate_report_draft(
        edition_name="acme_weekly", reports_root=reports, programs_root=root,
        archive_root=tmp_path / "archive", as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
    )
    stage, = selected
    observed = result.manifest.metadata["program_verdict_observation"]
    assert milestone.id in {row.id for row in stage.milestones}
    assert observed.inputs.milestones == tuple(sorted(stage.milestones, key=lambda row: row.id))
    assert observed.inputs.milestone_assessments == tuple(sorted(stage.milestone_assessments, key=hash_jsonable))
    assert observed.inputs.milestone_warnings == stage.milestone_warnings
    assessment, = (row for row in observed.inputs.milestone_assessments if row.milestone_id == milestone.id)
    assert assessment.computed_health.value == ("missed" if offset < 0 else "unknown")
    reasons = {reason.code for reason in observed.verdict.reasons}
    assert ("milestone_missed" in reasons) is (offset < 0)
    assert "milestone_evidence_incomplete" in {gap.code for gap in observed.verdict.evidence_gaps}
    assert observed.verdict.confidence == Confidence.NONE
    persisted = json.loads(result.manifest_path.read_text(encoding="utf-8"))["metadata"]["program_verdict_observation"]
    assert persisted["inputs_sha256"] == hash_jsonable(persisted["inputs"])
    assert not any("verdict" in gate for gate in result.manifest.qg_results)


@pytest.mark.parametrize("scope", ["program", "edition", "issue"])
def test_captured_pin_cannot_be_reused_for_another_output(
    tmp_path: Path, scope: str,
) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    pin = store.pin_snapshot(metadata={"edition_name": "acme_weekly", "issue_number": 1})
    if scope == "program":
        pin = replace(pin, program_id="other")
    else:
        pin = replace(
            pin,
            metadata={
                **pin.metadata,
                "edition_name" if scope == "edition" else "issue_number": "other" if scope == "edition" else 2,
            },
        )

    with pytest.raises(ConfigError, match="scope"):
        _pin_program_fact_snapshot(
            "acme", edition_name="acme_weekly", issue_number=1,
            generated_at=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            db_root=tmp_path, pin=pin,
        )


def test_ordinary_report_cannot_replace_a_missing_captured_pin(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    execute_output = report_module._OutputStage.execute

    def drop_captured_pin(stage, ctx):
        assert ctx.fact_snapshot_pin is not None
        return execute_output(stage, replace(ctx, fact_snapshot_pin=None))

    monkeypatch.setattr(report_module._OutputStage, "execute", drop_captured_pin)

    with pytest.raises(ConfigError, match="baseline is missing"):
        generate_report_draft(
            edition_name="acme_weekly",
            reports_root=reports_root,
            programs_root=programs_root,
            archive_root=tmp_path / "archive",
            as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
            open_browser=False,
        )

    output_dir = get_program_output_dir("acme_weekly", programs_root=programs_root)
    assert not (output_dir / "issue_001" / "issue_001.draft.json").exists()
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM program_fact_snapshot_pins").fetchone()[0] == 1


@pytest.mark.parametrize("program_id", [None, "acme"])
def test_legacy_pin_helper_keeps_optional_program_behavior(
    tmp_path: Path, program_id: str | None,
) -> None:
    root = tmp_path / "fact-db"
    draft_pin = _pin_program_fact_snapshot(
        program_id, edition_name="acme_weekly", issue_number=1,
        generated_at=datetime(2026, 5, 5, 18, tzinfo=timezone.utc), db_root=root,
    )

    if program_id is None:
        assert draft_pin is None
        assert not root.exists()
    else:
        assert draft_pin is not None
        assert draft_pin.program_id == program_id
        assert ProgramFactStore(program_id, db_root=root).detect_drift(draft_pin.snapshot_id) == ()
