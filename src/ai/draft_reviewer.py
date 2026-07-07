from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.core.archive_store import read_archive_index
from src.core.config_loader import EditorialRules, KustoSettings
from src.core.models import Confidence, ReportData, RiskLevel
from src.core.snapshot_store import read_snapshot
from src.core.voice_validator import find_voice_violations
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


SUGGESTION_CATEGORIES = {"data_gap", "leadership_question", "cross_issue", "structural"}


class DraftReviewerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewSuggestion:
    category: Literal["data_gap", "leadership_question", "cross_issue", "structural"]
    section_id: str
    suggestion_text: str
    confidence: Confidence
    reader_name: str | None = None
    action: str | None = None


@dataclass(frozen=True, slots=True)
class DraftReviewReport:
    issue_number: int
    suggestions: tuple[ReviewSuggestion, ...]
    data_gaps: int
    leadership_questions: int
    cross_issue_flags: int
    structural_notes: int


@dataclass(frozen=True, slots=True)
class DraftReviewArtifact:
    issue_number: int
    info_messages: tuple[str, ...]
    review_report: DraftReviewReport
    reviewed_sections: dict[str, str]
    rendered_kusto_query_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewSuggestionOutcome:
    index: int
    suggestion: ReviewSuggestion
    outcome: Literal["accepted", "dismissed"]
    reason: str


@dataclass(frozen=True, slots=True)
class SuggestionTrackingReport:
    issue_number: int
    accepted: int
    dismissed: int
    suggestions: tuple[ReviewSuggestionOutcome, ...]


def review_draft(
    *,
    report: ReportData,
    draft_markdown: str,
    program_context: Any | None,
    editorial_rules: EditorialRules,
    kusto_settings: KustoSettings,
    edition_name: str,
    archive_root: Path,
) -> tuple[DraftReviewReport, tuple[str, ...]]:
    info_messages = _leadership_skip_messages(program_context)
    prior_snapshots = _load_recent_confirmed_snapshots(
        edition_name=edition_name,
        current_issue_number=report.issue_number,
        archive_root=archive_root,
    )
    suggestions = (
        *_find_data_gaps(report=report, draft_markdown=draft_markdown, kusto_settings=kusto_settings),
        *_find_leadership_questions(program_context=program_context),
        *_find_cross_issue_flags(report=report, prior_snapshots=prior_snapshots),
        *_find_structural_notes(
            report=report,
            editorial_rules=editorial_rules,
            program_context=program_context,
            edition_name=edition_name,
        ),
    )
    review_report = DraftReviewReport(
        issue_number=report.issue_number,
        suggestions=tuple(suggestions),
        data_gaps=sum(1 for suggestion in suggestions if suggestion.category == "data_gap"),
        leadership_questions=sum(1 for suggestion in suggestions if suggestion.category == "leadership_question"),
        cross_issue_flags=sum(1 for suggestion in suggestions if suggestion.category == "cross_issue"),
        structural_notes=sum(1 for suggestion in suggestions if suggestion.category == "structural"),
    )
    return review_report, info_messages


def render_review_markdown(review_report: DraftReviewReport, info_messages: tuple[str, ...] = ()) -> str:
    lines = [
        f"AI DRAFT REVIEW - Issue {review_report.issue_number}",
        "=" * 32,
        "",
    ]
    lines.extend(info_messages)
    if info_messages:
        lines.append("")
    lines.extend(_render_category("DATA GAPS", _category_suggestions(review_report, "data_gap")))
    lines.append("")
    lines.extend(_render_category("LEADERSHIP QUESTIONS", _category_suggestions(review_report, "leadership_question")))
    lines.append("")
    lines.extend(_render_category("CROSS-ISSUE CONTINUITY", _category_suggestions(review_report, "cross_issue")))
    lines.append("")
    lines.extend(_render_category("STRUCTURAL", _category_suggestions(review_report, "structural")))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_review_summary(review_report: DraftReviewReport, info_messages: tuple[str, ...] = ()) -> str:
    summary = (
        f"Review: {review_report.data_gaps} data gaps, "
        f"{review_report.leadership_questions} leadership questions, "
        f"{review_report.cross_issue_flags} cross-issue flags, "
        f"{review_report.structural_notes} structural notes."
    )
    if not info_messages:
        return summary
    return "\n".join((summary, *info_messages))


