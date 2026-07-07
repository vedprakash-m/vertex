from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.edit_learner import (
    EditLearnerError,
    _load_prompt_learning_trace_records,
    _pattern_from_record,
    append_edit_patterns,
    build_edit_patterns,
    load_recent_edit_patterns,
    read_edit_patterns,
    summarize_recent_calibration,
    summarize_recent_confidence_bands,
    summarize_recent_models,
    summarize_recent_prompt_version_confidence_bands,
    summarize_recent_prompt_version_models,
    summarize_recent_prompt_versions,
)
from src.commands.report import _edit_pattern_context_lines
from src.core.models import Confidence


def test_build_and_load_recent_edit_patterns(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    patterns = build_edit_patterns(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        recorded_at=recorded_at,
        draft_exec_summary_text="",
        confirmed_exec_summary_text="",
        draft_workstream_blurbs={"deployment": "ETA risk remains elevated and requires escalation."},
        confirmed_workstream_blurbs={"deployment": "Deployment risk remains elevated and needs escalation now."},
        draft_prompt_versions={"deployment": "workstream_blurb.v1"},
        draft_ai_confidences={"deployment": Confidence.HIGH.value},
        draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
    )
    append_edit_patterns("acme", patterns, programs_root=programs_root)

    recent = load_recent_edit_patterns("acme", section_id="deployment", programs_root=programs_root)
    calibration = summarize_recent_calibration("acme", programs_root=programs_root)
    confidence_bands = summarize_recent_confidence_bands("acme", programs_root=programs_root)
    prompt_summaries = summarize_recent_prompt_versions("acme", programs_root=programs_root)
    prompt_confidence_summaries = summarize_recent_prompt_version_confidence_bands(
        "acme",
        programs_root=programs_root,
    )
    context_lines = _edit_pattern_context_lines("acme", workstream_ids=("deployment",), programs_root=programs_root)

    assert len(recent) == 1
    assert recent[0].section_id == "deployment"
    assert recent[0].task_type == "workstream_blurb"
    assert recent[0].prompt_version == "workstream_blurb.v1"
    assert recent[0].ai_confidence == Confidence.HIGH
    assert recent[0].trace_run_id == "acme_weekly:issue-077:20260508T120000Z"
    assert recent[0].author_override_magnitude is not None
    assert recent[0].author_override_magnitude > 0
    assert "Author edits" in recent[0].summary
    assert calibration[0].task_type == "workstream_blurb"
    assert calibration[0].sample_count == 1
    assert confidence_bands[0].task_type == "workstream_blurb"
    assert confidence_bands[0].ai_confidence == Confidence.HIGH.value
    assert confidence_bands[0].sample_count == 1
    assert prompt_summaries[0].task_type == "workstream_blurb"
    assert prompt_summaries[0].prompt_version == "workstream_blurb.v1"
    assert prompt_summaries[0].sample_count == 1
    assert prompt_confidence_summaries[0].task_type == "workstream_blurb"
    assert prompt_confidence_summaries[0].prompt_version == "workstream_blurb.v1"
    assert prompt_confidence_summaries[0].ai_confidence == Confidence.HIGH.value
    assert prompt_confidence_summaries[0].sample_count == 1
    assert context_lines[0].startswith("Recent calibration [workstream_blurb]: score=")
    assert context_lines[1].startswith("Recent confidence calibration [workstream_blurb/high]: score=")
    assert context_lines[2].startswith("Recent prompt confidence [workstream_blurb.v1/high]: score=")
    assert context_lines[3].startswith("Recent prompt performance [workstream_blurb.v1]: score=")
    assert context_lines[4:] == (f"Recent confirmed edit pattern [deployment]: {recent[0].summary}",)


def test_summarize_recent_prompt_versions_groups_patterns_by_task_and_prompt_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
            )[0],
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=78,
                recorded_at=recorded_at.replace(day=9),
                draft_exec_summary_text="AI exec summary draft two.",
                confirmed_exec_summary_text="Confirmed exec summary reworked draft two more heavily.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v2"},
                draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
            )[0],
        ),
        programs_root=programs_root,
    )

    summaries = summarize_recent_prompt_versions("acme", task_type="exec_summary", programs_root=programs_root)
    prompt_confidence_summaries = summarize_recent_prompt_version_confidence_bands(
        "acme",
        task_type="exec_summary",
        programs_root=programs_root,
    )

    assert len(summaries) == 2
    assert {summary.prompt_version for summary in summaries} == {"exec_summary_drafter.v1", "exec_summary_drafter.v2"}
    assert all(summary.task_type == "exec_summary" for summary in summaries)
    assert all(summary.sample_count == 1 for summary in summaries)
    assert len(prompt_confidence_summaries) == 2
    assert {
        (summary.prompt_version, summary.ai_confidence)
        for summary in prompt_confidence_summaries
    } == {
        ("exec_summary_drafter.v1", Confidence.MEDIUM.value),
        ("exec_summary_drafter.v2", Confidence.HIGH.value),
    }


