"""ADF-W2.8 (specs/arch-data-fix.md Section 8.9.4, Appendix A.2): the AI
Release Audit lifecycle -- QG-29's real implementation.

"No AI output is rendered, proposed, persisted, or applied without a
durable terminal authorization record." (Section 8.9.4). This module owns
the immutable lifecycle:

    planned -> requested -> responded -> schema_validated -> semantically_validated
    -> {released | rejected | fallback | discarded}
    -> {rendered | proposed | applied | not_applied | failed}

recording each transition as a registered ledger event
(``ai.run_lifecycle.v1`` / ``ai.release_decision.v1`` /
``ai.application_receipt.v1``, all registered in ADF-W0.18) and enforcing
QG-29 by checking a durable terminal ``released`` decision exists before
consumption. This module does not itself validate lifecycle-state ordering
across calls (that would require reading every prior event for an
``ai_run_id`` on every write, a real perf cost for a property the
append-only ledger already partially guarantees structurally -- no event
can be rewritten); it owns recording and the terminal gate, not sequencing
enforcement.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, read_events, write_event
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.quality_gates.models import GateEvaluation

GATE_ID = "QG-29"


class AIRunState(str, Enum):
    PLANNED = "planned"
    REQUESTED = "requested"
    RESPONDED = "responded"
    SCHEMA_VALIDATED = "schema_validated"
    SEMANTICALLY_VALIDATED = "semantically_validated"


class ReleaseTerminal(str, Enum):
    RELEASED = "released"
    REJECTED = "rejected"
    FALLBACK = "fallback"
    DISCARDED = "discarded"


class ApplicationReceipt(str, Enum):
    RENDERED = "rendered"
    PROPOSED = "proposed"
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    FAILED = "failed"


class AIReleaseAuditError(Exception):
    """QG-29: raised when consumption is attempted without a durable
    ``released`` terminal record for the given ``ai_run_id``."""


def new_ai_run_id() -> str:
    return uuid.uuid4().hex


def record_ai_run_lifecycle(
    *,
    program_id: str,
    ai_run_id: str,
    feature: str,
    state: AIRunState,
    prompt_version: str,
    policy_version: str,
    model_deployment: str = "",
    context_manifest_ref: str = "",
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    now = datetime.now(timezone.utc)
    envelope = build_event_envelope(
        program_id=program_id,
        event_type="ai.run_lifecycle.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ai_release_audit",
        payload={
            "ai_run_id": ai_run_id,
            "feature": feature,
            "state": state.value,
            "prompt_version": prompt_version,
            "policy_version": policy_version,
            "model_deployment": model_deployment,
            "context_manifest_ref": context_manifest_ref,
        },
        source_ref=OperatorAssertionRef(
            asserted_by="system:ai_release_audit", asserted_at=now, context=f"ai_run {ai_run_id} -> {state.value}"
        ),
    )
    write_event(envelope, programs_root=programs_root)


def record_ai_release_decision(
    *,
    program_id: str,
    ai_run_id: str,
    terminal: ReleaseTerminal,
    reason: str,
    validator_finding_count: int,
    released_content_hash: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "ai_run_id": ai_run_id,
        "terminal": terminal.value,
        "reason": reason,
        "validator_finding_count": validator_finding_count,
    }
    if released_content_hash is not None:
        payload["released_content_hash"] = released_content_hash
    envelope = build_event_envelope(
        program_id=program_id,
        event_type="ai.release_decision.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ai_release_audit",
        payload=payload,
        source_ref=OperatorAssertionRef(
            asserted_by="system:ai_release_audit", asserted_at=now, context=f"ai_run {ai_run_id} -> {terminal.value}"
        ),
    )
    write_event(envelope, programs_root=programs_root)


def record_ai_application_receipt(
    *,
    program_id: str,
    ai_run_id: str,
    receipt: ApplicationReceipt,
    artifact_ref: str | None = None,
    proposal_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {"ai_run_id": ai_run_id, "receipt": receipt.value}
    if artifact_ref is not None:
        payload["artifact_ref"] = artifact_ref
    if proposal_id is not None:
        payload["proposal_id"] = proposal_id
    envelope = build_event_envelope(
        program_id=program_id,
        event_type="ai.application_receipt.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ai_release_audit",
        payload=payload,
        source_ref=OperatorAssertionRef(
            asserted_by="system:ai_release_audit", asserted_at=now, context=f"ai_run {ai_run_id} -> {receipt.value}"
        ),
    )
    write_event(envelope, programs_root=programs_root)


def released_terminal_for_run(
    ai_run_id: str, *, program_id: str, programs_root: Path = PROGRAMS_ROOT
) -> ReleaseTerminal | None:
    """The most recently recorded ``ai.release_decision.v1`` terminal for
    this run, or ``None`` if no terminal decision has been recorded yet."""
    events = read_events(program_id, programs_root=programs_root)
    latest: ReleaseTerminal | None = None
    for event in events:
        if event.event_type != "ai.release_decision.v1":
            continue
        if event.payload.get("ai_run_id") != ai_run_id:
            continue
        latest = ReleaseTerminal(event.payload["terminal"])
    return latest


def evaluate_ai_release_gate(
    ai_run_id: str, *, program_id: str, programs_root: Path = PROGRAMS_ROOT
) -> GateEvaluation:
    terminal = released_terminal_for_run(ai_run_id, program_id=program_id, programs_root=programs_root)
    if terminal is ReleaseTerminal.RELEASED:
        return GateEvaluation(
            gate_id=GATE_ID,
            passed=True,
            message=f"ai_run {ai_run_id!r} has a durable 'released' authorization.",
            exit_code=0,
        )
    state = "no terminal decision recorded" if terminal is None else f"terminal={terminal.value}"
    return GateEvaluation(
        gate_id=GATE_ID,
        passed=False,
        message=(
            f"ai_run {ai_run_id!r} for program {program_id!r} has no durable 'released' authorization ({state}) "
            "-- output must not be rendered, proposed, persisted, or applied."
        ),
        exit_code=1,
        forceable=False,
    )


def assert_ai_output_released_or_raise(
    ai_run_id: str, *, program_id: str, programs_root: Path = PROGRAMS_ROOT
) -> None:
    """QG-29: the fail-closed gate itself. Every consumption call site
    (render, propose, persist, apply) must call this immediately before
    consuming an AI output."""
    evaluation = evaluate_ai_release_gate(ai_run_id, program_id=program_id, programs_root=programs_root)
    if not evaluation.passed:
        raise AIReleaseAuditError(evaluation.message)


def is_ai_output_released(ai_run_id: str, *, program_id: str, programs_root: Path = PROGRAMS_ROOT) -> bool:
    return released_terminal_for_run(ai_run_id, program_id=program_id, programs_root=programs_root) is ReleaseTerminal.RELEASED


__all__ = [
    "AIReleaseAuditError",
    "AIRunState",
    "ApplicationReceipt",
    "GATE_ID",
    "ReleaseTerminal",
    "assert_ai_output_released_or_raise",
    "evaluate_ai_release_gate",
    "is_ai_output_released",
    "new_ai_run_id",
    "record_ai_application_receipt",
    "record_ai_release_decision",
    "record_ai_run_lifecycle",
    "released_terminal_for_run",
]
