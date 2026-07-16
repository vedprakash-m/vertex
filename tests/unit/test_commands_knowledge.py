from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.backup import create_repository_backup
from src.core.knowledge_candidate_store import KnowledgeCandidateDecisionRecord, KnowledgeCandidateEntityResolution, append_candidate, append_triage_decision, build_candidate, load_triage_decisions
from src.core.knowledge_claim_store import append_claim_revision
from src.core.knowledge.vault import ingest_knowledge_source, load_vault_entry, write_shared_vault_verify_status
from src.core.ledger.candidate_store import load_pending_candidates as load_pending_ledger_candidates
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import KnowledgeDocumentRef, OperatorAssertionRef


runner = CliRunner()


def test_knowledge_assert_writes_claim_revision(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    written_files = tuple(claims_path.glob("claims-*.jsonl"))

    assert result.exit_code == 0
    assert len(written_files) == 1
    rows = written_files[0].read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    assert payload["predicate"] == "first_deployment"
    assert payload["value"] == "2025-H2"


def test_knowledge_show_resolves_claims_for_program_scope_chain(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "id: acme\nknowledge_scopes:\n  - domain:storage-platform\n",
        encoding="utf-8",
    )

    assert_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    show_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "show",
            "--program",
            "acme",
            "--entity",
            "sku_generation:gen9",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(show_result.stdout)

    assert assert_result.exit_code == 0
    assert show_result.exit_code == 0
    assert payload["entry"]["claims"][0]["predicate"] == "first_deployment"
    assert payload["entry"]["claims"][0]["value"] == "2025-H2"


def test_knowledge_assert_rejects_unregistered_predicate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "unknown_predicate",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "Unknown knowledge predicate" in result.output


def test_knowledge_supersede_resolves_claim_id_and_records_reason(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    assert_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    first_payload = json.loads(claims_path.glob("claims-*.jsonl").__iter__().__next__().read_text(encoding="utf-8").splitlines()[0])

    supersede_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "supersede",
            "--claim-id",
            first_payload["claim_id"],
            "--value",
            "2025-Q4",
            "--reason",
            "corrected per fleet data",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    rows = next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()
    second_payload = json.loads(rows[-1])

    assert assert_result.exit_code == 0
    assert supersede_result.exit_code == 0
    assert second_payload["supersedes"] == first_payload["claim_id"]
    assert second_payload["value"] == "2025-Q4"
    assert second_payload["source_ref"]["context"] == "corrected per fleet data"


def test_knowledge_supersede_rejects_unknown_claim_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "supersede",
            "--claim-id",
            "01UNKNOWNCLAIMID00000000000000",
            "--value",
            "2025-Q4",
            "--reason",
            "corrected per fleet data",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "Unknown claim id" in result.output


def test_knowledge_redact_rewrites_claim_and_hides_it_from_show(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "id: acme\nknowledge_scopes:\n  - domain:storage-platform\n",
        encoding="utf-8",
    )

    assert_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    claim_payload = json.loads(next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()[0])

    redact_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "redact",
            "--claim-id",
            claim_payload["claim_id"],
            "--reason",
            "pii cleanup",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    show_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "show",
            "--program",
            "acme",
            "--entity",
            "sku_generation:gen9",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    show_payload = json.loads(show_result.stdout)
    redaction_registry = json.loads((tmp_path / "knowledge" / ".claim-redactions.jsonl").read_text(encoding="utf-8").splitlines()[0])

    assert assert_result.exit_code == 0
    assert redact_result.exit_code == 0
    assert show_result.exit_code == 0
    assert show_payload["entry"]["claims"] == []
    assert redaction_registry["claim_id"] == claim_payload["claim_id"]


def test_knowledge_redact_reports_affected_backups_when_root_provided(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    assert_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    backup_root = tmp_path / "backups" / "snap-001"
    create_repository_backup(backup_root, source_root=tmp_path)
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    claim_payload = json.loads(next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()[0])

    redact_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "redact",
            "--claim-id",
            claim_payload["claim_id"],
            "--reason",
            "pii cleanup",
            "--actor",
            "operator",
            "--backup-root",
            str(tmp_path / "backups"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert assert_result.exit_code == 0
    assert redact_result.exit_code == 0
    assert "Affected backups:" in redact_result.stdout
    assert str(backup_root.resolve()) in redact_result.stdout


def test_knowledge_status_reports_scope_counts_and_vault_metrics(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    assert_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    knowledge_root = tmp_path / "knowledge"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )
    vault_dir = knowledge_root / "vault" / "ab"
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "abc123").write_text("payload", encoding="utf-8")

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert assert_result.exit_code == 0
    assert status_result.exit_code == 0
    assert payload["pending_candidate_count"] == 1
    assert payload["pending_candidates_by_pipeline"] == {"extract": 1}
    assert payload["oldest_pending_candidate_created_at"] is not None
    assert payload["oldest_pending_candidate_age_seconds"] is not None
    assert payload["pending_candidates_missing_created_at_count"] == 0
    assert payload["expired_claim_count"] == 0
    assert payload["expiring_soon_claim_count"] == 0
    assert payload["warning_window_days"] == 30
    assert payload["active_override_count"] == 0
    assert payload["active_override_program_count"] == 0
    assert payload["vault"]["file_count"] == 1
    assert payload["vault"]["missing_meta_count"] == 1
    assert payload["vault"]["hash_mismatch_count"] == 0
    assert payload["vault"]["missing_source_record_count"] == 0
    assert payload["vault"]["missing_claim_ref_count"] == 0
    assert payload["vault"]["missing_candidate_ref_count"] == 0
    assert payload["vault"]["last_deep_verify_at"] is None
    assert payload["vault"]["last_deep_verify_ok"] is None
    assert payload["vault"]["last_deep_verify_age_seconds"] is None
    assert payload["scopes"][0]["scope"] == "domain:storage-platform"
    assert payload["scopes"][0]["active_claims_by_confidence"] == {
        "ai_extracted": 0,
        "inferred": 0,
        "operator_confirmed": 1,
        "source_authoritative": 0,
    }


def test_knowledge_status_reports_missing_source_registry_records(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "gen9.md"
    source_path.write_text("# Gen9\n", encoding="utf-8")
    entry = ingest_knowledge_source(source_path, scope="domain:storage-platform", programs_root=programs_root)
    entry.content_path.unlink()
    entry.metadata_path.unlink()

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["vault"]["file_count"] == 0
    assert payload["vault"]["missing_source_record_count"] == 1


def test_knowledge_status_reports_last_shared_vault_deep_verify(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    verified_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    write_shared_vault_verify_status(
        verified_at=verified_at,
        ok=False,
        issue_records=[{"kind": "missing_source_record", "count": 1}],
        programs_root=programs_root,
        program_id="acme",
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["vault"]["last_deep_verify_at"] is not None
    assert payload["vault"]["last_deep_verify_ok"] is False
    assert payload["vault"]["last_deep_verify_age_seconds"] is not None
    assert payload["vault"]["last_deep_verify_age_seconds"] >= 240


def test_knowledge_status_counts_only_active_pending_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

    append_candidate(
        build_candidate(
            candidate_id="cand-active",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=now,
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            pipeline="extract",
            extraction_confidence=0.9,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-active",
        ),
        programs_root=programs_root,
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-approved",
            scope="domain:storage-platform",
            subject="sku_generation:gen10",
            predicate="first_deployment",
            value="2026-H2",
            valid_from=now,
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            pipeline="ingest",
            extraction_confidence=0.8,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-approved",
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-approved",
            kind="approved",
            decided_at=now,
            triage_actor="operator",
            batch_id="batch-approved",
        ),
        programs_root=programs_root,
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-expired-skip",
            scope="domain:storage-platform",
            subject="sku_generation:gen11",
            predicate="first_deployment",
            value="2027-H1",
            valid_from=now,
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            pipeline="extract",
            extraction_confidence=0.7,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-skip",
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-expired-skip",
            kind="skipped",
            decided_at=now - timedelta(days=91),
            triage_actor="operator",
            batch_id="batch-skip",
        ),
        programs_root=programs_root,
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["pending_candidate_count"] == 1
    assert payload["pending_candidates_by_pipeline"] == {"extract": 1}
    assert payload["oldest_pending_candidate_created_at"] is not None
    assert payload["oldest_pending_candidate_age_seconds"] is not None
    assert payload["pending_candidates_missing_created_at_count"] == 0
    assert payload["triaged_candidate_count"] == 2
    assert payload["latest_triage_session_actor"] == "operator"
    assert payload["latest_triage_session_decision_count"] == 1
    assert payload["latest_triage_session_duration_seconds"] == 0
    assert payload["latest_triage_session_throughput_per_minute"] == 1.0
    assert payload["triage_session_gap_minutes"] == 30


def test_knowledge_status_text_reports_latest_triage_session_metrics(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    decided_at = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

    for candidate_id in ("cand-1", "cand-2"):
        append_candidate(
            build_candidate(
                candidate_id=candidate_id,
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=decided_at,
                valid_until=None,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=decided_at),
                pipeline="extract",
                extraction_confidence=0.9,
                entity_resolution=(),
                corroborating_refs=(),
                batch_id="batch-1",
                created_at=decided_at,
            ),
            programs_root=programs_root,
        )

    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-1",
            kind="approved",
            decided_at=decided_at,
            triage_actor="operator",
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-2",
            kind="rejected",
            decided_at=decided_at + timedelta(minutes=1),
            triage_actor="operator",
            batch_id="batch-1",
            reason="duplicate",
        ),
        programs_root=programs_root,
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert status_result.exit_code == 0
    assert "latest_triage_actor=operator" in status_result.output
    assert "latest_triage_session_decisions=2" in status_result.output
    assert "latest_triage_session_duration_seconds=60" in status_result.output
    assert "latest_triage_throughput_per_minute=2.0" in status_result.output
    assert "triage_session_gap_minutes=30" in status_result.output


def test_knowledge_status_reports_batch_progress_metrics(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

    append_candidate(
        build_candidate(
            candidate_id="cand-staged",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=now,
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            pipeline="extract",
            extraction_confidence=0.9,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-staged",
            created_at=now,
        ),
        programs_root=programs_root,
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-approved",
            scope="domain:storage-platform",
            subject="sku_generation:gen10",
            predicate="first_deployment",
            value="2026-H1",
            valid_from=now,
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            pipeline="ingest",
            extraction_confidence=0.8,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-approved",
            created_at=now,
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-approved",
            kind="approved",
            decided_at=now + timedelta(minutes=1),
            triage_actor="operator",
            batch_id="batch-approved",
        ),
        programs_root=programs_root,
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-quarantined",
            scope="domain:storage-platform",
            subject="sku_generation:gen11",
            predicate="first_deployment",
            value="2027-H1",
            valid_from=now,
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            pipeline="extract",
            extraction_confidence=0.7,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-quarantined",
            created_at=now,
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-quarantined",
            kind="rejected",
            decided_at=now + timedelta(minutes=2),
            triage_actor="operator",
            batch_id="batch-quarantined",
            reason="quarantined: bad extraction",
        ),
        programs_root=programs_root,
    )

    status_json = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert status_json.exit_code == 0
    assert payload["batch_count"] == 3
    assert payload["staged_batch_count"] == 1
    assert payload["approved_batch_count"] == 1
    assert payload["quarantined_batch_count"] == 1
    assert {batch["batch_id"]: batch["status"] for batch in payload["batches"]} == {
        "batch-approved": "approved",
        "batch-quarantined": "quarantined",
        "batch-staged": "staged",
    }
    assert payload["batches"][0]["pipelines"]

    assert status_text.exit_code == 0
    assert "batches_total=3 batches_staged=1 batches_approved=1 batches_quarantined=1" in status_text.output
    assert "batch=batch-quarantined status=quarantined" in status_text.output


def test_knowledge_status_reports_missing_claim_and_candidate_vault_refs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"

    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=KnowledgeDocumentRef(
            vault_hash="sha256:missing-claim-hash",
            original_filename="claim.md",
            origin_kind="knowledge_markdown",
            origin_path="claim.md",
            ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            section="document",
        ),
        knowledge_root=knowledge_root,
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-missing-vault",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=KnowledgeDocumentRef(
                vault_hash="sha256:missing-candidate-hash",
                original_filename="candidate.md",
                origin_kind="knowledge_markdown",
                origin_path="candidate.md",
                ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                section="document",
            ),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["vault"]["missing_claim_ref_count"] == 1
    assert payload["vault"]["missing_candidate_ref_count"] == 1


def test_knowledge_status_reports_expired_and_expiring_latest_claims(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    now = datetime.now(timezone.utc)

    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=now - timedelta(days=90),
        valid_until=now - timedelta(days=1),
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now - timedelta(days=90)),
        knowledge_root=knowledge_root,
        recorded_at=now - timedelta(days=2),
    )
    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen10",
        predicate="first_deployment",
        value="2026-H2",
        valid_from=now - timedelta(days=10),
        valid_until=now + timedelta(days=7),
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now - timedelta(days=10)),
        knowledge_root=knowledge_root,
        recorded_at=now - timedelta(days=1),
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["expired_claim_count"] == 1
    assert payload["expiring_soon_claim_count"] == 1
    assert payload["warning_window_days"] == 30


