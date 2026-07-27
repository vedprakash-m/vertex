from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sqlite3

from src.ai.cost_guard import CostGuard
from src.commands.doctor_checks.models import directory_size, format_bytes
from src.commands.doctor_checks.storage_checks import (
    _ai_proposal_queue_check,
    _armada_leakage_hygiene_check,
    _cost_ledger_storage_check,
    _dc01_root_cleanliness_check,
    _dc02_runtime_layout_check,
    _dc03_docs_directory_check,
    _edition_workspace_layout_check,
    _fact_store_authority_check,
    _gather_completeness_oracle_check,
    _program_sqlite_storage_check,
    _rev_extraction_precision_regression_check,
    _sidecar_health_check,
    _sqlite_storage_check,
)
from src.commands.gather_pipeline.lifecycle_policy import GatherRuntimePolicy
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    QueryResultEntry,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
    ORACLE_RESULT_OPERATOR_EXPORT_MATCH,
)
from src.ai.edit_learner import get_edit_patterns_path
from src.core.action_tracker import get_actions_path
from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id, get_ai_proposals_path
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, WorkstreamSynthesis
from src.core.claim_tracker import get_claims_checksum_path, get_claims_path
from src.core.risk_register_engine import get_risk_updates_path
from src.core.fact_sor_state import save_fact_sor_state
from src.core.models import RiskLevel
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory import append_trajectory_point, get_trajectory_checksum_path, get_trajectory_path


def test_doctor_storage_shared_size_helpers(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "one.txt").write_text("abcd", encoding="utf-8")
    (nested / "two.txt").write_text("xy", encoding="utf-8")

    assert directory_size(tmp_path) == 6
    assert directory_size(tmp_path / "missing") == 0
    assert format_bytes(0) == "0B"
    assert format_bytes(1023) == "1023B"
    assert format_bytes(1536) == "1.5KB"
    assert format_bytes(1024 * 1024) == "1.0MB"


def test_program_sqlite_storage_check_missing_db_is_ok_without_confirmed_issues(tmp_path: Path) -> None:
    result = _program_sqlite_storage_check(
        "demo",
        storage_backend="sqlite",
        confirmed_issue_count=0,
        programs_root=tmp_path,
    )

    assert result.label == "Program SQLite"
    assert result.status == "ok"
    assert "storage_backend=sqlite" in result.detail


def test_sqlite_storage_check_warns_when_journal_mode_is_not_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "demo.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
        connection.commit()

    result = _sqlite_storage_check(
        "Program SQLite",
        db_path,
        expected_location=None,
        prefix="storage_backend=sqlite; ",
    )

    assert result.status == "warn"
    assert "journal_mode=delete" in result.detail


def test_sqlite_storage_check_warns_when_path_is_outside_expected_root(tmp_path: Path) -> None:
    db_path = tmp_path / "custom-root" / "demo.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
        connection.commit()

    result = _sqlite_storage_check(
        "Reality DB",
        db_path,
        expected_location=tmp_path / ".vertex",
        prefix="Program fact-store DB. ",
    )

    assert result.status == "warn"
    assert "outside" in result.detail


