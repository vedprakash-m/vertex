from __future__ import annotations
from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck
from src.commands.doctor_checks.kb_checks import knowledge_predicate_registry_check, run_kb_doctor
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_lease import acquire_registry_lease


def test_run_kb_doctor_fails_when_programs_and_editions_are_missing(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    labels = [check.label for check in report.checks]
    assert "Knowledge" in labels
    assert "Editions" in labels
    assert "Saved Queries" in labels
    assert "Registry storage class" in labels
    assert "Registry transaction recovery" in labels


def test_run_kb_doctor_registry_storage_class_check_reports_local_ok(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    storage_check = next(check for check in report.checks if check.label == "Registry storage class")
    assert storage_check.status == "ok"
    assert storage_check.metadata is not None
    assert storage_check.metadata["storage_class"] == "local"
    assert (tmp_path / "knowledge" / "registry_capability_status.yaml").exists()


def test_run_kb_doctor_registry_transaction_recovery_check_ok_when_nothing_pending(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    recovery_check = next(check for check in report.checks if check.label == "Registry transaction recovery")
    assert recovery_check.status == "ok"
    assert recovery_check.metadata is not None
    assert recovery_check.metadata["recovered_count"] == 0
    assert recovery_check.metadata["stale_lease_owner"] is None


def test_run_kb_doctor_registry_transaction_recovery_check_warns_on_stale_lease(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    acquire_registry_lease("stuck-worker", ttl_seconds=-1, knowledge_root=knowledge_root)

    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    recovery_check = next(check for check in report.checks if check.label == "Registry transaction recovery")
    assert recovery_check.status == "warn"
    assert recovery_check.metadata["stale_lease_owner"] == "stuck-worker"


def test_run_kb_doctor_dir11_check_ok_with_no_schema2_entities(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    dir11_check = next(check for check in report.checks if check.label == "Entities DIR-11")
    assert dir11_check.status == "ok"
    assert "nothing to check" in dir11_check.detail


def test_run_kb_doctor_dir11_check_fails_on_a_program_scoped_person_entity(tmp_path: Path) -> None:
    from src.core.people_entity_schema import CanonicalEntity, EntitiesDocument, write_entities_document
    from datetime import datetime, timezone

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(schema_version="2.0", entities=()),
    )
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text('schema_version: "1.0"\nid: "acme"\nname: "Acme"\n', encoding="utf-8")
    write_entities_document(
        program_dir / "knowledge" / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(
                    workspace_id="workspace:acme",
                    entity_id="person:1",
                    entity_type="person",
                    canonical_name="Program-scoped person",
                    aliases=(),
                    scope="program",
                    created_at=now,
                ),
            ),
        ),
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    dir11_check = next(check for check in report.checks if check.label == "Entities DIR-11")
    assert dir11_check.status == "fail"
    assert "program_scoped_org_only_type" in dir11_check.detail


def test_run_kb_doctor_dir11_skips_a_schema1_program_local_entities_file(tmp_path: Path) -> None:
    """Regression: real programs commonly carry a pre-existing, unrelated
    schema-1.0 entities.yaml (src/core/entity_registry.py's generic
    program-local entity-alias registry -- milestone/risk/product/person/
    team types used for report-text linking, deliberately never migrated
    into schema-2.0's person/team-only concern). Once a real shared
    schema-2.0 entities.yaml exists, DIR-11 must skip that file entirely,
    not hard-fail trying to load it as schema 2.0."""
    from src.core.people_entity_schema import EntitiesDocument, write_entities_document
    import yaml as yaml_module

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=()))

    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text('schema_version: "1.0"\nid: "xpf"\nname: "XPF"\n', encoding="utf-8")
    program_knowledge = program_dir / "knowledge"
    program_knowledge.mkdir(parents=True, exist_ok=True)
    (program_knowledge / "entities.yaml").write_text(
        yaml_module.safe_dump(
            {
                "schema_version": 1.0,
                "entities": [
                    {"id": "sample_owner", "type": "person", "name": "Sample Owner", "aliases": ["owner"], "scope": "program:xpf"},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    dir11_check = next(check for check in report.checks if check.label == "Entities DIR-11")
    assert dir11_check.status == "ok"
    assert "could not load" not in dir11_check.detail.lower()


def test_run_kb_doctor_dir05_check_ok_with_no_shared_people_directory(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    dir05_check = next(check for check in report.checks if check.label == "Registry DIR-05")
    assert dir05_check.status == "ok"
    assert "nothing to check" in dir05_check.detail


def test_run_kb_doctor_dir05_check_fails_on_a_diverging_shadowed_person(tmp_path: Path) -> None:
    from src.core.people_directory_schema import PersonDirectory, write_people_directory

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (PersonDirectory(entity_id="person:alice", alias="alice", title="Shared title"),),
    )
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text('schema_version: "1.0"\nid: "acme"\nname: "Acme"\n', encoding="utf-8")
    write_people_directory(
        program_dir / "knowledge" / "people_directory.yaml",
        (PersonDirectory(entity_id="", alias="alice", title="Stale local title"),),
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    dir05_check = next(check for check in report.checks if check.label == "Registry DIR-05")
    assert dir05_check.status == "fail"
    assert "diverges on title" in dir05_check.detail


def test_run_kb_doctor_dir05_check_ok_when_shadow_is_equivalent(tmp_path: Path) -> None:
    from src.core.people_directory_schema import PersonDirectory, write_people_directory

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (PersonDirectory(entity_id="person:alice", alias="alice", title="Same title"),),
    )
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text('schema_version: "1.0"\nid: "acme"\nname: "Acme"\n', encoding="utf-8")
    write_people_directory(
        program_dir / "knowledge" / "people_directory.yaml",
        (PersonDirectory(entity_id="", alias="alice", title="Same title"),),
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    dir05_check = next(check for check in report.checks if check.label == "Registry DIR-05")
    assert dir05_check.status == "ok"
    assert "DIR-05A" in dir05_check.detail
    assert "safe to remove" in dir05_check.detail


def test_run_kb_doctor_legacy_reference_check_ok_with_no_log(tmp_path: Path) -> None:
    report = run_kb_doctor(
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    legacy_check = next(check for check in report.checks if check.label == "Registry legacy references")
    assert legacy_check.status == "ok"
    assert legacy_check.metadata["legacy_reference_count"] == 0


def test_run_kb_doctor_legacy_reference_check_warns_on_recorded_legacy_lookups(tmp_path: Path) -> None:
    from src.core.people_legacy_reference_metrics import record_legacy_alias_reference

    knowledge_root = tmp_path / "knowledge"
    record_legacy_alias_reference(knowledge_root, entity_type="person", ref="P:alice")
    record_legacy_alias_reference(knowledge_root, entity_type="team", ref="team:acme-core")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    legacy_check = next(check for check in report.checks if check.label == "Registry legacy references")
    assert legacy_check.status == "warn"
    assert legacy_check.metadata["legacy_reference_count"] == 2
    assert "P:alice" in legacy_check.metadata["sample_refs"]
    assert "DIR-16" in legacy_check.detail


class _FakeKeyring:
    """Minimal in-memory keyring double, mirroring the pattern already used
    in tests/unit/test_people_registry_privacy_operations.py."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


def _bootstrap_registry_with_pii_principals(knowledge_root: Path, *, principals: tuple[str, ...] = ()) -> None:
    from dataclasses import replace as _replace
    from src.core.people_registry_identity import load_registry_config, write_registry_config

    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="test-boundary", apply=True)
    config = load_registry_config(knowledge_root)
    assert config is not None
    write_registry_config(knowledge_root / "registry.yaml", _replace(config, pii_reveal_principals=principals))


def test_registry_dir08_ok_when_registry_not_adopted_at_all(tmp_path: Path) -> None:
    """No registry.yaml and no people_profiles.yaml -- nothing to check yet,
    same bypass DIR-05 already applies for an unadopted shared registry.
    A workspace that has never turned the feature on has no privacy posture
    to be non-compliant with."""
    from src.commands.doctor_checks.kb_checks import _load_shared_registry_snapshot, registry_dir08_pii_policy_check

    programs_root = tmp_path / "programs"
    snapshot = _load_shared_registry_snapshot(programs_root)
    check = registry_dir08_pii_policy_check(programs_root=programs_root, snapshot=snapshot)

    assert check.status == "ok"


def test_registry_dir08_fails_when_nothing_configured(tmp_path: Path) -> None:
    """DIR-08B: a workspace with a registry.yaml (adopted) but no
    pii_reveal_principals, plus a plaintext people_profiles.yaml, violates
    both required-tier policy floors."""
    from src.commands.doctor_checks.kb_checks import _load_shared_registry_snapshot, registry_dir08_pii_policy_check

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_registry_with_pii_principals(knowledge_root, principals=())
    (knowledge_root / "people_profiles.yaml").write_text("{}\n", encoding="utf-8")

    snapshot = _load_shared_registry_snapshot(programs_root)
    check = registry_dir08_pii_policy_check(programs_root=programs_root, snapshot=snapshot)

    assert check.status == "fail"
    assert check.code == "DIR-08B"
    assert "pii_reveal_principals allowlist" in check.detail
    assert "encryption posture" in check.detail


def test_registry_dir08_warns_when_floor_met_but_not_recommended(tmp_path: Path, monkeypatch) -> None:
    """DIR-08A: reveal allowlist configured and profiles encrypted (meets the
    required `sensitive_only` floor) but the platform default's recommended
    posture ('all') is not achievable/met -- WARN, not FAIL."""
    from src.core.profile_encryption import encrypt_people_profiles_file
    from src.commands.doctor_checks.kb_checks import _load_shared_registry_snapshot, registry_dir08_pii_policy_check

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_registry_with_pii_principals(knowledge_root, principals=("dpo@example.com",))

    profiles_path = knowledge_root / "people_profiles.yaml"
    profiles_path.write_text("{}\n", encoding="utf-8")
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(profiles_path)

    snapshot = _load_shared_registry_snapshot(programs_root)
    check = registry_dir08_pii_policy_check(programs_root=programs_root, snapshot=snapshot)

    assert check.status == "warn"
    assert check.code == "DIR-08A"
    assert "not currently achievable" in check.detail


def test_registry_dir08_ok_when_override_matches_achievable_posture(tmp_path: Path, monkeypatch) -> None:
    """A knowledge-root override that sets recommended_encryption to the
    same achievable floor demonstrates the fully-compliant ('ok') path is
    real and reachable, not merely unreachable dead code."""
    from src.core.profile_encryption import encrypt_people_profiles_file
    from src.commands.doctor_checks.kb_checks import _load_shared_registry_snapshot, registry_dir08_pii_policy_check

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_registry_with_pii_principals(knowledge_root, principals=("dpo@example.com",))

    profiles_path = knowledge_root / "people_profiles.yaml"
    profiles_path.write_text("{}\n", encoding="utf-8")
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(profiles_path)

    policies_dir = knowledge_root / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    (policies_dir / "privacy_policy.yaml").write_text(
        "privacy_policy_override:\n"
        "  people_registry:\n"
        "    recommended_encryption: sensitive_only\n",
        encoding="utf-8",
    )

    snapshot = _load_shared_registry_snapshot(programs_root)
    check = registry_dir08_pii_policy_check(programs_root=programs_root, snapshot=snapshot)

    assert check.status == "ok"
    assert check.code == "DIR-08A"


def test_registry_dir08_fails_on_departed_person_past_retention_deadline(tmp_path: Path, monkeypatch) -> None:
    """DIR-08B: a departed person past the retention deadline who still
    carries PII fields is a required-policy violation, independent of the
    encryption/reveal posture."""
    from datetime import datetime, timedelta, timezone
    from src.core.people_directory_schema import PersonDirectory, PersonStatus, write_people_directory
    from src.core.profile_encryption import encrypt_people_profiles_file
    from src.commands.doctor_checks.kb_checks import _load_shared_registry_snapshot, registry_dir08_pii_policy_check

    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_registry_with_pii_principals(knowledge_root, principals=("dpo@example.com",))

    profiles_path = knowledge_root / "people_profiles.yaml"
    profiles_path.write_text("{}\n", encoding="utf-8")
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(profiles_path)

    long_departed = datetime.now(timezone.utc) - timedelta(days=400)
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(
                entity_id="person:departed-alice",
                alias="alice",
                title="Former PM",
                status=PersonStatus.DEPARTED,
                departed_at=long_departed,
            ),
        ),
    )

    snapshot = _load_shared_registry_snapshot(programs_root)
    check = registry_dir08_pii_policy_check(programs_root=programs_root, snapshot=snapshot)

    assert check.status == "fail"
    assert check.code == "DIR-08B"
    assert "retention deadline" in check.detail
    assert "person:departed-alice" in check.detail


def test_knowledge_predicate_registry_check_warns_when_threshold_exceeded(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.doctor_checks.kb_checks.predicate_count", lambda: 101)

    check = knowledge_predicate_registry_check()

    assert check.label == "Knowledge Predicates"
    assert check.status == "warn"
    assert "exceeds the review threshold 100" in check.detail


def _write_dir_check_entity(now, *, entity_id: str, aliases: tuple[str, ...] = ()):
    from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntityAlias

    return CanonicalEntity(
        workspace_id="ws-1", entity_id=entity_id, entity_type="person", canonical_name=entity_id,
        aliases=tuple(
            EntityAlias(
                value=a, kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
                source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
            )
            for a in aliases
        ),
        scope="org", created_at=now,
    )


def test_run_kb_doctor_dir01_check_ok_with_no_schema2_entities(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-01")
    assert check.status == "ok"
    assert check.code == "DIR-01"


def test_run_kb_doctor_dir01_check_fails_on_alias_collision(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from src.core.people_entity_schema import EntitiesDocument, write_entities_document

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                _write_dir_check_entity(now, entity_id="person:alice", aliases=("shared",)),
                _write_dir_check_entity(now, entity_id="person:bob", aliases=("shared",)),
            ),
        ),
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-01")
    assert check.status == "fail"
    assert "DIR-01" in check.detail


def test_run_kb_doctor_dir02_check_fails_on_unresolved_person_reference(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from src.core.people_directory_schema import PersonDirectory, write_people_directory
    from src.core.people_entity_schema import EntitiesDocument, write_entities_document

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=()))
    write_people_directory(knowledge_root / "people_directory.yaml", (PersonDirectory(entity_id="person:ghost", alias="ghost"),))

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-02")
    assert check.status == "fail"
    assert "DIR-02" in check.detail


def test_run_kb_doctor_dir03_check_warns_on_stale_field(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone
    from src.core.people_directory_schema import FieldVerification, PersonDirectory, write_people_directory

    old = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc) - timedelta(days=365)
    knowledge_root = tmp_path / "knowledge"
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(
                entity_id="person:alice", alias="alice",
                verifications=(
                    FieldVerification(
                        field_name="title", source="test", source_ref=None, observed_at=old,
                        verified_at=old, recorded_at=old, verified_by_principal="steward",
                    ),
                ),
            ),
        ),
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-03")
    assert check.status == "warn"
    assert check.code == "DIR-03"


def test_run_kb_doctor_people_enrichment_check_ok_when_no_candidates_pending(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry people enrichment")
    assert check.status == "ok"
    assert check.code == "PEOPLE-ENRICHMENT"


def test_run_kb_doctor_people_enrichment_check_reports_pending_candidates(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from src.core.people_enrichment import EnrichmentCandidateEvent, record_enrichment_event

    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "xpf"
    prog_dir.mkdir(parents=True, exist_ok=True)
    (prog_dir / "program.yaml").write_text('schema_version: "3.0"\nid: "xpf"\nname: "XPF"\n', encoding="utf-8")
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    record_enrichment_event(
        EnrichmentCandidateEvent(
            recorded_at=now, program_id="xpf", candidate_id="cand-1", entity_id="person:alice",
            alias="alice", field_name="title", current_value=None, event="proposed",
            workiq_question="What is alice's title?", workiq_answer="Senior TPM",
        ),
        programs_root=programs_root,
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.label == "Registry people enrichment")
    assert check.status == "info"
    assert check.metadata is not None
    assert check.metadata["pending_count"] == 1


def test_run_kb_doctor_dir06_check_fails_on_manager_cycle(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from src.core.people_directory_schema import PersonDirectory, write_people_directory
    from src.core.people_entity_schema import EntitiesDocument, write_entities_document

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=()))
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(entity_id="person:a", alias="a", manager_entity_id="person:b"),
            PersonDirectory(entity_id="person:b", alias="b", manager_entity_id="person:a"),
        ),
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-06")
    assert check.status == "fail"
    assert "DIR-06" in check.detail


def test_run_kb_doctor_dir07_check_fails_on_tampered_journal(tmp_path: Path) -> None:
    from src.core.people_change_journal import append_people_change_record, journal_active_path, STREAM_PEOPLE_CHANGES

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    append_people_change_record(
        knowledge_root, workspace_id="ws-1", transaction_id="tx-1", generation_id="gen-1",
        authenticated_principal="steward", operation="upsert", entity_id="person:alice", field="title",
        before="Old", after="New", source="test", reason="test",
    )
    journal_path = journal_active_path(knowledge_root, STREAM_PEOPLE_CHANGES)
    journal_path.write_text(journal_path.read_text(encoding="utf-8").replace("New", "Tampered"), encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-07")
    assert check.status == "fail"
    assert "DIR-07" in check.detail


def test_run_kb_doctor_dir15_check_ok_before_bootstrap(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-15")
    assert check.status == "ok"
    assert check.code == "DIR-15"


def test_run_kb_doctor_dir13_check_ok_before_bootstrap(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-13")
    assert check.status == "ok"
    assert check.code == "DIR-13A"
    assert "nothing to cache" in check.detail


def test_run_kb_doctor_dir13_check_rebuilds_a_missing_cache_after_bootstrap(tmp_path: Path) -> None:
    from src.core.people_entity_schema import EntitiesDocument, write_entities_document
    from src.core.people_registry_cache import cache_db_path
    from src.core.knowledge_store import get_shared_knowledge_root

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=()))

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-13")
    assert check.status == "ok"
    assert check.code == "DIR-13A"
    assert "rebuilt successfully" in check.detail
    assert cache_db_path(get_shared_knowledge_root(tmp_path / "programs")).exists()


def test_run_kb_doctor_dir13_check_rebuild_indexes_a_real_person_via_shared_snapshot(tmp_path: Path) -> None:
    """PPL-W3.5d: DIR-13's cache rebuild now reuses `run_kb_doctor`'s own
    already-loaded `_SharedRegistrySnapshot` instead of independently
    re-parsing entities/people/teams a third time -- verify the resulting
    cache is still CORRECT (a real alias resolves), not just that the
    check doesn't crash."""
    from datetime import datetime, timezone
    from src.core.people_directory_schema import PersonDirectory, PersonStatus, write_people_directory
    from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, EntityStatus, write_entities_document
    from src.core.people_registry_cache import lookup_alias_in_cache
    from src.core.knowledge_store import get_shared_knowledge_root

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(
                    workspace_id="ws-1", entity_id="person:alice", entity_type="person", canonical_name="Alice",
                    aliases=(
                        EntityAlias(
                            value="alice", kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
                            source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
                        ),
                    ),
                    scope="org", created_at=now, status=EntityStatus.ACTIVE,
                ),
            ),
        ),
    )
    write_people_directory(knowledge_root / "people_directory.yaml", (PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),))

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-13")
    assert check.status == "ok"
    assert check.code == "DIR-13A"
    assert lookup_alias_in_cache(get_shared_knowledge_root(tmp_path / "programs"), "alice") == (("person:alice", "person"),)


def test_run_kb_doctor_dir13_check_rebuild_with_legacy_schema0_entities_does_not_crash(tmp_path: Path) -> None:
    """PPL-W3.5d also fixed a real, pre-existing bug found while wiring the
    snapshot reuse through: a legacy schema-0 shared entities.yaml (no
    schema_version key) made `_build_index_rows`'s fresh-load fallback
    raise `ConfigError` uncaught (not `OSError`/`ValueError`, so DIR-13's
    own except clause never caught it) -- crashing the ENTIRE
    `run_kb_doctor` call, confirmed to reproduce before this fix."""
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    (knowledge_root / "entities.yaml").write_text("entities:\n  - id: person:alice\n", encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-13")
    assert check.status == "ok"
    assert check.code == "DIR-13A"
    assert "rebuilt successfully" in check.detail


def test_run_kb_doctor_dir13_check_ok_when_cache_already_valid(tmp_path: Path) -> None:
    from src.core.people_entity_schema import EntitiesDocument, write_entities_document
    from src.core.people_registry_cache import rebuild_cache
    from src.core.knowledge_store import get_shared_knowledge_root

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=()))
    rebuild_cache(get_shared_knowledge_root(tmp_path / "programs"))

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.label == "Registry DIR-13")
    assert check.status == "ok"
    assert check.detail == "Registry cache is present and valid."


def _write_provider_config(knowledge_root: Path, *, enabled: bool = True) -> None:
    (knowledge_root / "identity_providers.yaml").write_text(
        (
            'schema_version: "1.0"\nproviders:\n  - name: "acme_directory_export"\n'
            '    provider_type: "local_directory_export"\n    tenant_id: "acme-tenant"\n'
            f'    capability_contract_version: "1.0"\n    enabled: {str(enabled).lower()}\n'
        ),
        encoding="utf-8",
    )


def test_run_kb_doctor_dir09a_check_ok_with_no_providers_configured(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09A")
    assert check.status == "ok"
    assert "No identity_providers.yaml" in check.detail


def test_run_kb_doctor_dir09a_check_reports_never_refreshed_for_an_enabled_provider(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    _write_provider_config(knowledge_root)

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09A")
    assert check.status == "ok"
    assert "never refreshed" in check.detail
    assert check.metadata["provider_count"] == 1


def test_run_kb_doctor_dir09b_check_ok_when_provider_refresh_disabled(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09B")
    assert check.status == "ok"
    assert "disabled" in check.detail


def test_run_kb_doctor_dir09b_check_warns_when_no_providers_configured_but_refresh_enabled(tmp_path: Path) -> None:
    from src.core.people_registry_modes import set_registry_flag

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="steward")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09B")
    assert check.status == "warn"
    assert "no configured providers" in check.detail


def test_run_kb_doctor_dir09b_check_warns_when_every_provider_is_disabled(tmp_path: Path) -> None:
    from src.core.people_registry_modes import set_registry_flag

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="steward")
    _write_provider_config(knowledge_root, enabled=False)

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09B")
    assert check.status == "warn"
    assert "every configured provider is disabled" in check.detail


def test_run_kb_doctor_dir09b_check_ok_when_enabled_provider_has_no_stale_refresh(tmp_path: Path) -> None:
    from src.core.people_registry_modes import set_registry_flag

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="steward")
    _write_provider_config(knowledge_root)

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09B")
    assert check.status == "ok"
    assert "1 enabled provider" in check.detail


def _seed_program_with_entities(programs_root: Path, program_id: str, *, team_entity_id: str = "team:platform", team_alias: str = "platform") -> None:
    from datetime import datetime, timezone as tz

    from src.core.audience_scopes import audience_scopes_path_for_program
    from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, load_entities_document, write_entities_document

    now = datetime(2026, 7, 20, tzinfo=tz.utc)
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(f'schema_version: "1.0"\nid: "{program_id}"\nname: "{program_id}"\n', encoding="utf-8")

    knowledge_root = programs_root.parent / "knowledge"
    existing = load_entities_document(knowledge_root / "entities.yaml")
    entities = existing.entities if existing is not None else ()
    if not any(entity.entity_id == team_entity_id for entity in entities):
        alias = EntityAlias(
            value=team_alias, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
            source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
        )
        entities = entities + (
            CanonicalEntity(workspace_id="ws", entity_id=team_entity_id, entity_type="team", canonical_name=team_alias, aliases=(alias,), scope="org", created_at=now),
        )
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
    return audience_scopes_path_for_program(program_id, programs_root=programs_root)


def test_run_kb_doctor_dir10_check_ok_with_no_audience_scopes_configured(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-10")
    assert check.status == "ok"
    assert "nothing to check" in check.detail


def test_run_kb_doctor_dir10_check_ok_with_a_valid_scope(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    scope_path = _seed_program_with_entities(programs_root, "acme")
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text('schema_version: "1.0"\naudience_scopes:\n  engineering_hygiene:\n    team_refs: [platform]\n    require_verified_within_days: 30\n', encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-10")
    assert check.status == "ok"


def test_run_kb_doctor_dir10_check_fails_on_an_unresolvable_reference(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    scope_path = _seed_program_with_entities(programs_root, "acme")
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text('schema_version: "1.0"\naudience_scopes:\n  bad:\n    team_refs: [nonexistent]\n', encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-10")
    assert check.status == "fail"
    assert "unresolvable" in check.detail


def test_run_kb_doctor_dir10_check_warns_on_nonsensical_freshness_threshold(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    scope_path = _seed_program_with_entities(programs_root, "acme")
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text('schema_version: "1.0"\naudience_scopes:\n  engineering_hygiene:\n    team_refs: [platform]\n    require_verified_within_days: 0\n', encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-10")
    assert check.status == "warn"
    assert "require_verified_within_days" in check.detail


def test_run_kb_doctor_dir10_check_warns_on_cross_program_scope_id_collision(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    scope_path_a = _seed_program_with_entities(programs_root, "acme")
    scope_path_a.parent.mkdir(parents=True, exist_ok=True)
    scope_path_a.write_text('schema_version: "1.0"\naudience_scopes:\n  engineering_hygiene:\n    team_refs: [platform]\n', encoding="utf-8")
    scope_path_b = _seed_program_with_entities(programs_root, "contoso")
    scope_path_b.parent.mkdir(parents=True, exist_ok=True)
    scope_path_b.write_text('schema_version: "1.0"\naudience_scopes:\n  engineering_hygiene:\n    team_refs: [platform]\n', encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-10")
    assert check.status == "warn"
    assert "collision" in check.detail


def test_run_kb_doctor_dir09b_check_warns_on_a_stale_provider(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from src.core.people_change_journal import append_people_change_record
    from src.core.people_registry_identity import load_registry_manifest
    from src.core.people_registry_modes import set_registry_flag

    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="steward")
    _write_provider_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    old = datetime.now(timezone.utc) - timedelta(days=31)
    append_people_change_record(
        knowledge_root, workspace_id=manifest.workspace_id, transaction_id="tx-1", generation_id="gen-1",
        authenticated_principal="steward", operation="update", entity_id="person:alice", field="title",
        before="Old", after="New", source="provider_refresh", source_ref="acme_directory_export:refresh-1",
        reason="test", as_of=old,
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-09B")
    assert check.status == "warn"


def _seed_program_with_stakeholder(
    programs_root: Path, program_id: str, *, alias: str, create_entity: bool = True, person_status: str | None = "active",
) -> str:
    from datetime import datetime, timezone as tz

    from src.core.people_directory_schema import PersonDirectory, PersonStatus, load_people_directory, write_people_directory
    from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, load_entities_document, write_entities_document

    now = datetime(2026, 7, 20, tzinfo=tz.utc)
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        f'schema_version: "1.0"\nid: "{program_id}"\nname: "{program_id}"\n'
        f'stakeholder_register:\n  - alias: "{alias}"\n    email: "{alias}@acme.com"\n    role: "owner"\n',
        encoding="utf-8",
    )

    knowledge_root = programs_root.parent / "knowledge"
    entity_id = f"person:{alias}"
    # Always write a schema-2.0 entities.yaml (even with an unrelated
    # placeholder entity when create_entity=False) so `has_schema2_entities`
    # is true -- an "unresolvable alias" is only a meaningful DIR-04 signal
    # against an already-migrated registry; a wholly absent entities.yaml
    # means "nothing to check yet" (every other DIR-* check's own gate),
    # not "every alias is unresolvable."
    existing = load_entities_document(knowledge_root / "entities.yaml")
    entities = existing.entities if existing is not None else ()
    if create_entity and not any(entity.entity_id == entity_id for entity in entities):
        entity_alias = EntityAlias(
            value=alias, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
            source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
        )
        entities = entities + (
            CanonicalEntity(
                workspace_id="ws", entity_id=entity_id, entity_type="person", canonical_name=alias,
                aliases=(entity_alias,), scope="org", created_at=now,
            ),
        )
    elif not create_entity and not entities:
        placeholder_alias = EntityAlias(
            value="placeholder", kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
            source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
        )
        entities = (
            CanonicalEntity(
                workspace_id="ws", entity_id="person:placeholder", entity_type="person", canonical_name="placeholder",
                aliases=(placeholder_alias,), scope="org", created_at=now,
            ),
        )
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
    if person_status is not None:
        existing_people = load_people_directory(knowledge_root / "people_directory.yaml")
        people = existing_people.people if existing_people is not None else ()
        people = tuple(p for p in people if p.entity_id != entity_id) + (
            PersonDirectory(entity_id=entity_id, alias=alias, status=PersonStatus(person_status)),
        )
        write_people_directory(knowledge_root / "people_directory.yaml", people)
    return entity_id


def test_run_kb_doctor_dir04_check_ok_with_no_program_stakeholder_entries(tmp_path: Path) -> None:
    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    check = next(c for c in report.checks if c.code == "DIR-04")
    assert check.status == "ok"


def test_run_kb_doctor_dir04_check_ok_with_an_active_stakeholder(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program_with_stakeholder(programs_root, "acme", alias="jdoe", person_status="active")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-04")
    assert check.status == "ok"


def test_run_kb_doctor_dir04_check_warns_on_a_departed_stakeholder(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program_with_stakeholder(programs_root, "acme", alias="jdoe", person_status="departed")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-04")
    assert check.status == "warn"
    assert "departed" in check.detail


def test_run_kb_doctor_dir04_check_warns_on_an_unresolvable_stakeholder_alias(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program_with_stakeholder(programs_root, "acme", alias="jdoe", create_entity=False, person_status=None)

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-04")
    assert check.status == "warn"
    assert "does not resolve" in check.detail


def test_run_kb_doctor_dir04_check_warns_on_an_ambiguous_stakeholder_with_no_directory_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program_with_stakeholder(programs_root, "acme", alias="jdoe", create_entity=True, person_status=None)

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-04")
    assert check.status == "warn"


def test_run_kb_doctor_dir04_check_still_fires_when_the_programs_own_other_files_are_malformed(tmp_path: Path) -> None:
    """PPL-W3.5e's own documented, deliberate behavior difference,
    proven end-to-end through the real doctor loop: a malformed
    workstreams.yaml previously made the full `load_program_context`
    raise, which `_load_program_stakeholder_aliases`'s old
    except-ConfigError-then-skip pattern silently absorbed -- the
    program's real, valid stakeholder data never reached DIR-04 at all.
    The new `load_program_stakeholder_aliases` accessor never touches
    workstreams.yaml, so this program's departed stakeholder is now
    correctly flagged."""
    programs_root = tmp_path / "programs"
    _seed_program_with_stakeholder(programs_root, "acme", alias="jdoe", person_status="departed")
    (programs_root / "acme" / "workstreams.yaml").write_text("not_a_mapping\n", encoding="utf-8")

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    check = next(c for c in report.checks if c.code == "DIR-04")
    assert check.status == "warn"
    assert "departed" in check.detail


def test_run_kb_doctor_dir12_checks_ok_with_no_open_conflicts(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=tmp_path / "programs")

    dir12a = next(c for c in report.checks if c.code == "DIR-12A")
    dir12b = next(c for c in report.checks if c.code == "DIR-12B")
    assert dir12a.status == "ok"
    assert dir12b.status == "ok"


def test_run_kb_doctor_dir12a_check_warns_on_conflict_with_accountability_reference(tmp_path: Path) -> None:
    from src.core.people_change_journal import append_people_conflict_record
    from src.core.people_registry_identity import load_registry_manifest

    knowledge_root = tmp_path / "knowledge"
    programs_root = tmp_path / "programs"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    entity_id = _seed_program_with_stakeholder(programs_root, "acme", alias="jdoe", person_status="active")
    manifest = load_registry_manifest(knowledge_root)
    append_people_conflict_record(
        knowledge_root, workspace_id=manifest.workspace_id, conflict_id="conflict-1", decision="conflict",
        authenticated_principal="steward", reason="ambiguous alias", entity_id=entity_id,
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    dir12a = next(c for c in report.checks if c.code == "DIR-12A")
    dir12b = next(c for c in report.checks if c.code == "DIR-12B")
    assert dir12a.status == "warn"
    assert "WITH an active accountability reference" in dir12a.detail
    assert dir12b.status == "ok"


def test_run_kb_doctor_dir12b_check_warns_on_conflict_without_accountability_reference(tmp_path: Path) -> None:
    from src.core.people_change_journal import append_people_conflict_record
    from src.core.people_registry_identity import load_registry_manifest

    knowledge_root = tmp_path / "knowledge"
    programs_root = tmp_path / "programs"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    manifest = load_registry_manifest(knowledge_root)
    append_people_conflict_record(
        knowledge_root, workspace_id=manifest.workspace_id, conflict_id="conflict-1", decision="conflict",
        authenticated_principal="steward", reason="ambiguous alias", entity_id="person:unreferenced",
    )

    report = run_kb_doctor(editions_root=tmp_path / "editions", programs_root=programs_root)

    dir12a = next(c for c in report.checks if c.code == "DIR-12A")
    dir12b = next(c for c in report.checks if c.code == "DIR-12B")
    assert dir12a.status == "ok"
    assert dir12b.status == "warn"
    assert "without an active accountability reference" in dir12b.detail
