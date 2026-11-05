from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score, link_risk_action, load_risk_history, load_risk_register, record_risk_update, save_risk_register
from src.core.risk_register_engine import compute_risk_delta_preview_hash, preview_risk_upserts_from_signals, upsert_risk_from_signal
from src.core.program_fact_store import load_program_facts, project_risk_entries


def test_load_risk_register_returns_empty_tuple_when_file_absent(tmp_path: Path) -> None:
    assert load_risk_register("acme", programs_root=tmp_path / "programs") == ()


@pytest.mark.parametrize("kind", ["strategic", "candidate", "hygiene"])
def test_saved_risk_kind_survives_fact_projection(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "programs"
    risk = replace(_risk_entry(), kind=kind)
    save_risk_register("acme", (risk,), programs_root=root)
    assert load_risk_register("acme", programs_root=root)[0].kind == kind
    projected, = project_risk_entries(load_program_facts(
        "acme", programs_root=root, fact_types=("risk.entry",),
    ))
    assert projected.kind == kind


def _write_archive_history(programs_root: Path) -> tuple[Path, Path]:
    import json
    import yaml
    from scripts.archive_obsolete_risks import _content_hash
    from src.core.risk_register_engine import _risk_entry_to_record

    row = _risk_entry_to_record(_risk_entry())
    archive_dir = programs_root / "acme" / ".archive" / "risk_register" / "20261101T194714Z"
    archive_dir.mkdir(parents=True)
    archive_path = archive_dir / "archived_risks.yaml"
    manifest_path = archive_dir / "rollback_manifest.json"
    archive_path.write_text(yaml.safe_dump({
        "schema_version": "1.0", "program_id": "acme", "archived_at": archive_dir.name,
        "reason": "Synthetic archive", "risks": [row],
    }), encoding="utf-8")
    manifest_path.write_text(json.dumps({
        "schema_version": "1", "program_id": "acme", "created_at": archive_dir.name,
        "reason": "Synthetic archive", "archived_count": 1,
        "archive_path": f".archive/risk_register/{archive_dir.name}/archived_risks.yaml",
        "archived": [{"risk_id": row["id"], "title": row["title"], "kind": row["kind"],
                      "status": row["status"], "content_hash": _content_hash(row)}],
    }), encoding="utf-8")
    return archive_path, manifest_path


def test_archive_history_reads_typed_retained_rows_without_creating_stores(tmp_path: Path) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    root = tmp_path / "programs"
    _write_archive_history(root)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    rows = load_archived_risk_history("acme", programs_root=root)
    assert len(rows) == 1
    assert rows[0].record == _risk_entry()
    assert rows[0].archived_at == "20261101T194714Z"
    assert rows[0].archive_path.endswith("/archived_risks.yaml")
    assert load_archived_risk_history("acme", rows[0].record.id, root) == rows
    assert load_archived_risk_history("acme", "absent", root) == ()
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("risk_id", [None, "risk-1"])
def test_absent_archive_and_journal_history_does_not_create_program(tmp_path: Path, risk_id: str | None) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    root = tmp_path / "absent" / "programs"
    assert load_archived_risk_history("acme", programs_root=root) == ()
    assert load_risk_history("acme", risk_id, programs_root=root) == ()
    assert not root.exists()


@pytest.mark.parametrize("program_id", ["..", "."])
def test_archive_history_rejects_program_escape(tmp_path: Path, program_id: str) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    with pytest.raises(ConfigError, match="program path"):
        load_archived_risk_history(program_id, programs_root=tmp_path)


@pytest.mark.parametrize("target", ["root", "directory", "manifest", "archive"])
def test_archive_history_rejects_resolved_link_escape(monkeypatch, tmp_path: Path, target: str) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    root = tmp_path / "programs"
    archive, manifest = _write_archive_history(root)
    escaped = {"root": archive.parent.parent, "directory": archive.parent,
               "manifest": manifest, "archive": archive}[target]
    resolve = Path.resolve

    def redirected(path: Path, strict: bool = False) -> Path:
        return tmp_path / "foreign" if path == escaped else resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ConfigError, match="escapes"):
        load_archived_risk_history("acme", programs_root=root)


