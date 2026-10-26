from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone, tzinfo
import json
from pathlib import Path
import shutil

import pytest
import yaml

from tests.support.fact_store_doctor_setup import stage_fact_store_doctor_workspace
from tests.support.report_test_setup import stage_v2_report_workspace
from tests.support.slice_contract_fixtures import build_test_source_health_slice_contract


def pin_source_health_time(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return datetime(2026, 5, 28, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("src.core.source_health.datetime", FixedDatetime)


@pytest.fixture
def doctor_source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from src.commands.doctor import ADOProbeResult

    monkeypatch.setattr("src.commands.doctor._probe_ado_access", lambda bundle: ADOProbeResult(
        reachable=True, auth_method="synthetic", item_count=0,
        token_minutes_remaining=47, detail="Synthetic ADO probe.",
    ))
    monkeypatch.setattr("src.commands.doctor._live_kusto_probe", lambda: lambda query: None)
    return build_doctor_source(tmp_path / "doctor-source")


def build_doctor_source(root: Path) -> Path:
    stage_fact_store_doctor_workspace(root)
    (root / "reports" / "schemas").mkdir()
    program = root / "programs" / "acme"
    program_path = program / "program.yaml"
    config = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    config.update(schema_version="3.0", chapter_namespace="acme")
    config["m365"]["enabled"] = True
    config["m365"]["workiq_queries"] = {"review": "Synthetic program review"}
    config["communication_plan"] = [{"edition": "acme_weekly", "cadence": "weekly"}]
    program_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    workstreams = [
        {
            "id": identifier,
            "name": title,
            "area_paths": [rf"One\{title}"],
            "dri_email": "demo@example.com",
            "signal_sources": {"kusto_query_ids": ["velocity-p50"]},
        }
        for identifier, title in (("acme", "Acme"), ("diagnostics_surface", "Diagnostics Surface"))
    ]
    _write(program / "workstreams.yaml", {"schema_version": "2.0", "workstreams": workstreams})
    _write(program / "scorecards.yaml", {
        "schema_version": "2.0",
        "scorecards": [{
            "name": "Acme Readiness",
            "dimensions": [{
                "name": "Deployment Velocity",
                "workstream_id": "acme",
                "ado_filter": "area_path contains 'Acme'",
            }],
        }],
    })
    contract = asdict(build_test_source_health_slice_contract(
        contract_id="acme.deployment_velocity",
        scorecard_name="Acme Readiness",
        section="acme",
        workstream="acme",
        title="Deployment Velocity",
        source_of_truth="hybrid",
        fallback_sources=("lt_deck", "contoso_daily"),
        required=False,
    ))
    for key in ("decision_sources", "fallback_sources"):
        contract[key] = contract["source_contract"].pop(key)
    contract["decision_sources"] = [
        {
            "source_id": source_id,
            "channels": ["workiq"],
            "blocked_artifact_selectors": [],
            "blocked_artifact_ids": [],
        }
        for source_id in contract["fallback_sources"]
    ]
    contract["source_contract"]["ado"]["saved_queries"] = ["synthetic-query"]
    _write(program / "slice_contracts.yaml", {"schema_version": "1.0", "slices": [contract]})
    _write(program / "chapter_contract.yaml", {
        "schema_version": "1.0",
        "chapters": [{
            "id": "acme",
            "title": "Acme",
            "priority": 1,
            "dimensions": ["acme.deployment_velocity"],
            "include_in": ["detailed", "focused", "lookback", "deck"],
            "show_in_jump_list": True,
        }],
    })
    _write(program / "workstream_registry.yaml", {
        "schema_version": "1.0",
        "workstreams": [
            {"id": row["id"], "name": row["name"], "stakeholders": [{"alias": "demo", "name": "Demo Author", "role": "owner"}]}
            for row in workstreams
        ],
    })
    edition_path = program / "editions" / "acme_weekly.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    for identifier, kind, cadence in (
        ("acme_weekly", "detailed", "weekly"),
        ("example_quarterly", "lookback", "quarterly"),
        ("example_lt_deck", "deck", "weekly"),
        ("example_nudge", "nudge", "weekly"),
    ):
        _write(edition_path.parent / f"{identifier}.yaml", dict(
            edition, id=identifier, type=kind, cadence=cadence,
            hygiene={"stale_business_days": 5, "workstream_coverage_alerts": True},
        ))
    families = ["detailed", "focused", "lookback", "deck", "nudge", "narrative"]
    _write(program / "template_contract.yaml", {
        "schema_version": "1.0",
        "edition_family": {"default": "detailed", "allowed": families},
        "families": {name: {"order": ["exec_summary"], "mandatory": [], "optional": ["exec_summary"]} for name in families},
    })
    _write(program / "readiness.yaml", {"schema_version": "1.0", "enabled": False})
    knowledge = root / "knowledge"
    knowledge.mkdir()
    for name in ("people_directory", "teams", "products", "golden_queries"):
        document = yaml.safe_load((program / "knowledge" / f"{name}.yaml").read_text(encoding="utf-8"))
        if name == "golden_queries":
            for query in document["queries"]:
                query["program_ids"] = ["acme"]
            for identifier in ("icm-active", "icm-mttr"):
                document["queries"].append(dict(
                    document["queries"][0], id=identifier,
                    cluster="https://icmcluster.example.kusto.windows.net", database="IcMDataWarehouse",
                ))
        elif name == "people_directory":
            document["people"].append({"alias": "backup", "email": "backup@example.com", "display_name": "Backup Author"})
        elif name == "teams":
            document["teams"] = [{"id": "synthetic-partner", "name": "Synthetic Partner", "programs": ["fabrikam"]}]
        _write(knowledge / f"{name}.yaml", document)
        _write(program / "knowledge" / f"{name}.yaml", document)
    (program / "knowledge" / "golden_queries.yaml").unlink()
    _write(knowledge / "people_profiles.yaml", {"schema_version": "1.0", "profiles": []})
    other = root / "programs" / "fabrikam"
    shutil.copytree(program, other)
    other_config = dict(
        config, id="fabrikam", name="Synthetic Fabrikam",
        communication_plan=[{"edition": "fabrikam_weekly", "cadence": "weekly"}],
    )
    _write(other / "program.yaml", other_config)
    shutil.rmtree(other / "editions")
    (other / "editions").mkdir()
    _write(other / "editions" / "fabrikam_weekly.yaml", dict(
        edition, id="fabrikam_weekly", program_id="fabrikam", type="narrative",
    ))
    (other / "chapter_contract.yaml").unlink()
    archive = program / "archive" / "acme_weekly"
    archive.mkdir(parents=True)
    (archive / "index.json").write_text(json.dumps({
        "edition": "acme_weekly",
        "issues": [{
            "issue_number": 1,
            "generated_at": "2026-05-20T12:00:00+00:00",
            "kind": "confirmed",
            "snapshot_path": None,
            "html_path": None,
            "md_path": None,
            "manifest_path": None,
        }],
    }), encoding="utf-8")
    return root


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def stage_composition_workspace(
    source_root: Path, workspace_root: Path, *, edition_names: tuple[str, ...],
) -> Path:
    reports_root = stage_v2_report_workspace(source_root, workspace_root, edition_names=edition_names)
    program = workspace_root / "programs" / "acme"
    scorecards_path = program / "scorecards.yaml"
    scorecards = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    dimensions = scorecards["scorecards"][0]["dimensions"]
    dimensions.append(dict(dimensions[0], name="Deployment Frequency"))
    _write(scorecards_path, scorecards)
    slices_path = program / "slice_contracts.yaml"
    slices = yaml.safe_load(slices_path.read_text(encoding="utf-8"))
    slices["slices"].append(dict(
        slices["slices"][0], id="acme.deployment_frequency", title="Deployment Frequency",
    ))
    _write(slices_path, slices)
    chapters_path = program / "chapter_contract.yaml"
    chapters = yaml.safe_load(chapters_path.read_text(encoding="utf-8"))
    chapters["chapters"].append(dict(
        chapters["chapters"][0], id="frequency", title="Frequency", priority=2,
        dimensions=["acme.deployment_frequency"],
    ))
    _write(chapters_path, chapters)
    return reports_root


def write_pending_kusto_capability(programs_root: Path) -> None:
    _write(programs_root / "acme" / "capability_status.yaml", {
        "schema_version": "1.0",
        "capabilities": [{
            "id": "kusto_activation",
            "status": "in_progress",
            "summary": "Synthetic Kusto activation is pending.",
            "degradation": "Live cluster validation is incomplete.",
            "last_reviewed_on": "2026-05-17",
        }],
    })


def write_pending_kusto_frontier(knowledge_root: Path) -> None:
    path = knowledge_root / "golden_queries.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for query in document["queries"]:
        if query.get("id") in {"velocity-p50", "icm-mttr"}:
            query.update(validated=False, refresh_on_gather=True)
        if query.get("id") == "velocity-p50":
            query["metric_id"] = "acme.velocity_p50"
    _write(path, document)
    (knowledge_root / "metrics").mkdir()
    _write(knowledge_root / "metrics" / "acme.yaml", {"metrics": [{
        "id": "acme.velocity_p50", "title": "Velocity p50", "unit": "days",
        "aggregation": "last", "slo_target": 5, "slo_direction": "lte",
    }]})
