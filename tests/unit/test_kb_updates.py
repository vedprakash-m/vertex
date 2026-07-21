"""specs/people.md PPL-W3.5b (i): real test coverage for `kb_updates.py`'s
`read_program_kb_documents`/`validate_program_kb` -- the module had zero
prior coverage of its own (confirmed by a repo-wide grep before writing
this file; the only pre-existing test, `test_kb_updates_schema2_bridge.py`,
covers one narrow schema-2.0 team_ids bridging bug). This file establishes
a real behavioral baseline BEFORE the request-scoped caching refactor
(PPL-W3.5b (ii)) lands, so that refactor can be proven safe rather than
merely assumed safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.kb_updates import (
    SharedKnowledgeTempCache,
    apply_kb_update,
    parse_deterministic_kb_correction,
    parse_kb_update_operations,
    prepare_kb_update,
    read_program_kb_documents,
    validate_program_kb,
)
from src.core.knowledge_store import load_knowledge

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _seed_minimal_program(programs_root: Path, program_id: str, *, extra_program_yaml: str = "") -> Path:
    program_dir = programs_root / program_id
    (program_dir / "knowledge").mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        f'schema_version: "3.0"\nid: "{program_id}"\nname: "{program_id}"\n{extra_program_yaml}',
        encoding="utf-8",
    )
    (program_dir / "knowledge" / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "alice"\n    email: "alice@acme.com"\n    display_name: "Alice"\n',
        encoding="utf-8",
    )
    return program_dir


# ---------------------------------------------------------------------------
# read_program_kb_documents: path resolution
# ---------------------------------------------------------------------------

def test_read_program_kb_documents_returns_default_documents_when_nothing_exists(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    documents = read_program_kb_documents("acme", programs_root=programs_root)

    assert documents["knowledge/people_directory.yaml"] == {"schema_version": "1.0", "people": []}
    assert documents["workstreams.yaml"] == {"schema_version": "2.0", "workstreams": []}


def test_read_program_kb_documents_reads_program_local_when_no_shared_root(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")

    documents = read_program_kb_documents("acme", programs_root=programs_root)

    people = documents["knowledge/people_directory.yaml"]["people"]
    assert len(people) == 1
    assert people[0]["alias"] == "alice"


def test_read_program_kb_documents_prefers_shared_root_when_present(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "shared_bob"\n    email: "bob@acme.com"\n',
        encoding="utf-8",
    )

    documents = read_program_kb_documents("acme", programs_root=programs_root)

    people = documents["knowledge/people_directory.yaml"]["people"]
    assert len(people) == 1
    assert people[0]["alias"] == "shared_bob"


def test_read_program_kb_documents_engms_pages_prefers_local_over_shared(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root, "acme")
    (program_dir / "knowledge" / "engms_pages.yaml").write_text(
        'schema_version: "1.0"\npages:\n  - id: "local-page"\n', encoding="utf-8",
    )
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "engms_pages.yaml").write_text(
        'schema_version: "1.0"\npages:\n  - id: "shared-page"\n', encoding="utf-8",
    )

    documents = read_program_kb_documents("acme", programs_root=programs_root)

    pages = documents["knowledge/engms_pages.yaml"]["pages"]
    assert len(pages) == 1
    assert pages[0]["id"] == "local-page"


# ---------------------------------------------------------------------------
# validate_program_kb: happy path + each referential-integrity failure mode
# ---------------------------------------------------------------------------

def test_validate_program_kb_passes_with_clean_data(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(
        programs_root, "acme",
        extra_program_yaml='charter:\n  stakeholder_register:\n    - alias: "alice"\n',
    )
    (program_dir / "workstreams.yaml").write_text(
        'schema_version: "2.0"\nworkstreams:\n  - id: "ws1"\n    name: "WS1"\n    raci:\n      accountable: "alice"\n',
        encoding="utf-8",
    )

    knowledge = validate_program_kb("acme", programs_root=programs_root)

    assert len(knowledge.people_directory) == 1


def test_validate_program_kb_raises_on_unknown_charter_stakeholder(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(
        programs_root, "acme",
        extra_program_yaml='charter:\n  stakeholder_register:\n    - alias: "ghost"\n',
    )

    with pytest.raises(ConfigError, match="Unknown charter stakeholder alias"):
        validate_program_kb("acme", programs_root=programs_root)


def test_validate_program_kb_raises_on_unknown_raci_accountable(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root, "acme")
    (program_dir / "workstreams.yaml").write_text(
        'schema_version: "2.0"\nworkstreams:\n  - id: "ws1"\n    name: "WS1"\n    raci:\n      accountable: "ghost"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unknown raci.accountable alias"):
        validate_program_kb("acme", programs_root=programs_root)


def test_validate_program_kb_raises_on_unknown_workstream_owner(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root, "acme")
    (program_dir / "workstreams.yaml").write_text(
        'schema_version: "2.0"\nworkstreams:\n  - id: "ws1"\n    name: "WS1"\n    pm_owner: "ghost"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unknown pm_owner"):
        validate_program_kb("acme", programs_root=programs_root)


def test_validate_program_kb_raises_on_unknown_scorecard_workstream_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root, "acme")
    (program_dir / "scorecards.yaml").write_text(
        'schema_version: "2.0"\nscorecards:\n  - name: "sc1"\n    dimensions:\n      - name: "dim1"\n        workstream_id: "ghost-ws"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unknown workstream_id 'ghost-ws' referenced by scorecard"):
        validate_program_kb("acme", programs_root=programs_root)


def test_validate_program_kb_raises_on_unknown_golden_query_workstream_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root, "acme")
    (program_dir / "knowledge" / "golden_queries.yaml").write_text(
        'schema_version: "1.0"\nqueries:\n  - id: "q1"\n    workstream_ids: ["ghost-ws"]\n', encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unknown workstream_id 'ghost-ws' referenced by golden query"):
        validate_program_kb("acme", programs_root=programs_root)


# ---------------------------------------------------------------------------
# parse_deterministic_kb_correction
# ---------------------------------------------------------------------------

def test_parse_deterministic_kb_correction_set_title() -> None:
    plan = parse_deterministic_kb_correction("set alice title to Staff PM", program_id="acme")

    assert plan is not None
    assert plan.operations[0].file_path == "knowledge/people_directory.yaml"
    assert plan.operations[0].fields == (("title", "Staff PM"),)


def test_parse_deterministic_kb_correction_add_team() -> None:
    plan = parse_deterministic_kb_correction("add team platform to alice", program_id="acme")

    assert plan is not None
    assert plan.operations[0].action == "add_list_value"
    assert plan.operations[0].value == "platform"


def test_parse_deterministic_kb_correction_clear_owner() -> None:
    plan = parse_deterministic_kb_correction("clear ws1 pm owner", program_id="acme")

    assert plan is not None
    assert plan.operations[0].file_path == "workstreams.yaml"
    assert plan.operations[0].fields == (("pm_owner", None),)


def test_parse_deterministic_kb_correction_returns_none_for_unmatched_text() -> None:
    assert parse_deterministic_kb_correction("do something unrelated", program_id="acme") is None


# ---------------------------------------------------------------------------
# parse_kb_update_operations (AI planner payload)
# ---------------------------------------------------------------------------

def test_parse_kb_update_operations_parses_a_valid_payload() -> None:
    operations = parse_kb_update_operations({
        "operations": [
            {"path": "knowledge/people_directory.yaml", "action": "set_fields", "match_value": "alice", "fields": {"title": "Staff PM"}},
        ],
    })

    assert len(operations) == 1
    assert operations[0].match_value == "alice"


def test_parse_kb_update_operations_rejects_unsupported_path() -> None:
    with pytest.raises(ValueError, match="Unsupported KB document path"):
        parse_kb_update_operations({"operations": [{"path": "nope.yaml", "action": "set_fields", "match_value": "x"}]})


def test_parse_kb_update_operations_rejects_empty_operations_list() -> None:
    with pytest.raises(ValueError, match="non-empty operations list"):
        parse_kb_update_operations({"operations": []})


# ---------------------------------------------------------------------------
# prepare_kb_update / apply_kb_update round trip
# ---------------------------------------------------------------------------

def test_prepare_and_apply_kb_update_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    plan = parse_deterministic_kb_correction("set alice title to Staff PM", program_id="acme")
    assert plan is not None

    preview = prepare_kb_update(plan, programs_root=programs_root)
    assert len(preview.changes) == 1
    assert "Staff PM" in preview.diff

    result = apply_kb_update(preview, programs_root=programs_root, edited_by="test-operator")

    written = (programs_root / "acme" / "knowledge" / "people_directory.yaml").read_text(encoding="utf-8")
    assert "Staff PM" in written
    audit_records = [json.loads(line) for line in result.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(audit_records) == 1
    assert audit_records[0]["edited_by"] == "test-operator"
    assert audit_records[0]["correction"] == "set alice title to Staff PM"


def test_prepare_kb_update_raises_when_no_changes_produced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    # Setting the SAME value the fixture already has produces no diff.
    plan = parse_deterministic_kb_correction("set alice display name to Alice", program_id="acme")
    assert plan is not None

    with pytest.raises(ValueError, match="no YAML changes"):
        prepare_kb_update(plan, programs_root=programs_root)


# ---------------------------------------------------------------------------
# PPL-W3.5b (ii): request-scoped document_cache correctness
# ---------------------------------------------------------------------------

def test_document_cache_none_still_reads_fresh_every_call(tmp_path: Path) -> None:
    """The default (`document_cache=None`) must remain a true no-op --
    every pre-existing caller keeps reading fresh on every call."""
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v1"\n', encoding="utf-8",
    )

    first = read_program_kb_documents("acme", programs_root=programs_root)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v2"\n', encoding="utf-8",
    )
    second = read_program_kb_documents("acme", programs_root=programs_root)

    assert first["knowledge/people_directory.yaml"]["people"][0]["alias"] == "v1"
    assert second["knowledge/people_directory.yaml"]["people"][0]["alias"] == "v2"


def test_document_cache_reuses_the_stale_value_across_calls(tmp_path: Path) -> None:
    """Once populated, a shared `document_cache` returns the CACHED value
    even after the underlying file changes -- proving this is a genuine
    cache, not an accidental pass-through."""
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v1"\n', encoding="utf-8",
    )
    cache: dict = {}

    first = read_program_kb_documents("acme", programs_root=programs_root, document_cache=cache)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v2"\n', encoding="utf-8",
    )
    second = read_program_kb_documents("acme", programs_root=programs_root, document_cache=cache)

    assert first["knowledge/people_directory.yaml"]["people"][0]["alias"] == "v1"
    assert second["knowledge/people_directory.yaml"]["people"][0]["alias"] == "v1"


def test_document_cache_shared_across_programs_for_the_same_shared_root_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    _seed_minimal_program(programs_root, "contoso")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "shared"\n', encoding="utf-8",
    )
    cache: dict = {}

    read_program_kb_documents("acme", programs_root=programs_root, document_cache=cache)
    resolved_path = knowledge_root / "people_directory.yaml"
    assert resolved_path in cache
    cache[resolved_path] = {"schema_version": "1.0", "people": [{"alias": "poisoned"}]}

    contoso_documents = read_program_kb_documents("contoso", programs_root=programs_root, document_cache=cache)

    # Both programs resolve to the SAME shared-root file, so contoso's read
    # hits the same cache entry (proving one cache serves multiple programs).
    assert contoso_documents["knowledge/people_directory.yaml"]["people"][0]["alias"] == "poisoned"


def test_document_cache_returned_documents_are_independent_copies(tmp_path: Path) -> None:
    """Mutating a document returned from a cached read must never corrupt
    the cache entry itself for a later read."""
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    cache: dict = {}

    first = read_program_kb_documents("acme", programs_root=programs_root, document_cache=cache)
    first["knowledge/people_directory.yaml"]["people"][0]["alias"] = "mutated-by-caller"
    second = read_program_kb_documents("acme", programs_root=programs_root, document_cache=cache)

    assert second["knowledge/people_directory.yaml"]["people"][0]["alias"] == "alice"


def test_validate_program_kb_with_document_cache_produces_the_same_result_as_without(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(
        programs_root, "acme",
        extra_program_yaml='charter:\n  stakeholder_register:\n    - alias: "alice"\n',
    )
    cache: dict = {}

    without_cache = validate_program_kb("acme", programs_root=programs_root)
    with_cache = validate_program_kb("acme", programs_root=programs_root, document_cache=cache)

    assert len(without_cache.people_directory) == len(with_cache.people_directory) == 1
    assert without_cache.people_directory[0].alias == with_cache.people_directory[0].alias == "alice"


# ---------------------------------------------------------------------------
# PPL-W3.5c: SharedKnowledgeTempCache
# ---------------------------------------------------------------------------

def test_validate_program_kb_with_shared_knowledge_cache_produces_the_same_result_as_without(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(
        programs_root, "acme", extra_program_yaml='charter:\n  stakeholder_register:\n    - alias: "alice"\n',
    )
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "alice"\n', encoding="utf-8",
    )

    without_cache = validate_program_kb("acme", programs_root=programs_root)
    with SharedKnowledgeTempCache() as shared_cache:
        with_cache = validate_program_kb("acme", programs_root=programs_root, shared_knowledge_cache=shared_cache)

    assert len(without_cache.people_directory) == len(with_cache.people_directory) == 1
    assert without_cache.people_directory[0].alias == with_cache.people_directory[0].alias == "alice"


def test_shared_knowledge_cache_reuses_stable_shared_documents_across_programs(tmp_path: Path) -> None:
    """`shared_knowledge_cache` dumps the shared subset to one stable temp
    directory ONCE -- matching this file's own established
    mutate-between-calls idiom (`test_document_cache_reuses_the_stale_value_across_calls`),
    a later on-disk change to the shared root must NOT be observed by a
    later program's call reusing the SAME cache instance, proving genuine
    reuse rather than a per-call re-dump. Paired with `document_cache`
    exactly as `run_kb_doctor` wires them together in production -- the
    two caches share the same "the shared registry doesn't change during
    this run" scope."""
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    _seed_minimal_program(programs_root, "contoso")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v1"\n', encoding="utf-8",
    )
    document_cache: dict = {}

    with SharedKnowledgeTempCache() as shared_cache:
        acme_knowledge = validate_program_kb(
            "acme", programs_root=programs_root, document_cache=document_cache, shared_knowledge_cache=shared_cache,
        )
        (knowledge_root / "people_directory.yaml").write_text(
            'schema_version: "1.0"\npeople:\n  - alias: "v2"\n', encoding="utf-8",
        )
        contoso_knowledge = validate_program_kb(
            "contoso", programs_root=programs_root, document_cache=document_cache, shared_knowledge_cache=shared_cache,
        )

    assert acme_knowledge.people_directory[0].alias == "v1"
    assert contoso_knowledge.people_directory[0].alias == "v1"


