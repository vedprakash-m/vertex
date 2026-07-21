"""Focused PPL-W2B.4 shared factual writer coverage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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
from src.core.ncfl_apply import apply_proposal
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import stage_extracted_proposals
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, read_journal_records
from src.core.people_directory_schema import PersonDirectory, PersonStatus, Team, TeamKind, TeamStatus, load_people_directory, load_teams, write_people_directory, write_teams
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, load_entities_document, write_entities_document
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction
from src.core.people_registry_writer import (
    RegistryPatchOperation,
    apply_shared_registry_patch,
    register_onboarding_facts,
    shared_registry_is_active,
)


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
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    program_dir = programs_root / "demo"
    (program_dir / "knowledge").mkdir(parents=True)
    (program_dir / "program.yaml").write_text("id: demo\n", encoding="utf-8")
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_OPERATOR_PRINCIPAL", "test-principal")

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


def test_kb_update_keeps_unrelated_workstream_path_compatible_with_active_shared_registry(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_active_registry(programs_root)
    program_dir = programs_root / "demo"
    (program_dir / "knowledge").mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: '2.0'\nid: demo\nname: Demo\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text(
        "schema_version: '2.0'\nworkstreams:\n  - id: delivery\n    name: Delivery\n    pm_owner: alice\n",
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text(
        "schema_version: '2.0'\nscorecards:\n  - name: Delivery\n    dimensions:\n      - name: Health\n        workstream_id: delivery\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = _RUNNER.invoke(
        app,
        ["kb", "update", "Clear delivery pm owner", "--program", "demo", "--apply", "--no-ai"],
    )

    assert result.exit_code == 0, result.output
    assert "Applied 1 file(s)." in result.stdout
    assert "pm_owner: null" in (program_dir / "workstreams.yaml").read_text(encoding="utf-8")


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
        confidence="high",
        batch_eligible=False,
        extraction_method_rationale="synthetic",
        conflict_key="people_directory:alice:title",
        status="accepted",
    )
    stage_extracted_proposals("demo", 1, (proposal,), programs_root=programs_root)

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
