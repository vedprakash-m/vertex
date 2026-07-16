"""ADF-W3.7 remainder (specs/arch-data-fix.md Section 11.4, "reply
re-ingestion"): turning a stakeholder's reply into a staged, human-applied
config-write proposal.

Per the live decisions with the user (Decisions 3 and 3b, 2026-07-13): a
reply is never auto-applied to `workstream_registry.yaml`. It is parsed
into a ``ContextGapAnswerProposal`` -- staged, reviewable, editable -- and
only ``apply_context_gap_answer`` (fired on explicit approval) writes it
back, mirroring every other proposal type built this session
(`RiskProposal`/`MeetingAction`/`TopThreeCandidateProposal`/...). The write
itself is a full ``yaml.safe_load``/``yaml.safe_dump`` round-trip with a
``.bak`` backup, matching `workstream_documents.py::save_workstreams_document`'s
existing precedent for its own file (Decision 3b: no comments exist in the
real registry file, so the round-trip costs only the BOM/exact formatting,
not authored content).

Auto-apply is further narrowed to the subset of context gaps with an
unambiguous, safe write target: a workstream's `deep_context.why`/`.what`/
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
import os
from pathlib import Path
import shutil
from typing import Literal

import yaml

from src.core.context_gap_store import RankedGap
from src.core.workstream_registry import registry_path_for_program

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
    """The one supported auto-apply shape: a workstream's deep_context
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
    """Only fires on an approved, auto-applicable proposal.

    Per the live decision with the user (Decision 3b, 2026-07-13): a full
    ``yaml.safe_load``/``yaml.safe_dump`` round-trip, matching the exact
    safety pattern `workstream_documents.py::save_workstreams_document`
    already uses for its own file (`.bak` backup, atomic temp-then-replace)
    -- applied here to `workstream_registry.yaml` for the first time, since
    no writer existed for it before now (only readers:
    `workstream_registry.py::load_authored_workstream_registry`/
    `load_workstream_registry`). `programs/xpf/workstream_registry.yaml`
    (the real, largest instance) has zero comment lines, so the round-trip's
    only real cosmetic cost is the file's leading BOM and exact formatting,
    not any authored content."""
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
    path = registry_path_for_program(proposal.program_id, programs_root=programs_root)
    if not path.exists():
        raise ContextGapReplyError(f"{path} does not exist -- cannot apply.")
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ContextGapReplyError(f"Expected mapping at top-level in {path} -- cannot apply.")
    workstreams = document.get("workstreams")
    if not isinstance(workstreams, list):
        raise ContextGapReplyError(f"{path} has no 'workstreams' list -- cannot apply.")

    updated = False
    for entry in workstreams:
        if not isinstance(entry, dict) or entry.get("id") != proposal.target.workstream_id:
            continue
        deep_context = entry.setdefault("deep_context", {})
        if not isinstance(deep_context, dict):
            raise ContextGapReplyError(
                f"workstream {proposal.target.workstream_id!r} has a non-mapping 'deep_context' in {path} -- cannot apply."
            )
        deep_context[proposal.target.field] = proposal.proposed_value
        updated = True
        break
    if not updated:
        raise ContextGapReplyError(f"workstream {proposal.target.workstream_id!r} not found in {path} -- cannot apply.")

    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=False), encoding="utf-8")
    os.replace(temp_path, path)


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
