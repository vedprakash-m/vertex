"""specs/people.md Phase 1, PPL-W1.8: tests for default-PII-excluded
registry privacy summary (src/core/people_registry_privacy.py) and its
support-bundle wiring.

specs/people.md §9.1's own verification bar for PPL-W1.8: "default PII
exclusion"."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tarfile
import json

from src.core.people_change_journal import append_people_change_record
from src.core.people_registry_backup import create_registry_backup_snapshot
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_privacy import build_registry_privacy_summary
from src.core.support_bundle import build_support_bundle

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _bootstrap_with_a_change(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    append_people_change_record(
        knowledge_root,
        workspace_id="workspace:acme",
        transaction_id="registry-tx-1",
        generation_id="generation-1",
        authenticated_principal="ACME\\operator",
        operation="field_updated",
        entity_id="person:1",
        field="manager_entity_id",
        before="person:old",
        after="person:new",
        source="graph",
        reason="structured directory refresh",
        as_of=_NOW,
    )


def test_summary_excludes_raw_records_by_default(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_a_change(knowledge_root)

    summary = build_registry_privacy_summary(knowledge_root)  # include_pii defaults to False.

    assert summary.people_change_record_count == 1
    assert summary.raw_people_change_records == ()
    payload = summary.to_payload()
    assert "raw_people_change_records" not in payload
    assert "person:1" not in json.dumps(payload)
    assert "manager_entity_id" not in json.dumps(payload)


def test_summary_includes_raw_records_only_with_explicit_opt_in(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_a_change(knowledge_root)

    summary = build_registry_privacy_summary(knowledge_root, include_pii=True)

    assert len(summary.raw_people_change_records) == 1
    assert summary.raw_people_change_records[0]["entity_id"] == "person:1"
    payload = summary.to_payload()
    assert "raw_people_change_records" in payload


def test_summary_reports_bootstrapped_false_before_bootstrap(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    summary = build_registry_privacy_summary(knowledge_root)

    assert summary.bootstrapped is False
    assert summary.generation_id is None
    assert summary.people_change_record_count == 0


def test_backup_snapshot_does_not_leak_raw_field_values_outside_the_journal_file(tmp_path: Path) -> None:
    # The backup snapshot legitimately contains the raw journal file (it's a
    # full data backup, not a redacted export) -- this test just confirms
    # the snapshot's own METADATA manifest (registry_backup_manifest.json)
    # carries only paths/hashes, never raw field content.
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_a_change(knowledge_root)
    destination = tmp_path / "backup"

    create_registry_backup_snapshot(knowledge_root, destination)

    snapshot_manifest = json.loads((destination / "registry_backup_manifest.json").read_text(encoding="utf-8"))
    assert "person:1" not in json.dumps(snapshot_manifest)
    assert "manager_entity_id" not in json.dumps(snapshot_manifest)


def test_support_bundle_excludes_raw_registry_pii_by_default(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    knowledge_root = programs_root.parent / "knowledge"
    _bootstrap_with_a_change(knowledge_root)

    result = build_support_bundle("acme", programs_root=programs_root)

    with tarfile.open(result.bundle_path, "r:gz") as tar:
        member = tar.getmember("registry_summary.json")
        content = tar.extractfile(member).read().decode("utf-8")
    payload = json.loads(content)
    assert payload["people_change_record_count"] == 1
    assert "raw_people_change_records" not in payload
    assert "person:1" not in content
    assert "manager_entity_id" not in content
