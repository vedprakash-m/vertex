from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands.doctor import run_doctor
from src.core.action_tracker import get_actions_path
from src.core.claim_tracker import get_claims_path
from src.core.decision_register import get_decisions_path
from src.core.dependency_graph import get_dependencies_path
from src.core.fact_sor_state import save_fact_sor_state
from src.core.milestone_engine import get_milestones_path
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.risk_register_engine import get_risk_register_path
from tests.support.report_test_setup import stage_v2_report_workspace


def test_flip_status_reports_legacy_when_fact_store_is_empty(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)

    report = run_doctor(
        edition_name="acme_weekly",
        flip_status=True,
        reports_root=reports_root,
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        reality_db_root=tmp_path / "vertex-db",
    )

    check = report.checks[0]
    assert check.label == "Fact Store Flip"
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["accepted_revision_count"] == 0
    assert check.metadata["flip_status"] == "legacy"
    assert check.metadata["proposed_revision_count"] == 0
    assert check.metadata["snapshot_pin_count"] == 0


def test_flip_status_reports_dual_when_fact_store_rows_and_legacy_paths_coexist(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reality_db_root = tmp_path / "vertex-db"
    actions_path = get_actions_path("acme", programs_root)
    actions_path.parent.mkdir(parents=True, exist_ok=True)
    actions_path.write_text("", encoding="utf-8")
    _append_fact_revision(reality_db_root)

    report = run_doctor(
        edition_name="acme_weekly",
        flip_status=True,
        reports_root=reports_root,
        editions_root=tmp_path / "editions",
        programs_root=programs_root,
        reality_db_root=reality_db_root,
    )

    check = report.checks[0]
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["accepted_revision_count"] == 1
    assert check.metadata["flip_status"] == "dual"


def test_flip_status_reports_fact_store_when_no_legacy_mutable_paths_remain(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reality_db_root = tmp_path / "vertex-db"
    _remove_legacy_mutable_paths(programs_root)
    _append_fact_revision(reality_db_root)

    report = run_doctor(
        edition_name="acme_weekly",
        flip_status=True,
        reports_root=reports_root,
        editions_root=tmp_path / "editions",
        programs_root=programs_root,
        reality_db_root=reality_db_root,
    )

    check = report.checks[0]
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["accepted_revision_count"] == 1
    assert check.metadata["flip_status"] == "fact-store"
    assert check.metadata["legacy_mutable_paths"] == ()


def test_flip_status_surfaces_primary_sor_mode_and_disabled_shim(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    reality_db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    _append_fact_revision(reality_db_root)

    report = run_doctor(
        edition_name="acme_weekly",
        flip_status=True,
        reports_root=reports_root,
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        reality_db_root=reality_db_root,
    )

    check = report.checks[0]
    assert check.metadata is not None
    assert check.metadata["sor_mode"] == "primary"
    assert check.metadata["shim_mode"] == "disabled"
    assert "sor_mode=primary" in check.detail
    assert "shim_mode=disabled" in check.detail


def test_flip_status_uses_persisted_primary_sor_mode_when_env_is_unset(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    reality_db_root = tmp_path / "vertex-db"
    programs_root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    save_fact_sor_state(
        "acme",
        mode="primary",
        recorded_at=datetime(2026, 6, 7, 10, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        programs_root=programs_root,
    )
    _append_fact_revision(reality_db_root)

    report = run_doctor(
        edition_name="acme_weekly",
        flip_status=True,
        reports_root=reports_root,
        editions_root=tmp_path / "editions",
        programs_root=programs_root,
        reality_db_root=reality_db_root,
    )

    check = report.checks[0]
    assert check.metadata is not None
    assert check.metadata["sor_mode"] == "primary"
    assert check.metadata["shim_mode"] == "disabled"


def _append_fact_revision(reality_db_root: Path) -> None:
    store = ProgramFactStore("acme", db_root=reality_db_root)
    store.append_fact(
        ProgramFactInput(
            fact_type="action.item",
            entity_refs=("action:1",),
            payload={"summary": "Follow up on launch gate."},
            source_signal_ids=("signal-1",),
        ),
        recorded_at=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
    )


def _remove_legacy_mutable_paths(programs_root: Path) -> None:
    legacy_paths = (
        get_actions_path("acme", programs_root),
        get_claims_path("acme", programs_root),
        get_decisions_path("acme", programs_root),
        get_dependencies_path("acme", programs_root),
        get_milestones_path("acme", programs_root),
        get_risk_register_path("acme", programs_root),
    )
    for path in legacy_paths:
        if path.exists():
            path.unlink()