def test_shared_knowledge_cache_handles_program_local_engms_pages_override(tmp_path: Path) -> None:
    """A program with its OWN local engms_pages.yaml override must still
    see its local content even while shared_knowledge_cache is active for
    every OTHER shared document -- only that one document falls back to
    the always-fresh per-call path for that program (recomputed per call,
    not assumed static)."""
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root, "acme")
    (program_dir / "knowledge" / "engms_pages.yaml").write_text(
        'schema_version: "1.0"\npages:\n  - id: "local-page"\n    url: "https://eng.ms/local-page"\n', encoding="utf-8",
    )
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "alice"\n', encoding="utf-8",
    )
    (knowledge_root / "engms_pages.yaml").write_text(
        'schema_version: "1.0"\npages:\n  - id: "shared-page"\n    url: "https://eng.ms/shared-page"\n', encoding="utf-8",
    )

    with SharedKnowledgeTempCache() as shared_cache:
        knowledge = validate_program_kb("acme", programs_root=programs_root, shared_knowledge_cache=shared_cache)

    assert len(knowledge.people_directory) == 1
    assert [page.id for page in knowledge.engms_pages] == ["local-page"]


def test_shared_knowledge_cache_close_removes_the_temp_directory(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "alice"\n', encoding="utf-8",
    )

    shared_cache = SharedKnowledgeTempCache()
    validate_program_kb("acme", programs_root=programs_root, shared_knowledge_cache=shared_cache)
    assert shared_cache._temp_dir is not None
    temp_dir_path = Path(shared_cache._temp_dir.name)
    assert temp_dir_path.exists()

    shared_cache.close()

    assert not temp_dir_path.exists()