def test_load_prompt_learning_trace_records_rejects_unknown_task_type(tmp_path: Path) -> None:
    edition_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "ai"
    edition_root.mkdir(parents=True)
    trace_path = edition_root / "llm_trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-10T10:00:00+00:00",
                "run_id": "run-1",
                "edition": "acme_weekly",
                "model": "gpt-4.1",
                "prompt_version": "bad.v1",
                "metadata": {
                    "edition_id": "acme_weekly",
                    "issue_number": 77,
                    "section_id": "exec_summary",
                    "task_type": "bad_task",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    pattern = build_edit_patterns(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        recorded_at=datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc),
        draft_exec_summary_text="AI draft summary.",
        confirmed_exec_summary_text="Confirmed summary.",
        draft_workstream_blurbs={},
        confirmed_workstream_blurbs={},
    )[0]

    with pytest.raises(EditLearnerError, match="metadata.task_type must be one of"):
        _load_prompt_learning_trace_records((pattern,), programs_root=tmp_path / "programs")


def test_summarize_recent_model_guidance_joins_exact_and_legacy_traces(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary lightly tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
                draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
            )[0],
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=78,
                recorded_at=recorded_at.replace(day=9),
                draft_exec_summary_text="AI exec summary draft two is nearly final.",
                confirmed_exec_summary_text="AI exec summary draft two is nearly final with one new blocker.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v2"},
                draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
            )[0],
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        "\n".join(
            json.dumps(payload)
            for payload in (
                {
                    "timestamp": "2026-05-08T12:00:00+00:00",
                    "run_id": "acme_weekly:issue-077:20260508T120000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "exec_summary_drafter.v1",
                    "model": "gpt-4.1",
                    "deployment": "aoai-eastus",
                    "metadata": {
                        "issue_number": 77,
                        "section_id": "exec_summary",
                        "task_type": "exec_summary",
                    },
                },
                {
                    "timestamp": "2026-05-09T12:00:00+00:00",
                    "run_id": "acme_weekly:issue-078:20260509T120000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "exec_summary_drafter.v2",
                    "model": "gpt-4.1",
                    "deployment": "aoai-westus",
                    "metadata": {
                        "issue_number": 78,
                        "section_id": "exec_summary",
                        "task_type": "exec_summary",
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    model_summaries = summarize_recent_models(
        "acme",
        task_type="exec_summary",
        programs_root=programs_root,
    )
    prompt_model_summaries = summarize_recent_prompt_version_models(
        "acme",
        task_type="exec_summary",
        programs_root=programs_root,
    )

    assert len(model_summaries) == 1
    assert model_summaries[0].task_type == "exec_summary"
    assert model_summaries[0].model == "gpt-4.1"
    assert model_summaries[0].deployment_count == 2
    assert model_summaries[0].sample_count == 2
    assert len(prompt_model_summaries) == 2
    assert {
        (summary.prompt_version, summary.model, summary.deployment_count, summary.sample_count)
        for summary in prompt_model_summaries
    } == {
        ("exec_summary_drafter.v1", "gpt-4.1", 1, 1),
        ("exec_summary_drafter.v2", "gpt-4.1", 1, 1),
    }


def test_summarize_recent_models_rejects_missing_prompt_learning_trace_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
                draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
            )[0],
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-08T12:00:00+00:00",
                "run_id": "acme_weekly:issue-077:20260508T120000Z",
                "edition": "acme_weekly",
                "prompt_version": "exec_summary_drafter.v1",
                "model": "gpt-4.1",
                "deployment": "aoai-eastus",
                "metadata": {
                    "issue_number": 77,
                    "task_type": "exec_summary",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Prompt learning trace metadata must include section_id as a non-empty string"):
        summarize_recent_models(
            "acme",
            task_type="exec_summary",
            programs_root=programs_root,
        )


def test_summarize_recent_models_rejects_missing_prompt_learning_trace_root_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
                draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
            )[0],
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "run_id": "acme_weekly:issue-077:20260508T120000Z",
                "edition": "acme_weekly",
                "prompt_version": "exec_summary_drafter.v1",
                "model": "gpt-4.1",
                "deployment": "aoai-eastus",
                "metadata": {
                    "issue_number": 77,
                    "section_id": "exec_summary",
                    "task_type": "exec_summary",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Prompt learning trace must include timestamp as an ISO-8601 datetime"):
        summarize_recent_models(
            "acme",
            task_type="exec_summary",
            programs_root=programs_root,
        )


def test_summarize_recent_models_rejects_missing_prompt_learning_trace_metadata(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
                draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
            )[0],
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-08T12:00:00+00:00",
                "run_id": "acme_weekly:issue-077:20260508T120000Z",
                "edition": "acme_weekly",
                "prompt_version": "exec_summary_drafter.v1",
                "model": "gpt-4.1",
                "deployment": "aoai-eastus",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Prompt learning trace must include metadata as an object"):
        summarize_recent_models(
            "acme",
            task_type="exec_summary",
            programs_root=programs_root,
        )


def test_summarize_recent_models_rejects_invalid_prompt_learning_trace_json(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
                draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
            )[0],
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(EditLearnerError, match="Prompt learning trace journal at .*llm_trace.jsonl contains invalid JSON"):
        summarize_recent_models(
            "acme",
            task_type="exec_summary",
            programs_root=programs_root,
        )


def test_summarize_recent_models_rejects_non_string_prompt_learning_trace_error(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    append_edit_patterns(
        "acme",
        (
            build_edit_patterns(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                recorded_at=recorded_at,
                draft_exec_summary_text="AI exec summary draft one.",
                confirmed_exec_summary_text="Confirmed exec summary tightened draft one.",
                draft_workstream_blurbs={},
                confirmed_workstream_blurbs={},
                draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
                draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
                draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
            )[0],
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme_weekly" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-08T12:00:00+00:00",
                "run_id": "acme_weekly:issue-077:20260508T120000Z",
                "edition": "acme_weekly",
                "prompt_version": "exec_summary_drafter.v1",
                "model": "gpt-4.1",
                "deployment": "aoai-eastus",
                "error": {"message": "timeout"},
                "metadata": {
                    "issue_number": 77,
                    "section_id": "exec_summary",
                    "task_type": "exec_summary",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Prompt learning trace error must be a string"):
        summarize_recent_models(
            "acme",
            task_type="exec_summary",
            programs_root=programs_root,
        )


def test_read_edit_patterns_rejects_invalid_numeric_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": "not-a-number",
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
                "author_override_magnitude": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern issue_number must be an integer"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_read_edit_patterns_rejects_boolean_integer_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": True,
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
                "author_override_magnitude": 0.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern issue_number must be an integer"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_read_edit_patterns_rejects_boolean_float_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
                "author_override_magnitude": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern author_override_magnitude must be a number"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_read_edit_patterns_quarantines_invalid_jsonl_lines(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json\n", encoding="utf-8")

    result = read_edit_patterns("acme", programs_root=programs_root)
    assert result == ()
    quarantine_dir = path.parent / "quarantine"
    assert quarantine_dir.exists()
    quarantined = tuple(quarantine_dir.glob("edit_patterns.*.jsonl"))
    assert len(quarantined) == 1


def test_read_edit_patterns_rejects_invalid_confidence_values(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
                "ai_confidence": "certain",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern ai_confidence must be a valid confidence value"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_read_edit_patterns_rejects_invalid_recorded_at_values(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "section_id": "exec_summary",
                "recorded_at": "not-a-timestamp",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern recorded_at must be an ISO-8601 datetime"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_read_edit_patterns_rejects_non_string_required_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": ["not", "a", "string"],
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern summary must be a string"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_pattern_from_record_rejects_non_object_payloads() -> None:
    with pytest.raises(EditLearnerError, match="Edit pattern record must be an object"):
        _pattern_from_record([])  # type: ignore[arg-type]


def test_read_edit_patterns_rejects_non_string_optional_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
                "task_type": {"unexpected": "object"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern task_type must be a string"):
        read_edit_patterns("acme", programs_root=programs_root)


def test_read_edit_patterns_rejects_unknown_task_type(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "journal" / "edit_patterns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "program_id": "acme",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "section_id": "exec_summary",
                "recorded_at": "2026-05-08T12:00:00+00:00",
                "summary": "Author edits changed 4 words.",
                "before_excerpt": "before",
                "after_excerpt": "after",
                "before_word_count": 10,
                "after_word_count": 12,
                "task_type": "fabricated_task",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EditLearnerError, match="Edit pattern task_type must be one of"):
        read_edit_patterns("acme", programs_root=programs_root)
