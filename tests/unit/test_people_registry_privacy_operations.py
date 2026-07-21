"""Synthetic PPL-W2B.5 DSAR and forget coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import pytest
import yaml
from typer.testing import CliRunner

from cli import app
from src.core.archive_signing import (
    load_signature_record,
    manifest_signature_sidecar_path,
    verify_signature,
)
from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum, read_jsonl_records
from src.core.people_change_journal import (
    STREAM_PEOPLE_CHANGES,
    STREAM_PEOPLE_CONFLICTS,
    append_people_change_record,
    append_people_conflict_record,
    read_journal_records,
    verify_journal_hash_chain,
)
from src.core.people_directory_schema import (
    ContactKind,
    ContactPoint,
    ContactStatus,
    PersonDirectory,
    PersonStatus,
    Team,
    TeamKind,
    TeamStatus,
    load_people_directory,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityIdentifier,
    EntityStatus,
    load_entities_document,
    write_entities_document,
)
from src.core.people_membership_schema import (
    MembershipStatus,
    TeamMembership,
    load_memberships,
    read_memberships_as_of,
    write_memberships,
)
from src.core.people_registry_identity import (
    bootstrap_registry_identity,
    load_registry_config,
    load_registry_manifest,
    write_registry_config,
)
from src.core.people_registry_privacy import export_shared_registry_person
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction
from src.core.people_registry_writer import forget_shared_registry_person
from src.core.profile_encryption import encrypt_people_profiles_file, load_people_profiles_document

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_TARGET_ID = "person:synthetic-target"
_RUNNER = CliRunner()


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value,
        kind="vertex::alias",
        status=AliasStatus.ACTIVE,
        valid_from=None,
        valid_until=None,
        source="synthetic_fixture",
        source_ref=None,
        recorded_at=_NOW,
        verified_at=_NOW,
        verified_by_principal="synthetic_privacy_operator",
    )


def _copy_current_files(knowledge_root: Path, staged_dir: Path, files: tuple[str, ...]) -> None:
    for relative_path in files:
        destination = staged_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(knowledge_root / relative_path, destination)


def _seed_registry(tmp_path: Path) -> tuple[Path, Path]:
    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(
        knowledge_root=knowledge_root,
        customer_boundary_id="synthetic-boundary",
        apply=True,
        as_of=_NOW,
    )
    config = load_registry_config(knowledge_root)
    assert config is not None
    write_registry_config(
        knowledge_root / "registry.yaml",
        replace(config, pii_reveal_principals=("synthetic_privacy_operator",)),
    )
    entities = EntitiesDocument(
        schema_version="2.0",
        entities=(
            CanonicalEntity(
                workspace_id=config.workspace_id,
                entity_id=_TARGET_ID,
                entity_type="person",
                canonical_name="Synthetic Target",
                aliases=(_alias("synthetic-target"),),
                identifiers=(
                    EntityIdentifier(
                        provider="synthetic-directory",
                        kind="provider_subject",
                        subject_id="synthetic-subject",
                        handle="synthetic-handle",
                    ),
                ),
                scope="org",
                created_at=_NOW,
            ),
            CanonicalEntity(
                workspace_id=config.workspace_id,
                entity_id="team:synthetic",
                entity_type="team",
                canonical_name="Synthetic Team",
                aliases=(_alias("synthetic-team"),),
                scope="org",
                created_at=_NOW,
            ),
        ),
    )
    person = PersonDirectory(
        entity_id=_TARGET_ID,
        alias="synthetic-target",
        display_name="Synthetic Target",
        title="Synthetic Role",
        department="Synthetic Department",
        status=PersonStatus.ACTIVE,
        contacts=(
            ContactPoint(
                kind=ContactKind.PRIMARY_EMAIL,
                value="synthetic.target@example.invalid",
                status=ContactStatus.ACTIVE,
                valid_from=None,
                valid_until=None,
                source="synthetic_fixture",
                source_ref=None,
                recorded_at=_NOW,
                verified_at=_NOW,
                verified_by_principal="synthetic_privacy_operator",
                delivery_eligible=True,
            ),
        ),
    )
    team = Team(
        entity_id="team:synthetic",
        id="synthetic-team",
        name="Synthetic Team",
        kind=TeamKind.ORG_TEAM,
        status=TeamStatus.ACTIVE,
    )
    membership = TeamMembership(
        membership_id="membership:synthetic-target",
        person_entity_id=_TARGET_ID,
        team_entity_id="team:synthetic",
        role="member",
        valid_from=_NOW,
        valid_until=None,
        source="synthetic-directory",
        source_ref="synthetic-provider-record",
        observed_at=_NOW,
        verified_at=_NOW,
    )
    profiles = {
        "schema_version": "2.0",
        "profiles": [
            {
                "entity_id": _TARGET_ID,
                "alias": "synthetic-target",
                "comm_style": "concise",
                "cares_about": ["synthetic outcome"],
            },
        ],
    }
    delegations = {
        "schema_version": "1.0",
        "delegations": [
            {
                "delegation_id": "delegation:synthetic-target",
                "from_person_entity_id": _TARGET_ID,
                "to_person_entity_id": _TARGET_ID,
                "surfaces": ["vertex::synthetic"],
                "valid_from": _NOW.isoformat(),
                "valid_until": None,
                "reason": "Synthetic delegation reason",
                "actor_principal": "synthetic_privacy_operator",
                "status": "active",
            },
        ],
    }
    files = (
        "entities.yaml",
        "people_directory.yaml",
        "teams.yaml",
        "memberships.yaml",
        "people_profiles.yaml",
        "delegations.yaml",
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", entities)
        write_people_directory(staged_dir / "people_directory.yaml", (person,))
        write_teams(staged_dir / "teams.yaml", (team,))
        write_memberships(staged_dir / "memberships.yaml", (membership,))
        (staged_dir / "people_profiles.yaml").write_text(
            "schema_version: '2.0'\nprofiles:\n"
            "  - entity_id: person:synthetic-target\n"
            "    alias: synthetic-target\n"
            "    comm_style: concise\n"
            "    cares_about: [synthetic outcome]\n",
            encoding="utf-8",
        )
        (staged_dir / "delegations.yaml").write_text(
            "schema_version: '1.0'\ndelegations:\n"
            "  - delegation_id: delegation:synthetic-target\n"
            "    from_person_entity_id: person:synthetic-target\n"
            "    to_person_entity_id: person:synthetic-target\n"
            "    surfaces: [vertex::synthetic]\n"
            f"    valid_from: '{_NOW.isoformat()}'\n"
            "    valid_until: null\n"
            "    reason: Synthetic delegation reason\n"
            "    actor_principal: synthetic_privacy_operator\n"
            "    status: active\n",
            encoding="utf-8",
        )

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None
        assert load_memberships(staged_dir / "memberships.yaml")
        assert (staged_dir / "people_profiles.yaml").exists()
        assert (staged_dir / "delegations.yaml").exists()

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        files,
        owner="synthetic_privacy_operator",
        write_staged_files=write_staged,
        validate_staged_files=validate_staged,
        as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)

    # Seed a committed checkpoint whose pre-erasure bytes must also be scrubbed.
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    prepared = prepare_registry_files_transaction(
        knowledge_root,
        files,
        owner="synthetic_privacy_operator",
        write_staged_files=lambda staged_dir: _copy_current_files(knowledge_root, staged_dir, files),
        validate_staged_files=validate_staged,
        expected_generation_id=manifest.generation_id,
        as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)
    (knowledge_root / ".cache").mkdir()
    (knowledge_root / ".cache" / "synthetic.json").write_text(
        json.dumps({"person_entity_id": _TARGET_ID, "display_name": "Synthetic Target"}),
        encoding="utf-8",
    )
    append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id="synthetic-before-forget",
        generation_id=load_registry_manifest(knowledge_root).generation_id,  # type: ignore[union-attr]
        authenticated_principal="synthetic_privacy_operator",
        operation="update",
        entity_id=_TARGET_ID,
        field="person.display_name",
        before="Synthetic Target",
        after="Synthetic Target Updated",
        source="synthetic_fixture",
        reason="Synthetic history",
        as_of=_NOW,
    )
    append_people_conflict_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        conflict_id="synthetic-target-conflict",
        decision="synthetic",
        authenticated_principal="synthetic_privacy_operator",
        reason="Synthetic conflict history",
        entity_id=_TARGET_ID,
        as_of=_NOW,
    )
    return programs_root, knowledge_root


def _managed_bytes(knowledge_root: Path) -> dict[str, bytes]:
    paths = (
        "entities.yaml",
        "people_directory.yaml",
        "memberships.yaml",
        "people_profiles.yaml",
        "delegations.yaml",
        "_journal/people_changes.jsonl",
        "_journal/people_conflicts.jsonl",
    )
    return {
        relative_path: (knowledge_root / relative_path).read_bytes()
        for relative_path in paths
        if (knowledge_root / relative_path).exists()
    }


def _append_rotated_target_history(knowledge_root: Path) -> None:
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    assert config is not None and manifest is not None
    for index in range(2):
        append_people_change_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            transaction_id=f"synthetic-rotated-history-{index}",
            generation_id=manifest.generation_id,
            authenticated_principal="synthetic_privacy_operator",
            operation="update",
            entity_id=_TARGET_ID,
            field="person.display_name",
            before=f"Synthetic Target {index}",
            after=f"Synthetic Target Updated {index}",
            source="synthetic_fixture",
            reason="Synthetic rotated history",
            max_bytes=1,
            as_of=_NOW,
        )


def test_dasar_export_is_target_scoped_audited_and_excludes_raw_history(tmp_path: Path) -> None:
    programs_root, knowledge_root = _seed_registry(tmp_path)

    result = export_shared_registry_person(
        programs_root=programs_root,
        person_ref="synthetic-target",
        reason="Synthetic DSAR request",
        actor="synthetic_privacy_operator",
        as_of=_NOW,
    )

    payload = result.to_payload()
    assert payload["person"]["contacts"][0]["value"] == "synthetic.target@example.invalid"  # type: ignore[index]
    assert payload["profiles"][0]["comm_style"] == "concise"  # type: ignore[index]
    assert payload["memberships"][0]["team_entity_id"] == "team:synthetic"  # type: ignore[index]
    assert payload["historical_artifacts"]["journal_values_included"] == 0  # type: ignore[index]
    assert "Synthetic history" not in json.dumps(payload)
    audit = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)[-1]
    assert audit["operation"] == "privacy_export"
    assert audit["event_id"] == result.audit_event_id
    assert audit["after"]["profile_count"] == 1


def test_forget_preview_is_non_mutating_and_does_not_require_privacy_authorization(tmp_path: Path) -> None:
    programs_root, knowledge_root = _seed_registry(tmp_path)
    before = _managed_bytes(knowledge_root)

    result = forget_shared_registry_person(
        programs_root=programs_root,
        person_ref="synthetic-target",
        reason="Synthetic erasure preview",
        actor="<preview>",
        apply=False,
        as_of=_NOW,
    )

    assert result.entity_id == _TARGET_ID
    assert result.transaction_id is None
    assert set(result.affected_paths) == {
        "delegations.yaml",
        "entities.yaml",
        "memberships.yaml",
        "people_directory.yaml",
        "people_profiles.yaml",
    }
    assert _managed_bytes(knowledge_root) == before


def test_forget_applies_tombstone_redaction_and_manifest_consistency(tmp_path: Path) -> None:
    programs_root, knowledge_root = _seed_registry(tmp_path)

    result = forget_shared_registry_person(
        programs_root=programs_root,
        person_ref="synthetic-target",
        reason="Synthetic privacy erasure",
        actor="synthetic_privacy_operator",
        on_behalf_of="synthetic-request-context",
        apply=True,
        as_of=_NOW,
    )

    assert result.transaction_id is not None
    assert result.generation_id == load_registry_manifest(knowledge_root).generation_id  # type: ignore[union-attr]
    document = load_entities_document(knowledge_root / "entities.yaml")
    assert document is not None
    tombstone = next(entity for entity in document.entities if entity.entity_id == _TARGET_ID)
    assert tombstone.status is EntityStatus.TOMBSTONED
    assert tombstone.aliases == ()
    assert tombstone.identifiers == ()
    assert "Synthetic Target" not in tombstone.canonical_name
    directory = load_people_directory(knowledge_root / "people_directory.yaml")
    assert directory is not None and not directory.people
    memberships = load_memberships(knowledge_root / "memberships.yaml")
    assert memberships[0].status is MembershipStatus.TOMBSTONED
    assert memberships[0].source_ref is None
    assert read_memberships_as_of(knowledge_root, as_of=_NOW) == ()
    profiles = load_people_profiles_document(knowledge_root / "people_profiles.yaml")
    assert profiles["profiles"] == []
    delegation = yaml.safe_load((knowledge_root / "delegations.yaml").read_text(encoding="utf-8"))["delegations"][0]
    assert delegation["status"] == "tombstoned"
    assert delegation["reason"] == "[REDACTED]"
    assert not (knowledge_root / ".cache").exists()
    assert result.transaction_artifacts_redacted > 0
    assert result.journal_records_redacted >= 2
    assert len(result.journal_event_ids) == 2
    change_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert verify_journal_hash_chain(
        change_records,
        workspace_id=load_registry_config(knowledge_root).workspace_id,  # type: ignore[union-attr]
        stream=STREAM_PEOPLE_CHANGES,
    ).ok
    historical = next(record for record in change_records if record["transaction_id"] == "synthetic-before-forget")
    assert historical["before"] is None
    assert historical["after"] is None
    assert historical["reason"] == "[REDACTED]"
    assert "Synthetic Target" not in (knowledge_root / "_journal" / "people_changes.jsonl").read_text(encoding="utf-8")
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    for relative_path, expected_hash in manifest.source_hashes:
        assert compute_file_checksum(knowledge_root / relative_path) == expected_hash


def test_forget_cryptographically_shreds_a_sole_encrypted_profile(tmp_path: Path, monkeypatch) -> None:
    programs_root, knowledge_root = _seed_registry(tmp_path)
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypted = encrypt_people_profiles_file(knowledge_root / "people_profiles.yaml")
    assert encrypted.key_id is not None

    result = forget_shared_registry_person(
        programs_root=programs_root,
        person_ref=_TARGET_ID,
        reason="Synthetic encrypted-profile erasure",
        actor="synthetic_privacy_operator",
        apply=True,
        as_of=_NOW,
    )

    assert result.profile_disposition == "cryptographic_shred"
    assert (("vertex.people_profiles", encrypted.key_id) not in fake_keyring.values)
    assert load_people_profiles_document(knowledge_root / "people_profiles.yaml")["profiles"] == []


def test_forget_redacts_and_resigns_signed_archived_journals(tmp_path: Path, monkeypatch) -> None:
    import src.core.people_change_journal as journal_module

    programs_root, knowledge_root = _seed_registry(tmp_path)
    monkeypatch.setattr(journal_module, "archive_signing_unavailable", lambda: False)
    monkeypatch.setattr(journal_module, "get_archive_signing_key", lambda: b"synthetic-signing-key")
    _append_rotated_target_history(knowledge_root)

    forget_shared_registry_person(
        programs_root=programs_root,
        person_ref=_TARGET_ID,
        reason="Synthetic signed archive erasure",
        actor="synthetic_privacy_operator",
        apply=True,
        as_of=_NOW,
    )

    archive_dir = knowledge_root / "_journal" / "archive" / str(_NOW.year)
    archived_segments = tuple(archive_dir.glob("people_changes_*.jsonl"))
    assert archived_segments
    for segment_path in archived_segments:
        signature = load_signature_record(manifest_signature_sidecar_path(segment_path))
        assert signature is not None
        records = tuple(read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES))
        assert verify_signature(
            signature,
            manifest_payload=journal_module._segment_signature_payload(
                segment_path,
                stream=STREAM_PEOPLE_CHANGES,
                records=tuple(read_jsonl_records(segment_path)),
            ),
            key=b"synthetic-signing-key",
        )
        assert "Synthetic Target" not in segment_path.read_text(encoding="utf-8")
        assert verify_journal_hash_chain(
            records,
            workspace_id=load_registry_config(knowledge_root).workspace_id,  # type: ignore[union-attr]
            stream=STREAM_PEOPLE_CHANGES,
        ).ok


def test_forget_refuses_unresignable_signed_archive_without_mutation(tmp_path: Path, monkeypatch) -> None:
    import src.core.people_change_journal as journal_module

    programs_root, knowledge_root = _seed_registry(tmp_path)
    monkeypatch.setattr(journal_module, "archive_signing_unavailable", lambda: False)
    monkeypatch.setattr(journal_module, "get_archive_signing_key", lambda: b"synthetic-signing-key")
    _append_rotated_target_history(knowledge_root)
    before = _managed_bytes(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    archive_path = next((knowledge_root / "_journal" / "archive" / str(_NOW.year)).glob("people_changes_*.jsonl"))
    archive_before = archive_path.read_bytes()
    monkeypatch.setattr(journal_module, "archive_signing_unavailable", lambda: True)
    monkeypatch.setattr(journal_module, "get_archive_signing_key", lambda: None)

    with pytest.raises(ConfigError, match="cannot be re-signed"):
        forget_shared_registry_person(
            programs_root=programs_root,
            person_ref=_TARGET_ID,
            reason="Synthetic unresignable archive refusal",
            actor="synthetic_privacy_operator",
            apply=True,
            as_of=_NOW,
        )

    assert _managed_bytes(knowledge_root) == before
    assert load_registry_manifest(knowledge_root).generation_id == manifest.generation_id  # type: ignore[union-attr]
    assert archive_path.read_bytes() == archive_before


@pytest.mark.parametrize(
    ("actor", "reason", "match"),
    (
        ("untrusted_principal", "Synthetic DSAR refusal", "not authorized"),
        ("synthetic_privacy_operator", "", "non-empty DSAR export reason"),
    ),
)
def test_export_refuses_unsafe_requests_without_audit_mutation(
    tmp_path: Path,
    actor: str,
    reason: str,
    match: str,
) -> None:
    programs_root, knowledge_root = _seed_registry(tmp_path)
    before = _managed_bytes(knowledge_root)

    with pytest.raises(ConfigError, match=match):
        export_shared_registry_person(
            programs_root=programs_root,
            person_ref=_TARGET_ID,
            reason=reason,
            actor=actor,
            as_of=_NOW,
        )

    assert _managed_bytes(knowledge_root) == before


@pytest.mark.parametrize(
    ("actor", "reason", "match"),
    (
        ("untrusted_principal", "Synthetic refusal", "not authorized"),
        ("synthetic_privacy_operator", "", "non-empty privacy erasure reason"),
    ),
)
def test_forget_refuses_unsafe_requests_without_mutation(
    tmp_path: Path,
    actor: str,
    reason: str,
    match: str,
) -> None:
    programs_root, knowledge_root = _seed_registry(tmp_path)
    before = _managed_bytes(knowledge_root)

    with pytest.raises(ConfigError, match=match):
        forget_shared_registry_person(
            programs_root=programs_root,
            person_ref="synthetic-target",
            reason=reason,
            actor=actor,
            apply=True,
            as_of=_NOW,
        )

    assert _managed_bytes(knowledge_root) == before


def test_privacy_people_cli_routes_export_and_preview(monkeypatch, tmp_path: Path) -> None:
    programs_root, _ = _seed_registry(tmp_path)
    monkeypatch.setattr("src.commands.privacy.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_OPERATOR_PRINCIPAL", "synthetic_privacy_operator")

    export = _RUNNER.invoke(
        app,
        ["privacy", "people", "export", "--person", "synthetic-target", "--reason", "Synthetic request", "--format", "json"],
    )
    preview = _RUNNER.invoke(
        app,
        ["privacy", "people", "forget", "--person", "synthetic-target", "--reason", "Synthetic erasure", "--format", "json"],
    )

    assert export.exit_code == 0, export.output
    assert json.loads(export.output)["schema_version"] == "people-dsar.v1"
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["transaction_id"] is None