def test_knowledge_status_reports_active_program_scope_overrides(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "id: acme\nknowledge_scopes:\n  - domain:storage-platform\n",
        encoding="utf-8",
    )

    runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "program:acme",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-Q4",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["active_override_count"] == 1
    assert payload["active_override_program_count"] == 1


def test_knowledge_status_reports_scope_active_claims_by_confidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "assert",
            "--scope",
            "domain:storage-platform",
            "--subject",
            "sku_generation:gen9",
            "--predicate",
            "first_deployment",
            "--value",
            "2025-H2",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen10",
        predicate="first_deployment",
        value="2026-H2",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        knowledge_root=tmp_path / "knowledge",
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "status",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["registered_predicate_count"] == 4
    assert payload["scopes"][0]["predicate_count"] == 1
    assert payload["scopes"][0]["active_claims_by_confidence"] == {
        "ai_extracted": 0,
        "inferred": 0,
        "operator_confirmed": 1,
        "source_authoritative": 1,
    }


def test_knowledge_triage_approve_writes_claim_and_decision(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    rows = next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()
    decisions = load_triage_decisions(programs_root=programs_root)

    assert result.exit_code == 0
    assert json.loads(rows[0])["predicate"] == "first_deployment"
    assert decisions[0].kind == "approved"
    assert decisions[0].resulting_claim_id is not None


def test_knowledge_triage_reject_writes_decision_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "reject",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--reason",
            "duplicate",
            "--programs-root",
            str(programs_root),
        ],
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    decisions = load_triage_decisions(programs_root=programs_root)

    assert result.exit_code == 0
    assert not claims_path.exists()
    assert decisions[0].kind == "rejected"
    assert decisions[0].reason == "duplicate"


