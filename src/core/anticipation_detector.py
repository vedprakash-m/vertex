from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Literal

from src.core.models import Confidence, RiskLevel
from src.core.dependency_graph import dependency_impact_text, dependency_source_label, dependency_target_label
from src.core.models_v2 import Dependency, LeadershipReader, LegacyDependency, Signal
from src.core.trajectory_analyzer import DriftPattern
from src.core.view_models import WorkstreamData


AnticipationPattern = Literal["eta_drift", "risk_escalation", "stale_workstream", "dependency_chain_impact"]

_PATTERN_KEYWORDS: dict[AnticipationPattern, set[str]] = {
    "eta_drift": {"timeline", "eta", "date", "ramp", "slip", "slippage", "execution", "readiness"},
    "risk_escalation": {"risk", "decision", "gate", "blocking", "blocker", "readiness"},
    "stale_workstream": {"update", "updates", "stale", "fresh", "freshness", "coverage", "signal", "signals"},
    "dependency_chain_impact": {"dependency", "dependencies", "cross", "coordination", "execution", "slippage", "readiness"},
}


@dataclass(frozen=True, slots=True)
class AnticipationFinding:
    reader: str
    pattern: AnticipationPattern
    question_seed: str
    suggested_response_seed: str
    evidence: tuple[str, ...]
    confidence: Confidence


def detect_anticipated_questions(
    *,
    readers: tuple[LeadershipReader, ...],
    workstreams: tuple[WorkstreamData, ...],
    drift_patterns: tuple[DriftPattern, ...],
    approved_signals: tuple[Signal, ...],
    summaries: dict[str, str],
    dependencies: tuple[Dependency, ...] = (),
    as_of: datetime | None = None,
    max_questions: int = 5,
) -> tuple[AnticipationFinding, ...]:
    if not readers:
        return ()

    reference_time = _ensure_utc(as_of) if as_of is not None else datetime.now(timezone.utc)
    item_to_workstream = {
        item.id: workstream
        for workstream in workstreams
        for item in workstream.items
    }
    findings: list[AnticipationFinding] = []

    for reader in readers:
        findings.extend(
            _detect_eta_drift_findings(
                reader=reader,
                drift_patterns=drift_patterns,
                item_to_workstream=item_to_workstream,
                summaries=summaries,
            )
        )
        findings.extend(
            _detect_risk_escalation_findings(
                reader=reader,
                workstreams=workstreams,
                summaries=summaries,
            )
        )
        findings.extend(
            _detect_stale_workstream_findings(
                reader=reader,
                workstreams=workstreams,
                approved_signals=approved_signals,
                summaries=summaries,
                as_of=reference_time,
            )
        )
        findings.extend(
            _detect_dependency_findings(
                reader=reader,
                dependencies=dependencies,
                workstreams=workstreams,
                approved_signals=approved_signals,
                summaries=summaries,
            )
        )

    deduped: list[AnticipationFinding] = []
    seen: set[tuple[str, AnticipationPattern, str]] = set()
    for finding in sorted(findings, key=_finding_sort_key):
        key = (finding.reader, finding.pattern, finding.question_seed)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
        if len(deduped) >= max_questions:
            break
    return tuple(deduped)


def _detect_eta_drift_findings(
    *,
    reader: LeadershipReader,
    drift_patterns: tuple[DriftPattern, ...],
    item_to_workstream: dict[int, WorkstreamData],
    summaries: dict[str, str],
) -> tuple[AnticipationFinding, ...]:
    findings: list[AnticipationFinding] = []
    for pattern in drift_patterns:
        if pattern.pattern != "eta_drift":
            continue
        workstream = item_to_workstream.get(pattern.work_item_id)
        if workstream is None:
            continue
        summary = summaries.get(workstream.section_id, "")
        if not _reader_matches(reader, "eta_drift", workstream=workstream, summary=summary):
            continue
        item = next((candidate for candidate in workstream.items if candidate.id == pattern.work_item_id), None)
        item_label = item.title if item is not None else workstream.title
        findings.append(
            AnticipationFinding(
                reader=reader.name,
                pattern="eta_drift",
                question_seed=f"Why has {item_label} slipped {pattern.occurrences} times?",
                suggested_response_seed=(
                    f"Explain the current blocker in {workstream.title}, the next checkpoint, and what changed since the prior date for WI:{pattern.work_item_id}."
                ),
                evidence=(f"WI:{pattern.work_item_id}", f"DRIFT:{pattern.pattern}"),
                confidence=Confidence.HIGH if pattern.severity == "high" else Confidence.MEDIUM,
            )
        )
    return tuple(findings)


def _detect_risk_escalation_findings(
    *,
    reader: LeadershipReader,
    workstreams: tuple[WorkstreamData, ...],
    summaries: dict[str, str],
) -> tuple[AnticipationFinding, ...]:
    findings: list[AnticipationFinding] = []
    for workstream in workstreams:
        if workstream.risk != RiskLevel.HIGH or workstream.prior_risk not in {RiskLevel.LOW, RiskLevel.MEDIUM}:
            continue
        summary = summaries.get(workstream.section_id, "")
        if not _reader_matches(reader, "risk_escalation", workstream=workstream, summary=summary):
            continue
        findings.append(
            AnticipationFinding(
                reader=reader.name,
                pattern="risk_escalation",
                question_seed=f"What changed in {workstream.title} that drove the risk to High?",
                suggested_response_seed=(
                    f"Summarize the blocking condition in {workstream.title}, who owns the next move, and the consequence if the gate stays High."
                ),
                evidence=(
                    f"WS:{workstream.section_id}",
                    f"RISK:{workstream.prior_risk.value}->{workstream.risk.value}",
                ),
                confidence=Confidence.HIGH,
            )
        )
    return tuple(findings)


