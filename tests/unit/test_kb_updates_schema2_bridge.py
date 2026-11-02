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
from unittest.mock import patch

from src.core.kb_updates import read_program_kb_documents
from src.core.people_directory_schema import PersonDirectory, Team, TeamKind, write_people_directory, write_teams
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_schema2_bridge_reads_membership_person_keys_once(tmp_path: Path) -> None:
    from src.core import kb_updates

    root = tmp_path / "programs"
    (root / "acme").mkdir(parents=True)
    knowledge = tmp_path / "knowledge"
    people = tuple(PersonDirectory(entity_id=f"person:{i}", alias=f"person{i}", display_name=f"Person {i}") for i in range(32))
    write_people_directory(knowledge / "people_directory.yaml", people)
    write_teams(knowledge / "teams.yaml", (Team(entity_id="team:t", id="t", name="T", kind=TeamKind.ORG_TEAM),))
    members = tuple(TeamMembership(
        membership_id=f"m{i}", person_entity_id=f"person:{i}", team_entity_id="team:t", role="member",
        valid_from=None, valid_until=None, source="test", source_ref=None, observed_at=_NOW,
        verified_at=_NOW, status=MembershipStatus.ACTIVE,
    ) for i in range(32))
    reads: list[str] = []

    class ObservedMembership:
        def __init__(self, member: TeamMembership) -> None:
            self.member = member

        def __getattr__(self, name: str) -> object:
            if name == "person_entity_id":
                reads.append(name)
            return getattr(self.member, name)

    with patch.object(kb_updates, "load_memberships", return_value=tuple(ObservedMembership(member) for member in members)):
        documents = read_program_kb_documents("acme", programs_root=root)
    assert [person["team_ids"] for person in documents["knowledge/people_directory.yaml"]["people"]] == [["t"]] * 32
    assert len(reads) == 32


def test_schema2_bridge_preserves_order_duplicates_unknowns_and_dates(tmp_path: Path) -> None:
    from dataclasses import replace
    from src.core import kb_updates

    root = tmp_path / "programs"
    (root / "acme").mkdir(parents=True)
    knowledge = tmp_path / "knowledge"
    people = (PersonDirectory(entity_id="person:a", alias="a", display_name="A"),
              PersonDirectory(entity_id="person:a", alias="duplicate", display_name="Duplicate"),
              PersonDirectory(entity_id=None, alias="legacy", display_name="Legacy"))
    write_people_directory(knowledge / "people_directory.yaml", people)
    team = Team(entity_id="team:t", id="first", name="T", kind=TeamKind.ORG_TEAM)
    write_teams(knowledge / "teams.yaml", (team, replace(team, id="last")))
    member = TeamMembership(
        membership_id="m", person_entity_id="person:a", team_entity_id="team:t", role="member",
        valid_from=_NOW.date(), valid_until=_NOW.date(), source="test", source_ref=None,
        observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE,
    )
    members = (member, replace(member, membership_id="duplicate"),
               replace(member, membership_id="inactive", status=MembershipStatus.TOMBSTONED),
               replace(member, membership_id="unknown-team", team_entity_id="team:unknown"),
               replace(member, membership_id="unknown-person", person_entity_id="person:unknown"))
    with patch.object(kb_updates, "load_memberships", return_value=members):
        documents = read_program_kb_documents("acme", programs_root=root)
    rows = {row["alias"]: row for row in documents["knowledge/people_directory.yaml"]["people"]}
    assert rows["a"]["team_ids"] == rows["duplicate"]["team_ids"] == ["last", "last"]
    assert rows["legacy"]["team_ids"] == []
    rows["a"]["team_ids"].append("independent")
    assert rows["duplicate"]["team_ids"] == ["last", "last"]


def test_schema2_bridge_without_memberships_or_teams(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    (root / "acme").mkdir(parents=True)
    write_people_directory(tmp_path / "knowledge" / "people_directory.yaml",
                           (PersonDirectory(entity_id="person:a", alias="a", display_name="A"),))
    documents = read_program_kb_documents("acme", programs_root=root)
    assert documents["knowledge/people_directory.yaml"]["people"][0]["team_ids"] == []


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