def test_knowledge_triage_skip_preserves_candidate_as_active(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )

    skip_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "skip",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--reason",
            "needs more review",
            "--programs-root",
            str(programs_root),
        ],
    )
    list_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "list",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(list_result.stdout)
    decisions = load_triage_decisions(programs_root=programs_root)

    assert skip_result.exit_code == 0
    assert list_result.exit_code == 0
    assert decisions[0].kind == "skipped"
    assert decisions[0].reason == "needs more review"
    assert payload["candidates"][0]["candidate_id"] == "cand-1"
    assert payload["candidates"][0]["effective_subject"] == "sku_generation:gen9"
    assert payload["candidates"][0]["extraction_confidence"] == 0.8
    assert payload["candidates"][0]["corroborating_ref_count"] == 0
    assert payload["candidates"][0]["entity_resolution"][0]["resolved_entity_id"] == "sku_generation:gen9"


def test_knowledge_triage_refreshes_entity_resolution_from_current_registry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    org_entities_path = tmp_path / "vertex" / "knowledge" / "entities.yaml"
    org_entities_path.parent.mkdir(parents=True, exist_ok=True)
    org_entities_path.write_text(
        "entities:\n"
        "  - entity_id: sku_generation:gen9\n"
        "    entity_type: sku_generation\n"
        "    canonical_name: Gen9\n"
        "    aliases: [gen9]\n"
        "    scope: org\n",
        encoding="utf-8",
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.95,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id=None, match_kind="unresolved", score=0.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )

    list_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "list",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "batch-status",
            "--batch-id",
            "batch-1",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    payload = json.loads(list_result.stdout)
    batch_status = json.loads(status_result.stdout)
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    claim_payload = json.loads(next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()[0])

    assert list_result.exit_code == 0
    assert status_result.exit_code == 0
    assert approve_result.exit_code == 0
    assert payload["candidates"][0]["subject"] == "gen9"
    assert payload["candidates"][0]["effective_subject"] == "sku_generation:gen9"
    assert payload["candidates"][0]["effective_entity_resolution"][0]["resolved_entity_id"] == "sku_generation:gen9"
    assert batch_status["entity_resolution_rate"] == 1.0
    assert batch_status["entity_resolution_gate"] is True
    assert claim_payload["subject"] == "sku_generation:gen9"


