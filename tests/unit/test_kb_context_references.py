from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from shutil import rmtree
from typing import Any
from uuid import uuid4
import sqlite3

import pytest
import yaml

from src.core import kb_updates
from src.core.exceptions import ConfigError


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def _seed(root: Path, program_id: str = "fixture") -> Path:
    program = root / program_id
    _write(program / "program.yaml", {"schema_version": "3.0", "id": program_id, "name": program_id})
    _write(program / "knowledge" / "people_directory.yaml", {
        "schema_version": "1.0", "people": [{"alias": "alice"}],
    })
    _write(program / "workstreams.yaml", {
        "schema_version": "2.0",
        "workstreams": [{"id": "delivery", "name": "Delivery"}, {"id": "release", "name": "Release"}],
    })
    return program


@pytest.fixture
def programs_root() -> Iterator[Path]:
    workspace = Path.cwd() / f".test-context-refs-{uuid4().hex}"
    root = workspace / "programs"
    _seed(root)
    try:
        yield root
    finally:
        rmtree(workspace)


def _milestone(**fields: Any) -> dict[str, Any]:
    return {
        "id": "launch", "name": "Launch", "target_date": "2026-12-01",
        "owner_alias": "alice", "status": "on_track",
        "linked_workstream_ids": ["delivery"], **fields,
    }


def _dependency(**fields: Any) -> dict[str, Any]:
    return {
        "id": "handoff", "from_workstream_id": "delivery", "to_workstream_id": "release",
        "dependency_type": "blocks", "risk_if_broken": "Release blocked", "status": "active", **fields,
    }


def _milestones(root: Path, *entries: dict[str, Any], program_id: str = "fixture") -> None:
    _write(root / program_id / "milestones.yaml", {"schema_version": "1.0", "milestones": list(entries)})


def _dependencies(root: Path, *entries: dict[str, Any], shared: bool = False) -> None:
    path = root / "dependencies.yaml" if shared else root / "fixture" / "dependencies.yaml"
    _write(path, {"schema_version": "1.0", "dependencies": list(entries)})


@pytest.mark.parametrize("fields,message", [
    ({"owner_alias": "unknown"}, "Unknown owner_alias"),
    ({"linked_workstream_ids": ["unknown"]}, "Unknown linked_workstream_id"),
    ({"linked_workstream_ids": "delivery"}, "linked_workstream_ids"),
    ({"linked_work_item_ids": ["not-an-id"]}, "linked_work_item_ids"),
    ({"owner_alias": ["alice"]}, "owner_alias"),
])
def test_invalid_milestone_references_fail(
    programs_root: Path, fields: dict[str, Any], message: str,
) -> None:
    _milestones(programs_root, _milestone(**fields))
    with pytest.raises(ConfigError, match=message):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


@pytest.mark.parametrize("fields,message", [
    ({"from_workstream_id": "unknown"}, "Unknown from_workstream_id"),
    ({"to_workstream_id": "unknown"}, "Unknown to_workstream_id"),
    ({"from_program_id": "unknown"}, "Unknown from_program_id"),
    ({"to_program_id": "unknown"}, "Unknown to_program_id"),
    ({"from_milestone_id": "unknown"}, "Unknown from_milestone_id"),
    ({"to_milestone_id": "unknown"}, "Unknown to_milestone_id"),
    ({"from_workstream_id": None}, "at least one from_"),
    ({"to_workstream_id": None}, "at least one to_"),
    ({"to_item_id": "not-an-id"}, "non-integer to_item_id"),
    ({"from_item_id": ["42"]}, "invalid from_item_id"),
    ({"to_milestone_id": ":launch"}, "invalid to_milestone_id"),
    ({"to_program_id": "other", "to_workstream_id": "fixture:release"}, "mixes to_program_id"),
])
def test_invalid_dependency_references_fail(
    programs_root: Path, fields: dict[str, Any], message: str,
) -> None:
    _milestones(programs_root, _milestone())
    _dependencies(programs_root, _dependency(**fields))
    with pytest.raises(ConfigError, match=message):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_dependency_milestone_requires_its_optional_document_when_referenced(programs_root: Path) -> None:
    _dependencies(programs_root, _dependency(to_milestone_id="launch"))
    with pytest.raises(ConfigError, match="milestones.yaml is missing"):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


