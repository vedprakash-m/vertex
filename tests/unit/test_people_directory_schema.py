"""specs/people.md Phase 2a, PPL-W2A.2: tests for people_directory.yaml/
teams.yaml schema 2.0 with legacy dual-read
(src/core/people_directory_schema.py).

specs/people.md §9.1's own verification bar: "Legacy-field WARN
diagnostics fire; equivalent legacy vs typed data resolve identically."
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.people_directory_schema import (
    ContactKind,
    PersonStatus,
    TeamKind,
    TeamStatus,
    TenantRelationship,
    load_people_directory,
    load_people_profiles,
    load_teams,
)
from src.core.people_registry_diagnostics import DiagnosticSeverity


def _write_people_yaml(path: Path, people: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"schema_version": "2.0", "people": people}), encoding="utf-8")


def _write_teams_yaml(path: Path, teams: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"schema_version": "2.0", "teams": teams}), encoding="utf-8")


def test_load_people_directory_reads_the_real_example_fixture() -> None:
    path = Path(__file__).resolve().parents[2] / "knowledge" / "people_directory.example.yaml"

    result = load_people_directory(path)

    assert result is not None
    assert len(result.people) == 1
    assert result.diagnostics == ()  # Fully-typed fixture: zero legacy-field WARNs.
    person = result.people[0]
    assert person.status == PersonStatus.ACTIVE
    assert person.tenant_relationship == TenantRelationship.INTERNAL
    assert person.contacts[0].kind == ContactKind.PRIMARY_EMAIL


def test_load_teams_reads_the_real_example_fixture() -> None:
    path = Path(__file__).resolve().parents[2] / "knowledge" / "teams.example.yaml"

    result = load_teams(path)

    assert result is not None
    assert len(result.teams) == 2
    assert result.diagnostics == ()
    org_team = next(t for t in result.teams if t.kind == TeamKind.ORG_TEAM)
    assert org_team.legacy_programs == ()
    program_group = next(t for t in result.teams if t.kind == TeamKind.PROGRAM_GROUP)
    assert program_group.legacy_programs == ("acme",)


def test_load_people_directory_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_people_directory(tmp_path / "people_directory.yaml") is None


def test_load_teams_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_teams(tmp_path / "teams.yaml") is None


def test_missing_entity_id_produces_a_warn_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "people_directory.yaml"
    _write_people_yaml(path, [{"alias": "no_id_person"}])

    result = load_people_directory(path)

    assert result.people[0].entity_id == ""
    assert any(d.code == "missing_entity_id" and d.severity == DiagnosticSeverity.WARN for d in result.diagnostics)


def test_legacy_email_synthesizes_an_unverified_contact_and_warns(tmp_path: Path) -> None:
    path = tmp_path / "people_directory.yaml"
    _write_people_yaml(path, [{"entity_id": "person:1", "alias": "alice", "email": "alice@example.com"}])

    result = load_people_directory(path)

    person = result.people[0]
    assert len(person.contacts) == 1
    assert person.contacts[0].value == "alice@example.com"
    assert person.contacts[0].source == "legacy_migration"
    assert "unverified" in person.contacts[0].verified_by_principal.lower()
    assert any(d.code == "legacy_email_field" for d in result.diagnostics)


def test_legacy_manager_alias_warns_without_resolving(tmp_path: Path) -> None:
    path = tmp_path / "people_directory.yaml"
    _write_people_yaml(path, [{"entity_id": "person:1", "alias": "alice", "manager_alias": "bob"}])

    result = load_people_directory(path)

    assert result.people[0].manager_entity_id is None
    assert any(d.code == "legacy_manager_alias" and "bob" in d.detail for d in result.diagnostics)


def test_legacy_team_ids_and_org_chain_warn_but_are_not_dropped_silently(tmp_path: Path) -> None:
    path = tmp_path / "people_directory.yaml"
    _write_people_yaml(path, [{"entity_id": "person:1", "alias": "alice", "team_ids": ["team-a"], "org_chain": ["mgr1", "mgr2"]}])

    result = load_people_directory(path)

    codes = {d.code for d in result.diagnostics}
    assert "legacy_team_ids" in codes
    assert "legacy_org_chain" in codes


def test_typed_and_legacy_equivalent_person_records_resolve_identically(tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W2A.2 verification: "equivalent
    # legacy vs typed data resolve identically."
    from datetime import datetime, timezone

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    typed_path = tmp_path / "typed.yaml"
    _write_people_yaml(
        typed_path,
        [
            {
                "entity_id": "person:1",
                "alias": "alice",
                "display_name": "Alice",
                "contacts": [
                    {
                        "kind": "primary_email",
                        "value": "alice@example.com",
                        "status": "active",
                        "valid_from": None,
                        "valid_until": None,
                        "source": "legacy_migration",
                        "source_ref": None,
                        "recorded_at": now.isoformat(),
                        "verified_at": now.isoformat(),
                        "verified_by_principal": "<unverified -- legacy migration>",
                        "delivery_eligible": True,
                    }
                ],
            }
        ],
    )
    legacy_path = tmp_path / "legacy.yaml"
    _write_people_yaml(legacy_path, [{"entity_id": "person:1", "alias": "alice", "display_name": "Alice", "email": "alice@example.com"}])

    typed_result = load_people_directory(typed_path, as_of=now)
    legacy_result = load_people_directory(legacy_path, as_of=now)

    assert typed_result.people == legacy_result.people  # Identical resolved data.
    assert typed_result.diagnostics == ()
    assert legacy_result.diagnostics != ()  # The legacy path is the one that WARNs.


def test_team_missing_kind_defaults_to_program_group_and_warns(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    _write_teams_yaml(path, [{"entity_id": "team:1", "id": "team-a", "name": "Team A"}])

    result = load_teams(path)

    assert result.teams[0].kind == TeamKind.PROGRAM_GROUP
    assert any(d.code == "missing_team_kind" for d in result.diagnostics)


def test_team_legacy_programs_key_warns_but_carries_data(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    _write_teams_yaml(path, [{"entity_id": "team:1", "id": "team-a", "name": "Team A", "kind": "program_group", "programs": ["acme"]}])

    result = load_teams(path)

    assert result.teams[0].legacy_programs == ("acme",)
    assert any(d.code == "legacy_team_programs_key" for d in result.diagnostics)


def test_team_typed_legacy_programs_key_produces_no_warning(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    _write_teams_yaml(path, [{"entity_id": "team:1", "id": "team-a", "name": "Team A", "kind": "program_group", "legacy_programs": ["acme"]}])

    result = load_teams(path)

    assert result.teams[0].legacy_programs == ("acme",)
    assert result.diagnostics == ()


def test_typed_and_legacy_equivalent_team_records_resolve_identically() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        typed_path = tmp_path / "typed.yaml"
        _write_teams_yaml(typed_path, [{"entity_id": "team:1", "id": "team-a", "name": "Team A", "kind": "program_group", "legacy_programs": ["acme"]}])
        legacy_path = tmp_path / "legacy.yaml"
        _write_teams_yaml(legacy_path, [{"entity_id": "team:1", "id": "team-a", "name": "Team A", "kind": "program_group", "programs": ["acme"]}])

        typed_result = load_teams(typed_path)
        legacy_result = load_teams(legacy_path)

        assert typed_result.teams == legacy_result.teams
        assert typed_result.diagnostics == ()
        assert legacy_result.diagnostics != ()


def test_team_status_defaults_to_unknown_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    _write_teams_yaml(path, [{"entity_id": "team:1", "id": "team-a", "name": "Team A", "kind": "org_team"}])

    result = load_teams(path)

    assert result.teams[0].status == TeamStatus.UNKNOWN


# ---------------------------------------------------------------------------
# PPL-W2A.5: people_profiles.yaml schema 2.0, encrypted-envelope-aware.
# ---------------------------------------------------------------------------


def test_load_people_profiles_reads_the_real_example_fixture() -> None:
    path = Path(__file__).resolve().parents[2] / "knowledge" / "people_profiles.example.yaml"

    result = load_people_profiles(path)

    assert result is not None
    assert len(result.profiles) == 1
    assert result.profiles[0].entity_id == "person:01HQ8Y1A2B3C4D5E6F7G8H9J0K"
    assert result.diagnostics == ()


def test_load_people_profiles_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_people_profiles(tmp_path / "people_profiles.yaml") is None


def test_load_people_profiles_warns_on_missing_entity_id(tmp_path: Path) -> None:
    path = tmp_path / "people_profiles.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "1.0", "profiles": [{"alias": "alice", "comm_style": "concise"}]}), encoding="utf-8")

    result = load_people_profiles(path)

    assert result.profiles[0].entity_id == ""
    assert any(d.code == "missing_entity_id" for d in result.diagnostics)


class _FakeKeyring:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._values[(service_name, username)] = password


def test_load_people_profiles_transparently_decrypts_an_encrypted_envelope(tmp_path: Path, monkeypatch) -> None:
    # §7.9: "migrate encrypted documents" -- profile_encryption.py is
    # schema-agnostic, so entity_id round-trips through the encrypted
    # envelope exactly like every other field, with zero changes to that
    # module required.
    from src.core.profile_encryption import encrypt_people_profiles_file

    path = tmp_path / "people_profiles.yaml"
    path.write_text(
        yaml.safe_dump({"schema_version": "1.0", "profiles": [{"entity_id": "person:1", "alias": "alice", "comm_style": "concise"}]}),
        encoding="utf-8",
    )
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(path)
    assert "person:1" not in path.read_text(encoding="utf-8")  # Confirms it's actually encrypted now.

    result = load_people_profiles(path)

    assert result.profiles[0].entity_id == "person:1"
    assert result.profiles[0].comm_style == "concise"
    assert result.diagnostics == ()
