"""GAP-32: Platform self-observability (vertex doctor's view of itself).

This module emits operator alerts when the platform's own run-time
behavior deviates from healthy norms. Examples:
  - **Yield collapse**: 3+ consecutive gather cycles produced zero
    ADO/Kusto signals for an active workstream (mirrors QG-25's
    "email signal yield zero" advisory, but for any source channel).
  - **AI safety drop rate**: more than 50% of AI generations over the
    last N runs were dropped by `process_generated_text`'s
    injection/ban-list filter — a leading indicator of either an
    upstream prompt regression or a noisy ban-list.

These are *not* business-logic alerts. They are operator-visible
"the platform is sick" alerts surfaced through the existing
`_alerts/alerts.jsonl` channel (WS-17 / NG-3) so the next
`vertex gather / confirm / doctor` run picks them up.

The detector is intentionally simple and idempotent: it appends a
new alert only when the rule is currently firing AND no open alert
with the same `alert_id` already exists. Resolution is automatic
on the next run when the rule no longer fires (an alert's
`resolved_at` row is appended).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from src.core.alerts import (
    AlertRecord,
    AlertSeverity,
    append_alert,
    read_alerts,
)
from src.core.ai_telemetry import read_ai_telemetry
from src.core.gather_state_store import load_gather_state
from src.core.journal import PROGRAMS_ROOT


# ---------------------------------------------------------------------------
# Detector data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class YieldCollapseDetection:
    program_id: str
    workstream_id: str
    consecutive_zero_cycles: int
    last_signal_at: datetime | None


@dataclass(frozen=True, slots=True)
class AiSafetyDropDetection:
    program_id: str
    total_generations: int
    dropped_generations: int
    drop_rate: float


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def detect_yield_collapse(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    cycles_threshold: int = 3,
) -> YieldCollapseDetection | None:
    """Detect a workstream with 3+ consecutive zero-yield gather cycles.

    The check is per-workstream: a single silent workstream for too
    long should fire, even if other workstreams are healthy. The
    result is the **worst** workstream (most consecutive zeros) or
    None if no workstream has hit the threshold.
    """
    state = load_gather_state(program_id, programs_root=programs_root)
    if state is None or not getattr(state, "workstreams", None):
        return None
    worst: YieldCollapseDetection | None = None
    for ws in state.workstreams:  # type: ignore[attr-defined]
        zeros = int(getattr(ws, "consecutive_zero_signal_cycles", 0) or 0)
        if zeros >= cycles_threshold:
            detection = YieldCollapseDetection(
                program_id=program_id,
                workstream_id=str(getattr(ws, "workstream_id", "")),
                consecutive_zero_cycles=zeros,
                last_signal_at=getattr(ws, "last_signal_at", None),
            )
            if worst is None or zeros > worst.consecutive_zero_cycles:
                worst = detection
    return worst


def detect_ai_safety_drop_rate(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    lookback: int = 20,
    drop_rate_threshold: float = 0.5,
) -> AiSafetyDropDetection | None:
    """Detect AI safety dropping more than 50% of generations recently.

    A "drop" is any AI telemetry row with `status in
    ("injection_dropped", "ban_dropped", "discarded")` — i.e. the
    generation was produced but suppressed by the safety pipeline.
    """
    try:
        telemetry = read_ai_telemetry(program_id, programs_root=programs_root)
    except Exception:
        return None
    if not telemetry:
        return None
    recent = list(telemetry)[-lookback:]
    total = len(recent)
    if total < 5:
        return None
    dropped = sum(
        1 for row in recent
        if str(getattr(row, "status", "") or "").lower()
        in {"injection_dropped", "ban_dropped", "discarded", "blocked"}
    )
    drop_rate = dropped / total
    if drop_rate < drop_rate_threshold:
        return None
    return AiSafetyDropDetection(
        program_id=program_id,
        total_generations=total,
        dropped_generations=dropped,
        drop_rate=drop_rate,
    )


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _alert_id(program_id: str, category: str) -> str:
    return f"platform-{category}-{program_id}"


def emit_platform_alerts(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    """Run all platform self-observability checks and emit alerts.

    Returns the list of alert_ids emitted on this call (so the caller
    can log/audit them). Idempotent: an alert is only appended when
    the rule is firing AND no open alert with the same `alert_id`
    exists.
    """
    now = datetime.now(timezone.utc)
    emitted: list[str] = []
    open_alerts = read_alerts(
        program_id, programs_root=programs_root, include_resolved=False
    )
    open_ids = {a.alert_id for a in open_alerts}

    yield_collapse = detect_yield_collapse(program_id, programs_root=programs_root)
    if yield_collapse is not None:
        alert_id = _alert_id(program_id, "yield-collapse")
        if alert_id not in open_ids:
            append_alert(
                AlertRecord(
                    alert_id=alert_id,
                    program_id=program_id,
                    severity=AlertSeverity.WARN,
                    category="platform.yield_collapse",
                    message=(
                        f"Workstream {yield_collapse.workstream_id!r} produced "
                        f"zero signals for {yield_collapse.consecutive_zero_cycles} "
                        f"consecutive gather cycles."
                    ),
                    next_command="vertex doctor --diagnose " + program_id,
                    created_at=now,
                ),
                programs_root=programs_root,
            )
            emitted.append(alert_id)

    safety_drop = detect_ai_safety_drop_rate(
        program_id, programs_root=programs_root
    )
    if safety_drop is not None:
        alert_id = _alert_id(program_id, "ai-safety-drop-rate")
        if alert_id not in open_ids:
            append_alert(
                AlertRecord(
                    alert_id=alert_id,
                    program_id=program_id,
                    severity=AlertSeverity.WARN,
                    category="platform.ai_safety_drop",
                    message=(
                        f"AI safety dropped "
                        f"{safety_drop.dropped_generations}/"
                        f"{safety_drop.total_generations} "
                        f"({safety_drop.drop_rate:.0%}) recent generations."
                    ),
                    next_command=(
                        "vertex doctor --ai-proposals " + program_id
                    ),
                    created_at=now,
                ),
                programs_root=programs_root,
            )
            emitted.append(alert_id)

    return tuple(emitted)