def test_archive_history_ignores_unrelated_files_and_directories(tmp_path: Path) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    root = tmp_path / "programs"
    archive, _ = _write_archive_history(root)
    (archive.parent.parent / "unrelated").mkdir()
    (archive.parent.parent / "notes.txt").write_text("unrelated", encoding="utf-8")
    assert len(load_archived_risk_history("acme", programs_root=root)) == 1


@pytest.mark.parametrize("source", ["archive", "journal"])
def test_history_unexpected_parser_runtime_error_propagates(monkeypatch, tmp_path: Path, source: str) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("synthetic programming failure")

    if source == "archive":
        _write_archive_history(tmp_path)
        monkeypatch.setattr("src.core.risk_register_engine._parse_risk_entry", broken)
        call = lambda: load_archived_risk_history("acme", programs_root=tmp_path)
    else:
        record_risk_update("acme", "risk-1", "open", "closed", "reviewer", "evidence", tmp_path)
        monkeypatch.setattr("src.core.risk_register_engine.parse_jsonl_line", broken)
        call = lambda: load_risk_history("acme", None, tmp_path)
    with pytest.raises(RuntimeError, match="synthetic programming failure"):
        call()


def test_history_resolve_runtime_error_is_configuration_error(monkeypatch, tmp_path: Path) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    def loop(path: Path, strict: bool = False) -> Path:
        raise RuntimeError("Symlink loop")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", loop)
        with pytest.raises(ConfigError, match="Symlink loop"):
            load_archived_risk_history("acme", programs_root=tmp_path)


def test_journal_history_read_only_rejects_foreign_program_path(tmp_path: Path) -> None:
    path = tmp_path / "foreign" / "journal" / "risk_updates.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"risk_id":"risk-1"}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="escapes"):
        load_risk_history("../foreign", None, tmp_path / "programs")


@pytest.mark.parametrize("program_id", ["../foreign", ".", ".."])
def test_journal_history_read_only_rejects_missing_journal_escape(monkeypatch, tmp_path: Path, program_id: str) -> None:
    monkeypatch.setattr(Path, "exists", lambda path: False)
    with pytest.raises(ConfigError, match="escapes"):
        load_risk_history(program_id, None, tmp_path / "programs")