def _detect_stale_workstream_findings(
    *,
    reader: LeadershipReader,
    workstreams: tuple[WorkstreamData, ...],
    approved_signals: tuple[Signal, ...],
    summaries: dict[str, str],
    as_of: datetime,
) -> tuple[AnticipationFinding, ...]:
    findings: list[AnticipationFinding] = []
    cutoff = as_of - timedelta(days=14)
    for workstream in workstreams:
        if workstream.total_items <= 0:
            continue
        recent_signal_exists = any(
            workstream.section_id in signal.workstream_ids and _ensure_utc(signal.timestamp) >= cutoff
            for signal in approved_signals
        )
        if recent_signal_exists:
            continue
        summary = summaries.get(workstream.section_id, "")
        if not _reader_matches(reader, "stale_workstream", workstream=workstream, summary=summary):
            continue
        findings.append(
            AnticipationFinding(
                reader=reader.name,
                pattern="stale_workstream",
                question_seed=f"Any update on {workstream.title}?",
                suggested_response_seed=(
                    f"Confirm whether {workstream.title} is truly quiet or whether execution moved without new approved signals or ADO evidence in the last 14 days."
                ),
                evidence=(f"WS:{workstream.section_id}", "SIGNALS:none-recent"),
                confidence=Confidence.MEDIUM,
            )
        )
    return tuple(findings)


def _detect_dependency_findings(
    *,
    reader: LeadershipReader,
    dependencies: tuple[Dependency | LegacyDependency, ...],
    workstreams: tuple[WorkstreamData, ...],
    approved_signals: tuple[Signal, ...],
    summaries: dict[str, str],
) -> tuple[AnticipationFinding, ...]:
    findings: list[AnticipationFinding] = []
    search_text = "\n".join(
        entry
        for entry in (
            *(signal.text for signal in approved_signals),
            *(summary for summary in summaries.values()),
            *(workstream.blurb for workstream in workstreams),
        )
        if entry
    )
    normalized_search_text = _normalize_text(search_text)
    for dependency in dependencies:
        source_label = dependency_source_label(dependency)
        target_label = dependency_target_label(dependency)
        impact_text = dependency_impact_text(dependency)
        from_text = _normalize_text(source_label)
        to_text = _normalize_text(target_label)
        if from_text not in normalized_search_text and to_text not in normalized_search_text:
            continue
        matching_workstream = next(
            (
                workstream
                for workstream in workstreams
                if to_text in _normalize_text(workstream.title)
                or to_text in _normalize_text(summaries.get(workstream.section_id, ""))
                or to_text in _normalize_text(workstream.blurb)
            ),
            None,
        )
        if not _reader_matches(
            reader,
            "dependency_chain_impact",
            workstream=matching_workstream,
            summary=(summaries.get(matching_workstream.section_id, "") if matching_workstream is not None else ""),
            extra_text=f"{source_label} {target_label} {impact_text}",
        ):
            continue
        findings.append(
            AnticipationFinding(
                reader=reader.name,
                pattern="dependency_chain_impact",
                question_seed=f"How does {source_label} affect {target_label}?",
                suggested_response_seed=(
                    f"Trace the dependency chain from {source_label} to {target_label}, the next checkpoint, and the consequence if the path slips."
                ),
                evidence=(f"DEP:{source_label}->{target_label}",),
                confidence=Confidence.MEDIUM,
            )
        )
    return tuple(findings)


def _reader_matches(
    reader: LeadershipReader,
    pattern: AnticipationPattern,
    *,
    workstream: WorkstreamData | None,
    summary: str,
    extra_text: str = "",
) -> bool:
    cares_about = tuple(entry for entry in reader.cares_about if entry.strip())
    if not cares_about:
        return True
    haystack = _normalize_text(
        " ".join(
            part
            for part in (
                workstream.title if workstream is not None else "",
                summary,
                workstream.summary if workstream is not None else "",
                extra_text,
            )
            if part
        )
    )
    care_tokens = {token for entry in cares_about for token in _tokenize(entry)}
    if care_tokens & _PATTERN_KEYWORDS[pattern]:
        return True
    return any(_normalize_text(entry) in haystack for entry in cares_about)


def _finding_sort_key(finding: AnticipationFinding) -> tuple[int, str, str]:
    order = {
        Confidence.HIGH: 0,
        Confidence.MEDIUM: 1,
        Confidence.LOW: 2,
        Confidence.NONE: 3,
    }
    return order[finding.confidence], finding.reader, finding.question_seed


def _tokenize(text: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"[^a-z0-9]+", text.strip().lower()) if token)


def _normalize_text(text: str) -> str:
    return " ".join(_tokenize(text))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)