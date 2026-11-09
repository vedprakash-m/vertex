"""WS-6 doctor --confirm-readiness check.

Inspects selected local prerequisites without re-running confirmation or
evaluating every quality gate.

Blockers checked (and remediation commands):
  B-1  No overrides file found               → vertex report
  B-2  One or more override risks are        → vertex doctor --ids --edition <name>
       unresolved                           →   then edit overrides/<prog>/issue_NNN.yaml
  B-3  Gather state absent (never gathered)  → vertex gather --edition <name>
  B-4  Gather state stale (> 2 × cadence)    → vertex gather --edition <name>
  B-5  No confirmed issue yet (first run     → confirms after report + overrides
       — not a hard block, info-only)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.archive_store import _parse_archive_index_payload
from src.core.edition_resolver import resolve_edition_paths
from src.core.exceptions import ConfigError, StateError, VertexMigrationError
from src.core.gather_state_store import load_gather_state
from src.core.models import RiskLevel
from src.core.overrides_store import (
    _detect_latest_v2_issue_number,
    _parse_dimension_override,
    _parse_scorecards,
)
from src.core.snapshot_store import get_archive_root
from src.core.workspace_paths import validate_report_workspace

_STALE_MULTIPLIER = 2   # gather is "stale" when older than 2× edition cadence
_CADENCE_DAYS: dict[str, int] = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 90,
}
_UNSAFE_OVERRIDE_NAME = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")


def run_confirm_readiness_doctor(
    *,
    edition_name: str,
    program_id: str,
    programs_root: Path,
    editions_root: Path,
    archive_root: Path,
    cadence: str = "weekly",
    now: datetime | None = None,
) -> DoctorReport:
    """Return a DoctorReport on selected local confirmation prerequisites.

    Exit-code mapping (via ``DoctorReport.failures``):
    - 0 failures → these checks found no blocking condition
    - ≥1 failure → at least one selected check failed

    The result does not establish that confirmation or all its gates will pass.
    """
    now = now or datetime.now(timezone.utc)
    checks: list[DoctorCheck] = []

    # ------------------------------------------------------------------
    # B-1 / B-2: overrides file + Needs Input risk levels
    # ------------------------------------------------------------------
    program_dir = programs_root / program_id
    overrides_dir = program_dir / "overrides"
    try:
        overrides_path = _latest_overrides_path(overrides_dir)
        if overrides_path is None or not overrides_path.exists():
            checks.append(DoctorCheck(
                label="Overrides",
                status="fail",
                detail=(
                    f"No overrides file found for program '{program_id}'. "
                    "Run: vertex report --edition <name>  to generate one."
                ),
                metadata={"program_id": program_id, "overrides_dir": str(overrides_dir)},
            ))
        else:
            needs_input, inspected_count = _find_needs_input_dimensions(overrides_path)
            if needs_input:
                joined = ", ".join(needs_input)
                checks.append(DoctorCheck(
                    label="Overrides",
                    status="fail",
                    detail=(
                        f"{len(needs_input)} dimension(s) have '❓ Needs Input' or invalid override risk: {joined}. "
                        f"Review {overrides_path}; confirmation also evaluates derived risks."
                    ),
                    metadata={
                        "program_id": program_id,
                        "overrides_path": str(overrides_path),
                        "needs_input_dimensions": list(needs_input),
                        "action_category": "pm-decision-required",
                        "owner": ["pm"],
                        "evidence_to_gather": [
                            "A reviewed risk level for each listed scorecard dimension",
                            "The rationale/evidence used for each human-authored risk decision",
                        ],
                        "llm_support": (
                            "Can summarize current evidence and draft a risk-decision brief, "
                            "but cannot assign or approve the risk levels."
                        ),
                    },
                ))
            elif inspected_count == 0:
                checks.append(DoctorCheck(
                    label="Overrides",
                    status="warn",
                    detail=(
                        "No visible risk overrides were available to inspect; "
                        "confirmation still evaluates the configured scorecard."
                    ),
                    metadata={"program_id": program_id},
                ))
            else:
                checks.append(DoctorCheck(
                    label="Overrides",
                    status="ok",
                    detail=(
                        f"No unresolved visible risk overrides found in {overrides_path.name}; "
                        "confirmation still evaluates the full scorecard."
                    ),
                    metadata={"program_id": program_id},
                ))
    except (OSError, UnicodeError, yaml.YAMLError, ConfigError, RecursionError):
        checks.append(DoctorCheck(
            label="Overrides",
            status="fail",
            detail=(
                "The selected overrides could not be read or validated. "
                "Preserve the file, correct its access or YAML structure, and retry."
            ),
            metadata={"program_id": program_id},
        ))

    # ------------------------------------------------------------------
    # B-3 / B-4: gather state freshness
    # ------------------------------------------------------------------
    try:
        gather_state = load_gather_state(program_id, programs_root=programs_root)
    except (StateError, OSError, UnicodeError, ValueError, OverflowError, RecursionError):
        checks.append(DoctorCheck(
            label="Gather State",
            status="fail",
            detail=(
                "Gather state could not be read or validated. "
                "Preserve it and check access or structure before retrying."
            ),
            metadata={"program_id": program_id},
        ))
    else:
        if gather_state is None:
            checks.append(DoctorCheck(
                label="Gather State",
                status="fail",
                detail=(
                    f"No gather state found for program '{program_id}'. "
                    f"Run: vertex gather --edition {edition_name}"
                ),
                metadata={"program_id": program_id},
            ))
        else:
            cadence_days = _CADENCE_DAYS.get(cadence, 7)
            stale_threshold = timedelta(days=cadence_days * _STALE_MULTIPLIER)
            age = now - gather_state.gathered_at.replace(tzinfo=timezone.utc) if gather_state.gathered_at.tzinfo is None else now - gather_state.gathered_at
            if age > stale_threshold:
                checks.append(DoctorCheck(
                    label="Gather State",
                    status="warn",
                    detail=(
                        f"Gather state is {age.days}d old (stale after {stale_threshold.days}d for {cadence} cadence). "
                        f"Run: vertex gather --edition {edition_name}"
                    ),
                    metadata={
                        "program_id": program_id,
                        "gathered_at": gather_state.gathered_at.isoformat(),
                        "age_days": age.days,
                        "stale_threshold_days": stale_threshold.days,
                    },
                ))
            else:
                checks.append(DoctorCheck(
                    label="Gather State",
                    status="ok",
                    detail=f"Gather state is {age.days}d old ({gather_state.gathered_at.isoformat()[:10]}).",
                    metadata={
                        "program_id": program_id,
                        "gathered_at": gather_state.gathered_at.isoformat(),
                        "age_days": age.days,
                    },
                ))

    # ------------------------------------------------------------------
    # B-5: confirmed issue exists (info-only)
    # ------------------------------------------------------------------
    try:
        paths = resolve_edition_paths(
            edition_name, editions_root=editions_root, programs_root=programs_root,
        )
        selected_programs_root = None
        if paths is not None:
            if (
                paths.program_id != program_id
                or not paths.program_dir.resolve().is_relative_to(programs_root.resolve())
            ):
                raise ConfigError("Archive edition does not belong to the selected program")
            if paths.edition_path.is_relative_to(programs_root):
                selected_programs_root = programs_root
        if selected_programs_root is None:
            validate_report_workspace(
                programs_root=programs_root,
                reports_root=programs_root.parent / "reports",
                archive_root=archive_root,
            )
        archive_index_path = get_archive_root(
            edition_name, archive_root, programs_root=selected_programs_root,
        ) / "index.json"
        selected_home = (
            paths.program_dir
            if paths is not None and (
                selected_programs_root is not None or archive_index_path.is_relative_to(paths.program_dir)
            )
            else archive_root
        )
        if not archive_index_path.resolve().is_relative_to(selected_home.resolve()):
            raise ConfigError("Archive index escapes its selected workspace")
        try:
            index_text = archive_index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            try:
                archive_index_path.lstat()
            except FileNotFoundError:
                confirmed_count = 0
            else:
                raise
        else:
            index_payload = json.loads(index_text)
            if (
                not isinstance(index_payload, dict)
                or not isinstance(index_payload.get("issues"), list)
                or any(not isinstance(entry, dict) for entry in index_payload["issues"])
                or any(
                    "kind" in entry and (
                        not isinstance(entry["kind"], str)
                        or entry["kind"].strip().lower() not in ("confirmed", "skipped")
                    )
                    for entry in index_payload["issues"]
                )
            ):
                raise ConfigError("Archive index has an invalid shape")
            index = _parse_archive_index_payload(edition_name, index_payload)
            if index.edition != edition_name:
                raise ConfigError("Archive index edition does not match the selected edition")
            confirmed_count = sum(entry.kind == "confirmed" for entry in index.issues)
        if confirmed_count:
            checks.append(DoctorCheck(
                label="Archive",
                status="ok",
                detail=(
                    f"Confirmed archive entries: {confirmed_count} in the selected archive index "
                    f"for edition '{edition_name}'; archive integrity was not assessed."
                ),
                metadata={"edition_name": edition_name, "confirmed_count": confirmed_count},
            ))
        else:
            checks.append(DoctorCheck(
                label="Archive",
                status="info",
                detail=(
                    f"No confirmed entries were found in the selected archive index for edition '{edition_name}' "
                    "(expected before the first confirm); archive integrity was not assessed."
                ),
                metadata={"edition_name": edition_name},
            ))
    except (
        ConfigError, VertexMigrationError, OSError, UnicodeError, ValueError, TypeError,
        KeyError, OverflowError, RecursionError, RuntimeError,
    ):
        checks.append(DoctorCheck(
            label="Archive",
            status="warn",
            detail=(
                "The selected archive index could not be read or assessed. "
                "Preserve it and check its access, structure and workspace."
            ),
            metadata={"edition_name": edition_name},
        ))

    return DoctorReport(edition=edition_name, checks=tuple(checks))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _latest_overrides_path(overrides_dir: Path) -> Path | None:
    """Return the overrides YAML with the highest issue number, or None."""
    issue_number = _detect_latest_v2_issue_number(overrides_dir.parent)
    return overrides_dir / f"issue_{issue_number:03d}.yaml" if issue_number is not None else None


def _is_inspectable_override_name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and _UNSAFE_OVERRIDE_NAME.search(value) is None


def _find_needs_input_dimensions(overrides_path: Path) -> tuple[list[str], int]:
    """Return unresolved visible paths and the number of visible overrides checked."""
    doc = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    if doc is None:
        return [], 0
    if not isinstance(doc, dict):
        raise ConfigError("Overrides must be a mapping")

    if "scorecards" in doc:
        raw_scorecards = doc["scorecards"]
        if raw_scorecards is None:
            return [], 0
        if isinstance(raw_scorecards, dict):
            for name, card in raw_scorecards.items():
                if (
                    not _is_inspectable_override_name(name)
                    or not isinstance(card, dict)
                    or any(not _is_inspectable_override_name(dimension_name) for dimension_name in card)
                ):
                    raise ConfigError("Scorecard names and dimension keys must be inspectable")
        elif isinstance(raw_scorecards, list):
            for card in raw_scorecards:
                if (
                    not isinstance(card, dict)
                    or not _is_inspectable_override_name(card.get("name"))
                    or not isinstance(card.get("dimensions", []), list)
                ):
                    raise ConfigError("Scorecard dimensions must be a list")
                if any(
                    not isinstance(dimension, dict)
                    or not _is_inspectable_override_name(dimension.get("name"))
                    for dimension in card.get("dimensions", [])
                ):
                    raise ConfigError("Scorecard dimensions must have inspectable names")
        else:
            raise ConfigError("Scorecards must be a mapping or list")
        dimensions = (
            (f"{card.name} / {dimension.name}", dimension)
            for card in _parse_scorecards(raw_scorecards)
            for dimension in card.dimensions
        )
    else:
        raw_dimensions = doc.get("dimensions")
        if raw_dimensions is None:
            return [], 0
        if not isinstance(raw_dimensions, dict) or any(
            not _is_inspectable_override_name(name) for name in raw_dimensions
        ):
            raise ConfigError("Dimensions must have inspectable keys")
        dimensions = (
            (str(name), _parse_dimension_override(str(name), value))
            for name, value in raw_dimensions.items()
        )

    blocked: list[str] = []
    inspected_count = 0
    for path, dimension in dimensions:
        if dimension.hide_from_scorecard:
            continue
        inspected_count += 1
        if dimension.risk is None or dimension.risk == RiskLevel.UNKNOWN:
            blocked.append(path)
    return blocked, inspected_count