@pytest.mark.parametrize("mutation", [
    "manifest_json", "manifest_mapping", "manifest_version", "manifest_program",
    "manifest_time", "manifest_reason", "count_bool", "count_mismatch", "summary_list",
    "summary_mapping", "summary_type", "summary_status", "summary_id", "hash",
    "absolute_path", "escape_path", "other_sibling", "missing_manifest", "missing_archive",
    "archive_yaml", "archive_mapping", "archive_version", "archive_program", "archive_time",
    "archive_reason", "risks_mapping", "row_mapping", "row_program", "row_missing_id",
    "duplicate_row", "invalid_row",
])
def test_archive_history_rejects_invalid_actual_pairs_without_repair(tmp_path: Path, mutation: str) -> None:
    import json
    import yaml
    from scripts.archive_obsolete_risks import _content_hash
    from src.core.risk_register_engine import load_archived_risk_history

    root = tmp_path / "programs"
    archive, manifest = _write_archive_history(root)
    doc = yaml.safe_load(archive.read_text(encoding="utf-8"))
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    row = doc["risks"][0]
    if mutation == "manifest_json":
        manifest.write_text("{", encoding="utf-8")
    elif mutation == "archive_yaml":
        archive.write_text("risks: [", encoding="utf-8")
    elif mutation == "missing_manifest":
        manifest.unlink()
    elif mutation == "missing_archive":
        archive.unlink()
    else:
        if mutation == "manifest_mapping": meta = []
        elif mutation == "manifest_version": meta["schema_version"] = "2"
        elif mutation == "manifest_program": meta["program_id"] = "foreign"
        elif mutation == "manifest_time": meta["created_at"] = None
        elif mutation == "manifest_reason": meta["reason"] = 1
        elif mutation == "count_bool": meta["archived_count"] = True
        elif mutation == "count_mismatch": meta["archived_count"] = 2
        elif mutation == "summary_list": meta["archived"] = {}
        elif mutation == "summary_mapping": meta["archived"] = [None]
        elif mutation == "summary_type": meta["archived"][0]["title"] = 1
        elif mutation == "summary_status": meta["archived"][0]["status"] = "closed"
        elif mutation == "summary_id": meta["archived"][0]["risk_id"] = "foreign"
        elif mutation == "hash": row["description"] += " changed"
        elif mutation == "absolute_path": meta["archive_path"] = str(archive.resolve())
        elif mutation == "escape_path": meta["archive_path"] = "..\\foreign\\archived_risks.yaml"
        elif mutation == "other_sibling": meta["archive_path"] = ".archive/risk_register/other/archived_risks.yaml"
        elif mutation == "archive_mapping": doc = []
        elif mutation == "archive_version": doc["schema_version"] = "2.0"
        elif mutation == "archive_program": doc["program_id"] = "foreign"
        elif mutation == "archive_time": doc["archived_at"] = "different"
        elif mutation == "archive_reason": doc["reason"] = "different"
        elif mutation == "risks_mapping": doc["risks"] = {}
        elif mutation == "row_mapping": doc["risks"] = [None]
        elif mutation == "row_program": row["program_id"] = "foreign"
        elif mutation == "row_missing_id": row.pop("id")
        elif mutation == "duplicate_row":
            doc["risks"].append(dict(row))
            meta["archived"].append(dict(meta["archived"][0]))
            meta["archived_count"] = 2
        elif mutation == "invalid_row":
            row["probability"] = "invalid"
            meta["archived"][0]["content_hash"] = _content_hash(row)
        archive.write_text(yaml.safe_dump(doc), encoding="utf-8")
        manifest.write_text(json.dumps(meta), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ConfigError):
        load_archived_risk_history("acme", programs_root=root)
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_archive_history_keeps_same_id_occurrences_in_chronological_order(tmp_path: Path) -> None:
    import json
    import shutil
    import yaml
    from src.core.risk_register_engine import load_archived_risk_history

    root = tmp_path / "programs"
    archive, manifest = _write_archive_history(root)
    earlier = archive.parent.with_name("20261001T194714Z")
    shutil.copytree(archive.parent, earlier)
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    meta["created_at"] = earlier.name
    meta["archive_path"] = f".archive/risk_register/{earlier.name}/archived_risks.yaml"
    (earlier / manifest.name).write_text(json.dumps(meta), encoding="utf-8")
    doc = yaml.safe_load(archive.read_text(encoding="utf-8"))
    doc["archived_at"] = earlier.name
    (earlier / archive.name).write_text(yaml.safe_dump(doc), encoding="utf-8")
    rows = load_archived_risk_history("acme", programs_root=root)
    assert [entry.archived_at for entry in rows] == [earlier.name, archive.parent.name]
    assert rows[0].record.id == rows[1].record.id


