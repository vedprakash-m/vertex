"""Track K regression tests, part 2 (specs/fix-data-flow.md §6.11 / PR-12):
gather-freshness check and the render-manifest SoR-consistency check — the
two remaining Track K deliverables closed after the multi-DB detection,
root-cause fix, and path-determinism test (covered in
`test_substrate_health_track_k.py`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.commands.doctor_checks.storage_checks import (
    _gather_freshness_check,
    _render_manifest_sor_consistency_check,
)
from src.core.fact_sor_state import save_fact_sor_state
from src.core.run_telemetry import RunTelemetryRecord, append_run_telemetry


NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Gather freshness check
# ---------------------------------------------------------------------------


def test_gather_freshness_warns_when_no_run_ever_recorded(tmp_path: Path) -> None:
    check = _gather_freshness_check("acme", programs_root=tmp_path / "programs")
    assert check.label == "Gather Freshness"
    assert check.status == "warn"


def test_gather_freshness_ok_for_recent_run(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    append_run_telemetry(
        RunTelemetryRecord(
            run_id="run-1",
            program_id="acme",
            started_at=recent - timedelta(minutes=5),
            finished_at=recent,
            wall_time_seconds=300.0,
        ),
        programs_root=programs_root,
    )

    check = _gather_freshness_check("acme", programs_root=programs_root)
    assert check.status == "ok"


def test_gather_freshness_warns_for_stale_run(tmp_path: Path) -> None:
    """Minimal failing input: the only recorded gather run finished 48h ago,
    exceeding the 24h freshness threshold."""
    programs_root = tmp_path / "programs"
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    append_run_telemetry(
        RunTelemetryRecord(
            run_id="run-1",
            program_id="acme",
            started_at=stale - timedelta(minutes=5),
            finished_at=stale,
            wall_time_seconds=300.0,
        ),
        programs_root=programs_root,
    )

    check = _gather_freshness_check("acme", programs_root=programs_root)
    assert check.status == "warn"
    assert check.metadata["age_hours"] > 24


# ---------------------------------------------------------------------------
# Render manifest SoR consistency check
# ---------------------------------------------------------------------------


def test_manifest_consistency_ok_when_no_confirmed_issue(tmp_path: Path) -> None:
    check = _render_manifest_sor_consistency_check(
        "acme", edition_name="acme_weekly", programs_root=tmp_path / "programs", latest_confirmed_issue_number=None,
    )
    assert check.status == "ok"


def test_manifest_consistency_ok_when_manifest_absent(tmp_path: Path) -> None:
    check = _render_manifest_sor_consistency_check(
        "acme", edition_name="acme_weekly", programs_root=tmp_path / "programs", latest_confirmed_issue_number=42,
    )
    assert check.status == "ok"
    assert "no render manifest found" in check.detail


def test_manifest_consistency_ok_when_recorded_path_matches_current(tmp_path: Path) -> None:
    from src.core.manifest_writer import get_manifest_path

    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "acme", mode="legacy", recorded_at=NOW, recorded_by="test", programs_root=programs_root,
    )
    manifest_path = get_manifest_path("acme_weekly", 42, programs_root=programs_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"metadata": {"family_read_paths": {"risk": "legacy", "milestone": "legacy"}}}),
        encoding="utf-8",
    )

    check = _render_manifest_sor_consistency_check(
        "acme", edition_name="acme_weekly", programs_root=programs_root, latest_confirmed_issue_number=42,
    )
    assert check.status == "ok"


def test_manifest_consistency_warns_on_mismatch(tmp_path: Path) -> None:
    """Minimal failing input: the manifest recorded 'legacy' for risk, but the
    program's current fact_store_sor.yaml now resolves `judgment` to
    `primary` -- the exact SoR-vs-actual-read-path divergence this check
    exists to make concretely queryable (§6.11's Design)."""
    from src.core.manifest_writer import get_manifest_path

    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "acme", mode="primary", recorded_at=NOW, recorded_by="test", programs_root=programs_root,
    )
    manifest_path = get_manifest_path("acme_weekly", 42, programs_root=programs_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"metadata": {"family_read_paths": {"risk": "legacy"}}}),
        encoding="utf-8",
    )

    check = _render_manifest_sor_consistency_check(
        "acme", edition_name="acme_weekly", programs_root=programs_root, latest_confirmed_issue_number=42,
    )
    assert check.status == "warn"
    assert "risk" in check.metadata["mismatches"]
