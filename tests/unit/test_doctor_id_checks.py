from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.commands.doctor_checks.id_checks import (
    build_slice_anchor_checks,
    build_slice_crosswalk_checks,
    load_dependency_workstream_ids,
    load_program_edition_ids,
    load_scorecard_dimension_bindings,
)
from src.core.workstream_registry import WorkstreamRegistryEntry
from src.core.exceptions import ConfigError
from tests.support.slice_contract_fixtures import build_test_ado_source_contract, build_test_slice_contract


def test_load_program_edition_ids_ignores_invalid_documents(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    editions_root.mkdir()
    (editions_root / "demo_weekly.yaml").write_text("id: demo_weekly\nprogram_id: demo\n", encoding="utf-8")
    (editions_root / "demo_monthly.yaml").write_text("id: demo_monthly\nprogram_id: demo\n", encoding="utf-8")
    (editions_root / "other.yaml").write_text("id: other_weekly\nprogram_id: other\n", encoding="utf-8")
    (editions_root / "missing_id.yaml").write_text("program_id: demo\n", encoding="utf-8")
    (editions_root / "invalid.yaml").write_text("[\n", encoding="utf-8")

    assert load_program_edition_ids("demo", editions_root=editions_root) == ("demo_monthly", "demo_weekly")


def test_load_dependency_workstream_ids_reads_current_workstreams(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "workstreams.yaml").write_text(
        "workstreams:\n  - id: ws-beta\n    name: Beta\n  - id: ws-alpha\n    name: Alpha\n",
        encoding="utf-8",
    )

    assert load_dependency_workstream_ids("demo", programs_root=programs_root) == ("ws-alpha", "ws-beta")


def test_load_scorecard_dimension_bindings_requires_workstream_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "scorecards.yaml").write_text(
        "scorecards:\n"
        "  - name: Health\n"
        "    dimensions:\n"
        "      - name: Delivery\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="is missing workstream_id"):
        load_scorecard_dimension_bindings("demo", programs_root=programs_root)


def test_slice_anchor_check_flags_explicit_ids_without_saved_queries() -> None:
    """Armada spec D-1/§1.4: explicit IDs alone must no longer satisfy the anchor check."""
    ado = build_test_ado_source_contract(saved_queries=(), explicit_work_item_ids=(123,))
    contract = build_test_slice_contract(ado=ado)

    checks = build_slice_anchor_checks([contract], as_of=date(2026, 7, 25))

    assert len(checks) == 1
    assert checks[0].status == "warn"
    assert "scope completeness" in checks[0].detail


def test_slice_anchor_check_passes_with_saved_queries_regardless_of_explicit_ids() -> None:
    ado = build_test_ado_source_contract(saved_queries=("query-1",), explicit_work_item_ids=(123,))
    contract = build_test_slice_contract(ado=ado)

    checks = build_slice_anchor_checks([contract], as_of=date(2026, 7, 25))

    assert checks == ()


def test_slice_anchor_check_accepts_declared_exception_with_valid_expiry() -> None:
    ado = build_test_ado_source_contract(
        saved_queries=(),
        explicit_work_item_ids=(123,),
        intentional_filter_only=True,
        intentional_filter_only_expires_on=date(2099, 1, 1),
    )
    contract = build_test_slice_contract(ado=ado)

    checks = build_slice_anchor_checks([contract], as_of=date(2026, 7, 25))

    assert checks == ()


def test_slice_anchor_check_flags_expired_declared_exception() -> None:
    ado = build_test_ado_source_contract(
        saved_queries=(),
        explicit_work_item_ids=(123,),
        intentional_filter_only=True,
        intentional_filter_only_expires_on=date(2020, 1, 1),
    )
    contract = build_test_slice_contract(ado=ado)

    checks = build_slice_anchor_checks([contract], as_of=date(2026, 7, 25))

    assert len(checks) == 1
    assert checks[0].status == "warn"
    assert "expired" in checks[0].detail


def test_slice_anchor_check_flags_missing_exception_expiry() -> None:
    ado = build_test_ado_source_contract(
        saved_queries=(),
        explicit_work_item_ids=(123,),
        intentional_filter_only=True,
        intentional_filter_only_expires_on=None,
    )
    contract = build_test_slice_contract(ado=ado)

    checks = build_slice_anchor_checks([contract], as_of=date(2026, 7, 25))

    assert len(checks) == 1
    assert checks[0].status == "warn"
    assert "missing intentional_filter_only_expires_on" in checks[0].detail


def test_slice_anchor_check_skips_non_ado_primary_slices_without_saved_queries() -> None:
    ado = build_test_ado_source_contract(saved_queries=(), explicit_work_item_ids=())
    contract = build_test_slice_contract(source_of_truth="telemetry_primary", ado=ado)

    checks = build_slice_anchor_checks([contract], as_of=date(2026, 7, 25))

    assert checks == ()


def test_slice_crosswalk_requires_bound_lane_to_explicitly_name_owning_slice() -> None:
    """D-4 rejects an implicit/ID-coincidence lane-to-slice join."""
    from src.core.slice_contract_loader import SavedQueryBinding
    from dataclasses import replace

    ado = build_test_ado_source_contract(saved_queries=("query-1",))
    ado = replace(
        ado,
        saved_query_bindings=(SavedQueryBinding("current", "query-1", "full_scope", lane_ids=("lane-a",)),),
    )
    contract = build_test_slice_contract(ado=ado)
    lane = WorkstreamRegistryEntry(id="lane-a", name="Lane A", lifecycle_state="active", source_slice_ids=("other.slice",))

    checks = build_slice_crosswalk_checks([contract], [lane], known_workstream_ids=set())

    assert len(checks) == 1
    assert checks[0].status == "warn"
    assert "source_slice_ids" in checks[0].detail


def test_slice_crosswalk_accepts_explicit_binding_lane_mapping() -> None:
    from src.core.slice_contract_loader import SavedQueryBinding
    from dataclasses import replace

    ado = build_test_ado_source_contract(saved_queries=("query-1",))
    ado = replace(
        ado,
        saved_query_bindings=(SavedQueryBinding("current", "query-1", "full_scope", lane_ids=("lane-a",)),),
    )
    contract = build_test_slice_contract(ado=ado)
    lane = WorkstreamRegistryEntry(id="lane-a", name="Lane A", lifecycle_state="active", source_slice_ids=(contract.id,))

    assert build_slice_crosswalk_checks([contract], [lane], known_workstream_ids=set()) == ()
