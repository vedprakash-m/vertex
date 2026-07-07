from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.draft_reviewer import DraftReviewerError, _suggestion_from_payload, build_review_artifact, build_suggestion_tracking_report, render_review_markdown, render_tracking_summary
from src.ai.draft_reviewer import review_artifact_from_payload, review_draft
from src.core.archive_store import write_confirmed_issue
from src.core.config_loader import EditorialRules, KustoQuerySettings, KustoSettings, VoiceContractSettings
from src.core.models import DeltaKind, DeltaSet, EditionType, FreshnessReport, ProgramContext, ReportData, ReviewSection, ReviewState, ReviewStatus, RiskLevel, RunManifest, Snapshot
from src.core.models import SnapshotItem, WorkItem


EDITION_NAME = "acme_weekly"


def test_review_draft_returns_categorized_suggestions_and_skip_message(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    prior_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    current_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    current_item = _work_item(
        work_item_id=900001,
        title="Deployment velocity telemetry stabilization",
        risk=RiskLevel.HIGH,
        fetched_at=current_as_of,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot(issue_number=1, as_of=prior_as_of, items=(_snapshot_item(current_item, risk=RiskLevel.HIGH),)),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\nDeployment velocity telemetry stabilization remains high risk.\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of),
        archive_root=archive_root,
    )

    report = ReportData(
        issue_number=2,
        edition=EditionType.DETAILED,
        generated_at=current_as_of,
        ado_data_as_of=current_as_of,
        program=ProgramContext(
            program_name="Acme",
            mission="Track deployment execution",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(current_item,),
        deltas=DeltaSet(
            issue_number=2,
            previous_issue_number=1,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=1,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Deployment velocity is still a concern and needs more evidence.",
        workstream_blurbs={"deployment": "Deployment velocity remains elevated and needs a tighter narrative for leaders." * 4},
        freshness=FreshnessReport(issue_number=2, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=2,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
        manifest_id="review-manifest",
    )

    review_report, info_messages = review_draft(
        report=report,
        draft_markdown="# Draft\nCurrent draft still lacks quantified telemetry details.\n",
        program_context=None,
        editorial_rules=_editorial_rules(),
        kusto_settings=_kusto_settings(),
        edition_name=EDITION_NAME,
        archive_root=archive_root,
    )

    categories = {suggestion.category for suggestion in review_report.suggestions}

    assert review_report.data_gaps >= 1
    assert review_report.cross_issue_flags >= 1
    assert review_report.structural_notes >= 1
    assert categories == {"data_gap", "cross_issue", "structural"}
    assert any("Leadership question simulation skipped" in message for message in info_messages)

    rendered = render_review_markdown(review_report, info_messages)

    assert "AI DRAFT REVIEW" in rendered
    assert "DATA GAPS" in rendered
    assert "CROSS-ISSUE CONTINUITY" in rendered
    assert "STRUCTURAL" in rendered


def test_review_draft_generates_leadership_question_when_profiles_exist(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    current_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    current_item = _work_item(
        work_item_id=900001,
        title="Deployment velocity telemetry stabilization",
        risk=RiskLevel.HIGH,
        fetched_at=current_as_of,
    )
    report = ReportData(
        issue_number=2,
        edition=EditionType.DETAILED,
        generated_at=current_as_of,
        ado_data_as_of=current_as_of,
        program=ProgramContext(
            program_name="Acme",
            mission="Track deployment execution",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(current_item,),
        deltas=DeltaSet(
            issue_number=2,
            previous_issue_number=1,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=1,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Deployment velocity needs a clear decision path.",
        workstream_blurbs={},
        freshness=FreshnessReport(issue_number=2, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=2,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
        manifest_id="review-manifest",
    )

    leadership_reader = type(
        "LeadershipReader",
        (),
        {
            "name": "Executive Reader",
            "role": "PM Lead",
            "cares_about": ("accuracy", "exec summary quality"),
            "prefers": "Lead with wins + deltas.",
            "pet_peeves": ("verbosity",),
        },
    )()
    richer_program_context = type(
        "ProgramContextWithLeadership",
        (),
        {"leadership_readers": (leadership_reader,)},
    )()

    review_report, info_messages = review_draft(
        report=report,
        draft_markdown="# Draft\nDeployment velocity needs a clear decision path.\n",
        program_context=richer_program_context,
        editorial_rules=_editorial_rules(),
        kusto_settings=_kusto_settings(),
        edition_name=EDITION_NAME,
        archive_root=archive_root,
    )

    leadership_suggestions = [suggestion for suggestion in review_report.suggestions if suggestion.category == "leadership_question"]

    assert review_report.leadership_questions == 1
    assert not info_messages
    assert leadership_suggestions[0].reader_name == "Executive Reader"
    assert "accuracy" in leadership_suggestions[0].suggestion_text.lower()


def test_build_review_artifact_captures_section_snapshot_and_query_ids(tmp_path: Path) -> None:
    report, review_report, info_messages = _review_fixture(tmp_path)

    artifact = build_review_artifact(
        review_report,
        info_messages=info_messages,
        report=report,
        rendered_kusto_query_ids=("existing-query",),
    )

    assert artifact.issue_number == 2
    assert artifact.reviewed_sections["exec_summary"] == report.exec_summary_text
    assert artifact.reviewed_sections["ws:deployment"] == report.workstream_blurbs["deployment"]
    assert artifact.rendered_kusto_query_ids == ("existing-query",)


def test_review_draft_flags_nova_authentic_voice_drift_as_structural_note(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    current_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    current_item = _work_item(
        work_item_id=900001,
        title="Deployment safety closure",
        risk=RiskLevel.MEDIUM,
        fetched_at=current_as_of,
    )
    report = ReportData(
        issue_number=2,
        edition=EditionType.DETAILED,
        generated_at=current_as_of,
        ado_data_as_of=current_as_of,
        program=ProgramContext(
            program_name="Acme",
            mission="Track deployment execution",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(current_item,),
        deltas=DeltaSet(
            issue_number=2,
            previous_issue_number=1,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=1,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="The rest of the scorecard is materially narrower and the main job here is to keep the ramp story credible.",
        workstream_blurbs={"deployment": "Deployment is no longer the broad program blocker it was earlier in the spring."},
        freshness=FreshnessReport(issue_number=2, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=2,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
        manifest_id="review-manifest",
    )

    writing_style = type(
        "WritingStyle",
        (),
        {
            "voice": "Lane-first and decision-oriented.",
            "structure": "Lead with the blocking lane, concrete condition, checkpoint, and consequence.",
            "risk_framing": {},
            "preferred_patterns": (),
        },
    )()
    richer_program_context = type(
        "ProgramContextWithVoice",
        (),
        {
            "program_name": "Adventure + DD on PF",
            "current_phase": "Acme Ramp P1",
            "glossary": {"Acme": "Storage on Northwind", "SCHIE": "Storage Cluster Health Issues and Escalations"},
            "workstreams": (),
            "workstream_owners": (),
            "recurring_themes": ("Deployment Velocity", "SCHIE Gaps"),
            "key_dependency_chain": (),
            "writing_style": writing_style,
            "leadership_readers": (),
        },
    )()

    review_report, _ = review_draft(
        report=report,
        draft_markdown="# Draft\nAbstract Acme summary.\n",
        program_context=richer_program_context,
        editorial_rules=_editorial_rules(),
        kusto_settings=_kusto_settings(),
        edition_name=EDITION_NAME,
        archive_root=archive_root,
    )

    structural_texts = [suggestion.suggestion_text for suggestion in review_report.suggestions if suggestion.category == "structural"]

    assert any("authentic voice" in text.lower() for text in structural_texts)


def test_build_suggestion_tracking_report_marks_changed_sections_and_new_kusto_queries(tmp_path: Path) -> None:
    report, review_report, info_messages = _review_fixture(tmp_path)
    artifact = build_review_artifact(review_report, info_messages=info_messages, report=report)

    confirmed_report = replace(
        report,
        exec_summary_text="Deployment velocity now includes a decision path and quantified status.",
        workstream_blurbs=dict(report.workstream_blurbs),
    )

    tracking_report = build_suggestion_tracking_report(
        artifact,
        confirmed_report=confirmed_report,
        rendered_kusto_query_ids=("velocity-p50",),
    )

    outcomes_by_action = {
        outcome.suggestion.action: outcome
        for outcome in tracking_report.suggestions
        if outcome.suggestion.action is not None
    }
    workstream_outcome = next(
        outcome for outcome in tracking_report.suggestions if outcome.suggestion.section_id == "ws:deployment"
    )

    assert tracking_report.accepted >= 1
    assert tracking_report.dismissed >= 1
    assert outcomes_by_action["add_kusto:velocity-p50"].outcome == "accepted"
    assert "was absent in the reviewed draft" in outcomes_by_action["add_kusto:velocity-p50"].reason
    assert workstream_outcome.outcome == "dismissed"
    assert render_tracking_summary(tracking_report).startswith("AI review tracking:")


def test_review_artifact_from_payload_rejects_invalid_numeric_fields() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact issue_number must be an integer"):
        review_artifact_from_payload(
            {
                "issue_number": "not-a-number",
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_object_payloads() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact payload must be an object"):
        review_artifact_from_payload([])  # type: ignore[arg-type]


def test_suggestion_from_payload_rejects_non_object_payloads() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact suggestion payload must be an object"):
        _suggestion_from_payload([])  # type: ignore[arg-type]


def test_suggestion_from_payload_rejects_missing_required_fields() -> None:
    base_payload = {
        "category": "data_gap",
        "section_id": "exec_summary",
        "suggestion_text": "Lead with the delta.",
        "confidence": "medium",
    }
    missing_fields = (
        ("category", "Review artifact suggestion payload must include category as a string"),
        ("section_id", "Review artifact suggestion payload must include section_id as a string"),
        (
            "suggestion_text",
            "Review artifact suggestion payload must include suggestion_text as a string",
        ),
        ("confidence", "Review artifact suggestion payload must include confidence as a string"),
    )

    for field_name, message in missing_fields:
        payload = {key: value for key, value in base_payload.items() if key != field_name}
        with pytest.raises(DraftReviewerError, match=message):
            _suggestion_from_payload(payload)


def test_suggestion_from_payload_scrubs_pii_from_user_visible_fields() -> None:
    suggestion = _suggestion_from_payload(
        {
            "category": "leadership_question",
            "section_id": "exec_summary",
            "suggestion_text": "Ask foo@gmail.com to confirm the blocker.",
            "confidence": "medium",
            "reader_name": "foo@gmail.com",
        }
    )

    assert "foo@gmail.com" not in suggestion.suggestion_text
    assert "foo@gmail.com" not in (suggestion.reader_name or "")
    assert "[PII-FILTERED-EMAIL]" in suggestion.suggestion_text
    assert suggestion.reader_name == "[PII-FILTERED-EMAIL]"


def test_review_artifact_from_payload_rejects_boolean_numeric_fields() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact issue_number must be an integer"):
        review_artifact_from_payload(
            {
                "issue_number": True,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )



def test_review_artifact_from_payload_rejects_numeric_string_issue_number() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact issue_number must be an integer"):
        review_artifact_from_payload(
            {
                "issue_number": "2",
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "info_messages": [],
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_numeric_string_review_report_counts() -> None:
    with pytest.raises(DraftReviewerError, match=r"Review artifact review_report\.data_gaps must be an integer"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": "0",
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "info_messages": [],
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )

def test_review_artifact_from_payload_rejects_invalid_suggestion_confidence() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact suggestion.confidence must be a valid confidence value"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "structural",
                            "section_id": "exec_summary",
                            "suggestion_text": "Lead with the delta.",
                            "confidence": "certain",
                        }
                    ],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 1,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_invalid_suggestion_category() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact suggestion.category must be a valid suggestion category"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "bogus",
                            "section_id": "exec_summary",
                            "suggestion_text": "Lead with the delta.",
                            "confidence": "medium",
                        }
                    ],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 1,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_string_suggestion_action() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact suggestion.action must be a string when provided"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "data_gap",
                            "section_id": "exec_summary",
                            "suggestion_text": "Lead with the delta.",
                            "confidence": "medium",
                            "action": {"bad": "value"},
                        }
                    ],
                    "data_gaps": 1,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_string_suggestion_reader_name() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact suggestion.reader_name must be a string when provided"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "leadership_question",
                            "section_id": "exec_summary",
                            "suggestion_text": "What decision is blocked?",
                            "confidence": "medium",
                            "reader_name": ["bad-reader"],
                        }
                    ],
                    "data_gaps": 0,
                    "leadership_questions": 1,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_list_suggestions() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact review_report.suggestions must be a list"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": "not-a-list",
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_review_report() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact must include review_report as an object"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_suggestions() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact review_report must include suggestions as a list"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_review_report_issue_number() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact must include review_report.issue_number as an integer"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_issue_number() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact must include issue_number as an integer"):
        review_artifact_from_payload(
            {
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_mismatched_review_report_issue_number() -> None:
    with pytest.raises(DraftReviewerError, match=r"review_report\.issue_number must match issue_number"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 3,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_review_report_counts() -> None:
    base_payload = {
        "issue_number": 2,
        "info_messages": [],
        "review_report": {
            "issue_number": 2,
            "suggestions": [],
            "data_gaps": 0,
            "leadership_questions": 0,
            "cross_issue_flags": 0,
            "structural_notes": 0,
        },
        "reviewed_sections": {},
        "rendered_kusto_query_ids": [],
    }

    missing_fields = (
        ("data_gaps", "Review artifact must include review_report.data_gaps as an integer"),
        (
            "leadership_questions",
            "Review artifact must include review_report.leadership_questions as an integer",
        ),
        ("cross_issue_flags", "Review artifact must include review_report.cross_issue_flags as an integer"),
        ("structural_notes", "Review artifact must include review_report.structural_notes as an integer"),
    )

    for field_name, message in missing_fields:
        payload = {
            **base_payload,
            "review_report": {
                key: value
                for key, value in base_payload["review_report"].items()
                if key != field_name
            },
        }

        with pytest.raises(DraftReviewerError, match=message):
            review_artifact_from_payload(payload)


def test_review_artifact_from_payload_rejects_mismatched_review_report_counts() -> None:
    with pytest.raises(
        DraftReviewerError,
        match=r"review_report\.data_gaps must match the parsed suggestion counts",
    ):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "data_gap",
                            "section_id": "exec_summary",
                            "suggestion_text": "Need clearer evidence.",
                            "confidence": "medium",
                            "reader_name": None,
                            "action": None,
                        }
                    ],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {"exec_summary": "Summary text"},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_object_suggestion_entries() -> None:
    with pytest.raises(DraftReviewerError, match=r"Review artifact review_report.suggestions\[\] must be an object"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": ["bad-entry"],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_object_reviewed_sections() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact reviewed_sections must be an object"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": ["bad-sections"],
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_info_messages() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact must include info_messages as a list"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_reviewed_sections() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact must include reviewed_sections as an object"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_missing_rendered_kusto_query_ids() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact must include rendered_kusto_query_ids as a list"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
            }
        )


def test_review_artifact_from_payload_rejects_non_list_info_messages() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact info_messages must be a list"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": "not-a-list",
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_string_info_messages() -> None:
    with pytest.raises(DraftReviewerError, match=r"Review artifact info_messages\[\] must be a string"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [123],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_string_reviewed_sections_entries() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact reviewed_sections value must be a string"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {"exec_summary": False},
                "rendered_kusto_query_ids": [],
            }
        )


def test_review_artifact_from_payload_rejects_non_list_rendered_kusto_query_ids() -> None:
    with pytest.raises(DraftReviewerError, match="Review artifact rendered_kusto_query_ids must be a list"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": "not-a-list",
            }
        )


def test_review_artifact_from_payload_rejects_non_string_rendered_kusto_query_ids() -> None:
    with pytest.raises(DraftReviewerError, match=r"Review artifact rendered_kusto_query_ids\[\] must be a string"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": [False],
            }
        )


def test_review_artifact_from_payload_rejects_unknown_rendered_kusto_query_ids() -> None:
    with pytest.raises(DraftReviewerError, match="rendered_kusto_query_ids contains unknown ids: fabricated-query"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {},
                "rendered_kusto_query_ids": ["fabricated-query"],
            },
            valid_rendered_kusto_query_ids=("velocity-p50",),
        )


def test_review_artifact_from_payload_rejects_unknown_reviewed_sections() -> None:
    with pytest.raises(DraftReviewerError, match="reviewed_sections contains unknown section ids: ws:fabricated"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [],
                    "data_gaps": 0,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {"ws:fabricated": "invented section"},
                "rendered_kusto_query_ids": [],
            },
            valid_reviewed_section_ids=("exec_summary", "ws:deployment"),
        )


def test_review_artifact_from_payload_rejects_unknown_suggestion_section_id() -> None:
    with pytest.raises(DraftReviewerError, match=r"suggestion\.section_id contains unknown section id: ws:fabricated"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "data_gap",
                            "section_id": "ws:fabricated",
                            "suggestion_text": "Need clearer evidence.",
                            "confidence": "medium",
                            "evidence": "No supporting refs.",
                            "reader_name": None,
                            "action": None,
                        }
                    ],
                    "data_gaps": 1,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {"exec_summary": "Summary text"},
                "rendered_kusto_query_ids": [],
            },
            valid_reviewed_section_ids=("exec_summary", "ws:deployment"),
        )