def build_review_artifact(
    review_report: DraftReviewReport,
    *,
    info_messages: tuple[str, ...] = (),
    report: ReportData,
    rendered_kusto_query_ids: tuple[str, ...] = (),
) -> DraftReviewArtifact:
    return DraftReviewArtifact(
        issue_number=review_report.issue_number,
        info_messages=tuple(info_messages),
        review_report=review_report,
        reviewed_sections=_section_texts(report),
        rendered_kusto_query_ids=tuple(rendered_kusto_query_ids),
    )
def review_artifact_from_payload(
    payload: dict[str, Any],
    *,
    valid_reviewed_section_ids: tuple[str, ...] = (),
    valid_rendered_kusto_query_ids: tuple[str, ...] = (),
) -> DraftReviewArtifact:
    if not isinstance(payload, dict):
        raise DraftReviewerError("Review artifact payload must be an object.")
    if "issue_number" not in payload:
        raise DraftReviewerError("Review artifact must include issue_number as an integer.")
    if "review_report" not in payload:
        raise DraftReviewerError("Review artifact must include review_report as an object.")
    review_payload = payload.get("review_report")
    if not isinstance(review_payload, dict):
        raise DraftReviewerError("Review artifact review_report must be an object.")
    if "suggestions" not in review_payload:
        raise DraftReviewerError("Review artifact review_report must include suggestions as a list.")
    suggestions_payload = review_payload.get("suggestions")
    if not isinstance(suggestions_payload, list):
        raise DraftReviewerError("Review artifact review_report.suggestions must be a list.")
    if "info_messages" not in payload:
        raise DraftReviewerError("Review artifact must include info_messages as a list.")
    info_messages_payload = payload.get("info_messages")
    if not isinstance(info_messages_payload, list):
        raise DraftReviewerError("Review artifact info_messages must be a list.")
    if "reviewed_sections" not in payload:
        raise DraftReviewerError("Review artifact must include reviewed_sections as an object.")
    reviewed_sections_payload = payload.get("reviewed_sections")
    if not isinstance(reviewed_sections_payload, dict):
        raise DraftReviewerError("Review artifact reviewed_sections must be an object.")
    if "rendered_kusto_query_ids" not in payload:
        raise DraftReviewerError("Review artifact must include rendered_kusto_query_ids as a list.")
    rendered_kusto_query_ids_payload = payload.get("rendered_kusto_query_ids")
    if not isinstance(rendered_kusto_query_ids_payload, list):
        raise DraftReviewerError("Review artifact rendered_kusto_query_ids must be a list.")
    rendered_kusto_query_ids = tuple(
        _required_review_artifact_string(query_id, field_name="rendered_kusto_query_ids[]")
        for query_id in rendered_kusto_query_ids_payload
    )
    if valid_rendered_kusto_query_ids:
        allowed_query_ids = set(valid_rendered_kusto_query_ids)
        unknown_query_ids = tuple(
            query_id for query_id in rendered_kusto_query_ids if query_id not in allowed_query_ids
        )
        if unknown_query_ids:
            raise DraftReviewerError(
                "Review artifact rendered_kusto_query_ids contains unknown ids: "
                + ", ".join(unknown_query_ids)
            )
    suggestions: list[ReviewSuggestion] = []
    allowed_section_ids = set(valid_reviewed_section_ids)
    for entry in suggestions_payload:
        if not isinstance(entry, dict):
            raise DraftReviewerError("Review artifact review_report.suggestions[] must be an object.")
        suggestion = _suggestion_from_payload(entry)
        if valid_reviewed_section_ids and suggestion.section_id not in allowed_section_ids:
            raise DraftReviewerError(
                f"Review artifact suggestion.section_id contains unknown section id: {suggestion.section_id}"
            )
        if valid_rendered_kusto_query_ids and suggestion.action and suggestion.action.startswith("add_kusto:"):
            query_id = suggestion.action.split(":", maxsplit=1)[1].strip()
            if query_id not in allowed_query_ids:
                raise DraftReviewerError(
                    f"Review artifact suggestion.action contains unknown Kusto query id: {query_id}"
                )
        suggestions.append(suggestion)
    reviewed_sections = {
        _required_review_artifact_string(section_id, field_name="reviewed_sections key"): _required_review_artifact_string(
            text,
            field_name="reviewed_sections value",
        )
        for section_id, text in reviewed_sections_payload.items()
    }
    if valid_reviewed_section_ids:
        unknown_section_ids = tuple(
            section_id for section_id in reviewed_sections if section_id not in allowed_section_ids
        )
        if unknown_section_ids:
            raise DraftReviewerError(
                "Review artifact reviewed_sections contains unknown section ids: "
                + ", ".join(unknown_section_ids)
            )

    issue_number = _coerce_review_artifact_int(payload.get("issue_number"), field_name="issue_number")
    review_report_issue_number = _coerce_review_artifact_int(
        _required_review_artifact_field(
            review_payload,
            field_name="review_report.issue_number",
            message="Review artifact must include review_report.issue_number as an integer.",
        ),
        field_name="review_report.issue_number",
    )
    if review_report_issue_number != issue_number:
        raise DraftReviewerError("Review artifact review_report.issue_number must match issue_number.")

    expected_counts = {
        "data_gaps": sum(1 for suggestion in suggestions if suggestion.category == "data_gap"),
        "leadership_questions": sum(1 for suggestion in suggestions if suggestion.category == "leadership_question"),
        "cross_issue_flags": sum(1 for suggestion in suggestions if suggestion.category == "cross_issue"),
        "structural_notes": sum(1 for suggestion in suggestions if suggestion.category == "structural"),
    }
    for field_name, expected_count in expected_counts.items():
        count = _coerce_review_artifact_int(
            _required_review_artifact_field(
                review_payload,
                field_name=f"review_report.{field_name}",
                message=f"Review artifact must include review_report.{field_name} as an integer.",
            ),
            field_name=f"review_report.{field_name}",
        )
        if count != expected_count:
            raise DraftReviewerError(
                f"Review artifact review_report.{field_name} must match the parsed suggestion counts."
            )
        expected_counts[field_name] = count

    return DraftReviewArtifact(
        issue_number=issue_number,
        info_messages=tuple(
            _required_review_artifact_string(message, field_name="info_messages[]") for message in info_messages_payload
        ),
        review_report=DraftReviewReport(
            issue_number=review_report_issue_number,
            suggestions=tuple(suggestions),
            data_gaps=expected_counts["data_gaps"],
            leadership_questions=expected_counts["leadership_questions"],
            cross_issue_flags=expected_counts["cross_issue_flags"],
            structural_notes=expected_counts["structural_notes"],
        ),
        reviewed_sections=reviewed_sections,
        rendered_kusto_query_ids=rendered_kusto_query_ids,
    )


