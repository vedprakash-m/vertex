"""Nudge call-budget and deterministic performance contracts.

Spec: .archive/specs/fix-nudge.md §8.4, §8.6, §23.1, §23.3; gates NQ-11, NQ-17; AC-11, AC-14.

Two layers:

1. **CI-safe query-layer contracts** (run on fresh-clone CI — no local program data):
   - One WIQL call per tag/area section; zero for registry sections (NQ-11, AC-11).
   - Batch hydration issues exactly ``ceil(unique_ids / 200)`` calls per fetched set (§23.1).

2. **Staged orchestration contract** (skips on fresh-clone CI when local program data is
   absent, via ``stage_v2_report_workspace``'s built-in skip):
   - Comment calls never exceed ``comment_fetch_limit``; overflow items render as
     "not evaluated" (``None``), never as a hygiene failure (AC-14, NQ-17).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.nudge_models import (
    NUDGE_BATCH_SIZE,
    NudgeSectionCriteria,
    NudgeSectionSpec,
)
from src.core.nudge_query import fetch_section_candidates
from tests.support.report_test_setup import stage_v2_report_workspace


# ---------------------------------------------------------------------------
# Shared helpers (CI-safe — MagicMock program)
# ---------------------------------------------------------------------------


def _make_section(
    source: str = "tag",
    tags: tuple[str, ...] = ("RAMPP1",),
    area_path_filter: tuple[str, ...] = (),
    required_tags: tuple[str, ...] = (),
    legacy_scope_override: bool = False,
) -> NudgeSectionSpec:
    return NudgeSectionSpec(
        id="test_sec",
        title="Test",
        criteria=NudgeSectionCriteria(
            source=source,  # type: ignore[arg-type]
            tags=tags,
            area_path_filter=area_path_filter,
            required_tags=required_tags,
            legacy_scope_override=legacy_scope_override,
        ),
        stale_business_days=3,
        letter="A",
    )


def _make_program(
    area_paths: tuple[str, ...] = ("One\\Xstore",),
    work_item_types: tuple[str, ...] = (),
    excluded_states: tuple[str, ...] = (),
) -> Any:
    program = MagicMock()
    ado = MagicMock()
    ado.organization = "contoso"
    ado.project = "One"
    ado.area_paths = list(area_paths)
    ado.work_item_types = list(work_item_types)
    ado.excluded_states = list(excluded_states)
    ado.api_timeout_seconds = 30
    program.ado = ado
    return program


def _make_client_returning(ids: list[int]) -> Any:
    """A fake NudgeADOClient whose execute_wiql returns ``ids`` and hydrates each id."""
    client = MagicMock()
    client.execute_wiql.return_value = list(ids)

    def _hydrate(work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        del fields
        return [
            {
                "id": wid,
                "fields": {
                    "System.Id": wid,
                    "System.WorkItemType": "Feature",
                    "System.Title": f"Item {wid}",
                    "System.State": "Active",
                    "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                    "System.AreaPath": "One\\Xstore",
                    "System.ChangedDate": "2026-06-01T18:00:00+00:00",
                    "Microsoft.VSTS.Scheduling.TargetDate": "2026-06-30",
                    "System.Tags": "RAMPP1",
                    "Custom.RiskAssessment": "On Track",
                    "Custom.RiskAssessmentComment": "",
                    "System.Description": "desc",
                },
            }
            for wid in work_item_ids
        ]

    client.query_work_items_batch.side_effect = _hydrate
    return client


# ---------------------------------------------------------------------------
# NC-10: One WIQL call per tag/area section; zero for registry (NQ-11, AC-11)
# ---------------------------------------------------------------------------


def test_nc10_tag_section_issues_exactly_one_wiql_call() -> None:
    sec = _make_section(source="tag", tags=("RAMPP1", "RAMP P1"))
    client = _make_client_returning([940001, 940002])
    fetch_section_candidates(
        program=_make_program(),
        section=sec,
        authored_registry=(),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    assert client.execute_wiql.call_count == 1


def test_nc10_area_path_section_issues_exactly_one_wiql_call() -> None:
    sec = NudgeSectionSpec(
        id="area_sec",
        title="Area",
        criteria=NudgeSectionCriteria(source="area_path", area_path_filter=("One\\Xstore",)),
        stale_business_days=3,
        letter="B",
    )
    client = _make_client_returning([940010])
    fetch_section_candidates(
        program=_make_program(area_paths=("One\\Xstore",)),
        section=sec,
        authored_registry=(),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    assert client.execute_wiql.call_count == 1


def test_nc10_registry_section_issues_zero_wiql_calls() -> None:
    """Registry sections hydrate key_ado_items directly; they never execute WIQL."""
    registry_entry = MagicMock()
    registry_entry.id = "ws1"
    registry_entry.lifecycle_state = "active"
    registry_entry.key_ado_items = (940001, 940002)
    registry_entry.overdue_ado_item_ids = frozenset()

    client = _make_client_returning([940001, 940002])
    result = fetch_section_candidates(
        program=_make_program(),
        section=_make_section(source="registry"),
        authored_registry=(registry_entry,),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    assert client.execute_wiql.call_count == 0
    assert client.query_work_items_batch.call_count == 1  # one batch for 2 ids
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# NC-11: Batch hydration = ceil(unique_ids / 200) (§23.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n_ids, expected_batches",
    [
        (1, 1),
        (NUDGE_BATCH_SIZE, 1),          # exactly one full batch
        (NUDGE_BATCH_SIZE + 1, 2),       # one over → second batch
        (NUDGE_BATCH_SIZE * 2, 2),      # exactly two full batches
        (NUDGE_BATCH_SIZE * 2 + 50, 3),
    ],
)
def test_nc11_batch_hydation_ceil_over_batch_size(n_ids: int, expected_batches: int) -> None:
    ids = list(range(940001, 940001 + n_ids))
    sec = _make_section(source="tag", tags=("RAMPP1",))
    client = _make_client_returning(ids)
    fetch_section_candidates(
        program=_make_program(),
        section=sec,
        authored_registry=(),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    assert client.query_work_items_batch.call_count == expected_batches
    # Each batch must be no larger than NUDGE_BATCH_SIZE
    for call in client.query_work_items_batch.call_args_list:
        batch_ids = call.args[0]
        assert len(batch_ids) <= NUDGE_BATCH_SIZE


def test_nc11_empty_wiql_result_issues_zero_hydration_calls() -> None:
    sec = _make_section(source="tag", tags=("RAMPP1",))
    client = _make_client_returning([])
    fetch_section_candidates(
        program=_make_program(),
        section=sec,
        authored_registry=(),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    assert client.execute_wiql.call_count == 1
    assert client.query_work_items_batch.call_count == 0


# ---------------------------------------------------------------------------
# NC-12: Comment budget — staged orchestration (skips on fresh-clone CI)
# AC-14, NQ-17: comment calls ≤ comment_fetch_limit; overflow renders unknown.
# ---------------------------------------------------------------------------


class _CountingCommentClient:
    """Fake ADO client that counts ``list_work_item_comments`` calls class-wide.

    Registry/tag candidate hydration returns ``key_ado_items`` as active RAMPP1
    items so they survive cooldown/dedup/exempt filtering and reach comment fetch.
    """

    comment_calls: int = 0  # class-level counter shared across per-task + comment instances

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
        return []  # no tag-section items; all candidates come from registry Section A

    def query_work_items_batch(
        self, work_item_ids: list[int], fields: tuple[str, ...]
    ) -> list[dict[str, object]]:
        del fields
        return [
            {
                "id": wid,
                "fields": {
                    "System.Id": wid,
                    "System.WorkItemType": "Feature",
                    "System.Title": f"Registry item {wid}",
                    "System.State": "Active",
                    "System.AssignedTo": {
                        "displayName": "Registry Owner",
                        "uniqueName": "regowner@example.com",
                    },
                    "System.AreaPath": "One\\Adventure\\Acme",
                    "System.IterationPath": "FY26\\Sprint 20",
                    "System.ChangedDate": "2026-06-10T18:00:00+00:00",
                    "Microsoft.VSTS.Scheduling.TargetDate": "2026-06-30",
                    "System.Tags": "RAMPP1",
                    "Custom.RiskAssessment": "On Track",
                    "Custom.RiskAssessmentComment": "",
                    "System.Description": "Registry item description.",
                },
            }
            for wid in work_item_ids
        ]

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
        return []

    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, object]]:
        type(self).comment_calls += 1
        return []  # successful fetch, no recent comment → has_recent_comment=False (evaluated)


def test_nc12_comment_calls_capped_and_overflow_unknown(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    import yaml  # noqa: PLC0415

    from src.commands.nudge import generate_full_hygiene_nudges  # noqa: PLC0415

    # Stage the real nova program (has program.yaml + migrated nova_nudge edition).
    # Skips cleanly on fresh-clone CI where local program data is absent.
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("nova_nudge",), program_names=("nova",))
    programs_root = tmp_path / "programs"

    # Inject a SMALL comment_fetch_limit so the cap is exercised with few items,
    # and rewrite the recipient to a generic alias (the real edition pins a
    # personal alias).
    edition_path = programs_root / "nova" / "editions" / "nova_nudge.yaml"
    doc = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    fh = doc.setdefault("full_hygiene", {})
    fh["comment_fetch_limit"] = 5
    fh["recipient"] = "tpm"
    edition_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    # Resolve the generic recipient alias via the shared people directory. A
    # distinct alias (``tpm``) avoids colliding with the ``maintainer`` entry
    # staging writes (whose maintainer@example.com email _is_valid_email rejects).
    people_path = tmp_path / "knowledge" / "people_directory.yaml"
    people_doc = yaml.safe_load(people_path.read_text(encoding="utf-8"))
    people_doc.setdefault("people", []).append(
        {"alias": "tpm", "email": "tpm@microsoft.com", "display_name": "TPM Owner"}
    )
    people_path.write_text(yaml.safe_dump(people_doc, sort_keys=False), encoding="utf-8")

    # Registry with MORE eligible items than the comment cap. The nova `priority`
    # section is source=registry with no required_tags, so all 12 survive filtering.
    item_ids = list(range(900001, 900001 + 12))  # 12 items, cap is 5
    registry_path = programs_root / "nova" / "workstream_registry.yaml"
    registry = {
        "schema_version": "1.0",
        "workstreams": [
            {
                "id": "nova.test_ws",
                "name": "Test Workstream",
                "lifecycle_state": "active",
                "stakeholders": [{"name": "Maintainer", "role": "primary_owner"}],
                "key_ado_items": item_ids,
            }
        ],
    }
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")

    _CountingCommentClient.comment_calls = 0
    monkeypatch.setattr("src.commands.nudge.ADOClient", _CountingCommentClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="nova",
        dry_run=True,
        as_of=datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        candidate_workers=1,
    )

    # Comment calls never exceed the configured cap (AC-14 / NQ-17).
    assert _CountingCommentClient.comment_calls == 5, (
        f"Expected exactly 5 comment calls (== comment_fetch_limit), "
        f"got {_CountingCommentClient.comment_calls}"
    )

    # Overflow items must render as "not evaluated" (None), never as a failure (False).
    total_rows = sum(len(g.rows) for sec in artifacts.sections for g in sec.groups)
    evaluated = sum(
        1
        for sec in artifacts.sections
        for g in sec.groups
        for r in g.rows
        if r.has_recent_comment is False
    )
    unknown = sum(
        1
        for sec in artifacts.sections
        for g in sec.groups
        for r in g.rows
        if r.has_recent_comment is None
    )
    assert total_rows == 12, f"Expected 12 registry rows, got {total_rows}"
    assert evaluated == 5, f"Expected 5 evaluated comment rows, got {evaluated}"
    assert unknown == 7, f"Expected 7 not-evaluated rows, got {unknown}"