def test_effective_candidate_entity_resolution_surfaces_ambiguity(monkeypatch, tmp_path: Path) -> None:
    """ADF-W2.6: when the current registry finds a genuine near-tied
    ambiguous match for a candidate's raw_name, `_effective_candidate_entity_resolution`
    surfaces that as its own `match_kind="ambiguous"` entry (resolved_entity_id
    still None) instead of silently keeping the previously-recorded
    (unresolved) resolution as if the registry state hadn't changed."""
    from unittest.mock import patch

    from src.commands.knowledge import _effective_candidate_entity_resolution
    from src.core.entity_registry import EntityRegistry
    from src.core.program_reality import CanonicalEntity

    entity_a = CanonicalEntity(entity_id="t1", entity_type="person", canonical_name="Jordan Rivers", aliases=(), scope="program")
    entity_b = CanonicalEntity(entity_id="t2", entity_type="person", canonical_name="Jordan Rivera", aliases=(), scope="program")
    ambiguous_registry = EntityRegistry(program_entities=(entity_a, entity_b), org_entities=())

    candidate = build_candidate(
        candidate_id="cand-ambiguous",
        scope="domain:storage-platform",
        subject="Jordan River",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
        valid_until=None,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        pipeline="extract",
        extraction_confidence=0.95,
        entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="Jordan River", resolved_entity_id=None, match_kind="unresolved", score=0.0),),
        corroborating_refs=(),
        batch_id="batch-1",
    )

    with patch("src.commands.knowledge.EntityRegistry.load", return_value=ambiguous_registry):
        effective = _effective_candidate_entity_resolution(candidate, programs_root=tmp_path / "programs")

    assert len(effective) == 1
    assert effective[0].match_kind == "ambiguous"
    assert effective[0].resolved_entity_id is None


def test_knowledge_triage_approve_allows_unexpired_skipped_candidate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-1",
            kind="skipped",
            decided_at=datetime.now(timezone.utc),
            triage_actor="operator",
            batch_id="batch-1",
            reason="needs more review",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    decisions = load_triage_decisions(programs_root=programs_root)

    assert result.exit_code == 0
    assert len(decisions) == 2
    assert decisions[0].kind == "skipped"
    assert decisions[1].kind == "approved"


def test_knowledge_triage_expire_skips_materializes_terminal_rejection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="cand-1",
            kind="skipped",
            decided_at=datetime.now(timezone.utc) - timedelta(days=91),
            triage_actor="operator",
            batch_id="batch-1",
            reason="needs more review",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "expire-skips",
            "--programs-root",
            str(programs_root),
        ],
    )
    decisions = load_triage_decisions(programs_root=programs_root)

    assert result.exit_code == 0
    assert "Materialized 1 expired skipped candidate(s)." in result.stdout
    assert len(decisions) == 2
    assert decisions[1].kind == "rejected"
    assert decisions[1].reason == "skip expired after 90 days"