def test_review_artifact_from_payload_rejects_unknown_suggestion_kusto_query_id() -> None:
    with pytest.raises(DraftReviewerError, match=r"suggestion\.action contains unknown Kusto query id: fabricated-query"):
        review_artifact_from_payload(
            {
                "issue_number": 2,
                "info_messages": [],
                "review_report": {
                    "issue_number": 2,
                    "suggestions": [
                        {
                            "category": "data_gap",
                            "section_id": "exec_summary",
                            "suggestion_text": "Add the missing query.",
                            "confidence": "medium",
                            "reader_name": None,
                            "action": "add_kusto:fabricated-query",
                        }
                    ],
                    "data_gaps": 1,
                    "leadership_questions": 0,
                    "cross_issue_flags": 0,
                    "structural_notes": 0,
                },
                "reviewed_sections": {"exec_summary": "Summary text"},
                "rendered_kusto_query_ids": [],
            },
            valid_reviewed_section_ids=("exec_summary",),
            valid_rendered_kusto_query_ids=("velocity-p50",),
        )


def _editorial_rules() -> EditorialRules:
    return EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=28,
        banned_phrases=(),
        banned_openings=(),
        verbosity=type("Verbosity", (), {
            "workstream_blurb_max_sentences": 4,
            "workstream_blurb_max_words": 20,
            "exec_bullet_max_words": 30,
            "exec_max_bullets": 4,
            "scorecard_summary_max_sentences": 4,
        })(),
        voice_contract=VoiceContractSettings(
            applies_to_editions=("acme_weekly",),
            program_tokens=("acme", "northwind", "adventure"),
            abstract_phrases=("materially narrower", "broader program blocker"),
            synthetic_delta_prefixes=("NEW", "CLOSED", "RISK_UP", "RISK_DOWN", "ETA", "OWNER"),
            decision_lead_terms=("blocking", "checkpoint", "conditional", "eta", "gate", "target"),
            static_concrete_terms=("azure core", "schie", "northwind", "acme"),
            exec_summary_bucket_prefixes=("acme:",),
            objective_preamble_prefixes=("the objective of the acme program is", "northwind clusters live within azure"),
        ),
    )


