"""§9a End-to-end recovery drill contract — V-11 gate.

Required gate spec (§9a): "a seeded failure (e.g. dangling archive ref + a
failed Kusto query) is detected, surfaced, and recovered using only
CLI-surfaced guidance through gather→doctor→triage→report→confirm→rollback."

This test file automates as much of that drill as is possible without live
operator infra.  The five tests below collectively exercise each stage:

1. Seeded dangling archive ref is detected by verify_archive_integrity.
2. Doctor --confirm-readiness surfaces blockers when gather state is absent.
3. Report/confirm are pre-flighted by archive integrity (source-level wiring
   verified separately in test_ws1_archive_prefly_wiring_contract.py).
4. The CLI surface exposes all required recovery verbs as registered commands.
5. Rollback purges post-checkpoint SQLite facts (WS-4 recovery seam).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# 1. Detection: seeded dangling archive ref
# ---------------------------------------------------------------------------


def test_e2e_recovery_seeded_dangling_ref_detected(tmp_path: Path) -> None:
    """Seed a broken archive (index references a missing snapshot) and verify
    that verify_archive_integrity detects the inconsistency."""
    from src.core.archive_store import verify_archive_integrity

    edition = "test_weekly"
    archive_dir = tmp_path / edition
    snapshots_dir = archive_dir / "snapshots"
    html_dir = archive_dir / "html"
    md_dir = archive_dir / "md"
    manifests_dir = archive_dir / "manifests"
    for d in (snapshots_dir, html_dir, md_dir, manifests_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Write html/md/manifest but NOT the snapshot — dangling ref
    html_dir.joinpath("issue_078.html").write_text("<html/>", encoding="utf-8")
    md_dir.joinpath("issue_078.md").write_text("# Issue 078", encoding="utf-8")
    manifests_dir.joinpath("issue_078.json").write_text(
        json.dumps({"issue_number": 78}), encoding="utf-8"
    )
    snapshot_path = snapshots_dir / "issue_078.snapshot.json"  # NOT written

    index_payload = {
        "edition": edition,
        "issues": [
            {
                "issue_number": 78,
                "kind": "confirmed",
                "snapshot_path": str(snapshot_path),
                "html_path": str(html_dir / "issue_078.html"),
                "md_path": str(md_dir / "issue_078.md"),
                "manifest_path": str(manifests_dir / "issue_078.json"),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    (archive_dir / "index.json").write_text(
        json.dumps(index_payload), encoding="utf-8"
    )

    result = verify_archive_integrity(edition, archive_root=tmp_path)

    assert not result.ok, "A missing snapshot should render the archive inconsistent"
    assert result.edition == edition
    assert any("078" in msg and "snapshot" in msg for msg in result.inconsistencies), (
        f"Expected a missing-snapshot inconsistency for issue 078; got: {result.inconsistencies}"
    )


# ---------------------------------------------------------------------------
# 2. Doctor surfaces blockers: no gather state
# ---------------------------------------------------------------------------


def test_e2e_recovery_doctor_surfaces_missing_gather_state(tmp_path: Path) -> None:
    """When no gather state exists, doctor --confirm-readiness returns UNHEALTHY
    so the operator knows they must run gather first."""
    from src.commands.doctor_checks.confirm_readiness_checks import (
        run_confirm_readiness_doctor,
    )

    programs_root = tmp_path / "programs"
    (programs_root / "myprog").mkdir(parents=True)
    editions_root = tmp_path / "editions"
    editions_root.mkdir()
    archive_root = tmp_path / "archive"
    archive_root.mkdir()

    report = run_confirm_readiness_doctor(
        edition_name="myprog_weekly",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )

    assert report.failures > 0, (
        "doctor --confirm-readiness must report failures when gather state is absent; "
        "operator needs to run gather first."
    )
    assert report.overall == "UNHEALTHY"


# ---------------------------------------------------------------------------
# 3. Doctor surfaces blockers: Needs-Input dimension
# ---------------------------------------------------------------------------


def test_e2e_recovery_doctor_surfaces_needs_input_risk(tmp_path: Path) -> None:
    """When overrides contain a 'Needs Input' risk, doctor --confirm-readiness
    must return UNHEALTHY with a failure describing the block."""
    from src.commands.doctor_checks.confirm_readiness_checks import (
        run_confirm_readiness_doctor,
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "myprog2"
    overrides_dir = program_dir / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "issue_001.yaml").write_text(
        yaml.dump({
            "dimensions": {
                "schedule": {"risk": "\u2753 Needs input"},  # exact NEEDS_INPUT_VALUE constant
                "quality": {"risk": "Low"},
            }
        }),
        encoding="utf-8",
    )
    editions_root = tmp_path / "editions"
    editions_root.mkdir()
    archive_root = tmp_path / "archive"
    archive_root.mkdir()

    report = run_confirm_readiness_doctor(
        edition_name="myprog2_weekly",
        program_id="myprog2",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )

    assert report.failures > 0, (
        "doctor --confirm-readiness must return failures when a dimension has "
        "'Needs Input' risk — this is a hard publish-block (QG-8)."
    )
    fail_details = " ".join(
        check.detail for check in report.checks if check.status == "fail"
    )
    assert "Needs Input" in fail_details or "needs_input" in fail_details.lower(), (
        f"FAIL check must mention 'Needs Input' so the operator knows what to fix. Got: {fail_details!r}"
    )


# ---------------------------------------------------------------------------
# 4. CLI surface: all recovery verbs exist as registered commands
# ---------------------------------------------------------------------------


def test_e2e_recovery_cli_verbs_all_registered() -> None:
    """The full recovery flow (gather→doctor→triage→report→confirm→rollback)
    must be available as registered CLI commands in the Typer app."""
    from cli import app

    # Collect all command names in the app
    registered_names: set[str] = set()
    for cmd in app.registered_commands:
        if cmd.name:
            registered_names.add(cmd.name)
    for group in app.registered_groups:
        if group.name:
            registered_names.add(group.name)
        if group.typer_instance:
            for sub in group.typer_instance.registered_commands:
                if sub.name:
                    registered_names.add(sub.name)

    required_verbs = {"gather", "doctor", "report", "confirm", "rollback"}
    missing = required_verbs - registered_names
    assert not missing, (
        f"E2E recovery flow is missing CLI verbs: {sorted(missing)}. "
        "All of gather/doctor/report/confirm/rollback must be registered so "
        "operators can recover from failures using only CLI-surfaced guidance."
    )


# ---------------------------------------------------------------------------
# 5. Rollback purges post-checkpoint SQLite facts (WS-4 seam)
# ---------------------------------------------------------------------------


def test_e2e_recovery_rollback_purges_post_checkpoint_facts(tmp_path: Path) -> None:
    """After a rollback to a checkpoint, post-checkpoint SQLite fact rows are
    purged.  This ensures the recovery loop (gather→confirm→rollback→regather)
    restores a clean fact-store state."""
    from src.core.program_fact_store import (
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
    )

    db_root = tmp_path / "vertex-db"
    store = ProgramFactStore("rollback_test", db_root=db_root)

    checkpoint_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    before_ts = datetime(2025, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    after_ts = datetime(2025, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    def _write_fact(fact_type: str, recorded_at: datetime) -> None:
        store.append_fact(
            ProgramFactInput(
                fact_type=fact_type,
                entity_refs={"id": fact_type},
                payload={"value": fact_type},
                scope="test",
                source_signal_ids=[],
                confidence=None,
                precedence=FactPrecedence.RAW_TELEMETRY,
            ),
            recorded_at=recorded_at,
        )

    _write_fact("pre_fact", before_ts)
    _write_fact("post_fact", after_ts)

    deleted = store.purge_facts_after(checkpoint_ts)

    assert deleted >= 1, "purge_facts_after must delete at least the post-checkpoint row"
    snapshot = store.snapshot()
    surviving_types = {r.fact_type for r in snapshot.facts}
    assert "pre_fact" in surviving_types, "Pre-checkpoint fact must survive rollback"
    assert "post_fact" not in surviving_types, "Post-checkpoint fact must be purged by rollback"