def test_cost_ledger_storage_check_reports_dual_written_state(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    CostGuard(edition="acme_weekly", run_id="run-001", budget_usd=0.5, programs_root=programs_root).record_actual(0.2)

    result = _cost_ledger_storage_check("acme_weekly", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["cost_ledger_dual_written"] is True
    assert "ledger=present, projection=present" in result.detail


def test_cost_ledger_storage_check_warns_when_projection_is_legacy_only(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    projection_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "cost_guard.json"
    projection_path.parent.mkdir(parents=True, exist_ok=True)
    projection_path.write_text('{"edition": "acme_weekly", "runs": {}}', encoding="utf-8")

    result = _cost_ledger_storage_check("acme_weekly", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["cost_ledger_dual_written"] is False
    assert "ledger=missing, projection=present" in result.detail


def test_sidecar_health_check_warns_when_claim_log_was_quarantined(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("demo", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"demo","edition_id":"demo_weekly","issue_number":1,'
        '"workstream_id":"ws","text":"Ship by June 15","entity_refs":[],"claim_date":"2026-05-10",'
        '"owner_alias":"owner","due_date":null,"status":"open","contradiction_status":"none",'
        '"source_confidence_tier":"grounded","last_validated_date":null}\nnot-json\n',
        encoding="utf-8",
    )

    # Trigger quarantine through the owner reader before doctor reports it.
    from src.core.claim_tracker import read_claim_log

    read_claim_log("demo", programs_root=programs_root)

    result = _sidecar_health_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["claim_quarantine_count"] == 1
    assert "quarantined claims log file" in result.detail


def test_sidecar_health_check_warns_when_claim_checksum_is_missing_or_mismatched(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("demo", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(
        '{"record_type":"claim","id":"claim-1","program_id":"demo","edition_id":"demo_weekly","issue_number":1,'
        '"workstream_id":"ws","text":"Ship by June 15","entity_refs":[],"claim_date":"2026-05-10",'
        '"owner_alias":"owner","due_date":null,"status":"open","contradiction_status":"none",'
        '"source_confidence_tier":"grounded","last_validated_date":null}\n',
        encoding="utf-8",
    )
    get_claims_checksum_path("demo", programs_root).write_text("deadbeef\n", encoding="utf-8")

    result = _sidecar_health_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["claims_checksum_ok"] is False
    assert "checksum is missing or mismatched" in result.detail


def test_sidecar_health_check_warns_when_trajectory_checksum_is_missing_or_mismatched(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_trajectory_point(
        "demo",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="owner@example.com",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            area_path="Area\\Path",
            tags=(),
            risk_assessment=None,
            risk_assessment_comment=None,
        ),
        programs_root=programs_root,
    )
    get_trajectory_checksum_path("demo", 1001, programs_root).write_text("deadbeef\n", encoding="utf-8")

    result = _sidecar_health_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["trajectory_checksum_failures"] == (get_trajectory_path("demo", 1001, programs_root).name,)
    assert "trajectory checksum file" in result.detail


def test_sidecar_health_check_warns_when_actions_quarantined(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    actions_path = get_actions_path("demo", programs_root)
    actions_path.parent.mkdir(parents=True, exist_ok=True)
    actions_path.write_text('not-json\n', encoding="utf-8")
    from src.core.action_tracker import load_actions
    load_actions("demo", programs_root=programs_root)

    result = _sidecar_health_check("demo", programs_root=programs_root)
    assert result.status == "warn"
    assert "actions" in result.detail


def test_sidecar_health_check_warns_when_ai_proposals_checksum_mismatched(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_ai_proposals_path("demo", programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text('{"x":1}\n', encoding="utf-8")
    get_ai_proposals_path("demo", programs_root).with_suffix(".sha256").write_text("deadbeef\n", encoding="utf-8")

    result = _sidecar_health_check("demo", programs_root=programs_root)
    assert result.status == "warn"
    assert "ai_proposals" in str(result.metadata.get("extended_checksum_failures", ()))


def test_sidecar_health_check_warns_when_edit_patterns_quarantined(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    patterns_path = get_edit_patterns_path("demo", programs_root)
    patterns_path.parent.mkdir(parents=True, exist_ok=True)
    patterns_path.write_text('bad-line\n', encoding="utf-8")
    from src.ai.edit_learner import read_edit_patterns
    read_edit_patterns("demo", programs_root=programs_root)

    result = _sidecar_health_check("demo", programs_root=programs_root)
    assert result.status == "warn"
    assert "edit_patterns" in result.detail


def test_sidecar_health_check_warns_when_risk_updates_checksum_mismatched(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    risk_path = get_risk_updates_path("demo", programs_root)
    risk_path.parent.mkdir(parents=True, exist_ok=True)
    risk_path.write_text('{"x":1}\n', encoding="utf-8")
    get_risk_updates_path("demo", programs_root).with_suffix(".sha256").write_text("deadbeef\n", encoding="utf-8")

    result = _sidecar_health_check("demo", programs_root=programs_root)
    assert result.status == "warn"
    assert "risk_updates" in str(result.metadata.get("extended_checksum_failures", ()))


def test_fact_store_authority_check_warns_in_legacy_mode(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    claims_path = get_claims_path("demo", programs_root)
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("VERTEX_FACT_SOR", "legacy")

    result = _fact_store_authority_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["fact_store_authority"] == "legacy"
    assert result.metadata["shadow_write_retention"] == "enabled"


def _reset_output_subdir_cache(monkeypatch) -> None:
    """Ensure ``_output_subdir()`` resolves to the canonical default regardless of
    process-wide cache or env state, so the PO-01 layout tests are deterministic."""
    import src.core.edition_resolver as er

    monkeypatch.setattr(er, "_output_subdir_cached", None)
    monkeypatch.delenv("VERTEX_OUTPUT_SUBDIR", raising=False)


def test_po01_split_brain_is_a_failure_not_a_silent_error(tmp_path: Path, monkeypatch) -> None:
    """DC-02: a split-brain edition layout (both output/ and publications/) is a
    blocking state and must surface as ``status="fail"`` so DoctorReport.failures
    counts it and overall goes UNHEALTHY — not the old silent ``status="error"``."""
    _reset_output_subdir_cache(monkeypatch)
    program_dir = tmp_path / "nova"
    program_dir.mkdir()
    (program_dir / "publications").mkdir()  # canonical
    (program_dir / "output").mkdir()  # legacy

    result = _edition_workspace_layout_check("nova", programs_root=tmp_path)

    assert result.status == "fail"
    assert result.metadata["state"] == "split_brain"


def test_po01_marker_disk_mismatch_is_a_failure(tmp_path: Path, monkeypatch) -> None:
    """DC-02: marker declares canonical layout but only the legacy dir exists —
    blocking mismatch, must be ``status="fail"`` (not silent ``status="error"``)."""
    import json

    _reset_output_subdir_cache(monkeypatch)
    program_dir = tmp_path / "nova"
    program_dir.mkdir()
    (program_dir / "output").mkdir()  # legacy only
    (program_dir / ".edition_layout.json").write_text(
        json.dumps({"edition_workspace_layout": "publications"}), encoding="utf-8"
    )

    result = _edition_workspace_layout_check("nova", programs_root=tmp_path)

    assert result.status == "fail"
    assert result.metadata["state"] == "mismatch"


def test_fact_store_authority_check_reports_authoritative_primary_mode(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "demo" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text("schema_version: \"1.0\"\nworkstreams: []\n", encoding="utf-8")
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")

    result = _fact_store_authority_check("demo", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["fact_store_authority"] == "authoritative"
    assert result.metadata["shadow_write_retention"] == "enabled"
    assert "fact_store_authority=authoritative" in result.detail


def test_fact_store_authority_check_uses_persisted_primary_mode_when_env_is_unset(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "demo" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text("schema_version: \"1.0\"\nworkstreams: []\n", encoding="utf-8")
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    save_fact_sor_state(
        "demo",
        mode="primary",
        recorded_at=datetime(2026, 6, 7, 9, 0),
        recorded_by="operator",
        programs_root=programs_root,
    )

    result = _fact_store_authority_check("demo", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["fact_store_authority"] == "authoritative"
    assert result.metadata["sor_mode"] == "primary"


def _make_gather_run_manifest(*, query_results: tuple[QueryResultEntry, ...]) -> GatherRunManifest:
    now = datetime(2026, 7, 21, 12, 0, 0)
    return GatherRunManifest(
        run_id="gather-01ORACLE",
        status=GatherRunStatus.RUNNING,
        program_id="demo",
        actor_identity_type="interactive",
        lease_owner="host-a",
        lease_fencing_token=1,
        started_at=now,
        scope_as_of=now,
        required_scope_status=RequiredScopeStatus.FULL,
        query_results=query_results,
    )


def _query_result(scope_id: str, *, oracle_result: str | None) -> QueryResultEntry:
    return QueryResultEntry(
        query_id=f"q-{scope_id}",
        scope_id=scope_id,
        wiql_hash="h1",
        captured_at=datetime(2026, 7, 21, 12, 0, 0),
        raw_count=10,
        membership_ids=(),
        membership_hash="mh1",
        cap_reached=False,
        completeness_state="FULL",
        oracle_result=oracle_result,
    )


def test_gather_completeness_oracle_check_ok_when_manifest_mode_is_off(tmp_path: Path, monkeypatch) -> None:
    import src.commands.doctor_checks.storage_checks as storage_checks

    monkeypatch.setattr(
        storage_checks, "load_gather_runtime_policy", lambda *_a, **_k: GatherRuntimePolicy(run_manifest_mode="off")
    )

    result = _gather_completeness_oracle_check("demo", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["run_manifest_mode"] == "off"


def test_gather_completeness_oracle_check_warns_when_no_committed_run_exists(tmp_path: Path) -> None:
    result = _gather_completeness_oracle_check("demo", programs_root=tmp_path)

    assert result.status == "warn"
    assert result.metadata["committed_run"] is None


def test_gather_completeness_oracle_check_warns_on_weak_same_endpoint_rerun_scopes(tmp_path: Path) -> None:
    manifest = _make_gather_run_manifest(
        query_results=(_query_result("scope-a", oracle_result=None),)
    )
    create_staging_manifest(manifest, programs_root=tmp_path)
    commit_staging_run(manifest, finished_at=datetime(2026, 7, 21, 12, 5, 0), programs_root=tmp_path)

    result = _gather_completeness_oracle_check("demo", programs_root=tmp_path)

    assert result.status == "warn"
    assert result.metadata["same_endpoint_rerun_scopes"] == ["scope-a"]


def test_gather_completeness_oracle_check_ok_when_all_scopes_reconciled(tmp_path: Path) -> None:
    manifest = _make_gather_run_manifest(
        query_results=(_query_result("scope-a", oracle_result=ORACLE_RESULT_OPERATOR_EXPORT_MATCH),)
    )
    create_staging_manifest(manifest, programs_root=tmp_path)
    commit_staging_run(manifest, finished_at=datetime(2026, 7, 21, 12, 5, 0), programs_root=tmp_path)

    result = _gather_completeness_oracle_check("demo", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["run_id"] == "gather-01ORACLE"


def test_gather_completeness_oracle_check_warns_on_operator_export_mismatch(tmp_path: Path) -> None:
    manifest = _make_gather_run_manifest(
        query_results=(
            _query_result("scope-a", oracle_result="operator_source_export:mismatch:reported=8:observed=10"),
        )
    )
    create_staging_manifest(manifest, programs_root=tmp_path)
    commit_staging_run(manifest, finished_at=datetime(2026, 7, 21, 12, 5, 0), programs_root=tmp_path)

    result = _gather_completeness_oracle_check("demo", programs_root=tmp_path)

    assert result.status == "warn"
    assert result.metadata["mismatched_scopes"] == ["scope-a"]


def _write_quality_metrics(programs_root: Path, program_id: str, g_xtract_prec: float) -> None:
    import json as _json

    qdir = programs_root / program_id / "_quality"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "rev_quality_metrics.json").write_text(
        _json.dumps({"program_id": program_id, "g_xtract_prec": g_xtract_prec}), encoding="utf-8"
    )


def test_rev_extraction_precision_check_ok_when_no_metrics_published(tmp_path: Path) -> None:
    result = _rev_extraction_precision_regression_check("xpf", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["metrics_present"] is False


def test_rev_extraction_precision_check_ok_at_baseline(tmp_path: Path) -> None:
    _write_quality_metrics(tmp_path, "xpf", 0.8667)

    result = _rev_extraction_precision_regression_check("xpf", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["g_xtract_prec"] == 0.8667


def test_rev_extraction_precision_check_ok_just_above_floor(tmp_path: Path) -> None:
    _write_quality_metrics(tmp_path, "xpf", 0.8167)  # exactly at the floor -- not below it

    result = _rev_extraction_precision_regression_check("xpf", programs_root=tmp_path)

    assert result.status == "ok"


def test_rev_extraction_precision_check_warns_below_floor(tmp_path: Path) -> None:
    _write_quality_metrics(tmp_path, "xpf", 0.75)

    result = _rev_extraction_precision_regression_check("xpf", programs_root=tmp_path)

    assert result.status == "warn"
    assert result.metadata["g_xtract_prec"] == 0.75
    assert result.metadata["floor"] == 0.8167


def test_rev_extraction_precision_check_ok_when_field_missing(tmp_path: Path) -> None:
    import json as _json

    qdir = tmp_path / "xpf" / "_quality"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "rev_quality_metrics.json").write_text(_json.dumps({"program_id": "xpf"}), encoding="utf-8")

    result = _rev_extraction_precision_regression_check("xpf", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["metrics_present"] is True


def test_rev_extraction_precision_check_warns_on_unparseable_json(tmp_path: Path) -> None:
    qdir = tmp_path / "xpf" / "_quality"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "rev_quality_metrics.json").write_text("{not valid json", encoding="utf-8")

    result = _rev_extraction_precision_regression_check("xpf", programs_root=tmp_path)

    assert result.status == "warn"
    assert "parse_error" in result.metadata



# ---------------------------------------------------------------------------
# Declutter doctor checks DC-01 / DC-02 / DC-03 (specs/declutter.md §7)
# ---------------------------------------------------------------------------


def _seed_minimal_program(programs_root: Path, program_id: str = "demo") -> Path:
    """Seed a program dir with just enough recognized root entries to be 'clean'."""
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text("schema_version: '1.0'\nworkstreams: []\n", encoding="utf-8")
    (program_dir / "decisions.yaml").write_text("[]\n", encoding="utf-8")
    return program_dir


def test_dc01_reports_ok_when_root_is_clean(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root)

    result = _dc01_root_cleanliness_check("demo", programs_root=programs_root)

    assert result.label == "DC-01 Root Cleanliness"
    assert result.status == "ok"
    assert result.metadata["detail"] == "clean"


def test_dc01_warns_on_stale_bak_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    (program_dir / "program.yaml.bak").write_text("old", encoding="utf-8")
    (program_dir / "workstreams.yaml.cp1252bak").write_text("old", encoding="utf-8")

    result = _dc01_root_cleanliness_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["detail"] == "stale_backup_lock"
    assert "program.yaml.bak" in result.metadata["files"]
    assert "workstreams.yaml.cp1252bak" in result.metadata["files"]


def test_dc01_warns_on_unrecognized_root_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    (program_dir / "mystery_unknown.yaml").write_text("x", encoding="utf-8")

    result = _dc01_root_cleanliness_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["detail"] == "unrecognized"
    assert "mystery_unknown.yaml" in result.metadata["entries"]


def test_dc01_info_when_spike_exceeds_50_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    spike = program_dir / "_spike"
    spike.mkdir()
    for i in range(60):
        (spike / f"note_{i}.md").write_text("x", encoding="utf-8")

    result = _dc01_root_cleanliness_check("demo", programs_root=programs_root)

    assert result.status == "info"
    assert result.metadata["detail"] == "spike_prune"
    assert result.metadata["spike_count"] == 60


def test_dc01_runtime_artifact_at_root_is_not_unrecognized(tmp_path: Path) -> None:
    """A runtime-artifact legacy file at root is whitelisted during transition
    (it is in RUNTIME_FILENAMES via ROOT_WHITELIST), so DC-01-c must NOT flag it
    as unrecognized pre-migration."""
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")

    result = _dc01_root_cleanliness_check("demo", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["detail"] == "clean"


def test_dc02_pre_migration_reports_info_not_blocking(tmp_path: Path) -> None:
    """Pre-Phase-1-B: runtime files at root, runtime/ absent. This is the honest
    transition state and MUST be non-blocking (info), never fail — otherwise the
    flip would be impossible to land."""
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")
    (program_dir / "run_telemetry.jsonl").write_text('{"x":1}\n', encoding="utf-8")

    result = _dc02_runtime_layout_check("demo", programs_root=programs_root)

    assert result.status == "info"
    assert result.metadata["detail"] == "pre_migration"
    assert "gather_state" in result.metadata["at_root"]
    assert result.metadata["at_runtime"] == []
    assert result.metadata["both"] == []


def test_dc02_clean_when_all_artifacts_under_runtime(tmp_path: Path) -> None:
    from tests.support.runtime_paths import seed_canonical_runtime_artifact

    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root)
    seed_canonical_runtime_artifact(programs_root, "demo", "gather_state", content="{}")
    seed_canonical_runtime_artifact(programs_root, "demo", "run_telemetry", content=b"x")

    result = _dc02_runtime_layout_check("demo", programs_root=programs_root)

    # Only 2 of 7 artifacts are at runtime; the rest are missing -> partial,
    # not clean. Assert partial here and clean separately below.
    assert result.status == "info"
    assert result.metadata["detail"] == "partial"


def test_dc02_clean_when_every_artifact_canonical_and_none_at_root(tmp_path: Path) -> None:
    from tests.support.runtime_paths import seed_canonical_runtime_artifact
    from src.core.program_paths import RUNTIME_ARTIFACTS

    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root)
    for art in RUNTIME_ARTIFACTS:
        seed_canonical_runtime_artifact(programs_root, "demo", art.name, content=b"x")

    result = _dc02_runtime_layout_check("demo", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["detail"] == "clean"
    assert result.metadata["at_root"] == []
    assert len(result.metadata["at_runtime"]) == len(RUNTIME_ARTIFACTS)


def test_dc02_split_brain_warns_non_strict_and_fails_strict(tmp_path: Path) -> None:
    from tests.support.runtime_paths import seed_split_brain_runtime_artifact

    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root)
    seed_split_brain_runtime_artifact(programs_root, "demo", "gather_state", content="{}")

    non_strict = _dc02_runtime_layout_check("demo", programs_root=programs_root, strict=False)
    assert non_strict.status == "warn"
    assert non_strict.metadata["detail"] == "stale"
    assert "gather_state" in non_strict.metadata["both"]

    strict = _dc02_runtime_layout_check("demo", programs_root=programs_root, strict=True)
    assert strict.status == "fail"
    assert strict.metadata["detail"] == "split-brain"


def test_dc02_live_wal_sidecar_is_split_brain_evidence(tmp_path: Path) -> None:
    """A -wal sidecar at root while the canonical DB exists is split-brain
    evidence of an in-flight connection (R-2′), even if the main DB was moved."""
    from tests.support.runtime_paths import seed_canonical_runtime_artifact

    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    seed_canonical_runtime_artifact(programs_root, "demo", "channel_registry", content=b"x")
    # Leftover live -wal at root while canonical DB is in runtime/.
    (program_dir / "channel_registry.sqlite3-wal").write_bytes(b"\x00" * 32)

    result = _dc02_runtime_layout_check("demo", programs_root=programs_root, strict=True)

    assert result.status == "fail"
    assert result.metadata["detail"] == "split-brain"
    assert "channel_registry.sqlite3-wal" in result.metadata["live_sidecars"]


def test_dc02_missing_reports_info_for_fresh_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root)  # no runtime artifacts anywhere

    result = _dc02_runtime_layout_check("demo", programs_root=programs_root)

    assert result.status == "info"
    assert result.metadata["detail"] == "missing"


def test_dc03_ok_when_docs_absent(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_minimal_program(programs_root)

    result = _dc03_docs_directory_check("demo", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["detail"] == "absent"


def test_dc03_ok_when_docs_clean(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    docs = program_dir / "docs"
    docs.mkdir()
    (docs / "onboarding_notes.md").write_text("notes", encoding="utf-8")

    result = _dc03_docs_directory_check("demo", programs_root=programs_root)

    assert result.status == "ok"
    assert result.metadata["detail"] == "clean"


def test_dc03_warns_on_platform_filename_in_docs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_minimal_program(programs_root)
    docs = program_dir / "docs"
    docs.mkdir()
    (docs / "run_telemetry.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (docs / "gather_state_state.yaml").write_text("x", encoding="utf-8")

    result = _dc03_docs_directory_check("demo", programs_root=programs_root)

    assert result.status == "warn"
    assert result.metadata["detail"] == "platform_pattern"
    assert "run_telemetry.jsonl" in result.metadata["files"]


def test_armada_leakage_check_ok_when_no_query_configured(tmp_path: Path, monkeypatch) -> None:
    import src.commands.doctor_checks.storage_checks as storage_checks

    monkeypatch.setattr(storage_checks, "find_leakage_query", lambda *_a, **_k: None)

    result = _armada_leakage_hygiene_check("demo", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["applicable"] is False


def test_armada_leakage_check_ok_when_query_configured_but_never_synced(tmp_path: Path, monkeypatch) -> None:
    import src.commands.doctor_checks.storage_checks as storage_checks

    monkeypatch.setattr(storage_checks, "find_leakage_query", lambda *_a, **_k: object())

    result = _armada_leakage_hygiene_check("armada", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata == {"applicable": True, "synced": False}


def test_armada_leakage_check_ok_with_no_sla_violations(tmp_path: Path, monkeypatch) -> None:
    from datetime import timezone as _timezone

    import src.commands.doctor_checks.storage_checks as storage_checks
    from src.core.armada_leakage import RawAdoCandidate, sync_leakage_candidates

    monkeypatch.setattr(storage_checks, "find_leakage_query", lambda *_a, **_k: object())
    sync_leakage_candidates(
        "armada", org="contoso", project="One",
        raw_candidates=(RawAdoCandidate(1, "Bug", "T1", "Active", "alice"),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path,
        now=datetime(2026, 7, 22, 12, 0, 0, tzinfo=_timezone.utc),
    )

    result = _armada_leakage_hygiene_check("armada", programs_root=tmp_path)

    assert result.status == "ok"
    assert result.metadata["applicable"] is True
    assert result.metadata["synced"] is True
    assert result.metadata["sla_violation_count"] == 0


def test_armada_leakage_check_warns_on_sla_violations(tmp_path: Path, monkeypatch) -> None:
    import src.commands.doctor_checks.storage_checks as storage_checks
    from src.core.armada_leakage import RawAdoCandidate, sync_leakage_candidates

    monkeypatch.setattr(storage_checks, "find_leakage_query", lambda *_a, **_k: object())
    old_now = datetime(2026, 6, 1, 12, 0, 0)
    sync_leakage_candidates(
        "armada", org="contoso", project="One",
        raw_candidates=(RawAdoCandidate(1, "Bug", "T1", "Active", "alice"),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=old_now,
    )

    # The check calls leakage_sla_violations with no explicit `now`, which
    # defaults to the real wall clock -- freeze it at the storage_checks
    # call site so this test doesn't depend on when it happens to run.
    real_violations = storage_checks.leakage_sla_violations

    def _frozen_violations(program_id, *, programs_root, now=None):
        return real_violations(program_id, programs_root=programs_root, now=datetime(2026, 7, 22, 12, 0, 0))

    monkeypatch.setattr(storage_checks, "leakage_sla_violations", _frozen_violations)

    result = _armada_leakage_hygiene_check("armada", programs_root=tmp_path)

    assert result.status == "warn"
    assert result.metadata["sla_violation_count"] > 0
    assert "owner-disposition SLA" in result.detail
