from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.program_reality import FleetReality


@dataclass(frozen=True)
class _StubProgram:
    """`FleetReality.people_programs` only calls `self.program_ids()`,
    which only reads `.program_id` off each entry -- a lightweight stub
    keeps this test focused on the new method's own logic instead of
    paying for a full `ProgramReality.load()`."""

    program_id: str


def _seed_program(programs_root: Path, program_id: str, *, archived: bool = False) -> Path:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    archived_line = "archived: true\n" if archived else ""
    (program_dir / "program.yaml").write_text(
        f'schema_version: "3.0"\nid: "{program_id}"\nname: "{program_id.title()}"\n{archived_line}', encoding="utf-8"
    )
    editions_dir = program_dir / "editions"
    editions_dir.mkdir(parents=True, exist_ok=True)
    (editions_dir / f"{program_id}_weekly.yaml").write_text('schema_version: "2.0"\n', encoding="utf-8")
    return program_dir


def _seed_stakeholder(program_dir: Path, *, alias: str) -> None:
    (program_dir / "program.yaml").write_text(
        (program_dir / "program.yaml").read_text(encoding="utf-8")
        + f'stakeholder_register:\n  - alias: "{alias}"\n    role: "PM"\n',
        encoding="utf-8",
    )


def test_people_programs_returns_legacy_edges_for_an_active_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root, "acme")
    _seed_stakeholder(program_dir, alias="alice")

    fleet = FleetReality((_StubProgram(program_id="acme"),))
    edges = fleet.people_programs("alice", programs_root=programs_root)

    assert any(edge.program_id == "acme" for edge in edges)


def test_people_programs_excludes_archived_programs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root, "acme", archived=True)
    _seed_stakeholder(program_dir, alias="alice")

    fleet = FleetReality((_StubProgram(program_id="acme"),))
    edges = fleet.people_programs("alice", programs_root=programs_root)

    assert edges == ()


def test_people_programs_matches_find_alias_edges_for_a_legacy_mode_program(tmp_path: Path) -> None:
    from src.core.people_legacy_affiliation import find_alias_edges

    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root, "acme")
    _seed_stakeholder(program_dir, alias="alice")

    fleet = FleetReality((_StubProgram(program_id="acme"),))
    fleet_edges = fleet.people_programs("alice", programs_root=programs_root)
    direct_edges = find_alias_edges("alice", programs_root=programs_root)

    assert set(fleet_edges) == set(direct_edges)


def test_people_programs_adds_registry_derived_edges_for_a_shadow_mode_program(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from src.core.people_directory_schema import PersonDirectory, Team, TeamKind, write_people_directory, write_teams
    from src.core.people_entity_schema import (
        AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, EntityStatus, write_entities_document,
    )
    from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships
    from src.core.people_registry_identity import bootstrap_registry_identity
    from src.core.people_registry_modes import set_program_mode
    from src.core.knowledge_store import get_shared_knowledge_root

    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "acme")
    knowledge_root = get_shared_knowledge_root(programs_root)
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    set_program_mode(knowledge_root, "acme", "shadow", actor="steward")

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
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
    write_people_directory(knowledge_root / "people_directory.yaml", (PersonDirectory(entity_id="person:alice", alias="alice"),))
    write_teams(
        knowledge_root / "teams.yaml",
        (Team(entity_id="team:platform", id="platform", name="Platform Team", kind=TeamKind.ORG_TEAM, legacy_programs=("acme",)),),
    )
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(
                membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
                valid_from=None, valid_until=None, source="test", source_ref=None,
                observed_at=now, verified_at=now, status=MembershipStatus.ACTIVE,
            ),
        ),
    )

    fleet = FleetReality((_StubProgram(program_id="acme"),))
    edges = fleet.people_programs("alice", programs_root=programs_root)

    assert any(edge.relation_type == "legacy_team_program" and edge.program_id == "acme" for edge in edges)