def test_knowledge_triage_edit_writes_operator_confirmed_revision_and_marks_audit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.8,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "edit",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--value",
            "2025-Q4",
            "--reason",
            "corrected from operator review",
            "--programs-root",
            str(programs_root),
        ],
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    claim_rows = next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()
    claim_payload = json.loads(claim_rows[0])
    decisions = load_triage_decisions(programs_root=programs_root)

    assert result.exit_code == 0
    assert claim_payload["value"] == "2025-Q4"
    assert claim_payload["confidence"] == "operator_confirmed"
    assert claim_payload["source_ref"]["ref_type"] == "operator_assertion"
    assert claim_payload["source_ref"]["context"] == "corrected from operator review"
    assert decisions[0].kind == "approved"
    assert decisions[0].edited is True
    assert decisions[0].resulting_claim_id is not None


def test_knowledge_triage_batch_approve_promotes_active_batch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    candidate_ids = [f"cand-{index}" for index in range(12)]
    for index, candidate_id in enumerate(candidate_ids):
        subject = f"sku_generation:gen{index}"
        append_candidate(
            build_candidate(
                candidate_id=candidate_id,
                scope="domain:storage-platform",
                subject=subject,
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
                valid_until=None,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                pipeline="extract",
                extraction_confidence=0.95,
                entity_resolution=(KnowledgeCandidateEntityResolution(raw_name=subject, resolved_entity_id=subject, match_kind="exact", score=1.0),),
                corroborating_refs=(),
                batch_id="batch-1",
            ),
            programs_root=programs_root,
        )

    for candidate_id in candidate_ids[:10]:
        approve_result = runner.invoke(
            app,
            [
                "--no-catchup",
                "knowledge",
                "triage",
                "approve",
                "--candidate",
                candidate_id,
                "--actor",
                "operator",
                "--programs-root",
                str(programs_root),
            ],
        )
        assert approve_result.exit_code == 0

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "batch-approve",
            "--batch-id",
            "batch-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    claims_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "claims"
    decisions = load_triage_decisions(programs_root=programs_root)

    assert result.exit_code == 0
    assert "Batch approval progress" in result.stdout
    assert len(next(claims_path.glob("claims-*.jsonl")).read_text(encoding="utf-8").splitlines()) == 12
    assert len([decision for decision in decisions if decision.kind == "approved"]) == 12


def test_knowledge_triage_batch_approve_enforces_sample_gate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    for candidate_id, subject in (("cand-1", "sku_generation:gen9"), ("cand-2", "sku_generation:gen10")):
        append_candidate(
            build_candidate(
                candidate_id=candidate_id,
                scope="domain:storage-platform",
                subject=subject,
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
                valid_until=None,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                pipeline="extract",
                extraction_confidence=0.95,
                entity_resolution=(KnowledgeCandidateEntityResolution(raw_name=subject, resolved_entity_id=subject, match_kind="exact", score=1.0),),
                corroborating_refs=(),
                batch_id="batch-1",
            ),
            programs_root=programs_root,
        )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "batch-approve",
            "--batch-id",
            "batch-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 3
    assert "failed sample gate" in result.stdout


def test_knowledge_triage_batch_approve_enforces_confidence_gate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    candidate_ids = [f"cand-{index}" for index in range(11)]
    for index, candidate_id in enumerate(candidate_ids):
        subject = f"sku_generation:gen{index}"
        append_candidate(
            build_candidate(
                candidate_id=candidate_id,
                scope="domain:storage-platform",
                subject=subject,
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
                valid_until=None,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                pipeline="extract",
                extraction_confidence=0.95 if index < 10 else 0.5,
                entity_resolution=(KnowledgeCandidateEntityResolution(raw_name=subject, resolved_entity_id=subject, match_kind="exact", score=1.0),),
                corroborating_refs=(),
                batch_id="batch-1",
            ),
            programs_root=programs_root,
        )

    for candidate_id in candidate_ids[:10]:
        approve_result = runner.invoke(
            app,
            [
                "--no-catchup",
                "knowledge",
                "triage",
                "approve",
                "--candidate",
                candidate_id,
                "--actor",
                "operator",
                "--programs-root",
                str(programs_root),
            ],
        )
        assert approve_result.exit_code == 0

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "batch-approve",
            "--batch-id",
            "batch-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 3
    assert "failed confidence gate" in result.stdout


