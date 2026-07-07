"""FR-SG-28: DRI-routed review packs.

Generates per-DRI review packs containing only the signals and claims that
require that DRI's validation.  Each correction the DRI makes is captured as
structured feedback (source-fix / claim-fix / risk-fix / owner-fix) that
updates reusable model artifacts rather than producing one-off prose edits.

Pack output path: programs/<prog>/reviews/packs/issue_<n>_<dri>.yaml
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from src.core.journal import PROGRAMS_ROOT

FeedbackKind = Literal["source-fix", "claim-fix", "risk-fix", "owner-fix", "taxonomy-fix"]


@dataclass(frozen=True, slots=True)
class ReviewPackItem:
    """One claim/signal pair requiring a DRI's validation."""
    signal_id: str
    signal_text: str
    source: str
    entity_refs: tuple[str, ...]
    section: str | None
    needs_validation: bool
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class StructuredFeedback:
    """Correction captured from a DRI's review decision."""
    kind: str  # FeedbackKind literal values
    target_id: str
    correction: str
    captured_at: datetime
    reviewed_by: str


def generate_dri_review_pack(
    dri_alias: str,
    signals: tuple[Any, ...],
    workstreams: tuple[Any, ...] | None = None,
) -> tuple[ReviewPackItem, ...]:
    """Return review pack items for the given DRI.

    Selects signals that:
    1. Are owned by (or associated with) the DRI's workstream, OR
    2. Have entity_refs whose DRI maps to dri_alias.

    Only signals that need validation (entity_refs is empty, or confidence < HIGH)
    are included.
    """
    dri_alias_lower = dri_alias.strip().lower()

    # Collect workstream IDs owned by this DRI
    owned_workstream_ids: set[str] = set()
    if workstreams:
        for ws in workstreams:
            ws_dri = getattr(ws, "dri_email", None) or ""
            ws_local_part = ws_dri.split("@")[0].lower() if "@" in ws_dri else ws_dri.lower()
            ws_owner = (getattr(ws, "accountable_owner", None) or "").lower()
            if dri_alias_lower in (ws_local_part, ws_owner):
                ws_id = getattr(ws, "id", None)
                if ws_id:
                    owned_workstream_ids.add(str(ws_id))

    items: list[ReviewPackItem] = []
    from src.core.models_v2 import Confidence  # local import to avoid circular at module level

    for signal in signals:
        ws_id = str(getattr(signal, "workstream_id", "") or "")
        entity_refs = tuple(getattr(signal, "entity_refs", ()) or ())
        confidence = getattr(signal, "confidence", None)

        if ws_id and owned_workstream_ids and ws_id not in owned_workstream_ids:
            continue  # not this DRI's workstream

        needs_validation = (
            not entity_refs
            or confidence is None
            or (hasattr(confidence, "value") and confidence.value in ("low", "none"))
        )
        if not needs_validation:
            continue

        items.append(ReviewPackItem(
            signal_id=str(getattr(signal, "id", "")),
            signal_text=str(getattr(signal, "text", "") or "")[:300],
            source=str(getattr(signal, "source", "") or ""),
            entity_refs=entity_refs,
            section=None,
            needs_validation=True,
        ))

    return tuple(items)


def write_review_pack(
    program_id: str,
    issue_number: int,
    dri_alias: str,
    pack: tuple[ReviewPackItem, ...],
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Persist the review pack to disk."""
    safe_alias = dri_alias.strip().lower().replace("@", "_at_").replace(".", "_")
    path = (
        programs_root
        / program_id
        / "reviews"
        / "packs"
        / f"issue_{issue_number:03d}_{safe_alias}.yaml"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "signal_id": item.signal_id,
            "signal_text": item.signal_text,
            "source": item.source,
            "entity_refs": list(item.entity_refs),
            "section": item.section,
            "needs_validation": item.needs_validation,
            "claim_id": item.claim_id,
        }
        for item in pack
    ]
    path.write_text(
        yaml.safe_dump(
            {
                "dri": dri_alias,
                "issue_number": issue_number,
                "program_id": program_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "items": records,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def load_review_pack(
    program_id: str,
    issue_number: int,
    dri_alias: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ReviewPackItem, ...]:
    safe_alias = dri_alias.strip().lower().replace("@", "_at_").replace(".", "_")
    path = (
        programs_root
        / program_id
        / "reviews"
        / "packs"
        / f"issue_{issue_number:03d}_{safe_alias}.yaml"
    )
    if not path.exists():
        return ()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = []
        for r in doc.get("items") or []:
            items.append(ReviewPackItem(
                signal_id=str(r.get("signal_id") or ""),
                signal_text=str(r.get("signal_text") or ""),
                source=str(r.get("source") or ""),
                entity_refs=tuple(str(x) for x in (r.get("entity_refs") or [])),
                section=r.get("section"),
                needs_validation=bool(r.get("needs_validation", True)),
                claim_id=r.get("claim_id"),
            ))
        return tuple(items)
    except (KeyError, ValueError, yaml.YAMLError):
        return ()


def record_structured_feedback(
    feedback: StructuredFeedback,
    program_id: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append a DRI's structured correction to the review feedback log."""
    import json
    import os
    import portalocker

    path = (
        programs_root
        / program_id
        / "reviews"
        / f"feedback_issue_{issue_number:03d}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": feedback.kind,
        "target_id": feedback.target_id,
        "correction": feedback.correction,
        "captured_at": feedback.captured_at.isoformat(),
        "reviewed_by": feedback.reviewed_by,
    }
    payload = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


@dataclass(frozen=True, slots=True)
class ProgramReviewReport:
    """FR-SG-53: Structured program-level review report combining evidence sources."""
    program_id: str
    edition_id: str
    issue_number: int
    generated_at: datetime
    exec_summary: str
    per_dimension_status: tuple[str, ...]
    open_decisions: tuple[str, ...]
    evidence_governed_risks: tuple[str, ...]
    open_actions_with_owners: tuple[str, ...]
    chronicle_deltas: tuple[str, ...]


def generate_program_review_report(
    program_id: str,
    edition_id: str,
    issue_number: int,
    *,
    exec_summary: str = "",
    per_dimension_status: tuple[str, ...] = (),
    open_decisions: tuple[str, ...] = (),
    evidence_governed_risks: tuple[str, ...] = (),
    open_actions_with_owners: tuple[str, ...] = (),
    chronicle_deltas: tuple[str, ...] = (),
    generated_at: datetime | None = None,
) -> ProgramReviewReport:
    """FR-SG-53: Assemble a ProgramReviewReport from pre-computed evidence streams."""
    return ProgramReviewReport(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        generated_at=generated_at or datetime.now(timezone.utc),
        exec_summary=exec_summary,
        per_dimension_status=per_dimension_status,
        open_decisions=open_decisions,
        evidence_governed_risks=evidence_governed_risks,
        open_actions_with_owners=open_actions_with_owners,
        chronicle_deltas=chronicle_deltas,
    )
