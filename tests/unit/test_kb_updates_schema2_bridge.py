"""Regression test for a real NameError bug found via PPL-W3.5's 10,000-
person scale benchmark (specs/people.md): `kb_updates.py::_read_yaml_or_default`'s
schema-2.0 `people_directory.yaml` -> legacy-shaped bridge derived
`team_ids` via a list comprehension whose outer `if` clause referenced
`membership`, a name only bound inside a separate inner generator
expression -- a scope this list comprehension's own `if` clause cannot
see. This path was never exercised end-to-end with real schema-2.0 data
plus real memberships before (confirmed: no test file for `kb_updates.py`
existed at all prior to this test), so the bug shipped silently until a
real 10,000-person fixture with real team memberships triggered it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.kb_updates import read_program_kb_documents
from src.core.people_directory_schema import PersonDirectory, Team, TeamKind, write_people_directory, write_teams
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_read_program_kb_documents_derives_team_ids_from_schema2_memberships(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text('schema_version: "3.0"\nid: "acme"\nname: "Acme"\n', encoding="utf-8")

    knowledge_root = tmp_path / "knowledge"
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (PersonDirectory(entity_id="person:alice", alias="alice", display_name="Alice"),),
    )
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(
                membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
                valid_from=None, valid_until=None, source="test", source_ref=None,
                observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE,
            ),
        ),
    )

    documents = read_program_kb_documents("acme", programs_root=programs_root)

    people = documents["knowledge/people_directory.yaml"]["people"]
    assert len(people) == 1
    assert people[0]["alias"] == "alice"
    assert people[0]["team_ids"] == ["platform"]


def test_read_program_kb_documents_excludes_inactive_memberships_from_team_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text('schema_version: "3.0"\nid: "acme"\nname: "Acme"\n', encoding="utf-8")

    knowledge_root = tmp_path / "knowledge"
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (PersonDirectory(entity_id="person:alice", alias="alice", display_name="Alice"),),
    )
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(
                membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
                valid_from=None, valid_until=None, source="test", source_ref=None,
                observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.TOMBSTONED,
            ),
        ),
    )

    documents = read_program_kb_documents("acme", programs_root=programs_root)

    people = documents["knowledge/people_directory.yaml"]["people"]
    assert people[0]["team_ids"] == []
