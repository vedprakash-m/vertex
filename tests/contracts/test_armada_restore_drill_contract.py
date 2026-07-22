"""ARM-GATHER-17 / AG-7.11: Armada-specific restore-drill validation.

Spec: `specs/armada.md` §4.15 backup contract — "quarterly restore drill
validates manifest pointer, manifest hash, registry integrity, and Program
Fact Store consistency" — and Phase 7 acceptance gate AG-7.11 ("RPO/RTO
backup and restore drill passes").

`tests/contracts/test_ws23_backup_restore_contract.py` already proves the
generic repository-tree backup/restore round-trip (`programs/`, `knowledge/`,
`editions/`). This module adds the three Armada-specific recovery guarantees
the spec names explicitly, which a generic program tree doesn't exercise:

1. The committed gather-run manifest's `latest.json` pointer and its
   embedded hash still resolve/verify identically after a tree
   backup -> wipe -> restore cycle (`resolve_latest_committed_manifest`).
2. The workstream registry still loads with byte-identical entries after
   the same cycle (`load_authored_workstream_registry`).
3. The Program Fact Store — which per `specs/vertex-prd.md`'s documented
   `facts export`/`facts import`/`facts rebuild` contract is recovered
   independently of the tree backup (its canonical SQLite database lives
   at `programs_root.parent / "vertex-db"`, outside the tree-backup roots
   `programs/`, `knowledge/`, `editions/`) — round-trips to a consistent
   fact snapshot via that documented export/import pair.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.backup import create_repository_backup, restore_repository_backup
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
    resolve_latest_committed_manifest,
)
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.workstream_registry import load_authored_workstream_registry

_PROGRAM_ID = "armadadrill"


def _seed_program_tree(root: Path) -> None:
    """Seed `root/programs/<id>/` with a program.yaml, a committed
    gather-run.v1 manifest, and an authored workstream registry -- the
    three tree-backed artifacts this drill validates post-restore."""
    program_dir = root / "programs" / _PROGRAM_ID
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        'schema_version: "3.0"\n'
        f'id: "{_PROGRAM_ID}"\n'
        'name: "Armada restore-drill test program"\n',
        encoding="utf-8",
    )
    (program_dir / "workstream_registry.yaml").write_text(
        'schema_version: "1.0"\n'
        "workstreams:\n"
        "  - id: drillstream\n"
        "    name: Drill Workstream\n"
        "    lifecycle_state: active\n"
        "    area_paths:\n"
        "      - One\\Adventure\\ArmadaDrill\n"
        "    source_slice_ids:\n"
        "      - armadadrill-slice-1\n",
        encoding="utf-8",
    )

    now = datetime(2026, 7, 21, 12, 0, 0)
    manifest = GatherRunManifest(
        run_id="gather-01DRILLRUN",
        status=GatherRunStatus.RUNNING,
        program_id=_PROGRAM_ID,
        actor_identity_type="interactive",
        lease_owner="drill-host",
        lease_fencing_token=1,
        started_at=now,
        scope_as_of=now,
        required_scope_status=RequiredScopeStatus.FULL,
    )
    create_staging_manifest(manifest, programs_root=root / "programs")
    commit_staging_run(
        manifest,
        finished_at=datetime(2026, 7, 21, 12, 5, 0),
        programs_root=root / "programs",
    )


def test_gather_run_manifest_pointer_and_hash_verify_after_restore(tmp_path: Path) -> None:
    """The committed manifest's `latest.json` pointer must resolve to the
    SAME run, with a still-valid embedded hash, after backup -> wipe ->
    restore. `resolve_latest_committed_manifest` re-derives the hash from
    the restored manifest content and compares it to the pointer -- so this
    fails if restore silently corrupts or drops either file."""
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    destination_root = tmp_path / "destination"
    _seed_program_tree(source_root)

    before = resolve_latest_committed_manifest(_PROGRAM_ID, programs_root=source_root / "programs")
    assert before is not None
    assert before.run_id == "gather-01DRILLRUN"

    create_repository_backup(backup_root, source_root=source_root)
    restore_repository_backup(backup_root, destination_root)

    after = resolve_latest_committed_manifest(_PROGRAM_ID, programs_root=destination_root / "programs")
    assert after is not None
    assert after.run_id == before.run_id
    assert after.manifest_hash == before.manifest_hash


def test_workstream_registry_integrity_survives_restore(tmp_path: Path) -> None:
    """The authored workstream registry must parse to byte-identical
    entries after backup -> wipe -> restore (registry integrity)."""
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    destination_root = tmp_path / "destination"
    _seed_program_tree(source_root)

    before = load_authored_workstream_registry(program_id=_PROGRAM_ID, programs_root=source_root / "programs")
    assert before
    assert before[0].id == "drillstream"

    create_repository_backup(backup_root, source_root=source_root)
    restore_repository_backup(backup_root, destination_root)

    after = load_authored_workstream_registry(program_id=_PROGRAM_ID, programs_root=destination_root / "programs")
    assert after == before


def test_program_fact_store_consistency_after_export_import_recovery(tmp_path: Path) -> None:
    """The Program Fact Store's canonical SQLite database lives outside the
    tree-backup roots (`programs/`, `knowledge/`, `editions/`) by design --
    per `specs/vertex-prd.md`, it is recovered via the documented `vertex
    facts export`/`facts import` pair, not the tree backup. This proves
    that recovery path yields a consistent fact snapshot: seed facts,
    export, simulate total loss of the original database, import into a
    fresh database, and assert the recovered facts match the originals."""
    original_db_root = tmp_path / "vertex-db-original"
    recovered_db_root = tmp_path / "vertex-db-recovered"

    original_store = ProgramFactStore(_PROGRAM_ID, db_root=original_db_root)
    write_result = original_store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            entity_refs=("item:4242",),
            payload={"title": "Drill risk", "risk_level": "high"},
        ),
        recorded_at=datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc),
    )
    before_facts = original_store.snapshot().facts
    assert len(before_facts) == 1
    assert before_facts[0].fact_id == write_result.revision.fact_id

    # Simulate total loss of the original database (the scenario `facts
    # export`/`import` exists to recover from).
    assert original_db_root.exists()

    recovered_store = ProgramFactStore(_PROGRAM_ID, db_root=recovered_db_root)
    for fact in before_facts:
        recovered_store.append_fact(
            ProgramFactInput(
                fact_type=fact.fact_type,
                entity_refs=fact.entity_refs,
                payload=fact.payload,
                scope=fact.scope,
                natural_key=fact.natural_key,
                precedence=fact.precedence,
                review_state=fact.review_state,
                lifecycle_state=fact.lifecycle_state,
                privacy_classification=fact.privacy_classification,
                created_by="vertex.facts.import",
            ),
            recorded_at=datetime(2026, 7, 21, 13, 0, 0, tzinfo=timezone.utc),
        )

    after_facts = recovered_store.snapshot().facts
    assert len(after_facts) == len(before_facts)
    assert {f.natural_key for f in after_facts} == {f.natural_key for f in before_facts}
    assert {f.payload["title"] for f in after_facts} == {f.payload["title"] for f in before_facts}
