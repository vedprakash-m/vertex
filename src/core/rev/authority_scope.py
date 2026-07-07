"""REV v1 authority-scope evaluator.

This module is deliberately read-only.  It translates the S-0g decision packet
into executable evidence without flipping authority families or changing the
source-authority policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.core.truth_model import SourceAuthorityPolicy


RevAuthorityStatus = Literal[
    "recommended_v1_authoritative",
    "recommended_unsupported_v1",
]


@dataclass(frozen=True, slots=True)
class RevAuthorityEventSpec:
    claim_event_type: str
    ledger_event_type: str
    fact_type: str | None
    accessor: str | None
    status: RevAuthorityStatus
    reason: str


@dataclass(frozen=True, slots=True)
class RevAuthorityEventAssessment:
    claim_event_type: str
    ledger_event_type: str
    fact_type: str | None
    authority_family: str | None
    accessor: str | None
    status: RevAuthorityStatus
    reason: str


REV_AUTHORITY_EVENT_SPECS: tuple[RevAuthorityEventSpec, ...] = (
    RevAuthorityEventSpec(
        "deployment.completed",
        "milestone.completed.v1",
        "milestone.entry",
        "milestones()",
        "recommended_v1_authoritative",
        "workitem.state admits human_comms as secondary; recommended S-0g accepts after clean-cycle gates",
    ),
    RevAuthorityEventSpec(
        "milestone.completed",
        "milestone.completed.v1",
        "milestone.entry",
        "milestones()",
        "recommended_v1_authoritative",
        "workitem.state admits human_comms as secondary; recommended S-0g accepts after clean-cycle gates",
    ),
    RevAuthorityEventSpec(
        "commitment.date_set",
        "commitment.made.v1",
        "commitment.entry",
        "commitments()",
        "recommended_v1_authoritative",
        "commitment admits human_comms as secondary; recommended S-0g accepts after clean-cycle gates",
    ),
    RevAuthorityEventSpec(
        "ownership.changed",
        "workstream.owner_changed.v1",
        "workstream.entry",
        "workstreams()",
        "recommended_v1_authoritative",
        "workitem.state admits human_comms as secondary; recommended S-0g accepts after clean-cycle gates",
    ),
    RevAuthorityEventSpec(
        "risk.blocking_milestone",
        "risk.raised.v1",
        "risk.entry",
        "risks()",
        "recommended_unsupported_v1",
        "judgment does not admit human_comms; recommended S-0g descopes risk authority to Phase 2",
    ),
    RevAuthorityEventSpec(
        "deployment.rollback",
        "deliverable.status_changed.v1",
        None,
        None,
        "recommended_unsupported_v1",
        "Q9 removes deliverable authority from v1; detected/surfaced only",
    ),
    RevAuthorityEventSpec(
        "deployment.started",
        "deliverable.status_changed.v1",
        None,
        None,
        "recommended_unsupported_v1",
        "Q9 removes deliverable authority from v1; detected/surfaced only",
    ),
    RevAuthorityEventSpec(
        "incident.severity_changed",
        "incident.opened.v1",
        None,
        None,
        "recommended_unsupported_v1",
        "Q9 removes incident authority from v1; detected/surfaced only",
    ),
)


def assess_rev_authority_scope(policy: SourceAuthorityPolicy) -> tuple[RevAuthorityEventAssessment, ...]:
    assessments: list[RevAuthorityEventAssessment] = []
    for spec in REV_AUTHORITY_EVENT_SPECS:
        family = policy.family_map.get(spec.fact_type or "") if spec.fact_type else None
        status = spec.status
        reason = spec.reason
        if status == "recommended_v1_authoritative":
            if family is None:
                status = "recommended_unsupported_v1"
                reason = "fact_type is not present in source_authority.yaml family_map"
            else:
                authority = policy.authority.get(family)
                if authority is None or "human_comms" not in authority.secondary:
                    status = "recommended_unsupported_v1"
                    reason = f"{family} does not admit human_comms in source_authority.yaml"
        assessments.append(
            RevAuthorityEventAssessment(
                claim_event_type=spec.claim_event_type,
                ledger_event_type=spec.ledger_event_type,
                fact_type=spec.fact_type,
                authority_family=family,
                accessor=spec.accessor,
                status=status,
                reason=reason,
            )
        )
    return tuple(assessments)


def recommended_v1_authoritative_claim_types(policy: SourceAuthorityPolicy) -> frozenset[str]:
    return frozenset(
        assessment.claim_event_type
        for assessment in assess_rev_authority_scope(policy)
        if assessment.status == "recommended_v1_authoritative"
    )
