from __future__ import annotations

from datetime import date, datetime, timezone

from src.commands.gather_pipeline import dependency_stage
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, DependencyADOQuery, Program, Workstream, WorkstreamSignalSources


def test_load_dependency_program_items_reads_configured_area_paths_and_ids() -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                dependency_ado_queries=(
                    DependencyADOQuery(
                        label="OneDeploy stager",
                        area_path="One\\Azure Compute\\OneDeploy\\Stager",
                        resolution_path="cross_org_onedeploy",
                    ),
                    DependencyADOQuery(
                        label="SCHIE gap owners",
                        resolution_path="cross_org_compute_pf",
                        work_item_ids=(9001,),
                    ),
                )
            ),
        ),
    )
    captured_calls: list[tuple[str, object]] = []

    class _FakeDependencyADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def query_all(self, *, filter_expression: str, select_fields: tuple[str, ...]) -> list[dict[str, object]]:
            captured_calls.append(("query_all", filter_expression))
            captured_calls.append(("query_all_fields", select_fields))
            return [{"WorkItemId": 1001}]

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            captured_calls.append(("query_work_items_batch", tuple(work_item_ids)))
            captured_calls.append(("query_work_items_batch_fields", fields))
            return [
                {
                    "id": 1001,
                    "fields": {
                        "System.Id": 1001,
                        "System.WorkItemType": "Feature",
                        "System.Title": "OneDeploy staging blocker",
                        "System.State": "Active",
                        "System.AreaPath": "One\\Azure Compute\\OneDeploy\\Stager",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                        "System.ChangedDate": "2026-05-08T10:00:00+00:00",
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-01",
                    },
                },
                {
                    "id": 9001,
                    "fields": {
                        "System.Id": 9001,
                        "System.WorkItemType": "Feature",
                        "System.Title": "SCHIE owner action",
                        "System.State": "Active",
                        "System.AreaPath": "One\\Adventure\\Acme",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                        "System.ChangedDate": "2026-05-07T09:00:00+00:00",
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-01",
                    },
                },
            ]

    def _work_item_from_sources(**kwargs) -> WorkItem:
        batch_row = kwargs["batch_row"]
        fields = batch_row["fields"]
        return WorkItem(
            id=int(fields["System.Id"]),
            type=str(fields["System.WorkItemType"]),
            title=str(fields["System.Title"]),
            state=str(fields["System.State"]),
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path=str(fields["System.AreaPath"]),
            iteration_path=str(fields["System.IterationPath"]),
            target_date=date(2026, 5, 1),
            risk_level=RiskLevel.MEDIUM,
            tags=["acme"],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=kwargs["fetched_at"],
        )

    groups, ado_calls = dependency_stage.load_dependency_program_items(
        program,
        workstreams,
        datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ado_client_factory=_FakeDependencyADOClient,
        batch_fields=("System.Id", "System.Title"),
        work_item_from_sources_fn=_work_item_from_sources,
    )

    assert ado_calls == 3
    assert [(group.label, [item.id for item in group.items]) for group in groups] == [
        ("OneDeploy stager", [1001]),
        ("SCHIE gap owners", [9001]),
    ]
    assert groups[0].items[0].custom_fields["changed_date"] == "2026-05-08T10:00:00+00:00"
    assert "startswith(Area/AreaPath, 'One\\Azure Compute\\OneDeploy\\Stager')" in str(captured_calls[0][1])
    assert "ChangedDate ge 2026-04-26T08:00:00Z" in str(captured_calls[0][1])


def test_build_dependency_signals_records_frozen_query_state_history() -> None:
    dependency_item = WorkItem(
        id=4321,
        type="Feature",
        title="OneDeploy staging blocked",
        state="Active",
        assigned_to="Taylor",
        assigned_to_email="taylor@example.com",
        area_path="One\\Azure Compute\\OneDeploy\\Stager",
        iteration_path="One\\Sprint 24",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
        custom_fields={"changed_date": "2026-05-09T12:00:00+00:00"},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    group = dependency_stage._DependencyQueryItems(
        workstream_id="acme",
        label="OneDeploy stager",
        resolution_path="cross_org_onedeploy",
        items=(dependency_item,),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    state_sink: dict[str, dict[str, object]] = {}
    previous_state: dict[str, dict[str, object]] = {}

    for day in range(4):
        as_of = datetime(2026, 5, 10 + day, 8, 0, tzinfo=timezone.utc)
        dependency_stage.build_dependency_signals(
            (group,),
            program_id="acme",
            workstreams=workstreams,
            as_of=as_of,
            stale_warn_days=14,
            stale_block_days=30,
            freshness_signal_rule_ids={"FR-21", "FR-22", "FR-43", "FR-46"},
            query_state_sink=state_sink,
            previous_query_states=previous_state,
        )
        previous_state = dict(state_sink)

    query_state = state_sink["ado-dependency:acme:OneDeploy stager"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 1
    assert query_state["signal_count"] == 1
    assert query_state["dependency_label"] == "OneDeploy stager"
    assert query_state["resolution_path"] == "cross_org_onedeploy"
    assert query_state["data_freshness_ok"] is True
    assert query_state["value_last_4"] == [1.0, 1.0, 1.0, 1.0]
    assert query_state["value_frozen_warning"] is True
