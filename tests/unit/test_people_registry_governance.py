"""Synthetic PPL-W2B.2 governance coverage."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.exceptions import ConfigError
from src.core.operator_identity import OperatorIdentity
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, read_journal_records
from src.core.people_directory_schema import (
    PersonDirectory,
    PersonStatus,
    load_people_directory,
    write_people_directory,
)
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, load_entities_document, write_entities_document
from src.core.people_registry_governance import (
    adopt_registry_edits,
    govern_person_fields,
    inspect_registry_manifest_integrity,
)
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction
from src.commands.doctor_checks.kb_checks import registry_manifest_integrity_check
from src.commands import readiness

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_RUNNER = CliRunner()


def _seed_registry(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    entity = CanonicalEntity(
        workspace_id="workspace:synthetic",
        entity_id="person:alice",
        entity_type="person",
        canonical_name="Alice",
        aliases=(
            EntityAlias(
                value="alice",
                kind="vertex::alias",
                status=AliasStatus.ACTIVE,
                valid_from=None,
                valid_until=None,
                source="synthetic_fixture",
                source_ref=None,
                recorded_at=_NOW,
                verified_at=_NOW,
                verified_by_principal="test_steward",
            ),
        ),
        scope="org",
        created_at=_NOW,
    )
    person = PersonDirectory(
        entity_id="person:alice",
        alias="alice",
        display_name="Alice",
        title="PM",
        status=PersonStatus.ACTIVE,
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=(entity,)))
        write_people_directory(staged_dir / "people_directory.yaml", (person,))

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        ("entities.yaml", "people_directory.yaml"),
        owner="test_steward",
        write_staged_files=write_staged,
        validate_staged_files=validate_staged,
        as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def _edit_people_file(knowledge_root: Path, before: str, after: str) -> None:
    path = knowledge_root / "people_directory.yaml"
    text = path.read_text(encoding="utf-8")
    assert before in text
    path.write_text(text.replace(before, after, 1), encoding="utf-8")


def test_manifest_hash_detects_informational_and_critical_manual_edits(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    _edit_people_file(knowledge_root, "title: PM", "title: Principal PM")
    informational = inspect_registry_manifest_integrity(knowledge_root)

    assert len(informational.edits) == 1
    assert informational.edits[0].changed_fields == ("people[person:alice].title",)
    assert informational.edits[0].critical is False

    _edit_people_file(knowledge_root, "status: active", "status: inactive")
    critical = inspect_registry_manifest_integrity(knowledge_root)

    assert critical.has_critical_edits is True
    assert "people[person:alice].status" in critical.edits[0].changed_fields
    with pytest.raises(ConfigError, match="unadopted critical"):
        govern_person_fields(
            knowledge_root,
            operation="attest",
            person_ref="alice",
            fields=("title",),
            reason="synthetic check",
            actor="test_steward",
            apply=False,
            as_of=_NOW,
        )
    with pytest.raises(ConfigError, match="unadopted critical"):
        readiness.fetch_readiness_snapshot("synthetic", programs_root=tmp_path / "programs")
    doctor_check = registry_manifest_integrity_check(programs_root=tmp_path / "programs")
    assert doctor_check.status == "fail"
    assert doctor_check.detail.startswith("DIR-14B:")


def test_critical_manual_edit_blocks_until_adopted(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    _edit_people_file(knowledge_root, "status: active", "status: inactive")

    with pytest.raises(ConfigError, match="unadopted critical"):
        govern_person_fields(
            knowledge_root,
            operation="attest",
            person_ref="alice",
            fields=("title",),
            reason="synthetic check",
            actor="test_steward",
            apply=False,
            as_of=_NOW,
        )

    adopt_registry_edits(knowledge_root, actor="test_steward", reason="reviewed departure", apply=True, as_of=_NOW)
    result = govern_person_fields(
        knowledge_root,
        operation="attest",
        person_ref="alice",
        fields=("title",),
        reason="title remains verified",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )
    assert result.transaction_id is not None


def test_adopt_validates_commits_and_journals_manifest_drift(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    _edit_people_file(knowledge_root, "title: PM", "title: Principal PM")

    preview = adopt_registry_edits(knowledge_root, actor="test_steward", reason="reviewed synthetic correction", apply=False, as_of=_NOW)
    assert preview.transaction_id is None
    assert preview.integrity.edits[0].critical is False

    applied = adopt_registry_edits(
        knowledge_root,
        actor="test_steward",
        on_behalf_of="synthetic_owner",
        reason="reviewed synthetic correction",
        apply=True,
        as_of=_NOW,
    )

    assert applied.transaction_id is not None
    assert inspect_registry_manifest_integrity(knowledge_root).is_clean
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None and manifest.generation_id == applied.generation_id
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert any(
        record["operation"] == "adopt"
        and record["authenticated_principal"] == "test_steward"
        and record["on_behalf_of"] == "synthetic_owner"
        and record["field"] == "manifest_hash"
        for record in records
    )


def test_pin_unpin_and_attest_use_typed_transaction_and_preserve_pin_metadata(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    review_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    preview = govern_person_fields(
        knowledge_root,
        operation="pin",
        person_ref="alice",
        fields=("title",),
        reason="hold title pending review",
        actor="test_steward",
        review_at=review_at,
        apply=False,
        as_of=_NOW,
    )
    assert preview.transaction_id is None
    assert load_people_directory(knowledge_root / "people_directory.yaml").people[0].verifications == ()

    pinned = govern_person_fields(
        knowledge_root,
        operation="pin",
        person_ref="person:alice",
        fields=("title",),
        reason="hold title pending review",
        actor="test_steward",
        review_at=review_at,
        apply=True,
        as_of=_NOW,
    )
    assert pinned.transaction_id is not None
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    verification = person.verifications[0]
    assert verification.pinned is True
    assert verification.pin_reason == "hold title pending review"
    assert verification.pin_review_at == review_at
    assert verification.verified_by_principal == "test_steward"

    attested = govern_person_fields(
        knowledge_root,
        operation="attest",
        person_ref="alice",
        fields=("title", "status"),
        reason="synthetic human verification",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )
    assert attested.fields == ("title", "status")
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    title_verification = next(value for value in person.verifications if value.field_name == "title")
    assert title_verification.pinned is True
    assert title_verification.pin_reason == "hold title pending review"
    assert title_verification.source == "human_attestation"

    govern_person_fields(
        knowledge_root,
        operation="unpin",
        person_ref="alice",
        fields=("title",),
        reason="review complete",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    assert next(value for value in person.verifications if value.field_name == "title").pinned is False


def test_cli_governance_commands_preview_by_default_and_apply_with_principal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda actor: OperatorIdentity(actor=actor, principal="test_steward", machine="test", session="test"),
    )

    preview = _RUNNER.invoke(app, ["kb", "people", "pin", "--person", "alice", "--field", "title", "--reason", "synthetic"])
    assert preview.exit_code == 0
    assert "Preview: would pin" in preview.stdout

    applied = _RUNNER.invoke(app, ["kb", "people", "pin", "--person", "alice", "--field", "title", "--reason", "synthetic", "--apply"])
    assert applied.exit_code == 0
    assert "Applied pin" in applied.stdout

    _edit_people_file(knowledge_root, "title: PM", "title: Principal PM")
    adoption_preview = _RUNNER.invoke(app, ["kb", "registry", "adopt", "--reason", "synthetic review", "--format", "json"])
    assert adoption_preview.exit_code == 0
    assert json.loads(adoption_preview.stdout)["edits"][0]["path"] == "people_directory.yaml"
    adoption_apply = _RUNNER.invoke(app, ["kb", "registry", "adopt", "--reason", "synthetic review", "--apply"])
    assert adoption_apply.exit_code == 0
    assert "Committed transaction" in adoption_apply.stdout

    attested = _RUNNER.invoke(
        app,
        ["kb", "people", "attest", "--person", "alice", "--field", "title", "--field", "status", "--reason", "synthetic", "--apply"],
    )
    assert attested.exit_code == 0
    assert "Applied attest" in attested.stdout
    unpinned = _RUNNER.invoke(
        app,
        ["kb", "people", "unpin", "--person", "alice", "--field", "title", "--reason", "synthetic", "--apply"],
    )
    assert unpinned.exit_code == 0
    assert "Applied unpin" in unpinned.stdout