def _kusto_settings() -> KustoSettings:
    return KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Velocity",
                section="Deployment Velocity",
                render_as="chart_image",
                confidence="high",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url="https://adventure.kusto.windows.net",
            ),
        ),
    )


def _work_item(*, work_item_id: int, title: str, risk: RiskLevel, fetched_at: datetime) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="maintainer@example.com",
        area_path="One\\Adventure\\Acme\\Deployment",
        iteration_path="FY26\\Sprint 20",
        target_date=None,
        risk_level=risk,
        tags=["Safety"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=fetched_at,
    )


def _snapshot(issue_number: int, as_of: datetime, items: tuple[SnapshotItem, ...]) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=as_of,
        ado_data_as_of=as_of,
        edition_type=EditionType.DETAILED,
        items=items,
        scorecards=(),
    )


def _snapshot_item(item: WorkItem, *, risk: RiskLevel) -> SnapshotItem:
    return SnapshotItem(
        id=item.id,
        type=item.type,
        title=item.title,
        state=item.state,
        assigned_to=item.assigned_to,
        area_path=item.area_path,
        target_date=item.target_date,
        risk_level=risk,
        tags=list(item.tags),
    )


def _manifest(issue_number: int, as_of: datetime) -> RunManifest:
    return RunManifest(
        manifest_id=f"review-{issue_number}",
        issue_number=issue_number,
        edition=EDITION_NAME,
        started_at=as_of,
        ended_at=as_of,
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )


