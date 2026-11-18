"""RELO-W6.11 characterization: governed typed feedback vs. ungoverned learning paths.

These tests pin current behavior only; D19 decides which ungoverned paths are retired.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.ai.edit_learner import append_edit_patterns, build_edit_patterns, read_edit_patterns
from src.ai.learning_distiller import LearningDistiller
from src.core.config_loader import EditorialRules, VerbositySettings
from src.core.feedback.trust_profile_store import _read_edit_patterns as trust_profile_read_edit_patterns
from src.core.review_packs import StructuredFeedback, record_structured_feedback


def _editorial_rules() -> EditorialRules:
    return EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=("as a result",),
        banned_openings=("This week",),
        verbosity=VerbositySettings(
            workstream_blurb_max_sentences=3,
            workstream_blurb_max_words=60,
            exec_bullet_max_words=25,
            exec_max_bullets=3,
            scorecard_summary_max_sentences=3,
        ),
    )


def test_governed_structured_feedback_is_typed_jsonl_under_reviews(tmp_path: Path) -> None:
    captured_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    feedback = StructuredFeedback(
        kind="risk-fix",
        target_id="workstream:deployment",
        correction="Risk is amber, not red.",
        captured_at=captured_at,
        reviewed_by="dri@example.com",
    )

    record_structured_feedback(feedback, "acme", 7, programs_root=tmp_path)

    lines = (tmp_path / "acme" / "reviews" / "feedback_issue_007.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {
            "kind": "risk-fix",
            "target_id": "workstream:deployment",
            "correction": "Risk is amber, not red.",
            "captured_at": captured_at.isoformat(),
            "reviewed_by": "dri@example.com",
        }
    ]


def test_learning_distiller_without_client_proposes_nothing_and_leaves_rules_unchanged() -> None:
    rules = _editorial_rules()

    distillation = LearningDistiller(client=None).distill(editorial_rules=rules, tracking_reports=())

    assert distillation.proposals == ()
    assert rules == _editorial_rules()


def test_edit_patterns_journal_has_two_independent_readers(tmp_path: Path) -> None:
    patterns = build_edit_patterns(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=77,
        recorded_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        draft_exec_summary_text="",
        confirmed_exec_summary_text="",
        draft_workstream_blurbs={"deployment": "ETA risk remains elevated and requires escalation."},
        confirmed_workstream_blurbs={"deployment": "Deployment risk remains elevated and needs escalation now."},
        draft_prompt_versions={"deployment": "workstream_blurb.v1"},
        draft_ai_confidences={"deployment": "high"},
        draft_trace_run_id="acme_weekly:issue-077:20260508T120000Z",
    )
    append_edit_patterns("acme", patterns, programs_root=tmp_path)

    learner_view = read_edit_patterns("acme", programs_root=tmp_path)
    trust_view = trust_profile_read_edit_patterns("acme", programs_root=tmp_path)

    assert len(learner_view) == len(trust_view) == 1
    assert trust_view[0].issue_number == learner_view[0].issue_number == 77
    assert trust_view[0].task_type == learner_view[0].task_type == "workstream_blurb"
    assert trust_view[0].author_override_magnitude == learner_view[0].author_override_magnitude
    assert not (tmp_path / "acme" / "reviews").exists()