def test_journal_history_all_rows_is_read_only_and_preserves_string_filter(tmp_path: Path) -> None:
    record_risk_update("acme", "risk-1", "open", "closed", "reviewer", "one", tmp_path)
    record_risk_update("acme", "risk-2", "open", "mitigated", "reviewer", "two", tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    rows = load_risk_history("acme", None, tmp_path)
    assert [row["risk_id"] for row in rows] == ["risk-1", "risk-2"]
    assert load_risk_history("acme", "risk-1", tmp_path) == rows[:1]
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("bad_line", ["{", "[]"])
def test_journal_history_all_rows_refuses_corruption_without_quarantine(tmp_path: Path, bad_line: str) -> None:
    path = tmp_path / "acme" / "journal" / "risk_updates.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('\n{"risk_id":"risk-1"}\n' + bad_line + "\n", encoding="utf-8")
    before = {path: path.read_bytes()}
    with pytest.raises(ConfigError):
        load_risk_history("acme", None, tmp_path)
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_journal_history_existing_string_filter_keeps_legacy_recovery(tmp_path: Path) -> None:
    path = tmp_path / "acme" / "journal" / "risk_updates.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"risk_id":"risk-1"}\ninvalid\n', encoding="utf-8")
    assert load_risk_history("acme", "risk-1", tmp_path) == ({"risk_id": "risk-1"},)
    assert path.read_text(encoding="utf-8") == '{"risk_id":"risk-1"}\n'
    assert len(tuple((path.parent / "quarantine").glob("*.jsonl"))) == 1


def test_load_risk_register_round_trips_actual_empty_writer(tmp_path: Path) -> None:
    import yaml

    programs_root = tmp_path / "programs"
    save_risk_register("synthetic", (), programs_root=programs_root)
    path = programs_root / "synthetic" / "risk_register.yaml"
    before = path.read_bytes()
    assert yaml.safe_load(before) == {"schema_version": "1.0", "risks": []}
    assert load_risk_register("synthetic", programs_root=programs_root) == ()
    assert path.read_bytes() == before


@pytest.mark.parametrize("collection", ["", "risks: null", "risks: {}", "risks: {id: risk-1}",
                                      "risks: false", "risks: 0", "risks: ''", "risks: invalid"])
def test_load_risk_register_rejects_missing_or_malformed_collection(tmp_path: Path, collection: str) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "synthetic" / "risk_register.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f"schema_version: '1.0'\n{collection}\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigError, match="Expected 'risks' list"):
        load_risk_register("synthetic", programs_root=programs_root)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("schema_version: '1.0'\nrisks: [null]\n", "must be a mapping"),
        ("schema_version: '1.0'\nrisks: [{}]\n", "Invalid risk entry"),
        ("schema_version: '1.0'\nrisks: [\n", "Invalid YAML"),
        ("{}\n", "Unsupported risk register schema_version"),
        ("schema_version: 1\nrisks: []\n", "Unsupported risk register schema_version"),
        ("- invalid\n", "Expected mapping"),
    ],
)
def test_load_risk_register_preserves_invalid_document_refusal(tmp_path: Path, document: str, message: str) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "synthetic" / "risk_register.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_risk_register("synthetic", programs_root=programs_root)


def test_load_risk_register_preserves_duplicate_refusal(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = _risk_entry()
    save_risk_register("acme", (entry, entry), programs_root=programs_root)
    with pytest.raises(ConfigError, match="Duplicate risk id"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_reads_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    mitigation_plan: Escalate in the weekly checkpoint.",
                "    mitigation_due_date: 2026-05-20",
                "    linked_workstream_ids:",
                "      - acme",
                "    linked_work_item_ids:",
                "      - 900001",
                "    linked_milestone_ids:",
                "      - m4-contoso-pilot",
                "    linked_claim_ids:",
                "      - claim-1",
                "    linked_action_ids:",
                "      - action-1",
                "    status: open",
                "    identified_date: 2026-05-01",
                "    identified_in_vertex_issue: 77",
                "    last_reviewed_date: 2026-05-08",
                "    entity_refs:",
                "      - WI:900001",
            )
        ),
        encoding="utf-8",
    )

    entries = load_risk_register("acme", programs_root=programs_root)

    assert len(entries) == 1
    assert entries[0].category == RiskCategory.SCHEDULE
    assert entries[0].mitigation_due_date == date(2026, 5, 20)
    assert entries[0].linked_work_item_ids == (900001,)