def _review_fixture(tmp_path: Path) -> tuple[ReportData, object, tuple[str, ...]]:
    archive_root = tmp_path / "archive"
    prior_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    current_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    current_item = _work_item(
        work_item_id=900001,
        title="Deployment velocity telemetry stabilization",
        risk=RiskLevel.HIGH,
        fetched_at=current_as_of,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot(issue_number=1, as_of=prior_as_of, items=(_snapshot_item(current_item, risk=RiskLevel.HIGH),)),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\nDeployment velocity telemetry stabilization remains high risk.\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of),
        archive_root=archive_root,
    )

    report = ReportData(
        issue_number=2,
        edition=EditionType.DETAILED,
        generated_at=current_as_of,
        ado_data_as_of=current_as_of,
        program=ProgramContext(
            program_name="Acme",
            mission="Track deployment execution",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(current_item,),
        deltas=DeltaSet(
            issue_number=2,
            previous_issue_number=1,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=1,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Deployment velocity is still a concern and needs more evidence.",
        workstream_blurbs={"deployment": "Deployment velocity remains elevated and needs a tighter narrative for leaders." * 4},
        freshness=FreshnessReport(issue_number=2, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=2,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
        manifest_id="review-manifest",
    )

    review_report, info_messages = review_draft(
        report=report,
        draft_markdown="# Draft\nCurrent draft still lacks quantified telemetry details.\n",
        program_context=None,
        editorial_rules=_editorial_rules(),
        kusto_settings=_kusto_settings(),
        edition_name=EDITION_NAME,
        archive_root=archive_root,
    )
    return report, review_report, info_messages
