"""Program Fact Store drift gate (QG-SG-20).

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3). This
cluster implements the state-drift warning: it compares the Program Fact Store
snapshot pinned at ``report`` time against the live store and blocks confirm if
accepted revisions drifted in between (INV-SG-12 / §7.4.9). It depends only on
the gate value objects and ``ProgramFactStore``. The package ``__init__``
re-exports both entry points so existing imports keep working.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from src.core.exceptions import ConfigError
from src.core.manifest_writer import hash_jsonable
from src.core.pipeline import capture_milestone_trajectory_snapshot
from src.core.program_fact_store import ProgramFactStore
from src.core.quality_gates.models import GateEvaluation, QualityGateReport
from src.core.store_factory import build_trajectory_store_for_program_id


_UNSET = object()


def evaluate_program_fact_drift_gate(
    *,
    snapshot_id: str | None,
    drifted_facts: Collection[Any],
) -> QualityGateReport:
    if not snapshot_id:
        return QualityGateReport(results=())
    if not drifted_facts:
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id="QG-SG-20",
                    passed=True,
                    message="Program fact drift gate passed.",
                    exit_code=0,
                ),
            )
        )
    sampled_facts = ", ".join(
        sorted(
            {
                f"{getattr(fact, 'fact_type', 'fact')}:{getattr(fact, 'natural_key', '')[:12]}"
                for fact in tuple(drifted_facts)[:3]
            }
        )
    )
    drift_summary = f" ({sampled_facts})" if sampled_facts else ""
    return QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-SG-20",
                passed=False,
                message=(
                    "State Drift Warning: the live Program Fact Store changed after the draft snapshot "
                    f"was pinned ({len(drifted_facts)} accepted revision(s) drifted since {snapshot_id})"
                    f"{drift_summary}. Re-run `vertex report` and re-review before confirming."
                ),
                exit_code=3,
            ),
        )
    )


def evaluate_program_fact_drift_from_draft(
    *,
    draft_state: Mapping[str, Any],
    program_id: str | None,
    db_root: Any = None,
    edition_name: str | None = None,
    issue_number: int | None = None,
    milestone_assessments: Any = _UNSET,
    milestone_trajectory_snapshot: Any = _UNSET,
    programs_root: Path | None = None,
) -> QualityGateReport:
    fact_report = _evaluate_program_fact_pin_drift_from_draft(
        draft_state=draft_state,
        program_id=program_id,
        db_root=db_root,
        edition_name=edition_name,
        issue_number=issue_number,
    )
    if milestone_assessments is _UNSET and milestone_trajectory_snapshot is _UNSET:
        return fact_report
    trajectory_result = _evaluate_milestone_trajectory_drift(
        program_id=program_id,
        assessments=milestone_assessments,
        captured=milestone_trajectory_snapshot,
        edition_type=draft_state.get("edition_type"),
        programs_root=programs_root,
    )
    return QualityGateReport(results=(*fact_report.results, *trajectory_result))


def _evaluate_program_fact_pin_drift_from_draft(
    *,
    draft_state: Mapping[str, Any],
    program_id: str | None,
    db_root: Any = None,
    edition_name: str | None = None,
    issue_number: int | None = None,
) -> QualityGateReport:
    """QG-SG-20 wrapper that resolves the pinned snapshot from ``draft_state``.

    Reads the ``program_fact_snapshot`` pin recorded at ``report`` time, then
    diffs it against the live store via :meth:`ProgramFactStore.detect_drift`.
    Returns an empty report only when no pin metadata is declared. Malformed
    declarations block rather than disabling the gate (INV-SG-12 / §7.4.9).
    """
    if "program_fact_snapshot" not in draft_state:
        return QualityGateReport(results=())
    raw_snapshot = draft_state.get("program_fact_snapshot")
    snapshot_id = raw_snapshot.get("snapshot_id") if isinstance(raw_snapshot, dict) else None
    if (
        not isinstance(raw_snapshot, dict)
        or not isinstance(snapshot_id, str)
        or not snapshot_id.strip()
        or not isinstance(program_id, str)
        or not program_id.strip()
    ):
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id="QG-SG-20",
                    passed=False,
                    message=(
                        "Declared program fact snapshot is invalid or its evaluation program is missing. "
                        "Re-run `vertex report` and re-review before confirming."
                    ),
                    exit_code=3,
                ),
            )
        )
    snapshot_program_id = raw_snapshot.get("program_id")
    if snapshot_program_id not in (None, program_id):
        return evaluate_program_fact_drift_gate(
            snapshot_id=snapshot_id,
            drifted_facts=(f"program_id_mismatch:{snapshot_program_id}",),
        )
    store = ProgramFactStore(program_id, db_root=db_root)
    if edition_name is not None or issue_number is not None:
        pin = store.load_snapshot_pin(snapshot_id)
        if pin is not None and any(
            expected is not None
            and key in pin.metadata
            and (type(pin.metadata[key]) is not type(expected) or pin.metadata[key] != expected)
            for key, expected in (("edition_name", edition_name), ("issue_number", issue_number))
        ):
            return QualityGateReport(
                results=(
                    GateEvaluation(
                        gate_id="QG-SG-20",
                        passed=False,
                        message="Program fact snapshot scope does not match this edition and issue. Re-run `vertex report` and re-review.",
                        exit_code=3,
                    ),
                )
            )
    try:
        drifted_facts = store.detect_drift(snapshot_id)
    except KeyError as exc:
        raise ConfigError(
            "Declared program fact snapshot is unavailable. Re-run `vertex report` and re-review before confirming."
        ) from exc
    return evaluate_program_fact_drift_gate(
        snapshot_id=snapshot_id,
        drifted_facts=drifted_facts,
    )


def _evaluate_milestone_trajectory_drift(
    *,
    program_id: str | None,
    assessments: Any,
    captured: Any,
    edition_type: Any,
    programs_root: Path | None,
) -> tuple[GateEvaluation, ...]:
    if edition_type == "lookback":
        return ()
    if not isinstance(assessments, list):
        return _milestone_trajectory_failure("Milestone assessment metadata is missing or invalid.")
    if not assessments:
        return ()
    if not isinstance(program_id, str) or not program_id.strip():
        return _milestone_trajectory_failure("Milestone assessment program is missing.")

    assessed_ids: set[int] = set()
    milestone_ids: set[str] = set()
    for row in assessments:
        if not isinstance(row, dict):
            return _milestone_trajectory_failure("Milestone assessment metadata is invalid.")
        milestone_id = row.get("milestone_id")
        if not isinstance(milestone_id, str) or not milestone_id.strip() or milestone_id in milestone_ids:
            return _milestone_trajectory_failure("Milestone assessment scope is invalid.")
        milestone_ids.add(milestone_id)
        row_item_ids = _canonical_positive_ids(row.get("linked_work_item_ids"))
        if row_item_ids is None:
            return _milestone_trajectory_failure(
                "Assessed milestone item scope is missing or invalid; re-run the report."
            )
        assessed_ids.update(row_item_ids)
    expected_ids = sorted(assessed_ids)

    if not isinstance(captured, dict):
        return _milestone_trajectory_failure("Assessed milestone trajectory capture is missing or invalid.")
    if set(captured) != {"schema_version", "program_id", "backend", "items", "sha256"}:
        return _milestone_trajectory_failure("Assessed milestone trajectory capture has an unsupported shape.")
    if captured.get("schema_version") != "1" or captured.get("program_id") != program_id:
        return _milestone_trajectory_failure("Assessed milestone trajectory capture has an invalid version or program.")
    backend = captured.get("backend")
    if backend not in {"file", "sqlite"}:
        return _milestone_trajectory_failure("Assessed milestone trajectory backend is invalid.")
    items = captured.get("items")
    if not isinstance(items, list):
        return _milestone_trajectory_failure("Assessed milestone trajectory item list is invalid.")
    captured_item_ids: list[int] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"work_item_id", "points"}:
            return _milestone_trajectory_failure("Assessed milestone trajectory item is invalid.")
        work_item_id = item.get("work_item_id")
        if type(work_item_id) is not int or work_item_id <= 0:
            return _milestone_trajectory_failure("Assessed milestone trajectory item ID is invalid.")
        captured_item_ids.append(work_item_id)
        points = item.get("points")
        if not isinstance(points, list):
            return _milestone_trajectory_failure("Assessed milestone trajectory points are invalid.")
        for point in points:
            if not isinstance(point, dict) or set(point) != {"date", "state", "target_date"}:
                return _milestone_trajectory_failure("Assessed milestone trajectory point is invalid.")
            if not _is_iso_date(point.get("date")) or not isinstance(point.get("state"), str):
                return _milestone_trajectory_failure("Assessed milestone trajectory point is invalid.")
            target_date = point.get("target_date")
            if target_date is not None and not _is_iso_date(target_date):
                return _milestone_trajectory_failure("Assessed milestone trajectory point is invalid.")
    if captured_item_ids != expected_ids:
        return _milestone_trajectory_failure("Assessed milestone trajectory scope does not match the assessment rows.")
    payload = {key: value for key, value in captured.items() if key != "sha256"}
    if captured.get("sha256") != hash_jsonable(payload):
        return _milestone_trajectory_failure("Assessed milestone trajectory digest is invalid.")

    try:
        if programs_root is None:
            trajectory_store = build_trajectory_store_for_program_id(program_id)
        else:
            trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
        current = capture_milestone_trajectory_snapshot(
            program_id,
            trajectory_store,
            {item_id: trajectory_store.read(program_id, item_id) for item_id in expected_ids},
        )
    except Exception:  # noqa: BLE001 -- unavailable evidence must fail closed at confirmation
        return _milestone_trajectory_failure("Assessed milestone trajectories are unavailable; re-run the report.")
    if current["backend"] != backend:
        return _milestone_trajectory_failure("Assessed milestone trajectory backend changed; re-run the report.")
    if current["items"] != items:
        return _milestone_trajectory_failure("Assessed milestone trajectories changed; re-run the report.")
    return ()


def _canonical_positive_ids(value: Any) -> list[int] | None:
    if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value):
        return None
    if value != sorted(set(value)):
        return None
    return value


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _milestone_trajectory_failure(message: str) -> tuple[GateEvaluation, ...]:
    return (
        GateEvaluation(
            gate_id="QG-SG-20",
            passed=False,
            message=f"{message} Preserve the draft and re-run `vertex report` before confirming.",
            exit_code=3,
        ),
    )