def _required_review_artifact_field(payload: dict[str, Any], *, field_name: str, message: str) -> Any:
    if field_name.rsplit(".", 1)[-1] not in payload:
        raise DraftReviewerError(message)
    return payload.get(field_name.rsplit(".", 1)[-1])


def _coerce_review_artifact_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DraftReviewerError(f"Review artifact {field_name} must be an integer.")
    return value


def _required_review_artifact_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise DraftReviewerError(f"Review artifact {field_name} must be a string.")
    return value


def _required_suggestion_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftReviewerError(f"Review artifact {field_name} must be a non-empty string.")
    normalized = value.strip()
    try:
        processed = process_generated_text(normalized)
    except AIPipelineError as error:
        raise DraftReviewerError(f"Review artifact {field_name} rejected by safety pipeline: {error}") from error
    if not processed.text:
        raise DraftReviewerError(f"Review artifact {field_name} must be a non-empty string.")
    return processed.text


def _coerce_suggestion_category(value: Any) -> Literal["data_gap", "leadership_question", "cross_issue", "structural"]:
    from typing import cast

    category = _required_suggestion_string(value, field_name="suggestion.category")
    if category not in SUGGESTION_CATEGORIES:
        raise DraftReviewerError("Review artifact suggestion.category must be a valid suggestion category.")
    return cast("Literal['data_gap', 'leadership_question', 'cross_issue', 'structural']", category)


def _optional_suggestion_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DraftReviewerError(f"Review artifact {field_name} must be a string when provided.")
    text = value.strip()
    if not text:
        return None
    try:
        processed = process_generated_text(text)
    except AIPipelineError as error:
        raise DraftReviewerError(f"Review artifact {field_name} rejected by safety pipeline: {error}") from error
    return processed.text or None


def _coerce_suggestion_confidence(value: Any) -> Confidence:
    text = _required_suggestion_string(value, field_name="suggestion.confidence")
    try:
        return Confidence(text.strip().lower())
    except ValueError as error:
        raise DraftReviewerError("Review artifact suggestion.confidence must be a valid confidence value.") from error


