from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

import portalocker

from src.core.edition_resolver import get_program_output_dir
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records, validate_jsonl_row
from src.core.models import Confidence


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
_ALLOWED_TASK_TYPES = frozenset({"exec_summary", "workstream_blurb"})

# High-risk append-only file — grows with every confirmed issue and every author override.
# Rotated at 10 MB (spec §11.3 Phase 5 / D-23) to bound on-disk footprint while
# preserving the full append-only history under ``journal/rotated/``.
_EDIT_PATTERNS_MAX_BYTES = 10 * 1024 * 1024


class EditLearnerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EditPattern:
    program_id: str
    edition_id: str
    issue_number: int
    section_id: str
    recorded_at: datetime
    summary: str
    before_excerpt: str
    after_excerpt: str
    before_word_count: int
    after_word_count: int
    task_type: str | None = None
    prompt_version: str | None = None
    ai_confidence: Confidence | None = None
    trace_run_id: str | None = None
    author_override_magnitude: float | None = None


@dataclass(frozen=True, slots=True)
class EvidenceCorrectionPattern:
    """Records a manual correction to workiq_latest as a learning signal (BL-27)."""
    program_id: str
    lane_id: str
    corrected_at: datetime
    workiq_latest_before: str | None
    workiq_latest_after: str
    risk_level_before: str | None
    risk_level_after: str | None
    ado_ids_added: tuple[str, ...]
    icm_ids_added: tuple[str, ...]
    source_hint: str    # "cowork_manual" | "workiq_refresh" | "local_kb"
    operator: str       # alias of the operator who made the change


_EVIDENCE_CORRECTIONS_FILENAME = "evidence_corrections.jsonl"


# ── Evidence correction patterns (ME-04) ─────────────────────────────────────

def _get_evidence_corrections_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "journal" / _EVIDENCE_CORRECTIONS_FILENAME


def _correction_to_record(pattern: EvidenceCorrectionPattern) -> dict:
    return {
        "program_id": pattern.program_id,
        "lane_id": pattern.lane_id,
        "corrected_at": pattern.corrected_at.isoformat(),
        "workiq_latest_before": pattern.workiq_latest_before,
        "workiq_latest_after": pattern.workiq_latest_after,
        "risk_level_before": pattern.risk_level_before,
        "risk_level_after": pattern.risk_level_after,
        "ado_ids_added": list(pattern.ado_ids_added),
        "icm_ids_added": list(pattern.icm_ids_added),
        "source_hint": pattern.source_hint,
        "operator": pattern.operator,
    }


