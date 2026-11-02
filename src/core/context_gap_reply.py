"""ADF-W3.7 remainder (specs/arch-data-fix.md Section 11.4, "reply
re-ingestion"): turning a stakeholder's reply into a staged, manually
reviewed context proposal.

The legacy ``ContextGapAnswerProposal`` remains staged, reviewable and
editable, but its in-memory approval does not authorize registry writes.
``apply_context_gap_answer`` refuses application under the existing
manual-diff-only policy. Current reply intake stages pending NCFL proposals
for ``vertex context manual-diff``; it grants no authored-write authority.

Legacy target resolution is narrowed to the subset of context gaps with an
unambiguous field target: a workstream's `deep_context.why`/`.what`/
`.how` field (`context_gap_store.py::_make_fix_hint`'s own naming). Every
other gap type (owner email, KPI validation, generic "review X" gaps) is
still parsed and staged for visibility, but ``resolve_apply_target``
returns ``None`` for them -- there is no single, safe, generic YAML path
to write a co-mention/validation-flag/other-shaped answer into, and
inventing one per gap type is real, separate design work this pass does
not guess at.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from src.core.context_gap_store import RankedGap

ContextGapAnswerStatus = Literal["staged", "approved", "rejected"]

_DEEP_CONTEXT_FIELDS = frozenset({"why", "what", "how"})


class ContextGapReplyError(Exception):
    """Raised when a reply cannot be parsed or a proposal cannot be applied."""


@dataclass(frozen=True, slots=True)
class ParsedReply:
    sender_email: str | None
    subject: str
    body_text: str
    reference_marker: str | None


@dataclass(frozen=True, slots=True)
class ConfigWriteTarget:
    """The legacy proposal target shape: a workstream's deep_context
    sub-field in workstream_registry.yaml."""

    workstream_id: str
    field: Literal["why", "what", "how"]


@dataclass(frozen=True, slots=True)
class ContextGapAnswerProposal:
    id: str
    program_id: str
    solicitation_id: str
    gap_fingerprint: str
    sender_email: str | None
    raw_reply_text: str
    proposed_value: str
    target: ConfigWriteTarget | None
    status: ContextGapAnswerStatus = "staged"
    rejection_reason: str | None = None

    @property
    def is_auto_applicable(self) -> bool:
        return self.target is not None


def _gap_fingerprint(gap: RankedGap) -> str:
    return f"{gap.program}:{gap.feature}:{gap.lane or ''}:{gap.field}"


def resolve_apply_target(gap: RankedGap) -> ConfigWriteTarget | None:
    if gap.lane and gap.field in _DEEP_CONTEXT_FIELDS:
        return ConfigWriteTarget(workstream_id=gap.lane, field=gap.field)  # type: ignore[arg-type]
    return None


def assemble_context_gap_answer_proposal(
    parsed: ParsedReply, *, gap: RankedGap, solicitation_id: str, proposal_id: str
) -> ContextGapAnswerProposal:
    """The reply's own new text (everything above the quoted original,
    already isolated by ``parse_reply_eml``) becomes ``proposed_value``
    verbatim -- no LLM rewriting, no reinterpretation. A human reviews the
    exact text before it is ever written anywhere."""
    return ContextGapAnswerProposal(
        id=proposal_id,
        program_id=gap.program,
        solicitation_id=solicitation_id,
        gap_fingerprint=_gap_fingerprint(gap),
        sender_email=parsed.sender_email,
        raw_reply_text=parsed.body_text,
        proposed_value=parsed.body_text.strip(),
        target=resolve_apply_target(gap),
    )


def approve_context_gap_answer(proposal: ContextGapAnswerProposal) -> ContextGapAnswerProposal:
    if proposal.status == "rejected":
        raise ContextGapReplyError(
            f"ContextGapAnswerProposal {proposal.id!r} was rejected ({proposal.rejection_reason}) -- cannot approve."
        )
    return replace(proposal, status="approved")


def reject_context_gap_answer(proposal: ContextGapAnswerProposal, *, reason: str) -> ContextGapAnswerProposal:
    return replace(proposal, status="rejected", rejection_reason=reason)


def apply_context_gap_answer(proposal: ContextGapAnswerProposal, *, programs_root: Path) -> None:
    """Refuse legacy registry writes; in-memory approval grants no authority."""
    if proposal.status != "approved":
        raise ContextGapReplyError(
            f"ContextGapAnswerProposal {proposal.id!r} has status={proposal.status!r}, not 'approved' -- "
            "only a human-approved answer may be applied."
        )
    if proposal.target is None:
        raise ContextGapReplyError(
            f"ContextGapAnswerProposal {proposal.id!r} has no auto-apply target -- this gap type "
            "requires a manual config edit; there is no safe generic write path for it."
        )
    raise ContextGapReplyError(
        "workstream_registry is manual-diff-only -- legacy proposal approval does not authorize writes; "
        "use vertex context manual-diff for a pending NCFL reply proposal."
    )


__all__ = [
    "ConfigWriteTarget",
    "ContextGapAnswerProposal",
    "ContextGapAnswerStatus",
    "ContextGapReplyError",
    "ParsedReply",
    "apply_context_gap_answer",
    "approve_context_gap_answer",
    "assemble_context_gap_answer_proposal",
    "reject_context_gap_answer",
    "resolve_apply_target",
]