def build_suggestion_tracking_report(
    review_artifact: DraftReviewArtifact,
    *,
    confirmed_report: ReportData,
    rendered_kusto_query_ids: tuple[str, ...] = (),
) -> SuggestionTrackingReport:
    current_sections = _section_texts(confirmed_report)
    current_query_ids = set(rendered_kusto_query_ids)
    tracked_suggestions = tuple(
        _track_suggestion(
            index=index,
            suggestion=suggestion,
            reviewed_sections=review_artifact.reviewed_sections,
            current_sections=current_sections,
            reviewed_kusto_query_ids=set(review_artifact.rendered_kusto_query_ids),
            current_kusto_query_ids=current_query_ids,
        )
        for index, suggestion in enumerate(review_artifact.review_report.suggestions, start=1)
    )
    return SuggestionTrackingReport(
        issue_number=review_artifact.issue_number,
        accepted=sum(1 for suggestion in tracked_suggestions if suggestion.outcome == "accepted"),
        dismissed=sum(1 for suggestion in tracked_suggestions if suggestion.outcome == "dismissed"),
        suggestions=tracked_suggestions,
    )


def render_tracking_summary(tracking_report: SuggestionTrackingReport) -> str:
    return (
        f"AI review tracking: {tracking_report.accepted} accepted, "
        f"{tracking_report.dismissed} dismissed."
    )


def _leadership_skip_messages(program_context: Any | None) -> tuple[str, ...]:
    if program_context is None:
        return (
            "INFO: Leadership question simulation skipped — no program_context.yaml found. Create one with 'vertex onboard' or see §8.2.",
        )
    leadership_readers = getattr(program_context, "leadership_readers", ()) or ()
    if not leadership_readers:
        return (
            "INFO: No leadership_readers defined in program_context.yaml — skipping question simulation.",
        )
    return ()


def _load_recent_confirmed_snapshots(
    *,
    edition_name: str,
    current_issue_number: int,
    archive_root: Path,
) -> tuple[Any, ...]:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    confirmed_entries = [
        entry
        for entry in archive_index.issues
        if entry.kind == "confirmed" and entry.issue_number < current_issue_number and entry.snapshot_path is not None
    ]
    snapshots = []
    for entry in confirmed_entries[-3:]:
        assert entry.snapshot_path is not None
        snapshot_path = Path(entry.snapshot_path)
        if snapshot_path.exists():
            snapshots.append(read_snapshot(snapshot_path))
    return tuple(snapshots)


def _find_data_gaps(
    *,
    report: ReportData,
    draft_markdown: str,
    kusto_settings: KustoSettings,
) -> tuple[ReviewSuggestion, ...]:
    suggestions: list[ReviewSuggestion] = []
    high_risk_item = next((item for item in report.items if item.risk_level in {RiskLevel.HIGH, RiskLevel.MEDIUM}), None)
    if high_risk_item is not None:
        suggestions.append(
            ReviewSuggestion(
                category="data_gap",
                section_id="exec_summary",
                suggestion_text=(
                    f'"{high_risk_item.title}" is called out as a live risk without a quantified status update. '
                    "Add the current blocker count, ETA, or direct ADO link so the claim is easier to defend."
                ),
                confidence=Confidence.MEDIUM,
                action=f"add_ado:{high_risk_item.id}",
            )
        )

    if kusto_settings.enabled and kusto_settings.queries:
        rendered_text = draft_markdown.lower()
        unrendered_queries = [
            query for query in kusto_settings.queries if query.section and query.section.lower() not in rendered_text
        ]
        if unrendered_queries:
            query = unrendered_queries[0]
            suggestions.append(
                ReviewSuggestion(
                    category="data_gap",
                    section_id="exec_summary",
                    suggestion_text=(
                        f"The draft does not surface the configured {query.section} evidence. "
                        f"Add Kusto query {query.id} to back the narrative with a metric or chart."
                    ),
                    confidence=Confidence.MEDIUM,
                    action=f"add_kusto:{query.id}",
                )
            )

    return tuple(suggestions)


