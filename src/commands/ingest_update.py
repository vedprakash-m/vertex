"""vertex ingest-update — parse compact update emails and stage ContextUpdateProposals.

γ-Read Phase 3 (§16.2): Parses EML compact update emails (e.g. daily
DD-PF updates) using update_formats.yaml patterns and emits ContextUpdateProposal
objects into the shared NCFL proposal store (programs/<prog>/context_proposals/).

Zone A only. No AI inference. Pattern matching on the known compact-update format.
Apply via vertex context apply (unified per §16.2 Q-X-1 resolution — no separate apply-update).
"""
from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
import re

import typer

from src.core.config_loader import REPORTS_ROOT
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths
from src.core.ncfl_models import ContextUpdateProposal, EXTRACTION_METHOD_CONFIDENCE, NCFL_EXTRACTOR_VERSION
from src.core.ncfl_proposal_store import stage_extracted_proposals
from src.core.yaml_utils import load_yaml_mapping


def _load_update_formats(programs_root: Path, program_id: str) -> list[dict]:
    path = programs_root / program_id / "knowledge" / "update_formats.yaml"
    if not path.exists():
        return []
    doc = load_yaml_mapping(path)
    return list(doc.get("formats") or [])


def _parse_eml_text(path: Path) -> tuple[str, str, str]:
    """Return (subject, sender, body) from an EML file."""
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    subject = str(message.get("subject", "")).strip()
    sender = str(message.get("from", "")).strip()
    body = message.get_body(preferencelist=("plain",))
    if body is not None:
        return subject, sender, body.get_content()
    payload = message.get_payload()
    if isinstance(payload, str):
        return subject, sender, payload
    return subject, sender, ""


def _slugify_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_") or "unknown"


def _hash_value(value: str | None) -> str:
    return sha256((value or "").encode("utf-8")).hexdigest()


def _build_proposal(
    *,
    edition_id: str,
    program_id: str,
    issue_number: int,
    source_artifact: str,
    source_field: str,
    target_key: str,
    target_field: str,
    source_value: str,
) -> ContextUpdateProposal:
    extracted_at = datetime.now(timezone.utc)
    proposal_core = "|".join(
        ("risk_register", target_key, target_field, source_artifact, source_field, source_value)
    )
    conflict_core = "|".join(("risk_register", target_key, target_field))
    return ContextUpdateProposal(
        proposal_id=_hash_value(proposal_core)[:16],
        program_id=program_id,
        issue_number=issue_number,
        edition_id=edition_id,
        source_type="field_update_email",
        extracted_at=extracted_at,
        extractor_version=NCFL_EXTRACTOR_VERSION,
        source_artifact=source_artifact,
        source_field=source_field,
        extraction_method="field_update_email",
        target_store="risk_register",
        target_key=target_key,
        target_field=target_field,
        source_value=source_value,
        current_value=None,
        current_value_hash=None,
        confidence=EXTRACTION_METHOD_CONFIDENCE["field_update_email"],
        batch_eligible=False,
        extraction_method_rationale="Regex matched a compact-update email field using knowledge/update_formats.yaml.",
        conflict_key=_hash_value(conflict_core)[:16],
    )


def _extract_proposals_from_eml(
    *,
    source_path: Path,
    edition_id: str,
    program_id: str,
    issue_number: int,
    programs_root: Path,
) -> tuple[ContextUpdateProposal, ...]:
    formats = _load_update_formats(programs_root, program_id)
    if not formats:
        typer.echo(f"No update_formats.yaml found for {program_id}; nothing to extract.", err=True)
        return ()

    subject, sender, body = _parse_eml_text(source_path)
    proposals: list[ContextUpdateProposal] = []
    source_artifact = str(source_path)

    for fmt in formats:
        sender_pattern = str(fmt.get("sender_pattern", "") or "")
        subject_pattern = str(fmt.get("subject_pattern", "") or "")
        if sender_pattern and not re.search(sender_pattern, sender, re.IGNORECASE):
            continue
        if subject_pattern and not re.search(subject_pattern, subject, re.IGNORECASE):
            continue

        for field_spec in fmt.get("fields") or []:
            if not isinstance(field_spec, dict):
                continue
            label = str(field_spec.get("label", "unknown"))
            ws_key = _slugify_label(label)

            risk_pattern = field_spec.get("extract_risk")
            if isinstance(risk_pattern, str):
                match = re.search(risk_pattern, body, re.IGNORECASE)
                if match is not None and match.lastindex:
                    proposals.append(
                        _build_proposal(
                            edition_id=edition_id,
                            program_id=program_id,
                            issue_number=issue_number,
                            source_artifact=source_artifact,
                            source_field=f"body.{ws_key}.risk",
                            target_key=ws_key,
                            target_field="risk",
                            source_value=match.group(1).strip().lower(),
                        )
                    )

            eta_pattern = field_spec.get("extract_eta")
            if isinstance(eta_pattern, str):
                match = re.search(eta_pattern, body, re.IGNORECASE)
                if match is not None and match.lastindex:
                    proposals.append(
                        _build_proposal(
                            edition_id=edition_id,
                            program_id=program_id,
                            issue_number=issue_number,
                            source_artifact=source_artifact,
                            source_field=f"body.{ws_key}.eta",
                            target_key=ws_key,
                            target_field="eta",
                            source_value=match.group(1).strip(),
                        )
                    )

    deduped: dict[tuple[str, str], ContextUpdateProposal] = {}
    for proposal in proposals:
        deduped[(proposal.conflict_key, proposal.source_value)] = proposal
    return tuple(sorted(deduped.values(), key=lambda entry: (entry.conflict_key, entry.proposal_id)))


def ingest_update_command(
    edition: str = typer.Option(..., "--edition", help="Edition name."),
    source: Path = typer.Option(..., "--source", help="Path to the compact-update EML file."),
    issue: int = typer.Option(..., "--issue", min=1, help="Issue number to stage proposals for."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview proposals without writing."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    reports_root: Path = typer.Option(REPORTS_ROOT, hidden=True),
) -> None:
    """Parse a compact-update EML and stage ContextUpdateProposals (γ-Read Phase 3).

    Apply proposals with: vertex context apply --edition EDITION --proposal-id ID
    """
    del reports_root
    resolved = resolve_edition_paths(edition, programs_root=programs_root)
    if resolved is None:
        raise typer.BadParameter(f"Unknown edition {edition!r}.")

    if not source.exists():
        raise typer.BadParameter(f"Source file not found: {source}")

    proposals = _extract_proposals_from_eml(
        source_path=source,
        edition_id=resolved.edition_id,
        program_id=resolved.program_id,
        issue_number=issue,
        programs_root=programs_root,
    )

    if not proposals:
        typer.echo("No proposals extracted. Check update_formats.yaml and source file format.")
        return

    if dry_run:
        typer.echo(f"Dry run: extracted {len(proposals)} proposal(s) for {edition} issue {issue:03d}.")
        for proposal in proposals:
            typer.echo(
                f"  [{proposal.target_store}.{proposal.target_key}.{proposal.target_field}] "
                f"{proposal.source_value} ({proposal.confidence})"
            )
        return

    staged = stage_extracted_proposals(
        resolved.program_id,
        issue,
        proposals,
        programs_root=programs_root,
    )
    pending = sum(1 for proposal in staged if proposal.status == "pending")
    typer.echo(
        f"Staged {len(proposals)} proposal(s) for {edition} issue {issue:03d}. "
        f"{pending} pending. Apply with: vertex context apply --edition {edition} --issue {issue}"
    )
