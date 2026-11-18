"""Focused PPL-W2B.4 shared factual writer coverage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import onboard as onboard_module
from src.commands.onboard import (
    ADOStage,
    IdentityStage,
    OnboardDocuments,
    OnboardDraft,
    OnboardPaths,
    OnboardValidationResult,
    PeopleStage,
    WorkstreamStage,
)
from src.core import ncfl_apply, people_registry_lease, people_registry_transaction, people_registry_writer
from src.core.exceptions import ConfigError
from src.core.ncfl_apply import apply_proposal, read_target_prior_state
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import stage_extracted_proposals, update_proposal_status
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, read_journal_records
from src.core.people_directory_schema import PersonDirectory, PersonStatus, Team, TeamKind, TeamStatus, load_people_directory, load_teams, write_people_directory, write_teams
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, load_entities_document, write_entities_document
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_lease import (
    RegistryLeaseFencingTokenStale,
    acquire_registry_lease,
    read_registry_lease_state,
    release_registry_lease,
)
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction, recover_registry_transactions
from src.core.people_registry_writer import (
    RegistryPatchOperation,
    apply_shared_registry_patch,
    register_onboarding_facts,
    shared_registry_is_active,
)
from src.core.program_fact_store import load_program_facts, project_workstreams
from src.core.workstream_documents import save_workstreams_document


_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_RUNNER = CliRunner()


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
        verified_by_principal="test-principal",
    )


def _seed_active_registry(programs_root: Path) -> Path:
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(
        knowledge_root=knowledge_root,
        customer_boundary_id="synthetic",
        apply=True,
        as_of=_NOW,
    )
    entities = EntitiesDocument(
        schema_version="2.0",
        entities=(
            CanonicalEntity(
                workspace_id="workspace:synthetic",
                entity_id="person:alice",
                entity_type="person",
                canonical_name="Alice",
                aliases=(_alias("alice"),),
                scope="org",
                created_at=_NOW,
            ),
            CanonicalEntity(
                workspace_id="workspace:synthetic",
                entity_id="team:platform",
                entity_type="team",
                canonical_name="Platform",
                aliases=(_alias("platform"),),
                scope="org",
                created_at=_NOW,
            ),
        ),
    )
    person = PersonDirectory(
        entity_id="person:alice",
        alias="alice",
        display_name="Alice",
        title="PM",
        status=PersonStatus.ACTIVE,
    )
    team = Team(
        entity_id="team:platform",
        id="platform",
        name="Platform",
        kind=TeamKind.ORG_TEAM,
        status=TeamStatus.ACTIVE,
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", entities)
        write_people_directory(staged_dir / "people_directory.yaml", (person,))
        write_teams(staged_dir / "teams.yaml", (team,))

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None
        assert load_teams(staged_dir / "teams.yaml") is not None

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        ("entities.yaml", "people_directory.yaml", "teams.yaml"),
        owner="test-principal",
        write_staged_files=write_staged,
        validate_staged_files=validate_staged,
        as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)
    return knowledge_root


def test_stale_shared_file_transaction_cannot_commit_after_successor_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    knowledge_root = _seed_active_registry(tmp_path / "programs")
    people_path = knowledge_root / "people_directory.yaml"
    original_people = people_path.read_bytes()
    manifest_path = knowledge_root / "registry_manifest.json"
    original_manifest = manifest_path.read_bytes()

    def write_staged(staged_dir: Path) -> None:
        staged_dir.mkdir(parents=True, exist_ok=True)
        (staged_dir / "people_directory.yaml").write_bytes(original_people + b"\n")

    def validate_staged(staged_dir: Path) -> None:
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        ("people_directory.yaml",),
        owner="test-principal",
        write_staged_files=write_staged,
        validate_staged_files=validate_staged,
        as_of=_NOW,
    )
    monkeypatch.setattr(people_registry_lease, "_now", lambda: prepared.lease.expires_at + timedelta(microseconds=1))
    successor = acquire_registry_lease("test-principal", knowledge_root=knowledge_root)
    release_registry_lease(successor, knowledge_root=knowledge_root)
    current = acquire_registry_lease("test-principal", knowledge_root=knowledge_root)

    with pytest.raises(RegistryLeaseFencingTokenStale):
        commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)

    assert people_path.read_bytes() == original_people
    assert manifest_path.read_bytes() == original_manifest
    assert read_registry_lease_state(knowledge_root=knowledge_root) == current


def test_stale_single_file_recovery_preserves_untouched_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_root = _seed_active_registry(tmp_path / "programs")
    people_path = knowledge_root / "people_directory.yaml"
    original_people = people_path.read_bytes()
    untouched = {name: (knowledge_root / name).read_bytes() for name in ("entities.yaml", "teams.yaml")}

    def write_staged(staged_dir: Path) -> None:
        staged_dir.mkdir(parents=True, exist_ok=True)
        (staged_dir / people_path.name).write_bytes(original_people + b"\n")

    prepared = prepare_registry_files_transaction(
        knowledge_root, (people_path.name,), owner="test-principal",
        write_staged_files=write_staged, validate_staged_files=lambda path: None,
    )
    write_state = people_registry_transaction._write_json_atomic

    def write_state_and_release(path: Path, payload: dict) -> None:
        write_state(path, payload)
        release_registry_lease(prepared.lease, knowledge_root=knowledge_root)

    monkeypatch.setattr(people_registry_transaction, "_write_json_atomic", write_state_and_release)
    with pytest.raises(RegistryLeaseFencingTokenStale):
        commit_registry_files_transaction(prepared, knowledge_root=knowledge_root)

    state = json.loads((prepared.staged_dir / "state.json").read_text(encoding="utf-8"))
    assert state["relative_paths"] == [people_path.name]
    assert set(untouched) <= set(state["new_data_hashes"])
    recover_registry_transactions(knowledge_root)
    assert people_path.read_bytes() == original_people
    for name, data in untouched.items():
        assert (knowledge_root / name).read_bytes() == data


def test_expired_lease_refuses_shared_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    knowledge_root = _seed_active_registry(tmp_path / "programs")
    people_path = knowledge_root / "people_directory.yaml"
    original_people = people_path.read_bytes()
    manifest = (knowledge_root / "registry_manifest.json").read_bytes()

    def write_staged(staged_dir: Path) -> None:
        staged_dir.mkdir(parents=True, exist_ok=True)
        (staged_dir / people_path.name).write_bytes(original_people + b"\n")

    prepared = prepare_registry_files_transaction(
        knowledge_root, (people_path.name,), owner="test-principal",
        write_staged_files=write_staged, validate_staged_files=lambda path: None,
    )
    monkeypatch.setattr(people_registry_lease, "_now", lambda: prepared.lease.expires_at)
    with pytest.raises(ConfigError, match="expired"):
        commit_registry_files_transaction(prepared, knowledge_root=knowledge_root)

    assert people_path.read_bytes() == original_people
    assert (knowledge_root / "registry_manifest.json").read_bytes() == manifest


def test_shared_expiry_during_replacement_recovers_only_transaction_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_root = _seed_active_registry(tmp_path / "programs")
    names = ("people_directory.yaml", "teams.yaml")
    original = {name: (knowledge_root / name).read_bytes() for name in (*names, "entities.yaml")}
    manifest = (knowledge_root / "registry_manifest.json").read_bytes()

    def write_staged(staged_dir: Path) -> None:
        staged_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            (staged_dir / name).write_bytes(original[name] + b"\n")

    prepared = prepare_registry_files_transaction(
        knowledge_root, names, owner="test-principal",
        write_staged_files=write_staged, validate_staged_files=lambda path: None,
    )
    replace = people_registry_transaction._replace_with_bounded_retry

    def replace_and_expire(source: Path, destination: Path) -> None:
        replace(source, destination)
        monkeypatch.setattr(people_registry_lease, "_now", lambda: prepared.lease.expires_at)

    monkeypatch.setattr(people_registry_transaction, "_replace_with_bounded_retry", replace_and_expire)
    with pytest.raises(ConfigError, match="expired"):
        commit_registry_files_transaction(prepared, knowledge_root=knowledge_root)

    assert (knowledge_root / names[0]).read_bytes() != original[names[0]]
    assert (knowledge_root / names[1]).read_bytes() == original[names[1]]
    assert (knowledge_root / "registry_manifest.json").read_bytes() == manifest
    recover_registry_transactions(knowledge_root)
    for name, data in original.items():
        assert (knowledge_root / name).read_bytes() == data


@pytest.mark.parametrize("lease_change", ["expiry", "successor"])
def test_shared_manifest_refuses_changed_lease_after_last_data_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lease_change: str,
) -> None:
    knowledge_root = _seed_active_registry(tmp_path / "programs")
    people_path = knowledge_root / "people_directory.yaml"
    original_people = people_path.read_bytes()
    untouched = (knowledge_root / "entities.yaml").read_bytes()
    manifest = (knowledge_root / "registry_manifest.json").read_bytes()

    def write_staged(staged_dir: Path) -> None:
        staged_dir.mkdir(parents=True, exist_ok=True)
        (staged_dir / people_path.name).write_bytes(original_people + b"\n")

    prepared = prepare_registry_files_transaction(
        knowledge_root, (people_path.name,), owner="test-principal",
        write_staged_files=write_staged, validate_staged_files=lambda path: None,
    )
    replace = people_registry_transaction._replace_with_bounded_retry

    def replace_and_change_lease(source: Path, destination: Path) -> None:
        replace(source, destination)
        if source.parent == prepared.staged_dir:
            monkeypatch.setattr(people_registry_lease, "_now", lambda: prepared.lease.expires_at)
            if lease_change == "successor":
                acquire_registry_lease("successor", knowledge_root=knowledge_root)

    monkeypatch.setattr(people_registry_transaction, "_replace_with_bounded_retry", replace_and_change_lease)
    expected_error = ConfigError if lease_change == "expiry" else RegistryLeaseFencingTokenStale
    with pytest.raises(expected_error):
        commit_registry_files_transaction(prepared, knowledge_root=knowledge_root)

    assert people_path.read_bytes() != original_people
    assert (knowledge_root / "registry_manifest.json").read_bytes() == manifest
    # Recovery is only exercised after the synthetic successor is released.
    if lease_change == "successor":
        successor = read_registry_lease_state(knowledge_root=knowledge_root)
        assert successor is not None
        release_registry_lease(successor, knowledge_root=knowledge_root)
    recover_registry_transactions(knowledge_root)
    assert people_path.read_bytes() == original_people
    assert (knowledge_root / "entities.yaml").read_bytes() == untouched


def test_onboarding_registration_uses_canonical_writer_and_classifies_workstreams_as_program_groups(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)

    result = register_onboarding_facts(
        program_id="new-program",
        people=(
            onboard_module.OnboardingPerson(
                alias="bob",
                email="bob@example.com",
                display_name="Bob",
                team_ids=("delivery",),
            ),
        ),
        groups=(
            onboard_module.OnboardingProgramGroup(
                id="delivery",
                name="Delivery",
                area_paths=(r"Area\Delivery",),
            ),
        ),
        programs_root=programs_root,
        actor="test-principal",
        reason="vertex onboard --register-shared new_weekly",
        source_ref="edition:new_weekly",
        apply=True,
        as_of=_NOW,
    )

    assert result.transaction_id is not None
    assert result.generation_id == load_registry_manifest(knowledge_root).generation_id
    teams = load_teams(knowledge_root / "teams.yaml")
    assert teams is not None
    delivery = next(team for team in teams.teams if team.id == "delivery")
    assert delivery.kind == TeamKind.PROGRAM_GROUP
    assert (programs_root / "new-program" / "knowledge" / "people_directory.yaml").exists() is False
    changes = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert any(change["source"] == "onboarding_operator" and change["authenticated_principal"] == "test-principal" for change in changes)


def test_kb_update_cli_routes_active_shared_person_change_through_registry_writer(monkeypatch, tmp_path: Path) -> None:
    import src.core.kb_updates as kb_updates

    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    program_dir = programs_root / "demo"
    (program_dir / "knowledge").mkdir(parents=True)
    (program_dir / "program.yaml").write_text("id: demo\n", encoding="utf-8")
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_OPERATOR_PRINCIPAL", "test-principal")

    def reject_workstream_save(*args, **kwargs):
        raise AssertionError("Shared-only corrections must not save workstreams.")

    monkeypatch.setattr(kb_updates, "save_workstreams_document", reject_workstream_save, raising=False)
    result = _RUNNER.invoke(app, ["kb", "update", "Set alice title to Director", "--program", "demo", "--apply", "--no-ai"])

    assert result.exit_code == 0, result.output
    assert "Applied shared registry generation" in result.stdout
    shared_people = load_people_directory(knowledge_root / "people_directory.yaml")
    assert shared_people is not None
    assert shared_people.people[0].title == "Director"
    assert not (program_dir / "knowledge" / "people_directory.yaml").exists()
    assert any(
        change["source"] == "kb_update"
        and change["authenticated_principal"] == "test-principal"
        and change["field"] == "person.title"
        for change in read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    )


@pytest.mark.parametrize("mixed", [False, True])
def test_kb_update_keeps_unrelated_workstream_path_compatible_with_active_shared_registry(
    monkeypatch, tmp_path: Path, mixed: bool,
) -> None:
    from src.core.kb_updates import KbUpdateOperation, KbUpdatePlan

    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    shared_before = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    program_dir = programs_root / "demo"
    (program_dir / "knowledge").mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: '2.0'\nid: demo\nname: Demo\n", encoding="utf-8")
    save_workstreams_document(
        "demo", {
            "schema_version": "2.0",
            "workstreams": [{"id": "delivery", "name": "Delivery", "pm_owner": "alice"}],
        }, programs_root=programs_root,
    )
    (program_dir / "scorecards.yaml").write_text(
        "schema_version: '2.0'\nscorecards:\n  - name: Delivery\n    dimensions:\n      - name: Health\n        workstream_id: delivery\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    if mixed:
        monkeypatch.setenv("VERTEX_OPERATOR_PRINCIPAL", "test-principal")
        monkeypatch.setattr("src.commands.kb.parse_deterministic_kb_correction", lambda *args, **kwargs: KbUpdatePlan(
            "demo", "synthetic mixed correction", "deterministic", (
                KbUpdateOperation("workstreams.yaml", "set_fields", "delivery", fields=(("pm_owner", None),)),
                KbUpdateOperation("knowledge/people_directory.yaml", "set_fields", "alice", fields=(("title", "Director"),)),
            ),
        ))

    result = _RUNNER.invoke(
        app,
        ["kb", "update", "Clear delivery pm owner", "--program", "demo", "--apply", "--no-ai"],
    )

    assert result.exit_code == 0, result.output
    assert ("Applied shared registry generation" if mixed else "Applied 1 file(s).") in result.stdout
    assert "pm_owner: null" in (program_dir / "workstreams.yaml").read_text(encoding="utf-8")
    persisted = project_workstreams(load_program_facts(
        "demo", programs_root=programs_root, as_of=datetime.now(timezone.utc),
    ))
    assert persisted[0].pm_owner is None
    shared_after = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    if mixed:
        assert any(
            change["source"] == "kb_update" and change["field"] == "person.title"
            and change["authenticated_principal"] == "test-principal"
            for change in shared_after[len(shared_before):]
        )
    else:
        assert shared_after == shared_before


def test_ncfl_people_directory_apply_routes_through_shared_registry_writer(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    proposal = ContextUpdateProposal(
        proposal_id="proposal-people-1",
        program_id="demo",
        issue_number=1,
        edition_id="demo_weekly",
        source_type="field_update_email",
        extracted_at=_NOW,
        extractor_version="1.0.0",
        source_artifact="synthetic",
        source_field="people.alice.title",
        extraction_method="field_update_email",
        target_store="people_directory",
        target_key="alice",
        target_field="title",
        source_value="Principal PM",
        current_value="PM",
        current_value_hash=None,
        prior_state="sha256:" + sha256(b"PM").hexdigest(),
        confidence="high",
        batch_eligible=False,
        extraction_method_rationale="synthetic",
        conflict_key="people_directory:alice:title",
    )
    stage_extracted_proposals("demo", 1, (proposal,), programs_root=programs_root)
    proposal = update_proposal_status(
        "demo", proposal_id="proposal-people-1", new_status="accepted", actor="test-reviewer",
        issue_number=1, programs_root=programs_root,
    )

    result = apply_proposal(proposal, actor="test-principal", programs_root=programs_root)

    assert result.action == "applied"
    assert shared_registry_is_active(programs_root)
    people = load_people_directory(knowledge_root / "people_directory.yaml")
    assert people is not None and people.people[0].title == "Principal PM"
    assert not (programs_root / "demo" / "knowledge" / "people_directory.yaml").exists()
    assert any(
        change["source"] == "ncfl_apply"
        and change["source_ref"] == "proposal-people-1"
        and change["authenticated_principal"] == "test-principal"
        for change in read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    )


def _accepted_registry_proposal(
    programs_root: Path, target_store: str, target_key: str, target_field: str,
    baseline: str, source_value: str = "Proposed",
) -> ContextUpdateProposal:
    proposal = ContextUpdateProposal(
        proposal_id="proposal-race",
        program_id="demo",
        issue_number=1,
        edition_id="demo_weekly",
        source_type="field_update_email",
        extracted_at=_NOW,
        extractor_version="1.0.0",
        source_artifact="synthetic",
        source_field="synthetic",
        extraction_method="field_update_email",
        target_store=target_store,
        target_key=target_key,
        target_field=target_field,
        source_value=source_value,
        current_value=baseline,
        current_value_hash=None,
        prior_state="sha256:" + sha256(baseline.encode()).hexdigest(),
        confidence="high",
        batch_eligible=False,
        extraction_method_rationale="synthetic",
        conflict_key=f"{target_store}:{target_key}:{target_field}",
    )
    stage_extracted_proposals("demo", 1, (proposal,), programs_root=programs_root)
    return update_proposal_status(
        "demo", proposal_id=proposal.proposal_id, new_status="accepted", actor="test-reviewer",
        issue_number=1, programs_root=programs_root,
    )


@pytest.mark.parametrize("source_value", ("Proposed", "PM"))
def test_ncfl_same_principal_live_registry_lease_refuses_without_overwrite(
    tmp_path: Path, source_value: str,
) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    proposal = _accepted_registry_proposal(
        programs_root, "people_directory", "alice", "title", "PM", source_value=source_value,
    )
    lease = acquire_registry_lease("test-principal", knowledge_root=knowledge_root)
    people_path = knowledge_root / "people_directory.yaml"
    original_people = people_path.read_bytes()
    original_manifest = (knowledge_root / "registry_manifest.json").read_bytes()

    result = apply_proposal(proposal, actor="test-principal", programs_root=programs_root)

    assert result.action == "needs_repair"
    assert "lease" in result.note.lower()
    assert people_path.read_bytes() == original_people
    assert (knowledge_root / "registry_manifest.json").read_bytes() == original_manifest
    assert read_registry_lease_state(knowledge_root=knowledge_root) == lease
    journal = programs_root / "demo" / "_ncfl_apply" / f"{proposal.proposal_id}.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["state"] == "needs_repair"
    release_registry_lease(lease, knowledge_root=knowledge_root)


@pytest.mark.parametrize("target_store,target_key,target_field,baseline,interleave", [
    (store, key, field, baseline, interleave)
    for store, key, field, baseline in [
        ("people_directory", "alice", "title", "PM"),
        ("teams", "platform", "name", "Platform"),
    ]
    for interleave in ["before_read", "before_writer", "before_lease", "before_noop_preview", "dry_run"]
] + [("teams", "platform", "name", "Platform", "noop_before_lease")])
def test_ncfl_shared_registry_update_after_check_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    target_store: str, target_key: str, target_field: str, baseline: str, interleave: str,
) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    source_value = baseline if interleave == "noop_before_lease" else "Proposed"
    accepted = _accepted_registry_proposal(
        programs_root, target_store, target_key, target_field, baseline, source_value=source_value,
    )
    competing_value = "Proposed" if interleave == "before_noop_preview" else "Concurrent"
    competing_store, competing_key, competing_field = (
        ("teams", "platform", "name") if target_store == "people_directory"
        else ("people_directory", "alice", "title")
    ) if interleave == "before_read" else (target_store, target_key, target_field)
    injected = False

    def competing_update() -> None:
        nonlocal injected
        injected = True
        apply_shared_registry_patch(
            operations=(RegistryPatchOperation(
                relative_path=f"knowledge/{competing_store}.yaml",
                action="set_fields",
                match_value=competing_key,
                fields=((competing_field, competing_value),),
            ),),
            programs_root=programs_root,
            actor="other-principal",
            reason="synthetic concurrent update",
            source="kb_update",
            apply=True,
        )

    if interleave == "before_read":
        original_read = ncfl_apply._read_current_value

        def read_before(*args, **kwargs):
            competing_update()
            return original_read(*args, **kwargs)

        monkeypatch.setattr(ncfl_apply, "_read_current_value", read_before)
    elif interleave == "before_writer":
        original_write = ncfl_apply._write_to_store

        def write(*args, **kwargs):
            competing_update()
            return original_write(*args, **kwargs)

        monkeypatch.setattr(ncfl_apply, "_write_to_store", write)
    elif interleave == "before_lease":
        original_prepare = people_registry_writer.prepare_registry_files_transaction

        def prepare(*args, **kwargs):
            if not injected:
                competing_update()
            return original_prepare(*args, **kwargs)

        monkeypatch.setattr(people_registry_writer, "prepare_registry_files_transaction", prepare)
    elif interleave == "before_noop_preview":
        original_load = people_registry_writer._load_state
        loads = 0

        def load(*args, **kwargs):
            nonlocal loads
            loads += 1
            if loads == 2 and not injected:
                competing_update()
            return original_load(*args, **kwargs)

        monkeypatch.setattr(people_registry_writer, "_load_state", load)
    elif interleave == "noop_before_lease":
        original_acquire = people_registry_writer.acquire_registry_lease

        def acquire(*args, **kwargs):
            competing_update()
            return original_acquire(*args, **kwargs)

        monkeypatch.setattr(people_registry_writer, "acquire_registry_lease", acquire)
    else:
        original_read = ncfl_apply._read_current_value

        def read(*args, **kwargs):
            value = original_read(*args, **kwargs)
            competing_update()
            return value

        monkeypatch.setattr(ncfl_apply, "_read_current_value", read)

    result = apply_proposal(
        accepted, actor="test-principal", programs_root=programs_root, dry_run=interleave == "dry_run",
    )

    assert injected
    assert result.action == ("skipped_stale" if interleave == "dry_run" else "needs_repair")
    assert "generation" in result.note.lower()
    document = (
        load_people_directory(knowledge_root / "people_directory.yaml")
        if competing_store == "people_directory"
        else load_teams(knowledge_root / "teams.yaml")
    )
    assert document is not None
    record = document.people[0] if competing_store == "people_directory" else document.teams[0]
    assert getattr(record, competing_field) == competing_value
    changes = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert not any(change["source_ref"] == accepted.proposal_id for change in changes)
    journal = programs_root / "demo" / "_ncfl_apply" / f"{accepted.proposal_id}.json"
    if interleave == "dry_run":
        assert not journal.exists()
    else:
        journal_bytes = journal.read_bytes()
        assert json.loads(journal_bytes)["state"] == "needs_repair"
        assert apply_proposal(accepted, actor="test-principal", programs_root=programs_root).action == "needs_repair"
        assert journal.read_bytes() == journal_bytes


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("corruption", ["missing", "malformed"])
def test_ncfl_shared_registry_unavailable_generation_refuses_without_journal(
    tmp_path: Path, dry_run: bool, corruption: str,
) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    accepted = _accepted_registry_proposal(programs_root, "teams", "platform", "name", "Platform")
    manifest_path = knowledge_root / "registry_manifest.json"
    if corruption == "missing":
        manifest_path.unlink()
    else:
        manifest_path.write_text('{"generation_id":', encoding="utf-8")
    teams_before = (knowledge_root / "teams.yaml").read_bytes()

    result = apply_proposal(accepted, actor="test-principal", programs_root=programs_root, dry_run=dry_run)

    assert result.action == "skipped_stale"
    assert "generation is unavailable" in result.note
    assert (knowledge_root / "teams.yaml").read_bytes() == teams_before
    assert not (programs_root / "demo" / "_ncfl_apply" / f"{accepted.proposal_id}.json").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_ncfl_unchanged_shared_team_proposal_remains_supported(tmp_path: Path, dry_run: bool) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    accepted = _accepted_registry_proposal(
        programs_root, "teams", "platform", "name", "Platform", source_value="Platform",
    )
    generation = load_registry_manifest(knowledge_root)
    assert generation is not None
    teams_before = (knowledge_root / "teams.yaml").read_bytes()

    result = apply_proposal(accepted, actor="test-principal", programs_root=programs_root, dry_run=dry_run)

    assert result.action == "applied"
    assert load_registry_manifest(knowledge_root) == generation
    assert (knowledge_root / "teams.yaml").read_bytes() == teams_before


@pytest.mark.parametrize("target_store,target_key,target_field", [
    ("people_directory", "alice", "email"),
    ("teams", "platform", "name"),
])
@pytest.mark.parametrize("bootstrapped", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_ncfl_inactive_shared_registry_is_unavailable(
    tmp_path: Path, target_store: str, target_key: str, target_field: str,
    bootstrapped: bool, dry_run: bool,
) -> None:
    programs_root = tmp_path / "programs"
    if bootstrapped:
        bootstrap_registry_identity(
            knowledge_root=tmp_path / "knowledge",
            customer_boundary_id="synthetic",
            apply=True,
            as_of=_NOW,
        )
    assert not shared_registry_is_active(programs_root)
    proposal = ContextUpdateProposal(
        proposal_id="proposal-inactive",
        program_id="demo",
        issue_number=1,
        edition_id="demo_weekly",
        source_type="field_update_email",
        extracted_at=_NOW,
        extractor_version="1.0.0",
        source_artifact="synthetic",
        source_field="synthetic",
        extraction_method="field_update_email",
        target_store=target_store,
        target_key=target_key,
        target_field=target_field,
        source_value="new value",
        current_value=None,
        current_value_hash=None,
        prior_state="absent_verified",
        confidence="high",
        batch_eligible=False,
        extraction_method_rationale="synthetic",
        conflict_key=f"{target_store}:{target_key}:{target_field}",
    )
    assert read_target_prior_state(proposal, programs_root=programs_root) is None
    stage_extracted_proposals("demo", 1, (proposal,), programs_root=programs_root)
    accepted = update_proposal_status(
        "demo", proposal_id=proposal.proposal_id, new_status="accepted", actor="test-reviewer",
        issue_number=1, programs_root=programs_root,
    )

    result = apply_proposal(accepted, actor="test-principal", programs_root=programs_root, dry_run=dry_run)

    assert result.action == "skipped_stale"
    assert "unavailable" in result.note
    assert not (programs_root / "demo" / "_ncfl_apply" / "proposal-inactive.json").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_ncfl_active_shared_registry_missing_email_is_verified_absent(tmp_path: Path, dry_run: bool) -> None:
    programs_root = tmp_path / "programs"
    _seed_active_registry(programs_root)
    proposal = ContextUpdateProposal(
        proposal_id="proposal-absent",
        program_id="demo",
        issue_number=1,
        edition_id="demo_weekly",
        source_type="field_update_email",
        extracted_at=_NOW,
        extractor_version="1.0.0",
        source_artifact="synthetic",
        source_field="people.alice.email",
        extraction_method="field_update_email",
        target_store="people_directory",
        target_key="alice",
        target_field="email",
        source_value="alice@example.test",
        current_value=None,
        current_value_hash=None,
        prior_state="absent_verified",
        confidence="high",
        batch_eligible=False,
        extraction_method_rationale="synthetic",
        conflict_key="people_directory:alice:email",
    )
    assert read_target_prior_state(proposal, programs_root=programs_root) == "absent_verified"
    stage_extracted_proposals("demo", 1, (proposal,), programs_root=programs_root)
    accepted = update_proposal_status(
        "demo", proposal_id=proposal.proposal_id, new_status="accepted", actor="test-reviewer",
        issue_number=1, programs_root=programs_root,
    )

    result = apply_proposal(accepted, actor="test-principal", programs_root=programs_root, dry_run=dry_run)

    assert result.action == "applied"
    expected = "absent_verified" if dry_run else "sha256:" + sha256(b"alice@example.test").hexdigest()
    assert read_target_prior_state(proposal, programs_root=programs_root) == expected


def test_onboard_finalize_never_writes_shadowed_local_people_when_shared_root_exists(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_active_registry(programs_root)
    reports_root = tmp_path / "reports"
    program_dir = programs_root / "new-program"
    paths = OnboardPaths(
        repo_root=tmp_path,
        reports_root=reports_root,
        editions_root=program_dir / "editions",
        programs_root=programs_root,
        edition_path=program_dir / "editions" / "new_weekly.yaml",
        program_dir=program_dir,
        knowledge_dir=program_dir / "knowledge",
    )
    documents = OnboardDocuments(
        edition={"schema_version": "2.0", "id": "new_weekly"},
        program={"schema_version": "3.0", "id": "new-program"},
        workstreams={"schema_version": "2.0", "workstreams": []},
        scorecards={"schema_version": "2.0", "scorecards": []},
        editorial_rules={"schema_version": "1.0"},
        review={"schema_version": "1.0"},
        people_directory={"schema_version": "1.0", "people": [{"alias": "shadow"}]},
        teams={"schema_version": "1.0", "teams": [{"id": "shadow"}]},
        products={"schema_version": "1.0", "products": []},
        golden_queries={"schema_version": "1.0", "queries": []},
    )
    draft = OnboardDraft(
        identity=IdentityStage(
            program_name="New Program",
            program_id="new-program",
            objective="Objective",
            mission="Mission",
            newsletter_title="New",
            cadence="weekly",
            author_display_name="New Author",
            author_email="new.author@example.com",
            send_day=None,
            send_time_local=None,
            timezone=None,
        ),
        ado=ADOStage("org", "project", (), (), (), 14, 30),
        people=PeopleStage(
            workstreams=(
                WorkstreamStage(
                    name="Delivery",
                    aliases=(),
                    area_paths=(),
                    dri_email="new.author@example.com",
                    alternate_owner=None,
                    description=None,
                ),
            ),
            reviewers=(),
        ),
    )
    monkeypatch.setenv("VERTEX_OPERATOR_PRINCIPAL", "test-principal")
    monkeypatch.setattr(
        onboard_module,
        "_run_onboard_validation",
        lambda **_kwargs: OnboardValidationResult(),
    )
    monkeypatch.setattr(onboard_module, "_write_readme", lambda *_args, **_kwargs: None)

    result = onboard_module._finalize_onboarding("new_weekly", paths, documents, draft)

    assert result.shared_registry_transaction_id is not None
    assert not (paths.knowledge_dir / "people_directory.yaml").exists()
    assert not (paths.knowledge_dir / "teams.yaml").exists()
