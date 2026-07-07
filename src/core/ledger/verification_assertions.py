"""Append-only ``VerificationAssertion`` ledger (Zone A).

specs/program-context-intelligence.md §5.9. Each verification *check* is an
append-only assertion; the **effective verification state** is *derived* from
the full set of assertions for a candidate (monotonic accumulation, never
mutated). This preserves **QG-DM-2** replay determinism: the projection depends
only on the accepted-event log, not a mutable sidecar whose state could flip
it.

``triage approve`` (§5.9) rejects a candidate unless the effective state is
``human_verified`` or ``source_verified`` — enforced *before*
``_write_candidate_event`` + ``project_program_events()``. The gate is active
only under the ``rev_verified`` profile (§5.9 / rollout §7); under
``legacy_nl`` it is a no-op so the 25 existing ``CandidateEvent`` callsites and
the current ``triage_approve`` flow are unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records, validate_jsonl_row
from src.core.ledger.candidate_store import PROGRAMS_ROOT, get_candidate_dir
from src.core.ledger.rev_evidence import evidence_refs_from_dict, evidence_refs_to_dict


# --- check_type vocabulary (§5.9 layered checks) ---
CHECK_QUOTE_SPAN = "quote_span"
CHECK_ENTAILMENT = "entailment"
CHECK_ENTITY_DATE_VALUE = "entity_date_value"
CHECK_GROUNDEDNESS = "groundedness"
CHECK_MATERIALITY = "materiality"
CHECK_HUMAN = "human"

# --- status vocabulary ---
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ADVISORY = "advisory"
STATUS_DEFERRED = "deferred"

# --- effective verification states (derived, never stored on the candidate) ---
STATE_UNVERIFIED = "unverified"
STATE_LEGACY_UNVERIFIED = "legacy_unverified"
STATE_SOURCE_VERIFIED = "source_verified"
STATE_HUMAN_VERIFIED = "human_verified"

VERIFIED_STATES = frozenset({STATE_SOURCE_VERIFIED, STATE_HUMAN_VERIFIED})

LEGACY_POLICY_VERSION = "legacy"

ASSERTIONS_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class VerificationAssertion:
    candidate_id: str
    resulting_event_id: str | None
    check_type: str
    status: str
    policy_version: str
    evidence_refs: tuple[str, ...]      # vault hashes supporting this assertion
    set_by: str
    set_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "resulting_event_id": self.resulting_event_id,
            "check_type": self.check_type,
            "status": self.status,
            "policy_version": self.policy_version,
            "evidence_refs": list(self.evidence_refs),
            "set_by": self.set_by,
            "set_at": self.set_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VerificationAssertion":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            resulting_event_id=payload.get("resulting_event_id"),
            check_type=str(payload["check_type"]),
            status=str(payload["status"]),
            policy_version=str(payload["policy_version"]),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", ())),
            set_by=str(payload["set_by"]),
            set_at=datetime.fromisoformat(str(payload["set_at"])).astimezone(timezone.utc),
        )


def _assertions_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidate_dir(program_id, programs_root=programs_root) / "verification_assertions.jsonl"


def append_verification_assertion(
    assertion: VerificationAssertion,
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append one assertion to the append-only ledger (never mutates)."""
    line = json.dumps(assertion.to_dict(), sort_keys=True) + "\n"
    append_jsonl_line(
        _assertions_path(program_id, programs_root=programs_root),
        line,
        max_bytes=ASSERTIONS_MAX_BYTES,
    )


