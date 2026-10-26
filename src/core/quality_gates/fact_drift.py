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
from typing import Any

from src.core.exceptions import ConfigError
from src.core.program_fact_store import ProgramFactStore
from src.core.quality_gates.models import GateEvaluation, QualityGateReport


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
