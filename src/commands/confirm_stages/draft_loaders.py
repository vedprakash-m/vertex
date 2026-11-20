"""Read-only draft/readiness state loaders for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). Every function here
*reads* persisted draft artifacts (draft JSON, manifest JSON, overrides, review
status, program/edition YAML) and returns in-memory objects. None of them write
state, which is why they are safe to lift out of the confirm transaction module
ahead of the write path. ``confirm.py`` imports the seven entry points it calls
under their historical private aliases; ``load_optional_yaml_mapping`` and
``coerce_optional_int`` are internal helpers for the readiness-gate loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
import re
from typing import Any

import typer
import yaml

from src.commands.confirm_stages.deserialization import deserialize_retained_blocking_charts
from src.core.edition_resolver import find_edition_yaml, get_program_output_dir, PROGRAMS_ROOT
from src.core.draft_artifact_store import load_draft_state as load_optional_draft_state
from src.core.draft_artifact_store import load_draft_manifest as load_optional_draft_manifest
from src.core.exceptions import ConfigError, StateError
from src.core.manifest_writer import chart_gate_evidence_sha256, hash_jsonable, load_run_evidence_snapshot
from src.core.models import ReviewSection, ReviewState, ReviewStatus, RunEvidenceSnapshot
from src.core.overrides_store import OverridesDocument, load_overrides, merge_overrides
from src.core.program_verdict import (
    validate_retained_program_verdict,
    validate_retained_program_verdict_comparison,
)
from src.core.quality_gates import GateEvaluation, QualityGateReport, evaluate_chart_gates
from src.core.review_status_store import load_review_status


class ChartFreshnessExpired(ConfigError):
    pass


@dataclass(frozen=True, slots=True)
class ConfirmDraftManifest:
    edition_name: str
    issue_number: int
    programs_root: Path
    manifest_id: str
    config_hash: str
    ai_safety: dict[str, Any] | None
    draft_readiness: dict[str, Any] | None
    milestone_assessments: Any
    milestone_trajectory_snapshot: Any
    run_evidence_snapshot: RunEvidenceSnapshot | None = None
    program_verdict_observation: dict[str, Any] | None = None
    program_verdict_comparison: dict[str, Any] | None = None
    chart_hard_gate_failures: tuple[str, ...] = ()
    recorded_qg22_passed: bool = False
    chart_freshness_evidence: tuple[dict[str, Any], ...] = ()

    def validate_scope(self, edition_name: str, issue_number: int, programs_root: Path) -> None:
        if (
            self.edition_name != edition_name
            or self.issue_number != issue_number
            or self.programs_root != programs_root.absolute()
        ):
            raise typer.BadParameter(
                "Captured draft manifest scope does not match this confirmation. "
                "Preserve the existing artifacts and capture evidence from the selected workspace, edition and issue."
            )

    def require_bound_capture(self, program_id: str | None, draft_state: Mapping[str, Any]) -> None:
        if (
            program_id is not None
            and draft_state.get("edition_type") != "lookback"
            and (
                self.run_evidence_snapshot is None
                or self.run_evidence_snapshot.schema_version != "run-evidence-capture.v2"
                or {family for family, _ in self.run_evidence_snapshot.effective_family_modes}
                != {"judgment", "workitem.state"}
            )
        ):
            raise ConfigError(
                "This program-backed draft has no source-selector-bound capture. "
                "Preserve the existing artifacts, re-run `vertex report --dry-run` and review before confirming."
            )


def retained_chart_freshness_failures(
    draft_manifest: ConfirmDraftManifest, draft_state: Mapping[str, Any], current_time: datetime,
) -> tuple[GateEvaluation, ...]:
    if not draft_manifest.recorded_qg22_passed:
        return ()
    retained_charts = tuple(dict.fromkeys(deserialize_retained_blocking_charts(
        draft_manifest.chart_freshness_evidence + tuple(draft_state.get("kusto_sections", []))
    )))
    return tuple(
        result for result in evaluate_chart_gates(retained_charts, current_time=current_time).failing_results
        if result.gate_id == "QG-22"
    )


def chart_gate_failure_report(
    draft_manifest: ConfirmDraftManifest, draft_state: Mapping[str, Any], current_time: datetime,
) -> QualityGateReport:
    recorded = tuple(
        GateEvaluation(
            gate_id=gate_id,
            passed=False,
            message=f"{gate_id}: The draft recorded a chart hard-gate failure. Re-run and review the draft before confirming.",
            exit_code=3,
            forceable=False,
        )
        for gate_id in draft_manifest.chart_hard_gate_failures
    )
    return QualityGateReport(results=recorded + retained_chart_freshness_failures(draft_manifest, draft_state, current_time))


def require_retained_chart_freshness(
    draft_manifest: ConfirmDraftManifest, draft_state: Mapping[str, Any], current_time: datetime,
) -> None:
    failures = retained_chart_freshness_failures(draft_manifest, draft_state, current_time)
    if failures:
        raise ChartFreshnessExpired("; ".join(result.message for result in failures))


def load_confirm_draft_manifest(
    edition_name: str, issue_number: int, *, draft_state: Mapping[str, Any], programs_root: Path = PROGRAMS_ROOT,
) -> ConfirmDraftManifest:
    payload = _load_manifest(edition_name, issue_number, programs_root=programs_root)
    manifest_id = _require_manifest_id(payload, edition_name, issue_number, programs_root=programs_root)
    config_hash = payload.get("config_hash") if payload is not None else None
    if not isinstance(config_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", config_hash) is None:
        raise typer.BadParameter(
            f"Invalid or missing draft config hash for edition {edition_name!r}, issue {issue_number:03d}. "
            "Preserve the existing artifacts and re-run `vertex report --dry-run` before confirming."
        )
    try:
        capture = load_run_evidence_snapshot(payload or {}, draft_state)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    if (
        draft_state.get("program_fact_snapshot") is not None
        and draft_state.get("edition_type") != "lookback"
        and (
            capture is None
            or capture.schema_version != "run-evidence-capture.v2"
            or {family for family, _ in capture.effective_family_modes}
            != {"judgment", "workitem.state"}
        )
    ):
        raise typer.BadParameter(
            "This program-backed draft has no source-selector-bound capture. "
            "Preserve the existing artifacts, re-run `vertex report --dry-run` and review before confirming."
        )
    qg_results = payload.get("qg_results", {}) if payload is not None else {}
    if not isinstance(qg_results, dict) or any(
        gate_id in qg_results and not isinstance(qg_results[gate_id], bool)
        for gate_id in ("QG-21", "QG-22")
    ):
        raise typer.BadParameter(
            "Invalid recorded chart gate results in the draft manifest. "
            "Preserve the existing artifact and re-run `vertex report --dry-run` before confirming."
        )
    chart_evidence = _project_draft_metadata_value(payload, "chart_freshness_evidence", edition_name, issue_number)
    metadata = payload.get("metadata") if payload is not None else None
    expected_chart_fields = {
        "query_id", "chart_blocks_publish", "is_degraded", "chart_cache_ttl_hours",
        "captured_at", "cache_captured_at",
    }
    if isinstance(metadata, dict) and "chart_freshness_evidence" in metadata and (
        not isinstance(chart_evidence, list)
        or any(
            not isinstance(row, dict)
            or set(row) != expected_chart_fields
            or row["chart_blocks_publish"] is not True
            or row["is_degraded"] is not True
            for row in chart_evidence
        )
    ):
        raise typer.BadParameter(
            "Invalid chart freshness evidence in the draft manifest. "
            "Preserve the existing artifact and re-run `vertex report --dry-run` before confirming."
        )
    chart_reference = draft_state.get("chart_gate_evidence_sha256")
    has_chart_evidence = isinstance(metadata, dict) and "chart_freshness_evidence" in metadata
    has_binding_type = isinstance(metadata, dict) and "chart_binding_edition_type" in metadata
    binding_type = metadata.get("chart_binding_edition_type") if isinstance(metadata, dict) else None
    has_routing_marker = isinstance(metadata, dict) and "chart_routing_capture" in metadata
    routing_marker = metadata.get("chart_routing_capture") if isinstance(metadata, dict) else None
    routed_sections = draft_state.get("routed_chart_sections")
    ordinary_qg22_pass = qg_results.get("QG-22") is True and draft_state.get("edition_type") in (
        "detailed", "focused", "condensed", "narrative", "deck",
    )
    if chart_reference is not None or has_chart_evidence or has_binding_type or ordinary_qg22_pass or has_routing_marker or routed_sections is not None:
        sections = draft_state.get("kusto_sections")
        if (
            not has_chart_evidence
            or not isinstance(chart_reference, str)
            or not isinstance(sections, list)
            or (has_binding_type and (
                not isinstance(binding_type, str)
                or not binding_type
                or binding_type != draft_state.get("edition_type")
            ))
            or ((has_routing_marker or routed_sections is not None) and (
                not has_routing_marker
                or routing_marker != "v1"
                or not isinstance(routed_sections, list)
                or any(not isinstance(section, dict) for section in routed_sections)
            ))
            or chart_reference != chart_gate_evidence_sha256(
                manifest_id, qg_results, chart_evidence, sections, edition_type=binding_type,
                routed_chart_sections=routed_sections,
            )
        ):
            raise typer.BadParameter(
                "Invalid or missing chart evidence binding between the draft and manifest. "
                "Preserve the existing artifacts and re-run `vertex report --dry-run` before confirming."
            )
    observation = metadata.get("program_verdict_observation") if isinstance(metadata, dict) else None
    comparison = metadata.get("program_verdict_comparison") if isinstance(metadata, dict) else None
    verdict_reference = draft_state.get("program_verdict_observation_sha256")
    strict_verdict_draft = (
        draft_state.get("program_fact_snapshot") is not None
        and draft_state.get("edition_type") != "lookback"
        and capture is not None
        and verdict_reference is not None
    )
    if strict_verdict_draft:
        if not isinstance(observation, dict) or not isinstance(comparison, dict):
            raise typer.BadParameter(
                "This program-backed draft has no bound ProgramVerdict observation. "
                "Preserve the existing artifacts and re-run `vertex report --dry-run` before confirming."
            )
        snapshot_hash = payload.get("snapshot_hash") if payload is not None else None
        if not isinstance(snapshot_hash, str):
            raise typer.BadParameter("The retained draft snapshot binding is invalid.")
        if capture is None:
            raise typer.BadParameter(
                "The retained draft evidence capture is unavailable."
            )
        try:
            if (
                not isinstance(verdict_reference, str)
                or hash_jsonable(observation) != verdict_reference
            ):
                raise ConfigError("The retained program verdict observation reference is invalid.")
            observation_inputs_sha256 = validate_retained_program_verdict(
                observation,
                capture=capture,
                report_snapshot_hash=snapshot_hash,
            )
            validate_retained_program_verdict_comparison(
                comparison,
                capture_sha256=capture.sha256,
                observation_inputs_sha256=observation_inputs_sha256,
            )
        except ConfigError as error:
            raise typer.BadParameter(
                "The retained ProgramVerdict observation is invalid. "
                "Preserve the existing artifacts and re-run `vertex report --dry-run` before confirming."
            ) from error
    elif (observation is not None or comparison is not None) and (
        not isinstance(observation, dict) or not isinstance(comparison, dict)
    ):
        raise typer.BadParameter("The retained ProgramVerdict observation is invalid.")
    return ConfirmDraftManifest(
        edition_name=edition_name,
        issue_number=issue_number,
        programs_root=programs_root.absolute(),
        manifest_id=manifest_id,
        config_hash=config_hash,
        run_evidence_snapshot=capture,
        ai_safety=_project_draft_metadata_field(payload, "ai_safety", edition_name, issue_number),
        draft_readiness=_project_draft_metadata_field(payload, "draft_readiness", edition_name, issue_number),
        milestone_assessments=_project_draft_metadata_value(
            payload, "milestone_assessments", edition_name, issue_number,
        ),
        milestone_trajectory_snapshot=_project_draft_metadata_value(
            payload, "milestone_trajectory_snapshot", edition_name, issue_number,
        ),
        chart_hard_gate_failures=tuple(
            gate_id for gate_id in ("QG-21", "QG-22") if qg_results.get(gate_id) is False
        ),
        recorded_qg22_passed=qg_results.get("QG-22") is True,
        chart_freshness_evidence=tuple(chart_evidence or ()),
        program_verdict_observation=dict(observation) if isinstance(observation, dict) else None,
        program_verdict_comparison=dict(comparison) if isinstance(comparison, dict) else None,
    )


def load_confirm_overrides(
    edition_name: str,
    issue_number: int,
    bundle,
    reports_root: Path,
    *,
    programs_root: Path | None = None,
) -> OverridesDocument:
    existing = load_overrides(
        edition_name,
        reports_root=reports_root,
        issue_number=issue_number,
        **({"programs_root": programs_root} if programs_root is not None else {}),
    )
    if existing is None:
        raise typer.BadParameter("overrides.yaml is missing. Run `vertex report --dry-run` first.")
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    merged, _ = merge_overrides(
        issue_number=issue_number,
        expected_scorecards=expected_scorecards,
        existing=existing,
    )
    return merged


def load_draft_readiness_metadata(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    return _load_draft_metadata_field(
        "draft_readiness", edition_name=edition_name, issue_number=issue_number, programs_root=programs_root,
    )


def _load_manifest(
    edition_name: str, issue_number: int, *, programs_root: Path,
) -> dict[str, Any] | None:
    try:
        return load_optional_draft_manifest(edition_name, issue_number, programs_root=programs_root)
    except StateError as error:
        raise typer.BadParameter(str(error)) from error


def _load_draft_metadata_field(
    field: str, *, edition_name: str, issue_number: int, programs_root: Path,
) -> dict[str, Any] | None:
    payload = _load_manifest(edition_name, issue_number, programs_root=programs_root)
    return _project_draft_metadata_field(payload, field, edition_name, issue_number)


def _project_draft_metadata_field(
    payload: dict[str, Any] | None, field: str, edition_name: str, issue_number: int,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    metadata = payload.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise typer.BadParameter(
            f"Invalid draft manifest metadata for edition {edition_name!r}, issue {issue_number:03d}: "
            "expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    value = metadata.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise typer.BadParameter(
            f"Invalid draft manifest {field} metadata for edition {edition_name!r}, issue {issue_number:03d}: "
            "expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    return dict(value)


def _project_draft_metadata_value(
    payload: dict[str, Any] | None, field: str, edition_name: str, issue_number: int,
) -> Any:
    if payload is None:
        return None
    metadata = payload.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise typer.BadParameter(
            f"Invalid draft manifest metadata for edition {edition_name!r}, issue {issue_number:03d}: "
            "expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    return metadata.get(field)


def load_readiness_gate_settings(
    *,
    edition_name: str,
    program_id: str | None,
    editions_root: Path,
    programs_root: Path,
) -> tuple[bool, int | None]:
    program_enabled = False
    snapshot_max_age_days: int | None = None

    if program_id is not None:
        program_path = programs_root / program_id / "program.yaml"
        program_document = load_optional_yaml_mapping(program_path) or {}
        readiness_document = program_document.get("readiness")
        if readiness_document is not None:
            if not isinstance(readiness_document, dict):
                raise typer.BadParameter(
                    f"Invalid readiness settings in {program_path}: expected a mapping. "
                    "Preserve the file and review it before retrying."
                )
            program_enabled = _readiness_gate_enabled(readiness_document, "gate", program_path)
            snapshot_max_age_days = _readiness_age(readiness_document, "snapshot_max_age_days", program_path)

    edition_yaml_path = editions_root / f"{edition_name}.yaml"
    edition_document = load_optional_yaml_mapping(edition_yaml_path)
    if edition_document is None:
        edition_yaml_path = find_edition_yaml(edition_name, programs_root=programs_root)
        edition_document = load_optional_yaml_mapping(edition_yaml_path) or {}
    edition_enabled = _readiness_gate_enabled(edition_document, "readiness_gate", edition_yaml_path)
    if snapshot_max_age_days is None:
        snapshot_max_age_days = _readiness_age(edition_document, "readiness_snapshot_max_age_days", edition_yaml_path)

    return program_enabled or edition_enabled, snapshot_max_age_days


def load_optional_yaml_mapping(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise typer.BadParameter(
            f"Cannot read readiness settings at {path} ({type(error).__name__}). "
            "Preserve the file; check access and UTF-8 before retrying."
        ) from error
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as error:
        raise typer.BadParameter(
            f"Cannot parse readiness settings at {path} ({type(error).__name__}). "
            "Preserve the file; check YAML syntax and values before retrying."
        ) from error
    if not isinstance(document, dict):
        raise typer.BadParameter(
            f"Invalid readiness settings at {path}: expected a YAML mapping. "
            "Preserve the file and review it before retrying."
        )
    return document


def _readiness_gate_enabled(document: dict[str, Any], field: str, path: Path) -> bool:
    value = document.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise typer.BadParameter(
            f"Invalid readiness setting {field} in {path}: expected a boolean. "
            "Preserve the file and review it before retrying."
        )
    return value


def _readiness_age(document: dict[str, Any], field: str, path: Path) -> int | None:
    value = document.get(field)
    age = coerce_optional_int(value)
    if value is not None and age is None:
        raise typer.BadParameter(
            f"Invalid readiness setting {field} in {path}: expected an integer age in days. "
            "Preserve the file and review it before retrying."
        )
    return age


def coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def load_draft_ai_safety_metadata(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    return _load_draft_metadata_field(
        "ai_safety", edition_name=edition_name, issue_number=issue_number, programs_root=programs_root,
    )


def load_confirm_review_status(
    edition_name: str,
    issue_number: int,
    workstream_section_ids: tuple[str, ...],
    reports_root: Path,
    *,
    programs_root: Path | None = None,
) -> ReviewStatus:
    expected_section_ids = ("exec_summary",) + tuple(f"ws:{section_id}" for section_id in workstream_section_ids)
    existing = load_review_status(
        edition_name,
        reports_root=reports_root,
        **({"programs_root": programs_root} if programs_root is not None else {}),
    )
    if existing is not None and existing.issue_number == issue_number:
        existing_sections = {section.section_id: section for section in existing.sections}
        return ReviewStatus(
            issue_number=issue_number,
            sections=tuple(
                existing_sections.get(
                    section_id,
                    ReviewSection(
                        section_id=section_id,
                        state=ReviewState.PENDING,
                        reviewer=None,
                        note=None,
                        updated_at=None,
                    ),
                )
                for section_id in expected_section_ids
            ),
        )
    return ReviewStatus(
        issue_number=issue_number,
        sections=tuple(
            ReviewSection(
                section_id=section_id,
                state=ReviewState.PENDING,
                reviewer=None,
                note=None,
                updated_at=None,
            )
            for section_id in expected_section_ids
        ),
    )


def load_draft_state(edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> dict[str, Any]:
    try:
        payload = load_optional_draft_state(edition_name, issue_number, programs_root=programs_root)
    except StateError as error:
        raise typer.BadParameter(str(error)) from error
    if payload is None:
        path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"
        raise typer.BadParameter(
            f"Draft state not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    return payload


def load_current_draft_manifest_id(edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> str:
    payload = _load_manifest(edition_name, issue_number, programs_root=programs_root)
    return _require_manifest_id(payload, edition_name, issue_number, programs_root=programs_root)


def _require_manifest_id(
    payload: dict[str, Any] | None, edition_name: str, issue_number: int, *, programs_root: Path,
) -> str:
    if payload is None:
        path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
        raise typer.BadParameter(
            f"Manifest not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    manifest_id = payload.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise typer.BadParameter(
            f"Invalid draft manifest for edition {edition_name!r}, issue {issue_number:03d}: "
            "manifest_id must be a nonblank string. Preserve the existing artifact and review it before retrying."
        )
    return manifest_id
