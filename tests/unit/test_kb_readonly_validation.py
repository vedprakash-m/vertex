from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import json

import pytest

from src.core import kb_updates
from src.core.exceptions import ConfigError
from src.core.knowledge_store import load_knowledge, parse_knowledge_documents


def _seed(root: Path) -> Path:
    program = root / "fixture"
    knowledge = program / "knowledge"
    knowledge.mkdir(parents=True)
    (program / "program.yaml").write_text(
        "schema_version: '3.0'\nid: fixture\nname: Fixture\n"
        "charter:\n  stakeholder_register:\n    - alias: alice\n", encoding="utf-8",
    )
    (knowledge / "people_directory.yaml").write_text(
        "schema_version: '1.0'\npeople:\n  - alias: alice\n", encoding="utf-8",
    )
    (program / "workstreams.yaml").write_text(
        "schema_version: '2.0'\nworkstreams:\n  - id: delivery\n    name: Delivery\n"
        "    raci:\n      accountable: alice\n", encoding="utf-8",
    )
    return program


def test_doctor_reuse_preserves_per_program_references_and_local_pages(tmp_path: Path) -> None:
    from src.commands.doctor_checks import kb_checks

    root = tmp_path / "programs"
    program = _seed(root)
    shared = tmp_path / "knowledge"
    shared.mkdir()
    (shared / "people_directory.yaml").write_text("schema_version: '1.0'\npeople: [{alias: alice}]\n", encoding="utf-8")
    (shared / "engms_pages.yaml").write_text("schema_version: '1.0'\npages: [{id: shared, url: 'https://example.test/shared'}]\n", encoding="utf-8")
    other = root / "other"
    other.mkdir()
    (other / "program.yaml").write_text("schema_version: '3.0'\nid: other\nname: Other\n", encoding="utf-8")
    (program / "knowledge" / "engms_pages.yaml").write_text(
        "schema_version: '1.0'\npages: [{id: local, url: 'https://example.test/local'}]\n", encoding="utf-8",
    )
    baseline = {name: kb_updates.validate_program_kb(name, programs_root=root) for name in ("fixture", "other")}
    results: dict[str, object] = {}
    validator = kb_updates.validate_program_kb

    def observed(name: str, **kwargs: object) -> object:
        result = validator(name, **kwargs)
        results[name] = result
        return result

    with patch.object(kb_checks, "validate_program_kb", side_effect=observed):
        kb_checks.run_kb_doctor(editions_root=tmp_path / "editions", programs_root=root)
    assert results == baseline
    first = kb_updates.read_program_kb_documents("fixture", programs_root=root, document_cache={})
    second = kb_updates.read_program_kb_documents("fixture", programs_root=root, document_cache={})
    first["knowledge/people_directory.yaml"]["people"][0]["alias"] = "changed"
    assert second["knowledge/people_directory.yaml"]["people"][0]["alias"] == "alice"


@pytest.mark.parametrize("text", [
    "schema_version: '9.0'\npeople: []\n",
    "schema_version: '1.0'\npeople: wrong\n",
    "schema_version: '1.0'\npeople: [wrong]\n",
    "schema_version: '1.0'\npeople: [{}]\n",
    "people: [\n",
])
def test_failed_shared_validation_is_not_cached(tmp_path: Path, text: str) -> None:
    root = tmp_path / "programs"
    _seed(root)
    shared = tmp_path / "knowledge"
    shared.mkdir()
    path = shared / "people_directory.yaml"
    path.write_text(text, encoding="utf-8")
    compiled: dict = {}
    with pytest.raises(ConfigError) as baseline:
        kb_updates.validate_program_kb("fixture", programs_root=root)
    with pytest.raises(ConfigError) as cached:
        kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    assert str(cached.value) == str(baseline.value)
    assert compiled == {}
    path.write_text("schema_version: '1.0'\npeople: [{alias: alice}]\n", encoding="utf-8")
    result = kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    assert result.people_directory[0].alias == "alice"
    assert len(compiled) == 1


