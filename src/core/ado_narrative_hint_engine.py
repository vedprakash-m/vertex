from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Any

from src.core.models import Confidence, DeltaSet, WorkItem, RiskLevel, DeltaKind
from src.core.forecast_engine import ETAForecast
from src.core.scorecard_trends import ScorecardTrend
from src.core.models_v2 import Program, Signal

if TYPE_CHECKING:
    from src.core.overrides_store import GovernanceState


class HintKind(str, Enum):
    CLOSED = "CLOSED"
    RISK_UP = "RISK_UP"
    RISK_DOWN = "RISK_DOWN"
    ETA_CHANGED = "ETA_CHANGED"
    OWNER_CHANGED = "OWNER_CHANGED"
    NEW = "NEW"
    TREND_WORSENING = "TREND_WORSENING"
    TREND_IMPROVING = "TREND_IMPROVING"
    ICM_ACTIVE = "ICM_ACTIVE"
    ICM_MITIGATED = "ICM_MITIGATED"
    PR_MERGED = "PR_MERGED"
    PR_ACTIVE = "PR_ACTIVE"
    DFD_CHANGED = "DFD_CHANGED"
    ESCALATION_CHANGED = "ESCALATION_CHANGED"


@dataclass(frozen=True, slots=True)
class NarrativeDeltaHint:
    work_item_id: int | None      # None for TREND_* hints
    workstream_id: str
    hint_kind: HintKind
    old_value: str | None
    new_value: str | None
    work_item_title: str | None
    suggested_sentence: str
    confidence: Confidence
    hint_id: str                  # stable "{hint_kind}:{work_item_id_or_none}:{issue_number}"


@dataclass(frozen=True, slots=True)
class HintProposal:
    hint_id: str
    edition: str
    issue_number: int
    workstream_id: str
    hint_kind: str
    suggested_sentence: str
    status: str                  # "pending" | "accepted" | "rejected" | "modified"
    accepted_text: str | None = None


