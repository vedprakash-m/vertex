"""Cross-source conflict detection for workstream evidence (P4-10, §7.8).

Detects disagreements between an M365 ``WorkstreamEvidence`` record and the
quantitative signal sources (IcM blockers, Kusto metrics) that travel in a
``WorkstreamEvidenceBundle``, and surfaces them as ``RealityConflict`` items
(the existing type at ``src/core/program_reality.py:154``). P4-10 does not
introduce a new type — it wires M365 evidence into the existing conflict
infrastructure so disagreements reach the author rather than being silently merged.

Detected patterns (spec §7.8):

* **IcM Sev1/Sev2 active vs non-blocking evidence risk** — IcM wins by authority
  (§8.3); the disagreement is surfaced as ``family="icm_vs_evidence_risk"``.
* **Evidence marked ``blocked`` vs a Kusto metric whose text suggests progression**
  (``increase`` / ``progressing`` / ``completed`` / ``done`` / ``passing`` /
  ``improving``) — ``family="blocked_vs_kusto_progress"``; the author decides.

The detector is deterministic and conservative: it only flags patterns it can
read from structured signal fields + risk level, never mutates the evidence, and
returns conflicts that the bundle can attach for downstream doctor/confirm
surfacing.

Zone A: imports only from ``src/core/``.
"""
from __future__ import annotations

from typing import Iterable

from src.core.evidence_models import WorkstreamEvidence, extract_icm_ids
from src.core.models_v2 import Signal
from src.core.program_reality import RealityConflict

# risk_level values that already acknowledge a blocking condition.
_BLOCKING_RISK_LEVELS = frozenset({"blocked", "high"})

# Kusto signal-text stems that suggest forward progression (vs a "blocked" risk).
# Stems (not whole words) so conjugations match: increase/increasing, pass/passing, etc.
_PROGRESSION_TOKENS = (
    "increas",
    "progress",
    "complet",
    "done",
    "pass",
    "improv",
    "rollout",
    "rolling out",
)


def detect_evidence_conflicts(
    *,
    m365_evidence: WorkstreamEvidence | None,
    icm_blockers: Iterable[Signal] = (),
    kusto_metrics: Iterable[Signal] = (),
) -> tuple[RealityConflict, ...]:
    """Detect cross-source disagreements and return them as ``RealityConflict`` items.

    Returns an empty tuple when there is no M365 evidence (nothing to conflict
    against) or when no disagreement is detectable from the available fields.
    """
    if m365_evidence is None:
        return ()
    risk_value = _risk_value(m365_evidence.risk_level)
    lane_id = m365_evidence.lane_id
    conflicts: list[RealityConflict] = []

    # IcM Sev1/Sev2 active vs non-blocking evidence risk.
    icm_blockers = tuple(icm_blockers)
    for sig in icm_blockers:
        severity = _icm_severity(sig)
        if severity in (1, 2) and risk_value not in _BLOCKING_RISK_LEVELS:
            incident_id = _icm_incident_id(sig)
            entity_refs = (f"IcM:{incident_id}",) if incident_id else sig.entity_refs
            conflicts.append(RealityConflict(
                conflict_id=f"evd/icm-vs-risk/{lane_id}/{incident_id or sig.id}",
                entity_refs=entity_refs,
                family="icm_vs_evidence_risk",
                open=True,
                description=(
                    f"IcM Sev{severity} active blocker but evidence risk_level="
                    f"{risk_value or 'unknown'} is non-blocking for lane {lane_id}. "
                    "IcM wins by authority (§8.3)."
                ),
            ))

    # Evidence marked blocked vs a Kusto metric suggesting progression.
    if risk_value == "blocked":
        for sig in kusto_metrics:
            text = (sig.text or "").lower()
            if not text:
                continue
            if any(token in text for token in _PROGRESSION_TOKENS):
                conflicts.append(RealityConflict(
                    conflict_id=f"evd/blocked-vs-kusto/{lane_id}/{sig.id}",
                    entity_refs=sig.entity_refs,
                    family="blocked_vs_kusto_progress",
                    open=True,
                    description=(
                        f"Evidence risk_level=blocked but Kusto metric suggests progression: "
                        f"{(sig.text or '').strip()[:140]}. Author decides (§7.8)."
                    ),
                ))

    return tuple(conflicts)


def _risk_value(risk_level: object) -> str:
    value = getattr(risk_level, "value", None)
    if isinstance(value, str):
        return value
    return str(risk_level).lower() if risk_level else ""


def _icm_severity(signal: Signal) -> int | None:
    meta = signal.metadata if isinstance(signal.metadata, dict) else {}
    raw = meta.get("severity")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _icm_incident_id(signal: Signal) -> str | None:
    meta = signal.metadata if isinstance(signal.metadata, dict) else {}
    raw = meta.get("incident_id")
    if raw:
        return str(raw)
    blob = " ".join(signal.entity_refs) + " " + (signal.text or "")
    ids = extract_icm_ids(blob)
    return ids[0] if ids else None