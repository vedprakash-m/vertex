"""WS-6 coding stub: doctor --confirm-readiness check.

Enumerates exact live blockers that would prevent a non-forced
``vertex confirm`` from succeeding.  Reads existing static files
(overrides, gather state, archive index) — no pipeline re-run.

Blockers checked (and remediation commands):
  B-1  No overrides file found               → vertex report
  B-2  One or more dimensions have           → vertex doctor --ids --edition <name>
       '❓ Needs Input' risk level            →   then edit overrides/<prog>/issue_NNN.yaml
  B-3  Gather state absent (never gathered)  → vertex gather --edition <name>
  B-4  Gather state stale (> 2 × cadence)    → vertex gather --edition <name>
  B-5  No confirmed issue yet (first run     → confirms after report + overrides
       — not a hard block, info-only)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.gather_state_store import load_gather_state
from src.core.overrides_store import NEEDS_INPUT_VALUE

_STALE_MULTIPLIER = 2   # gather is "stale" when older than 2× edition cadence
_CADENCE_DAYS: dict[str, int] = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 90,
}
_NEEDS_INPUT_MARKERS = frozenset(
    {NEEDS_INPUT_VALUE, "needs_input", "unknown", ""}
)


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
    """Return a DoctorReport summarising what would block a non-forced confirm.

    Exit-code mapping (via ``DoctorReport.failures``):
    - 0 failures → confirm would succeed  (overall = "ok" or "warn")
    - ≥1 failure → confirm is blocked      (overall = "fail")
    """
    now = now or datetime.now(timezone.utc)
    checks: list[DoctorCheck] = []

    # ------------------------------------------------------------------
    # B-1 / B-2: overrides file + Needs Input risk levels
    # ------------------------------------------------------------------
    program_dir = programs_root / program_id
    overrides_dir = program_dir / "overrides"
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
        needs_input = _find_needs_input_dimensions(overrides_path)
        if needs_input:
            joined = ", ".join(needs_input)
            checks.append(DoctorCheck(
                label="Overrides",
                status="fail",
                detail=(
                    f"{len(needs_input)} dimension(s) have '❓ Needs Input' risk level: {joined}. "
                    f"Edit {overrides_path} and set a valid risk level for each."
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
        else:
            checks.append(DoctorCheck(
                label="Overrides",
                status="ok",
                detail=f"All dimensions have confirmed risk levels in {overrides_path.name}.",
                metadata={"program_id": program_id},
            ))

    # ------------------------------------------------------------------
    # B-3 / B-4: gather state freshness
    # ------------------------------------------------------------------
    gather_state = load_gather_state(program_id, programs_root=programs_root)
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
    archive_index_path = archive_root / edition_name / "archive_index.json"
    if not archive_index_path.exists():
        checks.append(DoctorCheck(
            label="Archive",
            status="info",
            detail=(
                f"No confirmed issues yet for edition '{edition_name}' "
                "(this is expected for the very first confirm)."
            ),
            metadata={"edition_name": edition_name},
        ))
    else:
        try:
            index_payload = json.loads(archive_index_path.read_text(encoding="utf-8"))
            confirmed_count = len(index_payload.get("issues", []))
            checks.append(DoctorCheck(
                label="Archive",
                status="ok",
                detail=f"{confirmed_count} confirmed issue(s) in archive for edition '{edition_name}'.",
                metadata={"edition_name": edition_name, "confirmed_count": confirmed_count},
            ))
        except (json.JSONDecodeError, KeyError):
            checks.append(DoctorCheck(
                label="Archive",
                status="warn",
                detail=f"Archive index at {archive_index_path.name} could not be parsed.",
                metadata={"edition_name": edition_name},
            ))

    return DoctorReport(edition=edition_name, checks=tuple(checks))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _latest_overrides_path(overrides_dir: Path) -> Path | None:
    """Return the overrides YAML with the highest issue number, or None."""
    if not overrides_dir.is_dir():
        return None
    candidates = sorted(overrides_dir.glob("issue_*.yaml"))
    return candidates[-1] if candidates else None


def _find_needs_input_dimensions(overrides_path: Path) -> list[str]:
    """Return every risk-bearing override path still awaiting a decision.

    Legacy overrides use a flat ``dimensions.<name>.risk`` shape while
    program scorecards use ``scorecards.<scorecard>.<dimension>.risk``.  The
    previous one-level scan reported the scorecard itself when its nested
    dimension mappings did not have a direct ``risk`` key, masking the actual
    individual PM decisions required for a safe first confirm.
    """
    try:
        doc = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    dimensions = doc.get("dimensions") or doc.get("scorecards") or {}
    if not isinstance(dimensions, dict):
        return []
    blocked: list[str] = []

    def collect(path: str, body: Any) -> None:
        if not isinstance(body, dict):
            return
        if "risk" in body:
            risk_value = str(body.get("risk", "") or "").strip()
            if risk_value in _NEEDS_INPUT_MARKERS:
                blocked.append(path)
            return

        child_mappings = [
            (str(name), value)
            for name, value in body.items()
            if isinstance(value, dict)
        ]
        if child_mappings:
            for child_name, child_body in child_mappings:
                collect(f"{path} / {child_name}", child_body)
            return

        # A direct dimension mapping without a risk is as unresolved as a
        # visible `Needs Input` marker; retain that established validation
        # behavior for malformed or incomplete flat overrides.
        blocked.append(path)

    for dim_name, dim_body in dimensions.items():
        collect(str(dim_name), dim_body)
    return blocked