def test_compiled_reuse_retains_fresh_context_local_pages_and_kql(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    shared = tmp_path / "knowledge"
    shared.mkdir()
    people = shared / "people_directory.yaml"
    people.write_text("schema_version: '1.0'\npeople: [{alias: alice}]\n", encoding="utf-8")
    (shared / "golden_queries.yaml").write_text(
        "schema_version: '1.0'\nqueries: [{id: query, kql_file: metric.kql}]\n", encoding="utf-8",
    )
    metric = tmp_path / "metric.kql"
    metric.write_text("print value=1", encoding="utf-8")
    documents: dict = {}
    compiled: dict = {}
    first = kb_updates.validate_program_kb("fixture", programs_root=root, document_cache=documents, compiled_knowledge_cache=compiled)
    metric.write_text("print value=2", encoding="utf-8")
    people.write_text("schema_version: '1.0'\npeople: [{alias: bob}]\n", encoding="utf-8")
    local_pages = program / "knowledge" / "engms_pages.yaml"
    local_pages.write_text("schema_version: '1.0'\npages: [{id: local, url: 'https://example.test/local'}]\n", encoding="utf-8")
    second = kb_updates.validate_program_kb("fixture", programs_root=root, document_cache=documents, compiled_knowledge_cache=compiled)
    assert first.people_directory is second.people_directory
    assert first.golden_queries[0].kql == "print value=1"
    assert second.golden_queries[0].kql == "print value=2"
    assert second.engms_pages[0].id == "local"
    local_pages.write_text("schema_version: '1.0'\npages: wrong\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="pages must be a list"):
        kb_updates.validate_program_kb("fixture", programs_root=root, document_cache=documents, compiled_knowledge_cache=compiled)
    local_pages.unlink()
    charter = program / "program.yaml"
    charter.write_text(charter.read_text().replace("alias: alice", "alias: unknown"), encoding="utf-8")
    with pytest.raises(ConfigError, match="Unknown.*unknown"):
        kb_updates.validate_program_kb("fixture", programs_root=root, document_cache=documents, compiled_knowledge_cache=compiled)
    assert len(compiled) == 1
    with pytest.raises(ConfigError, match="alice"):
        kb_updates.validate_program_kb("fixture", programs_root=root)


def test_compiled_scope_without_document_cache_and_without_shared_root(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    _seed(root)
    compiled: dict = {}
    local = kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    assert compiled == {}
    shared = tmp_path / "knowledge"
    shared.mkdir()
    (shared / "people_directory.yaml").write_text("schema_version: '1.0'\npeople: [{alias: alice}]\n", encoding="utf-8")
    first = kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    second = kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    assert local == first == second
    assert first.people_directory is second.people_directory


def test_compiled_reuse_reports_per_program_workstream_failure(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    shared = tmp_path / "knowledge"
    shared.mkdir()
    (shared / "people_directory.yaml").write_text("schema_version: '1.0'\npeople: [{alias: alice}]\n", encoding="utf-8")
    compiled: dict = {}
    kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    workstreams = program / "workstreams.yaml"
    workstreams.write_text(workstreams.read_text().replace("accountable: alice", "accountable: unknown"), encoding="utf-8")
    with pytest.raises(ConfigError) as baseline:
        kb_updates.validate_program_kb("fixture", programs_root=root)
    with pytest.raises(ConfigError) as cached:
        kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    assert str(cached.value) == str(baseline.value)


@pytest.mark.parametrize("queries", ["wrong", ["wrong"], [None], [1]])
def test_compiled_scope_without_document_cache_retains_invalid_query_diagnostics(
    tmp_path: Path, queries: object,
) -> None:
    root = tmp_path / "programs"
    _seed(root)
    shared = tmp_path / "knowledge"
    shared.mkdir()
    (shared / "people_directory.yaml").write_text(
        "schema_version: '1.0'\npeople: [{alias: alice}]\n", encoding="utf-8",
    )
    path = shared / "golden_queries.yaml"
    path.write_text("schema_version: '1.0'\nqueries: []\n", encoding="utf-8")
    compiled: dict = {}
    kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    path.write_text(json.dumps({"schema_version": "1.0", "queries": queries}), encoding="utf-8")
    with pytest.raises(ConfigError) as baseline:
        kb_updates.validate_program_kb("fixture", programs_root=root)
    with pytest.raises(ConfigError) as cached:
        kb_updates.validate_program_kb("fixture", programs_root=root, compiled_knowledge_cache=compiled)
    assert str(cached.value) == str(baseline.value)


@pytest.mark.parametrize("use_cache", [False, True])
def test_validation_does_not_copy_or_dump_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_cache: bool,
) -> None:
    root = tmp_path / "programs"
    _seed(root)
    with monkeypatch.context() as scoped:
        scoped.setattr(kb_updates, "TemporaryDirectory", lambda *args, **kwargs: pytest.fail("No temporary context copies."))
        scoped.setattr(kb_updates, "_dump_yaml", lambda *args, **kwargs: pytest.fail("No YAML reserialization."))
        scoped.setattr(Path, "write_text", lambda *args, **kwargs: pytest.fail("No validation writes."))
        with kb_updates.SharedKnowledgeTempCache() as shared_cache:
            result = kb_updates.validate_program_kb(
                "fixture", programs_root=root,
                document_cache={} if use_cache else None,
                shared_knowledge_cache=shared_cache if use_cache else None,
            )
    assert [person.alias for person in result.people_directory] == ["alice"]


def test_validation_retains_reference_failures_without_temp_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    workstreams = program / "workstreams.yaml"
    workstreams.write_text(workstreams.read_text().replace("accountable: alice", "accountable: unknown"), encoding="utf-8")
    with monkeypatch.context() as scoped:
        scoped.setattr(kb_updates, "TemporaryDirectory", lambda *args, **kwargs: pytest.fail("No temporary context copies."))
        with pytest.raises(ConfigError, match="Unknown raci.accountable"):
            kb_updates.validate_program_kb("fixture", programs_root=root)


@pytest.mark.parametrize("shared", [False, True])
def test_validation_uses_referenced_kql_in_actual_program_context(tmp_path: Path, shared: bool) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    knowledge = tmp_path / "knowledge" if shared else program / "knowledge"
    if shared:
        knowledge.mkdir()
        (knowledge / "people_directory.yaml").write_text(
            "schema_version: '1.0'\npeople:\n  - alias: alice\n", encoding="utf-8",
        )
    (knowledge.parent / "metric.kql").write_text("print value=1", encoding="utf-8")
    (knowledge / "golden_queries.yaml").write_text(
        "schema_version: '1.0'\nqueries:\n  - id: metric\n    kql_file: metric.kql\n"
        "    workstream_ids: [delivery]\n", encoding="utf-8",
    )
    result = kb_updates.validate_program_kb("fixture", programs_root=root)
    assert result.golden_queries[0].kql == "print value=1"


def test_parsed_knowledge_matches_disk_adapter_without_mutating_inputs(tmp_path: Path) -> None:
    documents = {
        "people_directory.yaml": {"people": [{"alias": "alice", "team_ids": ["delivery"]}]},
        "people_profiles.yaml": {"profiles": [{"alias": "alice", "comm_style": "concise"}]},
        "teams.yaml": {"teams": [{"id": "delivery", "name": "Delivery"}]},
        "products.yaml": {"products": [{"id": "widget", "related_teams": ["delivery"]}]},
        "golden_queries.yaml": {"queries": [{
            "id": "metric", "kql": "print value=1", "slo_target": "0.99",
            "classification": "evidence", "validated": False,
        }]},
        "engms_pages.yaml": {"pages": [{"id": "guide", "url": "https://example.test/guide"}]},
    }
    before = deepcopy(documents)
    for name, document in documents.items():
        (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")
    parsed = parse_knowledge_documents(documents, knowledge_root=tmp_path)
    assert parsed == load_knowledge(knowledge_root=tmp_path)
    assert documents == before
    assert parsed.products[0].id == "widget"
    assert parsed.golden_queries[0].slo_target == 0.99


def test_missing_program_is_not_created_by_validation(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(ConfigError, match="Missing required KB document"):
        kb_updates.validate_program_kb("fixture", programs_root=root)
    assert not root.exists()


def test_validation_retains_unknown_team_program_reference_check(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    program = _seed(root)
    (program / "knowledge" / "teams.yaml").write_text(
        "schema_version: '1.0'\nteams:\n  - id: delivery\n    programs: [unknown-program]\n", encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown-program"):
        kb_updates.validate_program_kb("fixture", programs_root=root)