def test_load_risk_register_rejects_invalid_schema(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text('schema_version: "2.0"\nrisks: []\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="Unsupported risk register schema_version"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    status: 1",
                "    identified_date: 2026-05-01",
                "    entity_refs: []",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="status must be a string"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_rejects_non_string_entity_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    status: open",
                "    identified_date: 2026-05-01",
                "    entity_refs:",
                "      - 900001",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="entity_refs must contain strings only"):
        load_risk_register("acme", programs_root=programs_root)


def test_load_risk_register_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "risks:",
                "  - id: risk-1",
                "    title: Firmware sign-off lag",
                "    description: Firmware sign-off may miss the pilot checkpoint.",
                "    probability: likely",
                "    impact: high",
                "    category: schedule",
                "    owner_alias: maintainer",
                "    status: open",
                "    identified_date: 2026-05-01",
                '    identified_in_vertex_issue: "77"',
                "    entity_refs: []",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="identified_in_vertex_issue must be an integer"):
        load_risk_register("acme", programs_root=programs_root)


def test_assess_risk_staleness_flags_unreviewed_open_risk() -> None:
    entry = _risk_entry(identified_date=date(2026, 4, 1), last_reviewed_date=None)

    assert assess_risk_staleness(entry, as_of=date(2026, 5, 10)) is True


def test_compute_risk_score_multiplies_probability_and_impact() -> None:
    entry = _risk_entry(probability=RiskProbability.LIKELY, impact=RiskImpact.HIGH)

    assert compute_risk_score(entry) == 9


def test_risk_summary_priority_preserves_existing_order_and_stable_ties() -> None:
    from src.core.risk_register_engine import risk_summary_sort_key

    base = _risk_entry()
    risks = (
        replace(base, id="tie-first", title="Alpha"),
        replace(base, id="tie-second", title="ALPHA"),
        replace(base, id="zulu", title="Zulu"),
        replace(base, id="score", probability=RiskProbability.VERY_LIKELY, impact=RiskImpact.CRITICAL),
        replace(base, id="escalated", status=RiskStatus.ESCALATED, probability=RiskProbability.UNLIKELY, impact=RiskImpact.LOW),
        replace(base, id="unassessed", probability=RiskProbability.UNASSESSED, impact=RiskImpact.UNASSESSED),
    )

    assert [risk.id for risk in sorted(risks, key=risk_summary_sort_key)] == [
        "escalated", "score", "tie-first", "tie-second", "zulu", "unassessed",
    ]