def append_verification_assertions(
    assertions: tuple[VerificationAssertion, ...],
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    for assertion in assertions:
        append_verification_assertion(assertion, program_id=program_id, programs_root=programs_root)


def load_verification_assertions(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[VerificationAssertion, ...]:
    path = _assertions_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    rows = read_jsonl_records(path)
    out: list[VerificationAssertion] = []
    for row in rows:
        validate_jsonl_row(
            row,
            ("candidate_id", "check_type", "status", "policy_version", "set_by", "set_at"),
            field_name="verification_assertion",
        )
        out.append(VerificationAssertion.from_dict(row))
    return tuple(out)


def assertions_for_candidate(
    program_id: str,
    candidate_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[VerificationAssertion, ...]:
    return tuple(
        a for a in load_verification_assertions(program_id, programs_root=programs_root)
        if a.candidate_id == candidate_id
    )


def effective_verification_state(assertions: tuple[VerificationAssertion, ...]) -> str:
    """Derive the effective verification state from a candidate's assertions.

    Rules (§5.9):
    * any ``human`` pass → ``human_verified`` (satisfies material requirement).
    * a ``legacy`` deferred assertion with no other pass → ``legacy_unverified``.
    * a ``materiality`` pass (claim determined material) without a ``human`` pass
      → ``unverified`` (material claims require human).
    * ``quote_span`` pass + (``entailment`` or ``entity_date_value``) pass and no
      material requirement → ``source_verified`` (non-material path).
    * any ``fail`` on a gating check (quote_span / entity_date_value / human for
      material) → ``unverified``.
    * ``groundedness`` is advisory — never changes state.
    """
    if not assertions:
        return STATE_UNVERIFIED
    has_human_pass = any(a.check_type == CHECK_HUMAN and a.status == STATUS_PASS for a in assertions)
    is_legacy = any(a.policy_version == LEGACY_POLICY_VERSION for a in assertions)
    materiality_required = any(a.check_type == CHECK_MATERIALITY and a.status == STATUS_PASS for a in assertions)
    quote_pass = any(a.check_type == CHECK_QUOTE_SPAN and a.status == STATUS_PASS for a in assertions)
    consistency_pass = any(
        a.check_type in (CHECK_ENTAILMENT, CHECK_ENTITY_DATE_VALUE) and a.status == STATUS_PASS
        for a in assertions
    )
    gating_fail = any(
        a.check_type in (CHECK_QUOTE_SPAN, CHECK_ENTITY_DATE_VALUE, CHECK_HUMAN) and a.status == STATUS_FAIL
        for a in assertions
    )
    if has_human_pass:
        return STATE_HUMAN_VERIFIED
    if materiality_required and not has_human_pass:
        # Material claim without a human pass is not verified (even with quote_span).
        return STATE_UNVERIFIED if gating_fail or not (quote_pass and consistency_pass) else STATE_UNVERIFIED
    if is_legacy and not (quote_pass and consistency_pass):
        return STATE_LEGACY_UNVERIFIED
    if quote_pass and consistency_pass and not gating_fail:
        return STATE_SOURCE_VERIFIED
    return STATE_UNVERIFIED


def is_verified_state(state: str) -> bool:
    return state in VERIFIED_STATES


def is_candidate_verified(
    program_id: str,
    candidate_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> bool:
    state = effective_verification_state(
        assertions_for_candidate(program_id, candidate_id, programs_root=programs_root)
    )
    return is_verified_state(state)


def legacy_assertion(
    candidate_id: str,
    *,
    set_at: datetime | None = None,
) -> VerificationAssertion:
    """The migration assertion instantiated for legacy M365/WorkIQ candidates (§5.9).

    ``effective_state = legacy_unverified`` — not admitted to verified drafting
    without rehydration or explicit human approval.
    """
    return VerificationAssertion(
        candidate_id=candidate_id,
        resulting_event_id=None,
        check_type=CHECK_HUMAN,
        status=STATUS_DEFERRED,
        policy_version=LEGACY_POLICY_VERSION,
        evidence_refs=(),
        set_by="legacy_migration",
        set_at=set_at or datetime.now(timezone.utc),
    )


def human_pass_assertion(
    candidate_id: str,
    *,
    actor: str,
    resulting_event_id: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    policy_version: str = "human.v1",
    set_at: datetime | None = None,
) -> VerificationAssertion:
    """A human-approval assertion (satisfies the material-claim requirement)."""
    return VerificationAssertion(
        candidate_id=candidate_id,
        resulting_event_id=resulting_event_id,
        check_type=CHECK_HUMAN,
        status=STATUS_PASS,
        policy_version=policy_version,
        evidence_refs=evidence_refs,
        set_by=actor,
        set_at=set_at or datetime.now(timezone.utc),
    )


def assertion_state_distribution(
    assertions: tuple[VerificationAssertion, ...],
) -> dict[str, int]:
    """Per-candidate effective-state distribution (FR-PCI-12 / doctor --rev-health)."""
    by_candidate: dict[str, list[VerificationAssertion]] = {}
    for a in assertions:
        by_candidate.setdefault(a.candidate_id, []).append(a)
    dist: dict[str, int] = {}
    for _cid, items in by_candidate.items():
        state = effective_verification_state(tuple(items))
        dist[state] = dist.get(state, 0) + 1
    return dict(sorted(dist.items()))


# Re-export evidence_refs (de)serialization for callers that build assertions.
__all__ = [
    "VerificationAssertion",
    "append_verification_assertion",
    "append_verification_assertions",
    "load_verification_assertions",
    "assertions_for_candidate",
    "effective_verification_state",
    "is_verified_state",
    "is_candidate_verified",
    "legacy_assertion",
    "human_pass_assertion",
    "assertion_state_distribution",
    "evidence_refs_to_dict",
    "evidence_refs_from_dict",
    "CHECK_QUOTE_SPAN",
    "CHECK_ENTAILMENT",
    "CHECK_ENTITY_DATE_VALUE",
    "CHECK_GROUNDEDNESS",
    "CHECK_MATERIALITY",
    "CHECK_HUMAN",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_ADVISORY",
    "STATUS_DEFERRED",
    "STATE_UNVERIFIED",
    "STATE_LEGACY_UNVERIFIED",
    "STATE_SOURCE_VERIFIED",
    "STATE_HUMAN_VERIFIED",
    "VERIFIED_STATES",
    "LEGACY_POLICY_VERSION",
]