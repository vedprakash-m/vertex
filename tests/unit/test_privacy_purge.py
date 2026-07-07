"""GAP-31: Unified privacy purge / retention execution."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.privacy_matrix import (
    RETENTION_DAYS,
    SIDECAR_RETENTION,
    DataClassification,
    RetentionClass,
    SidecarRetentionRule,
)
from src.core.privacy_purge import (
    PurgeRecord,
    PurgeReport,
    _parse_iso,
    _row_contains_pii,
    _row_timestamp,
    _tombstone_row,
    run_purge,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _now() -> datetime:
    return datetime(2026, 6, 17, tzinfo=timezone.utc)


def test_parse_iso_handles_z_suffix() -> None:
    parsed = _parse_iso("2026-01-01T00:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026


def test_parse_iso_handles_naive() -> None:
    parsed = _parse_iso("2026-01-01T00:00:00")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


def test_parse_iso_returns_none_for_garbage() -> None:
    assert _parse_iso(None) is None
    assert _parse_iso("") is None
    assert _parse_iso("not a date") is None


def test_row_timestamp_finds_known_fields() -> None:
    row = {"created_at": "2026-01-01T00:00:00Z"}
    ts = _row_timestamp(row)
    assert ts is not None
    assert ts.year == 2026


def test_row_timestamp_returns_none_when_missing() -> None:
    assert _row_timestamp({"id": "x"}) is None


def test_row_contains_pii_detects_known_markers() -> None:
    assert _row_contains_pii({"email": "a@b.com"}) is True
    assert _row_contains_pii({"person": "alice"}) is True
    assert _row_contains_pii({"recipient_emails": ["a@b.com"]}) is True
    assert _row_contains_pii({"id": "x"}) is False


def test_tombstone_row_redacts_pii_fields() -> None:
    redacted = _tombstone_row(
        {
            "id": "1",
            "email": "a@b.com",
            "body": "secret",
            "person": "alice",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    assert redacted["[EXCISED]"] is True
    assert "email" not in redacted
    assert "body" not in redacted
    assert "person" not in redacted
    assert redacted["id"] == "1"
    assert redacted["created_at"] == "2026-01-01T00:00:00Z"


def test_run_purge_skips_indefinite_retention(tmp_path: Path) -> None:
    """INDEFINITE retention → skip, never purge."""
    now = _now()
    rule = SidecarRetentionRule(
        artifact_path="journal/signals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.INDEFINITE,
        supports_excise=False,
    )
    report = run_purge(
        "acme",
        programs_root=tmp_path,
        now=now,
        apply=True,
        rules=(rule,),
    )
    # INDEFINITE rules are skipped, so no records are produced for them.
    assert report.records == ()
    assert report.skipped == ("journal/signals.jsonl=indefinite",)


def test_run_purge_dry_run_does_not_modify_files(tmp_path: Path) -> None:
    """Dry-run reports counts but does not write."""
    now = _now()
    sidecar = tmp_path / "acme" / "journal" / "signals.jsonl"
    old_rows = [
        {"id": "1", "created_at": "2020-01-01T00:00:00Z"},
        {"id": "2", "created_at": "2026-06-01T00:00:00Z"},  # recent, keep
    ]
    _write_jsonl(sidecar, old_rows)
    original = sidecar.read_bytes()

    rule = SidecarRetentionRule(
        artifact_path="journal/signals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    )
    report = run_purge(
        "acme",
        programs_root=tmp_path,
        now=now,
        apply=False,
        rules=(rule,),
    )
    assert report.dry_run is True
    assert report.total_rows_purged == 1
    assert sidecar.read_bytes() == original  # unchanged


def test_run_purge_apply_removes_expired_rows(tmp_path: Path) -> None:
    """Apply mode rewrites the sidecar to drop expired rows."""
    now = _now()
    sidecar = tmp_path / "acme" / "journal" / "signals.jsonl"
    old_rows = [
        {"id": "1", "created_at": "2020-01-01T00:00:00Z"},  # expired (5y old)
        {"id": "2", "created_at": "2026-06-01T00:00:00Z"},  # keep
        {"id": "3", "created_at": "2026-05-01T00:00:00Z"},  # keep
    ]
    _write_jsonl(sidecar, old_rows)

    rule = SidecarRetentionRule(
        artifact_path="journal/signals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    )
    report = run_purge(
        "acme",
        programs_root=tmp_path,
        now=now,
        apply=True,
        rules=(rule,),
    )
    assert report.dry_run is False
    assert report.total_rows_purged == 1
    remaining = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
    assert [r["id"] for r in remaining] == ["2", "3"]


def test_run_purge_tombstones_pii_rows(tmp_path: Path) -> None:
    """PII rows get tombstoned (not deleted) when supports_excise=True."""
    now = _now()
    sidecar = tmp_path / "acme" / "journal" / "autonomy_audit.jsonl"
    # 2010 is 16 years before 2026 — well past SEVEN_YEARS retention
    pii_rows = [
        {"id": "1", "created_at": "2010-01-01T00:00:00Z", "email": "a@b.com"},
        {"id": "2", "created_at": "2010-01-01T00:00:00Z", "note": "no PII"},
    ]
    _write_jsonl(sidecar, pii_rows)

    rule = SidecarRetentionRule(
        artifact_path="journal/autonomy_audit.jsonl",
        classification=DataClassification.PII,
        retention=RetentionClass.SEVEN_YEARS,
        supports_excise=True,
    )
    report = run_purge(
        "acme",
        programs_root=tmp_path,
        now=now,
        apply=True,
        rules=(rule,),
    )
    assert report.total_rows_tombstoned == 1
    assert report.total_rows_purged == 1  # non-PII row purged
    remaining = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
    assert len(remaining) == 1  # only the tombstoned PII row remains
    assert remaining[0]["[EXCISED]"] is True
    assert "email" not in remaining[0]


def test_run_purge_idempotent(tmp_path: Path) -> None:
    """Running apply twice yields the same end state."""
    now = _now()
    sidecar = tmp_path / "acme" / "journal" / "signals.jsonl"
    _write_jsonl(
        sidecar,
        [
            {"id": "1", "created_at": "2020-01-01T00:00:00Z"},
            {"id": "2", "created_at": "2026-06-01T00:00:00Z"},
        ],
    )
    rule = SidecarRetentionRule(
        artifact_path="journal/signals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    )
    first = run_purge("acme", programs_root=tmp_path, now=now, apply=True, rules=(rule,))
    second = run_purge("acme", programs_root=tmp_path, now=now, apply=True, rules=(rule,))
    assert first.total_rows_purged == 1
    assert second.total_rows_purged == 0
    remaining = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
    assert [r["id"] for r in remaining] == ["2"]


def test_run_purge_handles_missing_sidecar(tmp_path: Path) -> None:
    """Missing sidecar is a no-op (0 rows examined, 0 purged)."""
    now = _now()
    rule = SidecarRetentionRule(
        artifact_path="journal/signals.jsonl",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=False,
    )
    report = run_purge("acme", programs_root=tmp_path, now=now, apply=True, rules=(rule,))
    assert len(report.records) == 1
    assert report.records[0].rows_examined == 0


def test_run_purge_handles_unresolvable_artifact_path(tmp_path: Path) -> None:
    """Rules with unresolvable placeholders (e.g. <edition>) are skipped."""
    now = _now()
    rule = SidecarRetentionRule(
        artifact_path="archive/<edition>/snapshots/issue_NNN.snapshot.json",
        classification=DataClassification.CONFIDENTIAL,
        retention=RetentionClass.ONE_YEAR,
        supports_excise=True,
    )
    report = run_purge("acme", programs_root=tmp_path, now=now, apply=True, rules=(rule,))
    assert any("unresolved" in s for s in report.skipped)


def test_run_purge_uses_all_sidcar_rules_by_default(tmp_path: Path) -> None:
    """Default rules=SIDECAR_RETENTION covers the canonical set."""
    now = _now()
    report = run_purge("acme", programs_root=tmp_path, now=now, apply=True)
    # Indefinite rules → skipped; JSONL-with-<edition> → skipped
    # but the report should still be created with skip markers
    assert isinstance(report, PurgeReport)
    assert report.dry_run is False
    # At least 1 skipped (indefinite) + 1 skipped (unresolved edition) markers expected
    assert len(report.skipped) >= 1


def test_purge_report_serialization(tmp_path: Path) -> None:
    """PurgeReport.to_dict() returns audit-friendly JSON-serializable shape."""
    now = _now()
    report = run_purge("acme", programs_root=tmp_path, now=now, apply=False)
    payload = report.to_dict()
    assert payload["program_id"] == "acme"
    assert payload["dry_run"] is True
    assert "records" in payload
    assert "totals" in payload
    # Round-trip through json.dumps to confirm serializability
    import json as _json

    _json.dumps(payload, default=str)
