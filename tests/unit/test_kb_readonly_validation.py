from __future__ import annotations

from pathlib import Path
from copy import deepcopy
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