def test_shared_knowledge_cache_context_manager_cleans_up_on_exit(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root, "acme")
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "alice"\n', encoding="utf-8",
    )

    with SharedKnowledgeTempCache() as shared_cache:
        validate_program_kb("acme", programs_root=programs_root, shared_knowledge_cache=shared_cache)
        assert shared_cache._temp_dir is not None
        temp_dir_path = Path(shared_cache._temp_dir.name)
        assert temp_dir_path.exists()

    assert not temp_dir_path.exists()


# ---------------------------------------------------------------------------
# PPL-W3.5c: load_knowledge's document_cache (knowledge_store.py)
# ---------------------------------------------------------------------------

def test_load_knowledge_document_cache_none_still_reads_fresh_every_call(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v1"\n', encoding="utf-8",
    )

    first = load_knowledge(knowledge_root=knowledge_root)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v2"\n', encoding="utf-8",
    )
    second = load_knowledge(knowledge_root=knowledge_root)

    assert first.people_directory[0].alias == "v1"
    assert second.people_directory[0].alias == "v2"


def test_load_knowledge_document_cache_reuses_the_stale_value_across_calls(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v1"\n', encoding="utf-8",
    )
    cache: dict = {}

    first = load_knowledge(knowledge_root=knowledge_root, document_cache=cache)
    (knowledge_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v2"\n', encoding="utf-8",
    )
    second = load_knowledge(knowledge_root=knowledge_root, document_cache=cache)

    assert first.people_directory[0].alias == "v1"
    assert second.people_directory[0].alias == "v1"


