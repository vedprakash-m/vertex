"""D-17 (specs/armada.md): tests for ``_pin_gather_run_lineage`` -- the
function that resolves the latest committed gather run exactly once per
report invocation, immediately after ``ResolutionStage``, and pins its
``run_id``/``manifest_hash`` onto ``StageContext`` so every downstream stage
that builds a ``RunManifest`` or ``DraftState`` (standard or lookback path)
stamps the same identical ``gather_run_id``/``gather_run_hash`` pair.

Unlike ``tests/unit/test_commands_report.py``, this module has no dependency
on the private ``editions/``/``programs/acme`` fixture data (module-skipped
in sandboxes without it) -- it exercises ``_pin_gather_run_lineage`` and the
``RunManifest``/``build_run_manifest`` plumbing directly against a real
committed gather-run manifest written to a ``tmp_path`` programs root.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone
from pathlib import Path

from src.commands.report import _pin_gather_run_lineage
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
)
from src.core.ledger.ulid import new_ulid
from src.core.manifest_writer import build_run_manifest
from src.core.models import Snapshot, EditionType
from src.core.pipeline import StageContext


def _commit_gather_run(programs_root: Path, *, program_id: str = "acme") -> GatherRunManifest:
    started_at = datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc)
    staging_manifest = GatherRunManifest(
        run_id=f"gather-{new_ulid(started_at)}",
        status=GatherRunStatus.RUNNING,
        program_id=program_id,
        actor_identity_type="interactive",
        lease_owner="test-runner",
        lease_fencing_token=1,
        started_at=started_at,
        scope_as_of=started_at,
        required_scope_status=RequiredScopeStatus.FULL,
    )
    create_staging_manifest(staging_manifest, programs_root=programs_root)
    return commit_staging_run(
        staging_manifest,
        finished_at=datetime(2026, 5, 4, 8, 5, tzinfo=timezone.utc),
        programs_root=programs_root,
    )


def _fake_resolved_v2(program_id: str) -> object:
    return types.SimpleNamespace(program=types.SimpleNamespace(id=program_id))


def test_pin_gather_run_lineage_stamps_ctx_from_latest_committed_run(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    committed = _commit_gather_run(programs_root)

    ctx = StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("acme"))
    pinned = _pin_gather_run_lineage(ctx)

    assert pinned.gather_run_id == committed.run_id
    assert pinned.gather_run_hash == committed.manifest_hash


def test_pin_gather_run_lineage_noop_when_resolved_v2_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _commit_gather_run(programs_root)

    ctx = StageContext(programs_root=programs_root, resolved_v2=None)
    pinned = _pin_gather_run_lineage(ctx)

    assert pinned is ctx
    assert pinned.gather_run_id is None
    assert pinned.gather_run_hash is None


def test_pin_gather_run_lineage_noop_when_no_committed_run_exists(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    ctx = StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("acme"))
    pinned = _pin_gather_run_lineage(ctx)

    assert pinned is ctx
    assert pinned.gather_run_id is None
    assert pinned.gather_run_hash is None


def test_pin_gather_run_lineage_does_not_leak_across_programs(tmp_path: Path) -> None:
    """Two programs sharing a programs_root must each resolve their own
    latest committed run, never cross-contaminating gather_run_id."""
    programs_root = tmp_path / "programs"
    acme_committed = _commit_gather_run(programs_root, program_id="acme")
    nova_committed = _commit_gather_run(programs_root, program_id="nova")
    assert acme_committed.run_id != nova_committed.run_id

    acme_ctx = _pin_gather_run_lineage(StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("acme")))
    nova_ctx = _pin_gather_run_lineage(StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("nova")))

    assert acme_ctx.gather_run_id == acme_committed.run_id
    assert nova_ctx.gather_run_id == nova_committed.run_id


def _sample_snapshot() -> Snapshot:
    return Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )


def test_build_run_manifest_threads_gather_run_lineage_fields() -> None:
    manifest = build_run_manifest(
        manifest_id="manifest-1",
        issue_number=1,
        edition="acme_weekly",
        started_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 18, 5, tzinfo=timezone.utc),
        config_payload={},
        snapshot=_sample_snapshot(),
        html_content="<html></html>",
        markdown_content="# report",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={},
        git_sha=None,
        gather_run_id="gather-01ABC",
        gather_run_hash="sha256:deadbeef",
    )

    assert manifest.gather_run_id == "gather-01ABC"
    assert manifest.gather_run_hash == "sha256:deadbeef"


def test_build_run_manifest_defaults_gather_run_lineage_to_none() -> None:
    manifest = build_run_manifest(
        manifest_id="manifest-1",
        issue_number=1,
        edition="acme_weekly",
        started_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 18, 5, tzinfo=timezone.utc),
        config_payload={},
        snapshot=_sample_snapshot(),
        html_content="<html></html>",
        markdown_content="# report",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={},
        git_sha=None,
    )

    assert manifest.gather_run_id is None
    assert manifest.gather_run_hash is None