@pytest.mark.parametrize("shared", [False, True])
def test_cross_program_reference_uses_target_entities(programs_root: Path, shared: bool) -> None:
    other = _seed(programs_root, "other")
    _write(other / "workstreams.yaml", {
        "schema_version": "2.0", "workstreams": [{"id": "different", "name": "Different"}],
    })
    _milestones(programs_root, _milestone(id="other-launch"), program_id="other")
    _dependencies(programs_root, _dependency(
        from_workstream_id="fixture:delivery", to_workstream_id="other:release",
        to_milestone_id="other:launch",
    ), shared=shared)
    with pytest.raises(ConfigError, match="Unknown to_workstream_id"):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_dependency_cycles_fail(programs_root: Path) -> None:
    _dependencies(programs_root, _dependency(), _dependency(
        id="return", from_workstream_id="release", to_workstream_id="delivery",
    ))
    with pytest.raises(ConfigError, match="Dependency cycle detected"):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_duplicate_dependency_ids_fail(programs_root: Path) -> None:
    _dependencies(programs_root, _dependency(), _dependency())
    with pytest.raises(ConfigError, match="Duplicate dependency id"):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


@pytest.mark.parametrize("filename,document,message", [
    ("milestones.yaml", ["not-a-mapping"], "mapping"),
    ("dependencies.yaml", ["not-a-mapping"], "mapping"),
    ("milestones.yaml", {"schema_version": "9.0", "milestones": []}, "schema_version"),
    ("dependencies.yaml", {"schema_version": "9.0", "dependencies": []}, "schema_version"),
    ("milestones.yaml", {"schema_version": "1.0", "milestones": [False]}, "mapping"),
    ("dependencies.yaml", {"schema_version": "1.0", "dependencies": [False]}, "mapping"),
])
def test_authored_reference_documents_use_existing_schema_checks(
    programs_root: Path, filename: str, document: Any, message: str,
) -> None:
    _write(programs_root / "fixture" / filename, document)
    with pytest.raises(ConfigError, match=message):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_valid_context_is_read_only_without_db_or_temporary_copies(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _milestones(programs_root, _milestone(linked_work_item_ids=[123456]))
    _dependencies(programs_root, _dependency(from_milestone_id="launch", to_item_id=987654))
    before = {path: path.read_bytes() for path in programs_root.rglob("*") if path.is_file()}

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Read-only authored reference validation must not write, open a DB, or copy context.")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "write_text", forbidden)
        scoped.setattr(Path, "write_bytes", forbidden)
        scoped.setattr(sqlite3, "connect", forbidden)
        scoped.setattr(kb_updates, "TemporaryDirectory", forbidden)
        scoped.setattr(kb_updates, "_dump_yaml", forbidden)
        result = kb_updates.validate_program_kb("fixture", programs_root=programs_root)
    assert [person.alias for person in result.people_directory] == ["alice"]
    assert before == {path: path.read_bytes() for path in programs_root.rglob("*") if path.is_file()}


def test_missing_optional_documents_and_legacy_free_text_are_compatible(programs_root: Path) -> None:
    _write(programs_root / "fixture" / "program.yaml", {
        "schema_version": "3.0", "id": "fixture", "name": "Fixture",
        "key_dependencies": [{"from": "External team approval", "to": "Release readiness", "impact": "Delay"}],
    })
    kb_updates.validate_program_kb("fixture", programs_root=programs_root)
    assert not (programs_root / "fixture" / "milestones.yaml").exists()
    assert not (programs_root / "fixture" / "dependencies.yaml").exists()


@pytest.mark.parametrize("shared,explicit", [(False, False), (False, True), (True, False), (True, True)])
def test_valid_cross_program_edges_allow_both_existing_scope_forms(
    programs_root: Path, shared: bool, explicit: bool,
) -> None:
    _seed(programs_root, "other")
    _milestones(programs_root, _milestone(id="remote-launch"), program_id="other")
    fields = {
        "from_workstream_id": "fixture:delivery", "to_workstream_id": "other:release",
        "to_milestone_id": "other:remote-launch",
    }
    if explicit:
        fields = {
            "from_program_id": "fixture", "from_workstream_id": "delivery",
            "to_program_id": "other", "to_workstream_id": "release", "to_milestone_id": "remote-launch",
        }
    _dependencies(programs_root, _dependency(**fields), shared=shared)
    kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_preview_validates_references_against_proposed_workstream_changes(programs_root: Path) -> None:
    _milestones(programs_root, _milestone())
    plan = kb_updates.KbUpdatePlan(
        program_id="fixture", correction="remove delivery", planner="deterministic",
        operations=(kb_updates.KbUpdateOperation(
            file_path="workstreams.yaml", action="remove_entry", match_value="delivery",
        ),),
    )
    with pytest.raises(ConfigError, match="Unknown linked_workstream_id"):
        kb_updates.prepare_kb_update(plan, programs_root=programs_root)


@pytest.mark.parametrize("side", ["from", "to"])
@pytest.mark.parametrize("milestones_present", [False, True])
def test_cross_program_milestone_reference_failures(
    programs_root: Path, side: str, milestones_present: bool,
) -> None:
    _seed(programs_root, "other")
    if milestones_present:
        _milestones(programs_root, _milestone(id="different"), program_id="other")
    fields = {f"{side}_program_id": "other", f"{side}_milestone_id": "launch"}
    _dependencies(programs_root, _dependency(**fields))
    message = f"Unknown {side}_milestone_id" if milestones_present else "milestones.yaml is missing"
    with pytest.raises(ConfigError, match=message):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_item_only_dependency_preserves_offline_existence_unknown(programs_root: Path) -> None:
    _dependencies(programs_root, _dependency(
        from_workstream_id=None, to_workstream_id=None, from_item_id=123456, to_item_id="987654",
    ))
    kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_milestone_without_optional_entity_links_is_compatible(programs_root: Path) -> None:
    _milestones(programs_root, _milestone(linked_workstream_ids=[], linked_work_item_ids=[]))
    kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_shared_edges_do_not_turn_legacy_free_text_into_entity_ids(programs_root: Path) -> None:
    _seed(programs_root, "other")
    _write(programs_root / "fixture" / "program.yaml", {
        "schema_version": "3.0", "id": "fixture", "name": "Fixture",
        "key_dependencies": [{"from": "External approval", "to": "Release readiness", "impact": "Delay"}],
    })
    _dependencies(programs_root, _dependency(
        from_workstream_id="fixture:delivery", to_workstream_id="other:release",
    ), shared=True)
    kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_unrelated_shared_edge_is_not_a_required_context_floor(programs_root: Path) -> None:
    _dependencies(programs_root, _dependency(
        from_workstream_id="unconfigured-source:delivery", to_workstream_id="unconfigured-target:release",
    ), shared=True)
    kb_updates.validate_program_kb("fixture", programs_root=programs_root)


@pytest.mark.parametrize("fields,message", [
    ({}, "explicit program scoping"),
    ({"from_workstream_id": "fixture:delivery", "to_workstream_id": "fixture:release"}, "cross-program edges"),
])
def test_shared_edge_scope_uses_canonical_loader_invariants(
    programs_root: Path, fields: dict[str, Any], message: str,
) -> None:
    _dependencies(programs_root, _dependency(**fields), shared=True)
    with pytest.raises(ConfigError, match=message):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_duplicate_ids_across_local_and_shared_documents_fail(programs_root: Path) -> None:
    _seed(programs_root, "other")
    _dependencies(programs_root, _dependency())
    _dependencies(programs_root, _dependency(
        from_workstream_id="fixture:delivery", to_workstream_id="other:release",
    ), shared=True)
    with pytest.raises(ConfigError, match="Duplicate dependency id"):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


@pytest.mark.parametrize("filename", ["milestones.yaml", "dependencies.yaml"])
def test_malformed_yaml_is_a_config_error(programs_root: Path, filename: str) -> None:
    (programs_root / "fixture" / filename).write_text("broken: [", encoding="utf-8")
    with pytest.raises(ConfigError):
        kb_updates.validate_program_kb("fixture", programs_root=programs_root)


def test_context_documents_are_not_hidden_by_shared_knowledge_cache(programs_root: Path) -> None:
    _milestones(programs_root, _milestone())
    with kb_updates.SharedKnowledgeTempCache() as shared_cache:
        cache: dict[Path, dict[str, Any]] = {}
        kb_updates.validate_program_kb(
            "fixture", programs_root=programs_root, document_cache=cache, shared_knowledge_cache=shared_cache,
        )
        _milestones(programs_root, _milestone(owner_alias="unknown"))
        with pytest.raises(ConfigError, match="Unknown owner_alias"):
            kb_updates.validate_program_kb(
                "fixture", programs_root=programs_root, document_cache=cache, shared_knowledge_cache=shared_cache,
            )


def test_context_references_preserve_schema2_people_bridge(programs_root: Path) -> None:
    _write(programs_root.parent / "knowledge" / "people_directory.yaml", {
        "schema_version": "2.0",
        "people": [{"entity_id": "person:alice", "alias": "alice", "display_name": "Alice"}],
    })
    _milestones(programs_root, _milestone())
    knowledge = kb_updates.validate_program_kb("fixture", programs_root=programs_root)
    assert knowledge.people_directory[0].alias == "alice"


def test_context_references_preserve_encrypted_profiles_bridge(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet
    from src.core import profile_encryption

    key = Fernet.generate_key()
    profiles = {"schema_version": "1.0", "profiles": [{"alias": "alice", "comm_style": "concise"}]}
    _write(programs_root / "fixture" / "knowledge" / "people_profiles.yaml", {
        "schema_version": "1.0", "storage": "encrypted",
        "encryption": {"format": "fernet", "key_id": "synthetic"},
        "ciphertext": Fernet(key).encrypt(yaml.safe_dump(profiles).encode()).decode(),
    })
    monkeypatch.setattr(profile_encryption, "_load_fernet_key", lambda *_args: key.decode())
    _milestones(programs_root, _milestone())
    knowledge = kb_updates.validate_program_kb("fixture", programs_root=programs_root)
    assert knowledge.people_profiles[0].comm_style == "concise"