def test_knowledge_quarantine_batch_rejects_active_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    for candidate_id in ("cand-1", "cand-2"):
        append_candidate(
            build_candidate(
                candidate_id=candidate_id,
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
                valid_until=None,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                pipeline="extract",
                extraction_confidence=0.95,
                entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="sku_generation:gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
                corroborating_refs=(),
                batch_id="batch-1",
            ),
            programs_root=programs_root,
        )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "quarantine-batch",
            "--batch-id",
            "batch-1",
            "--actor",
            "operator",
            "--reason",
            "bad extraction",
            "--programs-root",
            str(programs_root),
        ],
    )
    decisions = load_triage_decisions(programs_root=programs_root)
    list_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "list",
            "--batch-id",
            "batch-1",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(list_result.stdout)

    assert result.exit_code == 0
    assert [decision.kind for decision in decisions] == ["rejected", "rejected"]
    assert all(decision.reason == "quarantined: bad extraction" for decision in decisions)
    assert payload["candidates"] == []


def test_knowledge_quarantine_batch_blocks_post_approval_cleanup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.95,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="sku_generation:gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-1",
        ),
        programs_root=programs_root,
    )
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    quarantine_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "quarantine-batch",
            "--batch-id",
            "batch-1",
            "--actor",
            "operator",
            "--reason",
            "bad extraction",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 0
    assert quarantine_result.exit_code == 3
    assert "already contains approved candidates" in quarantine_result.stdout


def test_knowledge_triage_batch_status_reports_current_gates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(
        build_candidate(
            candidate_id="cand-b1",
            scope="domain:storage-platform",
            subject="sku_generation:gen9",
            predicate="first_deployment",
            value="2025-H2",
            valid_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.95,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen9", resolved_entity_id="sku_generation:gen9", match_kind="exact", score=1.0),),
            corroborating_refs=(),
            batch_id="batch-qg",
        ),
        programs_root=programs_root,
    )
    append_candidate(
        build_candidate(
            candidate_id="cand-b2",
            scope="domain:storage-platform",
            subject="sku_generation:gen10",
            predicate="first_deployment",
            value="2026-H1",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_until=None,
            proposed_confidence=ConfidenceTier.AI_EXTRACTED,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            pipeline="extract",
            extraction_confidence=0.5,
            entity_resolution=(KnowledgeCandidateEntityResolution(raw_name="gen10", resolved_entity_id=None, match_kind="unresolved", score=0.0),),
            corroborating_refs=(),
            batch_id="batch-qg",
        ),
        programs_root=programs_root,
    )
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            "cand-b1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "batch-status",
            "--batch-id",
            "batch-qg",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert approve_result.exit_code == 0
    assert status_result.exit_code == 0
    assert payload["total_candidates"] == 2
    assert payload["approved_sample_count"] == 1
    assert payload["required_sample_count"] == 2
    assert payload["entity_resolution_rate"] == 0.5
    assert payload["entity_resolution_gate"] is False
    assert payload["sample_gate"] is False
    assert payload["confidence_gate"] is False


def test_knowledge_ingest_writes_vault_and_sources_registry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "dd-acme-kb.md"
    source_path.write_text("# Title\n\nHello\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_path),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )

    sources_path = tmp_path / "knowledge" / "domains" / "storage-platform" / "sources.yaml"
    vault_root = tmp_path / "knowledge" / "vault"

    assert result.exit_code == 0
    assert sources_path.exists()
    assert any(path.is_file() and not path.name.endswith(".meta.json") for path in vault_root.glob("**/*"))


def test_knowledge_extract_stages_candidates_from_ingested_markdown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "dd-acme-kb.md"
    source_path.write_text(
        "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2; valid_from=2025-07-01\n",
        encoding="utf-8",
    )

    ingest_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_path),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    pending_path = tmp_path / "knowledge" / "candidates" / "pending.jsonl"
    rows = pending_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])

    assert ingest_result.exit_code == 0
    assert extract_result.exit_code == 0
    assert payload["proposed_claim"]["predicate"] == "first_deployment"
    assert payload["source_ref"]["ref_type"] == "knowledge_document"


def test_knowledge_extract_uses_one_batch_id_across_multiple_sources(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_one = tmp_path / "dd-acme-kb-1.md"
    source_two = tmp_path / "dd-acme-kb-2.md"
    source_one.write_text(
        "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2\n",
        encoding="utf-8",
    )
    source_two.write_text(
        "Claim: subject=sku_generation:gen10; predicate=first_deployment; value=2026-H1\n",
        encoding="utf-8",
    )

    ingest_one = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_one),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    ingest_two = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_two),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )

    rows = [json.loads(line) for line in (tmp_path / "knowledge" / "candidates" / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    batch_ids = {row["batch_id"] for row in rows}

    assert ingest_one.exit_code == 0
    assert ingest_two.exit_code == 0
    assert extract_result.exit_code == 0
    assert len(rows) == 2
    assert len(batch_ids) == 1
    assert next(iter(batch_ids)) in extract_result.stdout


def test_knowledge_extract_collapses_duplicate_claims_across_sources(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_one = tmp_path / "dd-acme-kb-1.md"
    source_two = tmp_path / "dd-acme-kb-2.md"
    claim_line = "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2\n"
    source_one.write_text("# Source one\n" + claim_line, encoding="utf-8")
    source_two.write_text("# Source two\n" + claim_line, encoding="utf-8")

    ingest_one = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_one),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    ingest_two = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_two),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )

    rows = [json.loads(line) for line in (tmp_path / "knowledge" / "candidates" / "pending.jsonl").read_text(encoding="utf-8").splitlines()]

    assert ingest_one.exit_code == 0
    assert ingest_two.exit_code == 0
    assert extract_result.exit_code == 0
    assert len(rows) == 1
    assert len(rows[0]["corroborating_refs"]) == 1


