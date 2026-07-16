"""ADF-W3.7 (specs/arch-data-fix.md Section 8.10.8, Section 11.4's "Add
context-gap solicitation drafts, cooldown, approval, and reply
re-ingestion"): the draft/cooldown/approval half of the loop.

Targets the pre-existing ``context_gap_store.py`` (`RankedGap`, "Implements
Section 21 of the program-context-maturity spec") -- a context gap
solicitation asks a stakeholder to fill in exactly the missing information
that store already tracks and ranks (missing deep_context.why/what/how,
missing owner email, stale KPI validation, ...), rather than inventing a
second gap-tracking concept.

Vertex never sends anything itself. This module's job ends at writing a
real, reviewable ``.eml`` draft (``X-Unsent: 1``, via the same
``eml_writer.py::build_eml_bytes``/``write_eml_atomic`` the existing hygiene
nudges use) into ``NudgePaths.drafts_dir`` -- the SAME folder the existing
``vertex nudge --list-drafts``/``--approve-draft``/``--mark-sent`` CLI
already manages. A solicitation draft is reviewed, approved, and (manually,
by the operator, in their own mail client) sent through that exact
pre-existing flow; no new send path is introduced.

"Reply re-ingestion" (parsing an inbound reply and closing the gap as a
verified fact) is a distinct, larger piece deliberately not attempted here
-- see the module docstring's final paragraph.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

from src.core.context_gap_store import RankedGap
from src.core.edition_resolver import PROGRAMS_ROOT, get_nudge_paths
from src.core.eml_writer import build_eml_bytes, write_eml_atomic
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.nudge_models import ResolvedRecipient

SolicitationStatus = Literal["staged", "approved", "rejected"]

_DEFAULT_COOLDOWN_DAYS = 14  # matches INV-14's nudge cooldown convention


class ContextGapSolicitationError(Exception):
    """Raised when a solicitation cannot be generated or drafted."""


def _gap_fingerprint(gap: RankedGap) -> str:
    return f"{gap.program}:{gap.feature}:{gap.lane or ''}:{gap.field}"


@dataclass(frozen=True, slots=True)
class ContextGapSolicitation:
    id: str
    program_id: str
    gap_fingerprint: str
    recipient: ResolvedRecipient
    current_gap: str
    why_it_matters: str
    requested_action: str
    evidence_link: str
    generation_method: Literal["deterministic", "llm"]
    status: SolicitationStatus = "staged"
    rejection_reason: str | None = None


def generate_deterministic_solicitation(
    gap: RankedGap, *, recipient: ResolvedRecipient, evidence_link: str = ""
) -> ContextGapSolicitation:
    """The primary path -- Section 8.10.8: "Deterministic templates remain
    the fallback. AI is used only when it adds audience adaptation or
    synthesis," the same framing ADF-W3.6 already scoped around. Content
    is entirely derived from the gap's own fields (`message`/`fix_hint`),
    nothing invented."""
    lane_text = f" in workstream {gap.lane}" if gap.lane else ""
    current_gap = f"{gap.message}{lane_text} ({gap.count} occurrence(s), first seen {gap.first_seen.date().isoformat()})."
    why_it_matters = (
        f"This is a {gap.impact_estimate}-impact context gap affecting {gap.feature!r} -- "
        "Vertex cannot produce a fully confident output for this field until it is resolved."
    )
    requested_action = gap.fix_hint

    return ContextGapSolicitation(
        id=f"solicitation-{_gap_fingerprint(gap).replace(':', '-')}-{gap.last_seen.strftime('%Y%m%dT%H%M%S')}",
        program_id=gap.program,
        gap_fingerprint=_gap_fingerprint(gap),
        recipient=recipient,
        current_gap=current_gap,
        why_it_matters=why_it_matters,
        requested_action=requested_action,
        evidence_link=evidence_link,
        generation_method="deterministic",
    )


def approve_solicitation(solicitation: ContextGapSolicitation) -> ContextGapSolicitation:
    if solicitation.status == "rejected":
        raise ContextGapSolicitationError(
            f"Solicitation {solicitation.id!r} was rejected ({solicitation.rejection_reason}) -- cannot approve."
        )
    return replace(solicitation, status="approved")


def reject_solicitation(solicitation: ContextGapSolicitation, *, reason: str) -> ContextGapSolicitation:
    return replace(solicitation, status="rejected", rejection_reason=reason)


# ---------------------------------------------------------------------------
# Cooldown -- do not re-solicit the same gap within the window.
# ---------------------------------------------------------------------------


def _cooldown_log_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "_feedback" / "context_gap_solicitations.jsonl"


def is_in_cooldown(
    gap: RankedGap, *, programs_root: Path = PROGRAMS_ROOT, cooldown_days: int = _DEFAULT_COOLDOWN_DAYS, now: datetime | None = None
) -> bool:
    """True if a solicitation for this exact gap fingerprint was already
    drafted within ``cooldown_days``. Checked by the caller before
    generating a new one -- this module does not enforce it implicitly,
    matching ``append_context_gap``'s own explicit-dedup-checked-by-caller
    shape rather than a hidden side effect."""
    path = _cooldown_log_path(gap.program, programs_root=programs_root)
    if not path.exists():
        return False
    reference = now or datetime.now(timezone.utc)
    fingerprint = _gap_fingerprint(gap)
    cutoff = reference.timestamp() - cooldown_days * 86400
    for record in read_jsonl_records(path):
        if record.get("gap_fingerprint") != fingerprint:
            continue
        try:
            drafted_at = datetime.fromisoformat(str(record["drafted_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if drafted_at.timestamp() >= cutoff:
            return True
    return False


def record_solicitation_drafted(
    solicitation: ContextGapSolicitation, *, programs_root: Path = PROGRAMS_ROOT, now: datetime | None = None
) -> None:
    """Appends a cooldown-tracking record. Call once, immediately after
    ``write_solicitation_draft`` succeeds -- this is what
    ``is_in_cooldown`` reads."""
    path = _cooldown_log_path(solicitation.program_id, programs_root=programs_root)
    reference = now or datetime.now(timezone.utc)
    payload = {
        "gap_fingerprint": solicitation.gap_fingerprint,
        "solicitation_id": solicitation.id,
        "drafted_at": reference.isoformat().replace("+00:00", "Z"),
    }
    append_jsonl_line(path, json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Draft writing -- reuses the existing eml_writer.py + NudgePaths.drafts_dir,
# the same folder vertex nudge --list-drafts/--approve-draft/--mark-sent
# already manages. No new send path.
# ---------------------------------------------------------------------------


def write_solicitation_draft(
    solicitation: ContextGapSolicitation,
    *,
    from_email: str | None,
    from_display_name: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> Path:
    if solicitation.status != "approved":
        raise ContextGapSolicitationError(
            f"Solicitation {solicitation.id!r} has status={solicitation.status!r}, not 'approved' -- "
            "only a reviewed and approved solicitation may be drafted for send."
        )
    reference = now or datetime.now(timezone.utc)
    subject = f"[Vertex] Missing info needed: {solicitation.current_gap.split('.')[0].strip()}"
    text_body = (
        f"Hi {solicitation.recipient.display_name or solicitation.recipient.alias},\n\n"
        f"{solicitation.current_gap}\n\n"
        f"{solicitation.why_it_matters}\n\n"
        f"Requested: {solicitation.requested_action}\n"
    )
    if solicitation.evidence_link:
        text_body += f"\nMore detail: {solicitation.evidence_link}\n"
    # ADF-W3.7 remainder: a stable reference line, preserved verbatim in most
    # mail clients' quoted-reply content, is how context_gap_reply_import.py
    # correlates an inbound reply back to this exact solicitation -- reply
    # threading headers (Message-ID/In-Reply-To) are not reliable once a
    # reply is manually exported back to a local .eml by a different client.
    text_body += f"\nReference: {solicitation.id}\n"
    html_body = "<br>".join(line if line else "<br>" for line in text_body.splitlines())

    eml_bytes = build_eml_bytes(
        to=(solicitation.recipient.email,),
        cc=(),
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        from_display_name=from_display_name,
        from_email=from_email,
        generated_at=reference,
        mark_as_draft=True,
    )
    drafts_dir = get_nudge_paths(solicitation.program_id, programs_root=programs_root).drafts_dir
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"{solicitation.id}.eml"
    return write_eml_atomic(draft_path, eml_bytes=eml_bytes)


__all__ = [
    "ContextGapSolicitation",
    "ContextGapSolicitationError",
    "SolicitationStatus",
    "approve_solicitation",
    "generate_deterministic_solicitation",
    "is_in_cooldown",
    "record_solicitation_drafted",
    "reject_solicitation",
    "write_solicitation_draft",
]
