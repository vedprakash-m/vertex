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

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError, StateError
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
from src.core.program_fact_store import load_program_facts, project_workstreams
from src.core.workstream_documents import save_workstreams_document

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


def _seed_workstream_repair(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    document = {
        "schema_version": "2.0",
        "custom_context": {"preserve": "authored"},
        "workstreams": [
            {
                "id": "delivery", "name": "Delivery",
                "pm_owner": "alice", "eng_owner": "alice", "alternate_owner": "alice",
                "last_reviewed_date": "2026-06-01", "custom_notes": ["keep"],
                "ado_repository_ids": ["repo-1"],
            },
            {"id": "other", "name": "Other", "pm_owner": "alice"},
        ],
    }
    for program_id in ("acme", "fabrikam"):
        program_dir = _seed_minimal_program(programs_root, program_id)
        people_path = program_dir / "knowledge" / "people_directory.yaml"
        people_path.write_text(
            people_path.read_text(encoding="utf-8") + '  - alias: "bob"\n    display_name: "Bob"\n',
            encoding="utf-8",
        )
        save_workstreams_document(program_id, document, programs_root=programs_root)
    editions = programs_root / "acme" / "editions"
    editions.mkdir()
    (editions / "acme_weekly.yaml").write_text(
        'schema_version: "2.0"\nid: acme_weekly\nprogram_id: acme\nname: Weekly\n'
        'type: weekly\naltitude: program\ncadence: weekly\n',
        encoding="utf-8",
    )
    (programs_root / "acme" / "scorecards.yaml").write_text(
        'schema_version: "2.0"\nscorecards: []\n', encoding="utf-8",
    )
    return programs_root


def test_kb_workstream_apply_uses_one_canonical_save_and_preserves_preview_history(
    tmp_path: Path, monkeypatch,
) -> None:
    import src.core.kb_updates as kb_updates
    from src.core.kb_updates import KbUpdateOperation, KbUpdatePlan

    root = _seed_workstream_repair(tmp_path)
    before_time = datetime.now(timezone.utc)
    plan = KbUpdatePlan("acme", "repair owner and title", "deterministic", (
        KbUpdateOperation("workstreams.yaml", "set_fields", "delivery", fields=(("pm_owner", "bob"),)),
        KbUpdateOperation("knowledge/people_directory.yaml", "set_fields", "alice", fields=(("title", "Staff PM"),)),
    ))
    preview = prepare_kb_update(plan, programs_root=root)
    calls = []

    def save(program_id, document, *, programs_root):
        calls.append((program_id, document, programs_root))
        return save_workstreams_document(program_id, document, programs_root=programs_root)

    monkeypatch.setattr(kb_updates, "save_workstreams_document", save, raising=False)
    result = apply_kb_update(preview, programs_root=root, edited_by="test-operator")
    current = {entry.id: entry for entry in project_workstreams(load_program_facts(
        "acme", programs_root=root, as_of=datetime.now(timezone.utc),
    ))}
    assert current["delivery"].pm_owner == "bob"
    assert len(calls) == 1
    assert calls[0] == ("acme", next(
        change.after_document for change in preview.changes if change.relative_path == "workstreams.yaml"
    ), root)
    for change in preview.changes:
        assert change.absolute_path.read_bytes() == change.after_text.replace("\n", os.linesep).encode("utf-8")
    old = {entry.id: entry for entry in project_workstreams(load_program_facts("acme", programs_root=root, as_of=before_time))}
    assert old["delivery"].pm_owner == "alice"
    assert current["delivery"].last_reviewed_date == old["delivery"].last_reviewed_date == date(2026, 6, 1)
    assert current["delivery"].ado_repository_ids == old["delivery"].ado_repository_ids == ("repo-1",)
    assert current["other"] == old["other"]
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["edited_by"] == "test-operator"
    change = next(entry for entry in audit["files"] if entry["path"] == "workstreams.yaml")
    assert change["before"]["workstreams"][0]["pm_owner"] == "alice"
    assert change["after"]["workstreams"][0]["pm_owner"] == "bob"
    assert change["after"]["custom_context"] == {"preserve": "authored"}
    assert change["after"]["workstreams"][0]["custom_notes"] == ["keep"]
    assert (root / "acme" / "workstreams.yaml.bak").read_bytes() == next(
        change.before_text.replace("\n", os.linesep).encode("utf-8")
        for change in preview.changes if change.relative_path == "workstreams.yaml"
    )


@pytest.mark.parametrize("failure", ["drift", "unreadable"])
def test_kb_workstream_preflights_later_target_before_canonical_save(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    import src.core.kb_updates as kb_updates
    from dataclasses import replace

    root = _seed_workstream_repair(tmp_path)
    plan = parse_deterministic_kb_correction("set delivery pm owner to bob", program_id="acme")
    assert plan is not None
    preview = prepare_kb_update(plan, programs_root=root)
    first, = preview.changes
    later_path = root / "acme" / "scorecards.yaml"
    later = replace(
        first, relative_path="scorecards.yaml", absolute_path=later_path,
        before_text=later_path.read_text(encoding="utf-8"),
    )
    preview = replace(preview, changes=(first, later))
    if failure == "drift":
        later_path.write_text("changed\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    calls = []
    monkeypatch.setattr(kb_updates, "save_workstreams_document", lambda *args, **kwargs: calls.append(args), raising=False)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if failure == "unreadable" and path == later_path:
            raise PermissionError("synthetic")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(StateError, match="scorecards.yaml"):
        apply_kb_update(preview, programs_root=root)
    assert calls == []
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def _baseline_preview(tmp_path: Path):
    from dataclasses import replace

    root = tmp_path / "programs"
    _seed_minimal_program(root, "acme")
    plan = parse_deterministic_kb_correction("set alice title to Staff PM", program_id="acme")
    assert plan is not None
    preview = prepare_kb_update(plan, programs_root=root)
    first = preview.changes[0]
    second_path = root / "acme" / "scorecards.yaml"
    second_path.write_text("old second\n", encoding="utf-8")
    second = replace(
        first, relative_path="scorecards.yaml", absolute_path=second_path,
        before_text="old second\n", after_text="new second\n",
    )
    return root, replace(preview, changes=(first, second))


def test_apply_kb_update_rejects_stale_second_target_before_any_write(tmp_path: Path) -> None:
    root, preview = _baseline_preview(tmp_path)
    first, second = preview.changes
    second.absolute_path.write_text("concurrent secret\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (first.absolute_path, second.absolute_path)}
    with pytest.raises(StateError, match="scorecards.yaml") as error:
        apply_kb_update(preview, programs_root=root)
    assert "concurrent secret" not in str(error.value)
    assert "preview" in str(error.value).lower()
    assert {path: path.read_bytes() for path in before} == before
    assert not (root / "acme" / "journal").exists()


@pytest.mark.parametrize("initial_exists", [False, True])
def test_apply_kb_update_rejects_target_existence_drift(tmp_path: Path, initial_exists: bool) -> None:
    from dataclasses import replace

    root, preview = _baseline_preview(tmp_path)
    first, second = preview.changes
    path = second.absolute_path
    if initial_exists:
        path.write_text("", encoding="utf-8")
    else:
        path.unlink()
    second = replace(second, before_text="", before_exists=initial_exists)
    preview = replace(preview, changes=(first, second))
    first_before = first.absolute_path.read_bytes()
    if initial_exists:
        path.unlink()
    else:
        path.write_text("", encoding="utf-8")
    with pytest.raises(StateError, match="scorecards.yaml"):
        apply_kb_update(preview, programs_root=root)
    assert first.absolute_path.read_bytes() == first_before
    assert path.exists() is not initial_exists
    if path.exists():
        assert path.read_bytes() == b""
    assert not (root / "acme" / "journal").exists()


@pytest.mark.parametrize("read_error", [PermissionError, UnicodeError])
def test_apply_kb_update_preflight_read_failure_has_no_effects(tmp_path: Path, monkeypatch, read_error) -> None:
    root, preview = _baseline_preview(tmp_path)
    before = {change.absolute_path: change.absolute_path.read_bytes() for change in preview.changes}
    target = preview.changes[1].absolute_path
    original_read = Path.read_text

    def fail_read(path, *args, **kwargs):
        if path == target:
            raise read_error("private error content")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(StateError, match="scorecards.yaml") as error:
        apply_kb_update(preview, programs_root=root)
    assert "private error content" not in str(error.value)
    assert {path: path.read_bytes() for path in before} == before
    assert not (root / "acme" / "journal").exists()


@pytest.mark.parametrize("initial_exists", [False, True])
def test_build_kb_changes_captures_empty_or_missing_target_and_applies(
    tmp_path: Path, initial_exists: bool,
) -> None:
    from copy import deepcopy
    from src.core.kb_updates import KbUpdatePreview, _build_changes

    root = tmp_path / "programs"
    program_dir = _seed_minimal_program(root, "acme")
    target = program_dir / "knowledge" / "people_directory.yaml"
    before = read_program_kb_documents("acme", programs_root=root)
    after = deepcopy(before)
    after["knowledge/people_directory.yaml"]["people"][0]["title"] = "Staff PM"
    if initial_exists:
        target.write_text("", encoding="utf-8")
    else:
        target.unlink()
    changes = _build_changes(program_dir=program_dir, before=before, after=after)
    preview = KbUpdatePreview("acme", "synthetic correction", "deterministic", "", "", changes)
    change, = preview.changes
    assert change.before_exists is initial_exists
    assert change.before_text == ""
    apply_kb_update(preview, programs_root=root)
    assert "Staff PM" in target.read_text(encoding="utf-8")


def test_preflight_stale_second_target_does_not_create_first_target_directory(tmp_path: Path) -> None:
    from dataclasses import replace

    root, preview = _baseline_preview(tmp_path)
    first, second = preview.changes
    new_parent = root / "acme" / "not-created"
    first = replace(
        first, absolute_path=new_parent / "first.yaml", before_text="", before_exists=False,
    )
    second.absolute_path.write_text("concurrent\n", encoding="utf-8")
    second_before = second.absolute_path.read_bytes()
    with pytest.raises(StateError, match="scorecards.yaml"):
        apply_kb_update(replace(preview, changes=(first, second)), programs_root=root)
    assert not new_parent.exists()
    assert not (root / "acme" / "journal").exists()
    assert second.absolute_path.read_bytes() == second_before


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
    shared_cache.shared_root_path(
        relative_paths=("knowledge/people_directory.yaml",),
        documents=read_program_kb_documents("acme", programs_root=programs_root),
    )
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
        shared_cache.shared_root_path(
            relative_paths=("knowledge/people_directory.yaml",),
            documents=read_program_kb_documents("acme", programs_root=programs_root),
        )
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
