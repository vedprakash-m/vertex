from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from src.ai.discovery.lt_deck_extractor import extract_lt_deck_candidates_from_pptx
from src.ai.discovery.lt_deck_extractor import LTDeckExtractorError


def test_extract_lt_deck_candidates_from_pptx_reads_structured_marker_lines(tmp_path: Path) -> None:
    deck_path = tmp_path / "2025-03-20 Acme LT Update.pptx"
    _write_minimal_pptx(
        deck_path,
        [
            [
                "Acme LT Update",
                "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice,person:bob | forum=LT",
                "Risk: Supplier lead time remains unstable | risk_id=risk:supplier-lead | title=Supplier delay | severity=high | owner_person_id=person:alice | workstream_id=workstream:supply",
                "Metric: Deployment snapshot | kpi_id=kpi:deployments | value=12 | unit=count | window_end=2025-03-20 | dimensions=ring:prod",
            ],
            [
                "Milestones",
                "Milestone: kind=created | milestone_id=milestone:gen9-ga | name=Gen9 GA | target_date=2025-09-30 | workstream_id=workstream:gen9",
                "Milestone: kind=revised | milestone_id=milestone:gen9-ga | new_target_date=2025-10-15 | prior_target_date=2025-09-30 | reason=Vendor slip",
                "Milestone: kind=completed | milestone_id=milestone:gen9-ga | completed_on=2025-10-22 | evidence=Launch complete",
            ],
        ],
    )

    batch = extract_lt_deck_candidates_from_pptx(
        program_id="acme",
        source_path=deck_path,
        relative_path="2025/2025-03-20 Acme LT Update.pptx",
        batch_id="batch-1",
    )

    assert len(batch.candidates) == 6
    assert [candidate.proposed_event_type for candidate in batch.candidates] == [
        "decision.made.v1",
        "risk.raised.v1",
        "metric.observed.v1",
        "milestone.created.v1",
        "milestone.date_revised.v1",
        "milestone.completed.v1",
    ]
    assert batch.candidates[0].proposed_payload["decision_id"] == "decision:gen9-ship"
    assert batch.candidates[0].proposed_payload["decided_by"] == ["person:alice", "person:bob"]
    assert batch.candidates[0].source_ref.slide_number == 1
    assert batch.candidates[1].proposed_payload["risk_id"] == "risk:supplier-lead"
    assert batch.candidates[2].proposed_payload["kpi_id"] == "kpi:deployments"
    assert batch.candidates[2].proposed_occurred_at == datetime(2025, 3, 20, 0, 0, tzinfo=timezone.utc)
    assert batch.candidates[2].proposed_temporal_confidence == "approximate"
    assert batch.candidates[3].proposed_payload["milestone_id"] == "milestone:gen9-ga"
    assert batch.candidates[4].proposed_payload["new_target_date"] == "2025-10-15"
    assert batch.candidates[4].source_ref.slide_number == 2
    assert batch.candidates[5].proposed_payload["completed_on"] == "2025-10-22"


def test_extract_lt_deck_candidates_from_pptx_ignores_unstructured_lines(tmp_path: Path) -> None:
    deck_path = tmp_path / "2025-03-20 Acme LT Update.pptx"
    _write_minimal_pptx(deck_path, [["Acme LT Update", "General status with no structured markers"]])

    batch = extract_lt_deck_candidates_from_pptx(
        program_id="acme",
        source_path=deck_path,
        relative_path="2025/2025-03-20 Acme LT Update.pptx",
        batch_id="batch-1",
    )

    assert batch.candidates == ()
    assert batch.warnings == ()


def test_extract_lt_deck_candidates_from_pptx_can_continue_past_bad_marker_lines(tmp_path: Path) -> None:
    deck_path = tmp_path / "2025-03-20 Acme LT Update.pptx"
    _write_minimal_pptx(
        deck_path,
        [[
            "Acme LT Update",
            "Risk:",
            "Decision: Ship Gen9 in September | decision_id=decision:gen9-ship | title=Gen9 ship date | decided_by=person:alice | forum=LT",
        ]],
    )

    batch = extract_lt_deck_candidates_from_pptx(
        program_id="acme",
        source_path=deck_path,
        relative_path="2025/2025-03-20 Acme LT Update.pptx",
        batch_id="batch-1",
        continue_on_marker_errors=True,
    )

    assert [candidate.proposed_event_type for candidate in batch.candidates] == ["decision.made.v1"]
    assert batch.warnings == ("slide 1 (Acme LT Update): Risk: marker is empty.",)


def test_extract_lt_deck_candidates_from_pptx_rejects_corrupt_archives(tmp_path: Path) -> None:
    deck_path = tmp_path / "2025-03-20 Acme LT Update.pptx"
    deck_path.write_text("not-a-real-pptx", encoding="utf-8")

    try:
        extract_lt_deck_candidates_from_pptx(
            program_id="acme",
            source_path=deck_path,
            relative_path="2025/2025-03-20 Acme LT Update.pptx",
            batch_id="batch-1",
        )
    except LTDeckExtractorError as error:
        assert "could not be parsed" in str(error)
    else:
        raise AssertionError("Expected LTDeckExtractorError for corrupt PPTX input")


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
