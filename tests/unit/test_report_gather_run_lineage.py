"""D-17 (synthetic gather lifecycle contract): tests for ``_pin_gather_run_lineage`` -- the
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

import json
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.commands.report import _pin_gather_run_lineage
from src.core.exceptions import ConfigError
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    canonical_json,
    commit_staging_run,
    compute_manifest_hash,
    create_staging_manifest,
    get_committed_run_dir,
    get_latest_pointer_path,
    read_manifest,
    write_manifest,
)
from src.core.ledger.ulid import new_ulid
from src.core.manifest_writer import build_run_manifest, hash_content
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
    example_committed = _commit_gather_run(programs_root, program_id="contoso")
    assert acme_committed.run_id != example_committed.run_id

    acme_ctx = _pin_gather_run_lineage(StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("acme")))
    example_ctx = _pin_gather_run_lineage(StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("contoso")))

    assert acme_ctx.gather_run_id == acme_committed.run_id
    assert example_ctx.gather_run_id == example_committed.run_id


def _two_committed_runs(programs_root: Path) -> tuple[GatherRunManifest, GatherRunManifest]:
    older = _commit_gather_run(programs_root, program_id="synthetic")
    newest = _commit_gather_run(programs_root, program_id="synthetic")
    assert newest.finished_at is not None
    newest = replace(newest, finished_at=newest.finished_at + timedelta(minutes=5))
    newest = replace(newest, manifest_hash=compute_manifest_hash(newest))
    run_dir = get_committed_run_dir("synthetic", newest.run_id, programs_root=programs_root)
    write_manifest(run_dir, newest)
    get_latest_pointer_path("synthetic", programs_root=programs_root).write_text(
        json.dumps({"run_id": newest.run_id, "manifest_hash": newest.manifest_hash}),
        encoding="utf-8",
    )
    return older, newest


@pytest.mark.parametrize("selection", ["pointer", "scan"])
@pytest.mark.parametrize(
    "corruption",
    ["foreign_program", "foreign_run", "running", "failed", "unsupported_schema",
     "missing_schema", "null_schema", "unreadable", "missing_manifest", "hash"],
)
def test_pin_gather_run_lineage_rejects_corrupt_selected_evidence(
    tmp_path: Path, selection: str, corruption: str,
) -> None:
    programs_root = tmp_path / "programs"
    older, newest = _two_committed_runs(programs_root)
    run_dir = get_committed_run_dir("synthetic", newest.run_id, programs_root=programs_root)
    path = run_dir / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    field, value = {
        "foreign_program": ("program_id", "foreign"),
        "foreign_run": ("run_id", "gather-foreign"),
        "running": ("status", GatherRunStatus.RUNNING.value),
        "failed": ("status", GatherRunStatus.FAILED.value),
        "unsupported_schema": ("schema_version", "gather-run.v999"),
        "missing_schema": ("schema_version", None),
        "null_schema": ("schema_version", None),
        "unreadable": ("schema_version", "gather-run.v1"),
        "missing_manifest": ("schema_version", "gather-run.v1"),
        "hash": ("schema_version", "gather-run.v1"),
    }[corruption]
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    changed = read_manifest(run_dir)
    changed = replace(changed, manifest_hash=compute_manifest_hash(changed))
    write_manifest(run_dir, changed)
    assert compute_manifest_hash(read_manifest(run_dir)) == changed.manifest_hash
    selected_hash = changed.manifest_hash
    if corruption == "missing_schema":
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["schema_version"]
        del payload["manifest_hash"]
        payload.pop("legacy_cutoff_at", None)
        selected_hash = hash_content(canonical_json(payload))
        payload["manifest_hash"] = selected_hash
        path.write_text(json.dumps(payload), encoding="utf-8")
    pointer = get_latest_pointer_path("synthetic", programs_root=programs_root)
    pointer.write_text(
        json.dumps({"run_id": newest.run_id, "manifest_hash": selected_hash}),
        encoding="utf-8",
    )
    if selection == "scan":
        pointer.unlink()
    if corruption == "unreadable":
        path.write_text("{invalid json", encoding="utf-8")
    elif corruption == "missing_manifest":
        path.unlink()
    elif corruption == "hash":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["program_id"] = "tampered-without-rehash"
        path.write_text(json.dumps(payload), encoding="utf-8")

    ctx = StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("synthetic"))
    if selection == "scan" and corruption in {"running", "failed"}:
        pinned = _pin_gather_run_lineage(ctx)
        assert pinned.gather_run_id == older.run_id
        assert pinned.gather_run_hash == older.manifest_hash
        return
    with pytest.raises(ConfigError, match="(?i)committed gather"):
        pinned = _pin_gather_run_lineage(ctx)
        pytest.fail(
            f"{selection}/{corruption}: report pinned {pinned.gather_run_id!r}; "
            f"older={older.run_id!r}, selected directory={newest.run_id!r}, "
            f"embedded={changed.run_id!r}"
        )


@pytest.mark.parametrize(
    "selection", ["pointer", "missing_pointer", "corrupt_pointer", "invalid_shape",
                  "missing_key", "stale_run", "stale_hash"],
)
def test_pin_gather_run_lineage_preserves_latest_valid_selection(
    tmp_path: Path, selection: str,
) -> None:
    programs_root = tmp_path / "programs"
    older, newest = _two_committed_runs(programs_root)
    assert older.run_id != newest.run_id
    pointer = get_latest_pointer_path("synthetic", programs_root=programs_root)
    if selection == "missing_pointer":
        pointer.unlink()
    elif selection == "corrupt_pointer":
        pointer.write_text("{invalid json", encoding="utf-8")
    elif selection == "invalid_shape":
        pointer.write_text("[]", encoding="utf-8")
    elif selection == "missing_key":
        pointer.write_text("{}", encoding="utf-8")
    elif selection == "stale_run":
        pointer.write_text(
            json.dumps({"run_id": "gather-absent", "manifest_hash": newest.manifest_hash}),
            encoding="utf-8",
        )
    elif selection == "stale_hash":
        pointer.write_text(
            json.dumps({"run_id": newest.run_id, "manifest_hash": "stale-pointer"}),
            encoding="utf-8",
        )

    pinned = _pin_gather_run_lineage(
        StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("synthetic")),
    )
    assert pinned.gather_run_id == newest.run_id
    assert pinned.gather_run_hash == newest.manifest_hash


@pytest.mark.parametrize("status", ["running", "failed", "quarantined"])
def test_pin_gather_run_lineage_scan_ignores_incomplete_noncommitted_runs(
    tmp_path: Path, status: str,
) -> None:
    programs_root = tmp_path / "programs"
    _, newest = _two_committed_runs(programs_root)
    incomplete_dir = get_committed_run_dir("synthetic", "incomplete", programs_root=programs_root)
    incomplete_dir.mkdir()
    (incomplete_dir / "manifest.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    get_latest_pointer_path("synthetic", programs_root=programs_root).unlink()
    pinned = _pin_gather_run_lineage(
        StageContext(programs_root=programs_root, resolved_v2=_fake_resolved_v2("synthetic")),
    )
    assert pinned.gather_run_id == newest.run_id
    assert pinned.gather_run_hash == newest.manifest_hash


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
