from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil
from uuid import NAMESPACE_URL, uuid5

import yaml
import pytest

from src.core.chapter_contract_loader import canonical_dimension_binding_id
from src.core.slice_contract_loader import SliceFilterDefinition, SlicePredicateDefinition, SliceTelemetryContract
from tests.support.slice_contract_fixtures import build_test_ado_source_contract, build_test_slice_contract
from tests.unit.test_config_loader import _write_demo_v2_layout


EDITION_NAME = "acme_weekly"
FULL_SCOPE_QUERY = str(uuid5(NAMESPACE_URL, "https://example.test/fixtures/report/full-scope"))
ACTIVITY_QUERY = str(uuid5(NAMESPACE_URL, "https://example.test/fixtures/report/activity"))
FOLLOWUP_QUERY = str(uuid5(NAMESPACE_URL, "https://example.test/fixtures/report/followup"))


@pytest.fixture
def synthetic_report_root(tmp_path: Path) -> Path:
    return build_synthetic_report_source(tmp_path / "synthetic-source")


def build_synthetic_report_source(root: Path) -> Path:
    """Construct generic report inputs without opening private program data."""
    editions, programs = _write_demo_v2_layout(root)
    program = programs / "acme"
    shutil.copytree(programs / "demo", program)
    shutil.rmtree(programs / "demo")
    shutil.copytree(editions, program / "editions")
    shutil.rmtree(editions)
    edition = yaml.safe_load((program / "editions" / "demo_weekly.yaml").read_text(encoding="utf-8"))
    edition.update(id=EDITION_NAME, program_id="acme", name="Acme Issue {issue_number}", forecast_enabled=False, layout_mode="continuity", cadence_note={"focused": "Focused weekly update", "detailed": "Detailed weekly update"})
    (program / "editions" / "demo_weekly.yaml").unlink()
    for name, kind, cadence in ((EDITION_NAME, "detailed", "weekly"), ("example_quarterly", "lookback", "quarterly"), ("example_daily", "condensed", "daily"), ("example_lt_deck", "deck", "weekly")):
        (program / "editions" / f"{name}.yaml").write_text(yaml.safe_dump(dict(edition, id=name, type=kind, cadence=cadence)), encoding="utf-8")
    payload = yaml.safe_load((program / "program.yaml").read_text(encoding="utf-8"))
    payload.update(id="acme", name="Acme", chapter_namespace="acme", key_dependencies=[])
    payload["ado"]["work_item_types"] = ["Feature", "Risk", "Scenario", "Bug"]
    payload["ado"].update(organization="your-org", project="ExampleProject")
    payload["kusto"]["enabled"] = True
    payload["vitality"] = {"surfaces": {"newsletter_aggregate": True, "newsletter_individual_praise": True}}
    (program / "program.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    workstreams = [
        dict(id="deployment_readiness", name="Deployment Readiness", area_paths=[r"ExampleProject\Acme\Deployment"]),
        dict(id="networking", name="Networking", area_paths=[r"ExampleProject\Contoso\Networking"]),
        dict(id="artifact_service", name="Artifact Service", area_paths=[r"ExampleProject\Fabrikam\Scenarios"]),
        dict(id="package_currency", name="Package Currency", area_paths=[r"ExampleProject\Acme\Packages"]),
        dict(id="diagnostics_surface", name="Diagnostics Surface", area_paths=[r"ExampleProject\Diagnostics"]),
        dict(id="acme", name="Acme", area_paths=[r"ExampleProject\Acme"]),
    ]
    (program / "workstreams.yaml").write_text(yaml.safe_dump({"schema_version": "2.0", "workstreams": workstreams}), encoding="utf-8")
    dimensions = [
        dict(name="Deployment Velocity", workstream_id="deployment_readiness", ado_filter="area_path contains 'Deployment'"),
        dict(name="Deployment Safety", workstream_id="deployment_readiness", ado_filter="area_path contains 'Deployment'"),
        dict(name="Networking", workstream_id="networking", ado_filter="area_path contains 'Networking'"),
        dict(name="Service Validation", workstream_id="artifact_service", ado_filter="area_path contains 'Scenarios'"),
        dict(name="Package Currency", workstream_id="package_currency", ado_filter="area_path contains 'Packages'"),
    ]
    scorecards = [dict(name="Example Release Readiness", dimensions=dimensions), dict(name="Example Service Readiness", dimensions=[dict(name=name, workstream_id="networking", ado_filter="area_path contains 'Contoso'") for name in ("Performance", "Safety", "Pilot Readiness")])]
    (program / "scorecards.yaml").write_text(yaml.safe_dump({"schema_version": "2.0", "scorecards": scorecards}), encoding="utf-8")
    contracts = [
        build_test_slice_contract(
            contract_id=canonical_dimension_binding_id(scorecard["name"], dimension["name"], chapter_namespace="acme"),
            scorecard_name=scorecard["name"], section=dimension["workstream_id"], workstream=dimension["workstream_id"],
            title=dimension["name"], ado=build_test_ado_source_contract(
                saved_queries=("synthetic-query",),
                filters=SliceFilterDefinition(any_of=(SlicePredicateDefinition("area_path", "contains", dimension["ado_filter"].split("'")[1]),)),
            ),
            telemetry=SliceTelemetryContract("velocity-p50", "weekly", 24, "medium", "warn", ()),
        )
        for scorecard in scorecards for dimension in scorecard["dimensions"]
    ]
    slices = [asdict(contract) for contract in contracts]
    for contract in slices:
        contract["decision_sources"] = contract["source_contract"].pop("decision_sources")
        contract["fallback_sources"] = contract["source_contract"].pop("fallback_sources")
        contract["source_contract"]["ado"]["saved_queries"] = [dict(binding_id=contract["id"], query_id="synthetic-query", mode="activity_delta")]
    slices[0]["source_contract"]["ado"]["saved_queries"] = [dict(binding_id="deployment", query_id=FULL_SCOPE_QUERY, mode="full_scope", filter={"any_of": [dict(field="title", op="contains", value="Deployment")]})]
    (program / "slice_contracts.yaml").write_text(yaml.safe_dump({"schema_version": "1.1", "slices": slices}), encoding="utf-8")
    queries = [
        dict(id=query_id, cluster="https://telemetry.example.test", database="synthetic", kql="Synthetic | take 1", section=section, render_as=mode, confidence="medium", program_ids=["acme"], validated=True, kusto_section_validates_slice=True)
        for query_id, section, mode in (
            ("velocity-p50", "Deployment Readiness", "metric_highlight"), ("fleet-health", "Fleet Health", "table"),
            ("icm-active", "Active Incidents", "table"), ("icm-mttr", "Incident MTTR", "metric_highlight"),
            ("readiness_observability_coverage", "Observability Coverage", "metric_highlight"),
            ("readiness_capacity_headroom", "Capacity Headroom", "metric_highlight"),
            ("readiness_dora_fail_rate", "Change Failure Rate", "metric_highlight"),
            ("artifact-service-coverage", "Artifact Service Coverage", "metric_highlight"),
        )
    ]
    (program / "knowledge" / "golden_queries.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "queries": queries}), encoding="utf-8")
    (program / "kpis.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "kpis": [dict(queries[0], id="acme-deployment-velocity", workstream_ids=["deployment_readiness"], label="Deploy P50 (mins)", unit="mins")]}), encoding="utf-8")
    (program / "review.yaml").write_text(yaml.safe_dump({"required": True, "reviewers": [dict(name="Reviewer", sections=["exec_summary"])]}), encoding="utf-8")
    editorial_path = program / "editorial_rules.yaml"
    editorial = yaml.safe_load(editorial_path.read_text(encoding="utf-8"))
    editorial["voice_contract"] = dict(applies_to_editions=[EDITION_NAME], program_tokens=["acme"], abstract_phrases=["materially narrower"])
    editorial_path.write_text(yaml.safe_dump(editorial), encoding="utf-8")
    chapters = [
        dict(id="deployment_readiness", title="Deployment Readiness", dimensions=["example_release_readiness.deployment_velocity", "example_release_readiness.deployment_safety"]),
        dict(id="network_delivery_checks", title="Network Delivery Checks", dimensions=["example_release_readiness.networking"]),
        dict(id="artifact_service", title="Artifact Service", dimensions=["example_release_readiness.service_validation"]),
        dict(id="package_currency", title="Package Currency", dimensions=["example_release_readiness.package_currency"]),
        dict(id="service_checks", title="Service Checks", dimensions=[canonical_dimension_binding_id("Example Service Readiness", name, chapter_namespace="acme") for name in ("Performance", "Safety")]),
        dict(id="networking", title="Networking", dimensions=["example_release_readiness.networking"]),
    ]
    for index, chapter in enumerate(chapters, 1):
        chapter.update(priority=index, include_in=["detailed", "focused", "condensed", "deck", "lookback"], show_in_jump_list=True)
        if chapter["id"] == "package_currency":
            chapter["include_in"].remove("focused")
        if chapter["id"] == "networking":
            chapter["include_in"] = ["focused"]
    (program / "chapter_contract.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "chapters": chapters}), encoding="utf-8")
    order = ["scorecards:all", "workstream:deployment_readiness", "exec_summary", "health", "workstream:network_delivery_checks", "selected_changes", "workstreams:all", "top_3", "kusto:all", "ado_vitality", "charter_review", "assumption_lifecycle", "incident_learning", "retrospective_intelligence"]
    families = {name: dict(order=order, mandatory=[], optional=order) for name in ("detailed", "focused", "condensed", "deck", "narrative", "lookback")}
    (program / "template_contract.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "edition_family": dict(default="detailed", allowed=list(families)), "families": families}), encoding="utf-8")
    (program / "milestones.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "milestones": [
        dict(id="m3-code-complete", name="M3 - Code Complete", target_date="2026-05-18", owner_alias="operator", status="on_track", linked_workstream_ids=["deployment_readiness"], linked_work_item_ids=[900001]),
        dict(id="m1-design", name="Design", target_date="2026-05-18", owner_alias="operator", status="on_track", linked_workstream_ids=["deployment_readiness"], linked_work_item_ids=[]),
        dict(id="m2-validation", name="Validation", target_date="2026-05-18", owner_alias="operator", status="on_track", linked_workstream_ids=["deployment_readiness"], linked_work_item_ids=[]),
    ]}), encoding="utf-8")
    (program / "dependencies.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "dependencies": [dict(id="design-to-code", from_workstream_id="deployment_readiness", to_workstream_id="deployment_readiness", from_milestone_id="m1-design", to_milestone_id="m3-code-complete", dependency_type="blocks", risk_if_broken="Code complete requires design", status="active")]}), encoding="utf-8")
    other = programs / "fabrikam"
    shutil.copytree(program, other)
    payload.update(id="fabrikam", name="Fabrikam Program Update")
    (other / "program.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    secondary_program_names = ["Queue Processing", "Buildouts", "Worker Service", "Request Validation", "Rollout Approval"]
    (other / "scorecards.yaml").write_text(yaml.safe_dump({"schema_version": "2.0", "scorecards": [dict(name="Fabrikam", dimensions=[dict(name=name, workstream_id="acme", ado_filter="area_path contains 'Fabrikam'") for name in secondary_program_names])]}), encoding="utf-8")
    (other / "chapter_contract.yaml").unlink()
    (other / "slice_contracts.yaml").unlink()
    shutil.rmtree(other / "editions")
    (other / "editions").mkdir()
    (other / "editions" / "fabrikam_weekly.yaml").write_text(yaml.safe_dump(dict(edition, id="fabrikam_weekly", program_id="fabrikam", name="Fabrikam Program Update", type="narrative", layout_mode="dashboard")), encoding="utf-8")
    checkout = Path(__file__).resolve().parents[2]
    shutil.copytree(checkout / "reports" / "schemas", root / "reports" / "schemas")
    shutil.copytree(checkout / "templates", root / "templates")
    return root
