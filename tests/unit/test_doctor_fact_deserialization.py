"""Track L regression tests (specs/fix-data-flow.md §6.12 / PR-13).

Covers:
1. `run_fact_deserialization_doctor` — a `vertex doctor --fact-deserialization`
   check confirming persisted facts still deserialize against the *current*
   schema, referencing `admin_fact_store_migrate.py` as the remediation path.
2. `check_fact_store_schema_has_lineage_columns` — the schema-precondition
   check that would have caught PS-14's stray 23-column pre-lineage database
   automatically, proven here against a synthetic fixture reproducing that
   exact condition.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.commands.doctor_checks.fact_store_flip_checks import (
    REQUIRED_LINEAGE_COLUMNS,
    check_fact_store_schema_has_lineage_columns,
    run_fact_deserialization_doctor,
)
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.reality_store import get_program_reality_db_path

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# run_fact_deserialization_doctor
# ---------------------------------------------------------------------------


def test_fact_deserialization_doctor_is_ok_for_empty_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    report = run_fact_deserialization_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.label == "Fact Deserialization"
    assert check.status == "ok"


def test_fact_deserialization_doctor_is_ok_for_valid_risk_fact(tmp_path: Path) -> None:
    """Minimal input: a well-formed, real risk.entry fact must deserialize
    cleanly — this is the non-regression baseline the failure case (below)
    is contrasted against."""
    programs_root = tmp_path / "programs"
    db_root = programs_root.parent
    store = ProgramFactStore("acme", db_root=db_root)
    store.append_fact(
        ProgramFactInput(
            fact_type="risk.entry",
            entity_refs=("risk:r1",),
            payload={
                "id": "risk:r1",
                "program_id": "acme",
                "title": "Vendor delay",
                "description": "Vendor is behind schedule on the integration milestone.",
                "probability": "possible",
                "impact": "medium",
                "category": "external",
                "status": "open",
                "owner_alias": "tpm",
                "identified_date": "2026-06-01",
                "mitigation_plan": "Escalate to vendor management.",
                "linked_workstream_ids": [],
                "linked_milestone_ids": [],
                "linked_claim_ids": [],
            },
            confidence="operator_confirmed",
            created_by="test",
            write_authority="human",
        ),
        recorded_at=NOW,
    )

    report = run_fact_deserialization_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.status == "ok"
    assert "risk.entry" in check.metadata["checked_fact_types"]


def test_fact_deserialization_doctor_fails_loudly_on_malformed_payload(tmp_path: Path) -> None:
    """Minimal failing input: a persisted risk.entry fact whose payload is
    missing required fields (e.g. a schema evolved to require a field an
    old, un-migrated row doesn't have) — the exact class of gap this check
    exists to catch (a future schema evolution silently breaking every
    ProgramReality.risks() call, per §6.12's Design)."""
    programs_root = tmp_path / "programs"
    db_root = programs_root.parent
    store = ProgramFactStore("acme", db_root=db_root)
    store.append_fact(
        ProgramFactInput(
            fact_type="risk.entry",
            entity_refs=("risk:bad1",),
            payload={"id": "risk:bad1", "program_id": "acme"},  # missing required fields
            confidence="operator_confirmed",
            created_by="test",
            write_authority="human",
        ),
        recorded_at=NOW,
    )

    report = run_fact_deserialization_doctor(
        edition_name="acme_weekly", program_id="acme", programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.status == "fail"
    assert "risk.entry" in check.metadata["failed_fact_types"]
    assert "migrate-legacy-state" in check.detail


# ---------------------------------------------------------------------------
# check_fact_store_schema_has_lineage_columns — the schema-precondition check
# ---------------------------------------------------------------------------


def test_schema_check_reports_all_missing_when_db_absent(tmp_path: Path) -> None:
    missing = check_fact_store_schema_has_lineage_columns(tmp_path / "does-not-exist.sqlite3")
    assert missing == REQUIRED_LINEAGE_COLUMNS


def test_schema_check_detects_synthetic_pre_lineage_database() -> None:
    """Reproduces PS-14's exact stray-database symptom: a `program_fact_revisions`
    table that exists but predates the S-3 lineage-column migration (the
    23-column schema `~/.vertex/xpf/vertex.sqlite3` had). This is precisely
    the schema-precondition contract test §6.12 requires — proving the check
    would have caught PS-14's stray database automatically."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "vertex.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            # Minimal pre-lineage schema: no domain_event_id/candidate_id/
            # source_document_key/approval_event_id columns at all.
            connection.execute(
                """
                CREATE TABLE program_fact_revisions (
                    revision_id TEXT PRIMARY KEY,
                    fact_id TEXT NOT NULL,
                    program_id TEXT NOT NULL,
                    natural_key TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    review_state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        missing = check_fact_store_schema_has_lineage_columns(db_path)
        assert set(missing) == set(REQUIRED_LINEAGE_COLUMNS)


def test_schema_check_passes_for_current_lineage_capable_schema(tmp_path: Path) -> None:
    """The current, live schema (as created by `ProgramFactStore`'s own
    `_initialize_schema`) is fully lineage-capable — this is the
    non-regression counterpart to the synthetic pre-lineage fixture above."""
    programs_root = tmp_path / "programs"
    db_root = programs_root.parent
    # Constructing a ProgramFactStore and touching it initializes the schema
    # (including all idempotent lineage-column migrations) even with zero facts.
    store = ProgramFactStore("acme", db_root=db_root)
    store.snapshot()

    db_path = get_program_reality_db_path("acme", db_root=db_root)
    missing = check_fact_store_schema_has_lineage_columns(db_path)
    assert missing == ()