def generate_delta_hints(
    delta_set: DeltaSet,
    items: dict[int, WorkItem],
    issue_number: int,
    program: Program,
    forecasts: dict[int, ETAForecast] | None = None,
    trends: dict[str | tuple[str, str], ScorecardTrend] | None = None,
    signals: tuple[Signal, ...] = (),
    governance: "GovernanceState | None" = None,
    prior_governance: "GovernanceState | None" = None,
) -> list[NarrativeDeltaHint]:
    hints: list[NarrativeDeltaHint] = []
    forecast_lookup = forecasts or {}
    trend_lookup = trends or {}

    # Helper function to compute stable hint_id
    def make_hint_id(kind: HintKind, item_id: int | str | None) -> str:
        return f"{kind.value}:{item_id or 'none'}:{issue_number}"

    # Helper function to get workstream_id from work item or fallback
    def get_workstream_id(wi: WorkItem) -> str:
        if hasattr(wi, "workstream_id") and wi.workstream_id:
            return str(wi.workstream_id)
        if hasattr(wi, "area_path") and wi.area_path:
            return wi.area_path.split("\\")[-1].lower()
        return program.id

    # 1. Closed Items
    for delta in delta_set.closed_items:
        wi = items.get(delta.work_item_id)
        if wi is None:
            continue
        ws_id = get_workstream_id(wi)
        sentence = f"{wi.title} is now closed."
        hints.append(
            NarrativeDeltaHint(
                work_item_id=delta.work_item_id,
                workstream_id=ws_id,
                hint_kind=HintKind.CLOSED,
                old_value=str(delta.field_changes.get("state", (None, None))[0]),
                new_value=str(delta.field_changes.get("state", (None, None))[1]),
                work_item_title=wi.title,
                suggested_sentence=sentence,
                confidence=Confidence.HIGH,
                hint_id=make_hint_id(HintKind.CLOSED, delta.work_item_id),
            )
        )

    # 2. Risk Changes
    for delta in delta_set.risk_changes:
        wi = items.get(delta.work_item_id)
        if wi is None:
            continue
        ws_id = get_workstream_id(wi)
        old_risk = delta.old_risk.value if delta.old_risk else "unknown"
        new_risk = delta.new_risk.value if delta.new_risk else "unknown"
        
        if delta.kind == DeltaKind.RISK_UP:
            kind = HintKind.RISK_UP
            sentence = f"{wi.title} elevated to {new_risk} (was {old_risk})."
        else:
            kind = HintKind.RISK_DOWN
            sentence = f"{wi.title} risk reduced to {new_risk} (was {old_risk})."
            
        hints.append(
            NarrativeDeltaHint(
                work_item_id=delta.work_item_id,
                workstream_id=ws_id,
                hint_kind=kind,
                old_value=old_risk,
                new_value=new_risk,
                work_item_title=wi.title,
                suggested_sentence=sentence,
                confidence=Confidence.HIGH,
                hint_id=make_hint_id(kind, delta.work_item_id),
            )
        )

    # 3. ETA Changes
    for delta in delta_set.eta_changes:
        wi = items.get(delta.work_item_id)
        if wi is None:
            continue
        ws_id = get_workstream_id(wi)
        old_eta_str = delta.old_eta.isoformat() if delta.old_eta else "none"
        new_eta_str = delta.new_eta.isoformat() if delta.new_eta else "none"
        
        sentence = f"Target date for {wi.title} moved from {old_eta_str} to {new_eta_str}."
        
        forecast = forecast_lookup.get(delta.work_item_id)
        if forecast is not None and forecast.annotation:
            sentence += f" — {forecast.annotation}"

        hints.append(
            NarrativeDeltaHint(
                work_item_id=delta.work_item_id,
                workstream_id=ws_id,
                hint_kind=HintKind.ETA_CHANGED,
                old_value=old_eta_str,
                new_value=new_eta_str,
                work_item_title=wi.title,
                suggested_sentence=sentence,
                confidence=Confidence.HIGH,
                hint_id=make_hint_id(HintKind.ETA_CHANGED, delta.work_item_id),
            )
        )

    # 4. Owner Changes
    for delta in delta_set.owner_changes:
        wi = items.get(delta.work_item_id)
        if wi is None:
            continue
        ws_id = get_workstream_id(wi)
        old_owner = str(delta.field_changes.get("assigned_to", (None, None))[0] or "unassigned")
        new_owner = str(delta.field_changes.get("assigned_to", (None, None))[1] or "unassigned")
        
        sentence = f"{wi.title} reassigned to {new_owner} (was {old_owner})."
        
        hints.append(
            NarrativeDeltaHint(
                work_item_id=delta.work_item_id,
                workstream_id=ws_id,
                hint_kind=HintKind.OWNER_CHANGED,
                old_value=old_owner,
                new_value=new_owner,
                work_item_title=wi.title,
                suggested_sentence=sentence,
                confidence=Confidence.HIGH,
                hint_id=make_hint_id(HintKind.OWNER_CHANGED, delta.work_item_id),
            )
        )

    # 5. New Items
    for delta in delta_set.new_items:
        wi = items.get(delta.work_item_id)
        if wi is None:
            continue
        ws_id = get_workstream_id(wi)
        risk = wi.risk_level.value if wi.risk_level else "unknown"
        eta = wi.target_date.isoformat() if wi.target_date else "none"
        
        sentence = f"New item tracked: {wi.title} (risk: {risk}, target: {eta})."
        
        hints.append(
            NarrativeDeltaHint(
                work_item_id=delta.work_item_id,
                workstream_id=ws_id,
                hint_kind=HintKind.NEW,
                old_value=None,
                new_value=str(delta.work_item_id),
                work_item_title=wi.title,
                suggested_sentence=sentence,
                confidence=Confidence.MEDIUM,
                hint_id=make_hint_id(HintKind.NEW, delta.work_item_id),
            )
        )

    # 6. Scorecard Trends
    for key, trend in trend_lookup.items():
        if isinstance(key, tuple):
            scorecard_name, dimension_name = key
        else:
            scorecard_name, dimension_name = "scorecard", str(key)
        ws_id = dimension_name.lower().replace(" ", "-")
        
        if trend.direction == "worsening":
            kind = HintKind.TREND_WORSENING
            sentence = f"{dimension_name} is on a worsening trajectory ({trend.consecutive_high_count} consecutive {trend.current_risk.value})."
        elif trend.direction == "improving":
            kind = HintKind.TREND_IMPROVING
            prior = trend.prior_risk.value if trend.prior_risk else "unknown"
            sentence = f"{dimension_name} improved to {trend.current_risk.value} (was {prior})."
        else:
            continue
            
        hints.append(
            NarrativeDeltaHint(
                work_item_id=None,
                workstream_id=ws_id,
                hint_kind=kind,
                old_value=trend.prior_risk.value if trend.prior_risk else None,
                new_value=trend.current_risk.value,
                work_item_title=None,
                suggested_sentence=sentence,
                confidence=Confidence.HIGH,
                hint_id=make_hint_id(kind, dimension_name),
            )
        )

    # 7. PR and IcM Signals from the Signal Journal
    for signal in signals:
        ws_id = signal.workstream_id or program.id
        
        # PR Merged / Active signals
        if signal.source == "ado" and signal.metadata and "kind" in signal.metadata:
            kind_str = signal.metadata["kind"]
            pr_id = signal.metadata.get("pr_id")
            title = signal.metadata.get("title")
            target_ref = signal.metadata.get("target_ref", "main")
            created_by = signal.metadata.get("created_by", "Unknown")

            # Skip malformed PR signals: without an id or title the hint sentence would
            # render placeholders like "None merged to main", polluting the newsletter.
            if pr_id is None or not title:
                continue

            if kind_str == "PR_MERGED":
                kind = HintKind.PR_MERGED
                sentence = f"{title} merged to {target_ref}."
            elif kind_str == "PR_ACTIVE":
                kind = HintKind.PR_ACTIVE
                sentence = f"PR open: {title} ({created_by}, targeting {target_ref})."
            else:
                continue
                
            hints.append(
                NarrativeDeltaHint(
                    work_item_id=pr_id,
                    workstream_id=ws_id,
                    hint_kind=kind,
                    old_value=None,
                    new_value=target_ref,
                    work_item_title=title,
                    suggested_sentence=sentence,
                    confidence=Confidence.HIGH,
                    hint_id=make_hint_id(kind, pr_id),
                )
            )
            
        # IcM Active / Mitigated signals
        elif signal.source == "icm" and signal.metadata:
            incident_id = signal.metadata.get("incident_id")
            severity = signal.metadata.get("severity", "?")
            status = str(signal.metadata.get("status", "") or "")
            title = signal.text or ""

            # Skip malformed IcM signals lacking an incident id; the resulting hint would
            # reference "IcM None" and cannot be linked back to the incident.
            if incident_id is None:
                continue

            import re
            clean_title = re.sub(r"^\[Sev\s+\d+\]\s*", "", title)
            
            if status.lower() == "active":
                kind = HintKind.ICM_ACTIVE
                sentence = f"IcM {incident_id} (Sev{severity}) is active: {clean_title}. Workstream: {ws_id}."
            elif status.lower() in {"mitigated", "resolved"}:
                kind = HintKind.ICM_MITIGATED
                ttm = signal.metadata.get("ttm")
                if ttm is not None:
                    sentence = f"IcM {incident_id} mitigated after {ttm} min. Impact: {ws_id}."
                else:
                    sentence = f"IcM {incident_id} mitigated. Impact: {ws_id}."
            else:
                continue
                
            hints.append(
                NarrativeDeltaHint(
                    work_item_id=int(incident_id) if str(incident_id).isdigit() else None,
                    workstream_id=ws_id,
                    hint_kind=kind,
                    old_value=None,
                    new_value=status,
                    work_item_title=clean_title,
                    suggested_sentence=sentence,
                    confidence=Confidence.HIGH,
                    hint_id=make_hint_id(kind, incident_id),
                )
            )

    # 8. Governance Deltas (DFD date changes, escalation changes)
    if governance is not None and prior_governance is not None:
        # DFD date change detection
        if governance.dfd_date != prior_governance.dfd_date:
            old_dfd = prior_governance.dfd_date.isoformat() if prior_governance.dfd_date else "none"
            new_dfd = governance.dfd_date.isoformat() if governance.dfd_date else "none"
            if governance.dfd_date is not None and prior_governance.dfd_date is not None:
                # Extension: prior DFD exists and was extended
                kind = HintKind.DFD_CHANGED
                sentence = f"DFD extended from {old_dfd} to {new_dfd}."
            elif governance.dfd_date is None:
                kind = HintKind.DFD_CHANGED
                sentence = f"DFD cleared (was {old_dfd})."
            else:
                kind = HintKind.DFD_CHANGED
                sentence = f"DFD set to {new_dfd} (was {old_dfd})."
            hints.append(
                NarrativeDeltaHint(
                    work_item_id=None,
                    workstream_id=program.id,
                    hint_kind=kind,
                    old_value=old_dfd,
                    new_value=new_dfd,
                    work_item_title=None,
                    suggested_sentence=sentence,
                    confidence=Confidence.HIGH,
                    hint_id=make_hint_id(HintKind.DFD_CHANGED, "dfd"),
                )
            )

        # Escalation state change detection
        if governance.escalation_active != prior_governance.escalation_active:
            if governance.escalation_active:
                kind = HintKind.ESCALATION_CHANGED
                sentence = "LT escalation activated."
                if governance.escalation_workstreams:
                    ws_list = ", ".join(governance.escalation_workstreams[:3])
                    if len(governance.escalation_workstreams) > 3:
                        ws_list += f" (+{len(governance.escalation_workstreams) - 3} more)"
                    sentence = f"LT escalation activated for: {ws_list}."
            else:
                kind = HintKind.ESCALATION_CHANGED
                sentence = "LT escalation deactivated."
            hints.append(
                NarrativeDeltaHint(
                    work_item_id=None,
                    workstream_id=program.id,
                    hint_kind=kind,
                    old_value=str(prior_governance.escalation_active),
                    new_value=str(governance.escalation_active),
                    work_item_title=None,
                    suggested_sentence=sentence,
                    confidence=Confidence.HIGH,
                    hint_id=make_hint_id(HintKind.ESCALATION_CHANGED, "escalation"),
                )
            )

    return hints