def test_knowledge_extract_routes_decision_log_kb_to_ledger_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "decision-log-kb.md"
    source_path.write_text(
        "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=Decision Log\n",
        encoding="utf-8",
    )

    ingest_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_path),
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    ledger_pending = load_pending_ledger_candidates("acme", programs_root=programs_root)
    knowledge_pending_path = tmp_path / "knowledge" / "candidates" / "pending.jsonl"

    assert ingest_result.exit_code == 0
    assert extract_result.exit_code == 0
    assert len(ledger_pending) == 1
    assert ledger_pending[0].proposed_event_type == "decision.made.v1"
    assert ledger_pending[0].source_ref.ref_type == "knowledge_document"
    assert not knowledge_pending_path.exists()


def test_knowledge_extract_collapses_duplicate_decision_log_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_one = tmp_path / "team-one" / "decision-log-kb.md"
    source_two = tmp_path / "team-two" / "decision-log-kb.md"
    source_one.parent.mkdir(parents=True)
    source_two.parent.mkdir(parents=True)
    source_text = (
        "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | "
        "title=Gen9 ship date | decided_by=person:alice,person:bob | forum=Decision Log\n"
    )
    source_one.write_text(source_text, encoding="utf-8")
    source_two.write_text(source_text + "Context: repeated in another curated KB source\n", encoding="utf-8")

    ingest_one = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_one),
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )
    ingest_two = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_two),
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    ledger_pending = load_pending_ledger_candidates("acme", programs_root=programs_root)

    assert ingest_one.exit_code == 0
    assert ingest_two.exit_code == 0
    assert extract_result.exit_code == 0
    assert len(ledger_pending) == 1
    assert ledger_pending[0].proposed_event_type == "decision.made.v1"
    assert len(ledger_pending[0].corroborating_refs) == 1


def test_knowledge_extract_program_scope_routes_event_markers_and_claims_from_same_source(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "program-status-kb.md"
    source_path.write_text(
        "\n".join(
            [
                "# Program status notes",
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=Program KB",
                "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2",
            ]
        ),
        encoding="utf-8",
    )

    ingest_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_path),
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "program:acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    ledger_pending = load_pending_ledger_candidates("acme", programs_root=programs_root)
    knowledge_rows = [json.loads(line) for line in (tmp_path / "knowledge" / "candidates" / "pending.jsonl").read_text(encoding="utf-8").splitlines()]

    assert ingest_result.exit_code == 0
    assert extract_result.exit_code == 0
    assert len(ledger_pending) == 1
    assert ledger_pending[0].proposed_event_type == "decision.made.v1"
    assert len(knowledge_rows) == 1
    assert knowledge_rows[0]["proposed_claim"]["predicate"] == "first_deployment"


