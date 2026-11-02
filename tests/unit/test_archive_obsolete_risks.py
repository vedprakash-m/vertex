"""Unit tests for ADF-W4.3: archive obsolete machine-generated risk rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.core.models_v2 import RiskKind
from scripts.archive_obsolete_risks import (
    execute_archive,
    plan_archive,
    rollback_archive,
    select_archive_candidates,
)


def _risk(
    rid: str,
    *,
    kind: str = "strategic",
    status: str = "open",
    probability: str = "likely",
    impact: str = "high",
    title: str = "Risk",
) -> dict:
    return {
        "id": rid,
        "program_id": "samplealpha",
        "title": title,
        "description": "desc",
        "probability": probability,
        "impact": impact,
        "category": "schedule",
        "owner_alias": "unassigned",
        "status": status,
        "identified_date": "2026-07-12",
        "entity_refs": [],
        "kind": kind,
    }


def _write_register(program_dir: Path, risks: list[dict]) -> None:
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "risk_register.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "risks": risks}, sort_keys=False),
        encoding="utf-8",
    )


def test_select_archive_candidates_partitions_correctly(tmp_path: Path) -> None:
    _write_register(
        tmp_path / "samplealpha",
        [
            _risk("r1", kind="candidate", status="closed"),  # archive
            _risk("r2", kind="hygiene", status="mitigated"),  # archive
            _risk("r3", kind="candidate", status="open"),  # keep (not terminal)
            _risk("r4", kind="strategic", status="closed"),  # keep (strategic never auto-archived)
            _risk("r5", kind="strategic", status="open"),  # keep
        ],
    )
    to_archive, to_keep = select_archive_candidates("samplealpha", programs_root=tmp_path)
    archived_ids = {e.id for e in to_archive}
    kept_ids = {e.id for e in to_keep}
    assert archived_ids == {"r1", "r2"}
    assert kept_ids == {"r3", "r4", "r5"}


def test_strategic_risk_never_archived(tmp_path: Path) -> None:
    _write_register(
        tmp_path / "samplealpha",
        [_risk("r1", kind="strategic", status="closed")],
    )
    to_archive, _ = select_archive_candidates("samplealpha", programs_root=tmp_path)
    assert to_archive == []


def test_plan_archive_is_read_only(tmp_path: Path) -> None:
    _write_register(
        tmp_path / "samplealpha",
        [_risk("r1", kind="candidate", status="closed", title="Old cleanup")],
    )
    manifest = plan_archive("samplealpha", programs_root=tmp_path)
    assert manifest.archived_count == 1
    assert manifest.archived[0].risk_id == "r1"
    assert manifest.archived[0].kind == RiskKind.CANDIDATE.value
    # Register unchanged after plan.
    reg = yaml.safe_load((tmp_path / "samplealpha" / "risk_register.yaml").read_text())
    assert len(reg["risks"]) == 1


def test_execute_archive_moves_rows_and_writes_manifest(tmp_path: Path) -> None:
    _write_register(
        tmp_path / "samplealpha",
        [
            _risk("r1", kind="candidate", status="closed"),
            _risk("r2", kind="strategic", status="open", title="Real risk"),
        ],
    )
    manifest = execute_archive("samplealpha", programs_root=tmp_path)
    assert manifest.archived_count == 1
    # Active register now has only the strategic risk.
    reg = yaml.safe_load((tmp_path / "samplealpha" / "risk_register.yaml").read_text())
    assert len(reg["risks"]) == 1
    assert reg["risks"][0]["id"] == "r2"
    # Archive file has the moved row.
    archive_path = tmp_path / "samplealpha" / manifest.archive_path
    assert archive_path.exists()
    archive = yaml.safe_load(archive_path.read_text())
    assert len(archive["risks"]) == 1
    assert archive["risks"][0]["id"] == "r1"
    # Rollback manifest exists and is valid JSON.
    manifest_path = archive_path.parent / "rollback_manifest.json"
    assert manifest_path.exists()
    loaded = json.loads(manifest_path.read_text())
    assert loaded["archived_count"] == 1
    # Backup file exists.
    assert (tmp_path / "samplealpha" / "risk_register.yaml.bak").exists()


def test_rollback_restores_archived_rows(tmp_path: Path) -> None:
    _write_register(
        tmp_path / "samplealpha",
        [
            _risk("r1", kind="candidate", status="closed"),
            _risk("r2", kind="strategic", status="open"),
        ],
    )
    manifest = execute_archive("samplealpha", programs_root=tmp_path)
    archive_path = tmp_path / "samplealpha" / manifest.archive_path
    manifest_path = archive_path.parent / "rollback_manifest.json"
    restored = rollback_archive("samplealpha", manifest_path, programs_root=tmp_path)
    assert restored == 1
    reg = yaml.safe_load((tmp_path / "samplealpha" / "risk_register.yaml").read_text())
    ids = {r["id"] for r in reg["risks"]}
    assert ids == {"r1", "r2"}


def test_execute_archive_no_candidates_is_noop(tmp_path: Path) -> None:
    _write_register(
        tmp_path / "samplealpha",
        [_risk("r1", kind="strategic", status="open")],
    )
    manifest = execute_archive("samplealpha", programs_root=tmp_path)
    assert manifest.archived_count == 0
    # No archive file written.
    assert not (tmp_path / "samplealpha" / manifest.archive_path).exists()


def test_content_hash_stable_and_distinct() -> None:
    from scripts.archive_obsolete_risks import _content_hash

    h1 = _content_hash({"a": 1, "b": 2})
    h2 = _content_hash({"b": 2, "a": 1})  # same content, diff order
    h3 = _content_hash({"a": 1, "b": 3})
    assert h1 == h2
    assert h1 != h3


def test_writer_and_reader_share_exact_canonical_hash_contract() -> None:
    import hashlib
    from scripts.archive_obsolete_risks import _content_hash
    from src.core.risk_register_engine import risk_record_content_hash

    assert _content_hash is risk_record_content_hash
    record = {"z": ["é", "a"], "a": {"b": 1}, "date": Path("synthetic")}
    expected = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    assert _content_hash(record) == expected
    assert _content_hash(record) == _content_hash(dict(reversed(list(record.items()))))
    assert _content_hash(record) != _content_hash(record | {"z": ["a", "é"]})


def test_actual_archive_history_and_rollback_preserve_hashes_and_current_bytes(tmp_path: Path) -> None:
    from src.core.risk_register_engine import load_archived_risk_history

    _write_register(tmp_path / "samplealpha", [_risk("r1", kind="candidate", status="closed")])
    manifest = execute_archive("samplealpha", programs_root=tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    rows = load_archived_risk_history("samplealpha", programs_root=tmp_path)
    assert rows[0].content_hash == manifest.archived[0].content_hash
    assert rows[0].record.id == "r1"
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    rollback_archive("samplealpha", tmp_path / "samplealpha" / manifest.archive_path.replace("archived_risks.yaml", "rollback_manifest.json"), programs_root=tmp_path)
    assert load_archived_risk_history("samplealpha", programs_root=tmp_path) == rows


def test_main_dry_run_reports(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from scripts.archive_obsolete_risks import main

    _write_register(
        tmp_path / "samplealpha",
        [_risk("r1", kind="candidate", status="closed", title="Cleanup row")],
    )
    rc = main(["--program", "samplealpha", "--programs-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Rows to archive: 1" in out
    # Register unchanged.
    reg = yaml.safe_load((tmp_path / "samplealpha" / "risk_register.yaml").read_text())
    assert len(reg["risks"]) == 1