def _find_cross_issue_flags(
    *,
    report: ReportData,
    prior_snapshots: tuple[Any, ...],
) -> tuple[ReviewSuggestion, ...]:
    if not prior_snapshots:
        return ()

    suggestions: list[ReviewSuggestion] = []
    current_ids = {item.id for item in report.items if item.state.lower() not in TERMINAL_WORK_ITEM_STATES}
    latest_prior = prior_snapshots[-1]
    missing_follow_up = next((item for item in latest_prior.items if item.id not in current_ids), None)
    if missing_follow_up is not None:
        suggestions.append(
            ReviewSuggestion(
                category="cross_issue",
                section_id="exec_summary",
                suggestion_text=(
                    f'Issue {latest_prior.issue_number:03d} tracked "{missing_follow_up.title}" but the current draft does not give a follow-up. '
                    "Add a resolution note or explain why it dropped out of scope."
                ),
                confidence=Confidence.MEDIUM,
            )
        )

    history_by_item: dict[int, list[tuple[int, RiskLevel]]] = {}
    for snapshot in prior_snapshots:
        for item in snapshot.items:
            history_by_item.setdefault(item.id, []).append((snapshot.issue_number, item.risk_level))

    persistent_item = next(
        (
            item
            for item in report.items
            if item.risk_level in {RiskLevel.HIGH, RiskLevel.MEDIUM}
            and len(history_by_item.get(item.id, [])) == len(prior_snapshots)
        ),
        None,
    )
    if persistent_item is not None:
        first_issue = history_by_item[persistent_item.id][0][0]
        suggestions.append(
            ReviewSuggestion(
                category="cross_issue",
                section_id="exec_summary",
                suggestion_text=(
                    f'"{persistent_item.title}" has remained active since Issue {first_issue:03d}. '
                    "Add a resolution path, owner checkpoint, or escalation framing so the theme does not feel stalled."
                ),
                confidence=Confidence.MEDIUM,
            )
        )

    return tuple(suggestions)


def _find_leadership_questions(*, program_context: Any | None) -> tuple[ReviewSuggestion, ...]:
    leadership_readers = getattr(program_context, "leadership_readers", ()) or ()
    suggestions: list[ReviewSuggestion] = []
    for reader in leadership_readers:
        cares_about = tuple(getattr(reader, "cares_about", ()) or ())
        primary_focus = cares_about[0] if cares_about else "current decision risk"
        prefers = getattr(reader, "prefers", None)
        detail = f"{getattr(reader, 'name', 'Leadership')} will likely ask how the draft addresses {primary_focus}."
        if isinstance(prefers, str) and prefers.strip():
            detail += f" Match the answer to their preferred framing: {prefers.strip()}"
        suggestions.append(
            ReviewSuggestion(
                category="leadership_question",
                section_id="exec_summary",
                suggestion_text=detail,
                confidence=Confidence.MEDIUM,
                reader_name=getattr(reader, "name", None),
            )
        )
    return tuple(suggestions)


def _find_structural_notes(
    *,
    report: ReportData,
    editorial_rules: EditorialRules,
    program_context: Any | None,
    edition_name: str,
) -> tuple[ReviewSuggestion, ...]:
    suggestions: list[ReviewSuggestion] = []
    workstream_word_cap = getattr(editorial_rules.verbosity, "workstream_blurb_max_words", None)
    if isinstance(workstream_word_cap, int) and workstream_word_cap > 0:
        threshold = max(1, int(workstream_word_cap * 0.9))
        for section_id, blurb in report.workstream_blurbs.items():
            word_count = len([word for word in blurb.split() if word.strip()])
            if word_count >= threshold:
                suggestions.append(
                    ReviewSuggestion(
                        category="structural",
                        section_id=f"ws:{section_id}",
                        suggestion_text=(
                            f"Section {section_id} is {word_count}/{workstream_word_cap} words. "
                            "Trim a sentence to preserve edit margin before publish."
                        ),
                        confidence=Confidence.HIGH,
                    )
                )

    has_delta_rows = any(
        (
            report.deltas.new_items,
            report.deltas.closed_items,
            report.deltas.risk_changes,
            report.deltas.eta_changes,
            getattr(report.deltas, "owner_changes", ()),
        )
    )
    if not has_delta_rows:
        suggestions.append(
            ReviewSuggestion(
                category="structural",
                section_id="exec_summary",
                suggestion_text=(
                    "The draft does not surface a concrete delta from the prior issue. "
                    "Add what changed or trim the section so readers are not asked to reread static status."
                ),
                confidence=Confidence.HIGH,
            )
        )

    voice_violations = find_voice_violations(
        editorial_rules=editorial_rules,
        edition_name=edition_name,
        exec_summary_text=report.exec_summary_text,
        workstream_blurbs=report.workstream_blurbs,
        program_context=program_context,
    )
    for violation in voice_violations:
        section_id = "exec_summary"
        if violation.location.startswith("workstream:"):
            section_id = f"ws:{violation.location.split(':', maxsplit=1)[1]}"
        suggestions.append(
            ReviewSuggestion(
                category="structural",
                section_id=section_id,
                suggestion_text=violation.message,
                confidence=Confidence.HIGH,
            )
        )

    return tuple(suggestions)


