"""Claim-extractor resolution helpers for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). This cluster
decides whether AI claim extraction runs (mode resolution), invokes the
extractor, translates extractor failures into operator-facing fallback
warnings, prepares the in-memory calibration record, and performs the
post-confirm claim-tracker follow-through for the V2 path. Budget/safety
failures are re-raised as ``RuntimeError`` exactly as before. ``confirm.py``
imports the public entry points it calls under their historical private
aliases; the mode and warning helpers are internal to this module.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.ai.claim_extractor import (
    ClaimExtractor,
    ClaimExtractorBudgetError,
    ClaimExtractorError,
    ClaimExtractorSafetyError,
)
from src.core.claim_extraction_calibration_store import ClaimExtractionCalibrationRecord
from src.core.claim_tracker import (
    ClaimExtractionResult,
    build_claim_extraction_calibration_record,
    record_confirmed_claims,
)
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition
from src.core.models import WorkItem
from src.core.narrative_store import load_narratives
from src.core.quality_gates import GateEvaluation, QualityGateReport


def resolve_confirm_claim_extraction(
    *,
    resolved,
    edition_name: str,
    issue_number: int,
    confirmed_at: datetime,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
    legacy_regex_extractor: bool,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ClaimExtractionResult | None, str, tuple[str, ...]]:
    if legacy_regex_extractor:
        return None, "regex", ()
    mode = claim_extractor_mode(resolved.raw_program)
    if mode is None:
        return None, "regex", ()
    try:
        extraction_result = ClaimExtractor.from_program(resolved.program).extract_claims(
            program_id=resolved.program.id,
            edition_id=edition_name,
            issue_number=issue_number,
            claim_date=confirmed_at.date(),
            narratives=narratives,
            items=items,
            valid_workstream_ids=valid_workstream_ids,
            workstream_area_paths=workstream_area_paths,
            programs_root=programs_root,
        )
    except ClaimExtractorBudgetError as error:
        raise RuntimeError(str(error)) from error
    except ClaimExtractorSafetyError as error:
        raise RuntimeError(f"AI output rejected by safety pipeline: {error}") from error
    except ClaimExtractorError as error:
        return None, "regex", (render_claim_extractor_fallback_warning(error),)
    return extraction_result, mode, ()


def claim_extractor_mode(raw_program: dict[str, Any] | None) -> str | None:
    if not isinstance(raw_program, dict):
        return None
    ai_config = raw_program.get("ai")
    if not isinstance(ai_config, dict) or not bool(ai_config.get("enabled")):
        return None
    claim_extractor_config = ai_config.get("claim_extractor")
    if not isinstance(claim_extractor_config, dict):
        return "calibration"
    raw_mode = str(claim_extractor_config.get("mode") or "calibration").strip().lower()
    if raw_mode in {"calibration", "production"}:
        return raw_mode
    return "calibration"


def render_claim_extractor_fallback_warning(error: ClaimExtractorError) -> str:
    message = str(error)
    normalized = message.lower()
    if "429" in normalized or "rate limit" in normalized:
        return (
            "AOAI rate limit: all retries exhausted. Falling back to regex extractor. "
            "Space confirms >1 min apart, or reduce ai.requests_per_minute in program.yaml."
        )
    if "timeout" in normalized or "timed out" in normalized:
        return "AI claim extraction timed out. Falling back to regex extractor."
    if "invalid json" in normalized or "non-object payload" in normalized:
        return "AI claim extraction returned invalid structured output. Falling back to regex extractor."
    return f"AI claim extraction fell back to regex extractor: {message}"


def prepare_confirm_claim_extraction_for_v2(
    *,
    edition_name: str,
    issue_number: int,
    confirmed_at: datetime,
    reports_root: Path,
    items: tuple[WorkItem, ...],
    legacy_regex_extractor: bool = False,
) -> tuple[ClaimExtractionResult | None, str, tuple[str, ...], ClaimExtractionCalibrationRecord | None]:
    programs_root = reports_root.parent / "programs"
    editions_root = reports_root.parent / "editions"
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return None, "", (), None
    workstream_area_paths = {workstream.id: workstream.area_paths for workstream in resolved.workstreams}
    narratives = load_narratives(edition_name, issue_number, reports_root=reports_root)
    extraction_result, extraction_mode, warnings = resolve_confirm_claim_extraction(
        resolved=resolved,
        edition_name=edition_name,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        narratives=narratives,
        items=items,
        valid_workstream_ids=tuple(workstream_area_paths),
        workstream_area_paths=workstream_area_paths,
        legacy_regex_extractor=legacy_regex_extractor,
        programs_root=programs_root,
    )

    calibration_record = None
    if extraction_result is not None and extraction_mode.strip().lower() == "calibration":
        calibration_record = build_claim_extraction_calibration_record(
            program_id=resolved.program.id,
            edition_id=edition_name,
            issue_number=issue_number,
            claim_date=confirmed_at.date(),
            narratives=narratives,
            items=items,
            valid_workstream_ids=tuple(workstream_area_paths),
            workstream_area_paths=workstream_area_paths,
            ai_extracted=extraction_result,
        )
    return extraction_result, extraction_mode, warnings, calibration_record


def record_confirmed_claims_for_v2(
        *,
        edition_name: str,
        issue_number: int,
        confirmed_at: datetime,
        reports_root: Path,
        items: tuple[WorkItem, ...],
        legacy_regex_extractor: bool = False,
        extraction_result: ClaimExtractionResult | None = None,
        extraction_mode: str = "regex",
        resolve_extraction_if_missing: bool = True,
) -> tuple[str, ...]:
        programs_root = reports_root.parent / "programs"
        editions_root = reports_root.parent / "editions"
        try:
            resolved = resolve_edition(
                edition_name,
                editions_root=editions_root,
                programs_root=programs_root,
            )
        except FileNotFoundError:
            return ()
        if resolved is None:
            return ()

        workstream_area_paths = {workstream.id: workstream.area_paths for workstream in resolved.workstreams}
        narratives = load_narratives(edition_name, issue_number, reports_root=reports_root)
        warnings: list[str] = []
        if extraction_result is None and resolve_extraction_if_missing:
            extraction_result, extraction_mode, extractor_warnings = resolve_confirm_claim_extraction(
                resolved=resolved,
                edition_name=edition_name,
                issue_number=issue_number,
                confirmed_at=confirmed_at,
                narratives=narratives,
                items=items,
                valid_workstream_ids=tuple(workstream_area_paths),
                workstream_area_paths=workstream_area_paths,
                legacy_regex_extractor=legacy_regex_extractor,
                programs_root=programs_root,
            )
            warnings.extend(extractor_warnings)
        recorded = record_confirmed_claims(
            program_id=resolved.program.id,
            edition_id=edition_name,
            issue_number=issue_number,
            claim_date=confirmed_at.date(),
            narratives=narratives,
            items=items,
            valid_workstream_ids=tuple(workstream_area_paths),
            workstream_area_paths=workstream_area_paths,
            extraction_result=extraction_result,
            extraction_mode=extraction_mode,
            programs_root=programs_root,
        )
        warnings.extend(recorded.warnings)
        if recorded.written_claims or recorded.written_decision_asks:
            warnings.append(
                f"Claim tracker recorded {len(recorded.written_claims)} claim(s) and {len(recorded.written_decision_asks)} decision ask(s)."
            )
        return tuple(warnings)


def evaluate_claim_extraction_calibration_gate(
        calibration_record: ClaimExtractionCalibrationRecord | None,
) -> QualityGateReport:
    if calibration_record is None or calibration_record.mode.strip().lower() != "calibration":
        return QualityGateReport(results=())

    difference_count = calibration_record.ai_only_count + calibration_record.regex_only_count
    if difference_count < 3:
        return QualityGateReport(
            results=(
                GateEvaluation(
                    "QG-CE1",
                    True,
                    "AI and regex claim extraction remain within calibration tolerance.",
                    3,
                    forceable=True,
                ),
            )
        )

    if calibration_record.ai_only_count >= calibration_record.regex_only_count:
        message = (
            f"AI/regex claim extraction differs by {difference_count} claim(s) in calibration mode. "
            "AI extraction found more claims than regex. Review with `vertex claims --show-ai-only` before confirming."
        )
    else:
        message = (
            f"AI/regex claim extraction differs by {difference_count} claim(s) in calibration mode. "
            "Regex extraction found more claims than AI. Review the claim extraction comparison before confirming."
        )
    return QualityGateReport(
        results=(
            GateEvaluation(
                "QG-CE1",
                False,
                message,
                3,
                forceable=True,
            ),
        )
    )