def _correction_from_record(record: dict) -> "EvidenceCorrectionPattern | None":
    try:
        return EvidenceCorrectionPattern(
            program_id=record["program_id"],
            lane_id=record["lane_id"],
            corrected_at=datetime.fromisoformat(record["corrected_at"]),
            workiq_latest_before=record.get("workiq_latest_before"),
            workiq_latest_after=record["workiq_latest_after"],
            risk_level_before=record.get("risk_level_before"),
            risk_level_after=record.get("risk_level_after"),
            ado_ids_added=tuple(record.get("ado_ids_added", [])),
            icm_ids_added=tuple(record.get("icm_ids_added", [])),
            source_hint=record.get("source_hint", "unknown"),
            operator=record.get("operator", "unknown"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def append_evidence_correction(
    pattern: EvidenceCorrectionPattern,
    *,
    programs_root: Path,
) -> None:
    """Append one EvidenceCorrectionPattern to evidence_corrections.jsonl."""
    path = _get_evidence_corrections_path(pattern.program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_correction_to_record(pattern), ensure_ascii=False) + "\n"
    append_jsonl_line(path, payload)


def load_evidence_corrections(
    program_id: str,
    *,
    programs_root: Path,
    lane_id: str | None = None,
) -> list[EvidenceCorrectionPattern]:
    """Load EvidenceCorrectionPattern records, optionally filtered by lane_id."""
    path = _get_evidence_corrections_path(program_id, programs_root)
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        pattern = _correction_from_record(record)
        if pattern is None:
            continue
        if lane_id is not None and pattern.lane_id != lane_id:
            continue
        patterns.append(pattern)
    return patterns


@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    task_type: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class ConfidenceBandSummary:
    task_type: str
    ai_confidence: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class PromptVersionSummary:
    task_type: str
    prompt_version: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class PromptVersionConfidenceSummary:
    task_type: str
    prompt_version: str
    ai_confidence: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class ModelLeaderboardSummary:
    task_type: str
    model: str
    deployment_count: int
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class PromptVersionModelLeaderboardSummary:
    task_type: str
    prompt_version: str
    model: str
    deployment_count: int
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class _PromptLearningTraceRecord:
    timestamp: datetime
    run_id: str
    edition_id: str
    issue_number: int
    section_id: str
    task_type: str
    prompt_version: str | None
    model: str
    deployment: str


def get_edit_patterns_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "edit_patterns.jsonl"


def append_edit_patterns(
    program_id: str,
    patterns: tuple[EditPattern, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path | None:
    if not patterns:
        return None
    path = get_edit_patterns_path(program_id, programs_root)
    for pattern in patterns:
        payload = json.dumps(_pattern_to_record(pattern), ensure_ascii=False) + "\n"
        append_jsonl_line(path, payload, max_bytes=_EDIT_PATTERNS_MAX_BYTES)
    return path


def read_edit_patterns(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[EditPattern, ...]:
    path = get_edit_patterns_path(program_id, programs_root)
    if not path.exists():
        return ()
    patterns: list[EditPattern] = []
    for record in read_jsonl_records(path):
        # Strict field-presence gate: every edit pattern row must at least carry the
        # core identity + section + timing fields. The deeper parser below enforces
        # type correctness on these and the optional fields.
        validate_jsonl_row(
            record,
            required_fields=(
                "program_id",
                "edition_id",
                "issue_number",
                "section_id",
                "recorded_at",
                "summary",
                "before_excerpt",
                "after_excerpt",
                "before_word_count",
                "after_word_count",
            ),
            field_name="edit pattern row",
        )
        patterns.append(_pattern_from_record(record))
    patterns.sort(key=lambda pattern: (pattern.recorded_at, pattern.issue_number, pattern.section_id))
    return tuple(patterns)


def load_recent_edit_patterns(
    program_id: str,
    *,
    section_id: str | None = None,
    limit: int = 3,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[EditPattern, ...]:
    filtered = [
        pattern
        for pattern in read_edit_patterns(program_id, programs_root=programs_root)
        if section_id is None or pattern.section_id == section_id
    ]
    filtered.sort(key=lambda pattern: pattern.recorded_at, reverse=True)
    return tuple(filtered[:limit])


def build_edit_patterns(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    recorded_at: datetime,
    draft_exec_summary_text: str,
    confirmed_exec_summary_text: str,
    draft_workstream_blurbs: dict[str, str],
    confirmed_workstream_blurbs: dict[str, str],
    draft_prompt_versions: dict[str, str] | None = None,
    draft_ai_confidences: dict[str, str] | None = None,
    draft_trace_run_id: str | None = None,
) -> tuple[EditPattern, ...]:
    patterns: list[EditPattern] = []
    prompt_versions = dict(draft_prompt_versions or {})
    ai_confidences = dict(draft_ai_confidences or {})
    exec_pattern = _build_single_pattern(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        section_id="exec_summary",
        recorded_at=recorded_at,
        before_text=draft_exec_summary_text,
        after_text=confirmed_exec_summary_text,
        task_type="exec_summary",
        prompt_version=prompt_versions.get("exec_summary"),
        ai_confidence=_parse_confidence(ai_confidences.get("exec_summary")),
        trace_run_id=draft_trace_run_id,
    )
    if exec_pattern is not None:
        patterns.append(exec_pattern)

    for section_id in sorted(set(draft_workstream_blurbs) | set(confirmed_workstream_blurbs)):
        pattern = _build_single_pattern(
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            section_id=section_id,
            recorded_at=recorded_at,
            before_text=draft_workstream_blurbs.get(section_id, ""),
            after_text=confirmed_workstream_blurbs.get(section_id, ""),
            task_type="workstream_blurb",
            prompt_version=prompt_versions.get(section_id),
            ai_confidence=_parse_confidence(ai_confidences.get(section_id)),
            trace_run_id=draft_trace_run_id,
        )
        if pattern is not None:
            patterns.append(pattern)
    return tuple(patterns)


def _build_single_pattern(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    section_id: str,
    recorded_at: datetime,
    before_text: str,
    after_text: str,
    task_type: str,
    prompt_version: str | None,
    ai_confidence: Confidence | None,
    trace_run_id: str | None,
) -> EditPattern | None:
    normalized_before = _normalize_text(before_text)
    normalized_after = _normalize_text(after_text)
    if not normalized_before or not normalized_after or normalized_before == normalized_after:
        return None

    before_words = len(normalized_before.split())
    after_words = len(normalized_after.split())
    return EditPattern(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        section_id=section_id,
        recorded_at=_require_utc(recorded_at),
        summary=_summarize_change(normalized_before, normalized_after, before_words, after_words),
        before_excerpt=_excerpt(normalized_before),
        after_excerpt=_excerpt(normalized_after),
        before_word_count=before_words,
        after_word_count=after_words,
        task_type=task_type,
        prompt_version=prompt_version,
        ai_confidence=ai_confidence,
        trace_run_id=trace_run_id,
        author_override_magnitude=_author_override_magnitude(normalized_before, normalized_after),
    )


def summarize_recent_calibration(
    program_id: str,
    *,
    window_issues: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[CalibrationSummary, ...]:
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    grouped: dict[str, list[EditPattern]] = {}
    for pattern in recent_patterns:
        if pattern.task_type is None or pattern.author_override_magnitude is None:
            continue
        grouped.setdefault(pattern.task_type, []).append(pattern)

    summaries: list[CalibrationSummary] = []
    for task_type, patterns in sorted(grouped.items()):
        average_override = round(
            sum(pattern.author_override_magnitude for pattern in patterns if pattern.author_override_magnitude is not None) / len(patterns),
            4,
        )
        summaries.append(
            CalibrationSummary(
                task_type=task_type,
                sample_count=len(patterns),
                average_override_magnitude=average_override,
                calibration_score=round(max(0.0, 1.0 - average_override), 4),
            )
        )
    return tuple(summaries)


def summarize_recent_confidence_bands(
    program_id: str,
    *,
    task_type: str | None = None,
    window_issues: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ConfidenceBandSummary, ...]:
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    grouped: dict[tuple[str, str], list[EditPattern]] = {}
    for pattern in recent_patterns:
        if (
            pattern.task_type is None
            or pattern.author_override_magnitude is None
            or pattern.ai_confidence is None
            or pattern.ai_confidence == Confidence.NONE
            or (task_type is not None and pattern.task_type != task_type)
        ):
            continue
        grouped.setdefault((pattern.task_type, pattern.ai_confidence.value), []).append(pattern)

    summaries: list[ConfidenceBandSummary] = []
    for (resolved_task_type, resolved_confidence), patterns in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern in patterns if pattern.author_override_magnitude is not None)
            / len(patterns),
            4,
        )
        summaries.append(
            ConfidenceBandSummary(
                task_type=resolved_task_type,
                ai_confidence=resolved_confidence,
                sample_count=len(patterns),
                average_override_magnitude=average_override,
                calibration_score=round(max(0.0, 1.0 - average_override), 4),
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary.task_type,
            _confidence_sort_key(summary.ai_confidence),
            -summary.calibration_score,
            -summary.sample_count,
        )
    )
    return tuple(summaries)


def summarize_recent_prompt_versions(
    program_id: str,
    *,
    task_type: str | None = None,
    window_issues: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[PromptVersionSummary, ...]:
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    grouped: dict[tuple[str, str], list[EditPattern]] = {}
    for pattern in recent_patterns:
        if (
            pattern.task_type is None
            or pattern.prompt_version is None
            or pattern.author_override_magnitude is None
            or (task_type is not None and pattern.task_type != task_type)
        ):
            continue
        grouped.setdefault((pattern.task_type, pattern.prompt_version), []).append(pattern)

    summaries: list[PromptVersionSummary] = []
    for (resolved_task_type, prompt_version), patterns in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern in patterns if pattern.author_override_magnitude is not None)
            / len(patterns),
            4,
        )
        summaries.append(
            PromptVersionSummary(
                task_type=resolved_task_type,
                prompt_version=prompt_version,
                sample_count=len(patterns),
                average_override_magnitude=average_override,
                calibration_score=round(max(0.0, 1.0 - average_override), 4),
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary.task_type,
            -summary.calibration_score,
            -summary.sample_count,
            summary.prompt_version,
        )
    )
    return tuple(summaries)


def summarize_recent_prompt_version_confidence_bands(
    program_id: str,
    *,
    task_type: str | None = None,
    window_issues: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[PromptVersionConfidenceSummary, ...]:
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    grouped: dict[tuple[str, str, str], list[EditPattern]] = {}
    for pattern in recent_patterns:
        if (
            pattern.task_type is None
            or pattern.prompt_version is None
            or pattern.author_override_magnitude is None
            or pattern.ai_confidence is None
            or pattern.ai_confidence == Confidence.NONE
            or (task_type is not None and pattern.task_type != task_type)
        ):
            continue
        grouped.setdefault(
            (pattern.task_type, pattern.prompt_version, pattern.ai_confidence.value),
            [],
        ).append(pattern)

    summaries: list[PromptVersionConfidenceSummary] = []
    for (resolved_task_type, prompt_version, resolved_confidence), patterns in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern in patterns if pattern.author_override_magnitude is not None)
            / len(patterns),
            4,
        )
        summaries.append(
            PromptVersionConfidenceSummary(
                task_type=resolved_task_type,
                prompt_version=prompt_version,
                ai_confidence=resolved_confidence,
                sample_count=len(patterns),
                average_override_magnitude=average_override,
                calibration_score=round(max(0.0, 1.0 - average_override), 4),
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary.task_type,
            summary.prompt_version,
            _confidence_sort_key(summary.ai_confidence),
            -summary.calibration_score,
            -summary.sample_count,
        )
    )
    return tuple(summaries)


def summarize_recent_models(
    program_id: str,
    *,
    task_type: str | None = None,
    window_issues: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ModelLeaderboardSummary, ...]:
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    joined_patterns = _join_prompt_learning_patterns_to_traces(
        recent_patterns,
        programs_root=programs_root,
    )

    grouped: dict[tuple[str, str], list[tuple[EditPattern, _PromptLearningTraceRecord]]] = {}
    for pattern, trace in joined_patterns:
        if task_type is not None and pattern.task_type != task_type:
            continue
        grouped.setdefault((pattern.task_type or "", trace.model), []).append((pattern, trace))

    summaries: list[ModelLeaderboardSummary] = []
    for (resolved_task_type, model), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        summaries.append(
            ModelLeaderboardSummary(
                task_type=resolved_task_type,
                model=model,
                deployment_count=len({trace.deployment for _, trace in pairs}),
                sample_count=len(pairs),
                average_override_magnitude=average_override,
                calibration_score=round(max(0.0, 1.0 - average_override), 4),
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary.task_type,
            -summary.calibration_score,
            -summary.sample_count,
            summary.model,
        )
    )
    return tuple(summaries)


def summarize_recent_prompt_version_models(
    program_id: str,
    *,
    task_type: str | None = None,
    window_issues: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[PromptVersionModelLeaderboardSummary, ...]:
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    joined_patterns = _join_prompt_learning_patterns_to_traces(
        recent_patterns,
        programs_root=programs_root,
    )

    grouped: dict[tuple[str, str, str], list[tuple[EditPattern, _PromptLearningTraceRecord]]] = {}
    for pattern, trace in joined_patterns:
        if task_type is not None and pattern.task_type != task_type:
            continue
        prompt_version = pattern.prompt_version or trace.prompt_version
        if prompt_version is None:
            continue
        grouped.setdefault((pattern.task_type or "", prompt_version, trace.model), []).append((pattern, trace))

    summaries: list[PromptVersionModelLeaderboardSummary] = []
    for (resolved_task_type, prompt_version, model), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        summaries.append(
            PromptVersionModelLeaderboardSummary(
                task_type=resolved_task_type,
                prompt_version=prompt_version,
                model=model,
                deployment_count=len({trace.deployment for _, trace in pairs}),
                sample_count=len(pairs),
                average_override_magnitude=average_override,
                calibration_score=round(max(0.0, 1.0 - average_override), 4),
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary.task_type,
            -summary.calibration_score,
            -summary.sample_count,
            summary.prompt_version,
            summary.model,
        )
    )
    return tuple(summaries)


def _patterns_within_issue_window(
    patterns: tuple[EditPattern, ...],
    *,
    window_issues: int,
) -> tuple[EditPattern, ...]:
    if window_issues <= 0:
        return ()
    ordered = sorted(patterns, key=lambda pattern: (pattern.issue_number, pattern.recorded_at), reverse=True)
    recent_issue_numbers: list[int] = []
    for pattern in ordered:
        if pattern.issue_number not in recent_issue_numbers:
            recent_issue_numbers.append(pattern.issue_number)
        if len(recent_issue_numbers) >= window_issues:
            break
    allowed_issue_numbers = set(recent_issue_numbers)
    return tuple(pattern for pattern in ordered if pattern.issue_number in allowed_issue_numbers)


def _join_prompt_learning_patterns_to_traces(
    patterns: tuple[EditPattern, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[tuple[EditPattern, _PromptLearningTraceRecord], ...]:
    traces = _load_prompt_learning_trace_records(patterns, programs_root=programs_root)
    exact_traces_by_key, fallback_traces_by_key = _index_prompt_learning_traces(traces)
    pairs: list[tuple[EditPattern, _PromptLearningTraceRecord]] = []
    for pattern in patterns:
        if pattern.task_type is None or pattern.author_override_magnitude is None:
            continue
        matched_trace = _match_prompt_learning_trace_to_pattern(
            pattern,
            exact_traces_by_key=exact_traces_by_key,
            fallback_traces_by_key=fallback_traces_by_key,
        )
        if matched_trace is None:
            continue
        pairs.append((pattern, matched_trace))
    return tuple(pairs)


def _load_prompt_learning_trace_records(
    patterns: tuple[EditPattern, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[_PromptLearningTraceRecord, ...]:
    if not patterns:
        return ()

    exact_keys = {
        _prompt_learning_trace_join_key(
            run_id=pattern.trace_run_id,
            section_id=pattern.section_id,
            task_type=pattern.task_type,
        )
        for pattern in patterns
        if pattern.task_type is not None and pattern.trace_run_id is not None
    }
    fallback_keys = {
        _prompt_learning_join_key(
            edition_id=pattern.edition_id,
            issue_number=pattern.issue_number,
            section_id=pattern.section_id,
            task_type=pattern.task_type,
        )
        for pattern in patterns
        if pattern.task_type is not None and pattern.trace_run_id is None
    }
    if not exact_keys and not fallback_keys:
        return ()

    latest_by_exact_key: dict[tuple[str, str, str], _PromptLearningTraceRecord] = {}
    latest_by_fallback_key: dict[tuple[str, int, str, str], _PromptLearningTraceRecord] = {}
    for edition_id in sorted({pattern.edition_id for pattern in patterns}):
        trace_path = get_program_output_dir(edition_id, programs_root=programs_root) / "ai" / "llm_trace.jsonl"
        if not trace_path.exists():
            continue
        with trace_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    raise EditLearnerError(f"Prompt learning trace journal at {trace_path} contains invalid JSON.") from error
                trace_record = _parse_prompt_learning_trace_record(payload, trace_path=trace_path)
                if trace_record is None:
                    continue
                exact_key = _prompt_learning_trace_join_key(
                    run_id=trace_record.run_id,
                    section_id=trace_record.section_id,
                    task_type=trace_record.task_type,
                )
                if exact_key in exact_keys:
                    current = latest_by_exact_key.get(exact_key)
                    if current is None or trace_record.timestamp > current.timestamp:
                        latest_by_exact_key[exact_key] = trace_record
                    continue

                fallback_key = _prompt_learning_join_key(
                    edition_id=trace_record.edition_id,
                    issue_number=trace_record.issue_number,
                    section_id=trace_record.section_id,
                    task_type=trace_record.task_type,
                )
                if fallback_key not in fallback_keys:
                    continue
                current = latest_by_fallback_key.get(fallback_key)
                if current is None or trace_record.timestamp > current.timestamp:
                    latest_by_fallback_key[fallback_key] = trace_record

    exact_traces = tuple(latest_by_exact_key[key] for key in sorted(latest_by_exact_key))
    fallback_traces = tuple(latest_by_fallback_key[key] for key in sorted(latest_by_fallback_key))
    return exact_traces + fallback_traces


def _parse_prompt_learning_trace_record(payload: object, *, trace_path: Path) -> _PromptLearningTraceRecord | None:
    if not isinstance(payload, dict):
        raise EditLearnerError(f"Prompt learning trace journal at {trace_path} must contain JSON objects.")
    if _trace_error_present(payload.get("error")):
        return None

    metadata = _require_trace_object(
        _require_trace_field(payload, field_name="metadata", message="Prompt learning trace must include metadata as an object."),
        field_name="metadata",
    )

    timestamp = _require_trace_datetime(
        _require_trace_field(payload, field_name="timestamp", message="Prompt learning trace must include timestamp as an ISO-8601 datetime."),
        field_name="timestamp",
    )
    run_id = _require_trace_string(
        _require_trace_field(payload, field_name="run_id", message="Prompt learning trace must include run_id as a non-empty string."),
        field_name="run_id",
    )
    edition_id = _require_trace_string(
        _require_trace_field(payload, field_name="edition", message="Prompt learning trace must include edition as a non-empty string."),
        field_name="edition",
    )
    issue_number = _require_trace_int(
        _require_trace_field(
            metadata,
            field_name="issue_number",
            message="Prompt learning trace metadata must include issue_number as an integer.",
        ),
        field_name="metadata.issue_number",
    )
    section_id = _require_trace_string(
        _require_trace_field(
            metadata,
            field_name="section_id",
            message="Prompt learning trace metadata must include section_id as a non-empty string.",
        ),
        field_name="metadata.section_id",
    )
    task_type = _require_trace_string(
        _require_trace_field(
            metadata,
            field_name="task_type",
            message="Prompt learning trace metadata must include task_type as a non-empty string.",
        ),
        field_name="metadata.task_type",
    )
    if task_type not in _ALLOWED_TASK_TYPES:
        raise EditLearnerError(
            "Prompt learning trace metadata.task_type must be one of: exec_summary, workstream_blurb."
        )
    model = _require_trace_string(
        _require_trace_field(payload, field_name="model", message="Prompt learning trace must include model as a non-empty string."),
        field_name="model",
    )
    deployment = _optional_trace_string(payload.get("deployment"), field_name="deployment") or model

    return _PromptLearningTraceRecord(
        timestamp=timestamp,
        run_id=run_id,
        edition_id=edition_id,
        issue_number=issue_number,
        section_id=section_id,
        task_type=task_type,
        prompt_version=_optional_trace_string(payload.get("prompt_version"), field_name="prompt_version"),
        model=model,
        deployment=deployment,
    )


def _require_trace_object(value: object, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EditLearnerError(f"Prompt learning trace {field_name} must be an object.")
    return value


def _require_trace_field(payload: dict[str, Any], *, field_name: str, message: str) -> Any:
    if field_name not in payload:
        raise EditLearnerError(message)
    return payload.get(field_name)


def _trace_error_present(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        raise EditLearnerError("Prompt learning trace error must be a string.")
    return bool(value.strip())


def _require_trace_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise EditLearnerError(f"Prompt learning trace {field_name} must be a non-empty string.")
    text = value.strip()
    if not text:
        raise EditLearnerError(f"Prompt learning trace {field_name} must be a non-empty string.")
    return text


def _optional_trace_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EditLearnerError(f"Prompt learning trace {field_name} must be a string.")
    text = value.strip()
    return text or None


def _require_trace_int(value: object, *, field_name: str) -> int:
    parsed = _coerce_int(value)
    if parsed is None:
        raise EditLearnerError(f"Prompt learning trace {field_name} must be an integer.")
    return parsed


def _require_trace_datetime(value: object, *, field_name: str) -> datetime:
    parsed = _coerce_datetime(value)
    if parsed is None:
        raise EditLearnerError(f"Prompt learning trace {field_name} must be an ISO-8601 datetime.")
    return parsed


def _prompt_learning_join_key(
    *,
    edition_id: str,
    issue_number: int,
    section_id: str,
    task_type: str | None,
) -> tuple[str, int, str, str]:
    return (edition_id, issue_number, section_id, task_type or "")


def _prompt_learning_trace_join_key(
    *,
    run_id: str | None,
    section_id: str,
    task_type: str | None,
) -> tuple[str, str, str]:
    return (run_id or "", section_id, task_type or "")


def _index_prompt_learning_traces(
    traces: tuple[_PromptLearningTraceRecord, ...],
) -> tuple[
    dict[tuple[str, str, str], _PromptLearningTraceRecord],
    dict[tuple[str, int, str, str], _PromptLearningTraceRecord],
]:
    exact_traces_by_key = {
        _prompt_learning_trace_join_key(
            run_id=trace.run_id,
            section_id=trace.section_id,
            task_type=trace.task_type,
        ): trace
        for trace in traces
    }
    fallback_traces_by_key = {
        _prompt_learning_join_key(
            edition_id=trace.edition_id,
            issue_number=trace.issue_number,
            section_id=trace.section_id,
            task_type=trace.task_type,
        ): trace
        for trace in traces
    }
    return exact_traces_by_key, fallback_traces_by_key


def _match_prompt_learning_trace_to_pattern(
    pattern: EditPattern,
    *,
    exact_traces_by_key: dict[tuple[str, str, str], _PromptLearningTraceRecord],
    fallback_traces_by_key: dict[tuple[str, int, str, str], _PromptLearningTraceRecord],
) -> _PromptLearningTraceRecord | None:
    if pattern.task_type is None:
        return None
    if pattern.trace_run_id is not None:
        return exact_traces_by_key.get(
            _prompt_learning_trace_join_key(
                run_id=pattern.trace_run_id,
                section_id=pattern.section_id,
                task_type=pattern.task_type,
            )
        )
    return fallback_traces_by_key.get(
        _prompt_learning_join_key(
            edition_id=pattern.edition_id,
            issue_number=pattern.issue_number,
            section_id=pattern.section_id,
            task_type=pattern.task_type,
        )
    )


def _coerce_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _parse_confidence(value: str | None, *, field_name: str = "ai_confidence") -> Confidence | None:
    if value in (None, ""):
        return None
    try:
        return Confidence(str(value).strip().lower())
    except ValueError as error:
        raise EditLearnerError(f"Edit pattern {field_name} must be a valid confidence value.") from error


def _confidence_sort_key(value: str) -> tuple[int, str]:
    order = {
        Confidence.HIGH.value: 0,
        Confidence.MEDIUM.value: 1,
        Confidence.LOW.value: 2,
    }
    return (order.get(value, 99), value)


def _summarize_change(before_text: str, after_text: str, before_words: int, after_words: int) -> str:
    before_opening = _opening(before_text)
    after_opening = _opening(after_text)
    notes: list[str] = []
    if before_opening != after_opening:
        notes.append(f"changed the opening from '{before_opening}' to '{after_opening}'")
    word_delta = after_words - before_words
    if word_delta < 0:
        notes.append(f"tightened by {abs(word_delta)} words")
    elif word_delta > 0:
        notes.append(f"expanded by {word_delta} words")
    else:
        notes.append("kept a similar length")
    return "Author edits " + "; ".join(notes) + "."


def _opening(text: str, *, max_words: int = 8) -> str:
    return " ".join(text.split()[:max_words])


def _excerpt(text: str, *, max_chars: int = 180) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _normalize_text(text: str) -> str:
    return " ".join(str(text).split()).strip()


def _author_override_magnitude(before_text: str, after_text: str) -> float:
    before_tokens = before_text.split()
    after_tokens = after_text.split()
    if not before_tokens and not after_tokens:
        return 0.0
    if not before_tokens or not after_tokens:
        return 1.0
    max_length = max(len(before_tokens), len(after_tokens))
    if max_length == 0:
        return 0.0
    distance = _levenshtein_distance(before_tokens, after_tokens)
    return round(distance / max_length, 4)


def _levenshtein_distance(left: list[str], right: list[str]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_token in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_token in enumerate(right, start=1):
            substitution_cost = 0 if left_token == right_token else 1
            current.append(
                min(
                    current[right_index - 1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + substitution_cost,
                )
            )
        previous = current
    return previous[-1]




def _pattern_to_record(pattern: EditPattern) -> dict[str, Any]:
    record = {
        "program_id": pattern.program_id,
        "edition_id": pattern.edition_id,
        "issue_number": pattern.issue_number,
        "section_id": pattern.section_id,
        "recorded_at": _require_utc(pattern.recorded_at).isoformat(),
        "summary": pattern.summary,
        "before_excerpt": pattern.before_excerpt,
        "after_excerpt": pattern.after_excerpt,
        "before_word_count": pattern.before_word_count,
        "after_word_count": pattern.after_word_count,
    }
    if pattern.task_type is not None:
        record["task_type"] = pattern.task_type
    if pattern.prompt_version is not None:
        record["prompt_version"] = pattern.prompt_version
    if pattern.ai_confidence is not None:
        record["ai_confidence"] = pattern.ai_confidence.value
    if pattern.trace_run_id is not None:
        record["trace_run_id"] = pattern.trace_run_id
    if pattern.author_override_magnitude is not None:
        record["author_override_magnitude"] = pattern.author_override_magnitude
    return record


def _pattern_from_record(record: dict[str, Any]) -> EditPattern:
    if not isinstance(record, dict):
        raise EditLearnerError("Edit pattern record must be an object.")
    task_type = _optional_pattern_string(record.get("task_type"), field_name="task_type")
    if task_type is not None and task_type not in _ALLOWED_TASK_TYPES:
        raise EditLearnerError(
            "Edit pattern task_type must be one of: exec_summary, workstream_blurb."
        )
    return EditPattern(
        program_id=_require_pattern_string(record.get("program_id"), field_name="program_id"),
        edition_id=_require_pattern_string(record.get("edition_id"), field_name="edition_id"),
        issue_number=_coerce_pattern_int(record.get("issue_number"), field_name="issue_number"),
        section_id=_require_pattern_string(record.get("section_id"), field_name="section_id"),
        recorded_at=_coerce_pattern_datetime(record.get("recorded_at"), field_name="recorded_at"),
        summary=_require_pattern_string(record.get("summary"), field_name="summary"),
        before_excerpt=_require_pattern_string(record.get("before_excerpt"), field_name="before_excerpt"),
        after_excerpt=_require_pattern_string(record.get("after_excerpt"), field_name="after_excerpt"),
        before_word_count=_coerce_pattern_int(record.get("before_word_count"), field_name="before_word_count"),
        after_word_count=_coerce_pattern_int(record.get("after_word_count"), field_name="after_word_count"),
        task_type=task_type,
        prompt_version=_optional_pattern_string(record.get("prompt_version"), field_name="prompt_version"),
        ai_confidence=_parse_confidence(
            _optional_pattern_string(record.get("ai_confidence"), field_name="ai_confidence"),
            field_name="ai_confidence",
        ),
        trace_run_id=_optional_pattern_string(record.get("trace_run_id"), field_name="trace_run_id"),
        author_override_magnitude=(
            _coerce_pattern_float(record.get("author_override_magnitude"), field_name="author_override_magnitude")
            if record.get("author_override_magnitude") is not None
            else None
        ),
    )


def _require_pattern_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise EditLearnerError(f"Edit pattern {field_name} must be a string.")
    return value


def _optional_pattern_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EditLearnerError(f"Edit pattern {field_name} must be a string.")
    return value


def _coerce_pattern_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise EditLearnerError(f"Edit pattern {field_name} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as error:
            raise EditLearnerError(f"Edit pattern {field_name} must be an integer.") from error
    raise EditLearnerError(f"Edit pattern {field_name} must be an integer.")


def _coerce_pattern_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise EditLearnerError(f"Edit pattern {field_name} must be a number.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as error:
            raise EditLearnerError(f"Edit pattern {field_name} must be a number.") from error
    raise EditLearnerError(f"Edit pattern {field_name} must be a number.")


def _coerce_pattern_datetime(value: object, *, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except (TypeError, ValueError) as error:
        raise EditLearnerError(f"Edit pattern {field_name} must be an ISO-8601 datetime.") from error


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# WS-22: Edit distance trend (learning-loop efficacy coding slice)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EditDistanceTrend:
    """Per-task-type edit distance trend measured over confirmed issues.

    ``direction`` is one of: "improving", "declining", "flat", "insufficient_data".
    ``mean_override_early`` and ``mean_override_late`` are the mean
    ``author_override_magnitude`` for the earliest and latest half-windows.
    A declining mean override (authors change the AI draft less over time)
    indicates improvement.
    """

    task_type: str
    issue_count: int
    mean_override_early: float | None
    mean_override_late: float | None
    delta: float | None  # late - early; negative = improving
    direction: str  # "improving" | "declining" | "flat" | "insufficient_data"


def compute_edit_distance_trend(
    program_id: str,
    *,
    window_issues: int = 10,
    min_issues_for_trend: int = 4,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[EditDistanceTrend, ...]:
    """Compute draft↔confirm edit distance trend per task type (WS-22).

    Splits the most recent ``window_issues`` confirmed issues into an early
    half and a late half, computes mean ``author_override_magnitude`` for
    each, and derives direction:

    * **improving** — mean_override_late < mean_override_early  (fewer edits)
    * **declining** — mean_override_late > mean_override_early  (more edits)
    * **flat** — no change (delta == 0.0 after rounding to 4dp)
    * **insufficient_data** — fewer than ``min_issues_for_trend`` issues

    Returns one ``EditDistanceTrend`` per task type with recorded patterns.
    """
    all_patterns = read_edit_patterns(program_id, programs_root=programs_root)
    # Group by task_type, then by issue_number.
    by_task: dict[str, dict[int, list[float]]] = {}
    for pattern in all_patterns:
        if pattern.task_type is None or pattern.author_override_magnitude is None:
            continue
        by_task.setdefault(pattern.task_type, {}).setdefault(pattern.issue_number, []).append(
            pattern.author_override_magnitude
        )

    trends: list[EditDistanceTrend] = []
    for task_type, issues_map in sorted(by_task.items()):
        # Sort issue numbers ascending (chronological).
        issue_numbers = sorted(issues_map)
        # Apply window_issues cap.
        if len(issue_numbers) > window_issues:
            issue_numbers = issue_numbers[-window_issues:]

        issue_count = len(issue_numbers)
        if issue_count < min_issues_for_trend:
            trends.append(EditDistanceTrend(
                task_type=task_type,
                issue_count=issue_count,
                mean_override_early=None,
                mean_override_late=None,
                delta=None,
                direction="insufficient_data",
            ))
            continue

        # Split into early/late halves.
        mid = issue_count // 2
        early_issues = issue_numbers[:mid]
        late_issues = issue_numbers[mid:]

        def _mean(issue_list: list[int]) -> float:
            all_vals = [v for iss in issue_list for v in issues_map[iss]]
            return sum(all_vals) / len(all_vals) if all_vals else 0.0

        mean_early = round(_mean(early_issues), 4)
        mean_late = round(_mean(late_issues), 4)
        delta = round(mean_late - mean_early, 4)

        if delta < 0:
            direction = "improving"
        elif delta > 0:
            direction = "declining"
        else:
            direction = "flat"

        trends.append(EditDistanceTrend(
            task_type=task_type,
            issue_count=issue_count,
            mean_override_early=mean_early,
            mean_override_late=mean_late,
            delta=delta,
            direction=direction,
        ))

    return tuple(trends)
