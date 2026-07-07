from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re

from src.core.models import Confidence, WorkItem
from src.core.models_v2 import ClaimEntry, Contradiction, ContradictionPacket, DataSourceType, ForecastCalibrationModifier, ResolvedContradiction, Signal, Workstream
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_workstream_id


_ISO_DATE_PATTERN = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH_DATE_PATTERN = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(20\d{2}))?\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True, slots=True)
class ContradictionRule:
    name: str
    requires: frozenset[DataSourceType]


_CLAIM_VS_ADO_RULE = ContradictionRule(
    name="claim_vs_ado_target_date",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
_SIGNAL_VS_ADO_RULE = ContradictionRule(
    name="signal_vs_ado_target_date",
    requires=frozenset({DataSourceType.ADO}),
)


def build_contradiction_packets(
    *,
    items: tuple[WorkItem, ...],
    claims: tuple[ClaimEntry, ...],
    signals: tuple[Signal, ...],
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    calibration_modifier: ForecastCalibrationModifier | None = None,
) -> tuple[ContradictionPacket, ...]:
    item_lookup = {item.id: item for item in items}
    contradictions_by_item: dict[int, list[Contradiction]] = {}
    recommendations: dict[int, ResolvedContradiction] = {}
    generated_at = _ensure_utc(as_of)

    for claim in claims:
        contradiction = _evaluate_claim_vs_ado_target(
            claim=claim,
            item_lookup=item_lookup,
        )
        if contradiction is None:
            continue
        contradictions_by_item.setdefault(contradiction[0], []).append(contradiction[1])
        recommendation = _recommend_resolution(
            item=item_lookup[contradiction[0]],
            contradiction=contradiction[1],
            workstreams=workstreams,
            calibration_modifier=calibration_modifier,
        )
        if recommendation is not None:
            recommendations[contradiction[0]] = recommendation

    for signal in signals:
        contradiction = _evaluate_signal_vs_ado_target(
            signal=signal,
            item_lookup=item_lookup,
            reference_date=generated_at.date(),
        )
        if contradiction is None:
            continue
        contradictions_by_item.setdefault(contradiction[0], []).append(contradiction[1])
        recommendation = _recommend_resolution(
            item=item_lookup[contradiction[0]],
            contradiction=contradiction[1],
            workstreams=workstreams,
            calibration_modifier=calibration_modifier,
        )
        if recommendation is not None:
            recommendations[contradiction[0]] = recommendation

    packets: list[ContradictionPacket] = []
    for work_item_id, contradictions in sorted(contradictions_by_item.items()):
        item = item_lookup[work_item_id]
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        confidence = max((entry.confidence for entry in contradictions), key=_confidence_rank)
        packets.append(
            ContradictionPacket(
                work_item_id=work_item_id,
                workstream_id=workstream_id,
                contradictions=tuple(sorted(contradictions, key=lambda entry: (entry.field, entry.source_a, entry.source_b, entry.summary))),
                confidence=confidence,
                recommended_resolution=recommendations.get(work_item_id),
                generated_at=generated_at,
            )
        )
    return tuple(packets)


def _evaluate_claim_vs_ado_target(
    *,
    claim: ClaimEntry,
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    item = _first_referenced_item(claim.entity_refs, item_lookup)
    if item is None or item.target_date is None or claim.due_date is None or item.target_date == claim.due_date:
        return None
    days_delta = abs((item.target_date - claim.due_date).days)
    confidence = Confidence.HIGH if days_delta >= 14 else Confidence.MEDIUM
    summary = (
        f"ADO target date {item.target_date.isoformat()} disagrees with open claim due date {claim.due_date.isoformat()}"
    )
    return (
        item.id,
        Contradiction(
            field="target_date",
            source_a="ado/target_date",
            source_b="journal/claim",
            summary=summary,
            confidence=confidence,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _evaluate_signal_vs_ado_target(
    *,
    signal: Signal,
    item_lookup: dict[int, WorkItem],
    reference_date: date,
) -> tuple[int, Contradiction] | None:
    data_source = _signal_source_type(signal.source)
    if data_source is None:
        return None
    item = _first_referenced_item(signal.entity_refs, item_lookup)
    if item is None or item.target_date is None:
        return None
    signal_date = _extract_signal_date(signal, reference_date=reference_date)
    if signal_date is None:
        return None
    days_delta = abs((signal_date - item.target_date).days)
    if days_delta < 7:
        return None
    confidence = Confidence.HIGH if days_delta >= 14 else Confidence.MEDIUM
    return (
        item.id,
        Contradiction(
            field="target_date",
            source_a="ado/target_date",
            source_b=f"{data_source.value}/signal",
            summary=f"{signal.source} implies {signal_date.isoformat()} while ADO target date is {item.target_date.isoformat()}",
            confidence=confidence,
            evidence_refs=tuple(sorted({*signal.entity_refs, signal.id})),
        ),
    )


def _recommend_resolution(
    *,
    item: WorkItem,
    contradiction: Contradiction,
    workstreams: tuple[Workstream, ...],
    calibration_modifier: ForecastCalibrationModifier | None,
) -> ResolvedContradiction | None:
    if calibration_modifier is None:
        return None
    workstream_id = _resolve_workstream_id(item.area_path, workstreams)
    workstream_modifier = 0.0 if workstream_id is None else calibration_modifier.workstream_modifiers.get(workstream_id, 0.0)
    owner_alias = _normalize_alias(item.assigned_to_email or item.assigned_to)
    dri_modifier = 0.0 if owner_alias is None else calibration_modifier.dri_modifiers.get(owner_alias, 0.0)
    applied_modifier = max(workstream_modifier, dri_modifier)
    if applied_modifier < 0.15:
        return None

    winning_source = None
    if contradiction.source_b == "journal/claim":
        winning_source = DataSourceType.ADO
    elif contradiction.source_b.startswith("workiq/"):
        winning_source = DataSourceType.WORKIQ
    elif contradiction.source_b.startswith("kusto/"):
        winning_source = DataSourceType.KUSTO
    if winning_source is None:
        return None

    rationale = f"Calibration applies +{applied_modifier:.2f} slip bias for current owner/workstream, so prefer {winning_source.value}."
    return ResolvedContradiction(
        winning_source=winning_source,
        confidence=Confidence.HIGH if applied_modifier >= 0.18 else Confidence.MEDIUM,
        rationale=rationale,
        evidence_refs=contradiction.evidence_refs,
    )


def _first_referenced_item(entity_refs: tuple[str, ...], item_lookup: dict[int, WorkItem]) -> WorkItem | None:
    for ref in entity_refs:
        if not ref.upper().startswith("WI:"):
            continue
        try:
            item_id = int(ref.split(":", 1)[1])
        except ValueError:
            continue
        item = item_lookup.get(item_id)
        if item is not None:
            return item
    return None


def _signal_source_type(source: str) -> DataSourceType | None:
    normalized = source.strip().lower()
    if normalized.startswith("workiq/"):
        return DataSourceType.WORKIQ
    if normalized.startswith("kusto/"):
        return DataSourceType.KUSTO
    return None


def _extract_signal_date(signal: Signal, *, reference_date: date) -> date | None:
    metadata = signal.metadata or {}
    for key in ("target_date", "due_date", "eta", "reported_target_date"):
        raw_value = metadata.get(key)
        if isinstance(raw_value, str):
            parsed = _extract_date(raw_value, reference_date=reference_date)
            if parsed is not None:
                return parsed
    return _extract_date(signal.text, reference_date=reference_date)


def _extract_date(text: str, *, reference_date: date) -> date | None:
    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match is not None:
        year, month, day = (int(part) for part in iso_match.groups())
        return date(year, month, day)
    month_match = _MONTH_DATE_PATTERN.search(text)
    if month_match is None:
        return None
    month_name, day_text, year_text = month_match.groups()
    year = int(year_text) if year_text is not None else reference_date.year
    month = _MONTHS[month_name.lower()]
    return date(year, month, int(day_text))


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return re.sub(r"[^a-z0-9._-]", "", normalized) or None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _confidence_rank(value: Confidence) -> int:
    ranks = {
        Confidence.NONE: 0,
        Confidence.LOW: 1,
        Confidence.MEDIUM: 2,
        Confidence.HIGH: 3,
    }
    return ranks[value]