def test_load_knowledge_document_cache_keys_by_the_final_resolved_fallback_path(tmp_path: Path) -> None:
    """The realistic PPL-W3.5c shape: `knowledge_root` has nothing, so
    every read falls through to `fallback_root` -- the cache must key by
    the FINAL resolved (fallback) path, not the originally-requested
    `knowledge_root` path, so a second, DIFFERENT `knowledge_root` pointed
    at the SAME `fallback_root` still hits the same cache entry (mirroring
    `SharedKnowledgeTempCache`'s own real usage, where every program's
    per-call temp `knowledge_root` is empty for the shared documents)."""
    fallback_root = tmp_path / "shared"
    fallback_root.mkdir(parents=True)
    (fallback_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v1"\n', encoding="utf-8",
    )
    empty_root_one = tmp_path / "empty1"
    empty_root_one.mkdir(parents=True)
    empty_root_two = tmp_path / "empty2"
    empty_root_two.mkdir(parents=True)
    cache: dict = {}

    first = load_knowledge(knowledge_root=empty_root_one, fallback_root=fallback_root, document_cache=cache)
    (fallback_root / "people_directory.yaml").write_text(
        'schema_version: "1.0"\npeople:\n  - alias: "v2"\n', encoding="utf-8",
    )
    second = load_knowledge(knowledge_root=empty_root_two, fallback_root=fallback_root, document_cache=cache)

    assert first.people_directory[0].alias == "v1"
    assert second.people_directory[0].alias == "v1"