def test_save_risk_register_round_trips_and_creates_backup(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    register_path = programs_root / "acme" / "risk_register.yaml"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text('schema_version: "1.0"\nrisks: []\n', encoding="utf-8")

    save_risk_register("acme", (_risk_entry(),), programs_root=programs_root)

    backup_path = register_path.with_suffix(".yaml.bak")
    assert backup_path.exists()
    entries = load_risk_register("acme", programs_root=programs_root)
    assert len(entries) == 1
    assert entries[0].title == "Firmware sign-off lag"


def test_save_risk_register_dual_writes_current_fact_store_projection(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = _risk_entry()

    save_risk_register("acme", (entry,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert tuple(replace(risk, fact_id=None, last_validated_at=None) for risk in project_risk_entries(snapshot)) == (entry,)


def test_save_risk_register_threads_source_signal_ids_onto_the_fact_revision(tmp_path: Path) -> None:
    # ADF-W2.4/W2.5: a signal-derived risk's source_signal_ids must land on
    # the ProgramFactRevision's own top-level field (what fact_lineage_
    # coverage.py's classifier actually inspects), not just inside the
    # payload dict RiskEntry projection round-trips through.
    programs_root = tmp_path / "programs"
    entry = _risk_entry_with_source_signal_ids(("sig-abc-123",))

    save_risk_register("acme", (entry,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)
    risk_facts = [fact for fact in snapshot.facts if fact.fact_type == "risk.entry"]

    assert len(risk_facts) == 1
    assert risk_facts[0].source_signal_ids == ("sig-abc-123",)

    from src.core.fact_lineage_coverage import has_fact_provenance
    assert has_fact_provenance(risk_facts[0]) is True


def test_save_risk_register_closes_removed_fact_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = _risk_entry()

    save_risk_register("acme", (entry,), programs_root=programs_root)
    save_risk_register("acme", (), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_risk_entries(snapshot) == ()


def test_record_risk_update_and_load_risk_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    record_risk_update(
        "acme",
        "risk-1",
        "open",
        "escalated",
        "maintainer",
        "Escalated in the weekly sync.",
        programs_root=programs_root,
    )
    record_risk_update(
        "acme",
        "risk-2",
        "open",
        "mitigated",
        "maintainer",
        None,
        programs_root=programs_root,
    )

    history = load_risk_history("acme", "risk-1", programs_root=programs_root)

    assert len(history) == 1
    assert history[0]["risk_id"] == "risk-1"
    assert history[0]["new_status"] == "escalated"
    assert history[0]["author"] == "maintainer"


def test_link_risk_action_appends_new_action_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_risk_register("acme", (_risk_entry(linked_action_ids=("action-1",)),), programs_root=programs_root)

    updated = link_risk_action("acme", "risk-1", "action-2", programs_root=programs_root)
    entries = load_risk_register("acme", programs_root=programs_root)

    assert updated.linked_action_ids == ("action-1", "action-2")
    assert entries[0].linked_action_ids == ("action-1", "action-2")


def _risk_entry(
    *,
    probability: RiskProbability = RiskProbability.LIKELY,
    impact: RiskImpact = RiskImpact.HIGH,
    identified_date: date = date(2026, 5, 1),
    last_reviewed_date: date | None = date(2026, 5, 8),
    linked_action_ids: tuple[str, ...] = ("action-1",),
) -> RiskEntry:
    return RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Firmware sign-off lag",
        description="Firmware sign-off may miss the pilot checkpoint.",
        probability=probability,
        impact=impact,
        category=RiskCategory.SCHEDULE,
        owner_alias="maintainer",
        mitigation_plan="Escalate in the weekly checkpoint.",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("acme",),
        linked_work_item_ids=(900001,),
        linked_milestone_ids=("m4-contoso-pilot",),
        linked_claim_ids=("claim-1",),
        linked_action_ids=linked_action_ids,
        status=RiskStatus.OPEN,
        identified_date=identified_date,
        identified_in_vertex_issue=77,
        last_reviewed_date=last_reviewed_date,
        entity_refs=("WI:900001",),
    )


def _risk_entry_with_source_signal_ids(source_signal_ids: tuple[str, ...]) -> RiskEntry:
    return replace(_risk_entry(), source_signal_ids=source_signal_ids)


# ---------------------------------------------------------------------------
# FR-SG-20: derive_strategic_risk_level
# ---------------------------------------------------------------------------

from src.core.models_v2 import RiskDerivedLevel
from src.core.risk_register_engine import derive_strategic_risk_level
from src.core.models import WorkItem, RiskLevel


def _work_item(
    work_item_id: int,
    *,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    state: str = "Active",
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title="Test item",
        state=state,
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\Sprint 24",
        target_date=date(2026, 5, 20),
        risk_level=risk_level,
        tags=[],
        custom_fields={},
    )


def test_derive_strategic_risk_level_returns_risk_derived_level() -> None:
    result = derive_strategic_risk_level("acme", (_work_item(101),))
    assert isinstance(result, RiskDerivedLevel)


def test_derive_strategic_risk_level_is_always_proposal() -> None:
    result = derive_strategic_risk_level("acme", (_work_item(101),))
    assert result.is_proposal is True


def test_derive_strategic_risk_level_empty_items_returns_unknown() -> None:
    result = derive_strategic_risk_level("acme", ())
    assert result.proposed_level == RiskLevel.UNKNOWN


def test_derive_strategic_risk_level_majority_high_items_returns_high() -> None:
    items = (
        _work_item(1, risk_level=RiskLevel.HIGH),
        _work_item(2, risk_level=RiskLevel.HIGH),
        _work_item(3, risk_level=RiskLevel.MEDIUM),
    )
    result = derive_strategic_risk_level("acme", items)
    assert result.proposed_level == RiskLevel.HIGH


def test_derive_strategic_risk_level_scope_delta_upgrades_to_high() -> None:
    # All items are medium, but new high-risk items appeared → should upgrade
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    result = derive_strategic_risk_level("acme", items, scope_delta_new_high_risk=3)
    assert result.proposed_level == RiskLevel.HIGH
    assert result.upgrade_reason is not None


def test_derive_strategic_risk_level_low_trajectory_credibility_upgrades() -> None:
    # Medium base + very low trajectory credibility → should upgrade
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    result = derive_strategic_risk_level("acme", items, trajectory_credibility=0.2)
    assert result.proposed_level in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    # With low credibility, upgrade reason should be set
    assert result.upgrade_reason is not None


def test_derive_strategic_risk_level_escalated_risk_entry_upgrades() -> None:
    # MEDIUM base + escalated risk register entry → HIGH (one upgrade step: MEDIUM→HIGH)
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    escalated_entry = _risk_entry()
    from src.core.models_v2 import RiskStatus
    import dataclasses
    escalated = dataclasses.replace(escalated_entry, status=RiskStatus.ESCALATED)
    result = derive_strategic_risk_level("acme", items, risk_entries=(escalated,))
    assert result.proposed_level == RiskLevel.HIGH
    assert result.upgrade_reason is not None


def test_derive_strategic_risk_level_upgrade_reason_populated_on_upgrade() -> None:
    items = (_work_item(1, risk_level=RiskLevel.MEDIUM),)
    result = derive_strategic_risk_level("acme", items, scope_delta_new_high_risk=1)
    assert result.upgrade_reason is not None
    assert result.downgrade_reason is None


def test_derive_strategic_risk_level_no_upgrade_for_low_items() -> None:
    items = (
        _work_item(1, risk_level=RiskLevel.LOW),
        _work_item(2, risk_level=RiskLevel.LOW),
    )
    result = derive_strategic_risk_level("acme", items)
    assert result.proposed_level == RiskLevel.LOW
    assert result.upgrade_reason is None


class TestRiskDeltaPreview:
    """D-17/ARM-GATHER-11 AG-6.3: confirm dry-run's non-mutating risk-register
    delta preview must never drift from what upsert_risk_from_signal (the
    real, mutating path) would actually do -- both share _decide_risk_upsert.
    """

    def test_preview_new_signal_reports_new_without_writing_register(self, tmp_path: Path) -> None:
        register_path = tmp_path / "acme" / "risk_register.yaml"
        assert not register_path.exists()

        previews = preview_risk_upserts_from_signals(
            "acme",
            (("sig-1", "Build pipeline flakiness increasing", ("12345",), "ws-release"),),
            programs_root=tmp_path,
        )

        assert len(previews) == 1
        assert previews[0].signal_id == "sig-1"
        assert previews[0].action == "new"
        assert previews[0].title.startswith("Build pipeline flakiness")
        # Non-mutating: no register file was created by the preview.
        assert not register_path.exists()
        assert load_risk_register("acme", programs_root=tmp_path) == ()

    def test_preview_matches_real_upsert_for_new_then_repeated_signal(self, tmp_path: Path) -> None:
        signal_args = dict(
            signal_id="sig-2",
            signal_text="Deployment rollout stalled on ring 2",
            signal_entity_refs=("55555",),
            signal_workstream_id="ws-deploy",
        )

        preview_before = preview_risk_upserts_from_signals(
            "acme", ((signal_args["signal_id"], signal_args["signal_text"], signal_args["signal_entity_refs"], signal_args["signal_workstream_id"]),), programs_root=tmp_path
        )
        assert preview_before[0].action == "new"

        real_entry = upsert_risk_from_signal("acme", programs_root=tmp_path, **signal_args)

        # AG-6.5: preview's predicted risk_id/title match what the real upsert produced.
        assert preview_before[0].risk_id == real_entry.id
        assert preview_before[0].title == real_entry.title

        # Re-observing the identical signal is a no-op for both preview and real upsert.
        preview_after = preview_risk_upserts_from_signals(
            "acme", ((signal_args["signal_id"], signal_args["signal_text"], signal_args["signal_entity_refs"], signal_args["signal_workstream_id"]),), programs_root=tmp_path
        )
        assert preview_after[0].action == "no_change"
        assert preview_after[0].risk_id == real_entry.id

    def test_preview_matches_field_for_matching_entity_ref_update(self, tmp_path: Path) -> None:
        _write_minimal_register_for_preview(
            tmp_path / "acme",
            [
                {
                    "id": "risk-existing",
                    "program_id": "acme",
                    "title": "Existing risk",
                    "description": "Already tracked",
                    "probability": "likely",
                    "impact": "high",
                    "category": "technical",
                    "owner_alias": "owner",
                    "status": "open",
                    "identified_date": "2026-07-01",
                    "entity_refs": ["77777"],
                    "source_signal_ids": ["sig-original"],
                }
            ],
        )

        previews = preview_risk_upserts_from_signals(
            "acme",
            (("sig-new", "A related update on the same risk", ("77777",), None),),
            programs_root=tmp_path,
        )

        assert previews[0].action == "updated"
        assert previews[0].risk_id == "risk-existing"
        # Non-mutating: the register on disk is untouched.
        reloaded = load_risk_register("acme", programs_root=tmp_path)
        assert reloaded[0].source_signal_ids == ("sig-original",)

    def test_preview_hash_is_stable_for_identical_input_and_changes_on_divergence(self, tmp_path: Path) -> None:
        signals = (("sig-1", "Risk A", ("1",), None), ("sig-2", "Risk B", ("2",), None))

        preview_a = preview_risk_upserts_from_signals("acme", signals, programs_root=tmp_path)
        preview_b = preview_risk_upserts_from_signals("acme", signals, programs_root=tmp_path)
        assert compute_risk_delta_preview_hash(preview_a) == compute_risk_delta_preview_hash(preview_b)

        different_signals = signals + (("sig-3", "Risk C", ("3",), None),)
        preview_c = preview_risk_upserts_from_signals("acme", different_signals, programs_root=tmp_path)
        assert compute_risk_delta_preview_hash(preview_a) != compute_risk_delta_preview_hash(preview_c)

    def test_preview_batch_sequential_matching_mirrors_real_upsert_loop(self, tmp_path: Path) -> None:
        # Two signals sharing an entity_ref within the same preview batch: the
        # second must be seen as matching the first's not-yet-persisted new risk,
        # exactly like the real per-signal upsert loop in archive_transaction.py.
        signals = (
            ("sig-1", "Storage node flapping under load", ("88888",), None),
            ("sig-2", "Storage node flapping under load (follow-up)", ("88888",), None),
        )

        previews = preview_risk_upserts_from_signals("acme", signals, programs_root=tmp_path)

        assert previews[0].action == "new"
        assert previews[1].action == "updated"
        assert previews[1].risk_id == previews[0].risk_id


def _write_minimal_register_for_preview(program_dir: Path, risks: list[dict]) -> None:
    import yaml

    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "risk_register.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "risks": risks}, sort_keys=False),
        encoding="utf-8",
    )