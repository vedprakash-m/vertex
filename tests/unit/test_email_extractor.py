from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.ai.discovery.email_extractor import extract_email_candidates
from src.core.ledger.evidence_vault import evidence_vault_entry_status


def test_extract_email_candidates_reads_structured_marker_lines_and_stores_evidence(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "DDPF_daily" / "2025-03-20 daily.eml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "\n".join(
            [
                "Subject: Contoso Daily",
                "From: sender@example.com",
                "Date: Thu, 20 Mar 2025 10:15:00 +0000",
                "Message-ID: <msg-1@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=Daily",
                "Risk: Supplier lead time remains unstable | risk_id=risk:supplier-lead | title=Supplier delay | severity=high | owner_person_id=person:alice | workstream_id=workstream:supply",
                "KPI: Daily deployment count | kpi_id=kpi:deployments | value=7 | unit=count | window_end=2025-03-20 | dimensions=ring:prod",
            ]
        ),
        encoding="utf-8",
    )

    batch = extract_email_candidates(
        program_id="acme",
        source_path=source_path,
        batch_id="batch-1",
        programs_root=programs_root,
    )

    assert len(batch.candidates) == 3
    assert [candidate.proposed_event_type for candidate in batch.candidates] == ["decision.made.v1", "risk.raised.v1", "metric.observed.v1"]
    source_ref = batch.candidates[0].source_ref
    assert source_ref.ref_type == "email"
    assert source_ref.message_id == "<msg-1@example.com>"
    assert evidence_vault_entry_status(program_id="acme", vault_hash=source_ref.vault_hash, programs_root=programs_root) == "ok"
    assert batch.candidates[2].proposed_payload["dimensions"] == {"ring": "prod"}
    assert batch.candidates[2].proposed_occurred_at == datetime(2025, 3, 20, 0, 0, tzinfo=timezone.utc)
    assert batch.candidates[2].proposed_temporal_confidence == "approximate"


def test_extract_email_candidates_ignores_unstructured_body_lines(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "DDPF_daily" / "2025-03-20 daily.eml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "\n".join(
            [
                "Subject: Contoso Daily",
                "From: sender@example.com",
                "Date: Thu, 20 Mar 2025 10:15:00 +0000",
                "Message-ID: <msg-1@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "General update without markers.",
            ]
        ),
        encoding="utf-8",
    )

    batch = extract_email_candidates(
        program_id="acme",
        source_path=source_path,
        batch_id="batch-1",
        programs_root=programs_root,
    )

    assert batch.candidates == ()
