from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from typer.testing import CliRunner

from cli import app
from src.core.ledger.candidate_store import load_pending_candidates
from src.core.ledger.event_log import read_events
from src.core.models_v2 import EngMsPage, M365Config, Program
from src.m365.graph_mail_client import MailRecord
from src.m365.teams_reader import TeamsMessageRecord


runner = CliRunner()


def test_discover_candidates_preview_does_not_write_events(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--pipeline",
            "lt_deck",
            "--batch-id",
            "batch-1",
            "--candidate-count",
            "2",
            "--gap-json",
            '{"gap_kind": "parse_failure", "detail": "slide 7 unreadable"}',
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["pipeline"] == "lt_deck"
    assert payload["batch_id"] == "batch-1"
    assert payload["candidate_count"] == 2
    assert payload["gap_count"] == 1
    assert payload["recorded"] is False
    assert payload["written_event_ids"] == []
    assert read_events("acme", programs_root=programs_root) == ()


def test_discover_candidates_record_writes_governance_events(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--pipeline",
            "newsletter_backfill",
            "--batch-id",
            "batch-2",
            "--candidate-count",
            "3",
            "--gap-json",
            '{"gap_kind": "zero_yield", "detail": "mailbox returned nothing", "window_end": "2026-06-11T12:00:00Z"}',
            "--record",
            "--recorded-at",
            "2026-06-11T12:30:00Z",
            "--actor",
            "discover-test",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["recorded"] is True
    assert payload["written_event_types"] == [
        "pipeline.gap_detected.v1",
        "discovery.candidate_proposed.v1",
    ]
    assert len(payload["written_event_ids"]) == 2

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "pipeline.gap_detected.v1",
        "discovery.candidate_proposed.v1",
    ]
    assert events[0].payload["pipeline"] == "newsletter_backfill"
    assert events[1].payload["candidate_count"] == 3
    assert events[1].recorded_at == datetime(2026, 6, 11, 12, 30, tzinfo=timezone.utc)


def test_discover_candidates_accepts_result_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--result-json",
            json.dumps(
                {
                    "pipeline": "sharepoint_doc",
                    "batch_id": "batch-3",
                    "candidates_written": 0,
                    "heartbeat": True,
                    "gaps": [
                        {
                            "gap_kind": "auth_failure",
                            "detail": "token expired",
                            "window_start": "2026-06-11T11:00:00Z",
                        }
                    ],
                }
            ),
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "sharepoint_doc"
    assert payload["batch_id"] == "batch-3"
    assert payload["candidate_count"] == 0
    assert payload["gaps"][0]["gap_kind"] == "auth_failure"


def test_discover_candidates_rejects_mixed_result_json_and_inline_fields(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--result-json",
            '{"pipeline": "lt_deck", "batch_id": "batch-4", "candidates_written": 1, "heartbeat": true, "gaps": []}',
            "--pipeline",
            "lt_deck",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "--result-json cannot be combined" in result.output


def test_discover_candidates_source_backfill_import_stages_candidates_and_records_run(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "import.jsonl"
    source_path.write_text(
        json.dumps(
            {
                "proposed_event_type": "milestone.date_revised.v1",
                "proposed_payload": {"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
                "proposed_occurred_at": "2025-03-20T00:00:00+00:00",
                "proposed_temporal_confidence": "approximate",
                "proposed_confidence": "source_authoritative",
                "source_ref": {
                    "ref_type": "lt_deck",
                    "file_path": "deck.pptx",
                    "deck_date": "2025-03-20",
                    "slide_number": 9,
                    "slide_title": None,
                    "vault_hash": None,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "backfill_import",
            "--input-jsonl",
            str(source_path),
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "backfill_import"
    assert payload["candidate_count"] == 1
    assert payload["recorded"] is True
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 1
    assert pending[0].pipeline == "backfill_import"
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "backfill_import"


def test_discover_candidates_source_lt_deck_stages_candidates_and_records_run(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "Monthly_LT_Review"
    (source_dir / "2020").mkdir(parents=True)
    _write_minimal_pptx(
        source_dir / "2020" / "2020-11-03 Acme Review with Jordan and Tom.pptx",
        [
            [
                "Acme LT Update",
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=LT",
                "Risk: Supplier lead time remains unstable | risk_id=risk:supplier-lead | title=Supplier delay | severity=high | owner_person_id=person:alice | workstream_id=workstream:supply",
                "Metric: Deployment snapshot | kpi_id=kpi:deployments | value=12 | unit=count | window_end=2020-11-03 | dimensions=ring:prod",
            ]
        ],
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "lt_deck",
            "--source-dir",
            str(source_dir),
            "--from",
            "2020",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "lt_deck"
    assert payload["candidate_count"] == 3
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 3
    assert {candidate.pipeline for candidate in pending} == {"lt_deck"}
    assert {candidate.proposed_event_type for candidate in pending} == {"decision.made.v1", "risk.raised.v1", "metric.observed.v1"}
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "lt_deck"


def test_discover_candidates_source_lt_deck_zero_yield_records_gap(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "Monthly_LT_Review"
    (source_dir / "2020").mkdir(parents=True)
    _write_minimal_pptx(
        source_dir / "2020" / "2020-11-03 Acme Review with Jordan and Tom.pptx",
        [["Acme LT Update", "General status without structured markers"]],
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "lt_deck",
            "--source-dir",
            str(source_dir),
            "--from",
            "2020",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "zero_yield"
    assert load_pending_candidates("acme", programs_root=programs_root) == ()
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "lt_deck"


def test_discover_candidates_source_newsletter_stages_candidates_and_records_run(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "newsletters"
    source_dir.mkdir(parents=True)
    (source_dir / "2025-03-20_issue_051.html").write_text(
        """
        <html><body>
        <p>Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=LT</p>
        <p>Milestone: kind=revised | milestone_id=milestone:gen9-ga | new_target_date=2025-10-15 | prior_target_date=2025-09-30 | reason=Vendor slip</p>
        <p>Metric: KPI table snapshot | kpi_id=kpi:deployments | value=42 | unit=count | window_end=2025-03-20 | dimensions=ring:prod</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "newsletter",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "newsletter"
    assert payload["candidate_count"] == 4
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 4
    assert {candidate.pipeline for candidate in pending} == {"newsletter"}
    assert pending[0].proposed_event_type == "artifact.published.v1"
    assert "metric.observed.v1" in {candidate.proposed_event_type for candidate in pending}
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "newsletter"


def test_discover_candidates_source_newsletter_zero_yield_records_gap(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "newsletters"
    source_dir.mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "newsletter",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "zero_yield"
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "newsletter"


def test_discover_candidates_source_newsletter_records_missed_issue_gap(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "newsletters"
    source_dir.mkdir(parents=True)
    (source_dir / "2025-03-20_issue_051.html").write_text(
        "<html><body><p>Decision: Keep launch train | decision_id=decision:launch-train | title=Launch train | decided_by=person:alice | forum=Weekly</p></body></html>",
        encoding="utf-8",
    )
    (source_dir / "2025-04-03_issue_053.html").write_text(
        "<html><body><p>General update without markers</p></body></html>",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "newsletter",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 3
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "missed_window"
    assert "52" in payload["gaps"][0]["detail"]
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "pipeline.gap_detected.v1",
        "discovery.candidate_proposed.v1",
    ]
    assert events[0].payload["pipeline"] == "newsletter"
    assert events[0].payload["gap_kind"] == "missed_window"


def test_discover_candidates_source_newsletter_filters_real_corpus_filenames_by_year(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "newsletters"
    source_dir.mkdir(parents=True)
    (source_dir / "Adventure-Acme Program Update _ Issue 67 _ August 7, 2025.eml").write_text(
        "\n".join(
            [
                "Subject: Issue 67",
                "From: sender@example.com",
                "Date: Thu, 07 Aug 2025 21:12:07 +0000",
                "Message-ID: <issue-67@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: 2025 only | decision_id=decision:2025-only | title=2025 only | decided_by=person:alice | forum=Weekly",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "Program Hygiene _ Issue 76 _ April 10, 2026.eml").write_text(
        "\n".join(
            [
                "Subject: Issue 76",
                "From: sender@example.com",
                "Date: Fri, 10 Apr 2026 21:12:07 +0000",
                "Message-ID: <issue-76@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: 2026 only | decision_id=decision:2026-only | title=2026 only | decided_by=person:bob | forum=Weekly",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "newsletter",
            "--source-dir",
            str(source_dir),
            "--from",
            "2026",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 2
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 2
    assert {candidate.proposed_payload.get("artifact_id") for candidate in pending if candidate.proposed_event_type == "artifact.published.v1"} == {
        "published_artifact:issue-076"
    }


def test_discover_candidates_source_newsletter_uses_pdf_as_fallback_without_duplication(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "newsletters"
    source_dir.mkdir(parents=True)
    (source_dir / "Adventure-Acme Program Update _ Issue 67 _ August 7, 2025 - Alex Vance - Outlook.pdf").write_bytes(b"%PDF-1.4\n%minimal\n")
    (source_dir / "Program Hygiene _ Issue 76 _ April 10, 2026 - Alex Vance - Outlook.pdf").write_bytes(b"%PDF-1.4\n%minimal\n")
    (source_dir / "Program Hygiene _ Issue 76 _ April 10, 2026.eml").write_text(
        "\n".join(
            [
                "Subject: Issue 76",
                "From: sender@example.com",
                "Date: Fri, 10 Apr 2026 21:12:07 +0000",
                "Message-ID: <issue-76@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: 2026 only | decision_id=decision:2026-only | title=2026 only | decided_by=person:bob | forum=Weekly",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "newsletter",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 3
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 3
    artifact_ids = [candidate.proposed_payload["artifact_id"] for candidate in pending if candidate.proposed_event_type == "artifact.published.v1"]
    assert artifact_ids.count("published_artifact:issue-076") == 1
    assert artifact_ids.count("published_artifact:issue-067") == 1


def test_discover_candidates_source_email_stages_candidates_records_run_and_writes_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "DDPF_daily"
    source_dir.mkdir(parents=True)
    (source_dir / "2025-03-20 daily.eml").write_text(
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

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "email",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "email"
    assert payload["candidate_count"] == 3
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 3
    assert {candidate.pipeline for candidate in pending} == {"email"}
    assert {candidate.proposed_event_type for candidate in pending} == {"decision.made.v1", "risk.raised.v1", "metric.observed.v1"}
    assert all(getattr(candidate.source_ref, "vault_hash", None) for candidate in pending)
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "email"


def test_discover_candidates_source_email_collapses_same_week_duplicate_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "DDPF_daily"
    source_dir.mkdir(parents=True)
    (source_dir / "2025-03-17 daily.eml").write_text(
        "\n".join(
            [
                "Subject: Contoso Daily 1",
                "From: sender@example.com",
                "Date: Mon, 17 Mar 2025 10:15:00 +0000",
                "Message-ID: <msg-1@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=Daily",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "2025-03-18 daily.eml").write_text(
        "\n".join(
            [
                "Subject: Contoso Daily 2",
                "From: sender@example.com",
                "Date: Tue, 18 Mar 2025 10:15:00 +0000",
                "Message-ID: <msg-2@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=Daily",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "email",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 1
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 1
    assert pending[0].proposed_event_type == "decision.made.v1"
    assert len(pending[0].corroborating_refs) == 1
    assert getattr(pending[0].corroborating_refs[0], "message_id", None) == "<msg-2@example.com>"


def test_discover_candidates_source_email_filters_by_header_date_not_filename(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "DDPF_daily"
    source_dir.mkdir(parents=True)
    (source_dir / "RE_ [Contoso] Action required_ 2 slipping items need closure ASAP.eml").write_text(
        "\n".join(
            [
                "Subject: Action required",
                "From: sender@example.com",
                "Date: Tue, 02 Jun 2026 10:15:00 +0000",
                "Message-ID: <msg-2026@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Risk: Supplier lead time remains unstable | risk_id=risk:supplier-lead | title=Supplier delay | severity=high | owner_person_id=person:alice | workstream_id=workstream:supply",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "legacy-with-date-2025-03-20.eml").write_text(
        "\n".join(
            [
                "Subject: Old daily",
                "From: sender@example.com",
                "Date: Thu, 20 Mar 2025 10:15:00 +0000",
                "Message-ID: <msg-2025@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Risk: Old risk | risk_id=risk:old-risk | title=Old risk | severity=medium | owner_person_id=person:alice | workstream_id=workstream:supply",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "email",
            "--source-dir",
            str(source_dir),
            "--from",
            "2026",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 1
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 1
    assert getattr(pending[0].source_ref, "message_id", None) == "<msg-2026@example.com>"


def test_discover_candidates_source_email_zero_yield_records_gap(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "DDPF_daily"
    source_dir.mkdir(parents=True)
    (source_dir / "2025-03-20 daily.eml").write_text(
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

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "email",
            "--source-dir",
            str(source_dir),
            "--from",
            "2025",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "zero_yield"
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "email"


def test_discover_candidates_source_sharepoint_doc_stages_candidates_records_run_and_writes_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "sharepoint"
    (source_dir / "storage-site" / "roadmap").mkdir(parents=True)
    doc_path = source_dir / "storage-site" / "roadmap" / "gen9.md"
    doc_path.write_text(
        "\n".join(
            [
                "Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT",
                "Risk: rollout still blocked | risk_id=risk:gen9 | title=Rollout blocker | severity=high | owner_person_id=person:pm1",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "sharepoint_doc",
            "--source-dir",
            str(source_dir),
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "sharepoint_doc"
    assert payload["candidate_count"] == 2
    assert payload["recorded"] is True
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 2
    assert {candidate.pipeline for candidate in pending} == {"sharepoint_doc"}
    assert pending[0].source_ref.ref_type == "sharepoint_doc"
    assert pending[0].source_ref.vault_hash
    assert pending[0].source_ref.site == "storage-site"
    assert pending[0].source_ref.doc_path == "roadmap/gen9.md"
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "sharepoint_doc"


def test_discover_candidates_source_sharepoint_doc_zero_yield_records_gap(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "sharepoint"
    (source_dir / "storage-site").mkdir(parents=True)
    (source_dir / "storage-site" / "notes.md").write_text("No structured markers here.\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "sharepoint_doc",
            "--source-dir",
            str(source_dir),
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "sharepoint_doc"
    assert payload["candidate_count"] == 0
    assert payload["recorded"] is True
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "zero_yield"
    assert load_pending_candidates("acme", programs_root=programs_root) == ()
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "sharepoint_doc"


def test_discover_candidates_source_sharepoint_stages_candidates_and_collapses_duplicate_doc_hits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    monkeypatch.setattr(
        "src.m365.discovery.sharepoint_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(enabled=True, workiq_queries={"unused": "placeholder"}),
        ),
    )
    monkeypatch.setattr(
        "src.m365.discovery.sharepoint_pipeline.load_program_knowledge",
        lambda *_args, **_kwargs: SimpleNamespace(engms_pages=()),
    )
    monkeypatch.setattr(
        "src.m365.discovery.sharepoint_pipeline.select_engms_pages",
        lambda *_args, **_kwargs: (
            EngMsPage(
                id="acme-ramp-plan",
                title="Acme Ramp Plan",
                url=(
                    "https://microsoft.sharepoint.com/teams/Acme/_layouts/15/Doc.aspx?"
                    "file=Acme%20Ramp%20Plan.docx&action=default"
                ),
                workstream_ids=("acme",),
                program_ids=("acme",),
                tags=("ramp",),
            ),
            EngMsPage(
                id="acme-weekly-decision-log",
                title="Acme Weekly Decision Log",
                url=(
                    "https://microsoft.sharepoint.com/teams/Acme/_layouts/15/Doc.aspx?"
                    "file=Acme%20Weekly%20Decision%20Log.docx&action=default"
                ),
                workstream_ids=("acme",),
                program_ids=("acme",),
                tags=("decision-log",),
            ),
        ),
    )

    class FakeBridge:
        def __init__(self, **_kwargs):
            pass

        def probe(self):
            return SimpleNamespace(
                available=True,
                has_workiq=True,
                has_workiq_cli=False,
                server_tools={"workiq": ("ask_work_iq",)},
            )

        def ask_workiq(self, question, **_kwargs):
            if "Document title: Acme Ramp Plan." in question:
                return {
                    "response": "\n".join(
                        [
                            "Decision: Approve Acme ramp | decision_id=decision:acme-ramp | title=Acme ramp approved | decided_by=person:alice | forum=Weekly",
                            "Metric: Deployment snapshot | kpi_id=kpi:deployments | value=42 | unit=count | window_end=2026-01-15 | dimensions=ring:prod",
                            "Risk: Supplier lead time remains unstable | risk_id=risk:supplier-lead | title=Supplier lead time | severity=high | owner_person_id=person:alice | workstream_id=workstream:supply",
                        ]
                    )
                }
            return {
                "response": "Decision: Approve Acme ramp | decision_id=decision:acme-ramp | title=Acme ramp approved | decided_by=person:alice | forum=Weekly"
            }

        def last_mcp_error(self):
            return None

    monkeypatch.setattr("src.m365.discovery.sharepoint_pipeline.AgencyBridge", FakeBridge)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "sharepoint",
            "--from",
            "2026",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "sharepoint"
    assert payload["candidate_count"] == 3
    assert payload["gap_count"] == 0
    assert payload["recorded"] is True

    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 3
    assert {candidate.pipeline for candidate in pending} == {"sharepoint"}
    assert {candidate.source_ref.ref_type for candidate in pending} == {"sharepoint_doc"}
    assert all(candidate.source_ref.vault_hash for candidate in pending)
    assert any(len(candidate.corroborating_refs) == 1 for candidate in pending)
    metric_candidate = next(candidate for candidate in pending if candidate.proposed_event_type == "metric.observed.v1")
    assert metric_candidate.proposed_occurred_at == datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    assert metric_candidate.proposed_temporal_confidence == "approximate"

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "sharepoint"


def test_discover_candidates_source_sharepoint_records_auth_gap_when_runtime_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    monkeypatch.setattr(
        "src.m365.discovery.sharepoint_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(enabled=True, workiq_queries={"unused": "placeholder"}),
        ),
    )
    monkeypatch.setattr(
        "src.m365.discovery.sharepoint_pipeline.load_program_knowledge",
        lambda *_args, **_kwargs: SimpleNamespace(engms_pages=()),
    )
    monkeypatch.setattr(
        "src.m365.discovery.sharepoint_pipeline.select_engms_pages",
        lambda *_args, **_kwargs: (
            EngMsPage(
                id="acme-ramp-plan",
                title="Acme Ramp Plan",
                url=(
                    "https://microsoft.sharepoint.com/teams/Acme/_layouts/15/Doc.aspx?"
                    "file=Acme%20Ramp%20Plan.docx&action=default"
                ),
                workstream_ids=("acme",),
                program_ids=("acme",),
            ),
        ),
    )

    class FakeBridge:
        def __init__(self, **_kwargs):
            pass

        def probe(self):
            return SimpleNamespace(
                available=False,
                has_workiq=False,
                has_workiq_cli=False,
                server_tools={},
            )

        def ask_workiq(self, question, **_kwargs):
            raise AssertionError(f"ask_workiq should not be called when unavailable: {question}")

        def last_mcp_error(self):
            return "unavailable"

    monkeypatch.setattr("src.m365.discovery.sharepoint_pipeline.AgencyBridge", FakeBridge)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "sharepoint",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "sharepoint"
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "auth_failure"

    assert load_pending_candidates("acme", programs_root=programs_root) == ()
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "sharepoint"


def test_discover_candidates_source_workiq_stages_candidates_and_collapses_duplicate_query_hits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    monkeypatch.setattr(
        "src.m365.discovery.workiq_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(
                enabled=True,
                workiq_queries={
                    "risk_watch": "Find current risks.",
                    "weekly_events": "Find this week's decisions.",
                },
            ),
        ),
    )

    class FakeBridge:
        def probe(self):
            return SimpleNamespace(
                available=True,
                has_workiq=True,
                has_workiq_cli=False,
                server_tools={"workiq": ("ask_work_iq",)},
            )

        def ask_workiq(self, question, **_kwargs):
            if "Query name: risk_watch." in question:
                return {
                    "response": "\n".join(
                        [
                            "Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT",
                            "Metric: Deployment snapshot | kpi_id=kpi:deployments | value=7 | unit=count | window_end=2026-01-18 | dimensions=ring:prod",
                            "Risk: rollout still blocked | risk_id=risk:gen9 | title=Rollout blocker | severity=high | owner_person_id=person:pm1",
                        ]
                    )
                }
            if "Query name: weekly_events." in question:
                return {
                    "response": "Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT"
                }
            return {"response": "NO_EVENTS"}

        def last_mcp_error(self):
            return None

    monkeypatch.setattr("src.m365.discovery.workiq_pipeline.AgencyBridge", FakeBridge)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "workiq",
            "--from",
            "2026",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "workiq"
    assert payload["candidate_count"] == 3
    assert payload["gap_count"] == 0
    assert payload["recorded"] is True

    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 3
    assert {candidate.pipeline for candidate in pending} == {"workiq"}
    assert {candidate.source_ref.ref_type for candidate in pending} == {"workiq"}
    assert all(candidate.source_ref.vault_hash for candidate in pending)
    assert any(len(candidate.corroborating_refs) == 1 for candidate in pending)
    metric_candidate = next(candidate for candidate in pending if candidate.proposed_event_type == "metric.observed.v1")
    assert metric_candidate.proposed_occurred_at == datetime(2026, 1, 18, 0, 0, tzinfo=timezone.utc)
    assert metric_candidate.proposed_temporal_confidence == "approximate"

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "workiq"


def test_discover_candidates_source_workiq_records_auth_gap_when_runtime_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    monkeypatch.setattr(
        "src.m365.discovery.workiq_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(
                enabled=True,
                workiq_queries={"weekly_events": "Find this week's decisions."},
            ),
        ),
    )

    class FakeBridge:
        def probe(self):
            return SimpleNamespace(
                available=False,
                has_workiq=False,
                has_workiq_cli=False,
                server_tools={},
            )

        def ask_workiq(self, question, **_kwargs):
            raise AssertionError(f"ask_workiq should not be called when unavailable: {question}")

        def last_mcp_error(self):
            return None

    monkeypatch.setattr("src.m365.discovery.workiq_pipeline.AgencyBridge", FakeBridge)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "workiq",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "workiq"
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "auth_failure"
    assert "Agency CLI is unavailable" in payload["gaps"][0]["detail"]
    assert load_pending_candidates("acme", programs_root=programs_root) == ()

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "workiq"


def test_discover_candidates_source_teams_stages_candidates_and_collapses_duplicate_message_hits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        "\n".join(
            [
                "workstreams:",
                "  - id: ws1",
                "    name: Storage",
                "    signal_sources:",
                "      workiq_keywords:",
                "        - Gen9 rollout",
                "      teams_chats:",
                "        - display_name: Gen9 war room",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.m365.discovery.teams_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(enabled=True, workiq_queries={"unused": "placeholder"}),
        ),
    )

    class FakeBridge:
        def probe(self):
            return SimpleNamespace(
                available=True,
                has_workiq=True,
                has_workiq_cli=False,
                server_tools={"workiq": ("ask_work_iq",)},
            )

        def last_mcp_error(self):
            return None

    class FakeReader:
        def __init__(self, _bridge):
            pass

        def search_messages(self, *, channel, query, since, limit):
            assert channel == "all"
            assert since == "2026-01-01"
            assert limit == 25
            if query == "Gen9 rollout":
                return SimpleNamespace(
                    records=(
                        TeamsMessageRecord(
                            source_id="msg-1",
                            channel="Gen9 war room",
                            sender="pm@microsoft.com",
                            sent_at="2026-06-10T12:00:00Z",
                            web_url="https://teams.example/messages/1",
                            preview="Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT",
                            thread_id="thread-1",
                            conversation_id=None,
                            title=None,
                        ),
                        TeamsMessageRecord(
                            source_id="msg-2",
                            channel="Gen9 war room",
                            sender="pm@microsoft.com",
                            sent_at="2026-06-10T13:00:00Z",
                            web_url="https://teams.example/messages/2",
                            preview="Risk: rollout still blocked | risk_id=risk:gen9 | title=Rollout blocker | severity=high | owner_person_id=person:pm1",
                            thread_id="thread-2",
                            conversation_id=None,
                            title=None,
                        ),
                    ),
                )
            if query == "Gen9 war room":
                return SimpleNamespace(
                    records=(
                        TeamsMessageRecord(
                            source_id="msg-3",
                            channel="Gen9 war room",
                            sender="pm@microsoft.com",
                            sent_at="2026-06-10T14:00:00Z",
                            web_url="https://teams.example/messages/3",
                            preview="Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT",
                            thread_id="thread-3",
                            conversation_id=None,
                            title=None,
                        ),
                    ),
                )
            raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr("src.m365.discovery.teams_pipeline.AgencyBridge", FakeBridge)
    monkeypatch.setattr("src.m365.discovery.teams_pipeline.TeamsReader", FakeReader)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "teams",
            "--from",
            "2026",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "teams"
    assert payload["candidate_count"] == 2
    assert payload["gap_count"] == 0
    assert payload["recorded"] is True

    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 2
    assert {candidate.pipeline for candidate in pending} == {"teams"}
    assert {candidate.source_ref.ref_type for candidate in pending} == {"teams_message"}
    assert all(candidate.source_ref.vault_hash for candidate in pending)
    assert any(len(candidate.corroborating_refs) == 1 for candidate in pending)

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "teams"


def test_discover_candidates_source_teams_records_auth_gap_when_runtime_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        "\n".join(
            [
                "workstreams:",
                "  - id: ws1",
                "    name: Storage",
                "    signal_sources:",
                "      workiq_keywords:",
                "        - Gen9 rollout",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.m365.discovery.teams_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(enabled=True, workiq_queries={"unused": "placeholder"}),
        ),
    )

    class FakeBridge:
        def probe(self):
            return SimpleNamespace(
                available=False,
                has_workiq=False,
                has_workiq_cli=False,
                server_tools={},
            )

        def last_mcp_error(self):
            return None

    monkeypatch.setattr("src.m365.discovery.teams_pipeline.AgencyBridge", FakeBridge)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "teams",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "teams"
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "auth_failure"
    assert "Agency CLI is unavailable" in payload["gaps"][0]["detail"]
    assert load_pending_candidates("acme", programs_root=programs_root) == ()

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "teams"


def test_discover_candidates_source_outlook_stages_candidates_and_collapses_duplicate_mail_hits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        "\n".join(
            [
                "workstreams:",
                "  - id: ws1",
                "    name: Storage",
                "    signal_sources:",
                "      email_subject_filters:",
                "        - Gen9 rollout",
                "      email_threads:",
                "        - display_name: Gen9 weekly",
                "          thread_id: thread-1",
                "      workiq_keywords:",
                "        - rollout blocker",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.m365.discovery.outlook_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(enabled=True, workiq_queries={"unused": "placeholder"}),
        ),
    )

    class FakeBridge:
        def probe(self):
            return SimpleNamespace(
                available=True,
                has_workiq=True,
                has_workiq_cli=False,
                server_tools={"workiq": ("search_emails",)},
            )

        def last_mcp_error(self):
            return None

    class FakeMailClient:
        def __init__(self, _bridge):
            pass

        def search_emails(self, *, query, limit, allow_cli_fallback, timeout_seconds):
            assert limit == 25
            # P4-22: CLI fallback enabled + 300s timeout to cover workiq.exe exit-code-1 behavior
            assert allow_cli_fallback is True
            assert timeout_seconds == 300
            if query == "Gen9 rollout":
                return SimpleNamespace(
                    records=(
                        MailRecord(
                            source_id="mail-1",
                            subject="Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT",
                            sender="pm@microsoft.com",
                            recipients=("lead@microsoft.com",),
                            received_at="2026-06-10T12:00:00Z",
                            web_url="https://outlook.example/messages/1",
                            preview="Metric: Deployment snapshot | kpi_id=kpi:deployments | value=7 | unit=count | window_end=2026-06-09 | dimensions=ring:prod",
                            thread_id="thread-1",
                            conversation_id="conv-1",
                        ),
                        MailRecord(
                            source_id="mail-2",
                            subject="Risk review",
                            sender="pm@microsoft.com",
                            recipients=("lead@microsoft.com",),
                            received_at="2026-06-10T13:00:00Z",
                            web_url="https://outlook.example/messages/2",
                            preview="Risk: rollout still blocked | risk_id=risk:gen9 | title=Rollout blocker | severity=high | owner_person_id=person:pm1",
                            thread_id="thread-2",
                            conversation_id="conv-2",
                        ),
                    )
                )
            if query == "Gen9 weekly":
                return SimpleNamespace(
                    records=(
                        MailRecord(
                            source_id="mail-3",
                            subject="Decision: Gen9 rollout approved | decision_id=decision:gen9 | title=Gen9 rollout | decided_by=person:pm1 | forum=LT",
                            sender="pm@microsoft.com",
                            recipients=("lead@microsoft.com",),
                            received_at="2026-06-10T14:00:00Z",
                            web_url="https://outlook.example/messages/3",
                            preview="General status update",
                            thread_id="thread-3",
                            conversation_id="conv-3",
                        ),
                    )
                )
            if query == "rollout blocker":
                return SimpleNamespace(records=())
            raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr("src.m365.discovery.outlook_pipeline.AgencyBridge", FakeBridge)
    monkeypatch.setattr("src.m365.discovery.outlook_pipeline.GraphMailClient", FakeMailClient)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "outlook",
            "--from",
            "2026",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "outlook"
    assert payload["candidate_count"] == 3
    assert payload["gap_count"] == 0
    assert payload["recorded"] is True

    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 3
    assert {candidate.pipeline for candidate in pending} == {"outlook"}
    assert {candidate.source_ref.ref_type for candidate in pending} == {"email"}
    assert all(candidate.source_ref.vault_hash for candidate in pending)
    assert any(len(candidate.corroborating_refs) == 1 for candidate in pending)
    metric_candidate = next(candidate for candidate in pending if candidate.proposed_event_type == "metric.observed.v1")
    assert metric_candidate.proposed_occurred_at == datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc)
    assert metric_candidate.proposed_temporal_confidence == "approximate"

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_proposed.v1"]
    assert events[0].payload["pipeline"] == "outlook"


def test_discover_candidates_source_outlook_records_auth_gap_when_runtime_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        "\n".join(
            [
                "workstreams:",
                "  - id: ws1",
                "    name: Storage",
                "    signal_sources:",
                "      email_subject_filters:",
                "        - Gen9 rollout",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.m365.discovery.outlook_pipeline.load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="1",
            id="acme",
            name="Acme",
            m365=M365Config(enabled=True, workiq_queries={"unused": "placeholder"}),
        ),
    )

    class FakeBridge:
        def probe(self):
            return SimpleNamespace(
                available=False,
                has_workiq=False,
                has_workiq_cli=False,
                server_tools={},
            )

        def last_mcp_error(self):
            return None

    monkeypatch.setattr("src.m365.discovery.outlook_pipeline.AgencyBridge", FakeBridge)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "discover",
            "candidates",
            "--program",
            "acme",
            "--source",
            "outlook",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "outlook"
    assert payload["candidate_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["gap_kind"] == "auth_failure"
    assert "Agency CLI is unavailable" in payload["gaps"][0]["detail"]
    assert load_pending_candidates("acme", programs_root=programs_root) == ()

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1"]
    assert events[0].payload["pipeline"] == "outlook"


def _write_minimal_pptx(path: Path, slides: list[list[str]]) -> None:
    with ZipFile(path, "w") as archive:
        for index, lines in enumerate(slides, start=1):
            xml_lines = "".join(f"<a:p><a:r><a:t>{_xml_escape(line)}</a:t></a:r></a:p>" for line in lines)
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                (
                    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                    "<p:sld xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
                    "xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">"
                    f"<p:cSld><p:spTree><p:sp><p:txBody>{xml_lines}</p:txBody></p:sp></p:spTree></p:cSld>"
                    "</p:sld>"
                ),
            )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