def _category_suggestions(
    review_report: DraftReviewReport,
    category: Literal["data_gap", "leadership_question", "cross_issue", "structural"],
) -> tuple[ReviewSuggestion, ...]:
    return tuple(suggestion for suggestion in review_report.suggestions if suggestion.category == category)


def _render_category(title: str, suggestions: tuple[ReviewSuggestion, ...]) -> list[str]:
    lines = [f"{title} ({len(suggestions)})", ""]
    if not suggestions:
        lines.append("  - None")
        return lines
    for index, suggestion in enumerate(suggestions, start=1):
        lines.append(f"  {index}. [{suggestion.section_id}] {suggestion.suggestion_text}")
        if suggestion.action:
            lines.append(f"     Action: {suggestion.action}")
        if suggestion.reader_name:
            lines.append(f"     Reader: {suggestion.reader_name}")
    return lines


def _suggestion_from_payload(payload: dict[str, Any]) -> ReviewSuggestion:
    if not isinstance(payload, dict):
        raise DraftReviewerError("Review artifact suggestion payload must be an object.")
    if "category" not in payload:
        raise DraftReviewerError("Review artifact suggestion payload must include category as a string.")
    if "section_id" not in payload:
        raise DraftReviewerError("Review artifact suggestion payload must include section_id as a string.")
    if "suggestion_text" not in payload:
        raise DraftReviewerError("Review artifact suggestion payload must include suggestion_text as a string.")
    if "confidence" not in payload:
        raise DraftReviewerError("Review artifact suggestion payload must include confidence as a string.")
    return ReviewSuggestion(
        category=_coerce_suggestion_category(payload.get("category")),
        section_id=_required_suggestion_string(payload.get("section_id"), field_name="suggestion.section_id"),
        suggestion_text=_required_suggestion_string(
            payload.get("suggestion_text"),
            field_name="suggestion.suggestion_text",
        ),
        confidence=_coerce_suggestion_confidence(payload.get("confidence")),
        reader_name=_optional_suggestion_string(payload.get("reader_name"), field_name="suggestion.reader_name"),
        action=_optional_suggestion_string(payload.get("action"), field_name="suggestion.action"),
    )


def _section_texts(report: ReportData) -> dict[str, str]:
    return {
        "exec_summary": report.exec_summary_text.strip(),
        **{f"ws:{section_id}": blurb.strip() for section_id, blurb in report.workstream_blurbs.items()},
    }


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def _track_suggestion(
    *,
    index: int,
    suggestion: ReviewSuggestion,
    reviewed_sections: dict[str, str],
    current_sections: dict[str, str],
    reviewed_kusto_query_ids: set[str],
    current_kusto_query_ids: set[str],
) -> ReviewSuggestionOutcome:
    if suggestion.action and suggestion.action.startswith("add_kusto:"):
        query_id = suggestion.action.split(":", maxsplit=1)[1].strip()
        if query_id in current_kusto_query_ids and query_id not in reviewed_kusto_query_ids:
            return ReviewSuggestionOutcome(
                index=index,
                suggestion=suggestion,
                outcome="accepted",
                reason=f"Kusto query {query_id} is present in the confirmed draft and was absent in the reviewed draft.",
            )
        return ReviewSuggestionOutcome(
            index=index,
            suggestion=suggestion,
            outcome="dismissed",
            reason=f"Kusto query {query_id} is still absent from the confirmed draft.",
        )

    prior_text = _normalize_text(reviewed_sections.get(suggestion.section_id))
    current_text = _normalize_text(current_sections.get(suggestion.section_id))
    if prior_text != current_text:
        return ReviewSuggestionOutcome(
            index=index,
            suggestion=suggestion,
            outcome="accepted",
            reason=f"Section {suggestion.section_id} changed after the review was generated.",
        )
    return ReviewSuggestionOutcome(
        index=index,
        suggestion=suggestion,
        outcome="dismissed",
        reason=f"Section {suggestion.section_id} is unchanged from the reviewed draft.",
    )