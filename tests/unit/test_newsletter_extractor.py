from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.ai.discovery.newsletter_extractor import extract_newsletter_candidates


def test_extract_newsletter_candidates_reads_structured_marker_lines_from_html(tmp_path: Path) -> None:
    source_path = tmp_path / "2025-03-20_issue_051.html"
    source_path.write_text(
        """
        <html><body>
        <h1>Acme Weekly</h1>
        <p>Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=LT</p>
        <p>Risk: Supplier lead time remains unstable | risk_id=risk:supplier-lead | title=Supplier delay | severity=high | owner_person_id=person:alice | workstream_id=workstream:supply</p>
        <p>Milestone: kind=revised | milestone_id=milestone:gen9-ga | new_target_date=2025-10-15 | prior_target_date=2025-09-30 | reason=Vendor slip</p>
        <p>Metric: KPI table snapshot | kpi_id=kpi:deployments | value=42 | unit=count | window_end=2025-03-20 | dimensions=ring:prod</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    batch = extract_newsletter_candidates(
        program_id="acme",
        source_path=source_path,
        relative_path="2025/2025-03-20_issue_051.html",
        batch_id="batch-1",
    )

    assert len(batch.candidates) == 5
    assert [candidate.proposed_event_type for candidate in batch.candidates] == [
        "artifact.published.v1",
        "decision.made.v1",
        "risk.raised.v1",
        "milestone.date_revised.v1",
        "metric.observed.v1",
    ]
    assert batch.candidates[0].source_ref.issue_number == 51
    assert batch.candidates[0].source_ref.section is None
    assert batch.candidates[0].proposed_payload["artifact_id"] == "published_artifact:issue-051"
    assert batch.candidates[1].source_ref.section == "line:2"
    assert batch.candidates[4].proposed_payload == {
        "kpi_id": "kpi:deployments",
        "value": 42.0,
        "unit": "count",
        "window_end": "2025-03-20",
        "dimensions": {"ring": "prod"},
    }
    assert batch.candidates[4].proposed_occurred_at == datetime(2025, 3, 20, 0, 0, tzinfo=timezone.utc)
    assert batch.candidates[4].proposed_temporal_confidence == "approximate"


def test_extract_newsletter_candidates_ignores_unstructured_lines(tmp_path: Path) -> None:
    source_path = tmp_path / "2025-03-20_issue_051.eml"
    source_path.write_text("<html><body><p>General update without markers</p></body></html>", encoding="utf-8")

    batch = extract_newsletter_candidates(
        program_id="acme",
        source_path=source_path,
        relative_path="2025/2025-03-20_issue_051.eml",
        batch_id="batch-1",
    )

    assert len(batch.candidates) == 1
    assert batch.candidates[0].proposed_event_type == "artifact.published.v1"
    assert batch.candidates[0].proposed_payload["artifact_id"] == "published_artifact:issue-051"


def test_extract_newsletter_candidates_parses_real_corpus_filename_patterns(tmp_path: Path) -> None:
    source_path = tmp_path / "Program Hygiene _ Issue 76 _ April 10, 2026.eml"
    source_path.write_text(
        "\n".join(
            [
                "Subject: Program Hygiene | Issue 76 | April 10, 2026",
                "From: sender@example.com",
                "Date: Fri, 10 Apr 2026 21:12:07 +0000",
                "Message-ID: <issue-76@example.com>",
                "Content-Type: text/plain; charset=utf-8",
                "",
                "Decision: Keep LT cadence | decision_id=decision:lt-cadence | title=Keep LT cadence | decided_by=person:alice | forum=Weekly",
            ]
        ),
        encoding="utf-8",
    )

    batch = extract_newsletter_candidates(
        program_id="acme",
        source_path=source_path,
        relative_path="acme_newsletters/Program Hygiene _ Issue 76 _ April 10, 2026.eml",
        batch_id="batch-1",
    )

    assert [candidate.proposed_event_type for candidate in batch.candidates] == [
        "artifact.published.v1",
        "decision.made.v1",
    ]
    assert batch.candidates[0].proposed_payload["artifact_id"] == "published_artifact:issue-076"
    assert batch.candidates[0].proposed_payload["period_start"] == "2026-04-10"
    assert batch.candidates[1].source_ref.issue_number == 76


def test_extract_newsletter_candidates_pdf_stages_publication_artifact_only(tmp_path: Path) -> None:
    source_path = tmp_path / "Adventure-Acme Program Update _ Issue 67 _ August 7, 2025 - Alex Vance - Outlook.pdf"
    source_path.write_bytes(b"%PDF-1.4\n%minimal\n")

    batch = extract_newsletter_candidates(
        program_id="acme",
        source_path=source_path,
        relative_path="acme_newsletters/Adventure-Acme Program Update _ Issue 67 _ August 7, 2025 - Alex Vance - Outlook.pdf",
        batch_id="batch-1",
    )

    assert len(batch.candidates) == 1
    assert batch.candidates[0].proposed_event_type == "artifact.published.v1"
    assert batch.candidates[0].proposed_payload["artifact_id"] == "published_artifact:issue-067"


def test_extract_newsletter_candidates_reads_structured_marker_lines_from_pdf(tmp_path: Path) -> None:
    source_path = tmp_path / "Adventure-Acme Program Update _ Issue 67 _ August 7, 2025 - Alex Vance - Outlook.pdf"
    source_path.write_bytes(
        _build_text_pdf(
            [
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=PDF",
            ]
        )
    )

    batch = extract_newsletter_candidates(
        program_id="acme",
        source_path=source_path,
        relative_path="acme_newsletters/Adventure-Acme Program Update _ Issue 67 _ August 7, 2025 - Alex Vance - Outlook.pdf",
        batch_id="batch-1",
    )

    assert [candidate.proposed_event_type for candidate in batch.candidates] == [
        "artifact.published.v1",
        "decision.made.v1",
    ]
    assert batch.candidates[1].proposed_payload["decision_id"] == "decision:gen9-ship"


def _build_text_pdf(lines: Iterable[str]) -> bytes:
    text_commands = ["BT", "/F1 12 Tf", "72 720 Td"]
    for index, line in enumerate(lines):
        if index > 0:
            text_commands.append("0 -18 Td")
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        text_commands.append(f"({escaped}) Tj")
    text_commands.append("ET")
    stream = "\n".join(text_commands).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    chunks: list[bytes] = [b"%PDF-1.4\n"]
    offsets: list[int] = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    chunks.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    return b"".join(chunks)