def test_knowledge_redact_vault_cascades_claims_and_active_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "id: acme\nknowledge_scopes:\n  - domain:storage-platform\n",
        encoding="utf-8",
    )
    source_path = tmp_path / "knowledge-source.md"
    source_path.write_text(
        "\n".join(
            (
                "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2",
                "Claim: subject=sku_generation:gen10; predicate=first_deployment; value=2026-H1",
            )
        ),
        encoding="utf-8",
    )

    ingest_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_path),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    vault_hash = ingest_result.stdout.strip().split(" -> ", maxsplit=1)[1].split(" ", maxsplit=1)[0]
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "domain:storage-platform",
            "--source",
            vault_hash,
            "--programs-root",
            str(programs_root),
        ],
    )
    pending_path = tmp_path / "knowledge" / "candidates" / "pending.jsonl"
    pending_rows = [json.loads(line) for line in pending_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    approved_candidate_id = next(row["candidate_id"] for row in pending_rows if row["proposed_claim"]["subject"] == "sku_generation:gen9")

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            approved_candidate_id,
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    redact_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "redact-vault",
            "--vault-hash",
            vault_hash,
            "--reason",
            "pii cleanup",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    show_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "show",
            "--program",
            "acme",
            "--entity",
            "sku_generation:gen9",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    list_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "list",
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    show_payload = json.loads(show_result.stdout)
    list_payload = json.loads(list_result.stdout)
    decisions = load_triage_decisions(programs_root=programs_root)
    redaction_rows = [json.loads(line) for line in (tmp_path / "knowledge" / ".claim-redactions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    sources_yaml = (tmp_path / "knowledge" / "domains" / "storage-platform" / "sources.yaml").read_text(encoding="utf-8")

    assert ingest_result.exit_code == 0
    assert extract_result.exit_code == 0
    assert approve_result.exit_code == 0
    assert redact_result.exit_code == 0
    assert show_result.exit_code == 0
    assert list_result.exit_code == 0
    assert show_payload["entry"]["claims"] == []
    assert list_payload["candidates"] == []
    assert any(row["reason"] == "vault redacted: pii cleanup" for row in redaction_rows)
    assert any(decision.kind == "rejected" and decision.reason == "source vault redacted: pii cleanup" for decision in decisions)
    assert vault_hash not in sources_yaml
    assert not any((tmp_path / "knowledge" / "vault").glob(f"**/{vault_hash.split(':', maxsplit=1)[1]}"))


def test_knowledge_redact_vault_reports_affected_backups_when_root_provided(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "knowledge-source.md"
    source_path.write_text(
        "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2\n",
        encoding="utf-8",
    )
    ingest_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "ingest",
            "--source",
            str(source_path),
            "--scope",
            "domain:storage-platform",
            "--programs-root",
            str(programs_root),
        ],
    )
    vault_hash = ingest_result.stdout.strip().split(" -> ", maxsplit=1)[1].split(" ", maxsplit=1)[0]
    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "domain:storage-platform",
            "--source",
            vault_hash,
            "--programs-root",
            str(programs_root),
        ],
    )
    candidate_id = json.loads((tmp_path / "knowledge" / "candidates" / "pending.jsonl").read_text(encoding="utf-8").splitlines()[0])["candidate_id"]
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            candidate_id,
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    backup_root = tmp_path / "backups" / "snap-001"
    create_repository_backup(backup_root, source_root=tmp_path)

    redact_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "redact-vault",
            "--vault-hash",
            vault_hash,
            "--reason",
            "pii cleanup",
            "--actor",
            "operator",
            "--backup-root",
            str(tmp_path / "backups"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert ingest_result.exit_code == 0
    assert extract_result.exit_code == 0
    assert approve_result.exit_code == 0
    assert redact_result.exit_code == 0
    assert "Affected backups:" in redact_result.stdout
    assert str(backup_root.resolve()) in redact_result.stdout


def test_knowledge_gc_deletes_only_old_unreferenced_vault_entries(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_old_referenced = tmp_path / "old-referenced.md"
    source_old_orphan = tmp_path / "old-orphan.md"
    source_recent = tmp_path / "recent.md"
    source_old_referenced.write_text(
        "Claim: subject=sku_generation:gen9; predicate=first_deployment; value=2025-H2\n",
        encoding="utf-8",
    )
    source_old_orphan.write_text("orphan\n", encoding="utf-8")
    source_recent.write_text("recent\n", encoding="utf-8")
    old_time = datetime(2026, 2, 1, tzinfo=timezone.utc)
    recent_time = datetime(2026, 6, 1, tzinfo=timezone.utc)

    referenced_entry = ingest_knowledge_source(
        source_old_referenced,
        scope="domain:storage-platform",
        programs_root=programs_root,
        ingested_at=old_time,
    )
    orphan_entry = ingest_knowledge_source(
        source_old_orphan,
        scope="domain:storage-platform",
        programs_root=programs_root,
        ingested_at=old_time,
    )
    recent_entry = ingest_knowledge_source(
        source_recent,
        scope="domain:storage-platform",
        programs_root=programs_root,
        ingested_at=recent_time,
    )

    extract_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "extract",
            "--scope",
            "domain:storage-platform",
            "--source",
            referenced_entry.vault_hash,
            "--programs-root",
            str(programs_root),
        ],
    )
    pending_path = tmp_path / "knowledge" / "candidates" / "pending.jsonl"
    candidate_id = json.loads(pending_path.read_text(encoding="utf-8").splitlines()[0])["candidate_id"]
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "triage",
            "approve",
            "--candidate",
            candidate_id,
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    dry_run_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "gc",
            "--dry-run",
            "--format",
            "json",
            "--programs-root",
            str(programs_root),
        ],
    )
    dry_run_payload = json.loads(dry_run_result.stdout)
    gc_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "knowledge",
            "gc",
            "--format",
            "json",
            "--programs-root",
            str(programs_root),
        ],
    )
    gc_payload = json.loads(gc_result.stdout)

    assert extract_result.exit_code == 0
    assert approve_result.exit_code == 0
    assert dry_run_result.exit_code == 0
    assert gc_result.exit_code == 0
    assert dry_run_payload["candidate_count"] == 1
    assert dry_run_payload["candidates"][0]["vault_hash"] == orphan_entry.vault_hash
    assert gc_payload["deleted_count"] == 1
    assert gc_payload["candidates"][0]["vault_hash"] == orphan_entry.vault_hash
    load_vault_entry(referenced_entry.vault_hash, programs_root=programs_root)
    load_vault_entry(recent_entry.vault_hash, programs_root=programs_root)
    orphan_bucket = orphan_entry.vault_hash.split(":", maxsplit=1)[1][:2]
    orphan_file = tmp_path / "knowledge" / "vault" / orphan_bucket / orphan_entry.vault_hash.split(":", maxsplit=1)[1]
    assert not orphan_file.exists()